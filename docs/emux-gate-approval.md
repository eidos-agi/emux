# Approval gates

An AI terminal gate is a state-changing menu, not a normal composer. Ordinary
`emux send` never types through one, including when its deprecated `force`
argument is set.

First observe the exact gate:

```bash
emux gate NAME --json
```

Then deliberately approve or reject the returned fingerprint within 60 seconds:

```bash
emux approve NAME --fingerprint SHA256 --action approve --subject UID --device DEVICE --json
emux approve NAME --fingerprint SHA256 --action reject --json
```

Registered names are the default. Add `--session` to both calls for a raw tmux
session. Approval maps only to one named `Enter`; rejection maps only to one
named `Escape`. Other keys and actions are rejected.

Before sending, Emux holds a cross-process lock, checks the audit-backed
single-use challenge and expiry, confirms the session exists, recaptures the
live pane, and recomputes the fingerprint. A cleared, changed, stale, replayed,
or missing gate fails closed.

Records are appended and flushed to
`~/.local/state/emux/gate-approvals.jsonl`. They include timestamp, subject and
device when available, requested and resolved target, host, opaque gate
fingerprint and type, action, outcome, and request ID. Pane and prompt content
are never written to this audit.
