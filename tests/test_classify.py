"""Tests for the deterministic Tier-0 state classifier (emux.judge).

Each case feeds a synthetic capture window (and, where the state depends on
change-over-time, a synthetic activity sample list) and asserts the ONE state /
flag it is designed to produce. They are falsifiable: a regex or threshold drift
that changes the label will trip exactly one of these.
"""

from __future__ import annotations

from emux.judge import classify

# --- small builders so the intent of each case is obvious ---

def _samples(norms, changed):
    """Build an activity list: one {'norm','changed'} dict per frame."""
    return [{"norm": n, "changed": c} for n, c in zip(norms, changed, strict=False)]


def _meta(**kw):
    base = {"live": True, "last_change_age": 1.0, "agent": "claude",
            "pane_command": "claude", "attached": False}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# states
# --------------------------------------------------------------------------- #

def test_traceback_is_error():
    cap = (
        "Running the migration...\n"
        'Traceback (most recent call last):\n'
        '  File "run.py", line 42, in <module>\n'
        "    main()\n"
        "ZeroDivisionError: division by zero\n"
    )
    r = classify(cap, [], _meta())
    assert r["state"] == "error"
    assert "traceback" in r["evidence"].lower()
    assert r["confidence"] >= 0.4
    assert r["recommended_action"]


def test_failing_tests_is_error_with_count():
    cap = "collected 12 items\n\n3 failed, 9 passed in 4.21s\n"
    r = classify(cap, [], _meta(pane_command="python", agent="python"))
    assert r["state"] == "error"
    assert "3 test(s) failing" in r["evidence"]


def test_fatal_startup_banner_as_last_line_is_error_needs_reseed():
    """EID-1172: `claude --continue` with no conversation leaves its fatal
    banner as the last line and an otherwise quiet screen — this seat is dead
    and must classify as error/needs_reseed, never healthy/idle."""
    cap = "$ claude --continue\nNo conversation found to continue\n"
    r = classify(cap, [], _meta(last_change_age=600.0))
    assert r["state"] == "error"
    assert "needs_reseed" in r["flags"]
    assert "fatal startup banner" in r["evidence"]


def test_quoted_fatal_phrase_above_last_line_does_not_trip_reseed():
    """A healthy chat QUOTING the phrase (with the composer below it) must not
    classify as dead."""
    cap = (
        "the seat was dead-looping on 'No conversation found to continue'\n"
        "fixed in ensure-engine\n"
        "❯ \n"
    )
    r = classify(cap, [], _meta())
    assert "needs_reseed" not in r["flags"]
    assert r["state"] != "error"


def test_shell_prompt_after_success_is_done_idle():
    cap = (
        "pytest -q\n"
        "................\n"
        "All tests passed\n"
        "user@box project % \n"
    )
    # not active: last change was a while ago, at a shell prompt
    r = classify(cap, [], _meta(agent="shell", pane_command="zsh",
                                last_change_age=30.0))
    assert r["state"] == "done_idle"
    assert "success" in r["summary"].lower() or "success" in r["evidence"].lower()


def test_approval_prompt_is_waiting_human():
    cap = (
        "About to edit config.yaml\n"
        "Do you want to proceed?\n"
        "❯ 1. Yes\n"
        "  2. No\n"
    )
    r = classify(cap, [], _meta())
    assert r["state"] == "waiting_human"


def test_destructive_confirm_flags_dangerous_blocked():
    cap = (
        "This will delete the production database.\n"
        "Run: rm -rf /var/lib/pg\n"
        "Are you sure? [y/N]\n"
    )
    r = classify(cap, [], _meta())
    assert r["state"] == "waiting_human"
    assert "dangerous_blocked" in r["flags"]


def test_hidden_wait_flag_when_gate_up_and_nobody_attached():
    cap = "Enter verification code:\n"
    # no recent change, not attached ⇒ a gate nobody is answering
    r = classify(cap, _samples(["Enter verification code:"], [False]),
                 _meta(attached=False, last_change_age=120.0))
    assert r["state"] == "waiting_human"
    assert "hidden_wait" in r["flags"]


