from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .protocol import ProtocolError


@dataclass(frozen=True)
class PairedServer:
    alias: str
    endpoint: str
    credential_ref: str
    expected_protocol: str
    pinned_server_id: str

    def public(self) -> dict[str, str]:
        # credential_ref is an opaque local lookup key, not client-visible metadata.
        return {
            "alias": self.alias,
            "endpoint": self.endpoint,
            "expected_protocol": self.expected_protocol,
            "pinned_server_id": self.pinned_server_id,
        }


class ServerRegistry:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, PairedServer]:
        if not self.path.exists():
            return {}
        data = json.loads(self.path.read_text())
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("servers"), list)
        ):
            raise ProtocolError(
                "invalid_registry", "paired-server registry has an unsupported shape"
            )
        out: dict[str, PairedServer] = {}
        for raw in data["servers"]:
            if not isinstance(raw, dict):
                raise ProtocolError("invalid_registry", "server record must be an object")
            try:
                item = PairedServer(**{k: raw[k] for k in PairedServer.__dataclass_fields__})
            except (KeyError, TypeError) as exc:
                raise ProtocolError("invalid_registry", "server record is incomplete") from exc
            if item.alias in out:
                raise ProtocolError("ambiguous_server", f"duplicate server alias: {item.alias}")
            out[item.alias] = item
        return out

    def save(self, servers: list[PairedServer]) -> None:
        aliases = [s.alias for s in servers]
        if len(aliases) != len(set(aliases)):
            raise ProtocolError("ambiguous_server", "server aliases must be unique")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "servers": [s.__dict__ for s in servers]}
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)
