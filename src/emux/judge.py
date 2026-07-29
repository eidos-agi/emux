"""emux judge — a deterministic (Tier-0) session state classifier.

This is the bottom layer of the "smart classifiers" design
(`docs/emux-smart-classifiers.md`): turn terminal behaviour into a single
labelled state using ONLY rules + counters. No model calls, ever — this module
never imports or calls an LLM. The point of Tier-0 is to be cheap, precise, and
debuggable: it detects FACTS (prompt visible, traceback present, same command
re-run, session gone), and leaves JUDGEMENT-under-ambiguity to higher tiers a
cheap-model summariser could add on top later.

Two public entry points:

- ``classify(capture_text, activity, meta)`` — the PURE core. Same inputs →
  same output, no I/O. Signal-first: if ``meta["signals"]`` carries emux
  up-channel signals (DONE / ERROR / NEED / …) they are trusted before any
  screen scraping. This is what tests exercise with synthetic windows, and what
  the web daemon calls with its own capture + activity samples.

- ``extract_features(name, host=None)`` + ``classify_session(name, host=None)``
  — the LIVE path. Stateless: captures the live pane (falling back to the
  durable stream log when the pane can't be read — the pane is CURRENT state,
  the log keeps long-cleared gates in its tail) and reads pending up-channel
  signals (``_new_signals``, WITHOUT acking) plus the live pane command,
  assembles the (capture, activity, meta) triple, and runs ``classify``. The
  ``tmux_classify`` MCP tool wraps ``classify_session``.

States (taxonomy from the design doc):
    running          — output actively changing; the agent is working.
    planning         — an AI agent is laying out a plan / next steps.
    editing          — editor in the foreground, or a patch/diff being written.
    waiting_external — a long-running remote/build/network command is in flight.
    waiting_human    — an approval / login / confirmation prompt is on screen.
    thrashing        — busy but going in circles: repeated command / near-identical
                       windows with no net progress.
    stuck            — no meaningful change for a long time, and not at a prompt.
    error            — traceback, build failure, failing tests, or merge conflict.
    done_idle        — shell prompt returned and the screen has gone quiet.
    dead             — the tmux session is gone.

Flags (orthogonal warnings, any subset may fire):
    token_waste         — AI chatter / thrash with no artifact or command progress.
    possible_exhaustion — explicit rate-limit / context / quota text on screen.
    hidden_wait         — a human gate is up but nobody is attached to answer it.
    false_busy          — the only motion is a spinner; nothing meaningful moved.
    dangerous_blocked   — a DESTRUCTIVE action is sitting behind a confirm prompt.
    login_gate          — a login/auth sequence is on screen (logged-out banner,
                          /login method picker, OAuth URL + paste-code prompt).
                          Drive it with `emux login <name>` / the tmux_login tool.
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any

from . import server as _server  # reference helpers via the module so tests can patch them

# ---------------------------------------------------------------------------
# tunables — thresholds for the counters below, named so behaviour is inspectable.
# ---------------------------------------------------------------------------
_ACTIVE_AGE = 8.0        # < this many seconds since last change ⇒ "actively changing"
_STUCK_AGE = 45.0        # > this many seconds with no change and no prompt ⇒ stuck
_TAIL_LINES = 40         # only the last N lines matter for "what's on screen now"
_SIM_WINDOW = 6          # how many recent frames to compare for repetition
_SIM_RATIO = 0.92        # difflib ratio above which two frames are "near-identical"
_LOG_TAIL = 240          # lines of stream log to read for the live path

# The AI coding agents emux knows how to detect (matches web.py's _AGENT_TABLE keys).
_AI_AGENTS = {"claude", "codex", "gemini", "hermes", "aider"}
_EDITORS = {"vim", "nvim", "vi", "nano", "emacs", "hx", "helix"}
_SHELLS = {"zsh", "-zsh", "bash", "-bash", "fish", "sh", "-sh"}

# Spinner / progress glyphs that animate without representing real work. Kept in
# sync with web.py's _SPINNER_RE.
_SPINNER_RE = re.compile(r"[⠀-⣿▀-▟●○◐◑◒◓◜◝◞◟◢◣◤◥⠋⠹⠙⠸⠦⠇⠏]")

# --- terminal-semantic detectors (high-precision regexes over ANSI-stripped text) ---

# A shell prompt sitting at the end of the screen (matched against the LAST line).
_PROMPT_RE = re.compile(r"(?:^|\s)[\$%#❯➜»]\s*$")
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|^\s*File \".+\", line \d+"
    r"|^\s+at .+\(.+:\d+:\d+\)"          # node stack frame
    r"|Exception in thread"
    r"|^panic:"
    r"|Segmentation fault",
    re.M,
)
_BUILD_FAIL_RE = re.compile(
    r"Build failed"
    r"|Compilation (?:failed|terminated)"
    r"|^error\[E\d+\]"                   # rustc
    r"|npm ERR!"
    r"|make(?:\[\d+\])?: \*\*\*"
    r"|^error: ",
    re.M | re.I,
)
_TEST_FAILED_RE = re.compile(r"(\d+)\s+(?:failed|failing)", re.I)
# An agent that died at startup and left its fatal banner as the final line
# (e.g. `claude --continue` with no conversation to resume). Matched against
# the LAST non-blank line only, so a chat merely quoting the phrase higher up
# never trips it.
_AGENT_FATAL_RE = re.compile(r"No conversation found to continue")
_SUCCESS_RE = re.compile(
    r"All tests passed"
    r"|Build succeeded"
    r"|Completed successfully"
    r"|passed,? 0 failed"
    r"|\b0 failed\b"
    r"|✓ ",
    re.I,
)
_APPROVAL_RE = re.compile(
    r"Press Enter to continue"
    r"|Approve in browser"
    r"|Enter (?:the )?verification code"
    r"|Do you want to proceed"
    r"|\[y/N\]|\(y/n\)|\[Y/n\]"
    r"|Are you sure"
    r"|waiting for your approval"
    r"|please confirm"
    r"|provide credentials"
    r"|Enter your (?:API key|password|token|passphrase)"
    r"|Sign in to"
    r"|❯\s*1\.\s*Yes"                    # Claude Code confirmation menu
    r"|1\.\s*Yes\b.*2\.\s*No",
    re.I | re.S,
)
# A Claude Code login/auth sequence needing action. Deliberately EXCLUDES
# "Login successful" — that means the gate is over, not up. Kept in sync with
# the login driver in server.py (login_flow / _login_step).
_LOGIN_GATE_RE = re.compile(
    r"Select login method"
    r"|Paste code (?:here|if prompted)"
    r"|Press Enter to (?:log ?in|open)"
    r"|claude\.ai/oauth"
    r"|console\.anthropic\.com/oauth"
    r"|(?:Please )?run /login"
    r"|Invalid API key"
    r"|OAuth (?:token|error)"
    r"|(?:You (?:are|have been)|Successfully) logged out"
    r"|Login (?:failed|interrupted|expired)",
    re.I,
)
_DESTRUCTIVE_RE = re.compile(
    r"rm\s+-rf"
    r"|DROP\s+(?:TABLE|DATABASE)"
    r"|git push\s+--force|force[- ]push"
    r"|This will (?:delete|remove|overwrite|destroy)"
    r"|permanently delete"
    r"|overwrite",
    re.I,
)
_GIT_CONFLICT_RE = re.compile(
    r"^<{7} "
    r"|^={7}$"
    r"|^>{7} "
    r"|^CONFLICT \("
    r"|Automatic merge failed",
    re.M,
)
_QUOTA_RE = re.compile(
    r"rate limit"
    r"|session limit"
    r"|hit your (?:session|usage|token) limit"
    r"|/usage-credits"
    r"|context (?:length|window) exceeded"
    r"|token limit"
    r"|usage limit"
    r"|quota (?:exceeded|exhausted)"
    r"|too many requests"
    r"|\b429\b"
    r"|approaching (?:usage|context) limit",
    re.I,
)
_EXTERNAL_RE = re.compile(
    r"\bssh\b|\bscp\b|\brsync\b|\bcurl\b|\bwget\b"
    r"|docker (?:build|pull|push|run)"
    r"|\bkubectl\b|\bterraform\b"
    r"|git (?:clone|fetch|pull|push)"
    r"|npm (?:install|ci)\b|pip install|uv (?:sync|pip)|cargo (?:build|install)"
    r"|apt(?:-get)? install"
    r"|Cloning into|Receiving objects|Building wheel|Downloading ",
    re.I,
)
_PLAN_RE = re.compile(
    r"^\s*#*\s*Plan\b"
    r"|Next steps"
    r"|Here(?:'s| is) (?:my|the) plan"
    r"|^\s*1\.\s.+\n\s*2\.\s",
    re.I | re.M,
)
_EDITING_RE = re.compile(
    r"^@@ .+ @@"
    r"|^\+{3} |^-{3} "
    r"|str_replace|Applying (?:edit|patch)",
    # NB: no "Editing X" / "Writing X" prose alt — that's agent NARRATION (often
    # printed while it's actively generating), not a real editor/diff. Matching it
    # mislabels a working agent as "editing".
    re.M,
)
_GENERATING_RE = re.compile(r"esc to interrupt|\b\d+s\s*·|·\s*\d+s\b", re.I)
_CMD_AT_PROMPT_RE = re.compile(r"[\$%#❯➜»]\s+([A-Za-z][\w./-]*(?:\s+\S+)*)")

# What to DO about each state — a conservative operator recommendation.
_ACTION = {
    "running": "Leave alone — actively working.",
    "planning": "Leave alone — let it finish planning.",
    "editing": "Leave alone — mid-edit.",
    "waiting_external": "Leave alone — waiting on an external process; check back.",
    "waiting_human": "Respond — a human gate is up (approval / login / confirm).",
    "thrashing": "Interrupt — ask for root-cause analysis before another attempt.",
    "stuck": "Nudge — no progress for a while; prompt or interrupt.",
    "error": "Inspect — an error is on screen; it likely needs a fix.",
    "done_idle": "Reap or reassign — the session is idle at a prompt.",
    "dead": "Clean up — the tmux session is gone.",
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _agent_key(meta: dict[str, Any]) -> str:
    """Normalise the detected agent to a bare key string ('claude', 'shell', …).
    Accepts meta['agent'] as a string OR web.py's {'agent': ...} dict."""
    ag = meta.get("agent")
    if isinstance(ag, dict):
        return str(ag.get("agent") or "").lower()
    return str(ag or "").lower()


