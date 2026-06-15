# emux web — north star

## The job

**Watch and steer several local agent/tmux sessions from one screen.**

After a handful of `claude`, `codex`, build, and backfill sessions accumulate
in tmux, the friction is: *which session is doing what right now, which one is
stuck, and how do I jump in without `tmux attach`-ing around one at a time.*
`emux web` answers that from a browser:

- **grid / groups / activity** — every session as a live mini-pane, glowing when
  it changed; grouped by tag; with a per-session change strip.
- **flow** — the sessions laid out as a `manages` hierarchy of live panes
  (orchestrator on top, the agents it drives below), each titled with the AI
  detected running in it (Claude Code, Codex, Gemini, Hermes…).
- **click any box → zoom modal** — the full live screen plus an input bar to
  prompt/steer that session. Grab the wheel, then let go.

## Target user

A founder/operator (Daniel) running a small fleet of local agent sessions who
wants one cockpit instead of N terminal tabs.

## Success signal

You leave `emux web` open on a second monitor and it becomes the place you
glance to answer "what are my agents doing?" — and the place you click to
nudge one — instead of cycling through tmux panes.

## Non-goals (deliberately)

- **Not the deployed/hosted fleet view.** emux sees tmux on *this machine*.
  The Railway/bot-farm/MCP fleet is a different, bigger surface.
- **Not multi-user / no authentication.** It binds localhost and treats that as
  the trust boundary (with Host/Origin guards against browser-based forgery).
  Don't expose it to a network you don't fully control.
- **Not a tmux replacement.** It observes and drives existing sessions; it never
  spawns or kills them. Lifecycle stays yours.
- **`manages` edges are declared, not observed.** The hierarchy reflects what
  you put in the registry, not real delegation traffic. An evidence-based
  version (edges from observed `tmux_send` traffic) is possible future work.
- **Desktop-first.** The layout assumes a wide viewport; mobile is out of scope
  unless that changes.

## How "done" is measured

Verification lives in `.converge/` — a Converge target lattice that separates
real-surface proof (the live daemon + real tmux, via
`.converge/real_surface_probe.py`) from controlled-harness proof (the pytest
suite, which mocks tmux). Track real `score`, not `qualified_score`.
