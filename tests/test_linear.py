from __future__ import annotations

import asyncio
import json


def _paths(server, channels, linear, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(server, "_SIGNAL_LEDGER", tmp_path / "signals.jsonl")
    monkeypatch.setattr(server, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(server, "_INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(server, "_SIGNAL_OFFSETS", tmp_path / "offsets.json")
    monkeypatch.setattr(server, "_SIGNAL_SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(channels, "CHANNELS_PATH", tmp_path / "channels.json")
    monkeypatch.setattr(channels, "CHANNEL_NOTES_DIR", tmp_path / "channel-notes")
    monkeypatch.setattr(channels, "CHANNEL_INFORMED_PATH", tmp_path / "informed.json")
    monkeypatch.setattr(linear, "EVIDENCE_PATH", tmp_path / "linear-evidence.jsonl")


def test_linear_metadata_auto_tags_channel_and_enters_agent_context(tmp_path, monkeypatch):
    from emux import channels

    monkeypatch.setattr(channels, "CHANNELS_PATH", tmp_path / "channels.json")
    monkeypatch.setattr(channels, "CHANNEL_NOTES_DIR", tmp_path / "notes")
    channels.create_channel("rvs", 1, "RVS team", matchers=["rvs"])
    registry = {
        "worker": {
            "session": "w",
            "linear": {
                "issue": "RVS-12",
                "project": "Recurring Operations",
                "team": "RVS",
                "acceptance": ["report is attached"],
            },
        }
    }

    assert channels.resolve_channels("worker", registry["worker"]) == ["rvs"]
    context = channels.session_context("worker", registry)
    assert "issue=RVS-12" in context
    assert "acceptance 1: report is attached" in context
    assert "DONE is a worker claim, not closure" in context


def test_manager_reconciliation_requires_done_and_all_evidence(tmp_path, monkeypatch):
    from emux import channels, linear, server

    _paths(server, channels, linear, tmp_path, monkeypatch)
    server._save_registry({"child": {"session": "child-tmux", "tags": []}})
    linked = asyncio.run(
        server.tmux_linear_link(
            "child", "RVS-42", "Recurring Ops", "RVS", ["tests pass", "artifact exists"]
        )
    )
    assert linked["ok"] is True

    server._SIGNAL_LEDGER.write_text(
        json.dumps({"t": 1, "session": "child", "kind": "PROGRESS", "payload": "working"})
        + "\n"
        + json.dumps({"t": 1, "session": "child", "kind": "DONE", "payload": "finished"})
        + "\n"
    )
    first = asyncio.run(server.tmux_linear_status("child"))
    assert first["work"][0]["state"] == "evidence_missing"
    assert first["work"][0]["recommended_linear_status"] is None

    asyncio.run(server.tmux_linear_evidence("child", 1, "pytest: 214 passed", "manager"))
    asyncio.run(server.tmux_linear_evidence("child", 2, "dist/emux wheel exists", "manager"))
    ready = asyncio.run(server.tmux_linear_status("child"))
    row = ready["work"][0]
    assert row["state"] == "ready_for_review"
    assert row["recommended_linear_status"] == "In Review"
    assert "Closed" not in json.dumps(ready)


def test_register_accepts_linear_contract(tmp_path, monkeypatch):
    from emux import channels, linear, server

    _paths(server, channels, linear, tmp_path, monkeypatch)
    monkeypatch.setattr(server, "_session_exists", lambda *_args, **_kwargs: False)
    result = asyncio.run(
        server.tmux_register(
            "child",
            "tmux-child",
            linear_issue="RVS-9",
            linear_team="RVS",
            acceptance_criteria=["receipt uploaded"],
        )
    )
    assert result["entry"]["linear"]["issue"] == "RVS-9"
    assert result["entry"]["linear"]["acceptance"] == ["receipt uploaded"]


def test_spawn_validates_linear_contract_before_touching_tmux(monkeypatch):
    from emux import server

    calls = []
    monkeypatch.setattr(server, "_run_tmux", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = asyncio.run(
        server.tmux_spawn("child", linear_project="Recurring Ops", acceptance_criteria=["proof"])
    )
    assert result == {"ok": False, "error": "linear_issue is required with Linear metadata"}
    assert calls == []
