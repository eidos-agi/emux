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
    case 'refresh':
      await chrome.tabs.reload(tabId, { bypassCache: !!args.hard });
      return { ok: true, tabId };
    case 'reload_extension':
      // Answer first — chrome.runtime.reload() tears down this worker mid-flight, so
      // a reply sent after it would never reach the bridge.
      setTimeout(() => chrome.runtime.reload(), 250);
      return { ok: true, note: 'extension reloading' };
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

// --- Tab lifecycle + badge (pattern borrowed from apple-a-day's browser monitor) ---
//
// This worker is killed constantly, so anything it knows must live in chrome.storage,
// not in memory or in the panel's localStorage. Tracking tabs here rather than in the
// panel means Fleetkick still knows what happened while the panel was closed.

const store = {
  async get(k, d) { return (await chrome.storage.local.get(k))[k] ?? d; },
  async set(k, v) { await chrome.storage.local.set({ [k]: v }); },
};

async function rememberTab(tab) {
  if (!tab || tab.id === undefined || !tab.title) return;
  const titles = await store.get('fk-titles', {});
  if (titles[tab.id] === tab.title) return;
  titles[tab.id] = tab.title;
  await store.set('fk-titles', titles);
}

// The badge is the whole point of tracking in the worker: it shows how many sessions
// finished something while you weren't looking, without opening the panel at all.
async function updateBadge() {
  let list = [];
  try {
    list = await (await fetch(BRIDGE + '/sessions', { headers: FK })).json();
  } catch {
    return chrome.action.setBadgeText({ text: '' }); // bridge down — claim nothing
  }
  if (!Array.isArray(list)) return;
  const seen = await store.get('fk-seen', {});
  const n = list.filter((s) => !s.running && s.activity > (seen[s.name] || 0)).length;
  chrome.action.setBadgeText({ text: n ? String(n) : '' });
  chrome.action.setBadgeBackgroundColor({ color: '#4a9eff' });
}

chrome.tabs.onCreated.addListener(rememberTab);
chrome.tabs.onUpdated.addListener((_id, _info, tab) => rememberTab(tab));
chrome.tabs.onActivated.addListener(async (info) => {
  try { await rememberTab(await chrome.tabs.get(info.tabId)); } catch {}
});
chrome.tabs.onRemoved.addListener(async (tabId) => {
  // Keep the title: the session outlives its tab, and "fleetkick-tab-385591919" is
  // useless in a dropdown. The closed-ness is derived from the live tab list instead.
  const closed = await store.get('fk-closed', {});
  closed[tabId] = Date.now() / 1000;
  await store.set('fk-closed', closed);
  updateBadge();
});

chrome.runtime.onInstalled.addListener(() => { poll(); seedTitles(); });
chrome.runtime.onStartup.addListener(() => { poll(); seedTitles(); });
chrome.alarms.create('fleetkick-poll', { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => { poll(); updateBadge(); });

async function seedTitles() {
  for (const tab of await chrome.tabs.query({})) await rememberTab(tab);
  updateBadge();
}

// The panel holds a port open to keep this worker from being killed at ~30s idle —
// a dead worker stops polling, which reads as the tools "disconnecting" the moment
// you stop typing. Re-poll on every (re)connect, since a fresh worker starts cold.
chrome.runtime.onConnect.addListener((port) => {
  poll();
  port.onDisconnect.addListener(() => {});
});

poll();
seedTitles();
