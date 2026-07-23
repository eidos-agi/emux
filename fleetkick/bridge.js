#!/usr/bin/env node
// Fleetkick bridge: the embedded claude's MCP server POSTs commands here; the Chrome
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

// Sessions are fleetkick-<install>-<tabId>. The install id is what lets several Chromium
// browsers share one daemon: tab ids are unique only WITHIN a browser profile, so without
// it two browsers that both happen to hold tab 385592334 would drive the same tmux session,
// each believing it was alone. It also routes commands — a single global queue handed each
// command to whichever browser polled first, which is silently the wrong browser.
const PREFIX = 'fleetkick-';
const IID = /^[0-9a-f]{8}$/;                       // exactly what the extension generates
const NAME = /^fleetkick-([0-9a-f]{8})-([0-9]+)$/; // ignores pre-install-id session names
const sessionName = (install, tabId) => `${PREFIX}${install}-${tabId}`;

// NOT a tab. With no controlling terminal (i.e. under launchd, which is how this
// actually runs) tmux sanitizes control characters in list output to '_', so a \t
// separator silently collapses into the field values. Interactive tests never show
// it, because there tmux has a tty and emits the tab intact.
const SEP = '|';

// execFile, never exec — no shell, so a tabId can't smuggle shell syntax. Belt and
// braces with the format checks at every call site.
const tmux = (args) => new Promise((resolve) =>
  execFile('tmux', args, (err, stdout) => resolve(err ? null : String(stdout).trim())));

// Same as tmux(), but keeps stderr. Worth it where tmux's own message ("no space for new
// pane") is far more useful to show than a generic failure.
const tmuxE = (args) => new Promise((resolve) =>
  execFile('tmux', args, (err, stdout, stderr) =>
    resolve({ ok: !err, out: String(stdout).trim(), err: String(stderr || '').trim() })));

const digits = (v) => /^[0-9]+$/.test(String(v));

// Everything that makes a split usable, applied to every session on every /switch — not
// just at creation, or the sessions you already have keep behaving like the old ones.
//
// mouse on: tmux itself handles border-drag to resize and click to focus. The panel cannot
// do either — the terminal is a cross-origin iframe, so pane borders are not in a DOM it
// can reach. `mouse` is a SESSION option, so other tmux work on this server is untouched.
//
// The border styling exists because tmux's default separator is a thin unstyled line, which
// in a narrow side panel reads as nothing at all. Heavy lines plus a labelled top border
// make the divider obvious AND put each pane's role on screen, so the org chart is visible
// in the terminal rather than only in the panel's rail.
async function applyStyle(target) {
  await tmux(['set-option', '-t', target, 'mouse', 'on']);
  const w = (k, v) => tmux(['set-option', '-w', '-t', target, k, v]);
  await w('pane-border-lines', 'heavy');
  await w('pane-border-style', 'fg=#4a4a4a');
  await w('pane-active-border-style', 'fg=#4a9eff,bold');
  await w('pane-border-status', 'top');
  await w('pane-border-format',
    ' #{?#{==:#{@fk_role},manager},▲ MANAGER,▼ worker} #{@fk_agent} #{pane_id} ');
  // A client that attached BEFORE mouse was turned on never received tmux's mouse-tracking
  // escape sequence, so clicking and dragging kept doing nothing in exactly the sessions
  // that were already open. Refreshing each attached client is what makes it take effect.
  const clients = (await tmux(['list-clients', '-t', target, '-F', '#{client_tty}'])) || '';
  for (const tty of clients.split('\n').filter(Boolean)) {
    await tmux(['refresh-client', '-t', tty]);
  }
}

