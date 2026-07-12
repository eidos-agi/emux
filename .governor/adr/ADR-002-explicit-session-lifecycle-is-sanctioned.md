---
id: "ADR-002"
type: "decision"
title: "Explicit session lifecycle (tmux_spawn) is sanctioned; the autonomous loop still never spawns"
status: "accepted"
date: "2026-07-12"
supersedes: "the blanket never-spawn clause of GUARD-001 / ADR-001"
---

## Context

emux's founding invariant (GUARD-001, ADR-001, telos metric for
ns_1271c383bf3e) was: **never new-session, never kill-session — every
capability, the user owns lifecycle.** It was written 2026-07-03.

On 2026-07-12 `tmux_spawn` landed (commit 96e5c68): a tool that creates a
driveable session — local or remote over ssh — and it calls `new-session` /
`kill-session` directly. It contradicted the founding invariant, and neither the
guardrail, the ADR, nor the telos metric were amended to match. A governance
review caught the contradiction before more was built on top of it.

The trigger: the **nested-manager** pattern (a manager session that spawns and
drives a sub, so drive-churn stays one level down and the parent context only
manages the manager). That pattern is spawning by definition. It cannot exist
while "never spawn" stands.

## Decision

**Scope the invariant instead of dropping it.** Founder-authorized (Daniel,
2026-07-12):

- The **autonomous loop** (`ask` / `navigate` / `goal` / `pursue()`) still
  **never** spawns or kills. The judge loop drives existing sessions only. This
  is the part that made the invariant valuable — an autonomous operator must not
  create/destroy lifecycle on its own initiative.
- **Explicit** session lifecycle is sanctioned: `tmux_spawn` may create or
  replace a session under direct invocation by an operator or a managing agent.
  The human/agent that calls `tmux_spawn` owns that act; the autonomous loop
  never chose it.

GUARD-001 is amended to this scoped form. The old telos north star
ns_1271c383bf3e (plain "never spawn" metric) is closed `pivoted` and superseded
by a charter-backed north star carrying the scoped invariant as a runnable case
(the `pursue()` loop emits zero new-session/kill-session).

## Why not the alternatives

- **Keep the blanket invariant; spawn lives outside emux.** emux stays
  drive-only, and sessions are created by a shell, the human, or a separate
  tool. Rejected: emux already reaches across ssh and owns the registry and the
  drive loop; forcing lifecycle out to a second tool splits one coherent
  capability across two, and the nested-manager pattern — the thing we actually
  want — would need a non-emux spawner on every box just to create the sub the
  emux manager then drives. The seam buys nothing and costs a tool boundary in
  the hot path.
- **Drop the invariant entirely; let anything spawn.** Rejected: the invariant's
  worth is real and specific — an *autonomous* judge loop that can create and
  destroy sessions on its own is a materially larger blast radius (a stuck loop
  that spawns spawns spawns, a goal that kills the wrong session). Keeping
  never-spawn on the autonomous loop preserves exactly that protection while
  freeing the explicit path. Scoping is strictly safer than dropping and costs
  nothing the pattern needs.

## Consequences

- `tmux_spawn` is now doctrine-legal, and the `nested-manager` skill documents
  how to use it for context offloading. `tmux_spawn` gained a `manages` param so
  a manager→sub edge is declarable at spawn time (rendered in `emux web` flow).
- The autonomous loop's docstrings ("emux never spawns; create the session
  first") remain correct and are now the *point*, not an accident — they mark the
  line the judge loop must not cross.
- The telos metric is restated as "the pursue() loop emits zero
  new-session/kill-session," not "emux never spawns." This is now carried by the
  charter-backed north star **ns_4ba3587b3b24** (charter_hash b707ce1bf7a2545d),
  registered 2026-07-12 with invariant `autonomous_loop_never_owns_lifecycle` and
  its runnable case. The old north star ns_1271c383bf3e was closed `pivoted`.

## Note on process

Same retroactive shape as ADR-001: `tmux_spawn` was built, the contradiction was
found, the decision was made and recorded. The correct order (research → record
→ build) would have caught the invariant conflict at design time. Recorded here
so the next lifecycle change starts from an honest charter.
