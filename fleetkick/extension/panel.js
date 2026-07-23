const $ = (id) => document.getElementById(id);
const BRIDGE = 'http://127.0.0.1:7682';
const TTYD = 'http://127.0.0.1:7681';
const FK = { 'x-fleetkick': '1' };

let current = null;
let attached = false; // the iframe src is set ONCE, ever

// When each session was last on screen. Anything it printed after that is unseen.
const seen = JSON.parse(localStorage.getItem('fk-seen') || '{}');
const markSeen = (tabId) => {
  seen['fleetkick-tab-' + tabId] = Date.now() / 1000;
  localStorage.setItem('fk-seen', JSON.stringify(seen));
};

function bars(tab) {
  $('title').textContent = tab.title || '';
  $('url').textContent = tab.url || '';
  $('ids').textContent = `tab ${tab.id} · window ${tab.windowId}`;
  $('fav').hidden = !tab.favIconUrl;
  if (tab.favIconUrl) $('fav').src = tab.favIconUrl;
}

function scope() {
  chrome.tabs.query({}, (tabs) => {
    const windows = new Set(tabs.map((t) => t.windowId)).size;
    $('scope').textContent =
      `sees ${tabs.length} tab${tabs.length === 1 ? '' : 's'} · ${windows} window${windows === 1 ? '' : 's'}`;
  });
}

// Reloading the iframe would drop ttyd's websocket and trigger its "Leave site?"
// prompt on every tab change. So: attach once, and from then on only ever ask tmux
// to swap which session that same live client is showing.
async function show(tabId) {
  markSeen(tabId);
  if (!attached) {
    attached = true;
    const q = new URLSearchParams();
    q.append('arg', tabId);
    $('term').src = TTYD + '/?' + q.toString();
    return;
  }
  try {
    await fetch(BRIDGE + '/switch', {
      method: 'POST',
      headers: { ...FK, 'content-type': 'application/json' },
      body: JSON.stringify({ tabId }),
    });
  } catch {
    // bridge down; the terminal keeps showing whatever it had
  }
}

function pair(tab) {
  if (!tab || tab.id === undefined) return;
  const changed = !current || current.id !== tab.id;
  current = tab;
  $('dot').classList.remove('dead');
  bars(tab);
  if (changed) show(tab.id);
}

// Blue = it produced output after you last looked at it, and has since gone quiet.
// Still-chattering sessions aren't blue yet: the point is "finished while you were away".
async function refreshSessions() {
  let list;
  try {
    list = await (await fetch(BRIDGE + '/sessions', { headers: FK })).json();
  } catch {
    return;
  }
  if (!Array.isArray(list)) return;

  const tabs = await chrome.tabs.query({});
  const titleOf = new Map(tabs.map((t) => [t.id, t.title]));
  const sel = $('sessions');
  if (document.activeElement === sel) return; // don't yank the list open under the cursor

  sel.innerHTML = '';
  for (const s of list) {
    const unseen = s.activity > (seen[s.name] || 0);
    const blue = unseen && !s.running && s.tabId !== (current && current.id);
    const opt = document.createElement('option');
    opt.value = s.tabId;
    opt.textContent =
      (blue ? '🔵 ' : s.running ? '▸ ' : '   ') +
      (titleOf.get(s.tabId) || s.name) +
      (s.tabId === (current && current.id) ? '  (showing)' : '');
    sel.appendChild(opt);
  }
  if (current) sel.value = String(current.id);
}

$('sessions').addEventListener('change', async (e) => {
  const tabId = Number(e.target.value);
  await show(tabId);
  // Follow the session you picked — the tools act on the selected tab, so the
  // dropdown moving Chrome keeps those two in agreement.
  try {
    const tab = await chrome.tabs.get(tabId);
    chrome.tabs.update(tabId, { active: true });
    chrome.windows.update(tab.windowId, { focused: true });
  } catch {
    // tab is gone; its session lives on and is still selectable
  }
  refreshSessions();
});

chrome.tabs.query({ active: true, lastFocusedWindow: true }, (tabs) => pair(tabs[0]));
chrome.tabs.onActivated.addListener((info) => chrome.tabs.get(info.tabId, pair));
chrome.tabs.onUpdated.addListener((id, _info, tab) => {
  if (current && id === current.id) bars(tab);
  scope();
});
chrome.tabs.onCreated.addListener(scope);
chrome.tabs.onRemoved.addListener((id) => {
  if (current && id === current.id) {
    $('dot').classList.add('dead');
    $('title').textContent = ($('title').textContent || '') + ' (closed)';
  }
  scope();
});

scope();
refreshSessions();
setInterval(() => {
  if (current) markSeen(current.id); // whatever is on screen is by definition seen
  refreshSessions();
}, 2000);
