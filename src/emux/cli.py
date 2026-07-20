"""emux CLI dispatcher.

  emux              → TUI picker (registered + live tmux sessions)
  emux new          → new mission: chat → confirm spec → spawn (same as 'n' in the TUI)
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
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import channels as channel_store
from . import linear as linear_store
from .server import (
    _live_sessions,
    _load_registry,
    _read_log,
    _resolve_tmux,
    _run_tmux,
    _save_registry,
    _start_stream_log,
    converse,
    login_flow,
    navigate,
    pursue,
    run_mcp_server,
    tmux_approve_gate,
    tmux_capture,
    tmux_gate,
    tmux_linear_evidence,
    tmux_linear_link,
    tmux_linear_status,
    tmux_run,
    tmux_send,
)


def _attach_to_session(session: str) -> None:
    """Replace this process with `tmux attach -t <session>`. Does not return."""
    tmux = _resolve_tmux()
    if tmux is None:
        print(
            "emux: tmux not on PATH. install with `brew install tmux` or equivalent.",
            file=sys.stderr,
        )
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


_PLAN_MODEL = os.environ.get("EMUX_PLAN_MODEL", "claude-sonnet-5")


def _parse_plan(reply: str) -> dict[str, Any] | None:
    """Extract the plan JSON from a model reply (tolerates stray prose)."""
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if not m:
        return None
    try:
        plan = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return plan if isinstance(plan, dict) else None


def _plan_prompt(transcript: list[str]) -> str:
    registry = _load_registry()
    hosts = sorted({str(e["host"]) for e in registry.values() if e.get("host")})
    convo = "\n".join(transcript)
    return (
        "You are emux's mission planner. The user describes what they want; you "
        "turn it into a tmux session spec they will confirm before anything runs.\n\n"
        f"KNOWN REMOTE HOSTS (ssh destinations already used by this registry): {hosts or '(none)'}\n"
        "host must be null for the local machine, or one of the known hosts, or an "
        "ssh destination the user explicitly named.\n\n"
        f"CONVERSATION SO FAR:\n{convo}\n\n"
        "Reply with ONE JSON object, nothing else:\n"
        '{"question": "<ONE clarifying question if you genuinely cannot spec the '
        'mission yet, else empty string>",\n'
        ' "summary": "<one sentence: what this session will do>",\n'
        ' "name": "<short kebab-case registry/session name>",\n'
        ' "host": <"ssh-dest" or null>,\n'
        ' "cwd": <"absolute working dir" or null>,\n'
        ' "command": "<the exact shell command to launch in the session, usually '
        "claude '<mission prompt>' — single-quote the prompt>\",\n"
        ' "permission_mode": "<default|acceptEdits|bypassPermissions>"}\n'
        "permission_mode governs how unattended a claude mission can run: "
        '"bypassPermissions" for read-only/investigate/report missions (it can '
        'work with no human attached); "acceptEdits" for missions that edit files '
        'but where shell commands should still gate; "default" only when the '
        "mission is risky enough that the user should attach and approve each "
        "step. Missions run detached, so prefer the most autonomous mode that is "
        "safe for the mission.\n"
        "Ask at most one question per turn, and only when the answer changes the "
        "spec. Prefer sensible defaults over questions."
    )


def _run_with_spinner(
    label: str, argv: list[str], timeout: float = 120
) -> subprocess.CompletedProcess:
    """Run a subprocess with a live spinner on stdout so the user can see the
    AI is still working. Silent (no spinner) when stdout is not a tty."""
    import itertools
    import threading

    if not sys.stdout.isatty():
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)

    stop = threading.Event()

    def _spin() -> None:
        for ch in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            print(f"\r  {ch} {label}", end="", flush=True)
            if stop.wait(0.1):
                break
        print("\r" + " " * (len(label) + 5) + "\r", end="", flush=True)

    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    finally:
        stop.set()
        t.join()


_PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions"}


def _apply_permission_mode(plan: dict[str, Any]) -> str:
    """The final launch command: deterministically append --permission-mode to a
    claude invocation (never trust the model to write its own flags). Non-claude
    commands and commands that already carry a permission flag pass through."""
    command = str(plan.get("command", "")).strip()
    mode = plan.get("permission_mode")
    if mode not in _PERMISSION_MODES or mode == "default":
        return command
    first = (command.split() or [""])[0]
    if first != "claude" or "--permission-mode" in command or "skip-permissions" in command:
        return command
    return f"{command} --permission-mode {mode}"


def _mission_path(plan: dict[str, Any]) -> str:
    """The hop chain a mission takes, e.g.
    this terminal → ssh mac-mini → tmux new-session 'check-dally' → claude.
    Derived from the plan fields, never from the model."""
    hops = ["this terminal"]
    if plan.get("host"):
        hops.append(f"ssh {plan['host']}")
    tmux_hop = f"tmux new-session '{plan.get('name', '?')}'"
    if plan.get("cwd"):
        tmux_hop += f" (cwd {plan['cwd']})"
    hops.append(tmux_hop)
    prog = (str(plan.get("command", "")).split() or ["?"])[0]
    hops.append(prog)
    return " → ".join(hops)


def _new_mission_chat() -> dict[str, Any] | None:
    """Chat with `claude -p` until the user confirms a session spec. Returns the
    confirmed plan dict, or None on abort. Fixed-cost CLI — never the API."""
    claude = shutil.which("claude")
    if claude is None:
        print("emux: claude CLI not on PATH (needed for new-mission planning).", file=sys.stderr)
        return None

    print()
    want = input("  what do you want to do? ").strip()
    if not want:
        print("  aborted.")
        return None
    transcript = [f"user: {want}"]

    while True:
        try:
            proc = _run_with_spinner(
                "planning… (the AI is drafting your session spec)",
                [claude, "-p", _plan_prompt(transcript), "--model", _PLAN_MODEL],
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            print(f"  emux: claude -p failed: {e}", file=sys.stderr)
            return None
        plan = _parse_plan(proc.stdout or "")
        if plan is None:
            print(
                f"  emux: no usable plan in model reply: {(proc.stdout or proc.stderr or '')[:300]}",
                file=sys.stderr,
            )
            return None

        if plan.get("question"):
            print(f"\n  {plan['question']}")
            answer = input("  > ").strip()
            if not answer:
                print("  aborted.")
                return None
            transcript.append(f"planner asked: {plan['question']}")
            transcript.append(f"user: {answer}")
            continue

        print("\n  ━━ mission plan ━━")
        print(f"  summary   {plan.get('summary', '?')}")
        print(f"  name      {plan.get('name', '?')}")
        print(f"  host      {plan.get('host') or 'local'}")
        print(f"  cwd       {plan.get('cwd') or '(default)'}")
        plan["command"] = _apply_permission_mode(plan)
        mode = str(plan.get("permission_mode") or "default")
        if mode not in _PERMISSION_MODES:
            mode = "default"
        perms_note = {
            "default": "gated — attach to approve each step",
            "acceptEdits": "edits auto-approved; shell commands still gate",
            "bypassPermissions": "fully unattended — no approval prompts",
        }[mode]
        print(f"  command   {plan['command']}")
        print(f"  perms     {mode} ({perms_note})")
        print(f"  path      {_mission_path(plan)}")
        print(
            f"  attach    {_head_attach_command(str(plan.get('name', '?')), plan.get('host') or None)}"
        )
        answer = input("\n  start it? [Y/n, or type changes]: ").strip()
        if answer.lower() in {"", "y", "yes"}:
            return plan
        if answer.lower() in {"n", "no", "q"}:
            print("  aborted.")
            return None
        transcript.append(f"planner proposed: {json.dumps(plan)}")
        transcript.append(f"user feedback: {answer}")


def cmd_new_mission() -> int:
    """`n` in the TUI: describe a mission, confirm the AI's spec, spawn it."""
    plan = _new_mission_chat()
    if plan is None:
        return 0
    if not plan.get("name") or not plan.get("command"):
        print("emux: plan is missing a name or command; not starting.", file=sys.stderr)
        return 1

    from .server import _session_exists, tmux_spawn

    # Name-collision guard: tmux_spawn kills any same-name session, and a live
    # agent session is state you can't respawn. Never let that happen silently.
    name, host = str(plan["name"]), plan.get("host") or None
    if _session_exists(name, host=host):
        where = f" on {host}" if host else ""
        print(f"\n  ⚠ session '{name}' is already live{where} — starting would KILL it.")
        choice = (
            input(f"  [u]nique name '{name}-2' / [r]eplace it / [a]bort (default u): ")
            .strip()
            .lower()
        )
        if choice in {"a", "abort"}:
            print("  aborted.")
            return 0
        if choice not in {"r", "replace"}:
            n = 2
            while _session_exists(f"{name}-{n}", host=host):
                n += 1
            name = f"{name}-{n}"
            plan["name"] = name
            print(f"  starting as '{name}'.")

    result = asyncio.run(
        tmux_spawn(
            name=name,
            command=str(plan["command"]),
            host=host,
            cwd=plan.get("cwd") or None,
            description=plan.get("summary") or None,
            tags=["mission"],
        )
    )
    if not result.get("ok"):
        print(f"emux: spawn failed: {result.get('stderr') or result.get('error')}", file=sys.stderr)
        return 1
    where = f" on {result['host']}" if result.get("host") else ""
    print(f"\n  started '{result['name']}'{where}.")

    # Liftoff proof: "tmux accepted the keys" is not "the mission is running".
    # Capture the pane after a beat so a dead command (claude not on PATH, not
    # logged in, bad cwd) is caught HERE, not discovered on attach an hour later.
    time.sleep(2.5)
    code, out, _err = _run_tmux(
        ["capture-pane", "-t", result["session"], "-p", "-S", "-15"], host=host
    )
    pane = "\n".join(ln for ln in (out or "").splitlines() if ln.strip())[-600:]
    if code == 0 and pane:
        print("\n  ━━ first light (pane after 2.5s) ━━")
        for ln in pane.splitlines()[-8:]:
            print(f"  │ {ln}")
        if re.search(r"command not found|No such file or directory|not logged in", pane, re.I):
            print("\n  ⚠ that looks like a failed launch — attach and check.")
    else:
        print("\n  ⚠ could not capture the pane — the session may have died already.")
    attach = input("  attach now? [Y/n]: ").strip().lower()
    if attach in {"", "y", "yes"}:
        os.execvp(
            "/bin/sh",
            ["/bin/sh", "-c", _head_attach_command(result["session"], result.get("host"))],
        )
    return 0


