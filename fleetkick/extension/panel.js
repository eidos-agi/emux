const $ = (id) => document.getElementById(id);
const BRIDGE = 'http://127.0.0.1:7682';
const TTYD = 'http://127.0.0.1:7681';
const FK = { 'x-fleetkick': '1' };

let current = null;
let attached = false; // the iframe src is set ONCE, ever
// Which browser profile this panel belongs to. The worker owns it; everything the panel
// asks the daemon is scoped by it, so several Chromium browsers can share one daemon
// without seeing each other's sessions or stealing each other's commands.
let INSTALL = null;

// Shared with the service worker via chrome.storage.local, not localStorage — the
// worker computes the toolbar badge from this same "seen" map, and localStorage isn't
// reachable from a worker. Same source of truth, so the badge and the dots agree.
let seen = {};    // session name -> last time it was on screen
let titles = {};  // tabId -> last known title, so closed tabs still read as something

const hydrate = async () => {
  const o = await chrome.storage.local.get(['fk-seen', 'fk-titles']);
  seen = o['fk-seen'] || {};
  titles = o['fk-titles'] || {};
  // Must resolve before the first attach: the install id is the terminal's first ttyd arg,
  // and it namespaces every session this panel can see.
  const r = await chrome.runtime.sendMessage({ fkInstall: true });
  INSTALL = r && r.install;
};
const save = (k, v) => chrome.storage.local.set({ [k]: v });
// Must match the session name the daemon uses, or nothing is ever marked seen. 0.7.0
// renamed sessions to fleetkick-<install>-<tabId> and this kept writing the old
// fleetkick-tab-<tabId> key, so state() looked up a name that never existed and every
// session read as "finished something while you were away", forever.
const markSeen = (tabId) => {
  if (!INSTALL) return;
  seen[`fleetkick-${INSTALL}-${tabId}`] = Date.now() / 1000;
  save('fk-seen', seen);
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
      `· sees ${tabs.length} tab${tabs.length === 1 ? '' : 's'} in ${windows} window${windows === 1 ? '' : 's'}`;
  });
}

// Reloading the iframe drops ttyd's websocket, which makes it fire beforeunload —
// the "Leave site?" prompt on every tab change. Attach once; after that only ever ask
// tmux to swap which session this same live client is showing.
// ttyd passes each `arg` through to boot.sh positionally: $1=install, $2=tabId.
function attach(tabId) {
  const q = new URLSearchParams();
  q.append('arg', INSTALL);
  q.append('arg', tabId);
  attached = true;
  $('term').src = TTYD + '/?' + q.toString();
}

