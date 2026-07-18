from __future__ import annotations

import argparse
import asyncio
import os
import socket
from pathlib import Path

from .core import Controller
from .credentials import EnvironmentCredentialProvider
from .daemon import ControllerDaemon, load_or_create_client_token
from .registry import ServerRegistry
from .remote import HttpRemoteExecutor


def main() -> None:
    parser = argparse.ArgumentParser(description="Per-user local Emux control gateway")
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local" / "state" / "emux-controller",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path.home() / ".config" / "emux-controller",
    )
    parser.add_argument("--human-uid", default=os.environ.get("EMUX_CONTROLLER_HUMAN_UID"))
    parser.add_argument(
        "--device-id", default=os.environ.get("EMUX_CONTROLLER_DEVICE_ID", socket.gethostname())
    )
    args = parser.parse_args()
    if not args.human_uid:
        parser.error("--human-uid or EMUX_CONTROLLER_HUMAN_UID is required")
    token = load_or_create_client_token(args.state_dir / "client.token")
    controller = Controller(
        ServerRegistry(args.config_dir / "paired-servers.json"),
        EnvironmentCredentialProvider(dict(os.environ)),
        HttpRemoteExecutor(),
        args.state_dir / "audit.jsonl",
        args.human_uid,
        args.device_id,
    )
    asyncio.run(ControllerDaemon(controller, args.state_dir / "controller.sock", token).serve())


if __name__ == "__main__":
    main()
