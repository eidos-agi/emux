from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from .credentials import Credential
from .protocol import ProtocolError
from .registry import PairedServer


@dataclass(frozen=True)
class Capabilities:
    server_id: str
    protocol: str
    revision: str
    actions: frozenset[str]


class RemoteExecutor(Protocol):
    def capabilities(self, server: PairedServer, credential: Credential) -> Capabilities: ...
    def execute(
        self, server: PairedServer, credential: Credential, request: dict[str, Any]
    ) -> dict[str, Any]: ...
    def cancel(
        self, server: PairedServer, credential: Credential, request_id: str
    ) -> dict[str, Any]: ...


class HttpRemoteExecutor:
    """Versioned HTTPS adapter. The remote Emux API remains the sole executor."""

    def _call(
        self,
        server: PairedServer,
        credential: Credential,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = server.endpoint.rstrip("/") + path
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method="GET" if body is None else "POST")
        req.add_header("Authorization", f"Bearer {credential.value}")
        req.add_header("Accept", "application/json")
        req.add_header("X-Emux-Credential-Generation", credential.generation)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.load(response)
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise ProtocolError(
                "server_offline", f"remote Emux unavailable: {server.alias}"
            ) from exc
        if not isinstance(result, dict):
            raise ProtocolError("invalid_remote_response", "remote response must be an object")
        return result

    def capabilities(self, server: PairedServer, credential: Credential) -> Capabilities:
        raw = self._call(server, credential, "/api/controller/v1/capabilities")
        try:
            return Capabilities(
                str(raw["server_id"]),
                str(raw["protocol"]),
                str(raw["revision"]),
                frozenset(raw["actions"]),
            )
        except (KeyError, TypeError) as exc:
            raise ProtocolError(
                "invalid_capabilities", "remote capabilities are incomplete"
            ) from exc

    def execute(
        self, server: PairedServer, credential: Credential, request: dict[str, Any]
    ) -> dict[str, Any]:
        return self._call(server, credential, "/api/controller/v1/requests", request)

    def cancel(
        self, server: PairedServer, credential: Credential, request_id: str
    ) -> dict[str, Any]:
        return self._call(
            server, credential, f"/api/controller/v1/requests/{request_id}/cancel", {}
        )
