# emux smart classifiers

<!-- Paste your content below this line. Once it's in, tell me and I'll study it
     with a subagent (state classification: working / idle / blocked / frozen /
     dead — the "spinner that stopped animating" idea and friends). -->


would it be possible to use a cheap model to summarize what's going on in a tmux, especailly since it mostly reads coding agents? like somehow machine learning that could tell what its been up to, if its been thrashing, if its getting stuck, if its wasting tokens, if its succeeding, if it's done, if it's still cooking code or still running, if its errored out, or possibly other failure models like we've run out of tokens, etc ?

eidos-agi/emux: MCP server for running Claude Code CLI commands as subprocesses — for autonomous marketplace install testing, scripted invocations, and agent-driven Claude Code automation.
github

Yes — this is very possible, and you probably do not need an expensive model for the first useful version. Emux already has several signals that make this easier: it can capture pane output and scrollback, detect recent activity while ignoring spinner-only changes, expose session metadata and live status, and distinguish “watching” from actual supervision, which is exactly the substrate you’d want for a cheap classifier/summarizer layer on top.

Feasibility
A good architecture is not “ask an LLM to read the whole tmux transcript.” It’s “turn tmux behavior into structured signals first, then let a cheap model classify the current state and produce a short summary.” Emux already gives you capture, ask/run primitives, activity history, session liveness, and current-command/tool detection in the web view, so most of the needed observability is already present.

The key point is that coding agents are highly repetitive and semi-formal in their terminal behavior, which makes them unusually friendly to low-cost inference. You can get surprisingly far with rules + counters + a small model over short windows, and only escalate to a stronger model when confidence is low or the session looks pathological.

Best design
Use a two-layer monitor: a deterministic signal extractor plus a cheap model that reads compressed state. The extractor should sample each session every few seconds using pane capture, normalize ANSI/noise, diff against prior samples, and emit features like “lines changed,” “time since meaningful change,” “same command repeated,” “test failures increasing,” “git status loop,” “approval prompt visible,” “shell prompt visible,” and “session gone.” Emux’s APIs and watcher concepts map cleanly onto that.

Then feed only a compact bundle to the model: recent deltas, a few rolling counters, current command, detected tool, and maybe the last 20–50 semantic events rather than raw terminal text. That makes the cheap model do interpretation instead of transcription, which is what keeps token spend low and improves reliability.

States to detect
You can model this as a finite-state machine with confidence scores rather than free-form chat summarization. Emux already documents recovery and abort conditions like session_gone, blocked_dangerous, and telos_stop, plus activity tracking and AI/tool detection, which suggests a natural state ontology.

A practical first taxonomy:

Running code/build/tests.

Reading/planning.

Editing or generating code.

Waiting on external process.

Waiting on human approval/login/input.

Thrashing or looping.

Stuck with no progress.

Error/failure.

Completed or idle.

Hard-stop conditions like session disappeared or dangerous action blocked.

You also mentioned “wasting tokens” and “run out of tokens,” which should be separate detectors. Those are usually visible from repeated re-planning, long verbose assistant output with no filesystem/process changes, explicit quota/context-limit strings, or repeated restarts of the same task framing.

Signals that work
The strongest cheap signals are usually not linguistic elegance but pattern recurrence. For example, “still cooking” looks like ongoing pane diffs plus changing subprocess output; “done” often looks like the shell prompt returning after success text; “errored out” looks like traceback/build failure/test summary patterns; and “thrashing” looks like repeated high-similarity windows with no net state transition. Emux’s activity strip, pane capture, and command detection already support much of this directly.

A useful feature set would include:

Activity features: changed/not changed, meaningful diff ratio, last-active age, burstiness. Emux already tracks recent change history and strips spinner frames for activity detection.

Process features: pane_current_command, detected AI/tool, attached state, uptime. Emux’s Flow view already surfaces detected AI/tool and live pane command context.

Terminal semantics: prompt detected, test summary detected, traceback detected, approval prompt detected, git conflict markers, patch/apply loops. Emux capture returns pane content and scrollback, including raw ANSI if needed.

Outcome features: success keywords, failing test count trend, retry count, interrupt count, session death, dangerous-block events, telos drift-stop. Emux explicitly names several of these abort/recovery conditions.

Cheap-model strategy
The cheapest reliable approach is usually rules first, model second. Hard-detect obvious terminal states with regexes and counters, then run a small model only on ambiguous windows to answer questions like “what has this agent been trying to do?” or “is this loop productive or not?”

A good escalation policy:

Tier 0: regex/rules only for obvious states, near-free.

Tier 1: cheap model for 1–3 sentence status summaries over compressed events.

Tier 2: stronger model only when confidence is low, stakes are high, or the session is anomalous.

That keeps cost down because most coding-agent sessions are boring. They spend long periods in recognizable modes: streaming tool output, test/build loops, shell idle, approval waits, or repeated edit/test cycles.

Failure models
You can absolutely build explicit failure models, and they’ll likely outperform generic summarization. Emux’s own recovery language hints at several canonical failures already: stall, blank capture retry, stuck loop, dangerous action blocked, telos no-progress, and session death.

Examples worth modeling directly:

Thrash: repeated semantic intent with no artifact change, or alternating commands without improving failure counts.

Stuck: low meaningful-diff rate for too long while not at a shell prompt.

Token waste: long assistant chatter with no command execution or file-change evidence.

Context/token exhaustion: explicit CLI messages, abrupt reset-like behavior, or repeated “re-explaining the plan.”

