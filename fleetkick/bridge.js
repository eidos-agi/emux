#!/usr/bin/env node
// Fleetkick bridge: the embedded claude's MCP server POSTs commands here; the Chrome
// extension long-polls /pull, executes, and POSTs /result. Localhost only.
// ponytail: single global queue, one extension client assumed; add a shared token if
// this ever binds beyond 127.0.0.1.
const http = require('http');

let nextId = 1;
const queue = [];          // commands waiting for the extension
const pullers = [];        // extension long-polls waiting for a command
const pending = new Map(); // id -> /cmd response awaiting a result

const send = (res, code, obj) => {
  res.writeHead(code, { 'content-type': 'application/json' });
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

  if (req.method === 'GET' && req.url === '/health') return send(res, 200, { ok: true });

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
}).listen(7682, '127.0.0.1', () => console.log('fleetkick bridge on 127.0.0.1:7682'));
