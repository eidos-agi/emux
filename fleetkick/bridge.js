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

const digits = (v) => /^[0-9]+$/.test(String(v));

// Switching the iframe's src would drop the websocket and make ttyd fire its
// beforeunload ("Leave site?") on every tab change. Instead the terminal stays
// connected forever and tmux swaps which session that same client displays.
async function switchTo(install, tabId) {
  if (!IID.test(String(install || ''))) return { error: 'bad install id' };
  if (!digits(tabId)) return { error: 'bad tabId' };
  const target = sessionName(install, tabId);
  if ((await tmux(['has-session', '-t', target])) === null) {
    await tmux(['new-session', '-d', '-s', target, BOOT, install, String(tabId)]);
    // new-session exits 0 even when its command dies a millisecond later, and tmux then
    // discards the session — which is how a daemon that could not create a single session
    // still answered ok:true to every /switch. Confirm it exists before saying so.
    if ((await tmux(['has-session', '-t', target])) === null) {
      return { error: `session ${target} died immediately — is claude on the daemon's PATH?` };
    }
  }
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
