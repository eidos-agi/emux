# Changelog

All notable changes to Emux are documented here.

## v0.61.0 - 2026-07-14

- **Missions can actually run unattended.** The planner now picks a `permission_mode` (default / acceptEdits / bypassPermissions — most autonomous that's safe for the mission); emux appends `--permission-mode` to claude commands deterministically (never trusts model-written flags, never doubles an existing flag). The confirm screen shows a `perms` line with what the mode means.
- **Liftoff proof.** "tmux accepted the keys" is not "the mission is running": after spawn, emux captures the pane at 2.5s and prints it ("first light"), flagging command-not-found / not-logged-in launches. Already caught a real one in testing: Claude Code's folder-trust dialog silently stalling a fresh cwd.
- **Name-collision guard.** `tmux_spawn` kills any same-name session, and a live agent session is unrecoverable state. If the mission name is already live (host-aware), the flow now offers unique-name (default, auto-suffixes -2/-3…), replace, or abort — never a silent kill.


## v0.60.2 - 2026-07-14

- **Mission plan shows the path and the attach command.** Two new lines in the confirm screen: `path` — the hop chain the mission takes (`this terminal → ssh Daniels-Mac-mini.local → tmux new-session 'check-dally' → claude`), derived deterministically from the plan fields, never from the model; and `attach` — the exact command a head will use (`ssh -t … tmux attach …` for remote).
- **Planning spinner.** `claude -p` takes 5–15s; the chat now shows a live spinner ("planning… the AI is drafting your session spec") instead of dead air. Suppressed when stdout is not a tty.


## v0.60.1 - 2026-07-14

- **Fix: new-mission key is Ctrl-N.** The filter input takes initial focus and eats plain letters, so pressing `n` just typed "n" into the filter and the binding never fired. `ctrl+n` is now a priority binding (works even while the filter is focused); plain `n` still works once the list has focus, and Enter on the `(new mission)` actions row works as before.


## v0.60.0 - 2026-07-14

- **New mission: `n` in the TUI / `emux new`.** Describe what you want in plain English ("look into my finances on mac-mini"); a planner (`claude -p`, fixed-cost, model via `$EMUX_PLAN_MODEL`, default sonnet) turns it into a session spec — name, host (it knows the registry's remote hosts), cwd, exact launch command — and asks at most one clarifying question per turn. Nothing runs until you confirm the exact plan; free-text feedback refines it. On confirm it spawns via the existing `tmux_spawn` (local or remote), registers under the mission name with the summary as description, and offers to attach.


## v0.59.1 - 2026-07-14

- **Fix: an answered gate lingering in scrollback no longer re-escalates.** Found by the v0.59.0 live test (fraude fake claude, zero tokens): after the policy engine answered a gate, the dialog text still in the capture re-triggered detection and the one-attempt guard escalated a gate that was already resolved. Gate detection for escalation now judges the LIVE BOTTOM (last 10 non-blank lines) — the same window the needs-you flag uses — so detection and escalation agree on what "gated" means.


## v0.59.0 - 2026-07-13

- **The determinism release — judgment moved out of the token path.** Seven fixes from a day of live fleet driving:
  - **Gate policy engine.** The web daemon answers known modal gates itself from `~/.config/emux/gatepolicy.json` (regex over the pane's live bottom → exact keystrokes, host-aware). Destructive text never auto-answers; one attempt per gate, then it escalates to a human as before. Zero model calls for the recurring 90%.
  - **Gate ledger + `emux gates`.** Every gate sighting is logged (`~/.local/share/emux/gates.jsonl`) with how it was handled; escalated gates are joined against the audit trail to find the keystroke a human actually sent. `emux gates` reports recurring gates with their usual answers and prints ready-to-paste policy rules — the ledger is the labeled dataset, no ML infra needed.
  - **`tmux_wait` returns the decision.** Ready entries now carry the Tier-0 classification (state/flags/summary) and any modal gate text — act from one call instead of the wait→classify→capture triple.
  - **`tmux_send` reliability.** Accepts a LIST of named keys (`["BSpace","BSpace"]` — a multi-key string was silently typed as literal text), and verifies literal sends into AI panes actually submitted: if the paste-guarded composer still holds the text it retries Enter once and reports `submitted`/`resubmitted`.
  - **`tmux_classify` reads the live pane first.** The stream-log tail kept long-cleared gates classifying as `waiting_human`; the pane is current state, the log is the fallback.
  - **Remote heads.** `emux head` now works for sessions on other machines (`ssh -t` attach, host-aware liveness check) — a gated remote worker's auto-approved head opens instead of erroring "not live".
  - **`emux doctor` / `tmux_doctor`.** Environment diagnosis: liveness, pane, capture, stream log, gate, and the probe that found today's invisible failure — tmux-server vs fresh-process filesystem access (a stale macOS TCC grant EPERMs every pane while the disk is healthy; doctor names the desk-side fix).


## v0.58.0 - 2026-07-13

- **`emux head` escalations are auto-approved now.** v0.57.0 dropped the Hancock escalation outright; the actual intent was "stop making me sign head-openers". Restored the escalation, filed at LOW risk with a matching `^emux head \S+$` allow rule in hancock's license — so when a worker gates, its terminal head just opens (hancock auto-runs the request); nothing waits in the tray. High/critical requests still always wait for a signature.
- Known gap: `emux head` is local-only — a gated worker on a REMOTE host auto-approves but the head command exits 1 ("not live"). Remote heads (ssh -t attach, as `tmux_spawn gui` already does) are the next step.


## v0.57.0 - 2026-07-13

- **Dropped the Hancock `emux head` escalation.** A gated worker no longer files a Hancock tray request queuing `emux head <session>` (the tray filled with head-openers). Gates still escalate: the NEED up-channel signal fires once per gate (wakes a parent blocked in `tmux_wait`) and the control room's needs-you surfacing is unchanged. The Hancock tray tab in `emux web` (viewing/approving other Hancock requests) stays.


## v0.56.0 - 2026-07-13

- **Fixed the tmux_wait false-idle bug.** `tmux_wait(until="idle")` reported running sessions as ready-idle instantly (empty `last_line`) when they had no armed stream log — a session registered after spawn has none, and the idle detector read the missing log as quiet-forever. Now a missing log is never read as quiet: log-less sessions fall back to live pane comparison (host-aware), quiet is judged from actual pane stillness, `last_line` comes from the pane when there is no log to quote, and a log armed mid-wait upgrades back to the cheap stat path. Regression-tested in `tests/test_wait.py` (the running-session case fails on the old behavior).

## v0.55.0 - 2026-07-13

- **Plan failover facade — switch a session to another Claude account, in code.** When a session (especially the manager) runs its account out of tokens, emux can fail it over to another configured Claude account: it exits the agent, relaunches under that account's `CLAUDE_CONFIG_DIR`, and resumes the conversation (`claude -c`). Deterministic — tmux only, no LLM in the loop. Round-robins to the next available account and cools down an exhausted one (~5h) so it isn't retried too soon.
  - Configure accounts in `~/.config/emux/plans.json` (`{"plans":[{"name","config_dir"}...]}`); unconfigured falls back to a single default and the switch is guarded (needs >=2). You log each profile in — emux never touches credentials.
  - UI: a "⇄ switch account" button in the session modal (highlighted when that session is throttled). First click dry-runs and shows the target account; a second click within 4s confirms and does the switch. `GET /api/plans`, `POST /api/plan/switch` ({session, to?, dry_run?}).
  - Detection (v0.54 regex) feeds this; ML/log-based detection and auto-switch are the next steps.


## v0.55.0 - 2026-07-13

- **Manager-operable login gates.** When a managed session logs out (or is on the wrong account), supervision no longer dead-ends until a human finds the pane.
  - **Detect:** the Tier-0 classifier flags a login/auth sequence on screen (logged-out banner, "Select login method", OAuth URL + paste-code prompt) as `waiting_human` with a new **`login_gate`** flag and a "Needs login" summary, so `tmux_classify` surfaces it as actionable.
  - **Navigate:** `emux login <target>` (+ MCP `tmux_login`) drives the sequence with deterministic keystrokes — sends `/login`, steps the TUI, and prints the OAuth URL (the one hop that needs a browser). Finish with `--code <paste>`; success is verified from the screen. `--switch` changes account (`/logout` first). Works by registry name across hosts; the code is never persisted and is redacted from the audit trail.
  - Docs: `docs/emux-login.md`. Tests: `tests/test_login.py` (scripted fake tmux, zero tokens) + `login_gate` classifier cases.


## v0.54.0 - 2026-07-13

- **Cost / usage-overrun detection.** emux now catches when a session hits a usage limit, rate limit, quota, or runs out of credits — read straight from the pane's live bottom (specific phrases only, so ordinary mentions of "limit"/"rate" do not false-fire). A cost-limited session gets a gold ring + a "💸 USAGE / COST LIMIT" badge on its tile/card, and a gold top banner counts how many sessions are throttled and jumps you to one. Distinct from the red needs-you signal, because a throttle is a money/budget event, not a decision gate. (Detection is unit-tested; the automated *response* to an overrun is the next step.)


## v0.53.0 - 2026-07-13

- **Gist warm now waits for a real pause, not the between-turns flicker.** The proactive warm fires only when a session is settled AND has sat still (no pane change) for a longer pause (default 10s, `EMUX_GIST_PAUSE`), deduped by content so a still session warms once per genuine stop — instead of on every brief running↔idle flip between an agent's turns.


## v0.52.0 - 2026-07-13

- **The Gist runs the moment a session stops — and is cached.** When a session transitions from running to settled (idle / asking / waiting-on-you / errored), emux warms its gist in the background right away, so the digest + suggested replies are ready before you open it — no waiting.
- **Where it caches / cache-busts.** The gist is cached server-side keyed by a hash of the exact pane slice the model reads. An unchanged pane serves the cache instantly (no model call); the pane changing — including when you send a reply — changes the hash and self-busts; a session dying evicts its entry. Warming is edge-triggered (one call per stop, not per poll), inflight-guarded (no concurrent duplicates), and routes through the model-routing settings, so it can go to a local NIM.


## v0.51.0 - 2026-07-13

- **Reeves filter — a personal context, separate from Eidos.** Added a distinct "Reeves" company for the personal ecosystem (reeves-wealth / lens / cockpit / operator / grocery, ~/.reeves*, plugins/reeves). Any `reeves` path now reads as Reeves rather than the generic Personal, with its own periwinkle pill and a cool slate/navy skin (so switching to Reeves visibly signals personal mode). Routes to the mac-mini by default (`company_host.reeves = daniels-mac-mini`). Non-reeves personal paths still read as Personal.


## v0.50.0 - 2026-07-13

- **Confidence pies on the choices, in the app.** Each suggested reply in the floating chat now shows a green pie/wedge filled to how likely emux thinks that reply is the right move, plus the %. The Gist returns a per-suggestion confidence (0-100, comparative, best-first); the chip renders it as a `conic-gradient` green wedge + a green % — so you can see at a glance which choice to trust.
- **The floating chat now keeps a transcript.** The gist opens the conversation as an assistant bubble; your sends (chips or typed) echo as your-side bubbles, so the panel reads like a real chat local to that terminal.


## v0.49.0 - 2026-07-13

- **Choices are now a floating per-terminal chat (choose one, or type).** Instead of a bar of option bubbles above the composer, a session's choices live in a floating chat widget local to that terminal (NCDMV-style). It gathers the on-screen menu options (solid chips — picking one walks the cursor and confirms) and the gist's suggested replies (bordered chips), plus a "choose one, or type your reply…" box for a custom message. It floats bottom-right, auto-opens when a real choice lands, and collapses to a launcher bubble with a count badge. The gist digest text stays where it was; only the interactive choices moved into the chat.


## v0.48.0 - 2026-07-13

- **"Needs you" is now impossible to miss.** A session waiting on you gets a red ring + glow and a pulsing "⚠ NEEDS YOU" corner badge (red reads as attention against the amber theme; the old amber-on-amber ants blended). Detection broadened: besides formal gates/questions, a session whose gist reads like it's parked on a human action ("on your desk", "awaiting your approval", "until you authorize") is flagged too — catching an idle manager that's actually relaying a decision to you. Kept conservative so normal output (verify/paste/login/tests) does not false-positive.
- **Gone sessions are cached, not blanked.** When a session you were watching ends, its tile keeps its last gist ("⏹ session ended · 2m ago — last: …") instead of an empty "tmux session gone" ghost, so you can still see what it was doing and roughly when it went.
- **The filter searches content, not just names.** Typing "cloudflare" now matches a session whose gist/description/target mentions it — not only its name — so you can find the one that needs attention by what it is doing.


## v0.47.0 - 2026-07-13

- **Model-routing settings page (⚙ SETTINGS).** Route emux's model-backed tasks — The Gist (session digest + suggested replies) and session placement — to a self-hosted NIM instead of the Claude subscription, to cut token cost. Configure a NIM endpoint (base URL, model, optional key), test the connection, and pick the backend per task. **NIM must be self-hosted / fixed-cost** — if it's unset or unreachable, emux falls back to `claude -p` automatically, so nothing breaks. Persisted to `~/.config/emux/models.json`.


## v0.46.0 - 2026-07-13

- **Choice buttons are now solid-filled, not just bordered.** The bordered version still blended in some skins; the suggested-reply and menu-answer bubbles are now filled with the skin accent (on-accent text, drop shadow, hover-lift). Luminance offset against the page background is large in every skin by construction.


## v0.45.0 - 2026-07-13

- **Choice buttons stand out in every skin.** The suggested-reply and menu-answer bubbles used a faded accent border that blended into some company skins. They now carry the full accent-color border, accent text, weight, and a soft shadow — keyed to each skin's `--amber`, so they contrast against that skin's own background by construction. The Hancock deny button got a visible border too.
- **Hancock approve/deny gives an inline toast, not a browser alert.** A failed approve/deny now surfaces as a slide-in toast inside the tray (and a success confirmation), instead of a blocking `alert()` dialog. The acting card dims while in flight.
- **The Gist recovers itself.** If the modal digest ("the gist") fails to summarize but the session's pane then changes, it retries — capped at 10 attempts — instead of staying stuck on the error. The error line says it will retry when the session moves.


## v0.44.0 - 2026-07-13

- **Hancock tray opens itself.** No more waiting for a click: the moment a request needs your signature the tray slides open on its own. Close it and it stays closed for *those* requests — but a brand-new request re-opens it. When the queue clears, it slides away. Async by default; the banner is now the fallback, not the only signal.


## v0.43.0 - 2026-07-13

- **Hancock approvals, in-app and loud.** emux now surfaces Hancock's pending signing tray directly. When anything needs your signature a red banner pulses across the top (shaking bell + count + a red outline on the whole app) — impossible to miss. `⧉ HANCOCK` in the topbar carries a live count badge. Click either to open a right-side tray listing each request (command, why, cwd, risk-colored), with **approve & run** / **deny** per item. Approve routes through the real `hancock approve` path (signs + runs, scrubbed env so the daemon isn't blocked by Hancock's Claude-Code guard); deny records the decision and marks the request denied so a worker blocked in `hancock wait` unblocks. Opening the tray quiets the banner. Read path is a direct read-only query of Hancock's SQLite tray — no CLI guard involved.


## v0.42.0 - 2026-07-13

- **Thinking indicator.** While a session is generating (`esc to interrupt` on screen), the modal header shows bouncing dots + a live timer of how long it's been thinking (from the agent's own meter). Movement you can feel, and how long.
- **Pending-send bubble.** Click Send (or a suggested reply) and the message is held in a dashed "received · sending" bubble above the box until the session's pane echoes it — so you know emux got it even while a remote session lags over ssh. Clears itself when the message lands; restores your draft if the send fails.


## v0.41.0 - 2026-07-13

- **Deep links.** The view (grid/groups/activity/flow), the tag + company filters, the search text, and the open session are all reflected in the URL hash (`#view=flow&company=greenmark&tag=agents&session=ggo-chat2`) and restored from it — bookmark or share any state; it survives reload and back/forward. Falls back to localStorage when the URL is bare.
- **Clicks actually submit.** The web send path (modal composer, the suggested-reply bubbles, the menu bubbles) now waits the agent's measured paste-settle between the text and the Enter, so a clicked reply submits instead of the Enter being swallowed by Claude's paste detection.
- **Decisive replies.** Suggested replies are phrased to authorise the agent to PROCEED and finish autonomously without coming back to re-confirm — one click resolves it, rather than triggering another round of questions. A cancel/reverse option is the one exception.


## v0.40.0 - 2026-07-13

**The gist + "what do I say" — so a session's wall of text becomes a decision you can make.** Opening a session was dumping raw terminal output with a blank prompt; if you weren't the one who'd been driving it, you had no idea what it wanted.

- **A digest panel at the top of the modal**: one `claude -p` call (fixed-cost CLI, on-demand only when a modal is open — never per poll) reads the recent pane and returns a 1-2 sentence gist plus a few **ready-to-send replies as clickable bubbles**. Click one and it's sent as the prompt. `↻` re-reads. Proven live: a "should I switch to gm_auth_strict?" session digested to that question with yes / no / explain replies.
- **Menu parser fix**: a Claude dialog (trust prompt, selection menu) renders with blank space BELOW it, so the last raw lines are empty and the menu sits higher — the parser and the gate-tail check now scan the last non-empty lines, so those dialogs are detected (clickable option bubbles + ants) instead of read as idle.

## v0.39.0 - 2026-07-13

**You can tell when you're personally needed — and answer with a click.**

- **A selection menu is now a gate.** A Claude "Enter to select · ↑/↓ to navigate" menu (or a y/n) marks the session `waiting_human` → it marches ants in the grid AND flow. Previously only formal y/n gates counted, so a session sitting on a multi-option menu looked idle.
- **Broadened "needs you" text detection** for idle sessions that stated a decision is needed without a literal `?`: "decision needed", "waiting on you", "your call/input/approval", "approve/amend", "blocking the fleet", "pick one", etc. → state `asking` (pulsing `?` + ants).
- **Gate detection restricted to the live bottom of the pane** (last ~10 lines), so a session that merely PRINTED menu text up in its scrollback isn't falsely flagged.
- **Clickable answer bubbles.** When a session shows a numbered menu, the modal renders each option as a **clickable bubble** overlaying the chat (`_parse_options` — only for a real menu, never a prose list). Clicking navigates the ❯ cursor from where it is to your choice and presses Enter — universal across Claude/Codex cursor menus, no reliance on digit-select. Answer a fleet's prompts with taps instead of attaching to each terminal.

## v0.38.0 - 2026-07-13

**A super-cheap always-on "what's happening" rail — no model, no GPU.** A thin rail sits above each session's terminal box with a one-line, plain-English read of what the agent is doing right now; hover it for the full text.

- Deterministic, LOCAL, ~free: `_summarize` = a state verb (working / asks you / idle / error) plus the agent's last real line of output. `_headline` prefers Claude's own `⏺` action lines and strips the noise that clutters a terminal's bottom — spinner meters ("Cooked for 13m"), update notices, tips, menu items, box-spinners, shell prompts. It reads the bottom of the screen; that's the whole cost.
- On every grid tile AND flow box (with a hover overlay showing it in full), so the rail is present at every level of the hierarchy. Refreshes live in the flow without a rebuild.
- This is what a model WOULD say about a session, produced without a model — matching the "really really cheap, barely any CPU, local" constraint.

## v0.37.0 - 2026-07-13

**"It's asking you" is now a first-class state.** A session that finished a turn and is asking you a question ("say the word on the proposal", "should I…?") is not a formal gate, so nothing used to flag it. Now `_quick_state` detects a question — a trailing `?` or a question phrase in the agent's last real output lines (chrome/composer stripped) — and marks the session `asking`. The live indicator swaps the heartbeat for a **pulsing `?`**, the status pip reads "asks you", and the box marches ants (it needs you), same as a formal gate.

## v0.36.0 - 2026-07-13

**Fleet visibility + faster loads.**

- **The control room loads fast.** With several remote (rentamac) workers, `/api/grid` took ~14s because it captured each remote pane over ssh SEQUENTIALLY, and the poll loop only ever cached LOCAL sessions. Now remote captures run in PARALLEL (`_capture_many`, thread pool) and the poll loop caches remote sessions too. Measured: 14.4s → ~2s cold, 0.01s warm.
- **Per-agent status at every level (Vybhav's ask).** Each tile and flow box shows a status pip — run / idle / err / needs-you — from a cheap per-poll `_quick_state`, so a manager (and human) sees each agent's status AND its sub-agents' without opening any of them. Full precision stays in the modal (`/api/classify`).
- **Marching ants — actually marching.** The old "waiting on you" border used a masked-gradient trick that reported as animating but didn't visibly move. Replaced with the canonical four-edge marching-ants (each edge scrolls one dash-period). The ants now also wrap FLOW boxes, so a blocked sub-agent anywhere in the hierarchy is impossible to miss.
- **Heartbeat.** A RUNNING agent shows a hospital-monitor EKG (an SVG trace with a sweeping blip) instead of a static dot; idle/error/needs-you keep a colored dot. Swaps live as state changes.

## v0.35.0 - 2026-07-13

**Say it once; the system routes and starts the work.** Creating a session from an intent used to: pick a directory that ignored the company you named, ignore where that company's work belongs, and boot a BLANK agent that hadn't been told the task. Three fixes so you don't re-explain every time.

- **Kickstart.** A new session created from "say what you want to do" now hands that intent to the agent as its OPENING PROMPT (`claude <intent>` / `codex <intent>`), so it comes up already working instead of idling at a blank composer. Plain-shell sessions are untouched.
- **Company-aware directory routing.** If the wording names a company (`_intent_company_hint`), that company's directories float to the top of the choice and the model is told not to stray — so "an Eidos org digest" stops landing in `repos-aic/taskr`.
- **Standing machine preferences.** A durable routing rule maps a company to its home machine — **Eidos work runs on the mac-mini** — applied in the machine step. Defaults in code; override/extend at `~/.config/emux/routing.json` (`{"company_host": {...}}`). State the rule once; it sticks.

## v0.34.0 - 2026-07-13

**Connection-aware filtering: a group of terminals stays together.** Filtering by a company (or tag) used to match each terminal independently — and the flow view ignored the filter entirely, drawing every session. Now there's a real concept of a **group of terminals**: the connected components of the `manages` graph.

- `components()` unions terminals across `manages` edges (undirected). `shown()` is now connection-aware: if ANY terminal in a group matches the active filter, the WHOLE group shows — including members that aren't in the filtered set. So selecting Greenmark shows every Greenmark session AND the full manager→worker chains they belong to, even a connected worker that belongs to another company. An unconnected session of a different company is still excluded.
- The **flow view now respects the filter** (it built from raw `grid` before), using the same connection-aware set, so a manager and everything it manages render together. Its arrow color is themed too.

## v0.33.0 - 2026-07-13

**Readability + honest skinning.**

- **Removed the CRT scanline/vignette overlay.** It was cleverness at the cost of legibility — a dark line grid over the whole page, unreadable on the light skins. Gone.
- **Surfaces now follow the skin.** Tiles, terminal panes, the filter box, the modal input/screen, chips and the composer were hardcoded near-black (`#080705`) and stayed dark under a light theme. They now use the theme vars, so a light skin is light everywhere (cream panes, dark text) and a dark skin is dark everywhere.
- **`--on-accent`** — button/pill text on the accent color is now a per-theme token, so it stays legible whether the accent is brass, gold, or forest green (light text on the dark-brass Eidos-light accent, dark text on the lighter dark-mode brass).
- **Greenmark is now LIGHT** — its actual brand: forest-green ink (`#2d4a3e`) on warm cream (`#f5f0e8`), gold as the secondary. `:root` defaults to the Eidos-light palette so the first paint matches (no dark flash).

## v0.32.0 - 2026-07-13

**Skins: the whole UI recolors to what you're working on.** Selecting a company pill re-skins the entire control room, so the color IS the "what am I working on" signal.

- **Default = Eidos light** (warm cream `#f0ebe4` + amber-brass `#8e6129`). **The Eidos pill = Eidos dark** (`#15110f` + brass `#c4935a`). **The Greenmark pill = Greenmark Waste** (deep forest green `#16291d` + brand gold `#e8c95a` + cream) — the brand's own `#2d4a3e` green family, confirmed against `greenmark-assets/brand/palette.json` and the live site.
- A theme is just a remap of the 12 CSS vars; `applyTheme()` sets them on `:root`. `CO_THEME` maps company key → skin (unmapped companies fall back to Eidos light), and the choice persists — reload keeps the skin you were in. Adding a new company skin is one entry in `THEMES` + `CO_THEME`.
- Palettes are faithful to the Eidos brand (`eidos-assets/brand/palette.json`, light + dark) and the Greenmark brand, both researched from their canonical brand files.

## v0.31.0 - 2026-07-13

**See the fleet act, and see when it needs YOU.** Two additions that make the control room a live surface, not a static grid.

- **Live fleet feed** — a collapsible right rail (◫ FEED) showing what the agents do as they do it: up-channel signals (IDLE/DONE/NEED/ERROR/PROGRESS from every session inbox) and the meaningful tool-calls (spawn / send / register / wait), newest first, colour-coded, the newest one flashing. Skips the capture/poll read-noise. New `GET /api/events` merges the signal inboxes and the audit trail into one time-ordered stream. Polls every 2s; open by default, state remembered.
- **"Waiting on YOU" affordance** — a session sitting on a real gate (an approval menu, a y/n it can't answer itself) gets **marching ants** around its tile plus a slow breathing **orb** glow, so on a wall of tiles the one that needs your decision is impossible to miss. Driven by `needs_human` in the grid payload (from `adapters.gated()` on the live pane — precise: a genuine gate, not merely "there's a ❯ prompt", which over-fired on every Claude pane).

## v0.30.0 - 2026-07-13

**New-vs-resume: the wording now lifts the decision without overpowering it.** "I want to create an Eidos manager. He manages Kai…" was resolving to RESUME the existing `ggo-manager` — a name-match beating an explicit "create". Fixed two ways:

- **A deterministic keyword lean** (`_new_vs_resume_lean`) scores create-verbs vs resume-verbs in the phrasing and passes the model a HINT ("the wording leans NEW — treat a similarly-named running session as a coincidence"), which it weighs rather than obeys. The prompt now says naming the KIND of thing to make (a manager, a worker) is NOT a resume request. Verified: the exact phrase now returns `new`, name `eidos-manager`.
- **BM25 session ranking on the resume path** (`_bm25_rank`): when the AI does resume, running sessions are ranked by lexical relevance (name + path + description + tags) so the one the intent describes floats to the top of what the model picks from — "the greenmark reconcile work" ranks the `…/reconcile` session first. Lexical-only by design: at this fleet size the model reads the whole ranked list, so dense embeddings + RRF would be scale-work for no gain. This is the bm25 leg to fuse later when the fleet outgrows one prompt.

## v0.29.0 - 2026-07-13

**The control room sees remote workers.** The web daemon only ever talked to LOCAL tmux, so a registered session on another machine (a rentamac worker) showed as "gone", its modal failed to capture (`TMUX_CAPTURE_FAILED`, blank pane), and its classifier was stale — even while the worker was alive and working. Found the hard way: a laptop slept, the ssh attach died, the remote worker survived (as tmux is meant to), and the control room reported it dead.

- **Liveness over ssh.** `sessions_payload` probes each distinct remote host once (`_remote_live_names`, cached 5s) so a session with a `host` reads LIVE, not gone.
- **Capture + steer over ssh.** `capture_payload` and `send_payload` take a `host`; the modal's `/api/capture` and `/api/send` resolve it from the registry (`_session_host`), so the pane fills and you can drive a remote worker from the modal. Classify was already host-aware (`judge` reads the host from the registry).
- Proven live against rentamac: `ggo-build` reads live=true host=rentamac, captures its pane over ssh, and classifies `running` — where before it was "gone" with a failed capture.

## v0.28.0 - 2026-07-13

**A blocked worker reaches the human's tray — silence is now impossible.** The gate-escalation added in v0.25 fired a `NEED` signal into a jsonl nobody watches; it now ALSO files a **Hancock request**, so a worker stuck on an approval gate lands in the operator's actual signing tray with the specific ask.

- When the daemon polls a session and `adapters.gated()` sees a gate, it files `hancock add "emux head <session>"` with `-why` carrying the exact block ("blocked on a codex gate: update available — approve to open its terminal and resolve, or deny to leave it") at HIGH risk, so it always waits for a signature. One request per gate; rearmed when the gate clears.
- The daemon runs a **scrubbed env** (drops `CLAUDECODE`/session id) so Hancock's "don't drive the queue from Claude Code" guard doesn't reject it. Proven live: a synthetic Codex update-gate produced a real HIGH request in the tray.
- Best-effort and isolated: if hancock isn't installed or the call fails, the NEED signal already fired — escalation degrades, it never breaks the poll. Note: an agent can FILE a request but not withdraw it; only the human denies (in the TUI) — correct by design.

## v0.27.0 - 2026-07-13

- **A manager inherits the company of what it supervises.** A supervisor is defined by the work it manages, not the directory its process runs in — so a manager whose cwd would derive one company adopts its worker's company instead (when its managed set agrees). Explicit override still wins over both. Fixes a Claude manager running from the emux repo reading as Eidos while it supervised a Greenmark worker.
- **Explicit `company` on a registry entry.** A remote worker whose cwd the local daemon can't see (e.g. a rentamac session on `/Volumes/GREENMARK`) can carry `company` directly; the payload honors it over cwd-derivation.

## v0.26.0 - 2026-07-13

**Cross-machine, cross-vendor management — proven live.** A **Codex** manager on a MacBook supervised a **Claude** worker on a remote box (rentamac), through emux, over ssh. The manager called `tmux_capture(target="ggo-build", by_registry_name=True)` and got the worker's live pane back with `"host": "rentamac"` — it never had to know the worker was remote; it just used the registry name. The `manages` edge renders the manager→worker arrow across machines.

Two hard constraints found by doing it, both now in the Codex adapter:

- **`codex exec` cannot call MCP tools — it can only think.** Every tool call is auto-cancelled ("user cancelled MCP tool call") because Codex asks for approval PER TOOL CALL and headless has no one to answer. Verified against emux *and* a known-good server, so it is not an emux fault. Recorded as `oneshot_can_use_tools=False`: **a Codex manager must be an interactive session** (which is the right shape anyway — a manager should be warm).
- **Codex's per-tool MCP approval menu is now a gate** ("Allow the … MCP server to run tool …"), as is the second presentation of the hook gate ("Press t to trust all; enter to review hooks"). Driving blindly through either would auto-approve tools or grant trust.

## v0.25.0 - 2026-07-13

**Codex is a real fleet member now** — measured against a live Codex in tmux, not guessed. It has the same lifecycle emux already relies on for Claude, and every number below was established by experiment.

- **Codex has a NATIVE Stop hook**, same JSON shape as Claude's. Proven end-to-end: the hook fired `emux signal IDLE` into emux's inbox ~9s after the turn ended, the task's artifact was on disk, and the judge classified the session `done_idle` from the up-channel signal — zero scraping. Codex can be a warm worker.
- **Codex needs a paste-settle too, and it was MEASURED**: 0.2s does not submit, 0.4s does. Same number as Claude, but arrived at by experiment rather than inherited.
- **Fixed a real detection bug that guessing would have shipped.** Codex prints `• Working (1s • esc to interrupt)` — the *same* "esc to interrupt" string as Claude. That phrase was a Claude content-signature, so a node-wrapped Codex would have been misdetected AS Claude. Removed; Codex now has distinctive signatures.
- **SAFETY: `tmux_send` now REFUSES to type into a pane showing a modal gate** (`blocked_on_gate`, override with `force=True`). This is not theoretical. Codex's startup has three gates (directory-trust, hook-review, update-available), each of which eats keystrokes and PERSISTS the answer: sending the text "what is 2+2?" fed the `2` to the hook gate, which selected "Trust all and continue" and wrote hook-trust into the user's `~/.codex/config.toml`. The update gate defaults to "Update now (runs `brew upgrade`)", so a blind Enter upgrades the user's Codex. Typing into a gate is a config write, not a no-op.
- Codex launches unattended with `--dangerously-bypass-hook-trust` plus a pre-trusted cwd — deliberately NOT `--dangerously-bypass-approvals-and-sandbox`, which also removes the sandbox (a bigger concession than the gate requires).
- `adapters.table()` reports the honest matrix: claude and codex now full (detect/drive/read/resume/signal); gemini/grok/opencode/aider remain detect-only and declare their unknowns.

## v0.24.0 - 2026-07-13

**One adapter per agent** (`emux/adapters.py`). emux only really knew how to drive ONE agent, and Claude-specific facts were smeared through general code: the semver pane-title trick in the web daemon, `esc to interrupt` inside the judge's regexes, `settle=0.4` in `tmux_send` (a workaround for *Claude's* paste detection), and a Stop hook as *the* way a worker reports done. Each of those is a per-agent contract wearing a general rule's clothes. They now live with their agent.

Each adapter answers four questions: **DETECT** (what does it look like in a pane?), **DRIVE** (how do I type into it without the input being swallowed?), **READ** (how do I tell if it's working, blocked, or done?), **LIVE** (launch / resume / completion signal).

- **`tmux_send` now takes its paste-settle from the pane's agent**, not from the caller. A Claude pane gets its measured 0.4s (text → wait → separate Enter); a shell gets none. Callers no longer have to know one agent's quirk. Cached per session, so it costs no round-trip per keystroke.
- **Codex is a real adapter, not a glyph.** It has the same lifecycle as Claude and emux never knew: `codex resume <id>` / `--last`, `codex exec` for one-shot, and — the missing piece — **`notify` → `turn-ended`**, which is Codex's answer to Claude's Stop hook. Same idea: the harness fires at a real turn boundary, so nothing is scraped and the worker can't forget to report.
- **Adapters declare what they DON'T know.** Codex's busy/approval regexes and paste-settle are unmeasured, so they are empty and reported as unknowns rather than inheriting Claude's numbers — a wrong `busy` regex makes the judge confidently mislabel a session. `adapters.table()` prints the honest matrix of detect/drive/read/resume per agent.
- Detection is now single-source: `web._AGENT_TABLE` is gone.

## v0.23.0 - 2026-07-12

**Which agent for which scenario — a registry, not folklore.** emux spawns sessions that run AI agents; which one to run was a decision living only in someone's head. `emux/agents.py` is the smallest thing that fixes that: a table you can read, query, and correct.

- **The axis is capability, not price.** The operator subscribes to both Claude Code and Codex (flat fee), and the metered API is a hard constraint violation. So "route cheap tokens to a cheap tier to cut your bill" saves nothing here — routes are keyed on what each agent is GOOD at.
- **`agent_advice(scenario)` MCP tool + `GET /api/agents`.** Plain English in ("leave a long build running overnight") → agent, command, why, evidence, and whether it's actually installed. Omit the scenario for the whole table.
- **The new-session cascade routes the command through it.** The model no longer free-styles what runs in a session; the registry decides, and the model's suggestion is only a fallback.
- **Rejected claims are recorded so they don't get re-litigated**, each with source and date — including the "3-tier stack cuts cost 80%" claim (irrelevant under a subscription, and its own arithmetic gives ~52%, not 80%) and the forbidden metered-API route.
- Every route and note carries `evidence` + `updated`, and `~/.config/emux/agents.json` overrides the defaults. It is deliberately rudimentary — correct it as evidence arrives.

## v0.22.0 - 2026-07-12

**Show the answer, hide the machinery.** The cascade was correct but unreadable — four numbered steps, lane toggles, filters, 29 sessions and 200 directories all on screen at once. The tree is the mechanism; it is not what you should have to read.

- **Empty state is one question**: "say what you want to do…" and a go button. Nothing else.
- **The result is one card**: the verb (↺ RESUME / + NEW SESSION), the name in large type, one line of "on <machine> · <path>", any warning flags, a collapsed "what's in it" preview, and the one-line reason. Then a single button.
- **The tree lives behind `change…`** — all 11 machines, every running session, every directory, still one click away for overriding any node. Or "set it up by hand" from the empty state to skip the model entirely.

## v0.21.0 - 2026-07-12

**Resume is not Create.** The tree now branches on what actually exists on a machine: sessions already RUNNING there (resume) or directories to start fresh in (new). The LLM classifies your sentence into a path through that tree and pre-fills it; every node stays an overridable choice.

- **Resume a running session from plain English.** "resume one of the more recent tmuxes on rentamac" -> machine `rentamac`, then `ggo-build` picked by index from the 29 sessions actually running there. "pick up the greenmark reconcile work I had going on rentamac" -> `cerebro-claude`. `POST /api/adopt` registers it (with its `host`) so a remote session becomes a first-class emux citizen.
- **Look inside before you touch it.** Resuming means grabbing something that already has state, so selecting a session shows a live preview of its pane (`GET /api/peek`) and flags when it **holds an unsent prompt** or is **already attached elsewhere**.
- **The two paths diverge where they genuinely differ.** Resume hides the command step (it's already running) and renames step 4 to "adopt into emux as" (it already has a name); New keeps command + naming.
- **SECURITY/SAFETY FIX: attaching no longer steals keyboard focus.** The AppleScript `activate` meant an auto-opened attach window grabbed OS focus and swallowed whatever you were typing straight into a live agent's prompt — observed for real against a 2-day-old Claude session running with bypass-permissions, where stray keys landed in its input box. Attach windows now open WITHOUT focus; you click into them when you mean to type there.

## v0.20.0 - 2026-07-12

**The new-session UX is the cascade now, not a form with an AI bolted on.** v0.19 fixed the data dependency but still presented four independent-looking fields; the dependency was invisible.

- **Say it in plain English; it classifies down a series of real choices.** One sentence — "check on the eidos-mail sync running on the hostkey server" — resolves to ① machine, ② directory, ③ what runs there, ④ name. Each step shows the model's pick marked ✦ **alongside the real alternatives**, so every level is a choice you can override rather than a field that got filled in.
- **Steps are visibly dependent.** ②③④ render locked/dimmed until the step above resolves ("pick a machine first"), and the directory step lists the directories that actually exist on the chosen machine (9 on `eidos-bm`, 200 on `local`).
- **Changing an upper choice invalidates everything below it.** Switch the machine and the directory, command, and name clear, the directory options re-derive from the new machine, and CREATE disables until the cascade is complete again — so a path from the previous machine can never survive.

## v0.19.0 - 2026-07-12

**The new-session choices are a cascade, not a flat form.** Which directories exist depends on which machine you picked — so the machine is resolved FIRST and everything below it is derived from that real machine.

- **Per-machine directories.** `GET /api/dirs?host=…` lists the repo trees that actually exist on that box (local: glob; remote: one ssh round-trip, cached 2m). Changing the machine in the UI re-derives the directory options and clears any path from the previous machine. Previously the directory autocomplete was always the LOCAL tree, so choosing a remote host offered `/Users/...` paths that cannot exist there.
- **AI placement is now classification down the cascade, not generation.** `✦ suggest` asks `claude -p` (fixed-cost CLI, never the API) to (1) choose the MACHINE from the real host list, then (2) choose the DIRECTORY **by index** from the directories that actually exist on THAT machine. The path is picked from real options rather than invented, and the reply carries `verified: true` to say so. Tick "pin this machine" to fix step 1 yourself and let it only choose the directory.

## v0.18.0 - 2026-07-12

- **`+ NEW SESSION` button — start a session from the control room.** Pick the machine (local, or any `~/.ssh/config` alias / host already in the registry), the directory, and the command; optionally have an iTerm2 window open attached to it. New `POST /api/spawn` (wraps `tmux_spawn`, so remote works) and `GET /api/hosts`.
- **AI-suggested placement.** Type what you actually want to do — "reconcile the Greenmark metrics workbook against the warehouse" — and `✦ suggest` asks `claude -p` (fixed-cost CLI, never the API) to pick the machine, the repo/cockpit directory, the command, and a session name, with a one-line rationale. It reads the real repo trees as candidates, so it lands in the right project rather than guessing.
- **Fix: company classifier missed company repos in the generic `~/repos/` tree.** It only matched the `repos-<company>/` roots, so `repos/greenmark-claude-toolkit` read as no-company. Roots are still authoritative; a company keyword anywhere in the path is now the fallback.

## v0.17.0 - 2026-07-12

- **"⧉ iTerm2" button in the session modal.** Open a session, click it, and a new iTerm2 window opens attached to that session's tmux — so you can watch/drive it in a real terminal and close the window when done (the session keeps running; closing just detaches). Driven by AppleScript `write text` (not a `.command` file), so macOS Gatekeeper doesn't throw a quarantine prompt. New `POST /api/head` endpoint.

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
