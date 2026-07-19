"""Tests for tmux_send's list-of-keys support and composer submit-verification.

Two live failures drove these: (1) "BSpace BSpace BSpace" as one string was
typed as LITERAL text (tmux treats each argv as one key-or-literal), and
(2) a paste-guarded composer ate the Enter so the message sat unsubmitted
while the send reported ok. Lists send each key separately; literal sends
into an AI pane verify the composer emptied and retry Enter once.
"""

from __future__ import annotations

import asyncio

from emux import server
from emux.server import _composer_text, tmux_send

RULE = "─" * 40


def _screen(composer: str) -> str:
    return f"transcript above\n{RULE}\n❯ {composer}\n{RULE}\n  footer\n"


def test_composer_text_parses_between_last_two_rules():
    assert _composer_text(_screen("draft text")) == "❯ draft text"
    assert _composer_text("no rules here at all\n") is None


class FakeTmux:
    def __init__(self, composer_frames: list[str]):
        self.frames = composer_frames   # composer content per capture, last repeats
        self.i = 0
        self.sent: list[list[str]] = []

    def __call__(self, args, timeout=10, host=None):
        if args[0] == "send-keys":
            self.sent.append(list(args))
            return 0, "", ""
        if args[0] == "capture-pane":
            frame = self.frames[min(self.i, len(self.frames) - 1)]
            self.i += 1
            return 0, _screen(frame), ""
        return 0, "", ""


def _wire(monkeypatch, fake):
    monkeypatch.setattr(server, "_run_tmux", fake)
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_pane_agent", lambda s, h=None: "claude")
    monkeypatch.setattr(server.time, "sleep", lambda s: None)


def test_list_of_keys_sends_each_as_named_key(monkeypatch):
    fake = FakeTmux([""])
    _wire(monkeypatch, fake)
    r = asyncio.run(tmux_send("w1", ["BSpace", "BSpace", "Enter"],
                              enter=False, force=True))
    assert r["ok"]
    assert fake.sent[0][-3:] == ["BSpace", "BSpace", "Enter"]  # separate argv each


def test_swallowed_enter_is_retried_and_reported(monkeypatch):
    text = "please rerun the failing tests now"
    # composer still holds the text after the first Enter, empties after retry
    # blank pre-send gate check, then held composer, then empty after retry
    fake = FakeTmux(["", text, ""])
    _wire(monkeypatch, fake)
    r = asyncio.run(tmux_send("w1", text, enter=True, force=True))
    assert r["ok"] and r.get("resubmitted") is True and r.get("submitted") is True
    enters = [s for s in fake.sent if s[-1] == "Enter" and len(s) == 4]
    assert len(enters) == 2   # the settle-split Enter + the retry Enter


def test_clean_submit_reports_submitted_no_retry(monkeypatch):
    text = "list the open pull requests"
    fake = FakeTmux(["", ""])   # pre-send gate check, then empty verification
    _wire(monkeypatch, fake)
    r = asyncio.run(tmux_send("w1", text, enter=True, force=True))
    assert r["ok"] and r.get("submitted") is True and "resubmitted" not in r


def test_shell_pane_skips_verification(monkeypatch):
    fake = FakeTmux(["echo hello world"])
    _wire(monkeypatch, fake)
    monkeypatch.setattr(server, "_pane_agent", lambda s, h=None: None)  # not an AI pane
    r = asyncio.run(tmux_send("w1", "echo hello world", enter=True, force=True))
    assert r["ok"] and "submitted" not in r   # no composer to verify