Hidden wait: approval/login/MFA prompt visible but no input progress.

False busy: spinner or cursor movement only, which Emux already partially guards against in activity detection.

The important design choice is to separate “busy” from “productive.” A session can be highly active and still be thrashing, or nearly idle and actually waiting on a long compile.

Recommendation
I’d build this as an emux summarize or emux judge layer that polls captures, maintains a rolling event log per session, and emits state, confidence, summary, failure_flags, and recommended_action. That fits Emux’s current model well because the project already exposes capture, send, web monitoring, activity history, AI/tool detection, and autonomous goal/recovery concepts.

A concrete v1 could return something like:

Field	Example
State	thrashing
Confidence	0.87
Summary	“Claude has retried test fixes 6 times in 14 minutes; failure count is unchanged.”
Evidence	“Repeated pytest, same traceback signature, no prompt-free completion.”
Action	“Interrupt and ask for root-cause analysis before another patch.”
That would already be operationally useful without needing a frontier model. The biggest win will come from event compression and explicit state machines, not from model intelligence alone.

A solid next step is to define 12–20 labeled states and collect a few hundred real tmux windows from your agents to train/evaluate the classifier prompts and heuristics. Would you like a concrete schema for the event log plus the exact heuristics/model pipeline for an emux summarize prototype?

what 50 other signals would we want to know?

Here’s a concrete list of 50-ish additional signals that would be useful for judging “what’s going on” in an Emux-driven tmux session, beyond just “is it active.” I’ll group them by intent so they’re implementable as features or classifiers on top of tmux_capture, pane_current_command, the web daemon’s activity grid, telos, etc.

1. Command / process-level signals
These give you a read on what is ostensibly happening:

Current foreground command name (pane_current_command) and duration.

Classification of command type (build, test, git, package manager, editor, AI CLI, generic shell).

Count of distinct commands executed in the last N minutes.

Repetition count of the same command (e.g., pytest or npm test rerun loops).

Ratio of “meta” commands (ls, cd, cat) vs. “goalful” commands (build, test, deploy) over time.

Detect whether an editor/TUI (vim, nano, Claude Code, aider, etc.) is in the foreground vs. bare shell.

Time spent inside editor/TUI vs. time at shell prompt.

Presence of long-running processes (command age > threshold) and whether output is still changing.

Detected AI/tool identity (Claude Code, Codex, Gemini, Aider, etc.) from command signatures or content, as Emux’s Flow view already does.

Count of interrupts (Ctrl-C) issued in this session over the last window.

2. Terminal semantic signals
These come from parsing captured pane text and scrollback:

Shell prompt visibility (e.g., PS1-like pattern) vs. full-screen TUI in control.

Presence of stack traces / exceptions (Python traceback, Node stack, Java stack, etc.).

Presence of test result summaries (e.g., “X passed, Y failed,” “FAILED tests/test_…”) and trend of failure count.

Detection of compilation/build failures (e.g., “error:”, “Build failed”, “Compilation terminated”).

Detection of success indicators (“All tests passed”, “Build succeeded”, “Completed successfully”).

Detection of approval/login/MFA prompts (“Press Enter to continue”, “Approve in browser”, “Enter verification code”).

Detection of interactive confirmation prompts (“Delete? [y/N]”, “Are you sure?”).

Presence of git conflict markers (<<<<<<<, =======, >>>>>>>) in diffs or files.

Detection of git status / diff output and whether there are uncommitted changes.

Detection of package manager activity (pip, uv, npm, pnpm, cargo, etc.) and whether it succeeded or failed.

Detection of deployment or remote operations (ssh, kubectl, docker, cloud CLIs).

Presence of CLI messages about rate limits, quota exhaustion, or context length (“rate limit exceeded”, “context length exceeded”, “token limit”).

Detection of explicit “plan” or “summary” sections in AI output (e.g., bullet lists labeled “Plan”, “Next steps”).

Detection of “waiting on human” cues (“please confirm”, “waiting for your approval”, “provide credentials”).

Detection of help / usage output (--help, usage:) suggesting the agent is fumbling with tool syntax.

3. Activity / change-pattern signals
These build on Emux’s activity grid and spinner stripping:

Pane change rate (lines changed per second) excluding spinner/cursor noise—Emux already strips braille/block spinners before comparison.

Burstiness pattern (bursts of output vs. steady stream vs. quiet idle).

“Meaningful diff” ratio: proportion of changes that alter alphanumeric content instead of ANSI-only/whitespace.

Time since last meaningful change (not just visual jitter).

Entropy or diversity of recent output windows (high-entropy new content vs. repeated identical fragments).

Degree of self-similarity: repeated nearly-identical output windows → thrashing indicator.

Long idle-at-prompt periods (prompt visible, no commands typed) vs. long busy-subprocess periods (no prompt, changing output).

Ratio of AI output vs. shell output (e.g., more model chatter than actual command results).

Time spent in “tail -F” / log-watching patterns where agent is just passively observing.

Number of times a “same failure pattern” reappears (same traceback header, same error message) without new fixes.

4. Session / topology signals
These use Emux’s registry, Flow view, and uptime/attached info:

Session uptime since creation and since last attach.

Number of registered sessions this one manages or is managed by (from manages relationships).

Role classification in the topology: orchestrator vs. worker vs. unconnected. Emux already splits nodes into those layers.

Number of child sessions currently live vs. expected (e.g., some workers died).

Whether this session is currently attached by a human (attached marker in web UI) or only driven by agents.

