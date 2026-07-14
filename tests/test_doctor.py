"""Tests for `emux doctor` — the environment diagnostician.

The killer case it exists for: a long-running tmux server loses a macOS TCC
grant, every pane EPERMs on the volume while the disk is healthy, and the
session looks mysteriously broken. Doctor compares the tmux server's access
(run-shell) against a fresh process's and names the fix.
"""

from __future__ import annotations

from emux import server
from emux.server import doctor


def _wire(monkeypatch, run_shell_out: str):
    def fake_tmux(args, timeout=10, host=None):
        if args[0] == "has-session":
            return 0, "", ""
        if args[0] == "display-message":
            return 0, "claude|/Volumes/Big/repos/proj\n", ""
        if args[0] == "capture-pane":
            return 0, "some pane output\n", ""
        if args[0] == "run-shell":
            return 0, run_shell_out, ""
        return 0, "", ""
    monkeypatch.setattr(server, "_run_tmux", fake_tmux)
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_pane_agent", lambda s, h=None: "claude")
    monkeypatch.setattr(server, "_log_size", lambda n: 1024)
    monkeypatch.setattr(server.os, "access", lambda p, m: True)  # fresh process reads it


def test_doctor_names_the_tcc_mismatch(monkeypatch):
    _wire(monkeypatch, run_shell_out="DENIED\n")
    r = doctor("wrk")
    assert r["ok"]
    by = {c["check"]: c for c in r["checks"]}
    assert by["fs_tmux_server"]["ok"] is False
    assert by["fs_fresh_process"]["ok"] is True
    assert "restart" in r["diagnosis"].lower() and "tmux" in r["diagnosis"].lower()


def test_doctor_healthy_when_both_read(monkeypatch):
    _wire(monkeypatch, run_shell_out="OK\n")
    r = doctor("wrk")
    by = {c["check"]: c for c in r["checks"]}
    assert by["fs_tmux_server"]["ok"] is True
    assert by["session_live"]["ok"] is True
    assert r["diagnosis"] == "healthy"


def test_doctor_dead_session_short_circuits(monkeypatch):
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_run_tmux",
                        lambda args, timeout=10, host=None: (1, "", "no session"))
    r = doctor("gone")
    assert r["ok"] is False
    assert "clean up" in r["diagnosis"]
