from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .protocol import ProtocolError


@dataclass(frozen=True)
class Credential:
    value: str
    generation: str


class CredentialProvider(Protocol):
    def get(self, reference: str) -> Credential: ...


class EnvironmentCredentialProvider:
    """Development provider. Production can supply keychain/Hancock-backed providers."""

    def __init__(self, environ: dict[str, str]):
        self.environ = environ

    def get(self, reference: str) -> Credential:
        # Registry stores a stable lookup ref, never the secret or environment name.
        key = f"EMUX_CONTROLLER_CREDENTIAL_{reference.upper().replace('-', '_')}"
        value = self.environ.get(key)
        generation = self.environ.get(f"{key}_GENERATION")
        if not value or not generation:
            raise ProtocolError("credential_unavailable", "paired credential is missing or revoked")
        return Credential(value=value, generation=generation)
