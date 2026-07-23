const $ = (id) => document.getElementById(id);
const BRIDGE = 'http://127.0.0.1:7682';
const TTYD = 'http://127.0.0.1:7681';
const FK = { 'x-fleetkick': '1' };

let current = null;
// Which browser profile this panel belongs to. The worker owns it; everything the panel
// asks the daemon is scoped by it, so several Chromium browsers can share one daemon
// without seeing each other's sessions or stealing each other's commands.
let INSTALL = null;

// Shared with the service worker via chrome.storage.local, not localStorage — the worker
// computes the toolbar badge from this same "seen" map, and localStorage isn't reachable
// from a worker. Same source of truth, so the badge and the dots agree.
let seen = {};
let titles = {};

const hydrate = async () => {
  const o = await chrome.storage.local.get(['fk-seen', 'fk-titles']);
  seen = o['fk-seen'] || {};
  titles = o['fk-titles'] || {};
  const r = await chrome.runtime.sendMessage({ fkInstall: true });
  INSTALL = r && r.install;
};
const save = (k, v) => chrome.storage.local.set({ [k]: v });
// Must match the session name the daemon uses, or nothing is ever marked seen.
const markSeen = (tabId) => {
  if (!INSTALL) return;
  seen[`fleetkick-${INSTALL}-${tabId}`] = Date.now() / 1000;
  save('fk-seen', seen);
};

const post = (path, payload) => fetch(BRIDGE + path, {
  method: 'POST', headers: { ...FK, 'content-type': 'application/json' },
  body: JSON.stringify(payload),
}).then((r) => r.json()).catch(() => ({ error: 'bridge down' }));

const get = (path) => fetch(BRIDGE + path, { headers: FK })
  .then((r) => r.json()).catch(() => null);

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

// --- the stack ------------------------------------------------------------------------
//
// One terminal per agent, laid out by the panel. Each slot is its own ttyd client attached
// to its own tmux session, so clicking one focuses it natively and typing goes there — no
// tmux mouse mode required, no borders hidden inside a cross-origin iframe.
//
// Iframes are NEVER re-created for a slot that already exists: setting src again reloads
// the terminal. Only added and removed slots touch the DOM.
const slots = new Map(); // key `${tabId}:${slot}` -> wrapper element
let stackKey = '';       // which tab the stack is currently built for

function slotEl(tabId, s) {
  const wrap = document.createElement('div');
  wrap.className = 'slot';

  const head = document.createElement('header');
  const who = document.createElement('span');
  who.className = 'who';
  const role = document.createElement('span');
  const mail = document.createElement('span');
  mail.className = 'mail';
  const grow = document.createElement('span');
  grow.className = 'grow';
  const ren = document.createElement('button');
  ren.textContent = '✎';
  ren.title = 'rename';
  const del = document.createElement('button');
  del.textContent = '✕';
  del.title = 'close this agent';
  head.append(who, role, mail, grow, ren, del);

  const frame = document.createElement('iframe');
  const q = new URLSearchParams();
  q.append('arg', INSTALL);
  q.append('arg', tabId);
  q.append('arg', s.slot);
  frame.src = `${TTYD}/?${q.toString()}`;
  wrap.append(head, frame);

  ren.addEventListener('click', async () => {
    const name = prompt(`Rename ${s.label} to:`, s.label);
    if (!name) return;
    const r = await post('/rename', { install: INSTALL, tabId, slot: s.slot, name });
    if (r.error) $('roster').textContent = r.error;
    refreshGroup();
  });
  del.addEventListener('click', async () => {
    const r = await post('/remove', { install: INSTALL, tabId, slot: s.slot });
    if (r.error) $('roster').textContent = r.error;
    refreshGroup();
  });

  wrap._parts = { who, role, mail };
  return wrap;
}

