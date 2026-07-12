## Philosophy

emux exists so an agent can pick up a terminal that is already running and drive
it — watch what is on the screen, type the next thing, judge whether the task is
done — the same way a person would, without owning the machine's whole world. A
terminal is the most universal interface there is: every tool, every box, every
half-broken TUI speaks it. If an agent can observe-and-drive a tmux session, it
can operate anything a human at a keyboard could, including things that have no
API and never will. The belief that stays true when everything else changes is
that **driving beats reimplementing**: you do not need a bespoke integration for
each tool when you can read its screen and press its keys. The second belief is
that **context is the scarce resource**, not compute. An agent drowns not
because it cannot act but because watching the work fills the mind that is
supposed to be directing it. emux is the substrate that lets an agent both drive
what exists and push the noisy part of that driving down a level, so the
directing mind stays clear.

## The Friction

The concrete incident: an operator with a dozen `claude`, `codex`, build, and
backfill sessions open in tmux could not answer "which one is stuck, which one
is waiting on me, and how do I nudge it without attaching to each in turn." Every
`tmux attach` was a context switch; every long-running agent session buried its
one important line under a thousand routine ones. Worse, when the operator tried
to drive a remote or long TUI task directly from their own session, every screen
capture and every keystroke echo landed in *their* context — the very context
that was supposed to be steering, not transcribing. The friction is not that the
work is hard; it is that **watching the work costs the watcher their head**. A
migration that prints forty screens, a remote install wizard, a chatty agent —
driving any of them by hand means paying full attention to output you will
throw away, until there is no attention left for the decision that actually
matters.

## The Cost of Not

If emux is never built, or is built but stays a dumb driver, the agent operating
a fleet pays in the one currency it cannot mint: working context. It will read
the same status screen fifty times, hold a remote build's entire log in mind to
extract one error, and lose the thread of the mission because the mission got
buried under transcription. The operator pays too — back to cycling through tmux
panes, back to N terminal tabs, back to being the integration layer by hand. And
the deeper cost is a ceiling on autonomy: an agent that must personally witness
every keystroke of every sub-task can only manage as much work as fits in one
context window. That ceiling is exactly what stops "leave it running" from being
true. A shortcut is acceptable here only if it does not raise that ceiling's
cost — anything that makes the directing context hold more churn is the wrong
trade, no matter how much code it saves.

## Why Not The Alternatives

Several paths were tried or considered before landing on observe-drive-plus-
nesting, and each was insufficient for a specific reason:

- **A bespoke API/integration per tool** — insufficient because it does not
  generalize: every new tool is new engineering, tools without APIs are simply
  unreachable, and the long tail of half-broken internal TUIs is exactly where
  the work is. Driving the screen reaches all of them at once.
- **The agent drives the leaf session directly from its own context** —
  insufficient because it puts all the drive-churn in the one context that must
  stay clear. It works for a three-capture task and collapses for anything long,
  chatty, or remote; the watcher loses their head to transcription.
- **A blanket "never spawn, never kill" invariant** (the original charter) —
  insufficient because it forbids the very move that solves the context problem:
  a manager session that spawns and drives a sub so the churn lands one level
  down. Kept literally, it makes emux safe and small and unable to grow the
  capability the friction demands.
- **Let anything, including the autonomous loop, spawn freely** — insufficient
  and unsafe: an autonomous judge loop that can create and destroy sessions on
  its own initiative is a materially larger blast radius (a stuck loop that
  spawns without end, a goal that kills the wrong session). The real answer is
  to scope spawning to explicit invocation, not to permit it everywhere.
- **A hosted/remote fleet controller instead of tmux** — insufficient as the
  first surface: it solves the deployed case while abandoning the operator's
  actual laptop full of local sessions, which is where the daily friction lives.

## The Unique Offer