Frequency of session reattachments and head openings (emux head usage) over time.

Presence of “stale” registry entries vs. live sessions; whether this session’s registry state is stale or healthy.

Network of recent cross-session interactions (e.g., this session recently used tmux_send/tmux_run against others).

Telos involvement: whether this session is part of a goal --telos run and telos state (drift, no-progress, reached, abandoned). Emux’s telos integration already records ticks per step and telos_stop.

5. Autonomy / loop-quality signals
These tell you how healthy the agent loop is:

Number of autonomous goal/navigate steps executed in this session during the current run. Emux’s autonomous ladder tracks these.

Number of recovery events: stalls detected, blank capture retries, stuck-loop detections, session-death aborts.

Model escalation events: how often the loop escalated Haiku → Sonnet due to stalls. Emux already supports this escalation via $EMUX_NAV_MODEL_ESCALATE.

Destructive-action gate triggers: blocked_dangerous events where potentially destructive commands were prevented.

Telos drift/no-progress events: counts and last occurrence timestamps (telos_stop).

Driver identity: whether the current loop is being driven by a manager session (nested-manager pattern) or a leaf worker; Emux’s nested-manager skill and registry relationships expose this.

6. Resource / token-ish signals
Even though Emux doesn’t see raw API tokens, it can infer some resource pressure:

Approximate “verbosity-to-action” ratio: model tokens wasted estimate from long repetitive responses with minimal new commands or file changes.

Count of “re-explaining the same plan” messages vs. new plan branches.

Detection of CLI messages that hint at quota exhaustion, API failure, or context limits (as above).

Frequency of model/tool restarts (e.g., Claude Code CLI restarted multiple times within a short window).

Size / length of recent captures under tmux_ask vs. tmux_run—too many huge ask windows can signal token-heavy introspection.

7. Human-in-the-loop signals
You likely want to know whether human attention is involved:

Recent human keystrokes or commands vs. agent-driven ones (could be tagged by source in a higher layer, but even naive detection like “commands typed while attached” helps).

Human approvals in the loop (e.g., “yes” to confirmations, entering codes) occurring within some time window.

Interruption / override events where a human sends Ctrl-C or launches emux head and takes manual control mid-run.

If you treat these as a feature vector per session, you can build:

A cheap classifier for coarse state (running, planning, editing, waiting_human, thrashing, stuck, error, done).

A judgment layer (productive vs. wasteful, safe vs. risky, needs-human-attention vs. fine).

A short, cheap-model-generated summary that references a handful of these signals (“Haiku: 3 retries of pytest with identical stack trace; telos flagged no-progress; destructive-gate fired once; last human input 40m ago”).

Would you rather start by formalizing a single “agent_health_score” based on a subset of these, or by defining explicit state labels and transitions (i.e., a state machine spec) for Emux to expose over /api/sessions?

what tree would organize these into trainable tree of hierarchical mdoels or algos?

eidos-agi/emux: MCP server for running Claude Code CLI commands as subprocesses — for autonomous marketplace install testing, scripted invocations, and agent-driven Claude Code automation.
github

Yes — I’d organize it as a hierarchical decision stack rather than one giant classifier. Emux already naturally separates raw capture, command/process context, session topology, recovery events, and activity history, so the trainable system should mirror those layers instead of flattening everything into one model.

The right shape is: deterministic parsers at the bottom, narrow classifiers in the middle, then a top-level health/state judge and summarizer. That fits Emux’s surfaces well because it already exposes capture, live command detection, activity strips, topology via manages, and abort/recovery signals like session_gone, blocked_dangerous, and telos_stop.

Tree shape
I’d use a 6-level tree. Each node either emits a label, a score, or a compact latent vector for the layer above.

Signal extraction

Primitive detectors

Micro-state classifiers

Meso-state sequence models

Outcome / risk judges

Narrative summarizer

That gives you a trainable hierarchy where cheap logic handles obvious things and learned models handle ambiguity. It also keeps the output debuggable, which matters a lot for operations tooling around autonomous agent fleets.

Hierarchical tree
Here’s the tree I’d actually build:

