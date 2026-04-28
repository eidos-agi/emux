"""Smoke tests for claude-runner.

Does NOT exercise the actual `claude` CLI — that requires Claude Code to be
installed and authenticated, which we can't assume in CI. These tests
verify that the package imports, the MCP server initializes, and the
subprocess wrapper handles the missing-binary case gracefully.
"""

from __future__ import annotations

import asyncio


def test_import():
    import claude_runner
    assert claude_runner.__version__ == "0.1.0"


def test_server_module_loads():
    from claude_runner import server
    assert server.mcp is not None
    assert server.mcp.name == "claude-runner"


def test_resolve_claude_returns_string_or_none():
    from claude_runner.server import _resolve_claude
    result = _resolve_claude()
    assert result is None or isinstance(result, str)


def test_run_subprocess_handles_missing_command():
    from claude_runner.server import _run_subprocess
    result = _run_subprocess(
        ["this-binary-does-not-exist-xyzzy"],
        timeout=5,
        cwd=None,
    )
    assert result["ok"] is False
    assert result["error"] == "command_not_found"
    assert "hint" in result


def test_run_subprocess_captures_zero_exit():
    from claude_runner.server import _run_subprocess
    result = _run_subprocess(["true"], timeout=5, cwd=None)
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert "elapsed_seconds" in result


def test_run_subprocess_captures_nonzero_exit():
    from claude_runner.server import _run_subprocess
    result = _run_subprocess(["false"], timeout=5, cwd=None)
    assert result["ok"] is False
    assert result["returncode"] == 1


def test_claude_run_handles_missing_claude_gracefully(monkeypatch):
    """If `claude` is not on PATH, the tool returns a structured error."""
    from claude_runner import server
    monkeypatch.setattr(server, "_resolve_claude", lambda: None)
    result = asyncio.run(server.claude_run(args=["--version"]))
    assert result["ok"] is False
    assert result["error"] == "command_not_found"
