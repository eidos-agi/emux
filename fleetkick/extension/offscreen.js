// The only thing in this extension that Chrome does not kill on an idle timer.
//
// The service worker cannot hold the connection: MV3 terminates it after ~30s idle,
// and while it's dead nothing is pulling commands, so every tool call times out. An
// offscreen document has no such timer — it lives until it's closed. So it owns the
// long-poll to the daemon and wakes the worker with a message when work arrives
// (delivering a message is an event, which is exactly what revives a dead worker).
//
// It cannot touch chrome.tabs itself — offscreen documents have almost no API surface.
// That's fine: it is the connection, the worker is the hands.

const BRIDGE = 'http://127.0.0.1:7682';
const FK = { 'x-fleetkick': '1' };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Identity of THIS run of the extension. The install id says which browser; this says which
// execution of it, so a stale duplicate still polling after a reload is visible on /health
// instead of silently competing for the same commands.
const EXEC = crypto.randomUUID().replace(/-/g, '').slice(0, 8);

// The worker mints and owns the install id; asking it (rather than reading storage here)
// means offscreen and panel can never race to create two different ones.
async function installId() {
  for (;;) {
    try {
      const r = await chrome.runtime.sendMessage({ fkInstall: true });
      if (r && r.install) return r.install;
    } catch {
      // worker not up yet — it is revived by the very act of messaging it, so retry
    }
    await sleep(250);
  }
}

async function loop() {
  const install = await installId();
  const PULL = `${BRIDGE}/pull?install=${install}&exec=${EXEC}`;
  let backoff = 250;
  for (;;) {
    try {
      const r = await fetch(PULL, { headers: FK });
      // A 4xx means this document is talking a protocol the daemon no longer accepts —
      // an offscreen document that outlived an extension reload, retrying a URL shape the
      // bridge rejects. Retrying forever looks identical to "extension not responding",
      // which cost a live debugging session. Close instead: the worker's alarm rebuilds a
      // fresh one within 30s, running the current code.
      if (r.status >= 400 && r.status < 500) {
        console.error('fleetkick offscreen: bridge rejected this client (' + r.status + '); closing so the worker rebuilds it');
        return window.close();
      }
      const cmd = await r.json();
      backoff = 250; // the daemon answered, so it's up
      if (!cmd || !cmd.op) continue; // long-poll expired with nothing to do

      // Wakes the service worker if it's asleep, then it runs the chrome.* call.
      let result;
      try {
        result = await chrome.runtime.sendMessage({ fkCmd: cmd });
      } catch (e) {
        result = { error: 'worker unreachable: ' + String(e) };
      }

      await fetch(BRIDGE + '/result', {
        method: 'POST',
        headers: { ...FK, 'content-type': 'application/json' },
        body: JSON.stringify({ id: cmd.id, result: result ?? { error: 'no result' } }),
      });
    } catch {
      // Daemon down or restarting — back off, then keep trying forever. This is the
      // "just reconnects to it" half: the daemon can die and come back, and the only
      // cost is one retry interval.
      await sleep(backoff);
      backoff = Math.min(backoff * 2, 5000);
    }
  }
}

loop();
