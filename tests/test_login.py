"""Tests for the login driver (server._login_step / server.login_flow).

_login_step is the pure, testable core: settled screen text in, one action out.
The login_flow tests script a fake tmux (screens advance on Enter) so the whole
loop — /login, method picker, OAuth URL extraction, code paste, verification —
runs with zero real sessions and zero tokens.
"""

from __future__ import annotations

from emux import server
from emux.server import _login_step, login_flow

# --------------------------------------------------------------------------- #
# _login_step — pure decisions
# --------------------------------------------------------------------------- #

def test_fresh_claude_prompt_sends_login():
    assert _login_step("> Try 'fix the bug'\n")["action"] == "send_login"


def test_method_picker_presses_enter():
    screen = "Select login method:\n❯ 1. Claude account with subscription\n"
    assert _login_step(screen, sent_login=True)["action"] == "enter"


def test_oauth_url_is_extracted_even_when_joined_from_wrapped_lines():
    url = "https://claude.ai/oauth/authorize?code=true&client_id=abc&state=xyz"
    screen = f"Browser didn't open? Use the url below to sign in:\n{url}\nPaste code here if prompted > \n"
    step = _login_step(screen, sent_login=True)
    assert step["action"] == "url"
    assert step["url"] == url


def test_success_is_done():
    assert _login_step("Login successful. Press Enter to continue\n")["action"] == "done"


def test_unknown_screen_after_login_sent_is_unknown():
    assert _login_step("some random TUI\n", sent_login=True)["action"] == "unknown"


def test_code_pending_requires_paste_prompt():
    assert _login_step("Paste code here if prompted > \n", code_pending=True)["action"] == "paste"
    assert _login_step("> normal prompt\n", code_pending=True)["action"] == "unknown"


# --------------------------------------------------------------------------- #
# login_flow — the loop against a scripted fake tmux
# --------------------------------------------------------------------------- #

class FakeTmux:
    """Screens advance one step each time Enter is sent."""

    def __init__(self, screens: list[str]):
        self.screens = screens
        self.i = 0
        self.sent: list[list[str]] = []

    def __call__(self, args, timeout=10, host=None):
        if args[0] == "capture-pane":
            return 0, self.screens[min(self.i, len(self.screens) - 1)], ""
        if args[0] == "send-keys":
            self.sent.append(list(args))
            if args[-1] == "Enter":
                self.i = min(self.i + 1, len(self.screens) - 1)
            return 0, "", ""
        return 0, "", ""  # has-session etc.


def _wire(monkeypatch, fake):
    monkeypatch.setattr(server, "_run_tmux", fake)
    monkeypatch.setattr(server, "_pane_agent", lambda s, h=None: "claude")
    monkeypatch.setattr(server.time, "sleep", lambda s: None)


def test_login_flow_start_reaches_oauth_url(monkeypatch):
    url = "https://claude.ai/oauth/authorize?code=true&client_id=abc"
    fake = FakeTmux([
        "> Try 'fix the bug'\n",                                   # /login goes here
        "Select login method:\n❯ 1. Claude account with subscription\n",
        f"Use the url below to sign in:\n{url}\nPaste code here if prompted > \n",
    ])
    _wire(monkeypatch, fake)
    r = login_flow("worker-l1")
    assert r["ok"] is True and r["logged_in"] is False
    assert r["url"] == url
    assert "--code" in r["next"]
    # it typed /login literally (send-keys -l), then advanced with Enter
    assert any("-l" in s and "/login" in s for s in fake.sent)


def test_login_flow_code_finishes_and_verifies(monkeypatch):
    fake = FakeTmux([
        "Paste code here if prompted > \n",
        "Login successful. Press Enter to continue\n",
    ])
    _wire(monkeypatch, fake)
    r = login_flow("worker-l2", code="ac_secret123")
    assert r["ok"] is True and r["logged_in"] is True
    assert any("-l" in s and "ac_secret123" in s for s in fake.sent)


def test_login_flow_code_without_paste_prompt_refuses(monkeypatch):
    fake = FakeTmux(["> just a normal prompt\n"])
    _wire(monkeypatch, fake)
    r = login_flow("worker-l3", code="ac_secret123")
    assert r["ok"] is False and r["error"] == "no_paste_prompt"
    assert not any("ac_secret123" in " ".join(s) for s in fake.sent)  # never typed


def test_login_flow_switch_logs_out_first(monkeypatch):
    url = "https://claude.ai/oauth/authorize?code=true"
    fake = FakeTmux([
        "> \n",
        "Successfully logged out\n> \n",                            # after /logout
        "Select login method:\n❯ 1. Claude account\n",              # after /login
        f"{url}\nPaste code here if prompted > \n",
    ])
    _wire(monkeypatch, fake)
    r = login_flow("worker-l4", switch=True)
    assert r["ok"] is True and r["url"] == url
    typed = [" ".join(s) for s in fake.sent if "-l" in s]
    assert any("/logout" in t for t in typed)
    assert any("/login" in t for t in typed)


def test_login_flow_unrecognized_screen_bails_with_screen(monkeypatch):
    fake = FakeTmux(["> \n", "Something emux has never seen\n"])
    _wire(monkeypatch, fake)
    r = login_flow("worker-l5")
    assert r["ok"] is False and r["error"] == "unrecognized_screen"
    assert "screen" in r
