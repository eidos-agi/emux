"""emux MCP server.

Exposes MCP tools for attaching to and driving existing tmux sessions: list
live sessions, send keys, capture panes, run commands. Maintains a registry
of named sessions with metadata so an agent can refer to "claude-prod" or
"test-shell" without remembering tmux's underlying session ids.

Design principles:
- Operates on EXISTING tmux sessions only. Never spawns new ones, never kills
  them. The user owns the session lifecycle; this MCP just observes and drives.
- The registry is metadata only. Live state always comes from `tmux list-sessions`.
  If a registered session no longer exists, the registry entry is marked stale
  but not deleted — the user decides whether to re-register or unregister.
- All operations are best-effort capture. tmux output may include ANSI escapes;
  the caller is responsible for parsing if they need clean text.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("emux")


REGISTRY_PATH = Path(
    os.environ.get("EMUX_REGISTRY")
    or os.environ.get("TMUX_MCP_REGISTRY")  # back-compat with prior name
    or (Path.home() / ".config" / "emux" / "registry.json")
)


def _resolve_tmux() -> str | None:
    """Return path to the `tmux` binary, or None if not on PATH."""
    return shutil.which("tmux")


def _run_tmux(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Run `tmux <args>` and return (returncode, stdout, stderr).

    Raises FileNotFoundError if tmux is not installed.
    """
    tmux = _resolve_tmux()
    if tmux is None:
        raise FileNotFoundError("tmux not found on PATH")
    proc = subprocess.run(
        [tmux] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _live_sessions() -> list[dict[str, Any]]:
    """Return a list of currently-running tmux sessions with metadata."""
    code, out, err = _run_tmux([
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_windows}\t#{session_created}\t#{session_attached}",
    ])
    if code != 0:
        # tmux returns nonzero with "no server running" when no sessions exist
        if "no server running" in (err or "").lower() or "no server running" in (out or "").lower():
            return []
        return []
    sessions = []
    for line in (out or "").strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        sessions.append({
            "name": parts[0],
            "windows": int(parts[1]) if parts[1].isdigit() else parts[1],
            "created_unix": int(parts[2]) if parts[2].isdigit() else parts[2],
            "attached": parts[3] != "0",
        })
    return sessions


