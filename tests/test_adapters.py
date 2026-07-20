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
    assert adapters.detect("node", "… ? for shortcuts").key == "claude"
    assert adapters.detect("node", "OpenAI Codex v1").key == "codex"
    assert adapters.detect("zsh") is None          # a shell is not an agent
    assert adapters.detect("") is None


def test_paste_settle_comes_from_the_adapter_not_the_caller():
    # Claude's 0.4s is a MEASURED fact about Claude, not a global default.
    assert adapters.settle_for("claude") == 0.4
    # Codex was MEASURED separately (0.2 fails, 0.4 submits) — same number,
    # but arrived at by measurement, not by inheriting Claude's.
    assert adapters.settle_for("codex") == 0.4
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
    # An adapter we haven't measured must SAY so rather than guess. gemini is
    # detectable but undriven — it must not inherit anyone else's numbers.
    gem = next(r for r in adapters.table() if r["agent"] == "gemini")
    assert "busy_sigs" in gem["unknowns"] and gem["read"] is False
    assert adapters.settle_for("gemini") == 0.0
    # the two we PAY for are fully measured
    for key in ("claude", "codex"):
        row = next(r for r in adapters.table() if r["agent"] == key)
        assert row["read"] is True and row["unknowns"] == []


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


# --------------------------------------------------------------------------- #
# Codex — every value below was MEASURED against a live codex in tmux, not guessed
# --------------------------------------------------------------------------- #

def test_codex_pane_reports_the_platform_binary_not_codex():
    # tmux reports "codex-aarch64-a", not "codex". Don't "fix" this to an exact match.
    assert adapters.detect("codex-aarch64-a").key == "codex"


def test_codex_and_claude_both_say_esc_to_interrupt_so_it_cannot_identify_claude():
    # Codex prints "• Working (1s • esc to interrupt)". If that string were a
    # Claude content-signature, a node-wrapped Codex would misdetect AS Claude.
    assert "esc to interrupt" not in adapters.CLAUDE.content_sigs
    assert adapters.detect("node", "• Working (3s • esc to interrupt)").key == "codex"


def test_codex_needs_the_same_paste_settle_and_it_was_measured():
    # 0.2s does NOT submit; 0.4s does. Measured live, not borrowed from Claude.
    assert adapters.settle_for("codex") == 0.4


def test_codex_gates_are_known_and_typing_through_them_is_refused():
    # Every Codex startup gate eats keystrokes and PERSISTS the answer.
    screens = {
        "Do you trust the contents of this directory?": "do you trust the contents of this directory",
        "Hooks need review\n› 2. Trust all and continue": "hooks need review",
        "✨ Update available! 0.142.5 -> 0.144.1\n› 1. Update now (runs `brew upgrade`)": "update available",
    }
    for screen, expected in screens.items():
        assert adapters.gated("codex", screen) == expected, screen
    # a normal composer is NOT a gate
    assert adapters.gated("codex", "› write some code\n  gpt-5.5 high") is None


def test_gate_detected_even_when_agent_is_unidentified():
    # MEASURED LIVE (2026-07-20): codex runs under a `node` wrapper, so
    # pane_current_command is "node" and _pane_agent returns None. With the old
    # code, gated(None, screen) returned None → a real trust gate was missed →
    # session.send typed through it. The fix: unknown agent falls back to an
    # agent-agnostic scan of every known signature. Fail closed, not open.
    trust_gate = (
        "Do you trust the contents of this directory? Working with untrusted\n"
        "› 1. Yes, continue"
    )
    assert adapters.gated(None, trust_gate) is not None
    assert adapters.gated("", trust_gate) is not None
    # any known signature matching is enough to refuse — here both the codex
    # trust phrase and the generic "1. yes" selector are present.
    assert adapters.any_gate(trust_gate) is not None
    assert adapters.any_gate("do you trust the contents of this directory") is not None
    # a benign screen with no known gate signature stays sendable
    assert adapters.any_gate("$ ls -la\ntotal 4\ndrwxr-xr-x") is None
    assert adapters.gated(None, "just a normal shell prompt $ ") is None


