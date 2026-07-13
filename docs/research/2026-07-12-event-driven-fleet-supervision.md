# Research: event-driven fleet supervision — how the field does it, and what emux should borrow

**Date:** 2026-07-12
**Context:** emux v0.7.0 shipped `tmux_signals` (an `@@EMUX@@` output sentinel the
worker must voluntarily echo) + `tmux_wait` (a `stat()` poll loop over stream
logs). Before building more, we researched how mature systems solve "one
orchestrator supervises many workers": durable-execution engines, multi-agent LLM
frameworks, Claude Code's own harness, and OS/actor foundations + real tmux
orchestrators. This brief is the durable residue; the v2 decision (below) should
become an ADR when we commit to building it.

## The two principles the whole field converges on

1. **Worker→supervisor signaling is DETECTED AT A BOUNDARY, never ANNOUNCED by the
   worker.** Claude Code fires `SubagentStop`/`Notification` hooks at the harness
   level; Temporal persists a signal in the log before the worker sees it; OTP
   delivers a `{'DOWN', reason}` message on child death. → emux's `@@EMUX@@`
   sentinel depends on the worker *choosing* to echo, which is the weakest link.
   Claude Squad proves it fails: it hardcodes prompt strings and they break every
   time a CLI reworks its wording.

2. **"Wait" is EDGE-TRIGGERED on a real event, not a timer.** Temporal
   `wait_condition` re-evals only on a new log event; an actor is descheduled
   until a mailbox match; `tail -f` blocks on `inotify`, not `stat()`. → emux's
   1s `stat()` poll loop is an event costume over polling.

**Reframing insight:** tmux ITSELF already emits the push events emux is faking.
emux can't get events from inside the worker's *logic*, but it CAN get push
events about the *pane* — output, silence, death — from tmux. Earlier claim that
"emux is structurally a poll system" was half wrong.

## Worker lifecycle: warm-and-fed, NOT spawn-and-die (important correction)

The first draft of this research over-emphasized OTP's "death is an event, restart
the child" pattern. That is WRONG for an LLM worker, whose accumulated context IS
its valuable state — killing and respawning throws away everything it learned.
Two archetypes must be kept distinct:

- **Ephemeral worker** — spawn → do one task → exit. Death is normal; OTP restart
  fits. Good for a migration, a build, a one-shot.
- **Persistent / warm worker** — stays alive and IDLE between tasks, retains
  context, is FED more work via the down-channel. Death is a FAULT, not a
  lifecycle event. This is what a persistent agent (Eidos) wants, and it is the
  default we should design for.

Both systems studied most closely actually model the warm pattern; the earlier
draft mis-read them:
- The load-bearing property of an ACTOR is not "let it crash" — it is that the
  actor LOOPS FOREVER on its mailbox: finish a message, go back to waiting for the
  next, staying alive throughout. Restart-on-death is only the fault path.
- A TEMPORAL workflow doesn't exit after one signal — it is a `while` loop over
  `wait_condition`, durably suspended between signals, alive for days. That is
  literally "keep the worker open so it can be told about more work"
  (continue-as-new bounds the history without killing the loop).

**Design consequences:**
- The primary up-signal is `IDLE`/`READY` ("done with that, holding for the next
  task"), NOT `DONE`-and-exit. `DONE` should mean "this worker's whole purpose is
  finished, it may exit"; `IDLE` means "keep me, feed me."
- The DOWN-channel matters as much as the up-channel: the manager keeps the
  session alive and dispatches the next task with `tmux_send` (a warm Claude
  session idles at its prompt at ~zero token cost while waiting).
- `pane-died`/`session-closed` then correctly means FAULT — rare — and "recover"
  for an LLM worker can't be "spawn fresh" (loses context); it is resume/rehydrate
  or escalate to a human.
- Real tradeoff: a warm worker accumulates context (retains knowledge but costs
  more per turn and can bloat) → needs a RECYCLE/COMPACT policy, not a kill policy.

## What each domain does (mechanisms + sources)

### Durable execution (Temporal, DBOS, Restate, Step Functions, Azure Durable)
- **signal / query / update** split: signal = async fire-and-forget write (no ack);
  query = read-only, no history entry; update = request/response with a validator
  that can reject before writing. Adopt this vocabulary.
- `wait_condition(predicate)` + a raced durable timer = the HITL escalation pattern
  (Azure "Human Interaction Pattern"). Pause → wait for external decision → resume;
  state `pending → accepted/rejected`.
- Durable append-only log is why it scales: a supervisor "sleeps" for days at zero
  cost and can't lose an event (signal persisted before delivery). Signals are
  at-least-once → design idempotent handlers, dedup with keys.
- Step Functions `waitForTaskToken` / Restate awakeables = the "task token" pattern
  (emux analog: `; echo __DONE_$TOKEN__` and wait on the token).
- Sources: docs.temporal.io/encyclopedia/workflow-message-passing,
  /develop/python/message-passing, /workflow-execution/event, /child-workflows,
  /parent-close-policy; temporal.io/blog/robust-message-handlers;
  cadenceworkflow.io/docs/go-client/signals;
  learn.microsoft.com/azure/durable-task/common/durable-task-human-interaction;
  docs.dbos.dev/typescript/tutorials/workflow-communication;
  docs.restate.dev/develop/ts/awakeables;
  docs.aws.amazon.com/step-functions/latest/dg/connect-to-resource.html

### Multi-agent LLM frameworks (LangGraph, AutoGen/AG2, CrewAI, OpenAI Agents SDK)
- "I need a decision" almost never travels sideways worker→supervisor; it unwinds
  UP to whoever owns the loop: LangGraph `interrupt()` + `Command(resume=x)` (durable
  via checkpointer); OpenAI `result.interruptions` + serializable `RunState`;
  CrewAI blocking `human_input` at task boundaries. Only AutoGen v0.4 has a true
  peer bus (`HandoffMessage` on an event-driven actor runtime), and it pays with an
  in-process async runtime.
- All assume a control plane INSIDE the worker's process (shared event loop /
  checkpointer / delegation-tool return / RunState). emux has none — it's downstream
  of a byte stream. So: report-up + blocking-stdin HITL port faithfully as a
  sentinel + `send-keys` convention; durable interrupt-and-resume-elsewhere does NOT
  port (needs framework-owned serializable worker state).
