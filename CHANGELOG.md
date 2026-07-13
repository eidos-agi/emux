# Changelog

All notable changes to Emux are documented here.

## v0.7.0 - 2026-07-12

The poll→event pair — what lets one intelligence manage many terminals.

- **`tmux_signals` — the up-channel.** A worker talks UP to its manager by echoing a sentinel line, `@@EMUX@@ <KIND> <payload>` (KIND: DONE | NEED | PROGRESS | ERROR). emux lifts these from the stream log, new-since-last-read with byte-offset tracking (and a durable `signals.jsonl` ledger), so a manager learns "worker 4 done, worker 7 needs a decision" without scraping a screen. `under=<manager>` reads a whole `manages` subtree.
- **`tmux_wait` — poll→event.** Blocks until one or more sessions need you and returns which + why (`signal` / `idle` / `exit` / `change` / `prompt`). emux runs the watch loop internally — cheaply, by `stat`-ing each stream log and only looking closer when one grew — so the agent makes ONE call and stays context-empty until something happens, instead of capturing N sessions in a loop.
- **Fix (enables the above):** `tmux_spawn` now arms the stream log BEFORE launching the command, so a worker that signals early or exits fast no longer loses its output into an unrecorded pane.

## v0.6.0 - 2026-07-12

- Added an **operation audit trail**: every emux tool call appends one line to `~/.local/state/emux/audit.jsonl` — `{t, op, <salient args>, ok, error?}`, append-only, best-effort (an audit failure never breaks a tool). This is the per-CALL record that complements the session index (which jobs exist, running or ended) and the stream logs (what a job printed): together they let an agent reconstruct and reboot old jobs. Implemented as one `@audited` decorator under each `@mcp.tool()`, so tool signatures are unchanged.
- Fixed a stale package description that still claimed emux "never spawns" (the invariant was scoped to the autonomous loop in v0.5.0; explicit `tmux_spawn` may).

## v0.5.0 - 2026-07-12

- Added the **nested-manager** skill: a manager session spawns and drives a sub, so drive-churn stays one level down and the parent context only manages the manager. Context cost stays flat as the fleet grows, across ssh hops.
- `tmux_spawn` gained a `manages` param so a manager→sub edge is declarable at spawn time (rendered in `emux web` flow).
- **Governance:** scoped the founding never-spawn invariant. The autonomous loop (`ask`/`navigate`/`goal`/`pursue`) still never spawns or kills; explicit `tmux_spawn` may, under direct invocation. GUARD-001 amended, ADR-002 recorded, and the telos north star re-chartered (ns_1271c383bf3e closed `pivoted` → ns_4ba3587b3b24 with a runnable "autonomous loop emits zero new-session/kill-session" case).
- Synced plugin.json version (was stuck at 0.1.0) to the package.

## v0.4.0 - 2026-07-04

- Added the drive-tier ladder for autonomous TUI operation: `ask`/`tmux_ask` (send a prompt, wait for the reply to settle, return it), `navigate`/`tmux_navigate` (model-driven navigation to a target screen), and `goal`/`tmux_goal` (autonomous observe→act→judge loop until the task is done).
- Added recovery to navigate/goal: Haiku→Sonnet model escalation on a stall, transient blank/stall re-observe+retry, stuck-loop detection, and a clean `session_gone` abort.
- Added the telos-md drift-guard integration (`goal --telos` / `$EMUX_TELOS`): opens a north star for the goal, ticks each step, aborts on a telos `stop` signal, and records every run.
- Added a destructive-action gate to navigate/goal (default on): blocks a step that would type a destructive command (`rm -rf`, `DROP TABLE`, force-push…) or confirm a destructive on-screen prompt. Disable with `--yolo` / `$EMUX_ALLOW_DANGEROUS`.

## 0.1.0 - 2026-05-31

- Added the Emux CLI with `ls`, `register`, `unregister`, and `mcp` commands.
- Added the tmux-backed MCP tool surface for listing, registering, sending, capturing, and run-then-capture workflows.
- Added `emux watch` for watching many registered/live tmux sessions in one terminal dashboard.
- Added human CLI control commands: `emux send`, `emux interrupt`, `emux capture`, and `emux run`.
- Added `emux head` for opening a real macOS terminal head attached to a registered tmux session.
- Added Eidos/Codex/Claude plugin metadata for local and marketplace installation.
