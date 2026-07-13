"""Smoke tests for tmux-mcp.

Does NOT exercise live tmux operations — those require a running tmux server.
These tests verify the package imports, the MCP server initializes, the
registry round-trips through disk, and the tmux-not-installed path returns a
structured error.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import uuid

import pytest


def test_import():
    import re

    import emux
    # Format check, not an exact pin — so a version bump never breaks the suite.
    assert re.fullmatch(r"\d+\.\d+\.\d+", emux.__version__)


def test_server_module_loads():
    from emux import server
    assert server.mcp is not None
    assert server.mcp.name == "emux"


def test_resolve_tmux_returns_string_or_none():
    from emux.server import _resolve_tmux
    result = _resolve_tmux()
    assert result is None or isinstance(result, str)


def test_registry_round_trip(tmp_path, monkeypatch):
    """Registry persists through disk and reloads correctly."""
    from emux import server
    registry_path = tmp_path / "registry.json"
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)

    registry = {
        "claude-prod": {
            "session": "main",
            "description": "production claude session",
            "tags": ["prod", "claude"],
            "registered_at": 1700000000,
        }
    }
    server._save_registry(registry)
    assert registry_path.exists()

    loaded = server._load_registry()
    assert loaded == registry


def test_spawn_passes_manages_edge_to_registry(tmp_path, monkeypatch):
    """tmux_spawn records the manager→sub edge so the nested-manager tree is
    declarable at spawn time (regression: spawn used to drop `manages`)."""
    from emux import server
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    # No live tmux in this suite — fake the tmux calls and liveness.
    monkeypatch.setattr(server, "_run_tmux", lambda *a, **k: (0, "", ""))
    monkeypatch.setattr(server, "_session_exists", lambda *a, **k: False)

    asyncio.run(server.tmux_spawn(name="mgr", manages=["sub"]))
    entry = server._load_registry()["mgr"]
    assert entry["manages"] == ["sub"], entry


def test_a_tool_call_is_audited(tmp_path, monkeypatch):
    """Every emux tool call appends one line to the audit trail: op, salient
    args, outcome — the per-call record for rebooting/understanding old jobs."""
    from emux import server
    monkeypatch.setattr(server, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "reg.json")
    asyncio.run(server.tmux_search(query="zzz"))
    lines = (tmp_path / "audit.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["op"] == "tmux_search" and rec["query"] == "zzz" and rec["ok"] is True


def test_load_registry_returns_empty_when_missing(tmp_path, monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "does-not-exist.json")
    assert server._load_registry() == {}


def test_load_registry_handles_corrupt_file(tmp_path, monkeypatch):
    from emux import server
    bad = tmp_path / "registry.json"
    bad.write_text("this is not json")
    monkeypatch.setattr(server, "REGISTRY_PATH", bad)
    assert server._load_registry() == {}


def test_tmux_sessions_handles_missing_tmux(monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)
    result = asyncio.run(server.tmux_sessions())
    assert result["ok"] is False
    assert result["error"] == "tmux_not_installed"
    assert "hint" in result


def test_tmux_send_handles_missing_tmux(monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)
    result = asyncio.run(server.tmux_send(target="nope", keys="echo hi"))
    assert result["ok"] is False
    assert result["error"] == "tmux_not_installed"


def test_tmux_capture_handles_missing_tmux(monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)
    result = asyncio.run(server.tmux_capture(target="nope"))
    assert result["ok"] is False
    assert result["error"] == "tmux_not_installed"


def test_register_and_unregister_round_trip(tmp_path, monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(server, "_live_sessions", lambda: [])

    reg = asyncio.run(server.tmux_register(
        name="alpha", session="actual-tmux-name", description="test", tags=["t1"]
    ))
    assert reg["ok"]
    assert reg["entry"]["session"] == "actual-tmux-name"
    assert reg["session_live"] is False  # we mocked _live_sessions to []

    loaded = server._load_registry()
    assert "alpha" in loaded

    unreg = asyncio.run(server.tmux_unregister("alpha"))
    assert unreg["ok"]
    assert unreg["removed_entry"]["session"] == "actual-tmux-name"

    assert server._load_registry() == {}


def test_unregister_unknown_returns_error(tmp_path, monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    result = asyncio.run(server.tmux_unregister("never-registered"))
    assert result["ok"] is False
    assert result["error"] == "not_registered"


def test_send_by_registry_name_resolves(tmp_path, monkeypatch):
    """tmux_send with by_registry_name=True looks up the underlying session."""
    from emux import server
    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "alpha": {"session": "real-session-x", "description": None, "tags": [], "registered_at": 0}
    }))
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")

    captured_args: list[list[str]] = []

    def fake_run_tmux(args, timeout=10, host=None):
        captured_args.append(args)
        return (0, "", "")

    monkeypatch.setattr(server, "_run_tmux", fake_run_tmux)
    result = asyncio.run(server.tmux_send(target="alpha", keys="echo hi", by_registry_name=True))
    assert result["ok"]
    assert result["resolved_session"] == "real-session-x"
    assert captured_args[0] == ["send-keys", "-t", "real-session-x", "echo hi", "Enter"]


def test_send_by_registry_name_unknown_returns_error(tmp_path, monkeypatch):
    from emux import server
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    result = asyncio.run(server.tmux_send(target="not-here", keys="x", by_registry_name=True))
    assert result["ok"] is False
    assert result["error"] == "not_registered"


def test_build_groups_orders_registered_live_stale_unregistered(monkeypatch):
    from emux import tui

    registry = {
        "old-live": {
            "session": "tmux-live-old",
            "description": "older live",
            "tags": ["old"],
            "registered_at": 100,
        },
        "new-live": {
            "session": "tmux-live-new",
            "description": "newer live",
            "tags": ["new"],
            "registered_at": 200,
        },
        "gone": {
            "session": "tmux-gone",
            "description": "missing session",
            "tags": ["stale"],
            "registered_at": 300,
        },
    }
    live = [
        {"name": "tmux-live-old", "windows": 1, "created_unix": 10, "attached": False},
        {"name": "tmux-live-new", "windows": 2, "created_unix": 20, "attached": True},
        {"name": "scratch", "windows": 1, "created_unix": 30, "attached": False},
    ]
    monkeypatch.setattr(tui, "_load_registry", lambda: registry)
    monkeypatch.setattr(tui, "_live_sessions", lambda: live)

    groups = tui._build_groups()

    assert [item["name"] for item in groups["registered_live"]] == ["new-live", "old-live"]
    assert groups["registered_live"][0]["is_stale"] is False
    assert [item["name"] for item in groups["registered_stale"]] == ["gone"]
    assert groups["registered_stale"][0]["is_stale"] is True
    assert [item["session"] for item in groups["unregistered_live"]] == ["scratch"]
    assert groups["actions"][0]["kind"] == "register_new"


def test_tmux_sessions_marks_registered_stale(tmp_path, monkeypatch):
    from emux import server

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "live-name": {
            "session": "live-session",
            "description": None,
            "tags": [],
            "registered_at": 1,
        },
        "stale-name": {
            "session": "gone-session",
            "description": None,
            "tags": [],
            "registered_at": 2,
        },
    }))
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_live_sessions", lambda host=None: [
        {"name": "live-session", "windows": 1, "created_unix": 10, "attached": False}
    ])

    result = asyncio.run(server.tmux_sessions())

    assert result["ok"] is True
    assert result["registry"]["live-name"]["stale"] is False
    assert result["registry"]["stale-name"]["stale"] is True


def test_tmux_capture_by_registry_name_success(tmp_path, monkeypatch):
    from emux import server

    registry_path = tmp_path / "registry.json"
    registry_path.write_text(json.dumps({
        "alpha": {"session": "real-session", "description": None, "tags": [], "registered_at": 0}
    }))
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")

    captured_args: list[list[str]] = []

    def fake_run_tmux(args, timeout=10, host=None):
        captured_args.append(args)
        return (0, "hello\nworld\n", "")

    monkeypatch.setattr(server, "_run_tmux", fake_run_tmux)
    result = asyncio.run(server.tmux_capture(target="alpha", lines=20, by_registry_name=True))

    assert result["ok"] is True
    assert result["resolved_session"] == "real-session"
    assert result["content"] == "hello\nworld\n"
    assert result["lines_captured"] == 2
    assert captured_args[0] == ["capture-pane", "-t", "real-session", "-p", "-S", "-20"]


def test_tmux_run_returns_capture_content(monkeypatch):
    from emux import server

    async def fake_send(**kwargs):
        return {"ok": True, "resolved_session": "real-session"}

    async def fake_capture(**kwargs):
        return {"ok": True, "content": "EMUX_OK\n", "lines_captured": 1}

    monkeypatch.setattr(server, "tmux_send", fake_send)
    monkeypatch.setattr(server, "tmux_capture", fake_capture)

    result = asyncio.run(server.tmux_run("alpha", "printf EMUX_OK", wait_seconds=0))

    assert result["ok"] is True
    assert result["resolved_session"] == "real-session"
    assert result["content"] == "EMUX_OK\n"


def test_tmux_run_reports_send_failure(monkeypatch):
    from emux import server

    async def fake_send(**kwargs):
        return {"ok": False, "error": "tmux_send_failed"}

    monkeypatch.setattr(server, "tmux_send", fake_send)

    result = asyncio.run(server.tmux_run("alpha", "printf EMUX_OK", wait_seconds=0))

    assert result["ok"] is False
    assert result["stage"] == "send"
    assert result["send_result"]["error"] == "tmux_send_failed"


def test_cmd_ls_reports_registered_live_and_stale(monkeypatch, capsys):
    from emux import cli

    monkeypatch.setattr(cli, "_load_registry", lambda: {
        "alpha": {"session": "live-session", "description": "active shell", "tags": []},
        "beta": {"session": "gone-session", "description": "old shell", "tags": []},
    })
    monkeypatch.setattr(cli, "_live_sessions", lambda: [
        {"name": "live-session", "windows": 1, "created_unix": 10, "attached": False},
        {"name": "scratch", "windows": 1, "created_unix": 20, "attached": True},
    ])

    assert cli.cmd_ls() == 0
    out = capsys.readouterr().out

    assert "alpha → live-session — active shell" in out
    assert "beta → gone-session STALE — old shell" in out
    assert "live-session (registered)" in out
    assert "scratch (attached)" in out


def test_watch_targets_include_registered_stale_and_unregistered_live():
    from emux import cli

    targets = cli._watch_targets(
        registry={
            "alpha": {"session": "live-session", "description": "active shell", "tags": ["claude"]},
            "beta": {"session": "gone-session", "description": "old shell", "tags": []},
        },
        live=[
            {"name": "live-session", "windows": 1, "created_unix": 10, "attached": False},
            {"name": "scratch", "windows": 1, "created_unix": 20, "attached": True},
        ],
    )

    assert [(t["kind"], t["name"], t["session"], t["live"]) for t in targets] == [
        ("registered", "alpha", "live-session", True),
        ("registered", "beta", "gone-session", False),
        ("live", "scratch", "scratch", True),
    ]


def test_watch_targets_filter_and_registered_only():
    from emux import cli

    targets = cli._watch_targets(
        registry={
            "alpha": {"session": "live-session", "description": "Claude Code", "tags": ["claude"]},
            "beta": {"session": "gone-session", "description": "old shell", "tags": []},
        },
        live=[
            {"name": "live-session", "windows": 1, "created_unix": 10, "attached": False},
            {"name": "scratch", "windows": 1, "created_unix": 20, "attached": True},
        ],
        registered_only=True,
        needle="claude",
    )

    assert [t["name"] for t in targets] == ["alpha"]


def test_render_watch_snapshot_shows_captures_and_stale():
    from datetime import datetime

    from emux import cli

    rendered = cli._render_watch_snapshot(
        targets=[
            {
                "kind": "registered",
                "name": "alpha",
                "session": "live-session",
                "description": "active shell",
                "tags": [],
                "live": True,
            },
            {
                "kind": "registered",
                "name": "beta",
                "session": "gone-session",
                "description": None,
                "tags": [],
                "live": False,
            },
        ],
        captures={"live-session": (True, "line one\nline two")},
        lines=2,
        now=datetime(2026, 5, 31, 12, 0, 0),
    )

    assert "emux watch  2026-05-31 12:00:00" in rendered
    assert "=== alpha -> live-session [registered; live] — active shell" in rendered
    assert "    line one" in rendered
    assert "    line two" in rendered
    assert "=== beta -> gone-session [registered; STALE]" in rendered
    assert "tmux session is gone" in rendered


def test_capture_session_ignores_trailing_blank_pane_rows(monkeypatch):
    from emux import cli

    def fake_run_tmux(args, timeout=10, host=None):
        return (0, "old\nuseful one\nuseful two\n\n\n", "")

    monkeypatch.setattr(cli, "_run_tmux", fake_run_tmux)

    ok, content = cli._capture_session("alpha", lines=2)

    assert ok is True
    assert content == "useful one\nuseful two"


def test_cmd_send_targets_registry_name_by_default(monkeypatch, capsys):
    import argparse

    from emux import cli

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "target": kwargs["target"], "resolved_session": "real-session"}

    monkeypatch.setattr(cli, "tmux_send", fake_send)

    rc = cli.cmd_send(argparse.Namespace(
        target="alpha",
        keys=["echo", "hi"],
        no_enter=False,
        session=False,
        json=False,
    ))

    assert rc == 0
    assert calls == [{
        "target": "alpha",
        "keys": "echo hi",
        "enter": True,
        "by_registry_name": True,
    }]
    assert "ok: alpha -> real-session" in capsys.readouterr().out


def test_cmd_interrupt_sends_control_c_without_enter(monkeypatch):
    import argparse

    from emux import cli

    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "target": kwargs["target"], "resolved_session": "real-session"}

    monkeypatch.setattr(cli, "tmux_send", fake_send)

    rc = cli.cmd_interrupt(argparse.Namespace(target="alpha", session=False, json=False))

    assert rc == 0
    assert calls == [{
        "target": "alpha",
        "keys": "C-c",
        "enter": False,
        "by_registry_name": True,
    }]


def test_cmd_capture_prints_content(monkeypatch, capsys):
    import argparse

    from emux import cli

    async def fake_capture(**kwargs):
        return {"ok": True, "content": "line one\nline two\n"}

    monkeypatch.setattr(cli, "tmux_capture", fake_capture)

    rc = cli.cmd_capture(argparse.Namespace(target="alpha", lines=2, session=False, json=False))

    assert rc == 0
    assert capsys.readouterr().out == "line one\nline two\n"


def test_cmd_run_prints_content_and_supports_raw_session(monkeypatch, capsys):
    import argparse

    from emux import cli

    calls = []

    async def fake_run(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "content": "DONE\n"}

    monkeypatch.setattr(cli, "tmux_run", fake_run)

    rc = cli.cmd_run(argparse.Namespace(
        target="raw-session",
        command=["printf", "DONE"],
        wait=0.1,
        lines=5,
        session=True,
        json=False,
    ))

    assert rc == 0
    assert calls == [{
        "target": "raw-session",
        "command": "printf DONE",
        "wait_seconds": 0.1,
        "capture_lines": 5,
        "by_registry_name": False,
    }]
    assert capsys.readouterr().out == "DONE\n"


def test_resolve_session_target_registry_live(monkeypatch):
    from emux import cli

    monkeypatch.setattr(cli, "_load_registry", lambda: {
        "alpha": {"session": "real-session", "description": None, "tags": []}
    })
    monkeypatch.setattr(cli, "_live_sessions", lambda: [
        {"name": "real-session", "windows": 1, "created_unix": 1, "attached": False}
    ])

    ok, session, err = cli._resolve_session_target("alpha", by_registry_name=True)

    assert ok is True
    assert session == "real-session"
    assert err is None


def test_resolve_session_target_rejects_stale_registry(monkeypatch):
    from emux import cli

    monkeypatch.setattr(cli, "_load_registry", lambda: {
        "alpha": {"session": "gone-session", "description": None, "tags": []}
    })
    monkeypatch.setattr(cli, "_live_sessions", lambda: [])

    ok, session, err = cli._resolve_session_target("alpha", by_registry_name=True)

    assert ok is False
    assert session == "gone-session"
    assert "not live" in err


def test_cmd_head_print_command_resolves_registry(monkeypatch, capsys):
    import argparse

    from emux import cli

    monkeypatch.setattr(cli, "_load_registry", lambda: {
        "alpha": {"session": "real-session", "description": None, "tags": []}
    })
    monkeypatch.setattr(cli, "_live_sessions", lambda: [
        {"name": "real-session", "windows": 1, "created_unix": 1, "attached": False}
    ])

    rc = cli.cmd_head(argparse.Namespace(
        target="alpha",
        session=False,
        terminal="auto",
        window=False,
        print_command=True,
    ))

    assert rc == 0
    assert capsys.readouterr().out == "tmux attach -t real-session\n"


def test_cmd_head_opens_iterm_for_raw_session(monkeypatch, capsys):
    import argparse

    from emux import cli

    calls = []

    monkeypatch.setattr(cli, "_live_sessions", lambda: [
        {"name": "raw-session", "windows": 1, "created_unix": 1, "attached": False}
    ])

    def fake_open(session, terminal="auto", new_window=False):
        calls.append((session, terminal, new_window))
        return True, "iTerm", None

    monkeypatch.setattr(cli, "_open_terminal_head", fake_open)

    rc = cli.cmd_head(argparse.Namespace(
        target="raw-session",
        session=True,
        terminal="iterm",
        window=True,
        print_command=False,
    ))

    assert rc == 0
    assert calls == [("raw-session", "iterm", True)]
    assert "opened iTerm head for raw-session -> raw-session" in capsys.readouterr().out


def test_open_iterm_head_builds_command_file(monkeypatch, tmp_path):
    from emux import cli

    calls = []

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(args, capture_output=True, text=True, check=False):
        calls.append(args)
        if args[:2] == ["osascript", "-e"] and args[2] == 'id of application "iTerm2"':
            return Result(0, "com.googlecode.iterm2\n", "")
        if args[:3] == ["open", "-b", "com.googlecode.iterm2"]:
            return Result(0, "", "")
        return Result(1, "", "unexpected")

    monkeypatch.setattr(cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"osascript", "open"} else None)
    monkeypatch.setattr(cli.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    ok, err = cli._open_iterm_head("session with space", new_window=False)

    assert ok is True
    assert err is None
    command_files = list(tmp_path.glob("emux-head-*-session-with-space.command"))
    assert len(command_files) == 1
    assert "exec tmux attach -t 'session with space'" in command_files[0].read_text()
    assert any(call[:3] == ["open", "-b", "com.googlecode.iterm2"] for call in calls)


def test_open_terminal_head_auto_falls_back_to_terminal(monkeypatch):
    from emux import cli

    calls = []

    def fake_iterm(session, new_window=False):
        calls.append(("iterm", session, new_window))
        return False, "iTerm timeout"

    def fake_terminal(session):
        calls.append(("terminal", session))
        return True, None

    monkeypatch.setattr(cli, "_open_iterm_head", fake_iterm)
    monkeypatch.setattr(cli, "_open_terminal_app_head", fake_terminal)

    ok, app, err = cli._open_terminal_head("alpha", terminal="auto", new_window=True)

    assert ok is True
    assert app == "Terminal"
    assert err is None
    assert calls == [("iterm", "alpha", True), ("terminal", "alpha")]


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


@pytest.mark.skipif(not _tmux_available(), reason="tmux is not installed")
def test_real_tmux_register_run_capture(tmp_path, monkeypatch):
    from emux import server

    session = f"emux-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "sh"], check=True)
    try:
        reg = asyncio.run(server.tmux_register(
            "integration",
            session,
            "real tmux integration test",
            ["test"],
        ))
        assert reg["ok"] is True
        assert reg["session_live"] is True

        result = asyncio.run(server.tmux_run(
            "integration",
            "printf EMUX_TMUX_OK",
            wait_seconds=0.5,
            capture_lines=20,
            by_registry_name=True,
        ))

        assert result["ok"] is True
        assert "EMUX_TMUX_OK" in result["content"]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)


@pytest.mark.skipif(
    not _tmux_available() or shutil.which("claude") is None,
    reason="tmux and Claude Code CLI are required for the local Claude smoke",
)
def test_local_claude_code_version_through_registered_tmux(tmp_path, monkeypatch):
    from emux import server

    session = f"emux-claude-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    subprocess.run(["tmux", "new-session", "-d", "-s", session, "sh"], check=True)
    try:
        reg = asyncio.run(server.tmux_register(
            "claude-code",
            session,
            "local Claude Code smoke",
            ["claude", "local"],
        ))
        assert reg["ok"] is True

        result = asyncio.run(server.tmux_run(
            "claude-code",
            "claude --version",
            wait_seconds=0.75,
            capture_lines=30,
            by_registry_name=True,
        ))

        assert result["ok"] is True
        assert "Claude Code" in result["content"]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)