function divider() {
  const d = document.createElement('div');
  d.className = 'divider';
  d.addEventListener('mousedown', (e) => {
    e.preventDefault();
    const prev = d.previousElementSibling;
    const next = d.nextElementSibling;
    if (!prev || !next) return;
    const startY = e.clientY;
    const ph = prev.getBoundingClientRect().height;
    const nh = next.getBoundingClientRect().height;
    // Without this the drag dies the instant the cursor crosses a terminal: the iframe
    // swallows mousemove and the parent document never sees it again.
    document.body.classList.add('dragging');
    const move = (ev) => {
      const dy = ev.clientY - startY;
      const p = Math.max(60, ph + dy);
      const n = Math.max(60, nh - dy);
      prev.style.flex = `0 0 ${p}px`;
      next.style.flex = `0 0 ${n}px`;
    };
    const up = () => {
      document.body.classList.remove('dragging');
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
  });
  return d;
}

async function refreshGroup() {
  if (!current || !INSTALL) return;
  const tabId = current.id;
  const list = await get(`/group?install=${INSTALL}&tabId=${tabId}`);
  if (!Array.isArray(list)) return;
  const counts = (await get(`/inbox?install=${INSTALL}&tabId=${tabId}`)) || {};

  // A different tab is a different group of agents, so its terminals are different
  // sessions. ponytail: this reloads the iframes on tab switch. tmux holds every session,
  // so nothing is lost — only the websocket reconnects.
  if (stackKey !== String(tabId)) {
    stackKey = String(tabId);
    slots.clear();
    $('stack').textContent = '';
  }

  const wanted = new Set(list.map((s) => `${tabId}:${s.slot}`));
  for (const [k, el] of slots) {
    if (!wanted.has(k)) { el.remove(); slots.delete(k); }
  }

  for (const s of list) {
    const key = `${tabId}:${s.slot}`;
    let el = slots.get(key);
    if (!el) { el = slotEl(tabId, s); slots.set(key, el); }
    const { who, role, mail } = el._parts;
    who.textContent = s.label;
    role.textContent = `${s.role === 'manager' ? 'manager' : s.role === 'worker' ? 'worker' : 'solo'} · ${s.agent}`;
    const n = counts[s.label] || 0;
    mail.textContent = n ? `✉ ${n}` : '';
    el.classList.toggle('mgr', s.role === 'manager');
    el.classList.toggle('wkr', s.role !== 'manager');
  }

  // Rebuild the ordering with dividers between, reusing the existing elements so no
  // terminal reloads.
  const stack = $('stack');
  const order = list.map((s) => slots.get(`${tabId}:${s.slot}`)).filter(Boolean);
  const desired = [];
  order.forEach((el, i) => { if (i) desired.push(null); desired.push(el); });
  const currentEls = [...stack.children].filter((c) => !c.classList.contains('divider'));
  if (currentEls.length !== order.length || currentEls.some((c, i) => c !== order[i])) {
    stack.textContent = '';
    desired.forEach((el) => stack.appendChild(el || divider()));
  }

  $('add-manager').disabled = list.some((s) => s.role === 'manager');
  $('roster').textContent = list.length < 2 ? ''
    : list.map((s) => s.label + (s.role === 'manager' ? '*' : '')).join(' ');
  markSeen(tabId);
}

$('add-manager').addEventListener('click', () => addAgent('manager'));
$('add-worker').addEventListener('click', () => addAgent('worker'));
$('even').addEventListener('click', () => {
  for (const el of slots.values()) el.style.flex = '1 1 0';
});

async function addAgent(role) {
  if (!current) return;
  const btns = document.querySelectorAll('#tools button, #tools select');
  btns.forEach((b) => (b.disabled = true));
  const r = await post('/add', {
    install: INSTALL, tabId: current.id, role, agent: $('agent').value,
  });
  if (r && r.error) $('roster').textContent = r.error;
  btns.forEach((b) => (b.disabled = false));
  refreshGroup();
}

function pair(tab) {
  if (!tab || tab.id === undefined) return;
  const changed = !current || current.id !== tab.id;
  current = tab;
  $('dot').className = 'dot live';
  bars(tab);
  if (tab.title) { titles[tab.id] = tab.title; save('fk-titles', titles); }
  if (changed) {
    // Make sure the tab has its slot-0 session before the iframe asks ttyd for it.
    post('/switch', { install: INSTALL, tabId: tab.id }).then(refreshGroup);
    render();
  }
  if (tab.url) post('/seen', { install: INSTALL, tabId: tab.id, url: tab.url, title: tab.title });
}

const picker = $('picker');
$('trigger').addEventListener('click', (e) => {
  e.stopPropagation();
  picker.classList.toggle('open');
  if (picker.classList.contains('open')) refresh();
});
// A plain document click isn't enough: clicking a terminal lands inside its iframe, which
// swallows the event, so the menu used to stay open over it. Window blur covers that.
const closePicker = () => picker.classList.remove('open');
document.addEventListener('click', closePicker);
window.addEventListener('blur', closePicker);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePicker(); });

