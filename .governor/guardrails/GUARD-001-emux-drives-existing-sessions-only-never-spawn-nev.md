---
id: "GUARD-001"
type: "guardrail"
title: "The AUTONOMOUS loop never spawns or kills — only explicit tmux_spawn may"
status: "active"
date: "2026-07-03"
amended: "2026-07-12"
---

The **autonomous loop** — `ask` / `navigate` / `goal` / `pursue()` — operates on
EXISTING sessions only: send-keys / capture-pane / has-session / list-sessions.
It NEVER emits new-session or kill-session. The judge loop cannot create or
destroy a session on its own; it drives what already exists.

Session lifecycle is an **explicit** act. `tmux_spawn` (and only `tmux_spawn`)
may create or replace a session, and only under direct invocation by an operator
or a managing agent — never as a step the autonomous judge loop chose. This is
what makes the nested-manager pattern legal (a manager `tmux_spawn`s and drives a
sub; see the `nested-manager` skill).

**Amendment (2026-07-12, founder-authorized — Daniel).** The original invariant
read "NEVER new-session or kill-session, every capability." That was written
2026-07-03, before `tmux_spawn` existed. Blanket never-spawn was scoped down to
the autonomous loop so emux can spawn under explicit control. Rationale and the
alternatives weighed are in ADR-002. The old telos north star ns_1271c383bf3e
(plain "never spawn" metric) was closed `pivoted` and superseded by the
charter-backed north star **ns_4ba3587b3b24**, whose invariant
`autonomous_loop_never_owns_lifecycle` carries this scoped rule as a runnable
case (the `pursue`/`navigate` loop contains zero new-session/kill-session).
