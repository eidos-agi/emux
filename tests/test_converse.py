"""Tests for the converse primitive (talk to an AI through its TUI)."""

import shutil
import subprocess
import time

import pytest

from emux.server import _reply_delta, _strip_ansi, converse


def test_reply_delta_returns_added_lines():
    before = "a\nb\nc"
    after = "a\nb\nc\nhello\nworld"
    assert _reply_delta(before, after) == "hello\nworld"


def test_reply_delta_ignores_blank_lines():
    assert _reply_delta("x", "x\n\n   \ny") == "y"


def test_strip_ansi():
    assert _strip_ansi("\x1b[38;5;180mkai\x1b[0m ready") == "kai ready"


def test_navigate_stops_on_until_without_model(monkeypatch):
    """If `until` is already on screen, navigate returns immediately — no model
    call, no keystrokes."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "Message the agent…")
    called = {"decide": 0}
    monkeypatch.setattr(server, "_claude_decide",
                        lambda *a, **k: called.__setitem__("decide", called["decide"] + 1) or {"done": True})
    out = server.navigate("sess", "reach the prompt", until="Message the agent")
    assert out["ok"] and out["reached"] == "until"
    assert called["decide"] == 0  # short-circuited before any model call


def test_navigate_drives_keys_until_done(monkeypatch):
    """Model says Down/Enter once, then done — navigate should send those keys."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "a menu")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    sent: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: sent.append(args) or (0, "", ""))

    decisions = iter([
        {"thought": "pick 2nd", "done": False, "text": "", "keys": ["Down", "Enter"]},
        {"thought": "there", "done": True, "keys": []},
    ])
    monkeypatch.setattr(server, "_claude_decide", lambda *a, **k: next(decisions))

    out = server.navigate("sess", "reach the prompt", max_steps=5)
    assert out["ok"] and out["reached"] == "model_done"
    # Down and Enter were each sent as separate send-keys calls.
    assert ["send-keys", "-t", "sess", "Down"] in sent
    assert ["send-keys", "-t", "sess", "Enter"] in sent


def test_navigate_rejects_unknown_keys(monkeypatch):
    """Keys not in the allowlist are dropped; a step with no valid action errors."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "a menu")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "", ""))
    monkeypatch.setattr(server, "_claude_decide",
                        lambda *a, **k: {"done": False, "text": "", "keys": ["F13", "Meta-x"]})
    out = server.navigate("sess", "goal", max_steps=2)
    assert not out["ok"] and out["error"] == "model_returned_no_action"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux required")
def test_converse_against_cat_echo():
    """`cat` echoes stdin — a deterministic stand-in for a TUI AI. converse
    should send the prompt, wait for the echo to settle, and see it on screen."""
    sess = "emux_test_cat"
    subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", sess, "cat"], check=True)
    try:
        time.sleep(0.5)
        r = converse(sess, "ping-emux-42", settle_seconds=1.0, poll_interval=0.5, max_seconds=10)
        assert r["ok"], r
        assert r["settled"]
        assert "ping-emux-42" in r["screen"]
        assert "ping-emux-42" in r["reply"]
    finally:
        subprocess.run(["tmux", "kill-session", "-t", sess], capture_output=True)