def _load_registry() -> dict[str, dict[str, Any]]:
    """Load the named-session registry from disk. Returns empty dict if missing."""
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry(registry: dict[str, dict[str, Any]]) -> None:
    """Atomically write the registry to disk."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    tmp.replace(REGISTRY_PATH)


@mcp.tool()
async def tmux_sessions() -> dict[str, Any]:
    """List all currently-running tmux sessions on the host.

    Use this to discover what tmux sessions exist before attaching. Returns
    sessions whether or not they're in the named-session registry; cross-
    reference with `tmux_registered()` to see which have metadata.

    Returns:
        A dict with `live` (list of session dicts: name, windows, created_unix,
        attached) and `registry` (the named-session registry from disk).
        Each registered session is also marked `stale: true` if its tmux
        session no longer exists.
    """
    if _resolve_tmux() is None:
        return {
            "ok": False,
            "error": "tmux_not_installed",
            "hint": "Install tmux: `brew install tmux` (macOS) or `apt install tmux` (Debian).",
        }
    live = _live_sessions()
    registry = _load_registry()
    live_names = {s["name"] for s in live}
    annotated = {}
    for name, entry in registry.items():
        annotated[name] = {**entry, "stale": entry.get("session") not in live_names}
    return {"ok": True, "live": live, "registry": annotated}


@mcp.tool()
async def tmux_register(
    name: str,
    session: str,
    description: str | None = None,
    tags: list[str] | None = None,
    manages: list[str] | None = None,
) -> dict[str, Any]:
    """Register a tmux session under a friendly name with metadata.

    Use this to remember "this is the session running my claude prod loop" or
    "this is the test shell" so future calls can refer to it by `name` rather
    than the raw tmux `session` identifier. The registry persists at
    ~/.config/tmux-mcp/registry.json (override with $TMUX_MCP_REGISTRY).

    Args:
        name: The friendly name to register under (e.g., "claude-prod").
        session: The actual tmux session name as shown by `tmux list-sessions`.
        description: Optional human-readable note about what this session is for.
        tags: Optional list of tags for filtering.
        manages: Optional list of other registered names (or tmux session ids)
            that the agent in THIS session manages/drives. Rendered as directed
            edges in the `emux web` flow view.

    Returns:
        The registry entry that was saved, plus whether the underlying tmux
        session is currently live.
    """
    registry = _load_registry()
    entry = {
        "session": session,
        "description": description,
        "tags": tags or [],
        "manages": manages or [],
        "registered_at": int(time.time()),
    }
    registry[name] = entry
    _save_registry(registry)
    live_names = {s["name"] for s in _live_sessions()}
    return {
        "ok": True,
        "name": name,
        "entry": entry,
        "session_live": session in live_names,
    }


@mcp.tool()
async def tmux_unregister(name: str) -> dict[str, Any]:
    """Remove a named session from the registry. Does NOT touch tmux itself."""
    registry = _load_registry()
    if name not in registry:
        return {"ok": False, "error": "not_registered", "name": name}
    removed = registry.pop(name)
    _save_registry(registry)
    return {"ok": True, "name": name, "removed_entry": removed}


@mcp.tool()
async def tmux_send(
    target: str,
    keys: str,
    enter: bool = True,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Send keystrokes to a tmux session.

    Use this to type a command into the session, send a control sequence, or
    inject any input. Does NOT capture the response — pair with `tmux_capture`
    or use `tmux_run` if you need send-then-read.

    Args:
        target: The tmux session to target. By default this is a tmux session
            name as shown by `tmux list-sessions`. If `by_registry_name=True`,
            it's looked up in the registry first.
        keys: The keystrokes to send. Use tmux key syntax: literal text, or
            named keys like "C-c", "Escape", "Enter".
        enter: If True (default), append "Enter" to submit the command.
        by_registry_name: If True, resolve `target` via the registry.

    Returns:
        {ok, target, resolved_session, sent} on success.
    """
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    args = ["send-keys", "-t", session, keys]
    if enter:
        args.append("Enter")
    result = _run_tmux(args)
    if result[0] != 0:
        return {"ok": False, "error": "tmux_send_failed", "stderr": result[2], "session": session}
    return {"ok": True, "target": target, "resolved_session": session, "sent": keys, "enter": enter}


@mcp.tool()
async def tmux_capture(
    target: str,
    lines: int = 200,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Capture the visible content of a tmux session's active pane.

    Use this to read what's currently on screen — both the live state and the
    last N lines of scrollback. Output may contain ANSI escape sequences; the
    caller is responsible for stripping them if they need clean text.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        lines: How many lines of scrollback to include (default 200). Pass a
            larger number to see more history; tmux scrollback retention
            depends on the session's configured `history-limit`.
        by_registry_name: If True, resolve `target` via the registry.

    Returns:
        {ok, target, resolved_session, content, lines_captured}
    """
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    code, out, err = _run_tmux([
        "capture-pane",
        "-t", session,
        "-p",
        "-S", f"-{lines}",
    ])
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed", "stderr": err, "session": session}
    return {
        "ok": True,
        "target": target,
        "resolved_session": session,
        "content": out,
        "lines_captured": len((out or "").splitlines()),
    }


@mcp.tool()
async def tmux_run(
    target: str,
    command: str,
    wait_seconds: float = 2.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Send a command, wait, then capture — the convenience send-then-read.

    Use this for the common "run a command and observe the result" pattern.
    The wait is a simple sleep; for long-running commands, send+capture
    separately and poll capture until you see the prompt return.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        command: The command to type into the session (Enter is auto-appended).
        wait_seconds: How long to sleep before capturing. 2.0s catches most
            interactive responses; bump higher for slow commands. For commands
            taking >10s, prefer separate `tmux_send` + polling `tmux_capture`.
        capture_lines: How many scrollback lines to return after the wait.
        by_registry_name: If True, resolve `target` via the registry.

    Returns:
        {ok, target, command, wait_seconds, content, lines_captured}
    """
    send_result = await tmux_send(target=target, keys=command, enter=True, by_registry_name=by_registry_name)
    if not send_result.get("ok"):
        return {"ok": False, "stage": "send", "send_result": send_result}
    await asyncio.sleep(wait_seconds)
    capture_result = await tmux_capture(target=target, lines=capture_lines, by_registry_name=by_registry_name)
    if not capture_result.get("ok"):
        return {"ok": False, "stage": "capture", "send_result": send_result, "capture_result": capture_result}
    return {
        "ok": True,
        "target": target,
        "resolved_session": send_result.get("resolved_session"),
        "command": command,
        "wait_seconds": wait_seconds,
        "content": capture_result["content"],
        "lines_captured": capture_result["lines_captured"],
    }


# ---- converse: talk to another AI through its TUI --------------------------
#
# tmux_run waits a FIXED number of seconds — useless for a streaming AI whose
# reply takes an unknown amount of time. converse() instead polls the pane and
# waits until it stops changing (quiesces), so it works for railway.new's agent,
# a `claude`/`codex`/`aider` REPL, or any other terminal AI. It returns the
# reply (the new text since the prompt was sent) plus the full settled screen.

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _capture_text(session: str, lines: int) -> str:
    code, out, err = _run_tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
    if code != 0:
        raise RuntimeError(err or "capture-pane failed")
    return out or ""


def _reply_delta(before: str, after: str) -> str:
    """Best-effort: the non-blank lines present in `after` but not `before`."""
    sm = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines())
    added: list[str] = []
    after_lines = after.splitlines()
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(after_lines[j1:j2])
    return "\n".join(line for line in added if line.strip())


