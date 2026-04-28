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
import json
import os
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

    Returns:
        The registry entry that was saved, plus whether the underlying tmux
        session is currently live.
    """
    registry = _load_registry()
    entry = {
        "session": session,
        "description": description,
        "tags": tags or [],
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


def run_mcp_server() -> None:
    """Start the emux MCP server (stdio transport).

    Invoked by `emux mcp`. The CLI dispatcher in `emux.cli` calls this.
    """
    mcp.run()


if __name__ == "__main__":
    run_mcp_server()