What emux gives that nothing else does: a single substrate where an agent can
**observe, converse with, navigate, and autonomously pursue goals through any
tmux session — local or remote over ssh — and arrange those sessions into a
management tree so context cost stays flat as the work grows**. The nesting is
the part no plain terminal driver offers: spawn a manager that itself spawns and
drives a sub, and the parent context only ever exchanges high-level goals and
status with the manager. Drive-churn is absorbed at each layer instead of
flowing all the way up. That is the difference between an agent that can operate
one session at a time and an agent that can direct a fleet without drowning. If
that offer cannot be named, emux is just another tmux wrapper; the offer is
precisely the flat-context-cost tree.

The offer compounds over ssh. Because every tool carries a `host`, a single
local manager can reach through to another machine, spawn a session there, and
drive it — and that remote session can itself be a manager that reaches onward.
The tree is not confined to one box; it spans machines, and the context-cost
argument holds at every hop. An operator sitting at one laptop can direct work
running across a dozen remote hosts and still only hold, in their own context,
the handful of high-level manager conversations. No API-per-tool integration and
no hosted controller gives that particular shape — reach without ownership,
depth without context blowup — on the terminals that already exist.

## How It Grows

emux improves itself along three lines. First, capability tiers accrete on the
same observe-and-drive substrate — from attach/send/capture to ask, to navigate,
to autonomous goal mode, to nested management — each layered without breaking the
one below, and each recorded as a deliberate decision rather than accreted across
commits. Second, the registry becomes a richer map: today `manages` edges are
declared, and the growth path is edges observed from real `tmux_send` traffic, so
the tree reflects what is actually driving what. Third, every capability that
touches session lifecycle or the autonomous loop leaves an antibody behind — a
runnable case in the charter and a test in the suite — so the invariant that made
emux trustworthy cannot silently rot as the tool grows. Growth that cannot be
verified is not growth; each new tier must arrive with the check that proves it
did not cross the line the autonomous loop must never cross.

## Metric

name: autonomous_loop_lifecycle_calls
kind: count
target: 0

## Serves

parent: root
how: emux is a substrate organ of the Eidos ecosystem; it serves the root telos
  (ns_1118670c3875 in cockpit-eidos) by giving the agent a universal body for
  operating terminals. Cross-repo parent chaining is not yet resolvable by telos
  (the parent file check is same-repo), so emux registers as a local root until
  that chain exists; the intended parent is the ecosystem root charter.

## Invariants

### autonomous_loop_never_owns_lifecycle
must: The autonomous judge loop (`pursue` and `navigate`) never emits new-session or kill-session — it drives existing sessions only, and can never create or destroy a session on its own initiative.
case: python3 -c "import inspect,sys; sys.path.insert(0,'src'); from emux import server; s=inspect.getsource(server.pursue)+inspect.getsource(server.navigate); assert 'new-session' not in s and 'kill-session' not in s"
irreversible: false

### spawning_is_confined_to_explicit_tmux_spawn
must: Session creation/replacement lives only in tmux_spawn, invoked directly by an operator or a managing agent — never emitted from the autonomous loop or scattered across the codebase.
case: test "$(grep -rl 'new-session' src/emux/ | grep -c server.py)" = "1"
irreversible: false

## Requirements

### the_nested_manager_pattern_is_documented
must: The context-offloading pattern (a manager spawns and drives a sub so churn stays one level down) is written as a skill an agent can load, not left as folklore.
case: test -f skills/nested-manager/SKILL.md

### spawn_can_declare_the_manager_tree
must: tmux_spawn can record a manager→sub edge at spawn time, so the management tree is first-class and not a second manual step.
case: python3 -c "import inspect,sys; sys.path.insert(0,'src'); from emux import server; assert 'manages' in inspect.signature(server.tmux_spawn).parameters"

## Preferences

- flat context cost as work grows, over cleverness in any single driver
- declared tree today, observed tree tomorrow — ship the honest version now
- token burn is an acceptable cost for universality; lost operator context is not
