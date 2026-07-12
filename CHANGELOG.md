# Changelog

All notable changes to Emux are documented here.

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
