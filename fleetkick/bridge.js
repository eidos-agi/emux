#!/usr/bin/env node
// Fleetkick bridge: the embedded agent's MCP server POSTs commands here; the browser
// extension long-polls /pull, executes, and POSTs /result. Localhost only.
// ponytail: no shared token; add one if this ever binds beyond 127.0.0.1.
const http = require('http');
const path = require('path');
const { execFile } = require('child_process');

// 7682 is the real one the extension polls; tests override so a live extension
// can't steal their commands.
const PORT = Number(process.env.FLEETKICK_PORT) || 7682;
const BOOT = path.join(__dirname, 'boot.sh');
const STARTED_AT = Date.now();

// Sessions are fleetkick-<install>-<tabId>[-<slot>]. Three ids, three jobs:
//
//   install  which browser profile. Tab ids are unique only WITHIN a profile, so without
//            it two Chromium browsers holding the same tab id drive the same session.
//   tabId    which tab. One group of agents per tab.
//   slot     which agent within that group. Slot 0 is the tab's original session; 1+ are
//            teammates. Each slot is its own tmux session with its OWN ttyd client and its
//            own iframe, because the PANEL owns the layout — a divider drawn in panel HTML
//            can be dragged and clicked, whereas a tmux pane border lives inside a
//            cross-origin iframe the panel cannot reach. Splitting inside one terminal is
//            what made several agents feel like one crowded window.
const PREFIX = 'fleetkick-';
const IID = /^[0-9a-f]{8}$/;
const NAME = /^fleetkick-([0-9a-f]{8})-([0-9]+)(?:-([0-9]+))?$/;
const MAX_SLOTS = 6;
const sessionName = (install, tabId, slot = 0) =>
  Number(slot) ? `${PREFIX}${install}-${tabId}-${slot}` : `${PREFIX}${install}-${tabId}`;

// NOT a tab. With no controlling terminal (i.e. under launchd, which is how this actually
// runs) tmux sanitizes control characters in list output to '_', so a \t separator silently
// collapses into the field values. Interactive tests never show it, because there tmux has
// a tty and emits the tab intact.
const SEP = '|';

// execFile, never exec — no shell, so an id can't smuggle shell syntax. Belt and braces
// with the format checks at every call site.
const tmux = (args) => new Promise((resolve) =>
  execFile('tmux', args, (err, stdout) => resolve(err ? null : String(stdout).trim())));

// Same, but keeps stderr — worth it where tmux's own message is more useful than a
// generic failure.
const tmuxE = (args) => new Promise((resolve) =>
  execFile('tmux', args, (err, stdout, stderr) =>
    resolve({ ok: !err, out: String(stdout).trim(), err: String(stderr || '').trim() })));

const digits = (v) => /^[0-9]+$/.test(String(v));
const AGENTS = ['claude', 'grok', 'codex', 'gemini'];
const ROLES = ['manager', 'worker', 'solo'];

// Agents get names, not just slot numbers. "tell Sally to check the earnings page" is how
// you actually think about a team; "send_to_pane %53" is not. The name is also what the
// agent is told it is called, so teammates can address each other.
// ponytail: a fixed pool cycled by slot. Renaming is one call away when a name fits badly.
const NAME_POOL = ['todd', 'sally', 'marcus', 'nina', 'omar', 'wren'];
const OK_NAME = /^[a-z0-9][a-z0-9_-]{0,23}$/i;

const getOpt = async (target, k) => (await tmux(['show-options', '-v', '-t', target, k])) || '';
const setOpt = (target, k, v) => tmux(['set-option', '-t', target, k, v]);

// Applied on every /switch, not just at creation — anything applied only at creation
// silently skips every session that already exists, which are the ones being used.
// mouse on gives scrollback with the wheel; there are no panes to drag any more, because
// the panel draws the dividers now.
async function applyStyle(target) {
  await tmux(['set-option', '-t', target, 'mouse', 'on']);
  const clients = (await tmux(['list-clients', '-t', target, '-F', '#{client_tty}'])) || '';
  for (const tty of clients.split('\n').filter(Boolean)) {
    await tmux(['refresh-client', '-t', tty]);
  }
}

