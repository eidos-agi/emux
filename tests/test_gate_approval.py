"""Safe, explicit, fingerprint-bound approval of live terminal gates."""

from __future__ import annotations

import argparse
import asyncio
import json

import pytest


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    from emux import server

    state = {
        "exists": True,
        "screen": "Do you want to proceed?\nBash command: echo super-secret\n❯ 1. Yes\n  2. No",
    }
    calls: list[list[str]] = []

    def run(args, timeout=10, host=None):
        calls.append(args)
        if args[0] == "has-session":
            return (0, "", "") if state["exists"] else (1, "", "gone")
        if args[0] == "display-message":
            return (0, "claude", "")
        if args[0] == "capture-pane":
            return (0, state["screen"], "")
        if args[0] == "send-keys":
            return (0, "", "") if state["exists"] else (1, "", "gone")
        raise AssertionError(args)

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", run)
    monkeypatch.setattr(server, "_GATE_AUDIT_PATH", tmp_path / "gate-audit.jsonl")
    monkeypatch.setattr(server, "_GATE_LOCK_PATH", tmp_path / "gate-audit.lock")
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    server._PANE_AGENT_CACHE.clear()
    return server, state, calls, tmp_path


@pytest.mark.parametrize(
    ("screen", "gate_type"),
    [
        ("Do you trust the contents of this workspace?\n❯ 1. Yes", "trusted_workspace"),
        ('Allow the emux MCP server to run tool "tmux_capture"?\n❯ 1. Allow',
         "mcp_approval"),
        ("Do you want to proceed?\nBash command: git status\n❯ 1. Yes",
         "command_approval"),
    ],
)
def test_observe_classifies_required_gate_types(gate_env, screen, gate_type):
    server, state, _calls, _tmp = gate_env
    state["screen"] = screen
    result = asyncio.run(server.tmux_gate("worker"))
    assert result["ok"] and result["gate_type"] == gate_type
    assert len(result["gate_fingerprint"]) == 64
    assert result["allowed_actions"] == ["approve", "reject"]


def test_approve_sends_exactly_one_named_enter_and_redacted_audit(gate_env):
    server, _state, calls, tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    result = asyncio.run(server.tmux_approve_gate(
        "worker", observed["gate_fingerprint"], subject="uid-daniel",
        device="hostkey", request_id="req-1",
    ))
    assert result["ok"] and result["sent_key"] == "Enter"
    sends = [call for call in calls if call[0] == "send-keys"]
    assert sends == [["send-keys", "-t", "worker", "Enter"]]
    raw = (tmp / "gate-audit.jsonl").read_text()
    assert "super-secret" not in raw and "Bash command" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    sent = records[-1]
    assert sent["subject"] == "uid-daniel" and sent["device"] == "hostkey"
    assert sent["request_id"] == "req-1" and sent["outcome"] == "sent"
    assert sent["gate_type"] == "command_approval"


def test_reject_sends_exactly_one_escape(gate_env):
    server, _state, calls, _tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    result = asyncio.run(server.tmux_approve_gate(
        "worker", observed["gate_fingerprint"], action="reject",
    ))
    assert result["ok"] and result["sent_key"] == "Escape"
    assert [call for call in calls if call[0] == "send-keys"] == [
        ["send-keys", "-t", "worker", "Escape"]
    ]


def test_changed_gate_fails_closed_without_sending(gate_env):
    server, state, calls, _tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    state["screen"] += "\nchanged redraw with another option"
    result = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    assert not result["ok"] and result["error"] == "stale_gate"
    assert not [call for call in calls if call[0] == "send-keys"]


def test_cleared_gate_fails_closed(gate_env):
    server, state, calls, _tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    state["screen"] = "Claude Code\n❯ ready for a prompt"
    result = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    assert not result["ok"] and result["error"] == "no_active_gate"
    assert not [call for call in calls if call[0] == "send-keys"]


def test_non_gate_cannot_issue_fingerprint(gate_env):
    server, state, _calls, _tmp = gate_env
    state["screen"] = "Claude Code\n❯ ready for a prompt"
    result = asyncio.run(server.tmux_gate("worker"))
    assert not result["ok"] and result["error"] == "no_active_gate"


def test_ordinary_send_and_legacy_force_both_remain_blocked(gate_env):
    server, _state, calls, _tmp = gate_env
    ordinary = asyncio.run(server.tmux_send("worker", "Enter", enter=False))
    forced = asyncio.run(server.tmux_send("worker", "Enter", enter=False, force=True))
    assert ordinary["error"] == "blocked_on_gate"
    assert forced["error"] == "blocked_on_gate"
    assert not [call for call in calls if call[0] == "send-keys"]


