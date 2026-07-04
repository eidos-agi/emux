# ADR 0001 — Autonomous goal mode is part of emux's purpose

- **Status:** Accepted
- **Date:** 2026-07-03
- **telos north star:** `ns_1271c383bf3e` (emux)

## Context

emux began as a session picker + driver: "pick up where you left off in tmux,"
with an MCP for agent-driven attach / send / capture. Over one session it grew
three capability tiers on top of that substrate:

- `ask` / `converse` — send a prompt, wait for the reply to *settle*, return it.
- `navigate` — model-driven: read each screen, pick keystrokes to reach a goal.
- `goal` / `pursue` — an autonomous observe → act → judge loop that pursues a
  whole task through a TUI until done (with recovery for transient stalls,
  stuck loops, and dead sessions).

`goal` mode changes emux's *identity*: it is no longer only a picker/driver, it
is an autonomous operator of existing sessions. That is an expansion worth
recording deliberately rather than letting it accrete across commits.

## Decision

**Ratify autonomous operation as part of emux's purpose** — one north star that
includes "autonomously pursue goals through their TUIs," layered on the same
observe-and-drive substrate.

The founding **invariant is preserved and is the metric**: emux operates on
EXISTING sessions only. It drives via `send-keys` / `capture-pane` /
`has-session` / `list-sessions` and **never** `new-session` / `kill-session`.
The user owns session lifecycle; emux observes, drives, and now pursues — but
never spawns or kills.

## Consequences

- **Model dependency.** `navigate` / `goal` require the `claude` CLI on PATH
  (fixed-cost subscription tool — never the Anthropic API). Must be documented.
- **New failure surface** (autonomous loops): handled via escalation
  (Haiku→Sonnet), transient-stall retry, stuck-detection, and session-liveness.
- **Deliberate non-goal:** destructive actions are NOT gated (`Enter` + free
  text are permitted). Scope goals accordingly, or point goal mode at read-only
  surfaces. Revisit if a `--confirm`/denylist gate is needed.
- **Docs must catch up:** README + `pyproject` description still say
  "attach/send/capture"; they must name the ask/navigate/goal tier and the
  `claude` dependency for the north-star metric to stay honest.

## Note on process

This decision was made *after* building goal mode, then ratified. The correct
order (per the research-md → decision-record → telos chain) is research →
record → build. Recorded here retroactively; `visionlog` (the intended ADR
tool) is currently a non-functional shim, so this lives as a markdown ADR.
