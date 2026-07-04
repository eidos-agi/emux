---
id: "GUARD-001"
type: "guardrail"
title: "emux drives EXISTING sessions only \u2014 never spawn, never kill"
status: "active"
date: "2026-07-03"
---

emux operates on existing tmux sessions only: send-keys / capture-pane / has-session / list-sessions. NEVER new-session or kill-session — the user owns session lifecycle. Applies to every capability including goal mode. Founding invariant; the telos metric for ns_1271c383bf3e.
