# emux smart-classifier ("judge") — grounded implementation plan

Synthesized 2026-07-12 from the brainstorm in `emux-smart-classifiers.md`, read
against the actual emux code + the fleet-supervision research brief. The brainstorm
was written as if emux were greenfield; it isn't. Two corrections drive everything.

## Framing corrections

1. **Signal-first, not scrape-first.** A hooked worker already emits deterministic
   `@@EMUX@@ PROGRESS/IDLE/DONE/NEED/ERROR` at real turn boundaries (Claude Code
   Stop/UserPromptSubmit hooks → `emux signal` → inbox). Those are GROUND TRUTH.
   The classifier reads `_new_signals()` FIRST; scraping the pane is the *fallback*
   for unhooked/adopted sessions. The brainstorm's "read the pane and infer" is the
   fallback path, not the primary one.
2. **Skip learned models entirely for now.** The brainstorm's L2–L4 (gradient-boosted
   trees, HMM/CRF/TCN/LSTM/event-transformers, "collect a few hundred labeled
   windows and train", the 6-month morph schedule) is over-engineered: no labeled
   corpus, no fleet at scale. Tier-0 rules + ONE `claude -p` Haiku call on ambiguity
   answers every original question (stuck? waiting? errored? done?). Defer learning.

## Reuse map — most of the substrate already exists

Reuse, don't rebuild: pane capture (`tmux_capture`), the durable per-session stream
log (`_read_log` — this IS the rolling window, so classification works stateless
off the log with no daemon), spinner-stripped change detection (`web._normalize`/
`_SPINNER_RE` → diff NORMALIZED text so "false-busy" spinner churn reads as zero
diff for free), the up-channel signals (`_new_signals`), AI/tool detection
(`_detect_agent`), session metadata/liveness (`_live_sessions`/`_session_alive`),
the "actively generating" tell (`_looks_generating`/"esc to interrupt"), the
approval/danger regex (`_DANGER_SCREEN`), and — crucially — the exact fixed-cost
model harness (`_claude_decide`: `claude -p … --model claude-haiku-4-5…`, one-line
JSON parse, Haiku→Sonnet escalation). NEVER the anthropic SDK.

Genuinely new: ~4 regexes (traceback/build-fail, test-summary+counts, success-marker,
context/quota-exhaustion), rolling counters (repeated-command, repeated-error-signature,
meaningful-diff-ratio via `difflib`, log-growth-without-command-change, interrupts),
a single `extract_features()`, the FSM+confidence `classify()`, a `tmux_classify`
tool / `emux judge` CLI / web badge, and one cheap `git status --porcelain` probe
over `cwd` for the "did any artifact change" input.

## States (FSM + confidence) — each mapped to its cheapest signal

signal-derived = 0.9+, scrape-derived = 0.6–0.8, model = its stated value.
running · reading-planning(optional) · editing(→collapse into running in v1) ·
waiting-external · waiting-human(NEED signal 0.95 → else approval+prompt) ·
thrashing · stuck · error(ERROR signal → else traceback) · done-idle(IDLE/DONE
signal → else prompt+success) · dead(`_session_alive` false, 1.0) ·
dangerous-blocked(existing `blocked_dangerous` event).

## Failure flags (independent of state — "busy" ≠ "productive")

- **thrash** — repeated (command, error-hash) ≥3 while fail_count flat AND artifact unchanged.
- **stuck** — meaningful-diff ≈ 0 beyond T, not at a prompt, not generating, no NEED.
- **token-waste** — large log growth, no new command, no artifact change.
- **context-exhaustion** — quota/context regex hit, OR the plan block re-emitted (hash it).
- **hidden-wait** — approval visible + stale + not attached.
- **false-busy** — already free: diff normalized text, never raw.

## Model tier + escalation (fixed-cost only)

Tier 0 rules cover ~80% (any ground-truth signal, or a clean single-detector hit) —
no model. Tier 1 `claude -p` Haiku ONLY on ambiguity (unknown / tie / conflicting
flags), fed a compact ~300–600-token bundle (features + recent signals + last ~15
normalized deduped lines — NEVER the raw transcript) → returns
{state, confidence, summary, recommended_action}. Tier 2 (`_NAV_MODEL_ESCALATE`)
only when Tier 1 confidence low AND stakes high. Reuses the existing harness verbatim.

## API surface

- MCP tool `tmux_classify(target)` → {state, confidence, summary, evidence, flags,
  recommended_action, tier_used}. `emux judge [target]` CLI (all sessions if no arg).
- Web: per-session state chip on each grid/flow tile; a "needs attention" filter.
- **Highest leverage:** `tmux_wait(until="attention")` — block until the judge flags
  waiting-human | stuck | error | thrash on any target. This turns the classifier
  into an EVENT SOURCE for a supervisor — the fleet thesis ("react, don't poll").

## v1 scope (smallest useful)

`emux/judge.py` (`extract_features` stateless off `_read_log` + `classify`),
signal-first classification, the 4 new regexes + counters, the failure flags, the
`tmux_classify` tool + `emux judge` CLI, the `git status --porcelain` artifact
probe, and Tier-1 Haiku only on ambiguity. Defer: all learned models, Tier-2,
fleet-role judges, adaptive polling, a separate summarizer, daemon-stateful counters.
