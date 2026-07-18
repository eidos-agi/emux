from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .protocol import PROTOCOL_VERSION, ProtocolError


async def call(socket_path: Path, client_token: str, message: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(socket_path)
    try:
        payload = {"protocol": PROTOCOL_VERSION, **message, "client_token": client_token}
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        raw = await reader.readline()
        result = json.loads(raw)
        if not result.get("ok"):
            error = result.get("error") or {}
            raise ProtocolError(
                str(error.get("code", "controller_error")),
                str(error.get("message", "controller request failed")),
            )
        return result
    finally:
        writer.close()
        await writer.wait_closed()
