from __future__ import annotations

import json
from pathlib import Path

import pytest

from emux.remote_control.api import (
    RemoteConfig,
    RemoteControllerAPI,
    StaticTokenBoundary,
    TrustedIdentity,
)
from emux.remote_control.protocol import ProtocolError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "controller_v1.json").read_text())
HEADERS = {"Authorization": "Bearer paired-controller"}


class Boundary:
    def authenticate(self, headers):
        if headers.get("Authorization") != "Bearer paired-controller":
            raise ProtocolError("unauthorized", "bad controller credential")
        return TrustedIdentity("uid-daniel", "device-laptop", "controller-laptop")


class Gate:
    decision = "allow"

    def authorize(self, identity, action, target, request_id):
        return self.decision


@pytest.fixture()
def rig(tmp_path):
    calls, gate = [], Gate()

    def registry():
        return {
            "logical-name": {
                "session": "same-name",
                "workspace": "emux",
                "cwd": "/repo/emux",
                "channels": ["engineering"],
                "host": None,
            }
        }

    api = RemoteControllerAPI(
        RemoteConfig(
            "srv-alpha", frozenset({"alpha"}), "test-revision", tmp_path / "requests.json"
        ),
        Boundary(),
        gate,
        registry,
        lambda session, lines: {"ok": True, "session": session, "content": "pane secret"},
        lambda session, text, literal, enter: (
            calls.append((session, text, literal, enter))
            or {"ok": True, "session": session, "sent": text}
        ),
        clock=lambda: 1000.0,
    )
    return api, gate, calls, tmp_path


def request(rid="request-1", nonce="nonce-1234567890123456", action="session.send", server="alpha"):
    return {
        "protocol": FIXTURE["protocol"],
        "request_id": rid,
        "nonce": nonce,
        "issued_at": 1000.0,
        "human_uid": "uid-daniel",
        "device_id": "device-laptop",
        "target": {**FIXTURE["target"], "server": server},
        "action": action,
        "parameters": {
            "session.send": {"text": "do the work"},
            "session.capture": {"lines": 20},
        }.get(action, {}),
    }


def test_capabilities_require_boundary_and_match_shared_fixture(rig):
    api, *_ = rig
    assert api.capabilities(HEADERS) == {
        "server_id": "srv-alpha",
        "protocol": FIXTURE["protocol"],
        "revision": "test-revision",
        "actions": FIXTURE["actions"],
    }
    with pytest.raises(ProtocolError, match="bad controller"):
        api.capabilities({})


def test_execute_exact_target_and_idempotent_duplicate(rig):
    api, _, calls, _ = rig
    status, first = api.submit(HEADERS, request())
    status2, second = api.submit(HEADERS, request())
    assert status == status2 == 200
    assert first["request_id"] == second["request_id"] == "request-1"
    assert calls == [("same-name", "do the work", True, True)]


def test_wrong_server_target_and_replay_fail_closed(rig):
    api, *_ = rig
    with pytest.raises(ProtocolError) as exc:
        api.submit(HEADERS, request(server="beta"))
    assert exc.value.code == "wrong_server"
    api.submit(HEADERS, request())
    changed = request()
    changed["parameters"]["text"] = "different"
    with pytest.raises(ProtocolError) as exc:
        api.submit(HEADERS, changed)
    assert exc.value.code == "replay"
    with pytest.raises(ProtocolError) as exc:
        api.submit(HEADERS, request("request-2"))
    assert exc.value.code == "replay"


def test_version_identity_unknown_action_and_target_fail_closed(rig):
    api, *_ = rig
    skew = request()
    skew["protocol"] = "emux-remote/2.0"
    forged = request()
    forged["human_uid"] = "uid-attacker"
    unknown = request(action="memory.remember")
    target = request()
    target["target"]["workspace"] = "other"
    for data, code in [
        (skew, "protocol_skew"),
        (forged, "identity_mismatch"),
        (unknown, "unsupported_action"),
        (target, "unknown_target"),
    ]:
        with pytest.raises(ProtocolError) as exc:
            api.submit(HEADERS, data)
        assert exc.value.code == code


def test_hancock_pending_cancellation_and_denial(rig):
    # Gate flows apply to consequential actions only — session.interrupt here.
    api, gate, calls, _ = rig
    gate.decision = "pending"
    status, pending = api.submit(HEADERS, request(action="session.interrupt"))
    assert status == 202 and pending["status"] == "pending" and not calls
    status, cancelled = api.cancel(
        HEADERS,
        "request-1",
        {"protocol": FIXTURE["protocol"], "nonce": "cancel-1234567890123456", "issued_at": 1000.0},
    )
    assert status == 200 and cancelled["status"] == "cancelled"
    gate.decision = "deny"
    _, denied = api.submit(
        HEADERS, request("request-2", "nonce-2234567890123456", action="session.interrupt")
    )
    assert denied["status"] == "denied" and not calls


