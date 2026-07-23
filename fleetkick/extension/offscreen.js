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

async function loop() {
  let backoff = 250;
  for (;;) {
    try {
      const cmd = await (await fetch(BRIDGE + '/pull', { headers: FK })).json();
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
