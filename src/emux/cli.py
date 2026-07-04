"""emux CLI dispatcher.

  emux              → TUI picker (registered + live tmux sessions)
  emux mcp          → start the MCP server
  emux web          → start the web daemon (chat-style session monitor)
  emux register …   → CLI register
  emux ls           → list registered + live sessions
  emux send …        → send keys to a registered/live session
  emux interrupt …   → send C-c to a registered/live session
  emux capture …     → capture a registered/live session
  emux run …         → send a command, wait, and capture
  emux head …        → open a real terminal head for a session
  emux --version    → print version

The TUI is a Textual picker. It shows registered live sessions, registered
stale sessions, live-but-unregistered sessions, and registration actions. On
selection, exec `tmux attach -t <session>` so the user lands in the actual
tmux session — no further emux mediation.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from .server import (
    _live_sessions,
    _load_registry,
    _resolve_tmux,
    _run_tmux,
    _save_registry,
    converse,
    navigate,
    pursue,
    run_mcp_server,
    tmux_capture,
    tmux_run,
    tmux_send,
)


def _attach_to_session(session: str) -> None:
    """Replace this process with `tmux attach -t <session>`. Does not return."""
    tmux = _resolve_tmux()
    if tmux is None:
        print("emux: tmux not on PATH. install with `brew install tmux` or equivalent.", file=sys.stderr)
        sys.exit(2)
    os.execv(tmux, [tmux, "attach", "-t", session])


def _interactive_register(default_name: str | None = None) -> tuple[str, str] | None:
    """Prompt for a new registry entry. Returns (name, session) or None on abort."""
    print()
    name = input("  registry name (e.g. 'claude-prod'): ").strip()
    if not name:
        print("  aborted.")
        return None
    session_default = f" [{default_name}]" if default_name else ""
    session = input(f"  tmux session id{session_default}: ").strip() or (default_name or "")
    if not session:
        print("  aborted (no session id).")
        return None
    description = input("  description (optional): ").strip() or None
    tags_in = input("  tags (space-separated, optional): ").strip()
    tags = tags_in.split() if tags_in else []

    import time

    registry = _load_registry()
    registry[name] = {
        "session": session,
        "description": description,
        "tags": tags,
        "registered_at": int(time.time()),
    }
    _save_registry(registry)
    print(f"\n  registered '{name}' → {session}.")
    return name, session


def cmd_picker() -> int:
    """Run the textual TUI picker, then dispatch the user's selection."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        print("       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).", file=sys.stderr)
        return 2

    from .tui import run_tui

    result = run_tui()
    if result is None:
        # User quit, or there was nothing to pick.
        return 0

    action = result["action"]
    if action == "attach":
        _attach_to_session(result["session"])
        return 0  # not reached; execv replaces us
    if action == "register_then_attach":
        reg = _interactive_register(default_name=result["default_session"])
        if reg is None:
            return 0
        _attach_to_session(reg[1])
        return 0
    if action == "register_new":
        reg = _interactive_register()
        if reg is None:
            return 0
        prompt = f"\n  attach to '{reg[1]}' now? [Y/n]: "
        attach = input(prompt).strip().lower()
        if attach in {"", "y"}:
            _attach_to_session(reg[1])
        return 0
    if action == "unregister":
        registry = _load_registry()
        if result["name"] in registry:
            removed = registry.pop(result["name"])
            _save_registry(registry)
            print(f"\n  unregistered '{result['name']}' (was → {removed['session']}).")
        return 0

    print(f"emux: unknown TUI result action: {action!r}", file=sys.stderr)
    return 1