def test_repeated_command_same_traceback_is_thrashing():
    # Same command re-run, same failure, screen barely changes ⇒ going in circles.
    win = (
        "❯ pytest tests/test_auth.py\n"
        "E   AssertionError: token mismatch\n"
        "1 failed in 0.30s\n"
    )
    win2 = win.replace("0.30s", "0.31s")  # only the timer moved
    norms = [win, win2, win, win2, win]
    r = classify(win, _samples(norms, [True, True, True, True, True]),
                 _meta(last_change_age=1.0))
    assert r["state"] == "thrashing"
    assert "token_waste" in r["flags"]  # AI agent + no artifact progress


def test_identical_frozen_windows_no_diff_is_stuck():
    frame = "Compiling module 7 of 40...\n"
    norms = [frame] * 6
    r = classify(frame, _samples(norms, [False] * 6),
                 _meta(agent="node", pane_command="node", last_change_age=90.0))
    assert r["state"] == "stuck"


def test_spinner_only_activity_flags_false_busy():
    # A braille spinner is animating but nothing meaningful changed.
    cap = "Thinking ⠋\n"
    norms = ["Thinking", "Thinking", "Thinking"]
    r = classify(cap, _samples(norms, [False, False, False]),
                 _meta(last_change_age=20.0))
    assert "false_busy" in r["flags"]


def test_external_command_is_waiting_external():
    cap = (
        "$ git push origin main\n"
        "Enumerating objects: 12, done.\n"
        "Writing objects:  40% (5/12)\n"
    )
    r = classify(cap, [], _meta(agent="git", pane_command="git",
                                last_change_age=2.0))
    assert r["state"] == "waiting_external"


def test_editor_foreground_is_editing():
    cap = "1 def main():\n2     pass\n~\n~\n"
    r = classify(cap, [], _meta(agent="editor", pane_command="nvim",
                                last_change_age=1.0))
    assert r["state"] == "editing"


def test_ai_plan_actively_streaming_is_planning():
    cap = (
        "Here's my plan:\n"
        "1. Read the config loader\n"
        "2. Add the missing field\n"
        "3. Run the tests\n"
    )
    r = classify(cap, _samples(["a", "b"], [True, True]),
                 _meta(agent="claude", pane_command="claude", last_change_age=1.0))
    assert r["state"] == "planning"


def test_active_generation_is_running():
    cap = "Editing the parser... (12s · esc to interrupt)\n"
    r = classify(cap, _samples(["x", "y"], [True, True]),
                 _meta(agent="claude", pane_command="claude", last_change_age=1.0))
    assert r["state"] == "running"


def test_dead_session_short_circuits():
    r = classify("anything at all", [{"norm": "x", "changed": True}],
                 {"live": False})
    assert r["state"] == "dead"
    assert r["confidence"] == 1.0
    assert r["flags"] == []


def test_quota_text_flags_possible_exhaustion():
    cap = "Error: rate limit exceeded, retrying in 30s\ncontext window exceeded\n"
    r = classify(cap, [], _meta(last_change_age=2.0))
    assert "possible_exhaustion" in r["flags"]


def test_claude_session_limit_is_waiting_external_not_stuck():
    cap = (
        "You've hit your session limit · resets 4pm (America/Los_Angeles)\n"
        "/usage-credits to finish what you’re working on.\n"
        "❯ \n"
    )
    r = classify(
        cap,
        _samples([cap, cap], [False, False]),
        _meta(agent="claude", pane_command="claude", last_change_age=2915.0),
    )
    assert r["state"] == "waiting_external"
    assert "possible_exhaustion" in r["flags"]
    assert "quota" in r["summary"].lower()


def test_login_method_picker_is_login_gate():
    cap = (
        "Select login method:\n"
        "❯ 1. Claude account with subscription\n"
        "  2. Anthropic Console account\n"
    )
    r = classify(cap, [], _meta())
    assert r["state"] == "waiting_human"
    assert "login_gate" in r["flags"]
    assert "login" in r["summary"].lower()


def test_logged_out_banner_is_login_gate():
    cap = "Invalid API key · Please run /login\n> \n"
    r = classify(cap, [], _meta())
    assert r["state"] == "waiting_human"
    assert "login_gate" in r["flags"]