// session_activity is a unix ts that bumps on output, so "still running" is just "produced
// output very recently" — no process introspection needed.
async function sessions(install) {
  const out = (await tmux(['list-sessions', '-F',
    `#{session_name}${SEP}#{session_activity}${SEP}#{session_attached}`])) || '';
  const now = Date.now() / 1000;
  return out.split('\n').filter(Boolean)
    .map((l) => l.split(SEP))
    .map(([name, activity, attached]) => ({ m: NAME.exec(name || ''), name, activity, attached }))
    .filter(({ m }) => m && (!install || m[1] === install))
    .map(({ m, name, activity, attached }) => ({
      name,
      install: m[1],
      tabId: Number(m[2]),
      slot: Number(m[3] || 0),
      activity: Number(activity),
      attached: attached === '1',
      running: now - Number(activity) < 3,
    }));
}

// Every agent on one tab, in slot order. This is the group the panel renders as a stack of
// terminals, and what an agent sees when it asks who its teammates are.
async function group(install, tabId) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return [];
  const mine = (await sessions(install)).filter((s) => String(s.tabId) === String(tabId));
  const out = [];
  for (const s of mine.sort((a, b) => a.slot - b.slot)) {
    out.push({
      ...s,
      role: (await getOpt(s.name, '@fk_role')) || 'solo',
      agent: (await getOpt(s.name, '@fk_agent')) || 'claude',
      label: (await getOpt(s.name, '@fk_name')) || NAME_POOL[s.slot % NAME_POOL.length],
    });
  }
  return out;
}

async function addAgent(install, tabId, opts = {}) {
  const role = opts.role || 'worker';
  const agent = opts.agent || 'claude';
  if (!IID.test(String(install || ''))) return { error: 'bad install id' };
  if (!digits(tabId)) return { error: 'bad tabId' };
  if (!AGENTS.includes(agent)) return { error: `agent must be one of ${AGENTS.join(', ')}` };
  if (!ROLES.includes(role)) return { error: `role must be one of ${ROLES.join(', ')}` };

  if (opts.name && !OK_NAME.test(String(opts.name))) return { error: 'bad name' };

  const existing = await group(install, tabId);
  if (existing.length >= MAX_SLOTS) {
    return { error: `already ${existing.length} agents on this tab — close one first` };
  }
  // A group has exactly one manager. Stacking managers produced a chain where each believed
  // it ran one neighbour, which is not a hierarchy.
  const boss = existing.find((s) => s.role === 'manager');
  if (role === 'manager' && boss) {
    return { error: `this group already has a manager (slot ${boss.slot}). Close it, or add a worker.` };
  }

  const used = new Set(existing.map((s) => s.slot));
  let slot = 0;
  while (used.has(slot)) slot += 1;
  const target = sessionName(install, tabId, slot);

  // Pick a name nobody in this group is already using, so two agents are never both Todd.
  const taken = new Set(existing.map((s) => String(s.label || '').toLowerCase()));
  let label = String(opts.name || '').toLowerCase();
  if (!label) label = NAME_POOL.find((n) => !taken.has(n)) || `agent${slot}`;
  if (taken.has(label)) return { error: `${label} is already in this group` };

  // --inner matters: without it boot.sh runs its OUTER wrapper, which calls tmux
  // new-session from inside tmux, fails on nesting, and takes the session down with it.
  const r = await tmuxE(['new-session', '-d', '-s', target,
    BOOT, '--inner', install, String(tabId), String(slot), role, agent, label]);
  if (!r.ok) return { error: r.err || 'could not create session' };
  // new-session exits 0 even when its command dies a millisecond later, and tmux then
  // discards the session — which is how a daemon that could not create a single session
  // still answered ok to everything. Confirm it exists before saying so.
  if ((await tmux(['has-session', '-t', target])) === null) {
    return { error: `session died immediately — is ${agent} on the daemon's PATH?` };
  }
  await setOpt(target, '@fk_role', role);
  await setOpt(target, '@fk_agent', agent);
  await setOpt(target, '@fk_slot', String(slot));
  await setOpt(target, '@fk_name', label);
  // Promote the incumbent when a manager arrives, so the group has one head rather than a
  // manager plus a set of agents who think they are solo.
  if (role === 'manager') {
    for (const s of existing) if (s.role !== 'manager') await setOpt(s.name, '@fk_role', 'worker');
  }
  await applyStyle(target);
  return { ok: true, slot, role, agent, name: label, session: target };
}

