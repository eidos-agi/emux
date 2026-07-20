"""EID-869: bare registrations must stay targetable by the remote controller API.

The emux-remote/1.0 target resolver matches {session, workspace=basename(cwd),
channel in channels}. A registry entry without cwd or channels is structurally
unreachable, so `emux register` must always stamp both.
"""

from __future__ import annotations

import argparse
import json

from emux import cli, server
from emux.channels import resolve_channels


def _register_args(name: str, session: str, channels: list[str] | None = None) -> argparse.Namespace:
    return argparse.Namespace(
        name=name,
        session=session,
        description="",
        tags=[],
        manages=[],
        channels=channels,
        linear_issue=None,
        linear_project=None,
        linear_team=None,
        acceptance=None,
    )


def test_bare_register_stamps_cwd_and_default_channel(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}")
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(cli, "_run_tmux", lambda *a, **k: (1, "", "no server"))
    monkeypatch.setattr(cli, "_start_stream_log", lambda *a, **k: None)
    monkeypatch.setattr(cli.channel_store, "suggest_channels", lambda reg: [])
    monkeypatch.setattr(cli.channel_store, "resolve_channels", lambda name, entry, defs=None: [])
    monkeypatch.chdir(tmp_path)

    assert cli.cmd_register(_register_args("bare", "bare-session")) == 0

    entry = json.loads(registry_path.read_text())["bare"]
    assert entry["channels"] == ["default"], "bare registration must be remotely targetable"
    assert entry["cwd"] == str(tmp_path), "cwd falls back to the caller's cwd when tmux is absent"


def test_register_prefers_live_pane_cwd(tmp_path, monkeypatch):
    registry_path = tmp_path / "registry.json"
    registry_path.write_text("{}")
    monkeypatch.setattr(server, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(cli, "_run_tmux", lambda *a, **k: (0, "/srv/worktree\n", ""))
    monkeypatch.setattr(cli, "_start_stream_log", lambda *a, **k: None)
    monkeypatch.setattr(cli.channel_store, "suggest_channels", lambda reg: [])

    assert cli.cmd_register(_register_args("live", "live-session")) == 0

    entry = json.loads(registry_path.read_text())["live"]
    assert entry["cwd"] == "/srv/worktree"


def test_resolve_channels_preserves_explicit_unknown_channels():
    # An explicitly requested channel without a stored definition must not be
    # silently dropped — that is exactly what made `-c default` vanish.
    out = resolve_channels("name", {"channels": ["default"]}, definitions={})
    assert out == ["default"]
