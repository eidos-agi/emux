"""EID-874 — scoped delegation grant substrate. Deny by default is the contract:
a false 'authorized' would let an agent answer a live gate it was never granted,
so every ambiguity resolves to False."""

from __future__ import annotations

import json

from emux import delegation


def _write(tmp_path, monkeypatch, grants):
    path = tmp_path / "delegations.json"
    path.write_text(json.dumps({"version": 1, "grants": grants}))
    monkeypatch.setattr(delegation, "GRANTS_PATH", path)


NOW = 1000.0
GOOD = {
    "identity": "vybhav",
    "server": "emux-e1",
    "workspace": "eidos",
    "gate_types": ["trusted_workspace"],
    "expires_at": NOW + 3600,
}


def test_no_store_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(delegation, "GRANTS_PATH", tmp_path / "missing.json")
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "trusted_workspace", now=NOW) is False


def test_exact_unexpired_grant_authorizes(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [GOOD])
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "trusted_workspace", now=NOW) is True


def test_every_field_mismatch_denies(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [GOOD])
    m = delegation.may_answer_gate
    assert m("daniel", "emux-e1", "eidos", "trusted_workspace", now=NOW) is False   # wrong identity
    assert m("vybhav", "emux-e2", "eidos", "trusted_workspace", now=NOW) is False    # wrong server
    assert m("vybhav", "emux-e1", "other", "trusted_workspace", now=NOW) is False    # wrong workspace
    assert m("vybhav", "emux-e1", "eidos", "command_approval", now=NOW) is False      # ungranted gate type


def test_expired_grant_denies(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [GOOD])
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "trusted_workspace", now=NOW + 7200) is False


def test_malformed_grants_deny(tmp_path, monkeypatch):
    for bad in (
        {**GOOD, "expires_at": None},               # no expiry
        {**GOOD, "expires_at": True},               # bool is not a time
        {**GOOD, "gate_types": "trusted_workspace"},  # not a list
        {k: v for k, v in GOOD.items() if k != "identity"},  # missing field
    ):
        _write(tmp_path, monkeypatch, [bad])
        assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "trusted_workspace", now=NOW) is False


def test_empty_args_deny(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [GOOD])
    assert delegation.may_answer_gate("", "emux-e1", "eidos", "trusted_workspace", now=NOW) is False
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "", now=NOW) is False


def test_no_wildcard(tmp_path, monkeypatch):
    # A grant that literally lists "*" does NOT match a different gate type —
    # matching is exact, so authority never silently widens.
    _write(tmp_path, monkeypatch, [{**GOOD, "gate_types": ["*"]}])
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "trusted_workspace", now=NOW) is False
    assert delegation.may_answer_gate("vybhav", "emux-e1", "eidos", "*", now=NOW) is True  # only the literal


def test_active_grants_filters_expired_and_malformed(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [GOOD, {**GOOD, "expires_at": NOW - 1}, {"identity": "vybhav"}])
    active = delegation.active_grants("vybhav", now=NOW)
    assert len(active) == 1 and active[0]["gate_types"] == ["trusted_workspace"]
