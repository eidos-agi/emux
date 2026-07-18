# Emux controller protocol v1

`emux-controller` is the only client-side gateway for controlling paired Emux
servers. Claude Code, Codex, browser/native clients, CLI, and automation speak
this local protocol. They do not speak to tmux, Emux databases, or private
remote APIs.

## Boundaries

- The controller authenticates local clients over a per-user `0600` Unix socket
  using a `0600` bearer token. Production clients should receive the token via
  the local OS credential facility, not plugin output.
- The pairing registry contains stable aliases, endpoint, pinned server identity,
  expected protocol, and an opaque credential reference. Registry/list responses
  omit that reference. Credentials are supplied by a provider such as an OS
  keychain or Hancock; raw credentials never enter registry or responses.
- Every target is fully qualified as `server/channel/workspace/session`. The
  controller refuses partial targets, so identical session names on two servers
  cannot collide.
- The controller negotiates capabilities and verifies the pinned server ID and
  protocol before every control request. Missing/revoked credentials, stale
  capabilities, protocol skew, unknown actions, replayed IDs, and missing remote
  acknowledgements fail closed.
- Remote Emux is the executor. Authentik identifies the human. Hancock gates
  consequential remote actions. The controller transmits identity and request
  context but cannot grant either identity or authorization.

## Request

Newline-delimited JSON over the Unix socket:

```json
{
  "protocol": "emux-controller/1.0",
  "client_token": "<local secret>",
  "op": "request",
  "request_id": "018f...",
  "target": {
    "server": "hostkey",
    "channel": "engineering",
    "workspace": "emux",
    "session": "builder"
  },
  "action": "session.send",
  "parameters": {"text": "run tests"}
}
```

The remote acknowledgement must echo `request_id`. The controller returns a
receipt and writes an append-only `0600` audit record containing human UID,
device, server, full target, action, outcome, and request ID—but never request
parameters or credentials.

`health` and `servers.list` are read-only local operations. Cancellation uses a
new unique `request_id`, the original ID in `cancel_request_id`, and the same
fully qualified target.

## Operational-state-only rule

The controller may retain pairing records, negotiated capabilities, request
receipts, health state, and security audit logs subject to explicit retention.
It must not store conversation summaries, user preferences, learned concepts,
project knowledge, or any other semantic/product memory. Request parameters are
forwarded transiently and deliberately excluded from audit records. Fleet owns
durable learning, and Eidos Omni is Fleet memory authority. No controller
extension may blur this boundary.
