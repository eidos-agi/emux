const $ = (id) => document.getElementById(id);
const BRIDGE = 'http://127.0.0.1:7682';
const TTYD = 'http://127.0.0.1:7681';
const FK = { 'x-fleetkick': '1' };

let current = null;
let attached = false; // the iframe src is set ONCE, ever

// Shared with the service worker via chrome.storage.local, not localStorage — the
// worker computes the toolbar badge from this same "seen" map, and localStorage isn't
// reachable from a worker. Same source of truth, so the badge and the dots agree.
let seen = {};    // session name -> last time it was on screen
let titles = {};  // tabId -> last known title, so closed tabs still read as something

const hydrate = async () => {
  const o = await chrome.storage.local.get(['fk-seen', 'fk-titles']);
  seen = o['fk-seen'] || {};
  titles = o['fk-titles'] || {};
};
const save = (k, v) => chrome.storage.local.set({ [k]: v });
const markSeen = (tabId) => { seen['fleetkick-tab-' + tabId] = Date.now() / 1000; save('fk-seen', seen); };

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
      `· sees ${tabs.length} tab${tabs.length === 1 ? '' : 's'} in ${windows} window${windows === 1 ? '' : 's'}`;
  });
}

// Reloading the iframe drops ttyd's websocket, which makes it fire beforeunload —
// the "Leave site?" prompt on every tab change. Attach once; after that only ever ask
// tmux to swap which session this same live client is showing.
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
  $('dot').className = 'dot live';
  bars(tab);
  if (tab.title) { titles[tab.id] = tab.title; save('fk-titles', titles); }
  if (changed) { show(tab.id); render(); }
}

const picker = $('picker');
$('trigger').addEventListener('click', (e) => {
  e.stopPropagation();
  picker.classList.toggle('open');
  if (picker.classList.contains('open')) refresh();
});
document.addEventListener('click', () => picker.classList.remove('open'));

let sessions = [];
let liveTabIds = new Set();

// Blue = produced output after you last had it on screen, and has since gone quiet:
// it finished something while you were away. Pulsing green = still working.
function state(s) {
  if (s.tabId === (current && current.id)) return 'live';
  if (s.running) return 'running';
  if (s.activity > (seen[s.name] || 0)) return 'unseen';
  return '';
}

function render() {
  const list = $('list');
  list.textContent = '';

  const unseenCount = sessions.filter((s) => state(s) === 'unseen').length;
  $('count').textContent = sessions.length
    ? (unseenCount ? `${unseenCount} new · ${sessions.length}` : String(sessions.length))
    : '';
  $('trigger-dot').className = 'dot ' + (unseenCount ? 'unseen' : 'live');
  $('trigger-label').textContent =
    (current && (titles[current.id] || current.title)) || 'sessions';

  if (!sessions.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'no sessions yet';
    list.appendChild(empty);
    return;
  }

  for (const s of sessions) {
    const row = document.createElement('div');
    row.className = 'row' + (s.tabId === (current && current.id) ? ' current' : '') +
                    (liveTabIds.has(s.tabId) ? '' : ' gone');

    const dot = document.createElement('span');
    dot.className = 'dot ' + state(s);

    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = titles[s.tabId] || s.name;

    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = s.tabId === (current && current.id) ? 'showing'
                    : !liveTabIds.has(s.tabId) ? 'tab closed'
                    : s.running ? 'working' : '';

    row.append(dot, name, tag);
    row.addEventListener('click', async (e) => {
      e.stopPropagation();
      picker.classList.remove('open');
      await show(s.tabId);
      // Follow the session you picked — the tools act on the selected tab, so moving
      // Chrome keeps the dropdown and the tools in agreement.
      try {
        const tab = await chrome.tabs.get(s.tabId);
        chrome.tabs.update(s.tabId, { active: true });
        chrome.windows.update(tab.windowId, { focused: true });
      } catch {
        // tab is gone; its session lives on and stays selectable
      }
      refresh();
    });
    list.appendChild(row);
  }
}

async function refresh() {
  try {
    const got = await (await fetch(BRIDGE + '/sessions', { headers: FK })).json();
    if (Array.isArray(got)) sessions = got;
  } catch {
    return; // bridge down — keep the last list rather than blanking the UI
  }
  const tabs = await chrome.tabs.query({});
  liveTabIds = new Set(tabs.map((t) => t.id));
  for (const t of tabs) if (t.title) titles[t.id] = t.title;
  save('fk-titles', titles);
  render();
}

chrome.tabs.onActivated.addListener((info) => chrome.tabs.get(info.tabId, pair));
chrome.tabs.onUpdated.addListener((id, _info, tab) => {
  if (current && id === current.id) bars(tab);
  if (tab.title) { titles[id] = tab.title; save('fk-titles', titles); }
  scope();
});
chrome.tabs.onCreated.addListener(scope);
chrome.tabs.onRemoved.addListener((id) => {
  if (current && id === current.id) {
    $('dot').className = 'dot dead';
    $('title').textContent = ($('title').textContent || '') + ' (closed)';
  }
  scope();
});

// The MV3 worker is killed after ~30s idle, and a dead worker stops long-polling the
// bridge, which is why tools "disconnect" whenever you stop touching the terminal.
// An open port keeps it alive; reconnect inside the 5-minute hard cap.
function keepWorkerAlive() {
  const port = chrome.runtime.connect({ name: 'fleetkick-keepalive' });
  port.onDisconnect.addListener(() => setTimeout(keepWorkerAlive, 1000));
  setTimeout(() => port.disconnect(), 4 * 60 * 1000);
}
keepWorkerAlive();

// Hydrate before the first paint, or the first render marks everything unseen.
(async () => {
  await hydrate();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  pair(tab);
  scope();
  await refresh();
  setInterval(() => {
    if (current) markSeen(current.id); // whatever is on screen is by definition seen
    refresh();
  }, 2000);
})();
