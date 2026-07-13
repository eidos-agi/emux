# Changelog

All notable changes to Emux are documented here.

## v0.16.0 - 2026-07-12

- **Company classifier + filter.** Each session is tagged with the company/context that owns it — Eidos, Greenmark Waste, AIC, Jetta, Momentito, Rhea Impact, ASMP, Personal — derived deterministically from its working directory (`_detect_company`, keyed on the `repos-<x>/` prefix; no LLM). A colored company pill shows on each tile header and sidebar card, and the filter bar leads with filled company chips: click one to narrow every view to that company. Runs off the live pane's cwd, so it's accurate the moment a session is doing real work.

## v0.15.0 - 2026-07-12

- **Tag filter in the sidebar.** A chip bar under the session filter lists every tag across the fleet with its count; click one to narrow the whole view (grid / groups / activity / flow / sidebar) to sessions carrying that tag, click again (or `✕ all`) to clear. A card's own `#tag` chips now toggle the same filter instead of jumping to the groups anchor — one consistent "filter to this tag" gesture.

## v0.14.0 - 2026-07-12

- **`move_to_emux` MCP tool** — a Claude Code chat moves ITSELF into emux. Reads `CLAUDE_CODE_SESSION_ID` from the environment, derives the session's cwd from its transcript, and spawns a tmux window running `claude --resume <id>`, registered + managed like any other session (visible in the web control room, classifiable, steerable from the modal). One call turns the conversation you're in into a first-class fleet member.
- **Per-window AI-tool icons, brand-colored.** Each session's tile/flow/modal shows the detected tool's glyph in its brand color — Claude ✳ (clay), Codex ◇ (green), Gemini ♊ (blue), Grok ⚡, opencode ❖ (purple), Aider ✦, Hermes ☿. Grok and opencode added to the detection table.
- **Fixed: Claude Code sessions weren't detected as Claude.** Claude Code retitles its tmux pane to a bare version string (e.g. `2.1.207`), so the `"claude"` command-match never fired and it fell through to a generic `▸ 2.1.207` badge. Now a semver pane title resolves to Claude Code, and the content-signature fallback gained real TUI tells (`esc to interrupt`, `? for shortcuts`, `bypass permissions`).

## v0.13.0 - 2026-07-12

- Added the deterministic **session-state classifier** (`emux/judge.py`) — Tier-0, no LLM. `classify()` labels a session running / planning / editing / waiting_external / waiting_human / thrashing / stuck / error / done_idle / dead, with confidence, a one-line summary, evidence, and orthogonal flags (token_waste, possible_exhaustion, hidden_wait, false_busy, dangerous_blocked). Signal-first: trusts the `@@EMUX@@` up-channel (DONE/ERROR/NEED) before scraping; stateless off the durable stream log. `tmux_classify` MCP tool wraps it.
- **Live classifier report in the web modal**: click a session → a state strip under the header shows its state / confidence / recommended action / summary / flags, updated every ~1.2s (`/api/classify`).
- **Flow view: unconnected boxes wrap into a grid** — the old layout crammed them onto one fixed-width row and they overlapped past ~4; now they never overlay.

## v0.12.0 - 2026-07-12

- **Remote pull upgraded from `cat`-per-poll to a persistent `tail -F` follower.**
  A remote session is now watched by ONE `ssh host tail -F -n +1` that streams the
  remote inbox's lines into a LOCAL mirror file, so reads stay local (correct peek
  + id-dedup) and a remote signal arrives the instant it's written, not on the
  next poll tick. `-n +1` streams the whole file then follows (no startup gap);
  `stdbuf -oL` line-buffers so a single signal flushes across ssh immediately;
  the mirror is capped and the follower auto-restarts on drop (re-replay absorbed
  by dedup). Falls back cleanly if no follower can start. Proven live against a
  real box (eidos-bm): sub-second delivery (~0.23s) over one connection.

## v0.11.0 - 2026-07-12

Remote fan-out: a parent hears a child on another machine, over BOTH channels.