let sessions = [];
let liveTabIds = new Set();

function state(s) {
  if (s.tabId === (current && current.id)) return 'live';
  if (s.running) return 'running';
  if (s.activity > (seen[s.name] || 0)) return 'unseen';
  return '';
}

function render() {
  const list = $('list');
  list.textContent = '';
  const tabs = sessions.filter((s) => s.slot === 0);
  const unseenCount = tabs.filter((s) => state(s) === 'unseen').length;
  $('count').textContent = tabs.length
    ? (unseenCount ? `${unseenCount} new · ${tabs.length}` : String(tabs.length)) : '';
  $('trigger-dot').className = 'dot ' + (unseenCount ? 'unseen' : 'live');
  $('trigger-label').textContent =
    (current && (titles[current.id] || current.title)) || 'sessions';

  if (!tabs.length) {
    const empty = document.createElement('div');
    empty.className = 'empty';
    empty.textContent = 'no sessions yet';
    list.appendChild(empty);
    return;
  }

  for (const s of tabs) {
    const row = document.createElement('div');
    row.className = 'row' + (s.tabId === (current && current.id) ? ' current' : '')
      + (liveTabIds.has(s.tabId) ? '' : ' gone');
    const dot = document.createElement('span');
    dot.className = 'dot ' + state(s);
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = titles[s.tabId] || s.name;
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = s.tabId === (current && current.id) ? 'showing'
      : !liveTabIds.has(s.tabId) ? 'tab closed' : s.running ? 'working' : '';
    row.append(dot, name, tag);
    row.addEventListener('click', async (e) => {
      e.stopPropagation();
      picker.classList.remove('open');
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
  const got = await get(`/sessions?install=${INSTALL}`);
  if (Array.isArray(got)) sessions = got;
  const tabs = await chrome.tabs.query({});
  liveTabIds = new Set(tabs.map((t) => t.id));
  for (const t of tabs) if (t.title) titles[t.id] = t.title;
  save('fk-titles', titles);
  render();
}

chrome.tabs.onActivated.addListener((info) => chrome.tabs.get(info.tabId, pair));
chrome.tabs.onUpdated.addListener((id, _info, tab) => {
  if (current && id === current.id) { bars(tab); current = tab; }
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
function keepWorkerAlive() {
  const port = chrome.runtime.connect({ name: 'fleetkick-keepalive' });
  port.onDisconnect.addListener(() => setTimeout(keepWorkerAlive, 1000));
  setTimeout(() => port.disconnect(), 4 * 60 * 1000);
}
keepWorkerAlive();

$('build').textContent = 'fleetkick ' + chrome.runtime.getManifest().version;

(async () => {
  await hydrate();
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  pair(tab);
  scope();
  await refresh();
  setInterval(() => {
    if (current) markSeen(current.id);
    refresh();
    refreshGroup();
  }, 2000);
})();
