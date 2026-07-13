"""The per-agent contract. An adapter that LIES is worse than one that admits
it doesn't know — a wrong busy-regex makes the judge confidently mislabel."""

from __future__ import annotations

from emux import adapters


def test_claude_is_detected_by_its_semver_pane_title():
    # Claude Code retitles its pane to its version, so the binary name is absent.
    a = adapters.detect("2.1.207")
    assert a is not None and a.key == "claude"


def test_detect_by_binary_and_by_screen_content():
    assert adapters.detect("codex").key == "codex"
    assert adapters.detect("aider").key == "aider"
    # node-wrapped CLIs all report as "node" — fall back to what's on screen
    assert adapters.detect("node", "… esc to interrupt").key == "claude"
    assert adapters.detect("node", "OpenAI Codex v1").key == "codex"
    assert adapters.detect("zsh") is None          # a shell is not an agent
    assert adapters.detect("") is None


def test_paste_settle_comes_from_the_adapter_not_the_caller():
    # Claude's 0.4s is a MEASURED fact about Claude, not a global default.
    assert adapters.settle_for("claude") == 0.4
    # Codex is unmeasured — it must NOT inherit Claude's number.
    assert adapters.settle_for("codex") == 0.0
    assert adapters.settle_for(None) == 0.0


def test_both_subscribed_agents_can_resume_and_signal_done():
    # The two agents we actually pay for must support the whole lifecycle.
    for key in ("claude", "codex"):
        a = adapters.get(key)
        assert a.access == "subscription"
        assert a.resume("abc123"), f"{key} cannot resume by id"
        assert a.done_hook, f"{key} has no completion signal"
    assert "--resume" in adapters.get("claude").resume("abc123")
    assert "resume" in adapters.get("codex").resume("abc123")


def test_unmeasured_agents_declare_their_unknowns():
    # An adapter we haven't measured must SAY so rather than guess.
    codex = next(r for r in adapters.table() if r["agent"] == "codex")
    assert "busy_sigs" in codex["unknowns"]      # judge can't read codex state yet
    claude = next(r for r in adapters.table() if r["agent"] == "claude")
    assert claude["read"] is True                # …but it can read Claude's


def test_tmux_send_takes_its_settle_from_the_pane_S_AGENT(monkeypatch):
    """The caller no longer has to KNOW that Claude needs a paste-settle.
    tmux_send asks the pane what's running and uses that agent's number."""
    import asyncio

    from emux import server

    calls: list[list[str]] = []
    slept: list[float] = []

    def fake_tmux(args, timeout=10, host=None):
        calls.append(args)
        if args[0] == "display-message":
            return (0, pane_cmd, "")       # what's running in the pane
        return (0, "", "")

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", fake_tmux)
    monkeypatch.setattr(server.time, "sleep", lambda s: slept.append(s))

    # A Claude pane (reported as its version string) ⇒ text, wait, THEN Enter.
    pane_cmd = "2.1.207"
    server._PANE_AGENT_CACHE.clear()
    calls.clear()
    slept.clear()
    asyncio.run(server.tmux_send(target="s", keys="hello"))
    assert slept == [0.4], "Claude's measured paste-settle was not applied"
    sends = [c for c in calls if c[0] == "send-keys"]
    assert sends[0][-1] == "hello" and sends[1][-1] == "Enter"  # separate keystrokes

    # A plain shell ⇒ no settle, classic single send. Claude's quirk must not leak.
    pane_cmd = "zsh"
    server._PANE_AGENT_CACHE.clear()
    calls.clear()
    slept.clear()
    asyncio.run(server.tmux_send(target="s", keys="ls"))
    assert slept == [], "a shell was given an agent's paste-settle"
    sends = [c for c in calls if c[0] == "send-keys"]
    assert sends == [["send-keys", "-t", "s", "ls", "Enter"]]