- **Remote receive path.** A session with a `host` now has its signals read from
  the remote box's inbox over ssh (`ssh host cat …`; cheap under ssh ControlMaster
  multiplexing). `tmux_signals`/`tmux_wait` work for a remote child with no
  caller changes. (The drive layer — spawn/send/capture/run — was already
  ssh-capable via `host`.)
- **Both delivery channels, over a durable local write.** The child always writes
  its signal to its own inbox first (durable, survives the child's death). Then
  it's delivered up two ways: **pull** (parent reads the remote inbox over ssh)
  and **push** (`emux signal … --push HOST` ssh-appends the SAME record to the
  parent's inbox). Each covers the other's failure — a dying child may not finish
  a push, the pull still has the durable line; a dropped pull is caught by the
  pushed line.
- **Dedup by id.** Every signal now carries a uuid `id`; the parent dedups across
  push and pull so both channels delivering the same signal collapse to one
  (at-least-once → idempotent). Legacy id-less lines get a content-hash id.
- Proven live against a real remote box (eidos-bm): pull + push + dedup all green.

## v0.10.0 - 2026-07-12

- Added `claude_warm_worker` — a **real Claude Code** ctc case (dim `real-llm`), the end-to-end proof of the warm-worker loop. Spawns a real `claude` worker whose hooks fire deterministically at boundaries — `UserPromptSubmit` → `emux signal PROGRESS` (prompt landed), `Stop` → `emux signal IDLE` (turn done) — dispatches two tasks down the same warm session, and proves it kept context: task 2 recalls task 1's secret. Coordination is **zero-scraping**: PROGRESS confirms the prompt beat Claude's boot/paste timing (retry-until-landed), IDLE confirms completion. Falsifiable — GREEN when warm (recalls 42), RED when cold (a fresh worker never told the number cannot recall). Opt-in (`EMUX_CTC_LIVE=1` on macOS with `claude`); SKIPs otherwise so routine runs stay fast and CI-safe.

## v0.9.0 - 2026-07-12

The robust up-channel + the real down-channel — both proven live against Claude Code.

- **`emux signal <kind> [payload]` + a per-session inbox (robust up-channel).** A worker's Claude Code **Stop** hook runs `emux signal IDLE` (finished, warm, ready) and its **Notification** hook (`agent_needs_input`) runs `emux signal NEED "<blocked on>"`. These write directly to `~/.local/state/emux/inbox/<session>.jsonl`; `tmux_signals`/`tmux_wait` read them alongside the output sentinel, so a hook-injected signal is indistinguishable from a scraped one. This is boundary-detected completion — deterministic, harness-fired, no scraping and no dependence on the worker LLM remembering to echo. The `@@EMUX@@` output sentinel remains the fallback for non-hook-capable workers. (Fixed: a hook-only worker with no stream log used to be skipped — the reader now always drains the inbox.)
- **`tmux_send(settle=…)` (real down-channel).** Type the text, wait `settle` seconds, then send Enter as a SEPARATE keystroke. Without this, a fast text+Enter hits Claude Code's paste detection and lands as an unsubmitted multi-line blob. Verified live: `settle=0.4` submits a prompt to a real Claude session and gets the answer back.

## v0.8.0 - 2026-07-12

- Added `IDLE` / `READY` signal kinds — a warm worker's "finished that task, HOLDING for the next" (keep me + feed me), distinct from `DONE` ("my whole purpose is finished, I may exit"). This is the primary up-signal for the warm-worker model: an LLM worker's accumulated context IS its state, so death is a fault, not a lifecycle — you keep it alive and dispatch more work down the same session.
- Added a real-surface ctc case `warm_worker_loop` (dim `lifecycle`): one persistent worker is fed two tasks down the same session and must keep context across both (task 2 sees task 1) and never be respawned. Falsifiable — RED when the worker is respawned per task (context lost), which is exactly the anti-pattern to avoid. Proves the loop mechanics only; the Claude-TUI-specific risks (signal extraction from a redrawing TUI, send-keys timing, real context bloat) remain a live experiment, not a deterministic case.

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
