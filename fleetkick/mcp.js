#!/usr/bin/env node
// Fleetkick MCP server (stdio, newline-delimited JSON-RPC) — thin proxy to the bridge.
// Spawned per embedded claude session. No paired tab: with no tabId the extension
// targets whichever tab is currently active (FLEETKICK_TAB pins it if you want that).
const http = require('http');
const PORT = Number(process.env.FLEETKICK_PORT) || 7682;
const TAB = Number(process.env.FLEETKICK_TAB) || undefined;
// Which browser profile this session drives. boot.sh sets it. Omitted only when a session
// is started by hand, in which case the bridge picks the sole connected browser or refuses.
const INSTALL = process.env.FLEETKICK_INSTALL || undefined;
// Set only for panes spawned with a role. A solo session leaves these empty and simply
// never sees itself as part of a team.
const ROLE = process.env.FLEETKICK_ROLE || '';
const SLOT = process.env.FLEETKICK_SLOT || '';
const ME = process.env.FLEETKICK_NAME || '';
const SESSION_TAB = process.env.FLEETKICK_SESSION_TAB || '';

const TOOLS = [
  { name: 'read',       description: 'Read the tab: title, url, and visible text.', props: {} },
  { name: 'navigate',   description: 'Navigate the tab to a URL.', props: { url: { type: 'string' } }, required: ['url'] },
  { name: 'click',      description: 'Click the first element matching a CSS selector.', props: { selector: { type: 'string' } }, required: ['selector'] },
  { name: 'type',       description: 'Focus an element by CSS selector and set its value (fires input/change).', props: { selector: { type: 'string' }, text: { type: 'string' } }, required: ['selector', 'text'] },
  { name: 'screenshot', description: 'Screenshot the tab (activates it first).', props: {} },
  { name: 'tab_create', description: 'Open a new tab; returns its tabId.', props: { url: { type: 'string' } } },
  { name: 'tabs_list',  description: 'List EVERY open tab across all windows (id, windowId, index, title, url, active, pinned). Use it to find a tab, then pass its tabId to any other tool.', props: {} },
  { name: 'refresh',    description: 'Reload the page. Set hard for a cache-bypassing reload.', props: { hard: { type: 'boolean' } } },
  { name: 'version',    description: 'Version of the Fleetkick extension the browser currently has loaded. Check this before concluding a change did not work — it may simply not be running yet.', props: {} },
  { name: 'whoami',     description: 'Who you are: your name, role (manager/worker/solo), slot, browser install, and the tab this group works on.', props: {} },
  { name: 'group',      description: 'Your teammates on this tab: name, role, agent and slot. This is the roster — address people by name.', props: {} },
  { name: 'send_to_agent', description: 'Send a message to a teammate by name. It lands in their mailbox and they read it on their next turn — it does NOT type into their prompt, so it cannot interrupt or impersonate the human.', props: { to: { type: 'string' }, text: { type: 'string' } }, required: ['to', 'text'] },
  { name: 'inbox',      description: 'Read messages teammates sent you, oldest first. Reading clears them unless peek is true. Check this when you are waiting on someone.', props: { peek: { type: 'boolean' } } },
  { name: 'spawn',      description: "Add a teammate on this tab, in its own terminal beside yours. role 'worker' reports to the manager; 'manager' takes charge (only one allowed). Optionally give it a name.", props: { role: { type: 'string' }, agent: { type: 'string' }, name: { type: 'string' } } },
  { name: 'dismiss',    description: 'Close a teammate by slot number.', props: { slot: { type: 'number' } }, required: ['slot'] },
].map(t => ({
  name: t.name,
  description: t.description + ' Targets whichever tab is currently active unless tabId is given.',
  inputSchema: { type: 'object', properties: { ...t.props, tabId: { type: 'number' } }, required: t.required || [] },
}));

function call(path, method, payload) {
  return new Promise((resolve, reject) => {
    const data = payload === undefined ? '' : JSON.stringify(payload);
    const req = http.request(
      { host: '127.0.0.1', port: PORT, path, method,
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

const bridge = (cmd) => call('/cmd', 'POST', { ...cmd, install: INSTALL });

// Team tools go straight to the daemon, not through the extension — they are about the
// terminal side of the world, so routing them through the browser would be a detour.
const TEAM_TOOLS = {
  whoami: async () => ({
    name: ME || null, role: ROLE || 'solo', slot: SLOT === '' ? null : Number(SLOT),
    install: INSTALL || null, tabId: SESSION_TAB || null,
  }),
  group: () => call(`/group?install=${INSTALL}&tabId=${SESSION_TAB}`, 'GET'),
  // Posts to a mailbox instead of typing into the recipient's prompt. Typing would arrive
  // as if the human wrote it, is unbounded, and corrupts whatever they were mid-way through
  // typing. The recipient reads this on its own turn.
  send_to_agent: (a) => call('/post', 'POST', {
    install: INSTALL, tabId: SESSION_TAB, to: a.to, from: ME, text: a.text,
  }),
  inbox: (a) => call(
    `/inbox?install=${INSTALL}&tabId=${SESSION_TAB}&slot=${SLOT}${a && a.peek ? '&peek=1' : ''}`, 'GET'),
  spawn: (a) => call('/add', 'POST', {
    install: INSTALL, tabId: SESSION_TAB,
    role: a.role || 'worker', agent: a.agent || 'claude', name: a.name,
  }),
  dismiss: (a) => call('/remove', 'POST', { install: INSTALL, tabId: SESSION_TAB, slot: a.slot }),
};

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
        const result = TEAM_TOOLS[name]
          ? await TEAM_TOOLS[name](args)
          : await bridge({ op: name, tabId: args.tabId ?? TAB, args });
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