// Switching the iframe's src would drop the websocket and make ttyd fire its
// beforeunload ("Leave site?") on every tab change. Instead the terminal stays
// connected forever and tmux swaps which session that same client displays.
async function switchTo(install, tabId) {
  if (!IID.test(String(install || ''))) return { error: 'bad install id' };
  if (!digits(tabId)) return { error: 'bad tabId' };
  const target = sessionName(install, tabId);
  if ((await tmux(['has-session', '-t', target])) === null) {
    // --inner matters: without it boot.sh runs its OUTER wrapper, which calls tmux
    // new-session from inside tmux, fails on nesting, and takes the session down with it.
    // ttyd calls boot.sh without --inner on purpose; the bridge must not.
    await tmux(['new-session', '-d', '-s', target, BOOT, '--inner', install, String(tabId)]);
    // new-session exits 0 even when its command dies a millisecond later, and tmux then
    // discards the session — which is how a daemon that could not create a single session
    // still answered ok:true to every /switch. Confirm it exists before saying so.
    if ((await tmux(['has-session', '-t', target])) === null) {
      return { error: `session ${target} died immediately — is claude on the daemon's PATH?` };
    }
    // Record what this pane is running, so a later split can show a complete org chart
    // instead of a worker whose agent reads as null.
    await tmux(['set-option', '-p', '-t', target, '@fk_agent', 'claude']);
  }
  await applyStyle(target);
  // Only ever switch clients belonging to THIS install, or one browser's tab change would
  // yank the terminal out from under another browser's panel.
  const list = (await tmux(['list-clients', '-F', `#{client_tty}${SEP}#{client_session}`])) || '';
  const ttys = list.split('\n').filter(Boolean)
    .map((l) => l.split(SEP))
    .filter(([, session]) => {
      const m = NAME.exec(session || '');
      return m && m[1] === install;
    })
    .map(([tty]) => tty);
  for (const tty of ttys) await tmux(['switch-client', '-c', tty, '-t', target]);
  return { ok: true, session: target, switched: ttys.length };
}

// session_activity is a unix ts that bumps on output, so "still running" is just
// "produced output very recently" — no process introspection needed.
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
      activity: Number(activity),
      attached: attached === '1',
      running: now - Number(activity) < 3,
    }));
}

// --- panes: splits, roles, and who manages whom -------------------------------------
//
// tmux already is the window manager — split-window, join-pane and break-pane give
// unlimited splits, joins and forks for free, and the panel renders whatever tmux shows.
// So the only thing worth building is the part tmux doesn't know: which pane is a manager,
// which is a worker, and who reports to whom.
//
// That model lives in tmux too, as per-pane user options (@fk_role, @fk_manager,
// @fk_agent). No database, no state file, and it survives a daemon restart because tmux
// outlives the daemon. ponytail: if roles ever need history rather than current state,
// that is when they earn a real store.
const AGENTS = ['claude', 'grok', 'codex', 'gemini'];
const ROLES = ['manager', 'worker'];
const PANE = /^%[0-9]+$/;

async function panes(install, tabId) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return [];
  const F = ['#{pane_id}', '#{@fk_role}', '#{@fk_manager}', '#{@fk_agent}',
    '#{pane_active}', '#{pane_width}x#{pane_height}'].join(SEP);
  const out = await tmux(['list-panes', '-t', sessionName(install, tabId), '-F', F]);
  if (out === null) return [];
  const rows = out.split('\n').filter(Boolean).map((l) => {
    const [id, role, manager, agent, active, size] = l.split(SEP);
    return {
      pane: id, role: role || null, manager: manager || null, agent: agent || null,
      active: active === '1', size,
    };
  });
  // tmux never reuses a pane id, so a closed manager leaves its workers pointing at a %N
  // that no longer exists. Report that rather than letting the org chart quietly lie.
  const live = new Set(rows.map((r) => r.pane));
  for (const r of rows) r.managerAlive = r.manager ? live.has(r.manager) : null;
  return rows;
}

const setOpt = (pane, k, v) => tmux(['set-option', '-p', '-t', pane, k, v]);