text
Session Understanding Tree
├── L0. Raw observables
│   ├── Pane text windows
│   ├── Scrollback deltas
│   ├── ANSI / cursor / spinner-normalized text
│   ├── pane_current_command
│   ├── session metadata
│   ├── activity strip / last-active age
│   ├── topology edges (manages)
│   └── recovery / gate / telos events
│
├── L1. Primitive detectors
│   ├── Text pattern detectors
│   │   ├── shell prompt visible
│   │   ├── traceback/error present
│   │   ├── success text present
│   │   ├── test summary present
│   │   ├── approval/login prompt present
│   │   ├── dangerous confirmation prompt present
│   │   ├── rate-limit/token/context text present
│   │   └── git/package/deploy command family present
│   ├── Change detectors
│   │   ├── meaningful diff ratio
│   │   ├── output repetition score
│   │   ├── burstiness
│   │   ├── idle duration
│   │   └── command repetition count
│   ├── Process detectors
│   │   ├── current tool identity
│   │   ├── editor/TUI vs shell
│   │   ├── long-running subprocess
│   │   └── interrupt frequency
│   └── Topology/context detectors
│       ├── orchestrator / worker / unconnected
│       ├── human attached or not
│       ├── goal/telos active
│       └── recent recovery events
│
├── L2. Micro-state classifiers (instantaneous, 1 window)
│   ├── Screen mode classifier
│   │   ├── shell prompt
│   │   ├── AI chat / coding agent
│   │   ├── editor
│   │   ├── test/build output
│   │   ├── logs/stream
│   │   ├── approval/auth screen
│   │   └── unknown TUI
│   ├── Intent classifier
│   │   ├── planning
│   │   ├── coding
│   │   ├── running
│   │   ├── verifying
│   │   ├── deploying
│   │   ├── waiting
│   │   └── recovering
│   └── Status classifier
│       ├── healthy
│       ├── warning
│       ├── error-visible
│       └── blocked-visible
│
├── L3. Meso-state sequence models (time-series, 30s–30m)
│   ├── Progress model
│   │   ├── making progress
│   │   ├── slow progress
│   │   ├── no progress
│   │   └── regressing
│   ├── Loop-quality model
│   │   ├── productive iteration
│   │   ├── retry loop
│   │   ├── thrashing
│   │   ├── stuck
│   │   └── false busy
│   ├── Wait-state model
│   │   ├── waiting on compute
│   │   ├── waiting on human
│   │   ├── waiting on external system
│   │   └── idle done
│   └── Resource-pressure model
│       ├── likely token waste
│       ├── likely context exhaustion
│       ├── likely rate-limited
│       └── normal
│
├── L4. Outcome / risk judges
│   ├── Completion judge
│   │   ├── done-success
│   │   ├── done-failed
│   │   ├── not done
│   │   └── uncertain
│   ├── Intervention judge
│   │   ├── leave alone
│   │   ├── observe closely
│   │   ├── prompt with guidance
│   │   ├── interrupt and redirect
│   │   └── escalate to human now
│   ├── Safety/risk judge
│   │   ├── safe
│   │   ├── blocked-dangerous
│   │   ├── risky-destructive-surface
│   │   └── session-gone / broken
│   └── Fleet-role judge
│       ├── healthy worker
│       ├── failing worker
│       ├── bottleneck orchestrator
│       └── abandoned session
│
└── L5. Narrative summarizer
    ├── one-line status
    ├── what it has been doing
    ├── why we think it is stuck/thrashing/done
    ├── evidence bullets
    └── recommended next action
This structure maps cleanly onto Emux because the repo already provides raw capture, activity tracking that ignores spinner frames, current-command/tool detection, registry metadata, topology through manages, and autonomous recovery/abort signals.

Best model family per layer
You do not want the same algorithm at every node. Different layers want different tools.

Layer	Best algorithm class	Why
L0–L1	Rules, regex, parsers, counters	Cheap, precise, easy to debug on terminal text.
L2	Small classifiers, gradient-boosted trees, linear models, or tiny encoder	Great for single-window classification from engineered features.
L3	HMM/CRF, temporal boosting, TCN/LSTM, or event-sequence transformer	Time dependence matters for progress vs thrash vs wait.
L4	Calibrated ensemble / policy model	Needs confidence, triage, and action recommendations.
L5	Small LLM or templated NLG	Summaries should cite upstream evidence, not infer raw state from scratch.
That split matters because Emux is already rich in structured signals, so most of the intelligence should come from fusing features over time, not from throwing a big language model directly at raw pane dumps.

Recommended training decomposition
I’d train this in four independently labeled tasks, not one end-to-end monster. Emux’s existing API surfaces make it feasible to log the right supervision targets from real runs.

Frame labeling

Input: one capture window + command/process metadata.

Output: screen mode, visible status, visible intent.

Model: lightweight tree/linear/tiny transformer.

Sequence labeling

Input: last N frame labels + deltas + recovery events + activity features.

Output: progress state, thrash/stuck/wait labels.

Model: HMM/TCN/LSTM/event transformer.

Outcome judgment

Input: aggregated sequence features over 5–30 minutes.

Output: done, failed, blocked, needs intervention, likely token waste.

Model: boosted tree with calibrated probabilities.

Summary generation

Input: structured evidence bundle only.

Output: short explanation and recommendation.

Model: tiny LLM or template engine.

This decomposition keeps labeling tractable. Humans can much more reliably tag “this window is a pytest failure screen” or “this 10-minute segment is thrashing” than provide a single holistic label for the whole session.

Concrete subtrees
A few especially valuable subtrees deserve their own dedicated models:

Progress subtree
This should answer: is the agent advancing toward a result. Emux’s telos step ticks, recovery events, and settle-based ask/goal behavior provide strong auxiliary supervision for this branch.

text
Progress
├── visible artifact change?
├── failure count trend improving?
├── command diversity healthy?
├── same-plan repetition increasing?
├── prompt returned after successful command?
└── telos/no-progress events?
    └── classify: progress / slow / none / regress
Thrash subtree
Thrash is different from mere failure. It usually means repeated edit/test/retry cycles with low novelty and low improvement. Emux’s activity history plus capture diffs and repeated command/context signatures are perfect ingredients for this detector.

text
Thrash
├── same command repeated?
├── same error signature repeated?
├── low semantic novelty across windows?
├── many interrupts/retries?
├── no net improvement?
└── classify: normal retry / retry loop / thrash / hard stuck
Wait subtree
This is important because many “stuck” sessions are actually fine. Emux already distinguishes pane movement from spinner-only visual churn in the daemon’s activity tracking, which is exactly the kind of signal this subtree needs.

text
Wait
├── output still changing?
├── prompt visible?
├── approval/auth prompt visible?
├── remote/deploy/build command running?
├── hidden tab / no human attached?
└── classify: compute wait / human wait / external wait / idle done
Completion subtree
You want completion to be conservative. The best signal is usually a combination of success text, prompt reappearance, no further retries, and no open blocking prompt. Emux’s settled reply semantics and live capture make that combination observable.