def cmd_picker() -> int:
    """Run the textual TUI picker, then dispatch the user's selection."""
    if _resolve_tmux() is None:
        print("emux: tmux not found on PATH.", file=sys.stderr)
        print(
            "       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).",
            file=sys.stderr,
        )
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
    if action == "new_mission":
        return cmd_new_mission()
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
        print(
            f"emux goal: {result.get('error', 'failed')} ({result.get('detail', '')})",
            file=sys.stderr,
        )
        return 1
    verdict = "achieved" if result.get("success") else "not achievable"
    print(f"goal {verdict} in {len(result.get('steps', []))} step(s): {result.get('summary', '')}")
    return 0 if result.get("success") else 1


def cmd_login(args: argparse.Namespace) -> int:
    """Drive a Claude Code login sequence in a session; print the OAuth URL."""
    result = login_flow(
        target=args.target,
        code=args.code,
        switch=args.switch,
        by_registry_name=args.by_name,
    )
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok"):
        detail = result.get("detail", "")
        print(
            f"emux login: {result.get('error', 'failed')}" + (f" — {detail}" if detail else ""),
            file=sys.stderr,
        )
        if result.get("screen"):
            print(f"  screen: {result['screen']}", file=sys.stderr)
        return 1
    if result.get("logged_in"):
        print("login successful")
        return 0
    print("open this url in a browser and sign in:")
    print()
    print(f"  {result['url']}")
    print()
    print(result["next"])
    return 0


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
            targets.append(
                {
                    "kind": "live",
                    "name": session["name"],
                    "session": session["name"],
                    "description": None,
                    "tags": [],
                    "live": True,
                    "tmux": session,
                }
            )

    if not query:
        return targets

    def matches(item: dict[str, Any]) -> bool:
        haystack = " ".join(
            [
                str(item.get("name", "")),
                str(item.get("session", "")),
                str(item.get("description") or ""),
                " ".join(str(t) for t in item.get("tags") or []),
            ]
        ).lower()
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
        print(
            "       install with `brew install tmux` (macOS) or `apt install tmux` (Debian).",
            file=sys.stderr,
        )
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
    entry = {
        "session": args.session,
        "description": args.description,
        "tags": args.tags or [],
        "manages": args.manages or [],
        "registered_at": int(time.time()),
    }
    if args.linear_issue:
        try:
            entry["linear"] = linear_store.metadata(
                args.linear_issue, args.linear_project, args.linear_team, args.acceptance
            )
        except ValueError as e:
            print(f"emux register: {e}", file=sys.stderr)
            return 2
    elif args.linear_project or args.linear_team or args.acceptance:
        print("emux register: --linear-issue is required with Linear metadata", file=sys.stderr)
        return 2
    entry["channels"] = channel_store.resolve_channels(
        args.name, {**entry, "channels": args.channels or []}
    )
    if not entry["channels"]:
        # Remote targeting (emux-remote/1.0) requires >=1 channel; a bare
        # registration must stay reachable (EID-869).
        entry["channels"] = ["default"]
    code, out, _err = _run_tmux(
        ["display-message", "-p", "-t", args.session, "#{pane_current_path}"]
    )
    entry["cwd"] = (out.strip() if code == 0 else "") or os.getcwd()
    registry[args.name] = entry
    _save_registry(registry)
    _start_stream_log(args.session, args.name)  # arm durable logging on register
    suffix = f" channels={','.join(entry['channels'])}" if entry["channels"] else ""
    print(f"registered '{args.name}' → {args.session}{suffix}")
    for suggestion in channel_store.suggest_channels(registry):
        print(
            f"suggested channel: T{suggestion['tier']} {suggestion['name']} ({suggestion['reason']})"
        )
    return 0


