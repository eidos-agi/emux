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