def converse(
    target: str,
    prompt: str,
    submit: bool = True,
    settle_seconds: float = 2.5,
    poll_interval: float = 1.0,
    max_seconds: float = 90.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
    strip_ansi: bool = True,
    busy_markers: list[str] | None = None,
) -> dict[str, Any]:
    """Send `prompt` to a session running a TUI AI, wait for it to stop
    responding, and return the reply. Synchronous core shared by the MCP tool
    and `emux ask`. Operates on an EXISTING session (emux never spawns).

    busy_markers: substrings that mean the AI is still working (e.g. "thinking").
    While any appears on screen, the pane is never considered settled — this
    prevents a static "thinking…" indicator from being mistaken for the reply."""
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]

    try:
        before = _capture_text(session, capture_lines)
    except (RuntimeError, FileNotFoundError) as e:
        return {"ok": False, "error": "capture_failed", "stderr": str(e), "session": session}

    # Type the prompt literally (-l so a sentence is never parsed as key names),
    # then Enter as a real key.
    if _run_tmux(["send-keys", "-t", session, "-l", prompt])[0] != 0:
        return {"ok": False, "error": "send_failed", "session": session}
    if submit:
        _run_tmux(["send-keys", "-t", session, "Enter"])

    # Wait until the pane is unchanged for `settle_seconds` (or time out). If the
    # session dies mid-wait, stop and say so rather than reporting a false settle.
    last: str | None = None
    stable_for = 0.0
    elapsed = 0.0
    died = False
    while elapsed < max_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if not _session_alive(session):
            died = True
            break
        try:
            cur = _capture_text(session, capture_lines)
        except (RuntimeError, FileNotFoundError):
            died = not _session_alive(session)
            break
        busy = bool(busy_markers) and any(m in cur for m in busy_markers)
        if cur == last and not busy:
            stable_for += poll_interval
            if stable_for >= settle_seconds:
                break
        else:
            stable_for = 0.0
            last = cur

    if died:
        return {"ok": False, "error": "session_gone", "target": target,
                "resolved_session": session, "prompt": prompt,
                "detail": "tmux session vanished mid-reply"}

    after = last if last is not None else before
    reply = _reply_delta(before, after)
    if strip_ansi:
        reply = _strip_ansi(reply)
        after = _strip_ansi(after)
    return {
        "ok": True,
        "target": target,
        "resolved_session": session,
        "prompt": prompt,
        "settled": elapsed < max_seconds,
        "waited_seconds": round(elapsed, 1),
        "reply": reply.strip(),
        "screen": after,
    }


