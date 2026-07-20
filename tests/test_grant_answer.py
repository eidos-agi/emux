"""EID-874 stage 2 — delegation-authorized gate answering. Deny-by-default is
the whole contract: a gate is answered ONLY when the gate type is grantable, an
exact grant authorizes the identity for the LIVE directory, and a UNIQUE
highlighted option affirms trust. Every other path leaves the gate frozen.

The `adversarial_*` tests below are the confirmed fail-opens from the EID-874
safety audit (2026-07-20) turned into regression tests — each MUST now freeze."""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from emux import delegation, server

TRUST_GATE = "Do you trust the contents of this directory?\n❯ 1. Yes, I trust this folder\n  2. No"
BASH_GATE = "Do you want to proceed?\nBash command: echo hi\n❯ 1. Yes\n  2. No"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    live_path = str(proj)
    workspace_real = os.path.realpath(live_path)
    state = {"exists": True, "screen": TRUST_GATE, "path": live_path}
    calls: list[list[str]] = []

    def run(args, timeout=10, host=None):
        calls.append(args)
        if args[0] == "has-session":
            return (0, "", "") if state["exists"] else (1, "", "gone")
        if args[0] == "display-message":
            if args[-1] == "#{pane_current_path}":
                return (0, state["path"], "")
            return (0, "claude", "")   # pane_current_command / agent
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
    reg.write_text(json.dumps({"worker": {"session": "worker", "cwd": "/stale/ignored", "channels": ["default"]}}))
    monkeypatch.setattr(server, "REGISTRY_PATH", reg)
    server._PANE_AGENT_CACHE.clear()
    monkeypatch.setenv("EMUX_SERVER_ID", "test-server")
    grants = tmp_path / "delegations.json"
    monkeypatch.setattr(delegation, "GRANTS_PATH", grants)
    monkeypatch.setattr(delegation, "DECISION_LOG_PATH", tmp_path / "delegation.jsonl")

    def set_grant(gate_types, identity="daniel", workspace=None, server_id="test-server", expires=9_999_999_999):
        grants.write_text(json.dumps({"version": 1, "grants": [{
            "identity": identity, "server": server_id,
            "workspace": workspace if workspace is not None else workspace_real,
            "gate_types": gate_types, "expires_at": expires}]}))

    return server, state, calls, set_grant, workspace_real, tmp_path


def _answer(identity="daniel"):
    return asyncio.run(server.tmux_grant_answer("worker", identity, by_registry_name=True))


def _sent_keys(calls):
    return [c for c in calls if c and c[0] == "send-keys"]


def _decisions(tmp_path):
    p = tmp_path / "delegation.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- the pure guards -------------------------------------------------------

def test_highlighted_option_unique_and_affirms_trust():
    assert server._highlighted_option(TRUST_GATE) == "Yes, I trust this folder"
    assert server._affirms_trust("Yes, I trust this folder") is True
    assert server._affirms_trust("No, exit") is False
    assert server._affirms_trust(None) is False
    # a bare '>' line is NOT a cursor (agent can print it); no glyph line -> None
    assert server._highlighted_option("> 1. Yes, trust it\n  2. No") is None
    # two glyph lines = ambiguous -> None (fail closed)
    assert server._highlighted_option("❯ 1. Yes, trust\n❯ 2. No") is None


# ---- deny by default -------------------------------------------------------

def test_no_grant_leaves_gate_frozen(env):
    _s, _state, calls, _set, _ws, tmp = env
    res = _answer()
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)
    assert _decisions(tmp)[-1]["decision"] == "denied"


def test_matching_grant_answers_attributes_and_logs(env):
    _s, _state, calls, set_grant, _ws, tmp = env
    set_grant(["trusted_workspace"])
    res = _answer()
    assert res["ok"] and res["answered_via"] == "grant" and res["identity"] == "daniel"
    assert _sent_keys(calls) == [["send-keys", "-t", "worker", "Enter"]]
    assert _decisions(tmp)[-1] == {**_decisions(tmp)[-1], "decision": "allowed", "identity": "daniel"}


def test_wrong_identity_denied(env):
    _s, _state, calls, set_grant, _ws, _tmp = env
    set_grant(["trusted_workspace"], identity="daniel")
    res = _answer(identity="mallory")
    assert not res["ok"] and res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_expired_grant_denied(env):
    _s, _state, calls, set_grant, _ws, _tmp = env
    set_grant(["trusted_workspace"], expires=1)
    assert _answer()["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_empty_identity_denied(env):
    _s, _state, calls, set_grant, _ws, _tmp = env
    set_grant(["trusted_workspace"])
    assert _answer(identity="")["error"] == "no_identity"
    assert not _sent_keys(calls)


# ---- adversarial regression corpus (EID-874 audit fail-opens) --------------

def test_adversarial_command_gate_laundered_as_trusted_workspace(env):
    # HOLE 1/2/7 (critical): a bash command gate whose text contains 'trust' and
    # 'workspace' must NOT be laundered into the grantable trusted_workspace type.
    _s, state, calls, set_grant, _ws, _tmp = env
    state["screen"] = ("Bash command: rm -rf /home/x/proj/workspace/.trusted-cache\n"
                       "Do you want to proceed?\n❯ 1. Yes\n  2. No")
    set_grant(["trusted_workspace"])  # legit trust grant present
    res = _answer()
    assert res["error"] == "gate_type_not_grantable"  # classified command_approval
    assert not _sent_keys(calls)


def test_adversarial_decoy_cursor_line_in_scrollback(env):
    # HOLE 3/5 (high): an agent prints a fake '❯ 1. Yes, trust' line into
    # scrollback while the REAL cursor sits on '2. No'. Two glyph lines =>
    # ambiguous => freeze.
    _s, state, calls, set_grant, _ws, _tmp = env
    state["screen"] = ("Do you trust the contents of this directory?\n"
                       "❯ 1. Yes, trust this workspace\n❯ 2. No, keep untrusted")
    set_grant(["trusted_workspace"])
    res = _answer()
    assert res["error"] == "unsafe_highlighted_option"
    assert not _sent_keys(calls)


def test_adversarial_stale_cwd_does_not_authorize_live_dir(env):
    # HOLE 4/6 (critical): grant is for the registered dir, but the gate is about
    # a DIFFERENT live directory. Scope binds to live pane_current_path, so the
    # grant for the old dir must not answer the gate for the new one.
    _s, state, calls, set_grant, _ws, tmp = env
    evil = tmp / "evil-clone"
    evil.mkdir()
    state["path"] = str(evil)                       # agent cd'd elsewhere
    set_grant(["trusted_workspace"])                # grant is for tmp/proj (workspace_real)
    res = _answer()
    assert res["error"] == "no_grant"
    assert not _sent_keys(calls)


def test_bare_gt_prefix_is_not_a_cursor(env):
    # HOLE 3: '>' as selector let ordinary output pose as the cursor. With the
    # real cursor absent (only a '>' decoy), label verify must fail closed.
    _s, state, calls, set_grant, _ws, _tmp = env
    state["screen"] = "Do you trust the files in this folder?\n> 1. Yes, trust\n  2. No"
    set_grant(["trusted_workspace"])
    res = _answer()
    assert res["error"] == "unsafe_highlighted_option"
    assert not _sent_keys(calls)