def test_looks_gate_like_pre_filter():
    # Real gate shapes escalate; ordinary output does not.
    for gate in [
        "Do you want to proceed?\n❯ 1. Yes\n  2. No",
        "Do you trust the contents of this directory?",
        "Overwrite file? (y/n)",
        "✨ Update available! 0.1 -> 0.2",
        "Hooks need review",
        "Press enter to continue",
        "› 2. Skip",
    ]:
        assert adapters.looks_gate_like(gate), gate
    for clear in [
        "$ ls -la\ntotal 8\ndrwxr-xr-x 2 me me 4096 file.txt",
        "Successfully built emux-0.68.2-py3-none-any.whl",
        "393 passed, 2 warnings in 49.07s",
        "eidos@host:~/repo$ ",
    ]:
        assert not adapters.looks_gate_like(clear), clear


def test_detect_gate_layers_signature_then_ml_and_fails_closed():
    # 1. Known signature → instant path, ML never consulted.
    calls = []
    def ml_spy(content):
        calls.append(content); return False
    kind, detail = adapters.detect_gate("codex", "do you trust the contents of this directory", ml=ml_spy)
    assert kind == "signature" and not calls

    # 2. Identified agent, clear + not gate-shaped → clear, ML skipped.
    kind, _ = adapters.detect_gate("claude", "$ ls\nfile.txt", ml=ml_spy)
    assert kind is None and not calls

    # 3. Gate-shaped, no signature, model says CLEAR → allowed.
    kind, _ = adapters.detect_gate("claude", "Overwrite? (y/n)", ml=lambda c: False)
    assert kind is None

    # 4. Gate-shaped, no signature, model says GATE → gated via ML.
    kind, detail = adapters.detect_gate("claude", "Proceed with deploy? (y/n)", ml=lambda c: True)
    assert kind == "ml" and detail == "ml-gate"

    # 5. Model UNCERTAIN / unreachable on a suspicious screen → FAIL CLOSED.
    kind, detail = adapters.detect_gate("claude", "Some novel modal (y/n)", ml=lambda c: None)
    assert kind == "ml" and detail == "ml-uncertain"

    # 6. Unidentified agent escalates to ML even when not obviously gate-shaped
    #    (we can't trust the signature path at all for an unknown agent).
    kind, _ = adapters.detect_gate(None, "weird screen with no known signature", ml=lambda c: True)
    assert kind == "ml"


def test_detect_gate_ml_escape_hatch(monkeypatch):
    # EMUX_GATE_ML=off ⇒ signatures only, ML never runs (ops escape hatch).
    monkeypatch.setenv("EMUX_GATE_ML", "off")
    called = []
    kind, _ = adapters.detect_gate("claude", "Proceed? (y/n)", ml=lambda c: called.append(1) or True)
    assert kind is None and not called
    # signatures still work with the hatch on
    kind, _ = adapters.detect_gate("codex", "update available", ml=lambda c: called.append(1) or True)
    assert kind == "signature" and not called


def test_both_subscribed_agents_now_have_a_proven_done_signal():
    # Codex has a NATIVE Stop hook, same JSON shape as Claude's — proven live:
    # it fired `emux signal IDLE` into the inbox and the judge read done_idle.
    for key in ("claude", "codex"):
        a = adapters.get(key)
        assert a.done_hook_install["path"] == ["hooks", "Stop"]
        assert a.done_hook_install["run"] == "emux signal IDLE"
    # and codex no longer has unknowns that stop the judge reading it
    codex = next(r for r in adapters.table() if r["agent"] == "codex")
    assert codex["read"] is True and codex["unknowns"] == []


def test_codex_exec_cannot_call_tools_so_a_oneshot_manager_is_useless():
    # MEASURED: `codex exec` auto-cancels every MCP tool call (no approver in
    # headless), for emux AND a known-good server. A codex MANAGER must be an
    # interactive session. Claude's -p can call tools, so it may be one-shot.
    assert adapters.CODEX.oneshot_can_use_tools is False
    assert adapters.CLAUDE.oneshot_can_use_tools is True


def test_codex_mcp_approval_menu_is_a_gate():
    # Codex asks approval PER MCP TOOL CALL. Driving through that menu blindly
    # would auto-approve tools, so it must register as a gate.
    screen = ('Allow the emux MCP server to run tool "tmux_capture"?\n'
              '› 1. Allow\n  2. Allow for this session\n  4. Cancel')
    assert adapters.gated("codex", screen) is not None
    # …and its second hook-gate presentation, seen live
    assert adapters.gated("codex", "Press t to trust all; enter to review hooks; esc to close")
