"""Authoritative, versioned remote-control surface for an Emux server.

The local controller transports requests; this module validates identity and
targets and is the only component which executes them.  It intentionally owns
only operational receipts, never semantic memory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .protocol import REMOTE_PROTOCOL, ProtocolError, Target, request_id


@dataclass(frozen=True)
class TrustedIdentity:
    human_uid: str
    device_id: str
    controller_id: str


class TrustedBoundary(Protocol):
    def authenticate(self, headers: Mapping[str, str]) -> TrustedIdentity: ...


class HancockGate(Protocol):
    def authorize(
        self, identity: TrustedIdentity, action: str, target: Target, request_id: str
    ) -> str: ...


class DenyBoundary:
    def authenticate(self, headers: Mapping[str, str]) -> TrustedIdentity:
        raise ProtocolError("unauthorized", "controller boundary is not configured")


class StaticTokenBoundary:
    """Bind one revocable controller credential to one immutable identity."""

    def __init__(self, token: str, identity: TrustedIdentity):
        if len(token) < 32:
            raise ValueError("controller token must contain at least 32 characters")
        self.token = token
        self.identity = identity

    def authenticate(self, headers: Mapping[str, str]) -> TrustedIdentity:
        supplied = headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(supplied, expected):
            raise ProtocolError("unauthorized", "controller credential is invalid")
        return self.identity


class DenyConsequentialGate:
    def authorize(
        self, identity: TrustedIdentity, action: str, target: Target, request_id: str
    ) -> str:
        return "deny"


@dataclass(frozen=True)
class RemoteConfig:
    server_id: str
    aliases: frozenset[str]
    revision: str
    state_path: Path


class RemoteControllerAPI:
    ACTIONS = frozenset({"session.capture", "session.send", "session.interrupt"})
    # session.send is transport, not authorization: it is the paired human typing
    # into their own authenticated session. Consequences are governed by the
    # receiving agent's permission system, exactly as with local typing.
    # Approval gates apply only to controller actions that themselves create
    # consequences (answering a modal, killing sessions, deploys, credential or
    # permission changes, external publishing). Decision:
    # cockpit-eidos/decisions/2026-07-20-send-is-transport-not-authorization.md
    CONSEQUENTIAL = frozenset({"session.interrupt"})

    def __init__(
        self,
        config: RemoteConfig,
        boundary: TrustedBoundary,
        gate: HancockGate,
        registry: Callable[[], dict[str, dict[str, Any]]],
        capture: Callable[[str, int], dict[str, Any]],
        send: Callable[[str, str, bool, bool], dict[str, Any]],
        clock: Callable[[], float] = time.time,
        gate_probe: Callable[[str], dict[str, Any]] | None = None,
    ):
        if not config.server_id or not config.aliases:
            raise ValueError("server identity and at least one explicit alias are required")
        self.config, self.boundary, self.gate = config, boundary, gate
        self.registry, self.capture, self.send, self.clock = registry, capture, send, clock
        # Production wiring MUST pass the real gate probe (_gate_snapshot):
        # transport never answers or bypasses a visible permission gate.
        self.gate_probe = gate_probe or (lambda session: {"ok": False, "error": "no_active_gate"})
        self._lock = threading.Lock()
        self._state = self._load()

    def capabilities(self, headers: Mapping[str, str]) -> dict[str, Any]:
        self.boundary.authenticate(headers)
        return {
            "server_id": self.config.server_id,
            "protocol": REMOTE_PROTOCOL,
            "revision": self.config.revision,
            "actions": sorted(self.ACTIONS),
        }

    def submit(
        self, headers: Mapping[str, str], message: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self.boundary.authenticate(headers)
        self._validate_envelope(identity, message)
        rid = request_id(message.get("request_id"))
        nonce = self._token(message.get("nonce"), "invalid_nonce")
        target = Target.parse(message.get("target"))
        action = str(message.get("action") or "")
        if action not in self.ACTIONS:
            raise ProtocolError("unsupported_action", "action is not advertised")
        self._validate_target(target)
        params = message.get("parameters") or {}
        if not isinstance(params, dict):
            raise ProtocolError("invalid_parameters", "parameters must be an object")
        self._validate_parameters(action, params)
        fingerprint = self._fingerprint(identity, target, action, params, nonce)
        with self._lock:
            previous = self._state["requests"].get(rid)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ProtocolError("replay", "request_id was reused with different content")
                return 200, self._public(previous)
            if nonce in self._state["nonces"]:
                raise ProtocolError("replay", "nonce has already been used")
            record = {
                "request_id": rid,
                "fingerprint": fingerprint,
                "nonce": nonce,
                "human_uid": identity.human_uid,
                "device_id": identity.device_id,
                "controller_id": identity.controller_id,
                "target": target.as_dict(),
                "action": action,
                "status": "accepted",
                "outcome": "accepted",
                "created_at": self.clock(),
            }
            self._state["requests"][rid] = record
            self._state["nonces"][nonce] = rid
            self._save()

        if action in self.CONSEQUENTIAL:
            decision = self.gate.authorize(identity, action, target, rid)
            if decision not in {"allow", "pending", "deny"}:
                decision = "deny"
            if decision != "allow":
                return self._finish(rid, "pending" if decision == "pending" else "denied")
        return self._execute(rid, target, action, params)

    def status(self, headers: Mapping[str, str], rid: str) -> tuple[int, dict[str, Any]]:
        identity = self.boundary.authenticate(headers)
        with self._lock:
            record = self._state["requests"].get(rid)
            if (
                not record
                or record["human_uid"] != identity.human_uid
                or record["device_id"] != identity.device_id
            ):
                raise ProtocolError("unknown_request", "request is not visible to this identity")
            return 200, self._public(record)

    def cancel(
        self, headers: Mapping[str, str], rid: str, message: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self.boundary.authenticate(headers)
        if message.get("protocol") != REMOTE_PROTOCOL:
            raise ProtocolError("protocol_skew", f"required protocol is {REMOTE_PROTOCOL}")
        nonce = self._token(message.get("nonce"), "invalid_nonce")
        issued_at = message.get("issued_at")
        if not isinstance(issued_at, (int, float)) or abs(self.clock() - float(issued_at)) > 300:
            raise ProtocolError("stale_request", "issued_at must be within five minutes")
        with self._lock:
            record = self._state["requests"].get(rid)
            if (
                not record
                or record["human_uid"] != identity.human_uid
                or record["device_id"] != identity.device_id
            ):
                raise ProtocolError(
                    "unknown_request", "request is not cancellable by this identity"
                )
            if nonce in self._state["nonces"]:
                raise ProtocolError("replay", "nonce has already been used")
            if record["status"] == "cancelled":
                return 200, self._public(record)
            if record["status"] != "pending":
                raise ProtocolError("not_cancellable", "only pending requests can be cancelled")
            self._state["nonces"][nonce] = f"cancel:{rid}"
            record.update(status="cancelled", outcome="cancelled", completed_at=self.clock())
            self._save()
            return 200, self._public(record)

    def _validate_envelope(self, identity: TrustedIdentity, message: dict[str, Any]) -> None:
        if message.get("protocol") != REMOTE_PROTOCOL:
            raise ProtocolError("protocol_skew", f"required protocol is {REMOTE_PROTOCOL}")
        if (
            message.get("human_uid") != identity.human_uid
            or message.get("device_id") != identity.device_id
        ):
            raise ProtocolError(
                "identity_mismatch", "body identity does not match trusted boundary"
            )
        ts = message.get("issued_at")
        if not isinstance(ts, (int, float)) or abs(self.clock() - float(ts)) > 300:
            raise ProtocolError("stale_request", "issued_at must be within five minutes")

    @staticmethod
    def _token(value: Any, code: str) -> str:
        if not isinstance(value, str) or not 16 <= len(value) <= 256:
            raise ProtocolError(code, "token must contain 16 to 256 characters")
        return value

    def _validate_target(self, target: Target) -> None:
        if target.server not in self.config.aliases:
            raise ProtocolError("wrong_server", "target names a different server")
        matches = []
        for _name, entry in self.registry().items():
            if entry.get("host"):
                continue
            session = str(entry.get("session") or "")
            workspace = str(entry.get("workspace") or Path(str(entry.get("cwd") or "")).name)
            channels = {str(x) for x in entry.get("channels") or []}
            if (
                session == target.session
                and workspace == target.workspace
                and target.channel in channels
            ):
                matches.append(entry)
        if len(matches) != 1:
            raise ProtocolError(
                "unknown_target", "target must resolve to one registered local session"
            )

    @staticmethod
    def _validate_parameters(action: str, params: dict[str, Any]) -> None:
        allowed = {
            "session.capture": {"lines"},
            "session.send": {"text", "enter"},
            "session.interrupt": set(),
        }[action]
        if set(params) - allowed:
            raise ProtocolError("invalid_parameters", "unknown action parameters")
        if action == "session.capture" and (
            not isinstance(params.get("lines", 300), int)
            or not 1 <= params.get("lines", 300) <= 5000
        ):
            raise ProtocolError("invalid_parameters", "lines must be between 1 and 5000")
        if action == "session.send":
            if not isinstance(params.get("text"), str) or not params["text"]:
                raise ProtocolError("invalid_parameters", "text is required")
            if "enter" in params and not isinstance(params["enter"], bool):
                raise ProtocolError("invalid_parameters", "enter must be boolean")

    def _execute(
        self, rid: str, target: Target, action: str, params: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if action == "session.capture":
            lines = params.get("lines", 300)
            if not isinstance(lines, int) or not 1 <= lines <= 5000:
                raise ProtocolError("invalid_parameters", "lines must be between 1 and 5000")
            result = self.capture(target.session, lines)
        elif action == "session.send":
            text = params.get("text")
            if not isinstance(text, str) or not text:
                raise ProtocolError("invalid_parameters", "text is required")
            # Never inspect or classify the text itself — delivery is content-blind.
            # But a visible permission gate must never be answered or bypassed by
            # transport, and ambiguity (gone session, failed probe) fails closed.
            probe = self.gate_probe(target.session)
            if probe.get("ok"):
                return self._finish(rid, "denied", {"ok": False, "error": "gated_session"})
            if probe.get("error") != "no_active_gate":
                return self._finish(
                    rid, "denied", {"ok": False, "error": str(probe.get("error") or "gate_probe_failed")}
                )
            result = self.send(target.session, text, True, bool(params.get("enter", True)))
        else:
            result = self.send(target.session, "C-c", False, False)
        outcome = "completed" if result.get("ok") else "failed"
        # Return execution data transiently; never persist pane content or sent text.
        return self._finish(rid, outcome, result)

    def _finish(
        self, rid: str, outcome: str, result: dict[str, Any] | None = None
    ) -> tuple[int, dict[str, Any]]:
        with self._lock:
            record = self._state["requests"][rid]
            record.update(status=outcome, outcome=outcome)
            if outcome != "pending":
                record["completed_at"] = self.clock()
            self._save()
            payload = self._public(record)
            if result is not None:
                payload["result"] = result
            return (202 if outcome == "pending" else 200), payload

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            k: v
            for k, v in record.items()
            if k not in {"fingerprint", "nonce", "human_uid", "device_id", "controller_id"}
        } | {"acknowledged": True, "receipt_id": f"remote-{record['request_id']}"}

    @staticmethod
    def _fingerprint(
        identity: TrustedIdentity, target: Target, action: str, params: dict[str, Any], nonce: str
    ) -> str:
        value = [identity.human_uid, identity.device_id, target.as_dict(), action, params, nonce]
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.config.state_path.read_text())
            if isinstance(data.get("requests"), dict) and isinstance(data.get("nonces"), dict):
                return data
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return {"requests": {}, "nonces": {}}

    def _save(self) -> None:
        path = self.config.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(self._state, handle, sort_keys=True)
        tmp.replace(path)
