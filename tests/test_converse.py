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
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "Message the agent…")
    called = {"decide": 0}
    monkeypatch.setattr(server, "_decide_step",
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


def test_navigate_escalates_on_stall(monkeypatch):
    """Fast model returns only unknown keys (a stall) → escalate to the stronger
    model, which returns a valid action. The escalated model's keys are sent."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "a menu")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    sent: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: sent.append(args) or (0, "", ""))

    def fake_decide(model, goal, screen, history):
        if model == server._NAV_MODEL_DEFAULT:
            return {"done": False, "text": "", "keys": ["F13"]}  # unknown key -> stall
        return {"done": True, "thought": "escalated saw the goal"}  # sonnet resolves it

    monkeypatch.setattr(server, "_claude_decide", fake_decide)
    out = server.navigate("sess", "goal", max_steps=2)
    assert out["ok"] and out["reached"] == "model_done"


def test_navigate_stalls_when_all_models_fail(monkeypatch):
    """If every model in the chain stalls, navigate reports model_stalled."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "a menu")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "", ""))
    monkeypatch.setattr(server, "_claude_decide",
                        lambda *a, **k: {"done": False, "text": "", "keys": ["F13"]})
    out = server.navigate("sess", "goal", max_steps=2)
    assert not out["ok"] and out["error"] == "model_stalled"


def test_pursue_types_then_declares_done(monkeypatch):
    """Goal loop: model types a message, then next turn declares the goal done."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "prompt >")
    monkeypatch.setattr(server, "_wait_stable", lambda *a, **k: "agent replied: 3 services")
    sent: list[list[str]] = []
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: sent.append(args) or (0, "", ""))

    steps = iter([
        {"action": "type", "text": "list my services", "submit": True, "thought": "ask", "model": server._NAV_MODEL_DEFAULT},
        {"action": "done", "success": True, "summary": "3 services", "model": server._NAV_MODEL_DEFAULT},
    ])
    monkeypatch.setattr(server, "_pursue_decide", lambda *a, **k: next(steps))

    out = server.pursue("sess", "find out how many services I have", max_steps=5)
    assert out["ok"] and out["reached"] == "done" and out["success"] is True
    assert out["summary"] == "3 services"
    # It typed the message and submitted it.
    assert ["send-keys", "-t", "sess", "-l", "list my services"] in sent
    assert ["send-keys", "-t", "sess", "Enter"] in sent


def test_pursue_stall_propagates(monkeypatch):
    """A persistent stall (both the step and its retry) aborts with model_stalled."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "screen")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    monkeypatch.setattr(server, "_pursue_decide", lambda *a, **k: {"stall": "no JSON in model reply"})
    out = server.pursue("sess", "goal", max_steps=3)
    assert not out["ok"] and out["error"] == "model_stalled"


def test_pursue_hits_max_steps(monkeypatch):
    """A model that only ever says 'wait' must terminate at max_steps, not loop.
    ('wait' isn't an active action, so it never trips stuck-detection.)"""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "screen")
    monkeypatch.setattr(server, "_wait_stable", lambda *a, **k: "screen")
    monkeypatch.setattr(server, "_pursue_decide",
                        lambda *a, **k: {"action": "wait", "model": server._NAV_MODEL_DEFAULT})
    out = server.pursue("sess", "goal", max_steps=3)
    assert not out["ok"] and out["error"] == "max_steps_reached"
    assert len(out["steps"]) == 3


def test_pursue_decide_rejects_bad_action(monkeypatch):
    """_pursue_decide stalls on an unknown action name rather than passing it through."""
    from emux import server

    monkeypatch.setattr(server.shutil, "which", lambda _n: "/usr/bin/claude")

    class _P:
        stdout = '{"action": "sudo", "text": "rm"}'
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: _P())
    out = server._pursue_decide(["m"], "goal", "screen", [])
    assert "stall" in out and "bad action" in out["stall"]


def test_pursue_aborts_when_session_gone(monkeypatch):
    """A dropped/killed session (ssh disconnect) is detected, not flailed against."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_session_alive", lambda s: False)
    out = server.pursue("sess", "goal", max_steps=5)
    assert not out["ok"] and out["error"] == "session_gone"


def test_pursue_aborts_on_blank_screen(monkeypatch):
    """Session alive but rendering nothing → blank_screen, not a wasted run."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_session_alive", lambda s: True)
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "")  # always blank
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    out = server.pursue("sess", "goal", max_steps=5)
    assert not out["ok"] and out["error"] == "blank_screen"


def test_pursue_recovers_from_transient_stall(monkeypatch):
    """A one-off stall is retried after a re-observe; the run continues to done."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "a screen")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    decisions = iter([
        {"stall": "no JSON in model reply"},                       # transient
        {"action": "done", "success": True, "summary": "ok", "model": server._NAV_MODEL_DEFAULT},
    ])
    monkeypatch.setattr(server, "_pursue_decide", lambda *a, **k: next(decisions))
    out = server.pursue("sess", "goal", max_steps=5)
    assert out["ok"] and out["reached"] == "done"  # recovered, didn't abort


def test_pursue_detects_stuck_loop(monkeypatch):
    """Repeated actions that never change the screen abort with stuck_no_progress
    instead of silently burning to max_steps."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "frozen screen")
    monkeypatch.setattr(server, "_wait_stable", lambda *a, **k: "frozen screen")  # never changes
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "", ""))
    monkeypatch.setattr(server, "_pursue_decide",
                        lambda *a, **k: {"action": "keys", "keys": ["Down"], "model": server._NAV_MODEL_DEFAULT})
    out = server.pursue("sess", "goal", max_steps=15)
    assert not out["ok"] and out["error"] == "stuck_no_progress"
    assert len(out["steps"]) == server._STUCK_LIMIT  # gave up at the backstop, not max_steps


def test_navigate_aborts_when_session_gone(monkeypatch):
    """navigate detects a dropped session instead of aborting capture_failed."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_session_alive", lambda s: False)
    out = server.navigate("sess", "reach the prompt", max_steps=3)
    assert not out["ok"] and out["error"] == "session_gone"


def test_navigate_recovers_from_transient_stall(monkeypatch):
    """A one-off stall is retried after a re-observe; navigation continues to done."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_observe", lambda s, n, retries=1: "a menu")
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    decisions = iter([
        {"stall": "no JSON in model reply"},                       # transient
        {"done": True, "thought": "there", "model": server._NAV_MODEL_DEFAULT},
    ])
    monkeypatch.setattr(server, "_decide_step", lambda *a, **k: next(decisions))
    out = server.navigate("sess", "goal", max_steps=3)
    assert out["ok"] and out["reached"] == "model_done"


def test_converse_reports_session_gone(monkeypatch):
    """If the session dies mid-wait, converse returns session_gone, not a false settle."""
    from emux import server

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_capture_text", lambda s, n: "before")
    monkeypatch.setattr(server, "_run_tmux", lambda args, timeout=10: (0, "", ""))
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    monkeypatch.setattr(server, "_session_alive", lambda s: False)  # dies immediately
    out = server.converse("sess", "hello", poll_interval=0.1, max_seconds=5)
    assert not out["ok"] and out["error"] == "session_gone"


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