text
Completion
├── success markers?
├── shell prompt returned?
├── no active subprocess?
├── no pending approval prompt?
├── recent output stable?
└── classify: done-success / done-failed / not done / uncertain
Practical implementation order
For Emux specifically, I’d build the tree in this order:

L1 feature bank as deterministic extractors from tmux_capture, pane_current_command, activity strip, session metadata, and recovery/telos events. Emux already exposes these or describes them in the web and goal/navigate layers.

L2 frame classifier for screen mode + visible intent.

L3 progress/thrash/wait sequence model over rolling 5s snapshots.

L4 intervention judge for “leave / prompt / interrupt / escalate.”

L5 summary generator from structured evidence only.

That order gives operational value early. You can ship useful monitoring once L2 and L3 exist, before doing anything fancy with summarization.

Minimal trainable ontology
If you want the smallest viable hierarchical ontology, I’d start with this:

Mode: shell, AI-chat, editor, build/test, logs, approval, unknown.

Intent: planning, coding, running, verifying, waiting, recovering.

Progress: progress, slow, none, regress.

Health: healthy, warning, thrashing, stuck, blocked, error.

Outcome: done-success, done-failed, not-done, uncertain.

Action: leave, observe, nudge, interrupt, human-escalate.

That’s compact enough to label consistently and rich enough to drive summaries, dashboards, and automation policy. It also aligns well with Emux’s current product surfaces: watch/grid/activity/flow/chat, plus goal/navigate recovery and telos drift stopping.

Design principle
The big design principle is: bottom layers detect facts, top layers infer judgment. Facts are things like prompt visible, current command is pytest, same traceback seen three times, session_gone, or telos_stop; judgments are things like thrashing, wasting tokens, done, or intervene now. Emux already gives you many of the factual hooks, so the hierarchy should preserve that distinction instead of letting one opaque model learn everything poorly.

If you want, I can turn this into a concrete Python schema: Signal -> FrameFeatures -> SequenceState -> Judgment -> Summary, with suggested Pydantic models and a first-pass implementation plan for emux-signals and emux-judge.

in what order would you build them

I’d build them in the order that maximizes operational value and creates training data for the next layer. Since Emux already has capture, ask/run/goal, activity strips, Flow topology, current-command detection, and recovery events, the smartest sequence is to start with deterministic observability and only add learned judgment once you can log clean examples.

So the order is: instrumentation first, visible-state classifiers second, time-based health models third, intervention policy fourth, and natural-language summarization last. Emux’s existing web/API surfaces make that especially attractive because you can ship useful monitoring before any expensive ML work.

Phase 1
Start with the feature bank, not a model. Emux already exposes tmux_capture, tmux_run, tmux_ask, activity detection that ignores spinner frames, pane_current_command-based tool detection in Flow, topology via manages, and recovery/abort signals like session_gone, blocked_dangerous, and telos_stop, so you can immediately log structured events per session without inventing a new substrate.

Build these first:

Normalized capture snapshots: raw text, ANSI-stripped text, diff against previous frame.

Process/session context: current command, detected tool, uptime, attached flag, tags, topology role.

Event hooks: interrupt, send, ask, run, navigate step, goal step, recovery event, dangerous-block, telos stop.

Rolling counters: time since meaningful change, repeated command count, repeated error signature count, burstiness, prompt-visible ratio.

This phase gives immediate value because the data itself is already actionable, and it creates the labeled substrate for everything later.

Phase 2
Next build primitive detectors and keep them deterministic. Terminal sessions are full of highly regular text patterns, so regex/rule detectors for prompt visibility, traceback presence, test summaries, approval prompts, success markers, quota/rate-limit messages, and destructive confirmations will outperform a learned model early on while remaining debuggable.

Do this before any classifier because it gives you high-precision weak labels. Those weak labels can later supervise or enrich the learned models instead of making you hand-label everything from scratch.

Phase 3
Then build the single-frame classifier. This model should answer only low-ambiguity questions about the current screen, such as screen mode, visible intent, and visible severity, using one capture window plus structured features from Phases 1–2.

I’d target labels like:

Mode: shell, AI-chat, editor, build/test, logs, approval screen, unknown.

Visible intent: planning, coding, running, verifying, waiting, recovering.

Visible status: normal, warning, error-visible, blocked-visible.

This is the first trainable model because it is the easiest to label consistently and gives immediate UX wins in Grid, Activity, and Flow.

Phase 4
After that, build the sequence models for progress and loop quality. Emux’s ask settle behavior, navigate and goal recovery ladder, activity history, and telos tick/stop concepts make temporal inference much more valuable than static classification for deciding whether an agent is productive, stalled, or thrashing.

I’d build these in this order:

Progress model: progress, slow progress, no progress, regress.

Wait model: waiting on compute, waiting on human, waiting on external system, idle done.

Thrash/stuck model: productive retry, retry loop, thrashing, hard stuck, false busy.

Resource-pressure model: likely token waste, likely context exhaustion, likely rate-limited, normal.

These are the highest-value judgments for autonomous agent supervision because they turn “the pane is changing” into “the agent is actually getting somewhere.”

Phase 5
Then build the outcome and intervention judges. Once you have frame labels and temporal states, you can create a policy layer that decides whether to do nothing, observe, nudge, interrupt, or escalate to a human; this is safer and more reliable than trying to derive intervention directly from raw captures.

