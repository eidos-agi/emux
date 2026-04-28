"""Smoke tests for tmux-mcp.

Does NOT exercise live tmux operations — those require a running tmux server.
These tests verify the package imports, the MCP server initializes, the
registry round-trips through disk, and the tmux-not-installed path returns a
structured error.
"""

from __future__ import annotations

import asyncio
import json


def test_import():
    import emux
    assert emux.__version__ == "0.1.0"


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

    def fake_run_tmux(args, timeout=10):
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