def cmd_ls() -> int:
    """Print registered + live sessions to stdout. Non-interactive; CI-friendly."""
    registry = _load_registry()
    live = _live_sessions()
    live_names = {s["name"] for s in live}

    print("registered sessions:")
    if not registry:
        print("  (none)")
    else:
        for name, entry in sorted(registry.items()):
            stale = " STALE" if entry["session"] not in live_names else ""
            desc = f" — {entry['description']}" if entry.get("description") else ""
            print(f"  {name} → {entry['session']}{stale}{desc}")

    print("\nlive tmux sessions:")
    if not live:
        print("  (none — `tmux list-sessions` returned no sessions)")
    else:
        registered_sessions = {entry["session"] for entry in registry.values()}
        for s in live:
            mark = " (registered)" if s["name"] in registered_sessions else ""
            attached = " (attached)" if s.get("attached") else ""
            print(f"  {s['name']}{mark}{attached}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    """Send a prompt to an AI running in a tmux session, print its reply.

    The session must already be running the AI (emux never spawns) and sitting
    at its input prompt. Waits until the pane stops changing before reading.
    """
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        return 2
    result = converse(
        target=args.target,
        prompt=args.prompt,
        settle_seconds=args.settle,
        max_seconds=args.max,
        by_registry_name=args.by_name,
        busy_markers=args.busy or None,
    )
    if not result.get("ok"):
        print(f"emux ask: {result.get('error', 'failed')}", file=sys.stderr)
        return 1
    if args.screen:
        print(result["screen"])
    else:
        print(result["reply"] or "(no new output — the AI may still be responding)")
    if not result["settled"]:
        print(f"\n[emux: hit {args.max:g}s cap; reply may be truncated]", file=sys.stderr)
    return 0


def cmd_navigate(args: argparse.Namespace) -> int:
    """Drive a session's TUI toward a goal using a model to pick keystrokes."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        return 2
    result = navigate(
        target=args.target,
        goal=args.goal,
        until=args.until,
        max_steps=args.max_steps,
        by_registry_name=args.by_name,
        allow_dangerous=args.yolo or bool(os.environ.get("EMUX_ALLOW_DANGEROUS")),
    )
    if not result.get("ok"):
        print(f"emux navigate: {result.get('error', 'failed')}", file=sys.stderr)
        for s in result.get("steps", []):
            print(f"  {s}", file=sys.stderr)
        return 1
    print(f"reached ({result['reached']}) in {len(result.get('steps', []))} step(s)")
    return 0


def cmd_goal(args: argparse.Namespace) -> int:
    """Pursue a goal in a session's TUI: observe → act → repeat until done."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        return 2
    result = pursue(
        target=args.target,
        goal=args.goal,
        max_steps=args.max_steps,
        by_registry_name=args.by_name,
        telos=args.telos or bool(os.environ.get("EMUX_TELOS")),
        allow_dangerous=args.yolo or bool(os.environ.get("EMUX_ALLOW_DANGEROUS")),
    )
    for s in result.get("steps", []):
        print(f"  {s}", file=sys.stderr)
    if tel := result.get("telos"):
        print(f"[telos: {tel['north_star_id']} closed {tel['outcome']}]", file=sys.stderr)
    if not result.get("ok"):
        print(f"emux goal: {result.get('error', 'failed')} ({result.get('detail','')})", file=sys.stderr)
        return 1
    verdict = "achieved" if result.get("success") else "not achievable"
    print(f"goal {verdict} in {len(result.get('steps', []))} step(s): {result.get('summary','')}")
    return 0 if result.get("success") else 1


def _watch_targets(
    registry: dict[str, dict[str, Any]],
    live: list[dict[str, Any]],
    registered_only: bool = False,
    needle: str | None = None,
) -> list[dict[str, Any]]:
    """Build ordered watch targets from registry + live tmux state."""
    live_by_name = {s["name"]: s for s in live}
    registered_sessions = {entry["session"] for entry in registry.values()}
    query = (needle or "").strip().lower()
    targets: list[dict[str, Any]] = []

    for name, entry in sorted(registry.items()):
        session = entry["session"]
        item = {
            "kind": "registered",
            "name": name,
            "session": session,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "live": session in live_by_name,
            "tmux": live_by_name.get(session),
        }
        targets.append(item)

    if not registered_only:
        for session in live:
            if session["name"] in registered_sessions:
                continue
            targets.append({
                "kind": "live",
                "name": session["name"],
                "session": session["name"],
                "description": None,
                "tags": [],
                "live": True,
                "tmux": session,
            })

    if not query:
        return targets

    def matches(item: dict[str, Any]) -> bool:
        haystack = " ".join([
            str(item.get("name", "")),
            str(item.get("session", "")),
            str(item.get("description") or ""),
            " ".join(str(t) for t in item.get("tags") or []),
        ]).lower()
        return query in haystack

    return [item for item in targets if matches(item)]


def _capture_session(session: str, lines: int) -> tuple[bool, str]:
    code, out, err = _run_tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
    if code != 0:
        return False, (err or "capture failed").strip()
    pane_lines = (out or "").splitlines()
    while pane_lines and not pane_lines[-1].strip():
        pane_lines.pop()
    content = "\n".join(pane_lines[-lines:])
    return True, content


def _render_watch_snapshot(
    targets: list[dict[str, Any]],
    captures: dict[str, tuple[bool, str]],
    lines: int,
    now: _dt.datetime | None = None,
) -> str:
    """Render a multi-session watch snapshot."""
    stamp = (now or _dt.datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    out = [
        f"emux watch  {stamp}",
        f"showing {len(targets)} session(s), last {lines} line(s)",
        "",
    ]
    if not targets:
        out.append("(no matching registered or live tmux sessions)")
        return "\n".join(out)

    for item in targets:
        label = item["name"]
        session = item["session"]
        status = "live" if item["live"] else "STALE"
        kind = "registered" if item["kind"] == "registered" else "unregistered live"
        desc = f" — {item['description']}" if item.get("description") else ""
        out.append(f"=== {label} -> {session} [{kind}; {status}]{desc}")
        if not item["live"]:
            out.append("    tmux session is gone; unregister or re-register this name")
            out.append("")
            continue
        ok, content = captures.get(session, (False, "not captured"))
        if not ok:
            out.append(f"    capture failed: {content}")
        elif not content:
            out.append("    (pane empty)")
        else:
            for line in content.splitlines():
                out.append(f"    {line}")
        out.append("")
    return "\n".join(out).rstrip()


def cmd_watch(args: argparse.Namespace) -> int:
    """Watch many registered/live tmux sessions in one terminal."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        print("       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).", file=sys.stderr)
        return 2

    try:
        while True:
            registry = _load_registry()
            live = _live_sessions()
            targets = _watch_targets(
                registry,
                live,
                registered_only=args.registered_only,
                needle=args.filter,
            )
            captures: dict[str, tuple[bool, str]] = {}
            for item in targets:
                if item["live"]:
                    captures[item["session"]] = _capture_session(item["session"], args.lines)
            snapshot = _render_watch_snapshot(targets, captures, args.lines)
            if not args.once and not args.no_clear:
                print("\033[2J\033[H", end="")
            print(snapshot, flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
    except (KeyboardInterrupt, BrokenPipeError):
        return 0


def cmd_register(args: argparse.Namespace) -> int:
    """Non-interactive register command for scripting."""
    import time
    registry = _load_registry()
    registry[args.name] = {
        "session": args.session,
        "description": args.description,
        "tags": args.tags or [],
        "manages": args.manages or [],
        "registered_at": int(time.time()),
    }
    _save_registry(registry)
    print(f"registered '{args.name}' → {args.session}")
    return 0


def cmd_unregister(args: argparse.Namespace) -> int:
    registry = _load_registry()
    if args.name not in registry:
        print(f"emux: '{args.name}' not registered.", file=sys.stderr)
        return 1
    removed = registry.pop(args.name)
    _save_registry(registry)
    print(f"unregistered '{args.name}' (was → {removed['session']})")
    return 0


def _joined_words(words: list[str], field_name: str) -> str:
    value = " ".join(words).strip()
    if not value:
        raise SystemExit(f"emux: {field_name} is required")
    return value


def _print_result(result: dict[str, Any], as_json: bool = False, content_key: str | None = None) -> int:
    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result.get("ok") and content_key and content_key in result:
        print(result[content_key], end="" if str(result[content_key]).endswith("\n") else "\n")
    elif result.get("ok"):
        resolved = result.get("resolved_session")
        target = result.get("target")
        if resolved and target != resolved:
            print(f"ok: {target} -> {resolved}")
        else:
            print("ok")
    else:
        print(f"emux: {result.get('error') or 'command_failed'}", file=sys.stderr)
        if result.get("stderr"):
            print(str(result["stderr"]).rstrip(), file=sys.stderr)
        if result.get("send_result"):
            print(json.dumps(result["send_result"], indent=2, sort_keys=True), file=sys.stderr)
        if result.get("capture_result"):
            print(json.dumps(result["capture_result"], indent=2, sort_keys=True), file=sys.stderr)
    return 0 if result.get("ok") else 1


def cmd_send(args: argparse.Namespace) -> int:
    """Send tmux keys to a registered name by default."""
    keys = _joined_words(args.keys, "keys")
    result = asyncio.run(tmux_send(
        target=args.target,
        keys=keys,
        enter=not args.no_enter,
        by_registry_name=not args.session,
    ))
    return _print_result(result, as_json=args.json)


def cmd_interrupt(args: argparse.Namespace) -> int:
    """Send C-c to a registered name by default."""
    result = asyncio.run(tmux_send(
        target=args.target,
        keys="C-c",
        enter=False,
        by_registry_name=not args.session,
    ))
    return _print_result(result, as_json=args.json)


def cmd_capture(args: argparse.Namespace) -> int:
    """Capture a registered name by default."""
    result = asyncio.run(tmux_capture(
        target=args.target,
        lines=args.lines,
        by_registry_name=not args.session,
    ))
    return _print_result(result, as_json=args.json, content_key="content")


def cmd_run(args: argparse.Namespace) -> int:
    """Send a command, wait, then capture."""
    command = _joined_words(args.command, "command")
    result = asyncio.run(tmux_run(
        target=args.target,
        command=command,
        wait_seconds=args.wait,
        capture_lines=args.lines,
        by_registry_name=not args.session,
    ))
    return _print_result(result, as_json=args.json, content_key="content")


def _resolve_session_target(target: str, by_registry_name: bool) -> tuple[bool, str, str | None]:
    """Resolve a CLI target to a live tmux session."""
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return False, "", f"'{target}' is not registered with Emux"
        session = registry[target]["session"]

    live_names = {s["name"] for s in _live_sessions()}
    if session not in live_names:
        return False, session, f"tmux session '{session}' is not live"
    return True, session, None


def _find_iterm_bundle_id() -> str | None:
    for app_name in ("iTerm2", "iTerm"):
        result = subprocess.run(
            ["osascript", "-e", f'id of application "{app_name}"'],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip() or "com.googlecode.iterm2"
    return None


def _write_head_command_file(session: str) -> Path:
    command = f"tmux attach -t {shlex.quote(session)}"
    safe_session = "".join(ch if ch.isalnum() or ch in ".-_" else "-" for ch in session)
    script_path = Path(tempfile.gettempdir()) / f"emux-head-{os.getpid()}-{safe_session}.command"
    script_path.write_text(f"#!/bin/zsh\nrm -f \"$0\"\nexec {command}\n")
    script_path.chmod(0o700)
    return script_path


def _open_iterm_head(session: str, new_window: bool = False) -> tuple[bool, str | None]:
    """Open iTerm2/iTerm attached to an existing tmux session."""
    if platform.system() != "Darwin":
        return False, "emux head currently supports macOS iTerm2/iTerm only"
    if _resolve_tmux() is None:
        return False, "tmux not found on PATH"
    if shutil.which("osascript") is None:
        return False, "osascript not found on PATH"
    if shutil.which("open") is None:
        return False, "macOS open command not found on PATH"

    bundle_id = _find_iterm_bundle_id()
    if bundle_id is None:
        return False, "iTerm2/iTerm is not installed or not visible to AppleScript"

    script_path = _write_head_command_file(session)

    open_args = ["open"]
    if new_window:
        # `open -n` asks LaunchServices for a new app instance. iTerm may still
        # choose its configured tab/window behavior, but this is the best
        # non-AppleScript hint available.
        open_args.append("-n")
    open_args.extend(["-b", bundle_id, str(script_path)])

    result = subprocess.run(open_args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "failed to open iTerm head").strip()
    return True, None


def _open_terminal_app_head(session: str) -> tuple[bool, str | None]:
    """Open macOS Terminal.app attached to an existing tmux session."""
    if platform.system() != "Darwin":
        return False, "Terminal.app head currently supports macOS only"
    if _resolve_tmux() is None:
        return False, "tmux not found on PATH"
    if shutil.which("open") is None:
        return False, "macOS open command not found on PATH"

    script_path = _write_head_command_file(session)
    result = subprocess.run(
        ["open", "-a", "Terminal", str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "failed to open Terminal head").strip()
    return True, None


def _open_terminal_head(
    session: str,
    terminal: str = "auto",
    new_window: bool = False,
) -> tuple[bool, str | None, str | None]:
    if terminal == "iterm":
        ok, err = _open_iterm_head(session, new_window=new_window)
        return ok, "iTerm", err
    if terminal == "terminal":
        ok, err = _open_terminal_app_head(session)
        return ok, "Terminal", err

    iterm_ok, iterm_err = _open_iterm_head(session, new_window=new_window)
    if iterm_ok:
        return True, "iTerm", None
    terminal_ok, terminal_err = _open_terminal_app_head(session)
    if terminal_ok:
        return True, "Terminal", None
    return False, None, f"iTerm failed: {iterm_err}; Terminal failed: {terminal_err}"


def cmd_head(args: argparse.Namespace) -> int:
    """Open a real terminal head for a registered name by default."""
    ok, session, err = _resolve_session_target(args.target, by_registry_name=not args.session)
    if not ok:
        print(f"emux: {err}", file=sys.stderr)
        return 1

    if args.print_command:
        print(f"tmux attach -t {shlex.quote(session)}")
        return 0

    ok, app_name, err = _open_terminal_head(session, terminal=args.terminal, new_window=args.window)
    if not ok:
        print(f"emux: {err}", file=sys.stderr)
        return 1
    print(f"opened {app_name} head for {args.target} -> {session}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emux",
        description="Eidos mux — pick up where you left off in tmux. TUI picker by default; subcommands for scripting and the MCP server.",
    )
    parser.add_argument("--version", action="version", version=f"emux {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("mcp", help="start the emux MCP server (stdio)")
    sub.add_parser("ls", help="print registered + live sessions (non-interactive)")

    p_ask = sub.add_parser("ask", help="send a prompt to an AI in a tmux session, wait for it to settle, print the reply")
    p_ask.add_argument("target", help="tmux session name (or registry name with -n)")
    p_ask.add_argument("prompt", help="the message to send to the AI")
    p_ask.add_argument("-n", "--by-name", action="store_true", help="resolve target via the registry")
    p_ask.add_argument("--settle", type=float, default=2.5, help="reply is done once the pane is unchanged this long (default 2.5s)")
    p_ask.add_argument("--max", type=float, default=90.0, help="hard cap on total wait (default 90s)")
    p_ask.add_argument("-b", "--busy", action="append", metavar="MARKER",
                       help="substring meaning the AI is still working (e.g. 'thinking'); never settle while on screen. Repeatable.")
    p_ask.add_argument("--screen", action="store_true", help="print the full settled pane instead of just the reply delta")

    p_nav = sub.add_parser("navigate", help="drive a session's TUI toward a goal, letting a model (claude -p) pick keystrokes")
    p_nav.add_argument("target", help="tmux session name (or registry name with -n)")
    p_nav.add_argument("goal", help="plain-English description of where to get to")
    p_nav.add_argument("-n", "--by-name", action="store_true", help="resolve target via the registry")
    p_nav.add_argument("--until", default=None, help="stop early if this substring appears on screen")
    p_nav.add_argument("--max-steps", type=int, default=12, help="max navigation steps (default 12)")
    p_nav.add_argument("--yolo", action="store_true", help="disable the destructive-action gate (also via $EMUX_ALLOW_DANGEROUS)")

    p_goal = sub.add_parser("goal", help="pursue a GOAL in a session's TUI autonomously (observe→act→repeat until done)")
    p_goal.add_argument("target", help="tmux session name (or registry name with -n)")
    p_goal.add_argument("goal", help="what to accomplish, in plain English")
    p_goal.add_argument("-n", "--by-name", action="store_true", help="resolve target via the registry")
    p_goal.add_argument("--max-steps", type=int, default=15, help="max observe/act cycles (default 15)")
    p_goal.add_argument("--telos", action="store_true", help="guard the run with the telos-md drift-guard (record it; abort on a telos stop signal). Also on via $EMUX_TELOS")
    p_goal.add_argument("--yolo", action="store_true", help="disable the destructive-action gate (also via $EMUX_ALLOW_DANGEROUS)")

    p_web = sub.add_parser("web", help="start the web daemon — monitor sessions in a browser (grid/groups/activity/flow/chat)")
    p_web.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; no auth — keep it local)")
    p_web.add_argument("--port", type=int, default=8689, help="port (default 8689)")
    p_web.add_argument("--open", action="store_true", help="open the browser after starting")
    p_web.add_argument("--print-launchd", action="store_true",
                       help="print a launchd plist that keeps the daemon running, then exit")
    p_watch = sub.add_parser("watch", help="watch registered + live sessions in one terminal")
    p_watch.add_argument("--once", action="store_true", help="render one snapshot and exit")
    p_watch.add_argument("--no-clear", action="store_true", help="do not clear screen between refreshes")
    p_watch.add_argument("--registered-only", action="store_true", help="hide live unregistered tmux sessions")
    p_watch.add_argument("--filter", default=None, help="only show sessions matching text")
    p_watch.add_argument("--lines", type=int, default=8, help="pane lines to show per session")
    p_watch.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds")

    p_reg = sub.add_parser("register", help="register a session under a friendly name")
    p_reg.add_argument("name")
    p_reg.add_argument("session")
    p_reg.add_argument("-d", "--description", default=None)
    p_reg.add_argument("-t", "--tags", nargs="*")
    p_reg.add_argument("-m", "--manages", nargs="*",
                       help="other registered names (or session ids) this agent manages — drawn as arrows in `emux web` flow view")

    p_unreg = sub.add_parser("unregister", help="remove a session from the registry")
    p_unreg.add_argument("name")

    p_send = sub.add_parser("send", help="send keys to a registered session")
    p_send.add_argument("target", help="registered name by default, or tmux session with --session")
    p_send.add_argument("keys", nargs="+", help="tmux keys or literal text to send")
    p_send.add_argument("--no-enter", action="store_true", help="do not append Enter after the keys")
    p_send.add_argument("--session", action="store_true", help="target a raw tmux session instead of a registry name")
    p_send.add_argument("--json", action="store_true", help="print structured result JSON")

    p_interrupt = sub.add_parser("interrupt", help="send C-c to a registered session")
    p_interrupt.add_argument("target", help="registered name by default, or tmux session with --session")
    p_interrupt.add_argument("--session", action="store_true", help="target a raw tmux session instead of a registry name")
    p_interrupt.add_argument("--json", action="store_true", help="print structured result JSON")

    p_capture = sub.add_parser("capture", help="capture a registered session pane")
    p_capture.add_argument("target", help="registered name by default, or tmux session with --session")
    p_capture.add_argument("--lines", type=int, default=200, help="scrollback lines to capture")
    p_capture.add_argument("--session", action="store_true", help="target a raw tmux session instead of a registry name")
    p_capture.add_argument("--json", action="store_true", help="print structured result JSON")

    p_run = sub.add_parser("run", help="send a command, wait, and capture the session")
    p_run.add_argument("target", help="registered name by default, or tmux session with --session")
    p_run.add_argument("command", nargs="+", help="command text to send")
    p_run.add_argument("--wait", type=float, default=2.0, help="seconds to wait before capture")
    p_run.add_argument("--lines", type=int, default=200, help="scrollback lines to capture")
    p_run.add_argument("--session", action="store_true", help="target a raw tmux session instead of a registry name")
    p_run.add_argument("--json", action="store_true", help="print structured result JSON")

    p_head = sub.add_parser("head", help="open a real terminal head for a registered session")
    p_head.add_argument("target", help="registered name by default, or tmux session with --session")
    p_head.add_argument("--session", action="store_true", help="target a raw tmux session instead of a registry name")
    p_head.add_argument("--terminal", choices=["auto", "iterm", "terminal"], default="auto", help="terminal app to open")
    p_head.add_argument("--window", action="store_true", help="open a new iTerm window instead of a new tab")
    p_head.add_argument("--print-command", action="store_true", help="print the tmux attach command without opening a terminal")

    args = parser.parse_args(argv)

    if args.cmd is None:
        # Bare `emux` → TUI picker.
        return cmd_picker()
    if args.cmd == "mcp":
        run_mcp_server()
        return 0
    if args.cmd == "web":
        if args.print_launchd:
            from .web import launchd_plist
            print(launchd_plist(host=args.host, port=args.port), end="")
            return 0
        from .web import run_web
        return run_web(host=args.host, port=args.port, open_browser=args.open)
    if args.cmd == "ls":
        return cmd_ls()
    if args.cmd == "ask":
        return cmd_ask(args)
    if args.cmd == "navigate":
        return cmd_navigate(args)
    if args.cmd == "goal":
        return cmd_goal(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    if args.cmd == "register":
        return cmd_register(args)
    if args.cmd == "unregister":
        return cmd_unregister(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "interrupt":
        return cmd_interrupt(args)
    if args.cmd == "capture":
        return cmd_capture(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "head":
        return cmd_head(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
