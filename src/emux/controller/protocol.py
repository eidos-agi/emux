from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

PROTOCOL_VERSION = "emux-controller/1.0"
REMOTE_PROTOCOL = "emux-remote/1.0"


class ProtocolError(ValueError):
    """A fail-closed protocol error with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Target:
    server: str
    channel: str
    workspace: str
    session: str

    @classmethod
    def parse(cls, value: Any) -> Target:
        if not isinstance(value, dict):
            raise ProtocolError("invalid_target", "target must be an object")
        fields = ("server", "channel", "workspace", "session")
        if any(not isinstance(value.get(k), str) or not value[k].strip() for k in fields):
            raise ProtocolError(
                "ambiguous_target", "server, channel, workspace and session are required"
            )
        return cls(*(value[k].strip() for k in fields))

    def as_dict(self) -> dict[str, str]:
        return {
            "server": self.server,
            "channel": self.channel,
            "workspace": self.workspace,
            "session": self.session,
        }


@dataclass
class Receipt:
    request_id: str
    status: str
    outcome: str
    server: str
    target: dict[str, str]
    remote_receipt: str | None = None
    timestamp: float = field(default_factory=time.time)
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def request_id(value: Any) -> str:
    if value is None:
        return str(uuid.uuid4())
    if not isinstance(value, str) or not value or len(value) > 128:
        raise ProtocolError("invalid_request_id", "request_id must be a non-empty string")
    return value
