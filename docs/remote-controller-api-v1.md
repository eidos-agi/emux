# Remote controller API v1

The remote Emux server is the sole executor. A local `emux-controller` may use
these routes after a trusted pairing boundary authenticates the immutable human,
device, and controller identities:

- `GET /api/controller/v1/capabilities`
- `POST /api/controller/v1/requests`
- `GET /api/controller/v1/requests/<request-id>`
- `POST /api/controller/v1/requests/<request-id>/cancel`

The surface is disabled by default. The embedding service must inject a
`RemoteControllerAPI` configured with a stable server ID, explicit target
aliases, an authentication boundary, and a Hancock gate. It must not derive
trust from localhost, arbitrary forwarded headers, or browser origin checks.
Authentik remains the human identity authority; the pairing boundary must bind
its credential to the same immutable UID presented in the request envelope.

Every request uses `emux-remote/1.0`, an explicit
`server/channel/workspace/session` target, a unique nonce, a request ID, and a
five-minute `issued_at` window. Exact duplicate requests are idempotent;
request-ID or nonce reuse with different content fails as replay. Unknown
actions, parameters, versions, identities, servers, and targets fail closed.

`session.capture` is read-only. `session.send` and `session.interrupt` require a
Hancock decision. A pending consequential request may be cancelled; completed
terminal work cannot be retroactively cancelled. Receipts persist attribution,
target, action, status, and timing but never parameters, sent text, pane content,
credentials, summaries, preferences, embeddings, or other semantic memory.
Fleet and Eidos Omni retain their separate memory authority under EID-806.
