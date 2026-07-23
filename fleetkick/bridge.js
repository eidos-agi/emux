#!/usr/bin/env node
// Fleetkick bridge: the embedded claude's MCP server POSTs commands here; the Chrome
// extension long-polls /pull, executes, and POSTs /result. Localhost only.
// ponytail: single global queue, one extension client assumed; add a shared token if
// this ever binds beyond 127.0.0.1.
const http = require('http');
const path = require('path');
const { execFile } = require('child_process');

// 7682 is the real one the extension polls; tests override so a live extension
// can't steal their commands.
const PORT = Number(process.env.FLEETKICK_PORT) || 7682;
const BOOT = path.join(__dirname, 'boot.sh');
const PREFIX = 'fleetkick-tab-';
// NOT a tab. With no controlling terminal (i.e. under launchd, which is how this
// actually runs) tmux sanitizes control characters in list output to '_', so a \t
// separator silently collapses into the field values. Interactive tests never show
// it, because there tmux has a tty and emits the tab intact.
const SEP = '|';

// execFile, never exec — no shell, so a tabId can't smuggle shell syntax. Belt and
// braces with the digits-only check at every call site.
const tmux = (args) => new Promise((resolve) =>
  execFile('tmux', args, (err, stdout) => resolve(err ? null : String(stdout).trim())));

const digits = (v) => /^[0-9]+$/.test(String(v));

// Switching the iframe's src would drop the websocket and make ttyd fire its
// beforeunload ("Leave site?") on every tab change. Instead the terminal stays
// connected forever and tmux swaps which session that same client displays.
async function switchTo(tabId) {
  if (!digits(tabId)) return { error: 'bad tabId' };
  const target = PREFIX + tabId;
  if ((await tmux(['has-session', '-t', target])) === null) {
    await tmux(['new-session', '-d', '-s', target, BOOT, '--inner', String(tabId)]);
  }
  const clients = (await tmux(['list-clients', '-F', `#{client_tty}${SEP}#{client_session}`])) || '';
  const ttys = clients.split('\n').filter(Boolean)
    .map((l) => l.split(SEP))
    .filter(([, session]) => session && session.startsWith(PREFIX))
    .map(([tty]) => tty);
  for (const tty of ttys) await tmux(['switch-client', '-c', tty, '-t', target]);
  return { ok: true, session: target, switched: ttys.length };
}

// session_activity is a unix ts that bumps on output, so "still running" is just
// "produced output very recently" — no process introspection needed.
async function sessions() {
  const out = (await tmux(['list-sessions', '-F',
    `#{session_name}${SEP}#{session_activity}${SEP}#{session_attached}`])) || '';
  const now = Date.now() / 1000;
  return out.split('\n').filter(Boolean)
    .map((l) => l.split(SEP))
    .filter(([name]) => name.startsWith(PREFIX))
    .map(([name, activity, attached]) => ({
      name,
      tabId: Number(name.slice(PREFIX.length)),
      activity: Number(activity),
      attached: attached === '1',
      running: now - Number(activity) < 3,
    }));
}
let nextId = 1;
const queue = [];          // commands waiting for the extension
const pullers = [];        // extension long-polls waiting for a command
const pending = new Map(); // id -> /cmd response awaiting a result

const send = (res, code, obj) => {
  // Echoing ACAO lets the extension page read replies; it grants a web page nothing,
  // since it still can't get past the Origin and x-fleetkick checks to be answered.
  res.writeHead(code, { 'content-type': 'application/json', 'access-control-allow-origin': '*' });
  res.end(JSON.stringify(obj));
};

const dispatch = () => {
  while (queue.length && pullers.length) {
    const res = pullers.shift();
    clearTimeout(res.fkTimer);
    send(res, 200, queue.shift());
  }
};

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

  if (req.method === 'GET' && req.url === '/health') return send(res, 200, { ok: true });

  if (req.method === 'GET' && req.url === '/sessions') return send(res, 200, await sessions());

  if (req.method === 'POST' && req.url === '/switch') {
    const { tabId } = await body(req);
    return send(res, 200, await switchTo(tabId));
  }

  if (req.method === 'GET' && req.url === '/pull') {
    pullers.push(res);
    res.fkTimer = setTimeout(() => {
      const i = pullers.indexOf(res);
      if (i >= 0) { pullers.splice(i, 1); send(res, 200, {}); }
    }, 20000);
    return dispatch();
  }

  if (req.method === 'POST' && req.url === '/cmd') {
    const cmd = await body(req);
    cmd.id = nextId++;
    pending.set(cmd.id, res);
    setTimeout(() => {
      if (pending.delete(cmd.id)) {
        send(res, 504, { error: 'Fleetkick extension did not respond — is it loaded and Chrome running?' });
      }
    }, 30000);
    queue.push(cmd);
    return dispatch();
  }

  if (req.method === 'POST' && req.url === '/result') {
    const { id, result } = await body(req);
    const waiter = pending.get(id);
    pending.delete(id);
    if (waiter) send(waiter, 200, result ?? {});
    return send(res, 200, { ok: true });
  }

  send(res, 404, { error: 'not found' });
}).listen(PORT, '127.0.0.1', () => console.log('fleetkick bridge on 127.0.0.1:' + PORT));
