---
name: nested-manager
description: Push a noisy or long-running drive loop DOWN a level so it doesn't fill your context. Spawn a manager session that itself spawns and drives a sub-session; you only talk to the manager. Use when driving a session would generate lots of capture/send churn (a long TUI task, a chatty remote build, a multi-step agent job) — especially on a remote box.
---

# nested-manager — drive the manager, not the leaf

## The one idea

Driving a session directly means every `tmux_capture` and every screen of
output lands in **your** context. A long or chatty task drowns you in churn you
don't need — you only care about milestones and blockers.

So don't drive the leaf. Spawn a **manager**: a `claude` session that has emux,
and tell it to spawn and drive the **sub** (the leaf). The manager runs the
tight observe/act loop against the sub — that churn lives in the *manager's*
context. You exchange only high-level goals and status with the manager.

```
you (this context)  ──manage──▶  MANAGER (has emux)  ──drive──▶  SUB (the leaf)
    holds: goals, status              holds: the drive-churn        holds: the work
```

Each layer absorbs the verbosity of the layer below. Your context stays bounded
no matter how noisy or deep the real work gets. The manager can do the same to
*its* subs — it's a tree, and every node filters detail for its parent.

## When to reach for it

Reach for it when the drive loop is **expensive to watch**:

- A long TUI task (an install wizard, a migration, a multi-screen agent run).
- A chatty session whose output you'd have to capture many times.
- Work on a **remote** box — the manager sits next to the sub (one ssh hop away
  or on the same machine), so its drive loop is low-latency and local to it,
  while you stay one more hop up.
- Several leaves at once — one manager per group, you talk to the managers.

**Do NOT reach for it** when driving the thing yourself is cheap: a one-shot
command, a single `tmux_goal`, a quick capture. Nesting has a cost (a whole
extra `claude`); don't pay it to save three captures. Ponytail applies to
orchestration too — the flattest thing that works wins.

## The recipe

1. **Spawn the manager with emux + a brief, and declare the edge.** The
   `command` is a `claude` seeded with the manager brief (below). `manages`
   records the manager→sub edge up front so `emux web` draws the tree.

   ```
   tmux_spawn(
     name="mgr-migrate",
     host="eidos@box",                     # omit for a local manager
     command='claude "<manager brief — see below>"',
     manages=["sub-migrate"],              # the sub it will create
     description="manages the DB migration sub",
   )
   ```

2. **Drive the manager, not the sub.** Talk to it at a high level. Either
   converse:

   ```
   tmux_send(target="mgr-migrate", keys="status? one line.", by_registry_name=True)
   tmux_capture(target="mgr-migrate", by_registry_name=True)
   ```

   or hand it a whole goal and let it run its own loop against the sub:

   ```
   tmux_goal(target="mgr-migrate",
             goal="Drive your sub to finish the migration; report only milestones and blockers.",
             by_registry_name=True)
   ```

3. **You never touch the sub.** No `tmux_capture` on `sub-migrate` from up here —
   that would pull its churn back into your context, defeating the point. If you
   need detail, ask the manager for it.

## The manager brief (what you seed the manager `claude` with)

Bake the manager's job into the `command` so it knows it is a manager the moment
it boots:

> You are a MANAGER session. You have emux. Spawn a sub-session named
> `sub-migrate` and drive it to accomplish: `<the real task>`. Run the tight
> capture/send loop against the sub yourself — keep its output in YOUR context.
> Report UP to your parent tersely: only milestones, decisions that need a
> ruling, and blockers. Never dump raw sub output upward unless asked. When the
> task is done, say so in one line.

The manager then does, in its own context:
`tmux_spawn(name="sub-migrate", command="<the work>")` →
`tmux_capture`/`tmux_send`/`tmux_goal` against `sub-migrate` until done.

## Preconditions & failure modes

- **The manager needs emux + `claude` on its PATH.** That's what lets it spawn
  and drive the sub. If the box doesn't have emux, the manager degrades to a
  plain session you drive directly — no second level, no offloading. Check the
  host has emux before you count on nesting.
- **ssh must reach the box** (for a remote manager). `tmux_spawn` returns
  `spawn_failed` with a hint if it can't.
- **Declared, not observed.** The `manages` edge is what you record, not what
  emux watches. If the manager drives something else, the tree in `emux web`
  won't know unless you (or it) register that edge too.
- **Don't over-nest.** Two levels (you → manager → sub) covers almost everything.
  A third level (manager → sub-manager → leaf) is for genuinely large fan-outs;
  each level is another `claude` to pay for.

## Why this is emux's job

emux already drives existing sessions and reaches across ssh. The nested-manager
pattern is just those primitives arranged so **context cost stays flat as work
grows**. The spawn that makes it possible is an *explicit* lifecycle act
(`tmux_spawn` under direct invocation) — the autonomous loop (`navigate`/`goal`)
still never spawns on its own. See ADR-002.