@mcp.tool()
async def tmux_ask(
    target: str,
    prompt: str,
    submit: bool = True,
    settle_seconds: float = 2.5,
    poll_interval: float = 1.0,
    max_seconds: float = 90.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
    busy_markers: list[str] | None = None,
) -> dict[str, Any]:
    """Talk to another AI running in a tmux session and get its reply.

    The strategy for driving TUI-based AIs (railway.new's agent, a `claude` /
    `codex` / `aider` REPL, etc.): type a prompt, then wait until the pane STOPS
    changing before reading — because an AI's reply streams in over an unknown
    duration, a fixed sleep (like `tmux_run`) either cuts it off or wastes time.

    The session must already be running the AI and be at its input prompt. emux
    never spawns sessions — create it first (`tmux new-session -d -s x '<ai cmd>'`)
    and navigate it to the prompt, then call this.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        prompt: The message to send (typed literally, then Enter unless submit=False).
        submit: Append Enter to send the prompt (default True).
        settle_seconds: Consider the reply done once the pane is unchanged this
            long (default 2.5). Raise for choppy streamers.
        poll_interval: Seconds between screen captures (default 1.0).
        max_seconds: Hard cap on total wait (default 90).
        capture_lines: Scrollback lines to read each poll (default 200).
        by_registry_name: Resolve `target` via the registry.
        busy_markers: Substrings meaning "still working" (e.g. ["thinking"]).
            While one is on screen the pane is never treated as settled — stops
            a static busy indicator from being read back as the reply.

    Returns:
        {ok, reply, screen, settled, waited_seconds, resolved_session}. `reply`
        is the best-effort new text since the prompt; `screen` is the full
        settled pane (ANSI stripped) if you need more context. `settled=False`
        means it hit `max_seconds` — the AI may still be responding.
    """
    return await asyncio.to_thread(
        converse,
        target,
        prompt,
        submit,
        settle_seconds,
        poll_interval,
        max_seconds,
        capture_lines,
        by_registry_name,
        True,  # strip_ansi
        busy_markers,
    )


# ---- navigate: model-driven TUI navigation --------------------------------
#
# converse() assumes you're already at the AI's input prompt. Getting THERE
# through an arbitrary menu (railway.new's Chat→workspace→project walk, a
# settings screen, an installer) is the hard part. navigate() drives it with a
# model: capture the screen, ask the model which keys to press to move toward a
# stated goal, send them, repeat until the model says done (or `until` appears).
#
# The model call goes through the `claude -p` CLI (fixed-cost subscription tool)
# — NEVER the Anthropic API. A fast model is plenty for "which key next".

# Fast model for the per-keystroke decision; escalate to a stronger one only
# when a step stalls (model error or no usable action). Intelligence when it
# matters, cheap the rest of the time.
_NAV_MODEL_DEFAULT = os.environ.get("EMUX_NAV_MODEL", "claude-haiku-4-5-20251001")
_NAV_MODEL_ESCALATE = os.environ.get("EMUX_NAV_MODEL_ESCALATE", "claude-sonnet-5")

# tmux key names the model is allowed to emit (guards against it inventing keys).
_NAV_ALLOWED_KEYS = {
    "Up", "Down", "Left", "Right", "Enter", "Escape", "Tab", "BTab",
    "Space", "BSpace", "Home", "End", "PageUp", "PageDown",
    "C-c", "C-d", "C-u", "C-k", "C-a", "C-e",
}