- Sources: docs.langchain.com/oss/python/langgraph/interrupts, /durable-execution;
  github.com/langchain-ai/langgraph-supervisor-py;
  microsoft.github.io/autogen/stable (architecture, teams, human-in-the-loop);
  docs.crewai.com/en/learn/hierarchical-process;
  openai.github.io/openai-agents-python (handoffs, running_agents),
  openai-agents-js human-in-the-loop.

### Claude Code's own harness (the model to mirror)
- Does NOT rely on the worker LLM announcing state. Deterministic hooks fire at
  execution boundaries: `PreToolUse`/`PostToolUse` on every call, `Notification` on
  `agent_needs_input`/`agent_completed`, `SubagentStop` when a subagent halts,
  `PermissionRequest` for HITL. Hooks receive JSON on stdin and can inject context
  / block via exit code 2 or `{"decision":"block"}`.
- Background tasks + the Monitor tool re-invoke the agent when work completes.
- Sources: code.claude.com/docs/en/hooks.md, /hooks-guide.md, /tools-reference.md,
  /permissions.md, /sub-agents.md, /how-claude-code-works.md.

### OS + actor foundations, and real tmux orchestrators
- **fs-notify beats stat-polling** but only says "changed," not what/how much
  (coalescing) → keep a per-file byte offset and read the delta (emux already does).
  Handle rotation/truncation (`IN_MOVE_SELF`/`NOTE_RENAME`; `size < offset` → reset).
  Network mounts don't fire → keep a `stat()` fallback. Use `watchdog`/`fswatch`,
  don't hand-roll backends. Win is latency + idle-CPU at MANY workers; marginal at 2.
  Sources: man7.org/linux/man-pages/man7/inotify.7.html; infoq.com inotify article;
  Apple FSEvents/KernelQueues guide; github.com/emcrisostomo/fswatch;
  github.com/grafana/loki/issues/7967.
- **tmux is itself an event source (highest-leverage finding):**
  - `set-hook session-closed` / `pane-died` / `pane-exited` → worker death as a push
    event (the OTP `{'DOWN'}` message).
  - `monitor-activity`→`alert-activity`, `monitor-silence`→`alert-silence` →
    "produced output" / "idle N seconds" for free, robust, no sentinel.
  - Control mode `tmux -CC` → `%output` / `%exit` / `%window-close` for ALL panes
    over one fd = a literal actor mailbox.
  - `wait-for -S <chan>` / `wait-for <chan>` → tmux-native done-signal channel
    (worker signals without polluting/parsing output).
  - Sources: man7.org/linux/man-pages/man1/tmux.1.html;
    github.com/tmux/tmux/wiki/Control-Mode; deepwiki.com/tmux/tmux 7.2 hooks.
- **Actor model:** mailbox + selective `receive` = "block until a relevant message";
  supervision tree = the manages-hierarchy; monitors/links = "notify me when a child
  dies" (push, not scrape); let-it-crash + restart policy; BOUND the queue (unbounded
  mailbox OOM ↔ inotify `IN_Q_OVERFLOW`). Borrow the shape, not a runtime.