def cmd_channel(args: argparse.Namespace) -> int:
    registry = _load_registry()
    if args.channel_cmd == "create":
        try:
            created = channel_store.create_channel(
                args.name, args.tier, args.description, args.parent, args.matchers
            )
        except ValueError as e:
            print(f"emux channel: {e}", file=sys.stderr)
            return 2
        changed = channel_store.refresh_registry(registry)
        if changed:
            _save_registry(registry)
        created["tagged_sessions"] = changed
        print(
            json.dumps(created, indent=2)
            if args.json
            else f"created T{created['tier']} channel '{created['name']}'"
        )
        return 0
    if args.channel_cmd == "note":
        try:
            note = channel_store.append_note(args.name, args.kind, args.text, args.source)
        except ValueError as e:
            print(f"emux channel: {e}", file=sys.stderr)
            return 2
        print(json.dumps(note, indent=2) if args.json else f"noted {args.kind} in '{args.name}'")
        return 0
    if args.channel_cmd == "suggest":
        suggestions = channel_store.suggest_channels(registry)
        print(
            json.dumps(suggestions, indent=2)
            if args.json
            else "\n".join(f"T{x['tier']} {x['name']}: {x['reason']}" for x in suggestions)
            or "no channel suggestions"
        )
        return 0
    if args.channel_cmd == "refresh":
        changed = channel_store.refresh_registry(registry)
        channel_store.refresh_okf()
        if changed:
            _save_registry(registry)
        print(json.dumps(changed, indent=2) if args.json else f"refreshed {len(changed)} sessions")
        return 0
    definitions = channel_store.load_channels()
    if args.channel_cmd == "show":
        try:
            result = channel_store.channel_context(args.name, registry, definitions)
        except ValueError as e:
            print(f"emux channel: {e}", file=sys.stderr)
            return 2
        print(json.dumps(result, indent=2))
        return 0
    rows = [
        channel_store.channel_context(name, registry, definitions)
        for name in sorted(definitions, key=lambda n: (definitions[n].get("tier", 9), n))
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            parent = f" ← {row['parent']}" if row.get("parent") else ""
            print(
                f"T{row['tier']} {row['name']}{parent} ({len(row['sessions'])} sessions) — {row['description']}"
            )
    return 0


def cmd_linear(args: argparse.Namespace) -> int:
    """Manage local Linear work contracts; never mutates Linear itself."""
    if args.linear_cmd == "link":
        result = asyncio.run(
            tmux_linear_link(args.session, args.issue, args.project, args.team, args.acceptance)
        )
    elif args.linear_cmd == "evidence":
        result = asyncio.run(
            tmux_linear_evidence(args.session, args.criterion, args.proof, args.source)
        )
    else:
        result = asyncio.run(tmux_linear_status(args.session, args.channel))
    if not result.get("ok"):
        print(f"emux linear: {result.get('error', 'failed')}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, indent=2))
    elif args.linear_cmd == "link":
        print(f"linked {args.session} → {result['linear']['issue']}")
    elif args.linear_cmd == "evidence":
        evidence = result["evidence"]
        print(f"recorded evidence for {evidence['issue']} criterion {evidence['criterion']}")
    else:
        for row in result["work"]:
            missing = len(row["missing_evidence"])
            recommendation = (
                f" → recommend {row['recommended_linear_status']}"
                if row["recommended_linear_status"]
                else ""
            )
            print(
                f"{row['issue']}  {row['state']}  session={row['session']}  missing={missing}{recommendation}"
            )
        if not result["work"]:
            print("no Linear-linked sessions")
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    """Print a session's durable character log — the complete history emux
    streamed via pipe-pane, not an ephemeral pane snapshot."""
    out = _read_log(args.name, lines=args.lines, strip=not args.raw)
    if not out.strip():
        print(
            f"emux: no log for '{args.name}' yet — logging arms on register/drive "
            "and streams forward from that moment.",
            file=sys.stderr,
        )
        return 1
    print(out)
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


def _print_result(
    result: dict[str, Any], as_json: bool = False, content_key: str | None = None
) -> int:
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


# A single one of these is a NAMED KEY — a control/navigation keystroke, not a
# command line — so it must never auto-submit (appending Enter would fire a stray
# newline after e.g. an arrow key, or a second event after a bare Enter/C-c).
# Ordinary literal text ("git status") keeps the normal auto-submit.
_NAMED_KEYS = frozenset(
    {
        "Enter", "Escape", "Tab", "BTab", "Space", "BSpace",
        "Up", "Down", "Left", "Right", "Home", "End",
        "PageUp", "PageDown", "PPage", "NPage",
        "Delete", "DC", "Insert", "IC",
        "C-c", "C-d", "C-u", "C-k", "C-a", "C-e",
    }
)


def cmd_send(args: argparse.Namespace) -> int:
    """Send tmux keys to a registered name by default."""
    keys = _joined_words(args.keys, "keys")
    # A single recognized named key is a keystroke, not a command — don't submit
    # it. Anything else (literal text) retains the normal auto-submit.
    enter = not args.no_enter and keys not in _NAMED_KEYS
    result = asyncio.run(
        tmux_send(
            target=args.target,
            keys=keys,
            enter=enter,
            by_registry_name=not args.session,
        )
    )
    return _print_result(result, as_json=args.json)


def cmd_interrupt(args: argparse.Namespace) -> int:
    """Send C-c to a registered name by default."""
    result = asyncio.run(
        tmux_send(
            target=args.target,
            keys="C-c",
            enter=False,
            by_registry_name=not args.session,
        )
    )
    return _print_result(result, as_json=args.json)


def cmd_gate(args: argparse.Namespace) -> int:
    result = asyncio.run(tmux_gate(target=args.target, by_registry_name=not args.session))
    if result.get("ok") and not args.json:
        print(f"{result['gate_type']} {result['gate_fingerprint']} "
              f"(expires in {result['expires_in']}s)")
        return 0
    return _print_result(result, as_json=args.json)


def cmd_approve(args: argparse.Namespace) -> int:
    result = asyncio.run(tmux_approve_gate(
        target=args.target, gate_fingerprint=args.fingerprint, action=args.action,
        key=args.key, by_registry_name=not args.session, subject=args.subject,
        device=args.device, request_id=args.request_id,
    ))
    return _print_result(result, as_json=args.json)


def cmd_grant_answer(args: argparse.Namespace) -> int:
    """Answer a gate only if a scoped delegation grant pre-authorizes it (EID-874)."""
    from .server import tmux_grant_answer

    result = asyncio.run(tmux_grant_answer(
        target=args.target, identity=args.identity,
        by_registry_name=not args.session, request_id=args.request_id,
    ))
    return _print_result(result, as_json=args.json)


def cmd_capture(args: argparse.Namespace) -> int:
    """Capture a registered name by default."""
    result = asyncio.run(
        tmux_capture(
            target=args.target,
            lines=args.lines,
            by_registry_name=not args.session,
        )
    )
    return _print_result(result, as_json=args.json, content_key="content")


def cmd_run(args: argparse.Namespace) -> int:
    """Send a command, wait, then capture."""
    command = _joined_words(args.command, "command")
    result = asyncio.run(
        tmux_run(
            target=args.target,
            command=command,
            wait_seconds=args.wait,
            capture_lines=args.lines,
            by_registry_name=not args.session,
        )
    )
    return _print_result(result, as_json=args.json, content_key="content")


def _resolve_session_target(
    target: str, by_registry_name: bool
) -> tuple[bool, str, str | None, str | None]:
    """Resolve a CLI target to a live tmux session: (ok, session, host, err).
    Host-aware — a registry entry on another machine checks liveness THERE."""
    from .server import _session_exists

    session, host = target, None
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return False, "", None, f"'{target}' is not registered with Emux"
        session = registry[target]["session"]
        host = registry[target].get("host")
    if not _session_exists(session, host=host):
        where = f" on {host}" if host else ""
        return False, session, host, f"tmux session '{session}' is not live{where}"
    return True, session, host, None


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


def _head_attach_command(session: str, host: str | None = None) -> str:
    """The shell command a head runs: local attach, or `ssh -t` for a remote
    session (same PATH shim as server._run_tmux — non-interactive ssh misses
    Homebrew's bin)."""
    if host:
        remote = (
            f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH tmux attach -t {shlex.quote(session)}"
        )
        return f"ssh -t {shlex.quote(host)} {shlex.quote(remote)}"
    return f"tmux attach -t {shlex.quote(session)}"


def _write_head_command_file(session: str, host: str | None = None) -> Path:
    command = _head_attach_command(session, host)
    safe_session = "".join(ch if ch.isalnum() or ch in ".-_" else "-" for ch in session)
    script_path = Path(tempfile.gettempdir()) / f"emux-head-{os.getpid()}-{safe_session}.command"
    script_path.write_text(f'#!/bin/zsh\nrm -f "$0"\nexec {command}\n')
    script_path.chmod(0o700)
    return script_path


def _open_iterm_head(
    session: str, new_window: bool = False, host: str | None = None
) -> tuple[bool, str | None]:
    """Open iTerm2/iTerm attached to an existing tmux session (ssh -t if remote)."""
    if platform.system() != "Darwin":
        return False, "emux head currently supports macOS iTerm2/iTerm only"
    if host is None and _resolve_tmux() is None:
        return False, "tmux not found on PATH"
    if shutil.which("osascript") is None:
        return False, "osascript not found on PATH"
    if shutil.which("open") is None:
        return False, "macOS open command not found on PATH"

    bundle_id = _find_iterm_bundle_id()
    if bundle_id is None:
        return False, "iTerm2/iTerm is not installed or not visible to AppleScript"

    script_path = _write_head_command_file(session, host)

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


def _open_terminal_app_head(session: str, host: str | None = None) -> tuple[bool, str | None]:
    """Open macOS Terminal.app attached to an existing tmux session."""
    if platform.system() != "Darwin":
        return False, "Terminal.app head currently supports macOS only"
    if host is None and _resolve_tmux() is None:
        return False, "tmux not found on PATH"
    if shutil.which("open") is None:
        return False, "macOS open command not found on PATH"

    script_path = _write_head_command_file(session, host)
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
    host: str | None = None,
) -> tuple[bool, str | None, str | None]:
    if terminal == "iterm":
        ok, err = _open_iterm_head(session, new_window=new_window, host=host)
        return ok, "iTerm", err
    if terminal == "terminal":
        ok, err = _open_terminal_app_head(session, host=host)
        return ok, "Terminal", err

    iterm_ok, iterm_err = _open_iterm_head(session, new_window=new_window, host=host)
    if iterm_ok:
        return True, "iTerm", None
    terminal_ok, terminal_err = _open_terminal_app_head(session, host=host)
    if terminal_ok:
        return True, "Terminal", None
    return False, None, f"iTerm failed: {iterm_err}; Terminal failed: {terminal_err}"


def _current_tmux_session() -> str | None:
    """The tmux session this process is running inside (a hook runs in the
    worker's pane), or None if not in tmux."""
    import subprocess

    try:
        r = subprocess.run(
            ["tmux", "display-message", "-p", "#S"], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() or None
    except Exception:
        return None


def _push_signal(host: str, session: str, rec: dict) -> str:
    """Best-effort PUSH: ssh the SAME record (same id) to a target box's inbox.
    Dedup by id means push + pull collapse to one signal at the parent."""
    import json
    import shlex
    import subprocess

    from .server import remote_inbox_relpath

    rel = remote_inbox_relpath(session)
    line = json.dumps(rec)
    cmd = (
        f"mkdir -p {shlex.quote(rel.rsplit('/', 1)[0])} && "
        f"printf '%s\\n' {shlex.quote(line)} >> {shlex.quote(rel)}"
    )
    try:
        r = subprocess.run(["ssh", host, cmd], capture_output=True, text=True, timeout=15)
        return f" (pushed → {host})" if r.returncode == 0 else f" (push → {host} FAILED)"
    except Exception:
        return f" (push → {host} errored)"


def cmd_signal(args: argparse.Namespace) -> int:
    from .server import inject_signal

    session = args.session or _current_tmux_session()
    if not session:
        print("emux signal: no --session given and not inside a tmux session", file=sys.stderr)
        return 2
    rec = inject_signal(session, args.kind, args.payload or "")
    if rec is None:
        print("emux signal: write failed", file=sys.stderr)
        return 1
    pushed = _push_signal(args.push, session, rec) if args.push else ""
    print(f"emux signal: {rec['kind']} -> {session}{pushed}")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    """Mine the gate ledger into policy-rule suggestions.

    Every gate the web daemon sees is logged to ~/.local/share/emux/gates.jsonl
    with how it was handled (auto-answered by a policy rule, or escalated to a
    human). For escalated gates, the emux audit trail tells us what keystroke a
    human/manager actually sent next — so the ledger IS the labeled dataset,
    and this report turns 'this gate appeared 14x, always answered Enter' into
    a ready-to-paste gatepolicy.json rule. No model calls; pure counting."""
    gates_path = Path.home() / ".local" / "share" / "emux" / "gates.jsonl"
    audit_path = Path.home() / ".local" / "share" / "emux" / "audit.jsonl"
    if not gates_path.is_file():
        print("no gate ledger yet — gates are recorded as the web daemon sees them")
        return 0
    events = []
    for line in gates_path.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    sends = []
    if audit_path.is_file():
        for line in audit_path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("op") in ("tmux_send", "tmux_login") and rec.get("t"):
                sends.append(rec)
    # group by (agent, gate); for escalated sightings, the ANSWER is the first
    # audited send to that session within 120s — the human's actual keystroke.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in events:
        g = groups.setdefault(
            (ev.get("agent", ""), ev.get("gate", "")),
            {"auto": 0, "escalated": 0, "keys": {}, "last": 0},
        )
        g[ev.get("action", "escalated")] = g.get(ev.get("action", "escalated"), 0) + 1
        g["last"] = max(g["last"], ev.get("t", 0))
        if ev.get("action") == "auto":
            k = " ".join(ev.get("keys") or [])
            g["keys"][k] = g["keys"].get(k, 0) + 1
        else:
            for s in sends:
                if s.get("target") == ev.get("session") and 0 <= s["t"] - ev.get("t", 0) <= 120:
                    k = str(s.get("keys", ""))[:40]
                    g["keys"][k] = g["keys"].get(k, 0) + 1
                    break
    if not groups:
        print("gate ledger is empty")
        return 0
    order = sorted(groups.items(), key=lambda kv: -(kv[1]["auto"] + kv[1]["escalated"]))
    print(f"{len(events)} gate sightings, {len(groups)} distinct gates\n")
    for (agent, gate), g in order[: args.limit]:
        total = g["auto"] + g["escalated"]
        top = max(g["keys"].items(), key=lambda kv: kv[1]) if g["keys"] else None
        print(f"{total:4d}x  [{agent or '?'}] {gate[:70]!r}")
        print(
            f"       auto:{g['auto']} escalated:{g['escalated']}"
            + (f"  usual answer: {top[0]!r} ({top[1]}x)" if top else "  no recorded answer")
        )
        if g["escalated"] and top and top[1] >= max(3, total // 2):
            rule = {
                "pattern": re.escape(gate)[:80],
                "keys": top[0].split() or ["Enter"],
                "note": f"mined from {total} sightings",
            }
            print(f"       suggested rule: {json.dumps(rule)}")
    print(
        "\nadd rules to ~/.config/emux/gatepolicy.json as "
        '{"rules": [{"pattern": "...", "keys": ["Enter"], "note": "..."}]} '
        "— the daemon answers matching gates itself (destructive text never auto-answers)"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose a session's environment (liveness, capture, log, gate, fs access)."""
    from .server import doctor

    result = doctor(args.target, by_registry_name=not args.session)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if not result.get("ok") and result.get("error"):
        print(f"emux doctor: {result['error']}", file=sys.stderr)
        return 1
    for c in result.get("checks", []):
        mark = {True: "ok  ", False: "FAIL", None: "info"}[c["ok"]]
        print(f"  [{mark}] {c['check']}: {c['detail']}")
    print(f"diagnosis: {result.get('diagnosis', '?')}")
    return 0 if result.get("ok") else 1


def cmd_head(args: argparse.Namespace) -> int:
    """Open a real terminal head for a registered name by default. Remote
    sessions attach via `ssh -t` — same one-command feel as local."""
    ok, session, host, err = _resolve_session_target(args.target, by_registry_name=not args.session)
    if not ok:
        print(f"emux: {err}", file=sys.stderr)
        return 1

    if args.print_command:
        print(_head_attach_command(session, host))
        return 0

    ok, app_name, err = _open_terminal_head(
        session, terminal=args.terminal, new_window=args.window, host=host
    )
    if not ok:
        print(f"emux: {err}", file=sys.stderr)
        return 1
    where = f" on {host}" if host else ""
    print(f"opened {app_name} head for {args.target} -> {session}{where}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emux",
        description="Eidos mux — pick up where you left off in tmux. TUI picker by default; subcommands for scripting and the MCP server.",
    )
    parser.add_argument("--version", action="version", version=f"emux {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("mcp", help="start the emux MCP server (stdio)")
    sub.add_parser(
        "new",
        help="new mission: describe what you want, confirm the AI's session spec, start it (same as 'n' in the TUI)",
    )
    sub.add_parser("ls", help="print registered + live sessions (non-interactive)")

    p_ask = sub.add_parser(
        "ask",
        help="send a prompt to an AI in a tmux session, wait for it to settle, print the reply",
    )
    p_ask.add_argument("target", help="tmux session name (or registry name with -n)")
    p_ask.add_argument("prompt", help="the message to send to the AI")
    p_ask.add_argument(
        "-n", "--by-name", action="store_true", help="resolve target via the registry"
    )
    p_ask.add_argument(
        "--settle",
        type=float,
        default=2.5,
        help="reply is done once the pane is unchanged this long (default 2.5s)",
    )
    p_ask.add_argument(
        "--max", type=float, default=90.0, help="hard cap on total wait (default 90s)"
    )
    p_ask.add_argument(
        "-b",
        "--busy",
        action="append",
        metavar="MARKER",
        help="substring meaning the AI is still working (e.g. 'thinking'); never settle while on screen. Repeatable.",
    )
    p_ask.add_argument(
        "--screen",
        action="store_true",
        help="print the full settled pane instead of just the reply delta",
    )

    p_nav = sub.add_parser(
        "navigate",
        help="drive a session's TUI toward a goal, letting a model (claude -p) pick keystrokes",
    )
    p_nav.add_argument("target", help="tmux session name (or registry name with -n)")
    p_nav.add_argument("goal", help="plain-English description of where to get to")
    p_nav.add_argument(
        "-n", "--by-name", action="store_true", help="resolve target via the registry"
    )
    p_nav.add_argument(
        "--until", default=None, help="stop early if this substring appears on screen"
    )
    p_nav.add_argument(
        "--max-steps", type=int, default=12, help="max navigation steps (default 12)"
    )
    p_nav.add_argument(
        "--yolo",
        action="store_true",
        help="disable the destructive-action gate (also via $EMUX_ALLOW_DANGEROUS)",
    )

    p_goal = sub.add_parser(
        "goal", help="pursue a GOAL in a session's TUI autonomously (observe→act→repeat until done)"
    )
    p_goal.add_argument("target", help="tmux session name (or registry name with -n)")
    p_goal.add_argument("goal", help="what to accomplish, in plain English")
    p_goal.add_argument(
        "-n", "--by-name", action="store_true", help="resolve target via the registry"
    )
    p_goal.add_argument(
        "--max-steps", type=int, default=15, help="max observe/act cycles (default 15)"
    )
    p_goal.add_argument(
        "--telos",
        action="store_true",
        help="guard the run with the telos-md drift-guard (record it; abort on a telos stop signal). Also on via $EMUX_TELOS",
    )
    p_goal.add_argument(
        "--yolo",
        action="store_true",
        help="disable the destructive-action gate (also via $EMUX_ALLOW_DANGEROUS)",
    )

    p_login = sub.add_parser(
        "login",
        help="drive a Claude Code login in a session: surface the OAuth URL, then finish with --code",
    )
    p_login.add_argument("target", help="tmux session name (or registry name with -n)")
    p_login.add_argument(
        "-n",
        "--by-name",
        action="store_true",
        help="resolve target via the registry (works across hosts)",
    )
    p_login.add_argument(
        "--code",
        default=None,
        help="finish the flow: paste this authorization code into the waiting prompt",
    )
    p_login.add_argument(
        "--switch",
        action="store_true",
        help="change account: /logout first, then start the login sequence",
    )
    p_login.add_argument("--json", action="store_true", help="print structured result JSON")

    p_web = sub.add_parser(
        "web",
        help="start the web daemon — monitor sessions in a browser (grid/groups/activity/flow/chat)",
    )
    p_web.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default 127.0.0.1; no auth — keep it local)",
    )
    p_web.add_argument("--port", type=int, default=8689, help="port (default 8689)")
    p_web.add_argument(
        "--public-origin",
        default=None,
        help="single trusted reverse-proxy origin, e.g. https://emux.example.com",
    )
    p_web.add_argument("--open", action="store_true", help="open the browser after starting")
    p_web.add_argument(
        "--print-launchd",
        action="store_true",
        help="print a launchd plist that keeps the daemon running, then exit",
    )
    p_watch = sub.add_parser("watch", help="watch registered + live sessions in one terminal")
    p_watch.add_argument("--once", action="store_true", help="render one snapshot and exit")
    p_watch.add_argument(
        "--no-clear", action="store_true", help="do not clear screen between refreshes"
    )
    p_watch.add_argument(
        "--registered-only", action="store_true", help="hide live unregistered tmux sessions"
    )
    p_watch.add_argument("--filter", default=None, help="only show sessions matching text")
    p_watch.add_argument("--lines", type=int, default=8, help="pane lines to show per session")
    p_watch.add_argument("--interval", type=float, default=2.0, help="refresh interval in seconds")

    p_reg = sub.add_parser("register", help="register a session under a friendly name")
    p_reg.add_argument("name")
    p_reg.add_argument("session")
    p_reg.add_argument("-d", "--description", default=None)
    p_reg.add_argument("-t", "--tags", nargs="*")
    p_reg.add_argument(
        "-m",
        "--manages",
        nargs="*",
        help="other registered names (or session ids) this agent manages — drawn as arrows in `emux web` flow view",
    )
    p_reg.add_argument(
        "-c",
        "--channels",
        nargs="*",
        help="explicit channel names; matcher inference still applies",
    )
    p_reg.add_argument("--linear-issue", help="Linear issue identifier or URL")
    p_reg.add_argument("--linear-project")
    p_reg.add_argument("--linear-team")
    p_reg.add_argument("--acceptance", action="append", help="acceptance criterion; repeatable")

    p_channel = sub.add_parser(
        "channel", help="tiered topic memory over existing registry, logs, and signals"
    )
    channel_sub = p_channel.add_subparsers(dest="channel_cmd", required=True)
    p_channel_list = channel_sub.add_parser("list")
    p_channel_list.add_argument("--json", action="store_true")
    p_channel_create = channel_sub.add_parser("create")
    p_channel_create.add_argument("name")
    p_channel_create.add_argument("--tier", type=int, required=True, choices=range(4))
    p_channel_create.add_argument("--description", required=True)
    p_channel_create.add_argument("--parent")
    p_channel_create.add_argument("--match", dest="matchers", action="append")
    p_channel_create.add_argument("--json", action="store_true")
    p_channel_show = channel_sub.add_parser("show")
    p_channel_show.add_argument("name")
    p_channel_show.add_argument("--json", action="store_true")
    p_channel_suggest = channel_sub.add_parser("suggest")
    p_channel_suggest.add_argument("--json", action="store_true")
    p_channel_refresh = channel_sub.add_parser("refresh")
    p_channel_refresh.add_argument("--json", action="store_true")
    p_channel_note = channel_sub.add_parser("note")
    p_channel_note.add_argument("name")
    p_channel_note.add_argument("kind", choices=sorted(channel_store.NOTE_KINDS))
    p_channel_note.add_argument("text")
    p_channel_note.add_argument("--source")
    p_channel_note.add_argument("--json", action="store_true")

    p_linear = sub.add_parser(
        "linear", help="link workers to Linear contracts and reconcile evidence"
    )
    linear_sub = p_linear.add_subparsers(dest="linear_cmd", required=True)
    p_linear_link = linear_sub.add_parser("link", help="link a registered session to an issue")
    p_linear_link.add_argument("session")
    p_linear_link.add_argument("issue")
    p_linear_link.add_argument("--project")
    p_linear_link.add_argument("--team")
    p_linear_link.add_argument("--acceptance", action="append")
    p_linear_link.add_argument("--json", action="store_true")
    p_linear_evidence = linear_sub.add_parser("evidence", help="record verified acceptance proof")
    p_linear_evidence.add_argument("session")
    p_linear_evidence.add_argument(
        "criterion", type=int, help="1-based acceptance criterion number"
    )
    p_linear_evidence.add_argument("proof")
    p_linear_evidence.add_argument("--source")
    p_linear_evidence.add_argument("--json", action="store_true")
    p_linear_status = linear_sub.add_parser(
        "status", help="reconcile linked workers without writing Linear"
    )
    p_linear_status.add_argument("session", nargs="?")
    p_linear_status.add_argument("--channel")
    p_linear_status.add_argument("--json", action="store_true")

    p_unreg = sub.add_parser("unregister", help="remove a session from the registry")
    p_unreg.add_argument("name")

    p_log = sub.add_parser(
        "log",
        help="print a session's durable character log (complete history, not a pane snapshot)",
    )
    p_log.add_argument("name", help="registered name (or log basename)")
    p_log.add_argument("--lines", type=int, default=None, help="last N lines only")
    p_log.add_argument(
        "--raw",
        action="store_true",
        help="raw stream incl ANSI (for exact replay); default strips ANSI",
    )

    p_send = sub.add_parser("send", help="send keys to a registered session")
    p_send.add_argument("target", help="registered name by default, or tmux session with --session")
    p_send.add_argument("keys", nargs="+", help="tmux keys or literal text to send")
    p_send.add_argument(
        "--no-enter", action="store_true", help="do not append Enter after the keys"
    )
    p_send.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_send.add_argument("--json", action="store_true", help="print structured result JSON")

    p_gate = sub.add_parser("gate", help="observe a live approval gate and print its fingerprint")
    p_gate.add_argument("target", help="registered name by default, or tmux session with --session")
    p_gate.add_argument("--session", action="store_true", help="target a raw tmux session")
    p_gate.add_argument("--json", action="store_true", help="print structured result JSON")

    p_approve = sub.add_parser("approve", help="resolve an exact observed gate once")
    p_approve.add_argument("target", help="registered name by default, or tmux session with --session")
    p_approve.add_argument("--fingerprint", required=True, help="fingerprint returned by `emux gate`")
    p_approve.add_argument("--action", choices=("approve", "reject"), default="approve")
    p_approve.add_argument("--key", help="must be Enter for approve or Escape for reject")
    p_approve.add_argument("--subject", help="immutable operator subject when available")
    p_approve.add_argument("--device", help="operator device identifier when available")
    p_approve.add_argument("--request-id", help="caller request ID (must be unique)")
    p_approve.add_argument("--session", action="store_true", help="target a raw tmux session")
    p_approve.add_argument("--json", action="store_true", help="print structured result JSON")

    p_grant = sub.add_parser("gate-grant", help="answer a gate ONLY if a delegation grant authorizes it (EID-874)")
    p_grant.add_argument("target", help="registered name by default, or tmux session with --session")
    p_grant.add_argument("--identity", required=True, help="who the grant authorizes (e.g. daniel)")
    p_grant.add_argument("--request-id", help="caller request ID (must be unique)")
    p_grant.add_argument("--session", action="store_true", help="target a raw tmux session")
    p_grant.add_argument("--json", action="store_true", help="print structured result JSON")

    p_interrupt = sub.add_parser("interrupt", help="send C-c to a registered session")
    p_interrupt.add_argument(
        "target", help="registered name by default, or tmux session with --session"
    )
    p_interrupt.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_interrupt.add_argument("--json", action="store_true", help="print structured result JSON")

    p_capture = sub.add_parser("capture", help="capture a registered session pane")
    p_capture.add_argument(
        "target", help="registered name by default, or tmux session with --session"
    )
    p_capture.add_argument("--lines", type=int, default=200, help="scrollback lines to capture")
    p_capture.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_capture.add_argument("--json", action="store_true", help="print structured result JSON")

    p_run = sub.add_parser("run", help="send a command, wait, and capture the session")
    p_run.add_argument("target", help="registered name by default, or tmux session with --session")
    p_run.add_argument("command", nargs="+", help="command text to send")
    p_run.add_argument("--wait", type=float, default=2.0, help="seconds to wait before capture")
    p_run.add_argument("--lines", type=int, default=200, help="scrollback lines to capture")
    p_run.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_run.add_argument("--json", action="store_true", help="print structured result JSON")

    p_signal = sub.add_parser(
        "signal",
        help="inject an up-channel signal for THIS session — call from a Claude Code Stop/Notification hook",
    )
    p_signal.add_argument("kind", help="IDLE | READY | DONE | NEED | PROGRESS | ERROR")
    p_signal.add_argument(
        "payload", nargs="?", default="", help="optional text (e.g. what a NEED is blocked on)"
    )
    p_signal.add_argument(
        "--session", help="target session name (default: the current tmux session)"
    )
    p_signal.add_argument(
        "--push",
        metavar="HOST",
        help="also PUSH this signal to HOST's inbox over ssh (the parent); the parent dedups push vs pull by id",
    )

    p_gates = sub.add_parser(
        "gates",
        help="mine the gate ledger into auto-answer policy suggestions (counts, usual answers, ready-to-paste rules)",
    )
    p_gates.add_argument(
        "--limit", type=int, default=20, help="show top N distinct gates (default 20)"
    )

    p_doctor = sub.add_parser(
        "doctor",
        help="diagnose a session's environment: liveness, capture, log, gate, and tmux-server vs fresh-process filesystem access",
    )
    p_doctor.add_argument(
        "target", help="registered name by default, or tmux session with --session"
    )
    p_doctor.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_doctor.add_argument("--json", action="store_true", help="print structured result JSON")

    p_head = sub.add_parser("head", help="open a real terminal head for a registered session")
    p_head.add_argument("target", help="registered name by default, or tmux session with --session")
    p_head.add_argument(
        "--session",
        action="store_true",
        help="target a raw tmux session instead of a registry name",
    )
    p_head.add_argument(
        "--terminal",
        choices=["auto", "iterm", "terminal"],
        default="auto",
        help="terminal app to open",
    )
    p_head.add_argument(
        "--window", action="store_true", help="open a new iTerm window instead of a new tab"
    )
    p_head.add_argument(
        "--print-command",
        action="store_true",
        help="print the tmux attach command without opening a terminal",
    )

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

        return run_web(host=args.host, port=args.port, open_browser=args.open,
                       public_origin=args.public_origin)
    if args.cmd == "new":
        return cmd_new_mission()
    if args.cmd == "ls":
        return cmd_ls()
    if args.cmd == "ask":
        return cmd_ask(args)
    if args.cmd == "navigate":
        return cmd_navigate(args)
    if args.cmd == "goal":
        return cmd_goal(args)
    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "watch":
        return cmd_watch(args)
    if args.cmd == "register":
        return cmd_register(args)
    if args.cmd == "channel":
        return cmd_channel(args)
    if args.cmd == "linear":
        return cmd_linear(args)
    if args.cmd == "unregister":
        return cmd_unregister(args)
    if args.cmd == "log":
        return cmd_log(args)
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "gate":
        return cmd_gate(args)
    if args.cmd == "approve":
        return cmd_approve(args)
    if args.cmd == "gate-grant":
        return cmd_grant_answer(args)
    if args.cmd == "interrupt":
        return cmd_interrupt(args)
    if args.cmd == "capture":
        return cmd_capture(args)
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "head":
        return cmd_head(args)
    if args.cmd == "doctor":
        return cmd_doctor(args)
    if args.cmd == "gates":
        return cmd_gates(args)
    if args.cmd == "signal":
        return cmd_signal(args)

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