def _claude_decide(model: str, goal: str, screen: str, history: list[str]) -> dict[str, Any]:
    """Ask `claude -p` for the next navigation step. Returns the parsed JSON
    decision, or an {_error} dict. Fixed-cost CLI — not the API."""
    claude = shutil.which("claude")
    if claude is None:
        return {"_error": "claude CLI not on PATH (needed for model-driven navigate)"}
    hist = "\n".join(f"  - {h}" for h in history[-8:]) or "  (none yet)"
    prompt = (
        "You are navigating a terminal UI by choosing keystrokes. "
        "Given the GOAL and the current SCREEN, decide the next step.\n\n"
        f"GOAL: {goal}\n\n"
        f"ACTIONS ALREADY TAKEN:\n{hist}\n\n"
        f"CURRENT SCREEN:\n<<<\n{screen}\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else:\n"
        '{\"thought\": \"<brief>\", \"done\": <bool>, '
        '\"text\": \"<literal text to type, or empty>\", '
        '\"keys\": [<tmux key names to send after the text>]}\n'
        f"Allowed key names: {sorted(_NAV_ALLOWED_KEYS)}. "
        "Use \"text\" to type into a filter/input box; use \"keys\" for navigation "
        "(e.g. [\"Down\",\"Enter\"]). Set done=true ONLY when the GOAL is already "
        "satisfied by the current screen — then keys/text are ignored. "
        "Prefer the fewest keys. Never guess a key not in the allowed list."
    )
    try:
        proc = subprocess.run(
            [claude, "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"_error": f"claude -p failed: {e}"}
    out = (proc.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.DOTALL)  # tolerate stray prose around the JSON
    if not m:
        return {"_error": "no JSON in model reply", "raw": out[:400]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"_error": "unparseable JSON from model", "raw": out[:400]}


def _decide_step(
    models: list[str], goal: str, screen: str, history: list[str]
) -> dict[str, Any]:
    """Get a usable navigation step, escalating through `models` on a stall.

    A "stall" is a model/parse error OR a valid reply with no action (no text and
    no allowed keys) and not done. Returns the first usable decision with the
    validated `keys`/`text` attached and `model` used, or the last failure."""
    last: dict[str, Any] = {}
    for model in models:
        decision = _claude_decide(model, goal, screen, history)
        if "_error" in decision:
            last = {"stall": decision["_error"], "raw": decision.get("raw"), "model": model}
            continue
        if decision.get("done"):
            return {"done": True, "thought": decision.get("thought"), "model": model}
        text = (decision.get("text") or "").strip()
        keys = [k for k in (decision.get("keys") or []) if k in _NAV_ALLOWED_KEYS]
        if text or keys:
            return {"done": False, "text": text, "keys": keys,
                    "thought": decision.get("thought"), "model": model}
        last = {"stall": "no_action", "thought": decision.get("thought"), "model": model}
    return {"stall": last.get("stall", "unknown"), **last}


def navigate(
    target: str,
    goal: str,
    until: str | None = None,
    max_steps: int = 12,
    step_pause: float = 1.5,
    capture_lines: int = 200,
    model: str | None = None,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Drive a tmux session's TUI toward `goal` using a model to pick keystrokes.

    Loops: capture screen → `claude -p` decides the next keys → send them →
    repeat, until the model reports done, `until` appears on screen, or
    `max_steps` is hit. emux never spawns — the session must already exist.
    """
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    # Escalation chain: fast model, then the stronger one on a stall. A caller-
    # pinned `model` overrides both (no escalation — they asked for that model).
    chain = [model] if model else [_NAV_MODEL_DEFAULT, _NAV_MODEL_ESCALATE]

    history: list[str] = []
    for step in range(max_steps):
        # Observe with recovery from a dead session or a transient blank frame.
        screen = _observe(session, capture_lines)
        if screen is None:
            return {"ok": False, "error": "session_gone",
                    "detail": "tmux session vanished (ssh dropped or killed)", "steps": history}
        if not screen.strip():
            return {"ok": False, "error": "blank_screen",
                    "detail": "session alive but rendered nothing", "steps": history}
        # Hard stop: caller-supplied target string already visible.
        if until and until in screen:
            return {"ok": True, "reached": "until", "steps": history, "screen": screen}

        decision = _decide_step(chain, goal, screen, history)
        if "stall" in decision:
            # Transient stall: re-observe once and retry before giving up.
            time.sleep(1.0)
            rescreen = _observe(session, capture_lines)
            if rescreen is None:
                return {"ok": False, "error": "session_gone", "steps": history}
            if rescreen.strip():
                decision = _decide_step(chain, goal, rescreen, history)
                screen = rescreen
        if "stall" in decision:
            return {"ok": False, "error": "model_stalled", "detail": decision["stall"],
                    "thought": decision.get("thought"), "raw": decision.get("raw"),
                    "steps": history, "screen": screen}
        if decision.get("done"):
            return {"ok": True, "reached": "model_done", "steps": history,
                    "thought": decision.get("thought"), "screen": screen}

        text, keys = decision["text"], decision["keys"]
        if text:
            _run_tmux(["send-keys", "-t", session, "-l", text])
        for k in keys:
            _run_tmux(["send-keys", "-t", session, k])
        esc = "" if decision["model"] == chain[0] else f" [escalated:{decision['model']}]"
        history.append(f"step {step + 1}: {decision.get('thought','')!r} text={text!r} keys={keys}{esc}")
        time.sleep(step_pause)

    final = _observe(session, capture_lines) or ""
    return {"ok": False, "error": "max_steps_reached", "steps": history, "screen": final}


@mcp.tool()
async def tmux_navigate(
    target: str,
    goal: str,
    until: str | None = None,
    max_steps: int = 12,
    step_pause: float = 1.5,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Drive a tmux session's TUI toward a goal, letting a model pick keystrokes.

    Use this to get through an arbitrary menu/wizard to where you actually want
    to be (e.g. the free-text prompt of railway.new's agent, past a project
    picker, through an installer). Each step captures the screen and asks a model
    (`claude -p`, fixed-cost — never the API) which keys to press next.

    Pair with `tmux_ask`: navigate() gets you to the input prompt, tmux_ask()
    holds the conversation. emux never spawns sessions — create it first.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        goal: Plain-English description of where to get to, e.g. "reach the
            free-text chat input for the Railway agent; at any workspace/project
            picker choose the first option".
        until: Optional substring; if it appears on screen, stop and report
            success immediately (a cheap hard-stop alongside the model's own
            done signal).
        max_steps: Max navigation steps before giving up (default 12).
        step_pause: Seconds to wait after each keystroke batch for the UI to
            update (default 1.5).
        by_registry_name: Resolve `target` via the registry.

    Returns:
        {ok, reached: "model_done"|"until", steps: [...], screen} on success;
        {ok: false, error, steps, screen} if it stalls or hits max_steps.
    """
    return await asyncio.to_thread(
        navigate, target, goal, until, max_steps, step_pause, 200, None, by_registry_name
    )


# ---- pursue: goal mode -----------------------------------------------------
#
# navigate() reaches a SCREEN; pursue() reaches a GOAL. It's the full loop:
# observe → decide one action (navigate keys / type a message or field value /
# wait for a streamed reply / declare done) → act → observe again, until the
# model judges the goal met (or impossible) or max_steps is hit. Same escalation
# (Haiku → Sonnet on a stall) and the same keystroke allowlist as navigate.
#
# It's still keystrokes-into-a-TUI — no shell exec, no new powers — just a longer
# leash and a done-judgment about the goal rather than a single screen.

_PURSUE_ACTIONS = ("keys", "type", "wait", "done")


def _wait_stable(
    session: str, capture_lines: int, settle_seconds: float, poll: float, cap: float
) -> str:
    """Poll the pane until it's unchanged for settle_seconds (or cap). Returns
    the settled, ANSI-stripped screen. Used so the model always judges a settled
    frame, and for the explicit 'wait' action after sending a message to an AI."""
    last: str | None = None
    stable = 0.0
    waited = 0.0
    while waited < cap:
        time.sleep(poll)
        waited += poll
        try:
            cur = _strip_ansi(_capture_text(session, capture_lines))
        except (RuntimeError, FileNotFoundError):
            break
        if cur == last:
            stable += poll
            if stable >= settle_seconds:
                break
        else:
            stable = 0.0
            last = cur
    return last if last is not None else ""


def _pursue_decide(
    chain: list[str], goal: str, screen: str, history: list[str]
) -> dict[str, Any]:
    """Ask the model for the next goal-mode action, escalating on a stall.

    Returns a validated action dict {action, ...} or {stall: <reason>}."""
    claude = shutil.which("claude")
    if claude is None:
        return {"stall": "claude CLI not on PATH (needed for goal mode)"}
    hist = "\n".join(f"  - {h}" for h in history[-10:]) or "  (none yet)"
    prompt = (
        "You are pursuing a GOAL by operating a terminal UI. Each turn, look at "
        "the current SCREEN and history, and choose ONE action to make progress.\n\n"
        f"GOAL: {goal}\n\n"
        f"HISTORY (your prior actions + what you observed):\n{hist}\n\n"
        f"CURRENT SCREEN:\n<<<\n{screen}\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else. One of:\n"
        '  {\"thought\":\"..\",\"action\":\"keys\",\"keys\":[<tmux key names>]}\n'
        '  {\"thought\":\"..\",\"action\":\"type\",\"text\":\"..\",\"submit\":true}\n'
        '  {\"thought\":\"..\",\"action\":\"wait\"}\n'
        '  {\"thought\":\"..\",\"action\":\"done\",\"success\":true,\"summary\":\"..\"}\n\n'
        f"Allowed key names: {sorted(_NAV_ALLOWED_KEYS)}.\n"
        "- 'keys' to navigate menus (e.g. [\"Down\",\"Enter\"]).\n"
        "- 'type' to enter a message, answer, or field value; submit=true presses Enter.\n"
        "- 'wait' if a reply is still streaming / the screen is mid-update.\n"
        "- 'done' when the GOAL is achieved (success=true) or clearly impossible "
        "(success=false); summary states the outcome / answer.\n"
        "Prefer the fewest actions. Never invent a key outside the allowed list."
    )
    last: dict[str, Any] = {}
    for model in chain:
        try:
            proc = subprocess.run([claude, "-p", prompt, "--model", model],
                                  capture_output=True, text=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError) as e:
            last = {"stall": f"claude -p failed: {e}", "model": model}
            continue
        m = re.search(r"\{.*\}", proc.stdout or "", re.DOTALL)
        if not m:
            last = {"stall": "no JSON in model reply", "raw": (proc.stdout or "")[:400], "model": model}
            continue
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            last = {"stall": "unparseable JSON", "raw": (proc.stdout or "")[:400], "model": model}
            continue
        action = d.get("action")
        if action not in _PURSUE_ACTIONS:
            last = {"stall": f"bad action {action!r}", "model": model}
            continue
        if action == "keys":
            d["keys"] = [k for k in (d.get("keys") or []) if k in _NAV_ALLOWED_KEYS]
            if not d["keys"]:
                last = {"stall": "no valid keys", "model": model}
                continue
        if action == "type" and not (d.get("text") or "").strip():
            last = {"stall": "type with empty text", "model": model}
            continue
        d["model"] = model
        return d
    return {"stall": last.get("stall", "unknown"), **last}


def _session_alive(session: str) -> bool:
    """True if the tmux session still exists (detects an ssh drop / kill)."""
    try:
        return _run_tmux(["has-session", "-t", session])[0] == 0
    except FileNotFoundError:
        return False


def _observe(session: str, capture_lines: int, retries: int = 1) -> str | None:
    """Capture a settled, ANSI-stripped screen, retrying a transient blank/error.

    Returns the text, or None if the session is gone. An empty string means the
    session is alive but rendered nothing after the retries (a distinct signal)."""
    for attempt in range(retries + 1):
        if not _session_alive(session):
            return None
        try:
            txt = _strip_ansi(_capture_text(session, capture_lines))
        except (RuntimeError, FileNotFoundError):
            txt = ""
        if txt.strip():
            return txt
        time.sleep(0.5)  # transient blank (mid-redraw / just-cleared) — try once more
    return ""


# How many consecutive no-effect actions before goal mode gives up. The model is
# also TOLD it's stuck (via a history note) well before this, so it can recover
# itself; this is only the backstop against an infinite ineffective loop.
_STUCK_LIMIT = 3


def pursue(
    target: str,
    goal: str,
    max_steps: int = 15,
    settle_seconds: float = 2.5,
    wait_cap: float = 60.0,
    capture_lines: int = 200,
    model: str | None = None,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Pursue `goal` in a tmux TUI: observe → act → observe until done.

    The autonomous goal loop over navigate/ask primitives. Each step the model
    picks one action (keys / type / wait / done); after acting, emux waits for the
    screen to settle before the next observation, so the model reasons over a
    stable frame and can read an agent's reply. emux never spawns — the session
    must already exist and be running whatever UI the goal concerns."""
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    chain = [model] if model else [_NAV_MODEL_DEFAULT, _NAV_MODEL_ESCALATE]

    history: list[str] = []
    no_progress = 0  # consecutive actions that changed nothing on screen
    for step in range(max_steps):
        # --- observe, recovering from a dead session or a transient blank ---
        screen = _observe(session, capture_lines)
        if screen is None:
            return {"ok": False, "error": "session_gone",
                    "detail": "tmux session vanished (ssh dropped or killed)", "steps": history}
        if not screen.strip():
            return {"ok": False, "error": "blank_screen",
                    "detail": "session alive but rendered nothing", "steps": history}

        # Tell the model when its recent actions aren't moving the UI, so it can
        # change tactics (Escape, a different item) before we give up.
        ctx = history
        if no_progress:
            ctx = history + [f"NOTE: the last {no_progress} action(s) did not change the "
                             "screen — you appear stuck; try a DIFFERENT action (e.g. Escape, "
                             "a different menu item), or 'done' with success=false if impossible."]

        # --- decide, retrying once on a transient stall (re-observe first) ---
        d = _pursue_decide(chain, goal, screen, ctx)
        if "stall" in d:
            time.sleep(1.0)
            rescreen = _observe(session, capture_lines)
            if rescreen is None:
                return {"ok": False, "error": "session_gone", "steps": history}
            if rescreen and rescreen.strip():
                d = _pursue_decide(chain, goal, rescreen, ctx)
                screen = rescreen
        if "stall" in d:
            return {"ok": False, "error": "model_stalled", "detail": d["stall"],
                    "raw": d.get("raw"), "steps": history, "screen": screen}

        action = d["action"]
        esc = "" if d.get("model") == chain[0] else f" [escalated:{d.get('model')}]"
        thought = d.get("thought", "")

        if action == "done":
            return {"ok": True, "reached": "done", "success": bool(d.get("success", True)),
                    "summary": d.get("summary", ""), "steps": history, "screen": screen}
        if action == "wait":
            screen = _wait_stable(session, capture_lines, settle_seconds, 0.8, wait_cap)
            history.append(f"step {step + 1}: wait{esc} — observed: {_tail(screen)}")
            continue
        if action == "keys":
            for k in d["keys"]:
                _run_tmux(["send-keys", "-t", session, k])
            history.append(f"step {step + 1}: keys={d['keys']} ({thought!r}){esc}")
        elif action == "type":
            _run_tmux(["send-keys", "-t", session, "-l", d["text"]])
            if d.get("submit"):
                _run_tmux(["send-keys", "-t", session, "Enter"])
            history.append(f"step {step + 1}: type={d['text']!r} submit={bool(d.get('submit'))} ({thought!r}){esc}")

        # Let the UI react, then re-observe a settled frame.
        settled = _wait_stable(session, capture_lines, settle_seconds, 0.8, wait_cap)

        # --- stuck detection: an active action that changed nothing ---
        if settled == screen:
            no_progress += 1
            history[-1] += f" -> {_tail(settled)} [NO CHANGE x{no_progress}]"
            if no_progress >= _STUCK_LIMIT:
                return {"ok": False, "error": "stuck_no_progress",
                        "detail": f"{no_progress} consecutive actions changed nothing",
                        "steps": history, "screen": settled}
        else:
            no_progress = 0
            history[-1] += f" -> {_tail(settled)}"

    final = _observe(session, capture_lines) or ""
    return {"ok": False, "error": "max_steps_reached", "steps": history, "screen": final}


def _tail(screen: str, n: int = 3) -> str:
    """Last n non-blank lines of a screen, one line, for compact history."""
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:240]


@mcp.tool()
async def tmux_goal(
    target: str,
    goal: str,
    max_steps: int = 15,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Pursue a GOAL in a tmux TUI autonomously — observe, act, repeat until done.

    Goal mode: the level above tmux_navigate. Where navigate reaches a screen,
    this keeps going until the whole task is done — walking menus, typing
    messages/answers/field values, waiting for streamed replies, and judging
    completion. Each step a model (`claude -p`, fixed-cost — never the API) picks
    one action: navigate keys, type text, wait, or done. Escalates Haiku→Sonnet
    on a stall; keystrokes are allowlist-restricted.

    Still keystrokes-into-a-TUI — it cannot exec shell or do anything a person at
    that terminal couldn't. emux never spawns; create the session first.

    Recovers from: a flaky model step (escalates Haiku→Sonnet), a transient
    stall or blank capture (re-observe + retry once), a no-progress loop (warns
    the model, then aborts `stuck_no_progress` at the backstop), and a dropped
    session (`session_gone`). It does NOT gate destructive actions — `Enter` and
    free text are permitted, so a goal that leads to a "Delete? [y]" confirm will
    press it. Scope the goal accordingly, or point it at a read-only surface.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        goal: What to accomplish, e.g. "Ask the Railway agent to list my services,
            then tell me which have a cron schedule."
        max_steps: Max observe/act cycles before giving up (default 15).
        by_registry_name: Resolve `target` via the registry.

    Returns:
        {ok, reached:"done", success, summary, steps:[...], screen} on completion;
        {ok:false, error, steps, screen} on stall / max_steps.
    """
    return await asyncio.to_thread(pursue, target, goal, max_steps, 2.5, 60.0, 200, None, by_registry_name)


def run_mcp_server() -> None:
    """Start the emux MCP server (stdio transport).

    Invoked by `emux mcp`. The CLI dispatcher in `emux.cli` calls this.
    """
    mcp.run()


if __name__ == "__main__":
    run_mcp_server()
