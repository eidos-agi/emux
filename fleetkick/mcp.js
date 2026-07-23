#!/usr/bin/env node
// Fleetkick MCP server (stdio, newline-delimited JSON-RPC) — thin proxy to the bridge.
// Spawned per embedded claude session. No paired tab: with no tabId the extension
// targets whichever tab is currently active (FLEETKICK_TAB pins it if you want that).
const http = require('http');
const PORT = Number(process.env.FLEETKICK_PORT) || 7682;
const TAB = Number(process.env.FLEETKICK_TAB) || undefined;

const TOOLS = [
  { name: 'read',       description: 'Read the tab: title, url, and visible text.', props: {} },
  { name: 'navigate',   description: 'Navigate the tab to a URL.', props: { url: { type: 'string' } }, required: ['url'] },
  { name: 'click',      description: 'Click the first element matching a CSS selector.', props: { selector: { type: 'string' } }, required: ['selector'] },
  { name: 'type',       description: 'Focus an element by CSS selector and set its value (fires input/change).', props: { selector: { type: 'string' }, text: { type: 'string' } }, required: ['selector', 'text'] },
  { name: 'screenshot', description: 'Screenshot the tab (activates it first).', props: {} },
  { name: 'tab_create', description: 'Open a new tab; returns its tabId.', props: { url: { type: 'string' } } },
  { name: 'tabs_list',  description: 'List EVERY open tab across all windows (id, windowId, index, title, url, active, pinned). Use it to find a tab, then pass its tabId to any other tool.', props: {} },
  { name: 'refresh',    description: 'Reload the page. Set hard for a cache-bypassing reload.', props: { hard: { type: 'boolean' } } },
  { name: 'version',    description: 'Version of the Fleetkick extension Chrome currently has loaded. Check this before concluding a change did not work — it may simply not be running yet.', props: {} },
].map(t => ({
  name: t.name,
  description: t.description + ' Targets whichever tab is currently active unless tabId is given.',
  inputSchema: { type: 'object', properties: { ...t.props, tabId: { type: 'number' } }, required: t.required || [] },
}));

function bridge(cmd) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(cmd);
    const req = http.request(
      { host: '127.0.0.1', port: PORT, path: '/cmd', method: 'POST',
        headers: { 'content-type': 'application/json', 'content-length': Buffer.byteLength(data), 'x-fleetkick': '1' } },
      (res) => {
        let b = '';
        res.on('data', (c) => (b += c));
        res.on('end', () => { try { resolve(JSON.parse(b || '{}')); } catch { resolve({ error: 'bad bridge reply' }); } });
      }
    );
    req.on('error', () => reject(new Error('fleetkick bridge unreachable on 127.0.0.1:7682 — run fleetkick/serve.sh')));
    req.end(data);
  });
}

const reply = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\n');

let buf = '';
process.stdin.on('data', async (chunk) => {
  buf += chunk;
  let nl;
  while ((nl = buf.indexOf('\n')) >= 0) {
    const line = buf.slice(0, nl).trim();
    buf = buf.slice(nl + 1);
    if (!line) continue;
    let msg;
    try { msg = JSON.parse(line); } catch { continue; }
    if (msg.method === 'initialize') {
      reply(msg.id, {
        protocolVersion: (msg.params && msg.params.protocolVersion) || '2025-06-18',
        capabilities: { tools: {} },
        serverInfo: { name: 'fleetkick', version: '0.3.0' },
      });
    } else if (msg.method === 'tools/list') {
      reply(msg.id, { tools: TOOLS });
    } else if (msg.method === 'tools/call') {
      const { name, arguments: args = {} } = msg.params || {};
      try {
        const result = await bridge({ op: name, tabId: args.tabId ?? TAB, args });
        if (name === 'screenshot' && result.dataUrl) {
          reply(msg.id, { content: [{ type: 'image', data: result.dataUrl.split(',')[1], mimeType: 'image/png' }] });
        } else {
          reply(msg.id, { content: [{ type: 'text', text: JSON.stringify(result) }], isError: !!result.error });
        }
      } catch (e) {
        reply(msg.id, { content: [{ type: 'text', text: String(e.message || e) }], isError: true });
      }
    } else if (msg.id !== undefined) {
      reply(msg.id, {}); // ping and anything else that expects an answer
    }
  }
});