def test_send_is_transport_not_authorization(rig):
    # decision: cockpit-eidos/decisions/2026-07-20-send-is-transport-not-authorization.md
    # An ordinary authenticated send to an exact live target never consults the
    # approval gate — it is the paired human typing into their own session.
    api, gate, calls, _ = rig
    gate.decision = "deny"  # would block any consequential action
    status, done = api.submit(HEADERS, request())
    assert status == 200 and done["status"] == "completed"
    assert calls == [("same-name", "do the work", True, True)]


def test_send_fails_closed_on_visible_gate_and_probe_ambiguity(rig, tmp_path):
    # Transport must never answer or bypass a visible permission gate, and
    # ambiguity (gone session, failed capture) fails closed.
    api, _, calls, _ = rig
    probes = {"state": {"ok": True, "fingerprint": "f" * 64, "gate_type": "menu"}}
    api.gate_probe = lambda session: probes["state"]
    _, denied = api.submit(HEADERS, request())
    assert denied["status"] == "denied" and denied["result"]["error"] == "gated_session"
    assert not calls
    probes["state"] = {"ok": False, "error": "session_gone"}
    _, gone = api.submit(HEADERS, request("request-2", "nonce-2234567890123456"))
    assert gone["status"] == "denied" and gone["result"]["error"] == "session_gone"
    assert not calls
    probes["state"] = {"ok": False, "error": "no_active_gate"}
    _, sent = api.submit(HEADERS, request("request-3", "nonce-3234567890123456"))
    assert sent["status"] == "completed"
    assert calls == [("same-name", "do the work", True, True)]


def test_receipt_state_redacts_parameters_and_semantic_memory(rig):
    api, _, _, tmp_path = rig
    data = request(action="session.capture")
    data["parameters"]["semantic_memory"] = "never persist me"
    with pytest.raises(ProtocolError) as exc:
        api.submit(HEADERS, data)
    assert exc.value.code == "invalid_parameters"
    clean = request("request-2", "nonce-2234567890123456", action="session.capture")
    _, response = api.submit(HEADERS, clean)
    assert response["result"]["content"] == "pane secret"
    raw = (tmp_path / "requests.json").read_text()
    assert "pane secret" not in raw and "never persist me" not in raw
    assert "parameters" not in raw and "do the work" not in raw


def test_status_and_completed_work_not_cancellable(rig):
    api, *_ = rig
    api.submit(HEADERS, request())
    assert api.status(HEADERS, "request-1")[1]["status"] == "completed"
    with pytest.raises(ProtocolError) as exc:
        api.cancel(
            HEADERS,
            "request-1",
            {
                "protocol": FIXTURE["protocol"],
                "nonce": "cancel-1234567890123456",
                "issued_at": 1000.0,
            },
        )
    assert exc.value.code == "not_cancellable"



def test_static_boundary_binds_token_to_immutable_identity():
    identity = TrustedIdentity("uid-daniel", "device-laptop", "controller-laptop")
    boundary = StaticTokenBoundary("x" * 32, identity)
    assert boundary.authenticate({"Authorization": "Bearer " + "x" * 32}) == identity
    with pytest.raises(ProtocolError) as exc:
        boundary.authenticate({"Authorization": "Bearer wrong"})
    assert exc.value.code == "unauthorized"


def test_environment_wiring_is_disabled_or_fails_closed(monkeypatch, tmp_path):
    from emux import web

    monkeypatch.delenv("EMUX_REMOTE_CONTROLLER_TOKEN", raising=False)
    assert web._remote_controller_from_env() is None

    monkeypatch.setenv("EMUX_REMOTE_CONTROLLER_TOKEN", "x" * 32)
    with pytest.raises(RuntimeError, match="partially configured"):
        web._remote_controller_from_env()

    values = {
        "EMUX_REMOTE_CONTROLLER_HUMAN_UID": "uid-daniel",
        "EMUX_REMOTE_CONTROLLER_DEVICE_ID": "device-laptop",
        "EMUX_REMOTE_CONTROLLER_ID": "controller-laptop",
        "EMUX_REMOTE_CONTROLLER_SERVER_ID": "hostkey-server",
        "EMUX_REMOTE_CONTROLLER_ALIASES": "hostkey,hostkey-e1",
        "EMUX_REMOTE_CONTROLLER_STATE": str(tmp_path / "requests.json"),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    api = web._remote_controller_from_env()
    assert api.capabilities({"Authorization": "Bearer " + "x" * 32})["server_id"] == (
        "hostkey-server"
    )
    with pytest.raises(ProtocolError) as exc:
        api.capabilities({"Authorization": "Bearer wrong"})
    assert exc.value.code == "unauthorized"