def _agent_from_cmd(cmd: str) -> str:
    """Best-effort agent key from a pane command, for the live path."""
    c = (cmd or "").lower()
    for key in _AI_AGENTS:
        if key in c:
            return key
    if c in _SHELLS:
        return "shell"
    if c in _EDITORS:
        return "editor"
    return c


def _repeated_command(tail: str) -> str | None:
    """The command that appears after a shell prompt two or more times — the
    classic pytest / npm-test rerun loop. Returns the repeated command or None."""
    seen: dict[str, int] = {}
    for m in _CMD_AT_PROMPT_RE.finditer(tail):
        cmd = m.group(1).strip()
        head = cmd.split()[0] if cmd else ""
        if head:
            seen[head] = seen.get(head, 0) + 1
    for cmd, n in seen.items():
        if n >= 2:
            return cmd
    return None


def _high_similarity(norms: list[str]) -> bool:
    """True if the recent normalized frames are near-identical to each other — a
    window that keeps redrawing roughly the same screen (thrash or stall)."""
    frames = [n for n in norms[-_SIM_WINDOW:] if n]
    if len(frames) < 3:
        return False
    ratios = [
        difflib.SequenceMatcher(None, frames[i], frames[i + 1]).ratio()
        for i in range(len(frames) - 1)
    ]
    near = sum(1 for r in ratios if r >= _SIM_RATIO)
    return near >= max(2, len(ratios) - 1)