def test_oauth_paste_prompt_is_login_gate():
    cap = (
        "Browser didn't open? Use the url below to sign in:\n"
        "https://claude.ai/oauth/authorize?code=true&client_id=xyz\n"
        "Paste code here if prompted > \n"
    )
    r = classify(cap, [], _meta())
    assert r["state"] == "waiting_human"
    assert "login_gate" in r["flags"]


def test_login_successful_is_not_login_gate():
    # The gate is OVER once login succeeded — do not flag a healthy session.
    cap = "Login successful. Press Enter to continue\n"
    r = classify(cap, [], _meta())
    assert "login_gate" not in r["flags"]


# --------------------------------------------------------------------------- #
# signal-first
# --------------------------------------------------------------------------- #

def test_up_channel_error_signal_wins_over_quiet_screen():
    # Screen looks calm, but the worker explicitly reported an error.
    r = classify("just some log output\n", [],
                 _meta(signals=[{"kind": "ERROR", "payload": "build broke"}]))
    assert r["state"] == "error"
    assert "up-channel" in r["evidence"]


def test_up_channel_need_signal_is_waiting_human():
    r = classify("...\n", [], _meta(signals=[{"kind": "NEED", "payload": "creds"}]))
    assert r["state"] == "waiting_human"


def test_up_channel_done_signal_is_done_idle():
    r = classify("...\n", [], _meta(signals=[{"kind": "DONE", "payload": ""}]))
    assert r["state"] == "done_idle"


def test_progress_signal_is_soft_screen_still_decides():
    # PROGRESS is not authoritative: a traceback on screen still wins.
    cap = 'Traceback (most recent call last):\n  File "x.py", line 1\n'
    r = classify(cap, [], _meta(signals=[{"kind": "PROGRESS", "payload": "step 2"}]))
    assert r["state"] == "error"


# --------------------------------------------------------------------------- #
# live path (extract_features) — degrades gracefully without tmux/logs
# --------------------------------------------------------------------------- #

def test_extract_features_returns_classify_triple(monkeypatch):
    from emux import judge, server
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_read_log", lambda name, lines=None: "some output\n")
    monkeypatch.setattr(server, "_new_signals", lambda name, ack: [])
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)  # no tmux → assume live
    feats = judge.extract_features("worker-1")
    assert set(feats) == {"name", "capture_text", "activity", "meta"}
    assert feats["capture_text"] == "some output\n"
    assert feats["meta"]["live"] is True
    # the triple must be classifiable
    out = classify(feats["capture_text"], feats["activity"], feats["meta"])
    assert out["state"] in {
        "running", "planning", "editing", "waiting_external", "waiting_human",
        "thrashing", "stuck", "error", "done_idle", "dead",
    }


def test_live_pane_beats_stale_log_tail(monkeypatch):
    # The log tail keeps a gate that cleared minutes ago; the LIVE pane shows
    # the agent running again. Current state must come from the pane.
    from emux import judge, server
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(server, "_read_log",
                        lambda name, lines=None: "Do you want to proceed?\n❯ 1. Yes\n")
    monkeypatch.setattr(server, "_new_signals", lambda name, ack: [])
    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_session_exists", lambda s, host=None: True)

    def fake_tmux(args, timeout=10, host=None):
        if args[0] == "capture-pane":
            return 0, "Compiling step 3 of 9... (12s · esc to interrupt)\n", ""
        if args[0] == "display-message":
            return 0, "claude\n", ""
        return 0, "", ""
    monkeypatch.setattr(server, "_run_tmux", fake_tmux)
    r = judge.classify_session("worker-live")
    assert r["state"] != "waiting_human"   # the stale log gate must not win


def test_classify_session_maps_error_from_log(monkeypatch):
    from emux import judge, server
    monkeypatch.setattr(server, "_load_registry", lambda: {})
    monkeypatch.setattr(
        server, "_read_log",
        lambda name, lines=None: 'Traceback (most recent call last):\n  File "a.py", line 3\n',
    )
    monkeypatch.setattr(server, "_new_signals", lambda name, ack: [])
    monkeypatch.setattr(server, "_resolve_tmux", lambda: None)
    r = judge.classify_session("worker-2")
    assert r["state"] == "error"
    assert r["name"] == "worker-2"
