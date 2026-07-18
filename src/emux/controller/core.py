from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .credentials import CredentialProvider
from .protocol import PROTOCOL_VERSION, ProtocolError, Receipt, Target, request_id
from .registry import ServerRegistry
from .remote import RemoteExecutor


class Controller:
    def __init__(
        self,
        registry: ServerRegistry,
        credentials: CredentialProvider,
        remote: RemoteExecutor,
        audit_path: Path,
        human_uid: str,
        device_id: str,
    ):
        self.registry = registry
        self.credentials = credentials
        self.remote = remote
        self.audit_path = audit_path
        self.human_uid = human_uid
        self.device_id = device_id
        self._receipts: dict[str, Receipt] = {}

    def _audit(
        self, target: Target | None, action: str, outcome: str, rid: str, detail: str | None = None
    ) -> None:
        record = {
            "timestamp": time.time(),
            "human_uid": self.human_uid,
            "device_id": self.device_id,
            "server": target.server if target else None,
            "target": target.as_dict() if target else None,
            "action": action,
            "outcome": outcome,
            "request_id": rid,
            "detail": detail,
        }
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def handle(self, message: dict[str, Any]) -> dict[str, Any]:
        if message.get("protocol") != PROTOCOL_VERSION:
            raise ProtocolError("protocol_skew", f"required protocol is {PROTOCOL_VERSION}")
        op = message.get("op")
        if op == "health":
            return self._health()
        if op == "servers.list":
            return {"ok": True, "servers": [s.public() for s in self.registry.load().values()]}
        if op not in {"request", "cancel"}:
            raise ProtocolError("unknown_operation", "unsupported controller operation")
        rid = request_id(message.get("request_id"))
        target = Target.parse(message.get("target"))
        action = str(message.get("action") or "cancel")
        if rid in self._receipts:
            self._audit(target, action, "replay_rejected", rid)
            raise ProtocolError("replay", "request_id has already been used")
        servers = self.registry.load()
        server = servers.get(target.server)
        if server is None:
            raise ProtocolError("unknown_server", "target server is not paired")
        credential = self.credentials.get(server.credential_ref)
        caps = self.remote.capabilities(server, credential)
        if caps.server_id != server.pinned_server_id or caps.protocol != server.expected_protocol:
            raise ProtocolError(
                "protocol_skew", "remote identity or protocol does not match pairing"
            )
        if action not in caps.actions and op != "cancel":
            raise ProtocolError(
                "unsupported_action", "remote server does not advertise this action"
            )
        cancel_request_id: str | None = None
        if op == "cancel":
            cancel_request_id = message.get("cancel_request_id")
            if not isinstance(cancel_request_id, str) or cancel_request_id not in self._receipts:
                raise ProtocolError(
                    "unknown_request", "cancellation requires a known cancel_request_id"
                )
            result = self.remote.cancel(server, credential, cancel_request_id)
        else:
            result = self.remote.execute(
                server,
                credential,
                {
                    "protocol": server.expected_protocol,
                    "request_id": rid,
                    "human_uid": self.human_uid,
                    "device_id": self.device_id,
                    "target": target.as_dict(),
                    "action": action,
                    "parameters": message.get("parameters") or {},
                },
            )
        expected_ack = cancel_request_id if cancel_request_id is not None else rid
        if result.get("request_id") != expected_ack or not result.get("acknowledged"):
            raise ProtocolError(
                "invalid_acknowledgement", "remote did not acknowledge this request"
            )
        receipt = Receipt(
            rid,
            "acknowledged",
            str(result.get("outcome", "accepted")),
            server.alias,
            target.as_dict(),
            result.get("receipt_id"),
        )
        self._receipts[rid] = receipt
        self._audit(target, action, receipt.outcome, rid)
        return {"ok": True, "receipt": receipt.as_dict()}

    def _health(self) -> dict[str, Any]:
        states = []
        for server in self.registry.load().values():
            try:
                credential = self.credentials.get(server.credential_ref)
                caps = self.remote.capabilities(server, credential)
                state = (
                    "online"
                    if (
                        caps.server_id == server.pinned_server_id
                        and caps.protocol == server.expected_protocol
                    )
                    else "stale"
                )
                states.append({**server.public(), "state": state, "revision": caps.revision})
            except ProtocolError as exc:
                states.append({**server.public(), "state": "offline", "reason": exc.code})
        return {"ok": True, "protocol": PROTOCOL_VERSION, "servers": states}