def _confidence(n_signals: int) -> float:
    """Confidence rises with the number of corroborating signals that fired. One
    weak signal ⇒ ~0.4; three or more ⇒ capped near-certain (but never 1.0,
    reserved for the only truly factual state, `dead`)."""
    return round(min(0.9, 0.4 + 0.17 * max(0, n_signals)), 2)


def _from_signal(kind: str, payload: str, flags: list[str]) -> dict[str, Any] | None:
    """Map an emux up-channel signal to a state, or None to keep scraping.
    Hard signals (a worker explicitly reporting) are trusted over the screen;
    PROGRESS is soft (still working) so we fall through to describe HOW."""
    k = (kind or "").upper()
    tail = f" — {payload}" if payload else ""
    if k == "ERROR":
        return _result("error", 0.85, f"Worker reported an error{tail}.",
                       "up-channel ERROR signal", flags)
    if k == "NEED":
        return _result("waiting_human", 0.85, f"Worker needs input{tail}.",
                       "up-channel NEED signal", flags)
    if k == "DONE":
        return _result("done_idle", 0.85, f"Worker reported done{tail}.",
                       "up-channel DONE signal", flags)
    if k in ("IDLE", "READY"):
        return _result("done_idle", 0.7, "Worker is idle / holding for the next task.",
                       f"up-channel {k} signal", flags)
    return None  # PROGRESS / unknown → let the screen decide


