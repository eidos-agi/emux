from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from .core import Controller
from .protocol import ProtocolError


def load_or_create_client_token(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise ProtocolError("insecure_token_file", "client token file must be mode 0600")
        token = path.read_text().strip()
        if len(token) < 32:
            raise ProtocolError("invalid_client_token", "client token is invalid")
        return token
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token + "\n")
    return token


class ControllerDaemon:
    def __init__(self, controller: Controller, socket_path: Path, client_token: str):
        self.controller = controller
        self.socket_path = socket_path
        self.client_token = client_token

    async def serve(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = await asyncio.start_unix_server(self._client, path=self.socket_path)
        os.chmod(self.socket_path, 0o600)
        async with server:
            await server.serve_forever()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                response = self._dispatch(line)
                writer.write(json.dumps(response, sort_keys=True).encode() + b"\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def _dispatch(self, line: bytes) -> dict[str, Any]:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ProtocolError("invalid_message", "message must be an object")
            token = message.pop("client_token", None)
            if not isinstance(token, str) or not hmac.compare_digest(token, self.client_token):
                raise ProtocolError("unauthorized_client", "local client authentication failed")
            return self.controller.handle(message)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"ok": False, "error": {"code": "invalid_json", "message": "invalid JSON"}}
        except ProtocolError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
