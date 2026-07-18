from __future__ import annotations

import asyncio
import json

import pytest

from emux.controller.client import call
from emux.controller.core import Controller
from emux.controller.credentials import Credential
from emux.controller.daemon import ControllerDaemon
from emux.controller.protocol import PROTOCOL_VERSION, ProtocolError
from emux.controller.registry import PairedServer, ServerRegistry
from emux.controller.remote import Capabilities


class Credentials:
    def __init__(self):
        self.revoked = set()

    def get(self, reference):
        if reference in self.revoked:
            raise ProtocolError("credential_unavailable", "revoked")
        return Credential(f"secret-{reference}", "g1")


class Remote:
    def __init__(self):
        self.online = {"alpha": True, "beta": True}
        self.protocol = {"alpha": "emux-remote/1.0", "beta": "emux-remote/1.0"}
        self.calls = []
        self.cancelled = []

    def capabilities(self, server, credential):
        if not self.online[server.alias]:
            raise ProtocolError("server_offline", "offline")
        return Capabilities(
            server.pinned_server_id,
            self.protocol[server.alias],
            "r1",
            frozenset({"session.send", "session.capture"}),
        )

    def execute(self, server, credential, request):
        self.calls.append((server.alias, request))
        return {
            "acknowledged": True,
            "request_id": request["request_id"],
            "outcome": "accepted",
            "receipt_id": f"remote-{request['request_id']}",
        }

    def cancel(self, server, credential, request_id):
        self.cancelled.append((server.alias, request_id))
        return {"acknowledged": True, "request_id": request_id, "outcome": "cancelled"}


@pytest.fixture
def rig(tmp_path):
    registry = ServerRegistry(tmp_path / "pairs.json")
    registry.save(
        [
            PairedServer("alpha", "https://alpha.invalid", "a", "emux-remote/1.0", "srv-a"),
            PairedServer("beta", "https://beta.invalid", "b", "emux-remote/1.0", "srv-b"),
        ]
    )
    creds, remote = Credentials(), Remote()
    controller = Controller(
        registry, creds, remote, tmp_path / "audit.jsonl", "uid-daniel", "dev-1"
    )
    return controller, creds, remote, tmp_path


def message(server="alpha", session="same", rid="req-1", action="session.send"):
    return {
        "protocol": PROTOCOL_VERSION,
        "op": "request",
        "request_id": rid,
        "target": {"server": server, "channel": "eng", "workspace": "emux", "session": session},
        "action": action,
        "parameters": {"text": "hello", "semantic_memory": "must-not-persist"},
    }


def test_duplicate_session_names_are_isolated_by_server(rig):
    c, _, r, _ = rig
    c.handle(message("alpha", rid="a1"))
    c.handle(message("beta", rid="b1"))
    assert [x[0] for x in r.calls] == ["alpha", "beta"]


def test_target_must_be_fully_qualified(rig):
    c, *_ = rig
    data = message()
    del data["target"]["workspace"]
    with pytest.raises(ProtocolError) as exc:
        c.handle(data)
    assert exc.value.code == "ambiguous_target"


def test_replay_is_rejected(rig):
    c, *_ = rig
    c.handle(message())
    with pytest.raises(ProtocolError) as exc:
        c.handle(message())
    assert exc.value.code == "replay"


def test_protocol_skew_and_stale_capabilities_fail_closed(rig):
    c, _, r, _ = rig
    r.protocol["alpha"] = "emux-remote/2.0"
    with pytest.raises(ProtocolError) as exc:
        c.handle(message())
    assert exc.value.code == "protocol_skew"
    r.protocol["alpha"] = "emux-remote/1.0"
    data = message(rid="req-2", action="future.action")
    with pytest.raises(ProtocolError) as exc:
        c.handle(data)
    assert exc.value.code == "unsupported_action"


def test_revoked_credential_and_offline_reconnect(rig):
    c, creds, r, _ = rig
    creds.revoked.add("a")
    with pytest.raises(ProtocolError) as exc:
        c.handle(message())
    assert exc.value.code == "credential_unavailable"
    creds.revoked.clear()
    r.online["alpha"] = False
    assert (
        c.handle({"protocol": PROTOCOL_VERSION, "op": "health"})["servers"][0]["state"] == "offline"
    )
    r.online["alpha"] = True
    assert (
        c.handle({"protocol": PROTOCOL_VERSION, "op": "health"})["servers"][0]["state"] == "online"
    )


def test_cancellation_targets_original(rig):
    c, _, r, _ = rig
    c.handle(message(rid="original"))
    data = message(rid="cancel-1")
    data.update({"op": "cancel", "cancel_request_id": "original"})
    assert c.handle(data)["receipt"]["outcome"] == "cancelled"
    assert r.cancelled == [("alpha", "original")]


def test_audit_attributable_and_no_semantic_memory(rig):
    c, _, _, p = rig
    c.handle(message())
    raw = (p / "audit.jsonl").read_text()
    record = json.loads(raw)
    assert (record["human_uid"], record["device_id"], record["request_id"]) == (
        "uid-daniel",
        "dev-1",
        "req-1",
    )
    assert "parameters" not in record
    assert "semantic_memory" not in raw


def test_listing_hides_credentials(rig):
    c, *_ = rig
    result = c.handle({"protocol": PROTOCOL_VERSION, "op": "servers.list"})
    assert "credential_ref" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_daemon_rejects_unauthorized_local_client(rig):
    c, _, _, p = rig
    sock = p / "controller.sock"
    daemon = ControllerDaemon(c, sock, "correct-token-with-at-least-32-characters")
    server = await asyncio.start_unix_server(daemon._client, path=sock)
    try:
        with pytest.raises(ProtocolError) as exc:
            await call(sock, "wrong-token-with-at-least-32-characters", {"op": "health"})
        assert exc.value.code == "unauthorized_client"
    finally:
        server.close()
        await server.wait_closed()