# ---------------------------------------------------------------------------
# the pure classifier
# ---------------------------------------------------------------------------

def classify(capture_text: str, activity: list[dict], meta: dict) -> dict:
    """Classify a single session's state from its pane capture, recent activity
    samples, and metadata. Pure and deterministic — no I/O, no model calls.

    Args:
        capture_text: the session's most recent pane capture (raw, ANSI ok).
        activity: recent samples oldest→newest. Each is a dict; recognised keys
            (all optional): ``norm`` (spinner/whitespace-normalized frame text),
            ``changed`` (bool: did this frame meaningfully differ from the prior).
        meta: recognised keys (all optional): ``live`` (bool, default True),
            ``last_change_age`` (seconds since last meaningful change | None),
            ``agent`` (key string or web.py's {'agent': ...} dict),
            ``pane_command`` (foreground process name), ``attached`` (bool),
            ``signals`` (list of {'kind','payload'} emux up-channel signals).

    Returns:
        ``{"state", "confidence", "summary", "evidence", "flags",
           "recommended_action"}``.
    """
    meta = meta or {}
    activity = activity or []

    # ---- session liveness is a hard fact, decided before anything else ----
    if meta.get("live", True) is False:
        return _result("dead", 1.0, "tmux session is gone.", "session no longer live", [])

    text = _server._strip_ansi(capture_text or "")
    lines = text.splitlines()
    tail = "\n".join(lines[-_TAIL_LINES:])
    last_nonblank = next((ln for ln in reversed(lines) if ln.strip()), "")

    agent = _agent_key(meta)
    pane_cmd = str(meta.get("pane_command") or "").lower()
    attached = bool(meta.get("attached", False))
    last_change_age = meta.get("last_change_age")
    is_ai = agent in _AI_AGENTS

    # ---- activity counters ----
    changed_flags = [bool(s.get("changed")) for s in activity]
    norms: list[str] = [v for s in activity if isinstance(v := s.get("norm"), str)]
    n = len(activity)
    changed_count = sum(changed_flags)
    diff_ratio = (changed_count / n) if n else 0.0
    recent_changed = any(changed_flags[-3:])
    active = recent_changed or (
        isinstance(last_change_age, (int, float)) and last_change_age < _ACTIVE_AGE
    )
    idle_long = isinstance(last_change_age, (int, float)) and last_change_age >= _STUCK_AGE

    # ---- terminal-semantic detectors (facts on the current screen) ----
    has_prompt = bool(_PROMPT_RE.search(last_nonblank))
    has_traceback = bool(_TRACEBACK_RE.search(tail))
    has_build_fail = bool(_BUILD_FAIL_RE.search(tail))
    fm = _TEST_FAILED_RE.search(tail)
    test_failed = int(fm.group(1)) if fm else 0
    has_success = bool(_SUCCESS_RE.search(tail))
    has_approval = bool(_APPROVAL_RE.search(tail))
    has_login_gate = bool(_LOGIN_GATE_RE.search(tail))
    has_destructive = bool(_DESTRUCTIVE_RE.search(tail))
    has_conflict = bool(_GIT_CONFLICT_RE.search(text))
    has_quota = bool(_QUOTA_RE.search(tail))
    has_external = bool(_EXTERNAL_RE.search(tail)) or bool(_EXTERNAL_RE.search(pane_cmd))
    has_plan = bool(_PLAN_RE.search(tail))
    has_editing = bool(_EDITING_RE.search(tail)) or pane_cmd in _EDITORS
    is_generating = bool(_GENERATING_RE.search(tail))
    has_spinner = bool(_SPINNER_RE.search(capture_text or ""))

    repeated_cmd = _repeated_command(tail)
    repeats = _high_similarity(norms) or repeated_cmd is not None

    # ---- orthogonal flags (independent of the chosen state) ----
    flags: list[str] = []
    if has_quota:
        flags.append("possible_exhaustion")
    if has_spinner and diff_ratio == 0.0 and not recent_changed:
        flags.append("false_busy")

    # ---- signal-first: trust an explicit up-channel report over the screen ----
    for sig in reversed(meta.get("signals") or []):
        decided = _from_signal(sig.get("kind", ""), sig.get("payload", ""), flags)
        if decided is not None:
            return decided

    # ================= state decision cascade (first match wins) =================

    # 1. waiting_human — a visible human gate dominates everything below it.
    #    A login gate gets its own flag + summary so operators see "needs login"
    #    (actionable via `emux login`), not a generic approval prompt.
    if has_approval or has_login_gate:
        sig = []
        if has_login_gate:
            flags.append("login_gate")
            sig.append("login/auth sequence on screen")
        if has_approval:
            sig.append("approval/login prompt on screen")
        if has_destructive:
            flags.append("dangerous_blocked")
            sig.append("destructive action pending")
        if not recent_changed and not attached:
            flags.append("hidden_wait")
            sig.append("no human attached, no input progress")
        summary = ("Needs login — a login/auth sequence is on screen; "
                   "drive it with `emux login <name>`."
                   if has_login_gate else
                   "Waiting on a human — approval/login/confirmation prompt is up.")
        return _result("waiting_human", _confidence(len(sig)), summary,
                       "; ".join(sig), flags)

    # A hard provider quota is an external wait, not a stuck agent. Check this
    # before the activity/thrashing/error cascade: the quota banner can coexist
    # with stale spinner or command text from work that already finished.
    if has_quota:
        return _result("waiting_external", _confidence(2),
                       "Waiting for provider quota or context capacity.",
                       "explicit quota/context exhaustion text on screen", flags)

    # A fatal agent-startup banner as the LAST line (e.g. `claude --continue`
    # with no conversation): a process may still sit there but the seat is
    # dead — reseed, don't inspect. Checked early because the screen is
    # otherwise quiet and would misread as done_idle/stuck. (EID-1172)
    if _AGENT_FATAL_RE.search(last_nonblank):
        flags.append("needs_reseed")
        return _result("error", 0.9,
                       "Agent dead at startup — fatal banner is the last line; "
                       "reseed the seat (fresh start, not --continue).",
                       f"fatal startup banner: {last_nonblank.strip()[:80]!r}", flags)

    # 2. thrashing — busy, but cycling with no net progress. Checked BEFORE error:
    #    a command re-run producing the SAME failure over and over is thrash (more
    #    actionable), not a one-off error. A single, non-repeating failure falls
    #    through to `error` below.
    if repeats and (active or diff_ratio > 0.3):
        sig = []
        if repeated_cmd:
            sig.append(f"'{repeated_cmd}' re-run repeatedly")
        if _high_similarity(norms):
            sig.append("near-identical windows, no net change")
        if is_ai:
            flags.append("token_waste")
            sig.append("AI output with no artifact progress")
        if has_traceback or has_build_fail or test_failed > 0:
            sig.append("same failure recurring")
        return _result("thrashing", _confidence(len(sig) + 1),
                       "Thrashing — repeating the same action with no net progress.",
                       "; ".join(sig) or "repeated low-novelty windows", flags)

    # 3. error — a NON-repeating traceback, build failure, failing tests, or conflict.
    if has_traceback or has_build_fail or test_failed > 0 or has_conflict:
        sig = []
        if has_traceback:
            sig.append("traceback on screen")
        if has_build_fail:
            sig.append("build/compile failure")
        if test_failed > 0:
            sig.append(f"{test_failed} test(s) failing")
        if has_conflict:
            sig.append("git conflict markers")
        summary = "Error visible — " + (
            f"{test_failed} test(s) failing." if test_failed > 0
            else "traceback / build failure on screen." if (has_traceback or has_build_fail)
            else "unresolved merge conflict."
        )
        return _result("error", _confidence(len(sig)), summary, "; ".join(sig), flags)

    # 4. waiting_external — a long remote/build/network command is in flight.
    if has_external and not has_prompt:
        return _result("waiting_external", _confidence(2 if active else 1),
                       "Waiting on an external process (build / network / remote command).",
                       "long-running external command running, no prompt", flags)

    # 5. done_idle — shell prompt returned and the screen has gone quiet.
    if has_prompt and not active and pane_cmd in _SHELLS | {""}:
        sig = ["shell prompt returned", "output stable"]
        if has_success:
            sig.append("success marker present")
        return _result("done_idle", _confidence(len(sig)),
                       "Idle at a shell prompt" + (" after success." if has_success else "."),
                       "; ".join(sig), flags)

    # 6. editing — an editor is open, or a patch/diff is being written.
    if has_editing:
        return _result("editing", _confidence(2 if pane_cmd in _EDITORS else 1),
                       "Editing — editor open or a patch/diff being written.",
                       "editor foreground or diff/patch on screen", flags)

    # 7. planning — an AI agent laying out a plan while actively producing text.
    if is_ai and has_plan and (active or is_generating):
        return _result("planning", _confidence(2),
                       "Planning — an agent is laying out its plan / next steps.",
                       "plan/next-steps section on screen, output flowing", flags)

    # 8. stuck — no meaningful change for a long time and not at a prompt.
    if idle_long and not has_prompt and not active:
        if has_spinner and "false_busy" not in flags:
            flags.append("false_busy")
        return _result("stuck", _confidence(2),
                       "Stuck — no meaningful change for a while and not at a prompt.",
                       f"no meaningful change for ~{int(last_change_age or 0)}s, no prompt", flags)

    # 9. running — actively changing / generating, none of the above.
    if active or is_generating:
        who = agent if is_ai else (pane_cmd or "process")
        return _result("running", _confidence(1 + (1 if is_generating else 0)),
                       f"Running — output actively changing ({who}).",
                       "recent meaningful change"
                       + (", generation in progress" if is_generating else ""), flags)

    # 10. fallback — quiet but at a prompt ⇒ idle; otherwise low-confidence running.
    if has_prompt:
        return _result("done_idle", 0.4, "Idle at a shell prompt.",
                       "prompt visible, no activity", flags)
    return _result("running", 0.3, "Running (no strong signal either way).",
                   "no decisive signal", flags)


