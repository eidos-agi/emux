chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true });

// Fleetkick control plane: long-poll the local bridge for commands from the embedded
// claude's MCP server, execute them with chrome APIs, post results back.
const BRIDGE = 'http://127.0.0.1:7682';
// The bridge rejects anything without this header — a web page can't set it on a
// no-preflight request, so it can't reach the control plane over localhost.
const FK = { 'x-fleetkick': '1' };

// No tabId on the command = act on whatever tab the human is looking at right now.
// The panel is not a tab, so the focused window's active tab is always a real page.
async function activeTab() {
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return t && t.id;
}

async function exec({ op, tabId, args = {} }) {
  if (tabId == null) tabId = await activeTab();
  switch (op) {
    case 'navigate':
      await chrome.tabs.update(tabId, { url: args.url });
      return { ok: true };
    case 'tab_create': {
      const t = await chrome.tabs.create({ url: args.url });
      return { tabId: t.id, windowId: t.windowId };
    }
    case 'tabs_list':
      // Every tab in every window — chrome://, pinned, background, all of it.
      return (await chrome.tabs.query({})).map(t => ({
        id: t.id, windowId: t.windowId, index: t.index, title: t.title, url: t.url,
        active: t.active, pinned: t.pinned, audible: t.audible, discarded: t.discarded,
      }));
    case 'screenshot': {
      const t = await chrome.tabs.get(tabId);
      await chrome.tabs.update(tabId, { active: true });
      const dataUrl = await chrome.tabs.captureVisibleTab(t.windowId, { format: 'png' });
      return { dataUrl };
    }
    case 'read':
      return (await chrome.scripting.executeScript({
        target: { tabId },
        func: () => ({ title: document.title, url: location.href, text: document.body.innerText.slice(0, 30000) }),
      }))[0].result;
    case 'click':
      return (await chrome.scripting.executeScript({
        target: { tabId },
        args: [args.selector],
        func: (sel) => {
          const el = document.querySelector(sel);
          if (!el) return { error: 'no element matches ' + sel };
          el.click();
          return { ok: true };
        },
      }))[0].result;
    case 'type':
      return (await chrome.scripting.executeScript({
        target: { tabId },
        args: [args.selector, args.text],
        func: (sel, text) => {
          const el = document.querySelector(sel);
          if (!el) return { error: 'no element matches ' + sel };
          el.focus();
          el.value = text;
          el.dispatchEvent(new Event('input', { bubbles: true }));
          el.dispatchEvent(new Event('change', { bubbles: true }));
          return { ok: true };
        },
      }))[0].result;
    default:
      return { error: 'unknown op ' + op };
  }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    for (;;) {
      const cmd = await (await fetch(BRIDGE + '/pull', { headers: FK })).json();
      if (cmd && cmd.op) {
        let result;
        try { result = await exec(cmd); } catch (e) { result = { error: String(e) }; }
        await fetch(BRIDGE + '/result', {
          method: 'POST',
          headers: { 'content-type': 'application/json', ...FK },
          body: JSON.stringify({ id: cmd.id, result }),
        });
      }
    }
  } catch {
    // bridge down or fetch aborted — the alarm below restarts us
  } finally {
    polling = false;
  }
}

chrome.runtime.onInstalled.addListener(poll);
chrome.runtime.onStartup.addListener(poll);
chrome.alarms.create('fleetkick-poll', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(poll);
poll();