async function show(tabId) {
  markSeen(tabId);
  if (!attached) return attach(tabId);
  try {
    await fetch(BRIDGE + '/switch', {
      method: 'POST',
      headers: { ...FK, 'content-type': 'application/json' },
      body: JSON.stringify({ tabId, install: INSTALL }),
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
// Collapse on any click-away, with no change — opening the picker must never be a
// commitment. A plain document click isn't enough: clicking the terminal lands inside
// the iframe, which swallows the event, so the menu used to stay open over it. Losing
// window focus is the signal that actually covers that case (and clicking out of the
// panel entirely). Escape closes it too.
const closePicker = () => picker.classList.remove('open');
document.addEventListener('click', closePicker);
window.addEventListener('blur', closePicker);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePicker(); });

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

// A daemon restart takes ttyd's websocket with it, permanently. ttyd's own "Press ⏎ to
// Reconnect" can never succeed against a socket whose server is gone — only a fresh
// client can, and tmux still holds the session, so re-attaching resumes exactly where it
// left off. startedAt changing is the only reliable signal that this happened.
let bridgeStartedAt = null;
let needReattach = false;

async function checkRestart() {
  let h;
  try {
    h = await (await fetch(BRIDGE + '/health', { headers: FK })).json();
  } catch {
    return; // daemon down; nothing to re-attach to yet
  }
  if (!h.startedAt) return; // older bridge, no signal to act on
  if (bridgeStartedAt === null) { bridgeStartedAt = h.startedAt; return; }
  if (h.startedAt !== bridgeStartedAt) { bridgeStartedAt = h.startedAt; needReattach = true; }
  if (!needReattach) return;
  // ttyd comes back with the daemon but can lag it. Attaching before it listens leaves a
  // dead error page, and no later tick would notice — so probe first and retry instead.
  try {
    await fetch(TTYD, { mode: 'no-cors' });
  } catch {
    return;
  }
  needReattach = false;
  // about:blank first: re-assigning an identical src isn't a guaranteed reload.
  $('term').src = 'about:blank';
  requestAnimationFrame(() => attach(current ? current.id : ''));
}

async function refresh() {
  try {
    const got = await (await fetch(`${BRIDGE}/sessions?install=${INSTALL}`, { headers: FK })).json();
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

// --- splits ---------------------------------------------------------------------------
// tmux does the splitting; these buttons only say what kind of pane to make. Role picks the
// direction on its own — a manager appears above what it manages, a worker below whoever
// spawned it — so each is one click rather than a direction question every time.
let splitDir = 'v';
const post = (path, payload) => fetch(BRIDGE + path, {
  method: 'POST', headers: { ...FK, 'content-type': 'application/json' },
  body: JSON.stringify(payload),
}).then((r) => r.json());

$('dir').addEventListener('click', () => {
  splitDir = splitDir === 'v' ? 'h' : 'v';
  $('dir').textContent = splitDir === 'v' ? '⬍' : '⬌';
  $('dir').classList.toggle('on', splitDir === 'h');
});

const paneButtons = () => document.querySelectorAll('#panes button, #panes select');

async function addPane(role) {
  if (!current) return;
  paneButtons().forEach((b) => (b.disabled = true));
  try {
    const r = await post('/split', {
      install: INSTALL, tabId: current.id, role, agent: $('agent').value, dir: splitDir,
      pane: targetSel || undefined,
    });
    // Surface the daemon's reason in the bar rather than failing silently — a split that
    // quietly does nothing is the same trap /switch used to set.
    if (r && r.error) $('panes-count').textContent = r.error;
  } catch {
    $('panes-count').textContent = 'bridge down';
  } finally {
    paneButtons().forEach((b) => (b.disabled = false));
    refreshPanes();
  }
}

$('add-manager').addEventListener('click', () => addPane('manager'));
$('add-worker').addEventListener('click', () => addPane('worker'));

async function listPanes() {
  if (!current || !INSTALL) return [];
  try {
    const r = await (await fetch(`${BRIDGE}/panes?install=${INSTALL}&tabId=${current.id}`, { headers: FK })).json();
    return Array.isArray(r) ? r : [];
  } catch {
    return [];
  }
}

// Which pane the buttons act on. '' means "whichever is active", the common case.
let targetSel = '';
let mouseOn = true;

// The rail is the drag surface. tmux can resize by border-drag once mouse mode is on, but
// it has no drag-a-pane-to-a-new-position primitive — and the terminal is a cross-origin
// iframe, so the panel can't reach its pane borders anyway. Rearranging therefore happens
// out here on chips: drag one onto another to swap them, click one to focus it.
function renderRail(list) {
  const rail = $('rail');
  rail.hidden = list.length < 2;
  if (rail.hidden) { rail.textContent = ''; return; }
  rail.textContent = '';
  for (const p of list) {
    const chip = document.createElement('span');
    chip.className = 'chip' + (p.active ? ' active' : '') + (p.pane === targetSel ? ' sel' : '')
      + (p.managerAlive === false ? ' orphan' : '');
    chip.draggable = true;
    chip.textContent = `${p.role === 'manager' ? 'mgr' : 'wkr'} ${p.agent || '?'}`;
    chip.title = p.managerAlive === false
      ? `${p.pane} — its manager pane is gone`
      : `${p.pane} · click to focus · drag onto another to swap`;

    chip.addEventListener('click', async () => {
      targetSel = targetSel === p.pane ? '' : p.pane;
      await post('/select', { pane: p.pane });
      refreshPanes();
    });
    chip.addEventListener('dragstart', (e) => e.dataTransfer.setData('text/plain', p.pane));
    chip.addEventListener('dragover', (e) => { e.preventDefault(); chip.classList.add('over'); });
    chip.addEventListener('dragleave', () => chip.classList.remove('over'));
    chip.addEventListener('drop', async (e) => {
      e.preventDefault();
      chip.classList.remove('over');
      const from = e.dataTransfer.getData('text/plain');
      if (from && from !== p.pane) await post('/swap', { a: from, b: p.pane });
      refreshPanes();
    });
    rail.appendChild(chip);
  }
}

$('mouse').addEventListener('click', async () => {
  if (!current) return;
  mouseOn = !mouseOn;
  $('mouse').classList.toggle('on', mouseOn);
  await post('/mouse', { install: INSTALL, tabId: current.id, on: mouseOn });
});

async function refreshPanes() {
  const list = await listPanes();
  renderRail(list);
  const solo = list.length < 2;
  $('fork').disabled = solo;
  $('close-pane').disabled = solo;   // closing the only pane would kill the session
  $('tidy').disabled = solo;
  $('add-manager').disabled = list.some((p) => p.role === 'manager');
  // Reads as the org chart: m=manager, w=worker, * is the pane you're typing in, and ! is
  // a worker whose manager pane is gone — otherwise the chart would quietly lie.
  const orphan = list.some((p) => p.manager && p.managerAlive === false);
  $('panes-count').textContent = solo ? ''
    : `${list.map((p) => (p.role || '?')[0] + (p.active ? '*' : '') + (p.managerAlive === false ? '!' : '')).join(' ')}`
      + ` · ${list.length} panes${orphan ? ' · orphaned' : ''}`;
}

// The target dropdown resolves to a real pane id, so these act on what you picked rather
// than always on whatever happens to be focused.
const targetPane = async () => targetSel
  || ((await listPanes()).find((p) => p.active) || {}).pane;

$('fork').addEventListener('click', async () => {
  const p = await targetPane();
  if (p) await post('/break', { pane: p });
  refreshPanes();
});

$('close-pane').addEventListener('click', async () => {
  const p = await targetPane();
  if (p) await post('/close', { pane: p });
  targetSel = '';
  refreshPanes();
});

$('tidy').addEventListener('click', async () => {
  if (!current) return;
  await post('/layout', {
    install: INSTALL, tabId: current.id,
    preset: splitDir === 'v' ? 'even-vertical' : 'even-horizontal',
  });
  refreshPanes();
});

// Shows the version actually loaded, so "did my reload take?" is a glance, not a guess.
$('build').textContent = 'fleetkick ' + chrome.runtime.getManifest().version;

// Hydrate before the first paint, or the first render marks everything unseen.
(async () => {
  await hydrate();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  pair(tab);
  scope();
  await refresh();
  setInterval(() => {
    if (current) markSeen(current.id); // whatever is on screen is by definition seen
    checkRestart();
    refresh();
    refreshPanes();
  }, 2000);
})();