async function removeAgent(install, tabId, slot) {
  if (!IID.test(String(install || '')) || !digits(tabId) || !digits(slot)) {
    return { error: 'bad target' };
  }
  const target = sessionName(install, tabId, slot);
  const r = await tmuxE(['kill-session', '-t', target]);
  if (!r.ok) return { error: r.err || 'close failed' };
  // Demote leftovers rather than leaving workers reporting to a manager that is gone —
  // an org chart that quietly lies is the failure mode this whole thing keeps hitting.
  const left = await group(install, tabId);
  if (!left.some((s) => s.role === 'manager')) {
    for (const s of left) await setOpt(s.name, '@fk_role', 'solo');
  }
  return { ok: true, closed: slot };
}

// A manager drives a teammate by typing into it. -l sends the text literally, so an
// instruction can never be interpreted as tmux key names.
// who may be a slot number or a name — "tell sally" should just work.
async function sendTo(install, tabId, who, text, enter = true) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return { error: 'bad target' };
  if (typeof text !== 'string') return { error: 'text must be a string' };
  let slot = who;
  if (!digits(who)) {
    const hit = (await group(install, tabId))
      .find((s) => String(s.label).toLowerCase() === String(who || '').toLowerCase());
    if (!hit) return { error: `no agent named ${who} in this group` };
    slot = hit.slot;
  }
  const target = sessionName(install, tabId, slot);
  if ((await tmux(['has-session', '-t', target])) === null) return { error: 'no such agent' };
  if ((await tmux(['send-keys', '-t', target, '-l', text])) === null) return { error: 'send failed' };
  if (enter) await tmux(['send-keys', '-t', target, 'Enter']);
  return { ok: true, slot };
}

// Ensures the tab's slot-0 session exists. Slots 1+ are created explicitly via /add.
async function switchTo(install, tabId) {
  if (!IID.test(String(install || ''))) return { error: 'bad install id' };
  if (!digits(tabId)) return { error: 'bad tabId' };
  const target = sessionName(install, tabId, 0);
  if ((await tmux(['has-session', '-t', target])) === null) {
    await tmux(['new-session', '-d', '-s', target,
      BOOT, '--inner', install, String(tabId), '0']);
    if ((await tmux(['has-session', '-t', target])) === null) {
      return { error: `session ${target} died immediately — is claude on the daemon's PATH?` };
    }
    await setOpt(target, '@fk_agent', 'claude');
    await setOpt(target, '@fk_role', 'solo');
    await setOpt(target, '@fk_name', NAME_POOL[0]);
  }
  await applyStyle(target);
  return { ok: true, session: target };
}

// --- url memory: "you were here before" ----------------------------------------------
//
// tmux sessions outlive the browser, so yesterday's CNBC conversation is still sitting
// there when you come back — but the tab id is new, so nothing connects the two.
// ponytail: last url + a short ring of recent ones. Full history earns a real store.
const URLS_KEPT = 8;

async function recordSeen(install, tabId, url, title) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return { error: 'bad target' };
  if (typeof url !== 'string' || !/^https?:\/\//.test(url)) return { ok: true, skipped: 'not a web url' };
  const target = sessionName(install, tabId, 0);
  if ((await tmux(['has-session', '-t', target])) === null) return { ok: true, skipped: 'no session' };
  const list = (await getOpt(target, '@fk_urls')).split('\n').filter(Boolean);
  if (list[0] !== url) {
    list.unshift(url);
    await setOpt(target, '@fk_urls', [...new Set(list)].slice(0, URLS_KEPT).join('\n'));
  }
  await setOpt(target, '@fk_url', url);
  if (title) await setOpt(target, '@fk_title', String(title).slice(0, 200));
  await setOpt(target, '@fk_seen', String(Math.floor(Date.now() / 1000)));
  return { ok: true };
}

// Structured comparison, deliberately NOT edit distance. Levenshtein calls cnbc.com and
// cnba.com near-identical (one character) while putting cnbc.com/tech/intel far from
// cnbc.com/markets — backwards on exactly the cases that matter. A different host is a
// different site, not a near miss; within a host, agreement is how deep the paths share.
function urlScore(a, b) {
  let A, B;
  try { A = new URL(a); B = new URL(b); } catch { return 0; }
  const host = (u) => u.host.replace(/^www\./, '');
  if (host(A) !== host(B)) return 0;
  const pa = A.pathname.split('/').filter(Boolean);
  const pb = B.pathname.split('/').filter(Boolean);
  let shared = 0;
  while (shared < pa.length && shared < pb.length && pa[shared] === pb[shared]) shared += 1;
  const depth = Math.max(pa.length, pb.length);
  return depth === 0 ? 1 : 0.6 + 0.4 * (shared / depth);
}