// role decides direction, which is the whole trick: a manager appears ABOVE the pane it
// takes over (and adopts it), a worker appears BELOW the pane that spawned it.
async function split(install, tabId, opts = {}) {
  const { dir = 'v', agent = 'claude', role = 'worker' } = opts;
  if (!IID.test(String(install || ''))) return { error: 'bad install id' };
  if (!digits(tabId)) return { error: 'bad tabId' };
  if (!AGENTS.includes(agent)) return { error: `agent must be one of ${AGENTS.join(', ')}` };
  if (!ROLES.includes(role)) return { error: `role must be one of ${ROLES.join(', ')}` };
  if (dir !== 'v' && dir !== 'h') return { error: "dir must be 'v' or 'h'" };

  const target = sessionName(install, tabId);
  if ((await tmux(['has-session', '-t', target])) === null) return { error: 'no such session' };

  const before = role === 'manager';   // managers sit above/left of what they manage
  const existing = await panes(install, tabId);
  // Split relative to a NAMED pane when given, not always the active one — otherwise
  // "add a worker below that pane" is impossible to express.
  const wanted = String(opts.pane || '');
  if (wanted && !PANE.test(wanted)) return { error: 'bad pane id' };
  if (wanted && !existing.some((p) => p.pane === wanted)) return { error: `${wanted} is not in this session` };
  const incumbent = wanted || (existing.find((p) => p.active) || existing[0] || {}).pane || '';

  // A team has exactly ONE manager. Stacking managers produced panes that each believed
  // they were in charge of one neighbour, which is a chain, not a hierarchy — and it is
  // what turned a 2-pane demo into five unusable panes.
  const boss = existing.find((p) => p.role === 'manager');
  if (role === 'manager' && boss) {
    return { error: `this group already has a manager (${boss.pane}). Close it first, or add a worker.` };
  }

  const args = ['split-window', dir === 'h' ? '-h' : '-v'];
  if (before) args.push('-b');
  // Workers report to the team's manager when there is one, regardless of which pane they
  // were split from. Reporting to whatever you happened to split from is how the chain grew.
  const parent = before ? '' : (boss ? boss.pane : incumbent);
  args.push('-t', incumbent || target, '-P', '-F', '#{pane_id}',
    BOOT, '--pane', install, String(tabId), role, agent, parent);
  const r = await tmuxE(args);
  // tmux's own message ("no space for new pane") is the useful one — a generic failure
  // here reads as a bug when it is really a full window.
  if (!r.ok || !PANE.test(r.out)) return { error: r.err || 'split failed' };
  const pane = r.out;

  await setOpt(pane, '@fk_role', role);
  await setOpt(pane, '@fk_agent', agent);
  if (before) {
    // A new manager adopts every pane that does not already have one, so the team has a
    // single head rather than one adopted neighbour and a set of orphans.
    await setOpt(pane, '@fk_manager', '');
    for (const p of existing) {
      await setOpt(p.pane, '@fk_manager', pane);
      await setOpt(p.pane, '@fk_role', 'worker');
    }
  } else {
    await setOpt(pane, '@fk_manager', parent);
    if (parent && !boss) await setOpt(parent, '@fk_role', 'manager');
  }
  return { ok: true, pane, role, agent, manager: before ? null : parent };
}

// Creation without deletion is how the window filled with panes nobody could remove.
async function closePane(pane) {
  if (!PANE.test(String(pane || ''))) return { error: 'bad pane id' };
  const r = await tmuxE(['kill-pane', '-t', pane]);
  return r.ok ? { ok: true, closed: pane } : { error: r.err || 'close failed' };
}

// A manager drives its worker by typing into it. -l sends the text literally, so a
// worker's prompt can never be interpreted as tmux key names.
async function paneSend(pane, text, enter = true) {
  if (!PANE.test(String(pane || ''))) return { error: 'bad pane id' };
  if (typeof text !== 'string') return { error: 'text must be a string' };
  if ((await tmux(['send-keys', '-t', pane, '-l', text])) === null) return { error: 'send failed' };
  if (enter) await tmux(['send-keys', '-t', pane, 'Enter']);
  return { ok: true, pane };
}

// --- url memory: "you were here before" ----------------------------------------------
//
// tmux sessions outlive the browser, so yesterday's CNBC conversation is still sitting
// there when you come back — but the tab id is new, so nothing connects the two. Recording
// what each session was looking at is what makes that reconnectable.
//
// Stored on the tmux session, like roles: no database, survives daemon restarts.
// ponytail: last url + a short ring of recent ones. Full history earns a real store.
const URLS_KEPT = 8;