I’d split this into three narrow judges:

Completion judge: done-success, done-failed, not-done, uncertain.

Intervention judge: leave alone, observe closely, prompt with guidance, interrupt and redirect, human now.

Risk/safety judge: safe, risky surface, blocked-dangerous, session broken/gone.

This is where the system becomes operationally useful for actual fleet management.

Phase 6
Only after that would I build the summarizer. Emux already supports chat-like session viewing and live pane monitoring, but summary quality will be much better if the summarizer consumes structured evidence from the earlier layers instead of raw tmux text.

The summarizer should be almost boring:

Input: current state, recent transitions, top evidence features, last key events, recommended action.

Output: one-line status, short explanation, evidence bullets, next action.

That lets you use a cheap model because it is explaining known facts rather than discovering them from scratch.

Concrete build order
If I were shipping this inside Emux, I’d do it in this exact sequence:

emux-signals

Event log schema and capture sampler.

ANSI stripping, diffing, rolling windows, command/tool extraction.

Expose structured features via /api/sessions or a new endpoint.

emux-detectors

Regex/rule detectors for prompts, errors, tests, success, auth, quota, danger, git/deploy/package patterns.

Confidence + evidence per detector.

emux-frame

Single-frame classifier for mode, visible intent, visible status.

Start with gradient-boosted trees or a compact encoder over featureized text.

emux-sequence

Progress model first.

Then wait model.

Then thrash/stuck model.

Then resource-pressure model.

emux-judge

Completion, intervention, and safety/risk policies.

Return action recommendations, not just labels.

emux-summary

Small-model or templated explanation layer from structured evidence.

emux-autopilot-hooks

Use judge outputs to power nudges, interrupts, escalations, or adaptive polling/model escalation in goal/navigate. Emux already has recovery escalation from Haiku to Sonnet and telos-driven aborts, so this layer can plug into existing autonomy controls.

Why this order
This order works because every layer produces training labels or clean inputs for the next one. Emux’s current design is already registry-centric and observability-heavy, so you should exploit that by making the early stages precise and inspectable rather than prematurely training an opaque end-to-end model.

In short:

Build what is observable first.

Then build what is classifiable from one frame.

Then build what is only knowable over time.

Then build what is actionable.

Then build what is explainable in English.

Fastest MVP
If you want the shortest path to usefulness, I’d do just this first slice:

Feature bank.

Primitive detectors.

Thrash/wait/progress heuristic sequence judge, even before training.

Tiny summary layer.

That MVP would already answer most of your original questions: “What has it been doing?”, “Is it stuck?”, “Is it just waiting?”, “Did it error?”, “Is it done?”, and “Should I intervene?”

My strongest recommendation is to postpone LLM summarization until after you have a clean SessionState object. Once that exists, the summary becomes cheap, accurate, and easy to swap across models.

Would you like me to turn this into a concrete repo plan with proposed module names, Pydantic types, and which parts belong in Emux core versus a companion emux-signals package?

how would you morph them over time?

I’d morph them from rules -> weak labels -> narrow learned models -> temporal ensembles -> policy system. Emux already has the right operational surfaces for that evolution — shared registry, tmux_capture, tmux_ask settle behavior, navigate/goal recovery events, activity history that strips spinner noise, Flow topology via manages, and HTTP APIs over the same session graph — so the smartest path is to let the system become more learned only as real fleet data accumulates.

The key idea is that each generation should subsume the previous one rather than replace it. Your rules become features, your heuristics become teacher labels, your classifiers become calibrated components, and your top-level judge becomes a policy engine over evidence instead of an opaque monolith.

Stage 1
Start with a fact layer that is almost entirely deterministic. Emux already makes this practical because it can capture pane output, detect current command/tool identity, surface activity recency, expose session metadata and topology, and emit recovery/abort markers like session_gone, blocked_dangerous, and telos_stop.

At this stage, every signal should be one of:

Parser output, like prompt-visible, traceback-visible, success-visible, approval-visible.

Counter or trend, like repeated command count, idle duration, meaningful-diff ratio, failure-signature recurrence.

Session context, like orchestrator/worker role, human attached, goal/telos active.

This stage is not glamorous, but it gives you a trustworthy substrate and avoids premature ML.

Stage 2
Then build heuristic state machines on top of those facts. Emux’s existing semantics already suggest the first derived states: settled reply, stalled loop, session gone, dangerous-block, telos drift/no-progress, active-but-not-spinner, and attached vs. unattended.

Examples:

If prompt-visible and recent success marker and no active subprocess, infer likely done.

If same command repeats with same error signature and no improvement trend, infer retry loop or thrash.

If no prompt, output changing meaningfully, and build/test/deploy command active, infer working/compute wait.

If auth/approval text visible and no progress, infer waiting on human.

At this stage, don’t train anything yet. Instead, log every heuristic judgment with evidence and later outcome.

Stage 3
Now treat those heuristics as weak supervision. This is where the morphing starts: the rules stop being the final product and become noisy teachers for trainable models. Emux’s web daemon and API make this especially valuable because the same session can be observed repeatedly across many windows and users without duplicating capture cost.

Concretely:

High-precision heuristics produce auto-labels for obvious cases.

Humans review only the uncertain or contradictory windows.

You train first-pass frame classifiers on the combined labeled set.

This gives you a cheap path to scale labels for things like screen mode, visible intent, visible error state, approval screen, and likely tool family.

