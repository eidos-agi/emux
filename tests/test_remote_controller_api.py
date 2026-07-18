from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from emux.controller.credentials import Credential
from emux.controller.protocol import ProtocolError
from emux.controller.registry import PairedServer
from emux.controller.remote import HttpRemoteExecutor
from emux.controller.remote_api import RemoteConfig, RemoteControllerAPI, TrustedIdentity

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
        "parameters": {"text": "do the work"} if action == "session.send" else {"lines": 20},
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
    api, gate, calls, _ = rig
    gate.decision = "pending"
    status, pending = api.submit(HEADERS, request())
    assert status == 202 and pending["status"] == "pending" and not calls
    status, cancelled = api.cancel(
        HEADERS,
        "request-1",
        {"protocol": FIXTURE["protocol"], "nonce": "cancel-1234567890123456", "issued_at": 1000.0},
    )
    assert status == 200 and cancelled["status"] == "cancelled"
    gate.decision = "deny"
    _, denied = api.submit(HEADERS, request("request-2", "nonce-2234567890123456"))
    assert denied["status"] == "denied" and not calls


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


def test_http_adapter_round_trip_capability_request_and_status(rig, monkeypatch):
    from emux import web
    from emux.controller import remote as remote_module

    monkeypatch.setattr(remote_module.time, "time", lambda: 1000.0)
    api, _, calls, _ = rig
    handler = type("RemoteHandler", (web.EmuxWebHandler,), {"remote_controller_api": api})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{httpd.server_address[1]}"
    server = PairedServer("alpha", endpoint, "credential", FIXTURE["protocol"], "srv-alpha")
    remote = HttpRemoteExecutor()
    credential = Credential("paired-controller", "generation-1")
    try:
        caps = remote.capabilities(server, credential)
        assert caps.server_id == "srv-alpha" and caps.actions == frozenset(FIXTURE["actions"])
        result = remote.execute(
            server,
            credential,
            {
                "protocol": FIXTURE["protocol"],
                "request_id": "http-request",
                "human_uid": "uid-daniel",
                "device_id": "device-laptop",
                "target": FIXTURE["target"],
                "action": "session.send",
                "parameters": {"text": "from adapter"},
            },
        )
        assert result["acknowledged"] and result["status"] == "completed"
        status = remote.status(server, credential, "http-request")
        assert status["status"] == "completed"
        assert calls == [("same-name", "from adapter", True, True)]
    finally:
        httpd.shutdown()
        httpd.server_close()
