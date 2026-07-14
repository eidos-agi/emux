"""Tests for tmux_wait's idle detection — specifically the false-idle bug.

A session registered after spawn has no armed stream log. The old idle
detector read the missing/empty log as quiet-forever and reported ready-idle
instantly (observed live: 8 running sessions, all "idle" with empty
last_line). These tests fail on that behavior: with no armed log, quiet must
be judged from the live pane, never assumed.
"""

from __future__ import annotations

import asyncio
import itertools

from emux import server
from emux.server import tmux_wait


def _wire(monkeypatch, tmp_path, pane_frames):
    """A registered, LIVE session 'w1' with NO stream log whose pane capture
    yields pane_frames in order (last frame repeats forever)."""
    monkeypatch.setattr(server, "_LOG_DIR", tmp_path)  # empty dir → no logs
    monkeypatch.setattr(server, "_load_registry", lambda: {"w1": {"session": "s1"}})
    monkeypatch.setattr(server, "_session_exists", lambda session, host=None: True)
    frames = itertools.chain(pane_frames, itertools.repeat(pane_frames[-1]))

    def fake_run_tmux(args, timeout=10, host=None):
        assert args[0] == "capture-pane", f"unexpected tmux call: {args}"
        return 0, next(frames), ""

    monkeypatch.setattr(server, "_run_tmux", fake_run_tmux)


def test_no_log_running_session_is_not_idle(monkeypatch, tmp_path):
    # Pane output keeps changing — the session is RUNNING. Old behavior:
    # ready-idle almost instantly with an empty last_line. New: still_working.
    _wire(monkeypatch, tmp_path, [f"working... step {i}\n" for i in range(60)])
    r = asyncio.run(tmux_wait(targets=["w1"], until="idle", timeout=3, quiet=1.0))
    assert r["timed_out"] is True, f"false idle: {r['ready']}"
    assert r["still_working"] == ["w1"]
    assert r["ready"] == []


def test_no_log_stable_pane_is_idle_with_pane_last_line(monkeypatch, tmp_path):
    # Pane genuinely still → idle fires via the pane fallback, and last_line
    # comes from the pane (the old bug reported an empty last_line).
    _wire(monkeypatch, tmp_path, ["$ make test\nAll tests passed\n"])
    r = asyncio.run(tmux_wait(targets=["w1"], until="idle", timeout=10, quiet=1.0))
    assert r["timed_out"] is False
    assert r["ready"][0]["name"] == "w1" and r["ready"][0]["why"] == "idle"
    assert r["ready"][0]["last_line"] == "All tests passed"


def test_armed_log_idle_path_unchanged(monkeypatch, tmp_path):
    # A session WITH an armed log that stops growing still reads idle from the
    # log (the cheap stat path — no pane capture at all).
    monkeypatch.setattr(server, "_LOG_DIR", tmp_path)
    (tmp_path / "w1.log").write_text("compiling\ndone\n")
    monkeypatch.setattr(server, "_load_registry", lambda: {"w1": {"session": "s1"}})
    monkeypatch.setattr(server, "_session_exists", lambda session, host=None: True)

    def no_tmux(args, timeout=10, host=None):
        raise AssertionError(f"log-armed session must not touch tmux: {args}")

    monkeypatch.setattr(server, "_run_tmux", no_tmux)
    r = asyncio.run(tmux_wait(targets=["w1"], until="idle", timeout=10, quiet=1.0))
    assert r["timed_out"] is False
    assert r["ready"][0]["why"] == "idle"
    assert r["ready"][0]["last_line"] == "done"