Stage 4
Next morph the static classifiers into temporal models. This is the biggest qualitative jump, because many of the questions you care about — thrashing, stuckness, no-progress, false busy, token waste — are not properties of a single screen but of a trajectory. Emux’s ask settle logic, goal step loop, recovery events, activity strip, and telos ticks make this kind of sequence modeling natural.

The migration path I’d use:

Start with rolling-window heuristics over 30 seconds to 10 minutes.

Replace hand-tuned thresholds with learned sequence models once enough logs exist.

Keep the heuristics as guardrails and fallback when model confidence is low.

So “thrash” evolves from “same error appeared 3 times” into a trained notion of repeated low-novelty action loops with poor improvement odds.

Stage 5
After that, morph from descriptive models into a judgment ensemble. Emux already has an autonomy ladder — send/capture, ask, navigate, goal — plus escalation and drift stopping, so the next natural step is a judge that decides how much autonomy is still warranted.

This layer should combine:

Frame outputs, like visible mode and current severity.

Temporal outputs, like progress, wait type, thrash score, resource-pressure score.

Structural context, like orchestrator/worker role and whether the session is managed or abandoned.

Existing autonomy signals, like model escalation, recovery retries, dangerous blocks, telos stops.

The result is a policy-facing object: not just “thrashing,” but “interrupt and ask for root-cause analysis,” or “leave alone, it’s still compiling.”

Stage 6
Only then should you morph the system into a self-improving policy loop. Emux’s current architecture is already close to this because navigate and goal can escalate models, retry captures, detect stuck loops, and stop on drift; a higher-order judge could eventually tune those controls dynamically.

Examples of late-stage morphing:

Adaptive polling: active sessions sample faster, quiet stable ones slower.

Adaptive model routing: cheap heuristics for obvious states, small classifier for ambiguous states, stronger model only for intervention-critical cases.

Adaptive autonomy: a session with high thrash probability gets shorter leash and earlier human escalation.

Adaptive memory: recurring failure motifs become reusable signatures for future sessions.

At this point, the system is no longer just a monitor. It becomes a governor over agent fleets.

How each node evolves
The cleanest way to think about morphing is node-by-node:

Node type	V1	V2	V3	V4
Primitive signal	Regex/counter/parser	Better normalization	Confidence-scored parser	Learned extractor only where needed
Frame classifier	Rules	Weakly supervised classifier	Fully trained lightweight model	Distilled compact model
Sequence model	Rolling heuristics	Threshold tuning from logs	HMM/LSTM/TCN/event transformer	Calibrated temporal ensemble
Intervention judge	Handwritten policy	Policy over model outputs	Learned ranker for next action	Closed-loop adaptive controller
Summary	Template	Small LLM over evidence	Better explanation/rationale	Personalized/fleet-aware briefings
That progression matters because it keeps the system operational at every step instead of waiting for a big-bang ML rewrite.

What should never morph away
Some parts should remain mostly deterministic even in a mature system. Emux already encodes several of these kinds of hard truths in its product design and recovery semantics.

Keep these rule-based:

Session existence and liveness, because session_gone is factual.

Dangerous-action denylists and explicit destructive confirmations, because safety gates should be inspectable.

Basic prompt/success/error detectors where precision is high.

Topology facts from registry manages edges, because those are declared structure rather than inferred behavior.

Learn around them, not instead of them.

Good morphing schedule
A practical schedule for Emux would be:

Month 1: build signal bank and evidence logging over tmux_capture, command context, activity history, topology, and recovery events.

Month 2: add deterministic detectors and heuristic session states to the web/API surfaces.

Month 3: collect reviewed labels from real sessions and train frame classifiers.

Month 4: train progress/wait/thrash temporal models on rolling windows.

Month 5: add intervention and completion judges, initially conservative.

Month 6+: add adaptive routing, active learning, and summary generation from structured evidence.

That path matches Emux’s current maturity well because the repo already appears to be growing staircase-style into separate banks and releases such as emux-signals and emux-wait, while retaining a single shared operating surface across TUI, MCP, CLI, and web.

Core principle
The core principle is: models should absorb ambiguity, not facts. Emux already provides a lot of facts — current command, pane text, activity strips, session relationships, settle behavior, recovery and drift-stop markers — so over time you should morph the system by moving only the ambiguous layers upward into trainable models while preserving the factual substrate underneath.

A good end state is not “one model that reads tmux.” It’s “a fact graph plus temporal judges plus a tiny explainer.” That will be cheaper, safer, and easier to debug.

Would you like the next pass as a concrete morphing roadmap from emux-signals -> emux-frame -> emux-progress -> emux-judge, with suggested interfaces and training datasets for each?

how would this help us achieve agi like behavior

eidos-agi/emux: MCP server for running Claude Code CLI commands as subprocesses — for autonomous marketplace install testing, scripted invocations, and agent-driven Claude Code automation.
github

Building this kind of hierarchical “what’s going on in tmux” brain is directly useful for AGI-like behavior because it gives your agents situational awareness, temporal memory, and self-governance over their own processes, not just better answers to prompts. Emux is already architected as a shared registry and control surface over live sessions (send, capture, ask, navigate, goal, web/Flow), so adding a supervisory layer on top of that is effectively adding a cortex around a swarm of local LLM processes.

Persistent world model
AGI-like behavior needs a persistent world the agent can reason about, not just stateless API calls. Emux already centralizes all tmux sessions into one registry, exposes them to agents via MCP tools, and keeps a live monitoring plane via emux web (Grid, Activity, Flow, Chat).