async function matchSessions(install, url, excludeTabId) {
  const out = [];
  for (const s of (await sessions(install)).filter((x) => x.slot === 0)) {
    if (String(s.tabId) === String(excludeTabId)) continue;
    const urls = (await getOpt(s.name, '@fk_urls')).split('\n').filter(Boolean);
    if (!urls.length) continue;
    let best = 0;
    for (const u of urls) best = Math.max(best, urlScore(u, url));
    if (best < 0.5) continue;
    out.push({
      ...s,
      score: Number(best.toFixed(2)),
      url: (await getOpt(s.name, '@fk_url')) || urls[0],
      pageTitle: (await getOpt(s.name, '@fk_title')) || null,
      lastSeen: Number(await getOpt(s.name, '@fk_seen')) || 0,
      preview: ((await tmux(['capture-pane', '-t', s.name, '-p', '-S', '-40'])) || '')
        .split('\n').map((l) => l.trim()).filter(Boolean).slice(-3).join(' · ').slice(0, 240),
    });
  }
  return out.sort((x, y) => y.score - x.score || y.lastSeen - x.lastSeen);
}


// --- mailbox: how teammates talk without stepping on each other ----------------------
//
// The obvious way to deliver a worker's report is send-keys into the manager's prompt.
// That is wrong three ways: the report arrives as if the HUMAN typed it, so provenance is
// lost; it is unbounded, so a worker can eat the manager's context in one dump; and it
// types into an input line the human may be using, corrupting what they were writing.
//
// So messages sit in a mailbox and the recipient reads them on its own turn. Stored on the
// tmux session like everything else, which means they survive a daemon restart.
// ponytail: capped ring, no threading. Reply-to earns its keep when someone needs it.
const INBOX_MAX = 20;
const MSG_CHARS = 4000;

async function postMessage(install, tabId, toWho, from, text) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return { error: 'bad target' };
  if (typeof text !== 'string' || !text.trim()) return { error: 'text is required' };
  const g = await group(install, tabId);
  const hit = digits(toWho)
    ? g.find((s) => s.slot === Number(toWho))
    : g.find((s) => String(s.label).toLowerCase() === String(toWho || '').toLowerCase());
  if (!hit) return { error: `no agent "${toWho}" in this group` };

  let list = [];
  try { list = JSON.parse(await getOpt(hit.name, '@fk_inbox') || '[]'); } catch { list = []; }
  const truncated = text.length > MSG_CHARS;
  list.push({
    from: String(from || 'someone'),
    at: Math.floor(Date.now() / 1000),
    text: truncated ? text.slice(0, MSG_CHARS) + '\n…[TRUNCATED]' : text,
  });
  await setOpt(hit.name, '@fk_inbox', JSON.stringify(list.slice(-INBOX_MAX)));
  return { ok: true, to: hit.label, slot: hit.slot, queued: Math.min(list.length, INBOX_MAX), truncated };
}

async function readInbox(install, tabId, slot, drain = true) {
  if (!IID.test(String(install || '')) || !digits(tabId) || !digits(slot)) return { error: 'bad target' };
  const target = sessionName(install, tabId, slot);
  if ((await tmux(['has-session', '-t', target])) === null) return { error: 'no such agent' };
  let list = [];
  try { list = JSON.parse(await getOpt(target, '@fk_inbox') || '[]'); } catch { list = []; }
  if (drain && list.length) await setOpt(target, '@fk_inbox', '[]');
  return { messages: list };
}

async function inboxCounts(install, tabId) {
  const out = {};
  for (const s of await group(install, tabId)) {
    let n = 0;
    try { n = JSON.parse(await getOpt(s.name, '@fk_inbox') || '[]').length; } catch { n = 0; }
    if (n) out[s.label] = n;
  }
  return out;
}

let nextId = 1;
const pending = new Map(); // id -> /cmd response awaiting a result

// Per install, not global: each browser gets its own queue and its own waiting pullers.
const clients = new Map();
const clientFor = (id) => {
  if (!clients.has(id)) clients.set(id, { pullers: [], queue: [], lastPullAt: 0, exec: null });
  return clients.get(id);
};

const send = (res, code, obj) => {
  // Echoing ACAO lets the extension page read replies; it grants a web page nothing, since
  // it still can't get past the Origin and x-fleetkick checks to be answered.
  res.writeHead(code, { 'content-type': 'application/json', 'access-control-allow-origin': '*' });
  res.end(JSON.stringify(obj));
};