- **Real tmux orchestrators all poll + hardcode strings + use git as milestones:**
  Tmux-Orchestrator (self-scheduled check-ins + git commits), Claude Squad
  (`capture-pane` + SHA-256 diff + hardcoded prompt strings — brittle, version-fragile),
  uzi (git checkpoints + an auto-confirmer). claude-swarm escapes screen-scraping by
  making workers MCP servers. NONE use tmux hooks/control-mode/`wait-for` — that gap
  is emux's differentiator.
  Sources: github.com/Jedward23/Tmux-Orchestrator; github.com/smtg-ai/claude-squad
  (session/tmux/tmux.go, issues #189/#216); github.com/devflowinc/uzi;
  github.com/stevegeek/claude-swarm.

## v0.7.0 scorecard (honest)

- **Right:** read the durable stream log, not the live pane (Temporal-correct);
  byte-offset delta reads (`tail -f` discipline); arm the log before launch; a
  timeout on wait.
- **Naive/wrong:** `stat()` poll loop (should be tmux events / fs-notify); voluntary
  `@@EMUX@@` echo (fragile — death/output/silence are tmux ground truth); no
  send-and-await-ack ("update") primitive; no escalation branch; no death-as-event +
  restart policy; unbounded scanning (should coalesce per worker).

## Recommended v2 (ranked) — becomes an ADR when we build it

1. **Rebuild the watch layer on tmux native events** — `set-hook`
   session-closed/pane-died (death), monitor-activity/monitor-silence
   (output/idle), one control-mode `-CC` client as the pane mailbox, and
   `wait-for` for the explicit done-signal instead of an echoed string. Less code
   than a robust fs-notify watcher; deletes the fragile sentinel; nobody else does it.
2. **Adopt signal/query/update vocabulary**; add the missing `update` (send →
   wait-for-confirming-token); give `tmux_wait` an escalation branch (HITL pattern).
3. **Warm-worker framing (NOT restart-on-death).** Default worker is persistent:
   stays alive + IDLE between tasks, fed via the down-channel. Add an `IDLE`/`READY`
   up-signal distinct from `DONE`-and-exit; keep the session warm and dispatch next
   work with `tmux_send`. Treat `pane-died` as a FAULT (rare) → resume/rehydrate or
   escalate, never silently respawn (loses LLM context). Bound/coalesce the event
   queue. Borrow the actor "loop on mailbox, stay alive" shape — not let-it-crash,
   and not an actor runtime. A warm worker needs a recycle/compact policy for
   context bloat, not a kill policy.
4. **fs-notify (`watchdog`/`fswatch`)** as the fallback where control-mode isn't
   available; keep `stat()` for network mounts. Real win at many workers, marginal at 2.

## Proven findings from driving REAL Claude Code (2026-07-12)

These were established live, not assumed — several killed a naive design:

- **Driving a Claude worker via `send-keys` needs text and Enter SEPARATELY.** A
  combined text+Enter hits Claude's paste-detection and lands as an unsubmitted
  multi-line blob. Fix shipped: `tmux_send(settle=…)` — type, wait, Enter alone.
- **Boot timing drops early prompts.** A prompt sent before Claude is input-ready
  vanishes into an empty box. Do NOT scrape for readiness (the ❯ / "bypass
  permissions" strings render during boot, before input is ready). Robust gate:
  retry the dispatch until the `UserPromptSubmit` hook confirms it LANDED.
- **Completion is best detected by the worker's hooks, not by scraping.** A
  `Stop` hook → `emux signal IDLE` and a `UserPromptSubmit` hook → `emux signal
  PROGRESS` fire deterministically at turn boundaries. Proven: real Claude, warm
  context retained across two dispatches, zero TUI scraping. Shipped: the `emux
  signal` CLI + per-session inbox, read by `tmux_signals`/`tmux_wait` alongside
  the output sentinel.

## Hook lifecycle & the SELF-HEAL design (proven 2026-07-12)

Requirement (Daniel): it must work WHETHER OR NOT a worker has the hook, and an
unhooked worker should be detected and repaired by telling the CHILD to install
the hook — self-healing observability.

Proven behavior:
- A child CAN install its own hook — told to, a Claude worker wrote a valid
  `.claude/settings.json` Stop hook. The self-heal ACTION works.
- BUT **Claude Code caches hooks at startup; a mid-session edit does NOT
  activate** (verified: after self-install, the next turn still fired zero IDLE).
  Activating it needs a restart — which wipes the warm context. So self-heal is
  FUTURE-TENSE, not instant.
- tmux lifecycle hooks for DEATH are frictionful too: per-session `session-closed`
  doesn't fire on self-termination; `pane-died` needs `remain-on-exit on` which
  changes lifecycle and breaks `search_finds_ended`; a global `set-hook -g
  session-closed` fires reliably but mutates the user's tmux server state.

Resulting layered design (works with or without a hook):
1. **Spawn hooked from birth** — an emux-spawned worker is born with the
   PROGRESS/IDLE (and future DEAD) hooks, so it never needs healing. Open
   question: how to inject hooks without clobbering the user's own
   `.claude/settings.json` (candidate: `claude --settings <emux-managed file>`).
2. **Live fallback, ALWAYS** — for any unhooked worker (an adopted session),
   completion falls back to output-settle / poll and death to a `has-session`
   poll. Works now, less crisp. NEVER restart a warm worker to activate a hook —
   context outweighs the hook.
3. **Self-heal = future-proof, not repair** — detect the missing hook, have the
   child install it; it activates on the next spawn/restart. Heals the fleet's
   observability over time without sacrificing a live worker's memory.
4. **Death as event** is a real gap the up-channel hook does NOT cover (a killed
   worker fires no Stop hook). Options: a registry-scoped global `session-closed`
   hook (mutates tmux server state — a user decision) OR keep the cheap
   `has-session` poll (fine at small scale). Not yet decided.

Full four-agent research transcripts live in this session's task outputs
(2026-07-12).