Once you add the hierarchical models we discussed on top of tmux_capture, pane_current_command, topology and recovery signals, you effectively get:

A shared, structured description of “what each agent/session is doing,” updated over time.

A persistent record of goals, telos north stars, ticks, and reached/abandoned runs.

A unified view over orchestrators and workers in the Flow topology, not isolated processes.

That’s a world model for agent behavior, not just for files or APIs. It’s a necessary piece if you want a system that behaves coherently across many tools and long time spans.

Meta-cognition and self-monitoring
AGI isn’t just “solve tasks” — it’s “notice when you’re stuck, change strategy, ask for help, and avoid hurting yourself.” Emux already has a ladder from send/capture/run up through ask, navigate, and goal, plus recovery (stall detection, blank/stall retry, session-gone abort), destructive-action gating (blocked_dangerous), and telos drift/no-progress aborts (telos_stop).

Adding the hierarchical signals we outlined gives the system:

Explicit notions of progress vs. no-progress vs. regress, learned from temporal patterns.

Thrash vs. productive retry vs. false busy, not just “pane changed.”

Wait-type distinctions: waiting on compute, on human, on external system, or idle done.

Health and risk scores per session, plus an intervention policy (“leave alone”, “nudge”, “interrupt”, “escalate to human”).

That is meta-cognition over the agent’s own behavior: the system can reason about whether its current strategy is good, and change how it allocates attention and autonomy accordingly.

Multi-agent coordination
AGI-like behavior in a real environment almost certainly looks like a society of specialized agents rather than one monolith. Emux’s Flow view already models orchestrator/worker relationships via manages edges and can show detected AI/tool identity per pane (Claude Code, Codex, Gemini, Aider, Hermes, or raw process names).

With a hierarchical monitor on top of that:

Orchestrators get live health summaries of all workers (“worker-3: thrashing on pytest; worker-5: waiting on SSH; worker-7: done-success”).

Managers can automatically reassign tasks or open heads (emux head) for failing workers.

Fleet-level policies can be encoded (“don’t let more than N workers thrash; pause goals when telos drift exceeds threshold”).

That’s coordination and resource management at the system level — a key ingredient for AGI-like behavior in non-trivial environments.

Temporal reasoning and credit assignment
Your monitor tree is inherently a temporal model: it cares about sequences of screen states, commands, and errors, not isolated snapshots. Emux’s goal/telos integration already logs autonomous runs (north star + ticks + close states) and uses telos to abort on drift or no-progress.

Layering learned progress/thrash/wait models on this gives you:

A way to assign credit/blame over episodes (“this 25-minute run made progress until minute 17, then thrash began after a tool change”).

A basis for learning better policies and tool-usage strategies over time.

The ability to compare different strategies or models on the same task, not just raw answer quality.

That sort of temporal credit assignment is central to a system that can improve its own procedures, not just its answers.

Safety and alignment as first-class behaviors
Achieving AGI-like behavior safely requires built-in safeguards and external conscience. Emux already embeds an external telos layer (goal --telos / tmux_goal(telos=True)), destructive-action gating for dangerous commands or confirmations, and explicit recovery semantics for session death and stalls.

Your hierarchical monitor makes those safeguards richer and more data-driven:

It can learn more nuanced notions of “dangerous surface” based on repeated patterns (e.g., migrations, cluster-wide operations).

It can treat telos drift/no-progress as a feature in its own progress/stuck judgments, not just a binary abort.

It can surface safety-relevant signals (dangerous blocks, repeated failures touching prod tags, human overrides) to human operators as summaries.

That’s an environment where autonomous behavior is constantly filtered through safety-aware monitors and external north stars — much closer to what you’d want from any AGI-like system interacting with real infrastructure.

Bridging symbol and behavior
LLMs today mostly see text. Emux lets them see behavioral traces of themselves and other agents in tmux sessions: commands executed, outputs, errors, terminal UIs, and the topology of sessions they manage via registry metadata.

Once you build the hierarchical monitoring:

Agents can be trained or prompted on behavioral features (“avoid thrash”, “recognize stuckness”, “prioritize progress positions”).

Higher-level planners can reason about “states of the fleet,” not just states of an individual document or API.

You can integrate behavioral metrics (progress, thrash, wait, human-intervention events) directly into reward or evaluation signals for new agent strategies.

That starts to bridge the gap between “symbolic AGI inside text” and “embodied AGI in a real computing environment,” because the environment’s complexity and the agent’s behavior in it are visible and learnable.

Path to AGI-like systems
Putting it all together, a system like Emux plus hierarchical monitoring gives you:

A persistent, observable environment (tmux sessions, registries, topology, history).

A ladder of autonomy (send/capture/run → ask → navigate → goal), already implemented using the Claude CLI and tmux.

A safety and alignment layer (destructive gates, telos drift guard, explicit recovery semantics).

A meta-cognitive monitor (progress, thrash, stuck, wait, resource pressure, outcome, intervention).

A multi-agent coordination plane (Flow topology with live panes, registry manages relations, nested-manager patterns).

That’s very close to the kind of substrate you’d expect under an AGI-like system: many tools and agents, shared memory and control, external conscience, and a continuous self-model of what it is doing and how well it’s doing it.

In other words, this doesn’t magically “cause AGI,” but it gives you the behavioral skeleton and governance that any serious AGI-like system will need once it leaves the sandbox of single-shot question answering and starts living inside a real cluster of terminals, services, and long-running tasks.

Is your main interest to use this as training data for better agents, or as a real-time governor that keeps a large agent fleet safe and productive?