const dispatch = (id) => {
  const c = clientFor(id);
  while (c.queue.length && c.pullers.length) {
    const res = c.pullers.shift();
    clearTimeout(res.fkTimer);
    send(res, 200, c.queue.shift());
  }
};

// With one browser connected, omitting the install id is unambiguous and convenient. With
// several it is a coin flip, so refuse and name them rather than act on the wrong browser.
function resolveInstall(want) {
  if (want) return IID.test(String(want)) ? { install: String(want) } : { error: 'bad install id' };
  const live = [...clients.entries()].filter(([, c]) => c.pullers.length).map(([k]) => k);
  if (live.length === 1) return { install: live[0] };
  if (!live.length) return { error: 'no Fleetkick extension is connected' };
  return { error: `${live.length} browsers connected (${live.join(', ')}) — pass install` };
}

const body = (req) => new Promise((resolve) => {
  let data = '';
  req.on('data', (c) => (data += c));
  req.on('end', () => { try { resolve(JSON.parse(data || '{}')); } catch { resolve({}); } });
});

http.createServer(async (req, res) => {
  // Web pages send an http(s) Origin; the extension worker and local curl/MCP don't.
  if (/^https?:/.test(req.headers.origin || '')) return send(res, 403, { error: 'forbidden' });
  // ...but header-less page requests (<img src=.../pull>) carry no Origin at all. A page
  // can't set a custom header without a preflight, and preflights fall through to the 404
  // below with no CORS headers, so requiring one closes that hole without a shared secret.
  if (req.method === 'OPTIONS') {
    res.writeHead(204, {
      'access-control-allow-origin': req.headers.origin || '*',
      'access-control-allow-headers': 'x-fleetkick, content-type',
      'access-control-allow-methods': 'GET, POST, OPTIONS',
      'access-control-max-age': '86400',
    });
    return res.end();
  }
  if (req.headers['x-fleetkick'] !== '1') return send(res, 403, { error: 'forbidden' });

  const url = new URL(req.url, 'http://127.0.0.1');
  const qInstall = url.searchParams.get('install') || '';
  const qTab = url.searchParams.get('tabId') || '';

  // pullers/lastPullAt exist because "the extension is not responding" was indistinguishable
  // from a dozen other faults from outside.
  if (req.method === 'GET' && url.pathname === '/health') {
    const installs = {};
    for (const [id, c] of clients) {
      installs[id] = { pullers: c.pullers.length, lastPullAt: c.lastPullAt, queued: c.queue.length, exec: c.exec };
    }
    return send(res, 200, { ok: true, startedAt: STARTED_AT, installs });
  }

  if (req.method === 'GET' && url.pathname === '/sessions') {
    return send(res, 200, await sessions(IID.test(qInstall) ? qInstall : null));
  }

  if (req.method === 'GET' && url.pathname === '/group') {
    const r = resolveInstall(qInstall);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await group(r.install, qTab));
  }

  if (req.method === 'POST' && url.pathname === '/add') {
    const { tabId, install, role, agent, name } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await addAgent(r.install, tabId, { role, agent, name }));
  }

  if (req.method === 'POST' && url.pathname === '/remove') {
    const { tabId, install, slot } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await removeAgent(r.install, tabId, slot));
  }

  if (req.method === 'POST' && url.pathname === '/send') {
    const { tabId, install, slot, to, text, enter } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await sendTo(r.install, tabId, to ?? slot, text, enter !== false));
  }

  if (req.method === 'POST' && url.pathname === '/post') {
    const { tabId, install, to, from, text } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await postMessage(r.install, tabId, to, from, text));
  }

  if (req.method === 'GET' && url.pathname === '/inbox') {
    const r = resolveInstall(qInstall);
    if (r.error) return send(res, 200, r);
    const slot = url.searchParams.get('slot');
    if (slot === null) return send(res, 200, await inboxCounts(r.install, qTab));
    return send(res, 200, await readInbox(r.install, qTab, slot,
      url.searchParams.get('peek') !== '1'));
  }

  // Theme the whole terminal, not a header strip. tmux window-style takes hex, so the
  // pane's default background and foreground can carry the page's own hue — programs that
  // set their own colours still win, so claude's output stays readable on top of it.
  if (req.method === 'POST' && url.pathname === '/theme') {
    const { tabId, install, slot, bg, fg, accent } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    if (!digits(tabId) || !digits(slot)) return send(res, 200, { error: 'bad target' });
    const HEX = /^#[0-9a-f]{6}$/i;
    if (![bg, fg, accent].every((c) => HEX.test(String(c || '')))) {
      return send(res, 200, { error: 'bg, fg and accent must be #rrggbb' });
    }
    const target = sessionName(r.install, tabId, slot);
    if ((await tmux(['has-session', '-t', target])) === null) return send(res, 200, { error: 'no such agent' });
    const w = (k, v) => tmux(['set-option', '-w', '-t', target, k, v]);
    await w('window-style', `bg=${bg},fg=${fg}`);
    await w('window-active-style', `bg=${bg},fg=${fg}`);
    await w('pane-active-border-style', `fg=${accent}`);
    await w('pane-border-style', `fg=${accent}`);
    await tmux(['set-option', '-t', target, 'message-style', `bg=${accent},fg=${bg}`]);
    await tmux(['set-option', '-t', target, 'mode-style', `bg=${accent},fg=${bg}`]);
    // Repaint attached clients, or the new colours only appear on the next redraw.
    const cl = (await tmux(['list-clients', '-t', target, '-F', '#{client_tty}'])) || '';
    for (const tty of cl.split('\n').filter(Boolean)) await tmux(['refresh-client', '-t', tty]);
    return send(res, 200, { ok: true, slot: Number(slot), bg, fg, accent });
  }

  if (req.method === 'POST' && url.pathname === '/rename') {
    const { tabId, install, slot, name } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    if (!digits(tabId) || !digits(slot)) return send(res, 200, { error: 'bad target' });
    if (!OK_NAME.test(String(name || ''))) return send(res, 200, { error: 'bad name' });
    const label = String(name).toLowerCase();
    const g = await group(r.install, tabId);
    if (g.some((s2) => s2.slot !== Number(slot) && String(s2.label).toLowerCase() === label)) {
      return send(res, 200, { error: `${label} is already in this group` });
    }
    const target = sessionName(r.install, tabId, slot);
    if ((await tmux(['has-session', '-t', target])) === null) return send(res, 200, { error: 'no such agent' });
    await setOpt(target, '@fk_name', label);
    // The running agent already introduced itself under the old name; it learns the new one
    // on its next restart. ponytail: telling it live would mean typing into its prompt.
    return send(res, 200, { ok: true, slot: Number(slot), name: label });
  }

  if (req.method === 'POST' && url.pathname === '/switch') {
    const { tabId, install } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await switchTo(r.install, tabId));
  }

  if (req.method === 'POST' && url.pathname === '/seen') {
    const { tabId, install, url: seenUrl, title } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await recordSeen(r.install, tabId, seenUrl, title));
  }

  if (req.method === 'GET' && url.pathname === '/match') {
    const r = resolveInstall(qInstall);
    if (r.error) return send(res, 200, r);
    const want = url.searchParams.get('url') || '';
    if (!want) return send(res, 200, []);
    return send(res, 200, await matchSessions(r.install, want, qTab));
  }

  if (req.method === 'GET' && url.pathname === '/pull') {
    if (!IID.test(qInstall)) return send(res, 400, { error: 'pull requires a valid install id' });
    const c = clientFor(qInstall);
    c.exec = url.searchParams.get('exec') || null;
    c.lastPullAt = Date.now();
    c.pullers.push(res);
    res.fkTimer = setTimeout(() => {
      const i = c.pullers.indexOf(res);
      if (i >= 0) { c.pullers.splice(i, 1); send(res, 200, {}); }
    }, 20000);
    return dispatch(qInstall);
  }

  if (req.method === 'POST' && url.pathname === '/cmd') {
    const cmd = await body(req);
    const r = resolveInstall(cmd.install);
    if (r.error) return send(res, 200, r);
    delete cmd.install; // routing detail; the extension shouldn't see it as a command field
    cmd.id = nextId++;
    pending.set(cmd.id, res);
    setTimeout(() => {
      if (pending.delete(cmd.id)) {
        send(res, 504, { error: 'Fleetkick extension did not respond — is it loaded and the browser running?' });
      }
    }, 30000);
    clientFor(r.install).queue.push(cmd);
    return dispatch(r.install);
  }

  if (req.method === 'POST' && url.pathname === '/result') {
    const { id, result } = await body(req);
    const waiter = pending.get(id);
    pending.delete(id);
    if (waiter) send(waiter, 200, result ?? {});
    return send(res, 200, { ok: true });
  }

  send(res, 404, { error: 'not found' });
}).listen(PORT, '127.0.0.1', () => console.log('fleetkick bridge on 127.0.0.1:' + PORT));