def _result(state: str, confidence: float, summary: str, evidence: str,
            flags: list[str]) -> dict:
    """Assemble the return dict, de-duplicating flags while preserving order and
    attaching the state's recommended operator action."""
    seen: set[str] = set()
    ordered = [f for f in flags if not (f in seen or seen.add(f))]
    return {
        "state": state,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "flags": ordered,
        "recommended_action": _ACTION.get(state, "Observe."),
    }


# ---------------------------------------------------------------------------
# the live path — read real session state, then classify
# ---------------------------------------------------------------------------

def _pane_command(session: str, host: str | None) -> str:
    """Live foreground process name in a session's active pane ('' on failure)."""
    try:
        code, out, _ = _server._run_tmux(
            ["display-message", "-p", "-t", session, "#{pane_current_command}"], host=host
        )
    except Exception:
        return ""
    return out.strip() if code == 0 else ""


def extract_features(name: str, host: str | None = None) -> dict[str, Any]:
    """Assemble the (capture_text, activity, meta) triple for a session, WITHOUT
    running any model. Signal-first and stateless: it reads the durable stream
    log and the pending up-channel signals (never acking them), and probes the
    live pane for its command + liveness.

    `name` is a registry name when one exists, otherwise a raw tmux session id.
    Returns ``{"name", "capture_text", "activity", "meta"}``; ``meta`` is exactly
    what ``classify`` consumes.
    """
    registry = _server._load_registry()
    entry = registry.get(name)
    if entry is not None:
        session = entry.get("session") or name
        host = host or entry.get("host")
    else:
        session = name

    # Signal-first: read (do NOT ack) any up-channel signals the worker emitted.
    try:
        signals = _server._new_signals(name, ack=False)
    except Exception:
        signals = []

    # Liveness: a real has-session check when tmux is available; if tmux isn't
    # installed we can't prove death, so assume live and classify from the log.
    live = True
    if _server._resolve_tmux() is not None or host is not None:
        try:
            live = _server._session_exists(session, host=host)
        except Exception:
            live = True

    # Capture: prefer the LIVE pane — the log tail keeps text that has already
    # scrolled into history (a gate that cleared minutes ago still classified
    # as waiting_human, observed live). The durable log is the fallback when
    # the pane can't be captured, and stays the source for last_change_age.
    capture_text = ""
    if live:
        try:
            _s, _h, rerr = _server._resolve_target(name, by_registry_name=entry is not None)
            if rerr is None and _s is not None:
                code, out, _ = _server._run_tmux(
                    ["capture-pane", "-t", _s, "-p", "-S", f"-{_LOG_TAIL}"], host=_h
                )
                if code == 0:
                    capture_text = out or ""
        except Exception:
            pass
    if not capture_text.strip():
        capture_text = _server._read_log(name, lines=_LOG_TAIL)

    pane_cmd = _pane_command(session, host) if live else ""

    # last_change_age: time since the stream log last grew — a stateless proxy
    # for "time since last meaningful change" that needs no daemon history.
    last_change_age = None
    p = _server._log_path(name)
    if p.is_file():
        try:
            last_change_age = max(0.0, time.time() - p.stat().st_mtime)
        except Exception:
            last_change_age = None

    meta = {
        "live": live,
        "last_change_age": last_change_age,
        "agent": _agent_from_cmd(pane_cmd) or _server._classify(pane_cmd),
        "pane_command": pane_cmd,
        "attached": False,
        "signals": [{"kind": s.get("kind", ""), "payload": s.get("payload", "")}
                    for s in signals],
    }
    # The live path has no per-frame history (that lives in the web daemon); the
    # text-based repeated-command detector still catches thrash from the log.
    return {"name": name, "capture_text": capture_text, "activity": [], "meta": meta}


def classify_session(name: str, host: str | None = None) -> dict[str, Any]:
    """Classify a live session by name: ``classify(**extract_features(...))``.
    This is what the ``tmux_classify`` MCP tool wraps."""
    feats = extract_features(name, host=host)
    result = classify(feats["capture_text"], feats["activity"], feats["meta"])
    result["name"] = name
    return result