def test_unsupported_key_fails_closed(gate_env):
    server, _state, calls, tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    result = asyncio.run(server.tmux_approve_gate(
        "worker", observed["gate_fingerprint"], key="Down",
    ))
    assert not result["ok"] and result["error"] == "unsupported_gate_key"
    assert not [call for call in calls if call[0] == "send-keys"]
    assert json.loads((tmp / "gate-audit.jsonl").read_text().splitlines()[-1])["outcome"] == "denied"


def test_malformed_fingerprint_and_action_cannot_leak_into_audit(gate_env):
    server, _state, calls, tmp = gate_env
    result = asyncio.run(server.tmux_approve_gate(
        "worker", "secret prompt text", action="secret action text",
        subject="uid\nsecret", device="laptop\nsecret",
    ))
    assert not result["ok"] and result["error"] == "invalid_gate_fingerprint"
    assert not [call for call in calls if call[0] == "send-keys"]
    raw = (tmp / "gate-audit.jsonl").read_text()
    assert "secret prompt text" not in raw and "secret action text" not in raw
    record = json.loads(raw)
    assert record["gate_fingerprint"] == "invalid" and record["action"] == "unsupported"


def test_expired_fingerprint_fails_closed_and_is_audited(gate_env):
    server, _state, calls, tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    record = json.loads((tmp / "gate-audit.jsonl").read_text())
    record["t"] -= server._GATE_FINGERPRINT_TTL + 1
    (tmp / "gate-audit.jsonl").write_text(json.dumps(record) + "\n")
    result = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    assert not result["ok"] and result["error"] == "expired_or_unobserved_gate"
    assert not [call for call in calls if call[0] == "send-keys"]
    assert json.loads((tmp / "gate-audit.jsonl").read_text().splitlines()[-1])["outcome"] == "denied"


def test_fingerprint_is_single_use_across_calls(gate_env):
    server, _state, calls, _tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    first = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    second = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    assert first["ok"]
    assert not second["ok"] and second["error"] == "gate_replay"
    assert len([call for call in calls if call[0] == "send-keys"]) == 1


def test_session_gone_after_observation_fails_closed(gate_env):
    server, state, calls, _tmp = gate_env
    observed = asyncio.run(server.tmux_gate("worker"))
    state["exists"] = False
    result = asyncio.run(server.tmux_approve_gate("worker", observed["gate_fingerprint"]))
    assert not result["ok"] and result["error"] == "session_gone"
    assert not [call for call in calls if call[0] == "send-keys"]


def test_registered_name_resolves_to_exact_session(gate_env):
    server, _state, calls, tmp = gate_env
    (tmp / "registry.json").write_text(json.dumps({
        "alpha": {"session": "real-worker", "registered_at": 0, "tags": []},
    }))
    observed = asyncio.run(server.tmux_gate("alpha", by_registry_name=True))
    result = asyncio.run(server.tmux_approve_gate(
        "alpha", observed["gate_fingerprint"], by_registry_name=True,
    ))
    assert result["ok"] and result["resolved_session"] == "real-worker"
    assert [call for call in calls if call[0] == "send-keys"] == [
        ["send-keys", "-t", "real-worker", "Enter"]
    ]


def test_cli_gate_and_approve_preserve_json_and_target_mode(monkeypatch, capsys):
    from emux import cli

    calls = []

    async def fake_gate(**kwargs):
        calls.append(("gate", kwargs))
        return {"ok": True, "gate_fingerprint": "abc"}

    async def fake_approve(**kwargs):
        calls.append(("approve", kwargs))
        return {"ok": True, "request_id": "req"}

    monkeypatch.setattr(cli, "tmux_gate", fake_gate)
    monkeypatch.setattr(cli, "tmux_approve_gate", fake_approve)
    assert cli.cmd_gate(argparse.Namespace(target="raw", session=True, json=True)) == 0
    assert cli.cmd_approve(argparse.Namespace(
        target="alpha", fingerprint="abc", action="approve", key=None,
        session=False, subject="uid", device="laptop", request_id="req", json=True,
    )) == 0
    assert calls[0] == ("gate", {"target": "raw", "by_registry_name": False})
    assert calls[1][1]["by_registry_name"] is True
    assert '"gate_fingerprint": "abc"' in capsys.readouterr().out
