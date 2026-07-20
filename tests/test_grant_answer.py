"""EID-874 stage 2 — delegation-authorized gate answering. Deny-by-default is
the whole contract: a gate is answered ONLY when the gate type is grantable,
an exact grant authorizes the identity, and the highlighted option is verified
safe. Every other path leaves the gate frozen (no send-keys) for a human."""

from __future__ import annotations

import asyncio
import json

import pytest

from emux import delegation, server

TRUST_GATE = "Do you trust the contents of this directory?\n❯ 1. Yes, I trust this folder\n  2. No"
BASH_GATE = "Do you want to proceed?\nBash command: echo hi\n❯ 1. Yes\n  2. No"
NOW_ENV = None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    state = {"exists": True, "screen": TRUST_GATE}
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
            return (0, "", "")
        raise AssertionError(args)

    monkeypatch.setattr(server, "_resolve_tmux", lambda: "/usr/bin/tmux")
    monkeypatch.setattr(server, "_run_tmux", run)
    monkeypatch.setattr(server, "_GATE_AUDIT_PATH", tmp_path / "gate.jsonl")
    monkeypatch.setattr(server, "_GATE_LOCK_PATH", tmp_path / "gate.lock")
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"worker": {"session": "worker", "cwd": "/home/eidos/proj", "channels": ["default"]}}))
    monkeypatch.setattr(server, "REGISTRY_PATH", reg)
    server._PANE_AGENT_CACHE.clear()
    monkeypatch.setenv("EMUX_SERVER_ID", "test-server")
    grants = tmp_path / "delegations.json"
    monkeypatch.setattr(delegation, "GRANTS_PATH", grants)

    def set_grant(gate_types, identity="daniel", workspace="proj", server_id="test-server", expires=9_999_999_999):
        grants.write_text(json.dumps({"version": 1, "grants": [{
            "identity": identity, "server": server_id, "workspace": workspace,
            "gate_types": gate_types, "expires_at": expires}]}))

    return server, state, calls, set_grant


def _answer(identity="daniel"):
    return asyncio.run(server.tmux_grant_answer("worker", identity, by_registry_name=True))


def _sent_keys(calls):
    return [c for c in calls if c and c[0] == "send-keys"]


# ---- the pure guards -------------------------------------------------------

def test_highlighted_option_and_affirms_trust():
    assert server._highlighted_option(TRUST_GATE) == "Yes, I trust this folder"
    assert server._affirms_trust("Yes, I trust this folder") is True
    assert server._affirms_trust("No") is False
    assert server._affirms_trust("No, exit") is False
    assert server._affirms_trust("Skip") is False
    assert server._affirms_trust(None) is False
    # a trust-shaped label carrying a denial word is refused (defense in depth)
    assert server._affirms_trust("Don't trust") is False


# ---- deny by default -------------------------------------------------------

def test_no_grant_leaves_gate_frozen(env):
    _server, _state, calls, _set = env
    res = _answer()
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_matching_grant_answers_and_attributes(env):
    _server, _state, calls, set_grant = env
    set_grant(["trusted_workspace"])
    res = _answer()
    assert res["ok"] and res["answered_via"] == "grant" and res["identity"] == "daniel"
    keys = _sent_keys(calls)
    assert keys == [["send-keys", "-t", "worker", "Enter"]]  # exactly one Enter


def test_non_allowlisted_gate_type_never_answered_even_with_grant(env):
    _server, state, calls, set_grant = env
    state["screen"] = BASH_GATE  # command_approval — NOT in GRANTABLE_GATE_TYPES
    set_grant(["command_approval", "trusted_workspace"])  # grant even lists it
    res = _answer()
    assert not res["ok"] and res["error"] == "gate_type_not_grantable"
    assert not _sent_keys(calls)


def test_wrong_identity_denied(env):
    _server, _state, calls, set_grant = env
    set_grant(["trusted_workspace"], identity="daniel")
    res = _answer(identity="mallory")
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_wrong_workspace_denied(env):
    _server, _state, calls, set_grant = env
    set_grant(["trusted_workspace"], workspace="other")
    res = _answer()
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_expired_grant_denied(env):
    _server, _state, calls, set_grant = env
    set_grant(["trusted_workspace"], expires=1)
    res = _answer()
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_unsafe_highlighted_option_denied(env):
    # A grant exists and the gate type is right, but the highlighted option is
    # "No" — grant-driven Enter must refuse rather than press it.
    _server, state, calls, set_grant = env
    state["screen"] = "Do you trust the contents of this directory?\n  1. Yes, I trust this folder\n❯ 2. No, exit"
    set_grant(["trusted_workspace"])
    res = _answer()
    assert not res["ok"] and res["error"] == "unsafe_highlighted_option"
    assert not _sent_keys(calls)


def test_empty_identity_denied(env):
    _server, _state, calls, set_grant = env
    set_grant(["trusted_workspace"])
    res = _answer(identity="")
    assert not res["ok"] and res["error"] == "no_identity"
    assert not _sent_keys(calls)