async function recordSeen(install, tabId, url, title) {
  if (!IID.test(String(install || '')) || !digits(tabId)) return { error: 'bad target' };
  if (typeof url !== 'string' || !/^https?:\/\//.test(url)) return { ok: true, skipped: 'not a web url' };
  const target = sessionName(install, tabId);
  if ((await tmux(['has-session', '-t', target])) === null) return { ok: true, skipped: 'no session' };

  const prev = (await tmux(['show-options', '-v', '-t', target, '@fk_urls'])) || '';
  const list = prev.split('\n').filter(Boolean);
  if (list[0] !== url) {
    list.unshift(url);
    await tmux(['set-option', '-t', target, '@fk_urls',
      [...new Set(list)].slice(0, URLS_KEPT).join('\n')]);
  }
  await tmux(['set-option', '-t', target, '@fk_url', url]);
  if (title) await tmux(['set-option', '-t', target, '@fk_title', String(title).slice(0, 200)]);
  await tmux(['set-option', '-t', target, '@fk_seen', String(Math.floor(Date.now() / 1000))]);
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
  while (shared < pa.length && shared < pb.length && pa[shared] === pb[shared]) shared++;
  const depth = Math.max(pa.length, pb.length);
  return depth === 0 ? 1 : 0.6 + 0.4 * (shared / depth);
}

async function matchSessions(install, url, excludeTabId) {
  const mine = await sessions(install);
  const out = [];
  for (const s of mine) {
    if (String(s.tabId) === String(excludeTabId)) continue;
    const target = s.name;
    const urls = ((await tmux(['show-options', '-v', '-t', target, '@fk_urls'])) || '')
      .split('\n').filter(Boolean);
    if (!urls.length) continue;
    // Best of the session's recent urls: a session that wandered off is still the CNBC
    // session if that is where it did its work.
    let best = 0;
    for (const u of urls) best = Math.max(best, urlScore(u, url));
    if (best < 0.5) continue;
    out.push({
      ...s,
      score: Number(best.toFixed(2)),
      url: (await tmux(['show-options', '-v', '-t', target, '@fk_url'])) || urls[0],
      pageTitle: (await tmux(['show-options', '-v', '-t', target, '@fk_title'])) || null,
      lastSeen: Number((await tmux(['show-options', '-v', '-t', target, '@fk_seen'])) || 0),
      // What it looks like you were doing. ponytail: the visible tail, not real intent
      // extraction — reading the transcript would be the real answer.
      preview: ((await tmux(['capture-pane', '-t', target, '-p', '-S', '-40'])) || '')
        .split('\n').map((l) => l.trim()).filter(Boolean).slice(-3).join(' · ').slice(0, 240),
    });
  }
  return out.sort((x, y) => y.score - x.score || y.lastSeen - x.lastSeen);
}

let nextId = 1;
const pending = new Map(); // id -> /cmd response awaiting a result

// Per install, not global: each browser gets its own queue and its own waiting pullers.
const clients = new Map(); // install -> { pullers, queue, lastPullAt, exec }
const clientFor = (id) => {
  if (!clients.has(id)) clients.set(id, { pullers: [], queue: [], lastPullAt: 0, exec: null });
  return clients.get(id);
};

const send = (res, code, obj) => {
  // Echoing ACAO lets the extension page read replies; it grants a web page nothing,
  // since it still can't get past the Origin and x-fleetkick checks to be answered.
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
  // The panel is an extension page, so its fetches preflight. Answer OPTIONS before the
  // header check (a preflight can't carry it) — safe, because any http(s) origin already
  // 403'd above, which is every case a web page could be in.
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

  // pullers/lastPullAt exist because "the extension is not responding" was indistinguishable
  // from a dozen other faults from outside. A waiting puller means that browser's command
  // channel is live; a healthy daemon with a stale lastPullAt means the fault is browser-side.
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

  if (req.method === 'POST' && url.pathname === '/switch') {
    const { tabId, install } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await switchTo(r.install, tabId));
  }

  if (req.method === 'GET' && url.pathname === '/panes') {
    const tabId = url.searchParams.get('tabId');
    const r = resolveInstall(qInstall);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await panes(r.install, tabId));
  }

  if (req.method === 'POST' && url.pathname === '/split') {
    const { tabId, install, dir, agent, role, pane } = await body(req);
    const r = resolveInstall(install);
    if (r.error) return send(res, 200, r);
    return send(res, 200, await split(r.install, tabId, { dir, agent, role, pane }));
  }

  // Precise resizing, for when dragging a border is fiddly in a narrow side panel.
  if (req.method === 'POST' && url.pathname === '/resize') {
    const { pane, dir, amount } = await body(req);
    const flags = { L: '-L', R: '-R', U: '-U', D: '-D' };
    if (!PANE.test(String(pane || ''))) return send(res, 200, { error: 'bad pane id' });
    if (!flags[dir]) return send(res, 200, { error: 'dir must be L, R, U or D' });
    const n = Math.min(Math.max(parseInt(amount, 10) || 3, 1), 40);
    const r2 = await tmuxE(['resize-pane', flags[dir], '-t', pane, String(n)]);
    return send(res, 200, r2.ok ? { ok: true, pane, dir, amount: n } : { error: r2.err || 'resize failed' });
  }

  // Mouse mode is a genuine trade: dragging borders resizes, but drag-to-select-text stops
  // working (that becomes Option-drag). So it is a toggle, not a decision made for you.
  if (req.method === 'POST' && url.pathname === '/mouse') {
    const { tabId, install, on } = await body(req);
    const r2 = resolveInstall(install);
    if (r2.error) return send(res, 200, r2);
    if (!digits(tabId)) return send(res, 200, { error: 'bad tabId' });
    const r3 = await tmuxE(['set-option', '-t', sessionName(r2.install, tabId), 'mouse', on ? 'on' : 'off']);
    return send(res, 200, r3.ok ? { ok: true, mouse: !!on } : { error: r3.err || 'mouse toggle failed' });
  }

  if (req.method === 'POST' && url.pathname === '/select') {
    const { pane } = await body(req);
    if (!PANE.test(String(pane || ''))) return send(res, 200, { error: 'bad pane id' });
    const r2 = await tmuxE(['select-pane', '-t', pane]);
    return send(res, 200, r2.ok ? { ok: true, pane } : { error: r2.err || 'select failed' });
  }

  if (req.method === 'POST' && url.pathname === '/close') {
    const { pane } = await body(req);
    return send(res, 200, await closePane(pane));
  }

  // Reordering, so a pane can be moved rather than only created and destroyed.
  if (req.method === 'POST' && url.pathname === '/swap') {
    const { a, b } = await body(req);
    if (!PANE.test(String(a || '')) || !PANE.test(String(b || ''))) {
      return send(res, 200, { error: 'bad pane id' });
    }
    const r2 = await tmuxE(['swap-pane', '-s', a, '-t', b]);
    return send(res, 200, r2.ok ? { ok: true, a, b } : { error: r2.err || 'swap failed' });
  }

  // Panes drift to unusable sizes after a few splits; this is the "tidy" button.
  if (req.method === 'POST' && url.pathname === '/layout') {
    const { tabId, install, preset } = await body(req);
    const allowed = ['even-vertical', 'even-horizontal', 'tiled', 'main-vertical', 'main-horizontal'];
    if (!allowed.includes(preset)) return send(res, 200, { error: `preset must be one of ${allowed.join(', ')}` });
    const r2 = resolveInstall(install);
    if (r2.error) return send(res, 200, r2);
    if (!digits(tabId)) return send(res, 200, { error: 'bad tabId' });
    const r3 = await tmuxE(['select-layout', '-t', sessionName(r2.install, tabId), preset]);
    return send(res, 200, r3.ok ? { ok: true, preset } : { error: r3.err || 'layout failed' });
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
    return send(res, 200, await matchSessions(r.install, want, url.searchParams.get('tabId')));
  }

  if (req.method === 'POST' && url.pathname === '/pane_send') {
    const { pane, text, enter } = await body(req);
    return send(res, 200, await paneSend(pane, text, enter !== false));
  }

  // fork: pull a pane out into its own window. join: put one back beside another.
  if (req.method === 'POST' && url.pathname === '/break') {
    const { pane } = await body(req);
    if (!PANE.test(String(pane || ''))) return send(res, 200, { error: 'bad pane id' });
    const r2 = await tmux(['break-pane', '-d', '-s', pane]);
    return send(res, 200, r2 === null ? { error: 'break failed' } : { ok: true, pane });
  }

  if (req.method === 'POST' && url.pathname === '/join') {
    const { src, dst, dir } = await body(req);
    if (!PANE.test(String(src || '')) || !PANE.test(String(dst || ''))) {
      return send(res, 200, { error: 'bad pane id' });
    }
    const r2 = await tmux(['join-pane', dir === 'h' ? '-h' : '-v', '-s', src, '-t', dst]);
    return send(res, 200, r2 === null ? { error: 'join failed' } : { ok: true, src, dst });
  }

  if (req.method === 'GET' && url.pathname === '/pull') {
    if (!IID.test(qInstall)) return send(res, 400, { error: 'pull requires a valid install id' });
    const c = clientFor(qInstall);
    // exec changes whenever the extension reloads. Recording it makes a stale duplicate
    // client visible instead of silently competing for the same commands.
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
