from __future__ import annotations

import asyncio


def _paths(channels, tmp_path, monkeypatch):
    monkeypatch.setattr(channels, "CHANNELS_PATH", tmp_path / "channels.json")
    monkeypatch.setattr(channels, "CHANNEL_OKF_DIR", tmp_path / "channel-okf")
    monkeypatch.setattr(channels, "CHANNEL_NOTES_DIR", tmp_path / "notes")
    monkeypatch.setattr(channels, "CHANNEL_INFORMED_PATH", tmp_path / "informed.json")


def test_tiered_matching_inherits_parents_and_canon(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("canon", 0, "Daniel-wide rules")
    channels.create_channel("tally", 1, "Personal finance", "canon", ["tally", "personal-finance"])
    channels.create_channel("bills", 2, "Bills work", "tally", ["bills"])

    entry = {"session": "x", "description": "Tally bills cleanup", "tags": []}
    assert channels.resolve_channels("worker", entry) == ["canon", "tally", "bills"]


def test_channel_and_learning_log_are_valid_okf_shape(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("rvs", 1, "RVS operations", matchers=["rvs"])
    channels.append_note("rvs", "policy", "Linear owns recurrence.\n## not-a-date", "daniel")

    bundle = channels.CHANNEL_OKF_DIR / "rvs"
    assert (bundle / "index.md").read_text().startswith('---\nokf_version: "0.1"\n---')
    assert (bundle / "channel.md").read_text().startswith("---\ntype: emux-channel\n")
    log = (bundle / "log.md").read_text()
    assert "Linear owns recurrence. ## not-a-date" in log
    assert "\n## not-a-date" not in log


def test_suggests_repeated_meaningful_tag(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    registry = {
        "a": {"tags": ["personal-finance", "worker"]},
        "b": {"tags": ["personal-finance", "manager"]},
    }
    assert channels.suggest_channels(registry) == [
        {"name": "personal-finance", "tier": 1, "reason": "tag appears on 2 sessions"}
    ]


def test_refresh_backfills_existing_registry_entries(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("canon", 0, "Rules")
    channels.create_channel("tally", 1, "Finance", "canon", ["dally"])
    registry = {"dally-worker": {"session": "x", "tags": []}}
    assert channels.refresh_registry(registry) == ["dally-worker"]
    assert registry["dally-worker"]["channels"] == ["canon", "tally"]


def test_agent_context_is_injected_once_until_learning_changes(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("tally", 1, "Personal finance", matchers=["tally"])
    registry = {"tally-worker": {"session": "x", "description": "", "tags": []}}

    first = channels.agent_prelude_once("tally-worker", registry)
    assert "T1 tally" in first
    assert channels.agent_prelude_once("tally-worker", registry) == ""

    channels.append_note("tally", "failure", "Do not ask Dally to reason.", "session-1")
    changed = channels.agent_prelude_once("tally-worker", registry)
    assert "Do not ask Dally to reason." in changed


def test_channel_notes_redact_financial_identifiers(tmp_path, monkeypatch):
    from emux import channels

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("tally", 1, "Personal finance")
    note = channels.append_note(
        "tally", "fact", "account_id abcdefghijklmnop and card 4111111111111111"
    )
    assert "abcdefghijklmnop" not in note["text"]
    assert "4111111111111111" not in note["text"]


def test_tmux_ask_injects_changed_context_only_once(tmp_path, monkeypatch):
    from emux import channels, server

    _paths(channels, tmp_path, monkeypatch)
    channels.create_channel("tally", 1, "Personal finance", matchers=["tally"])
    monkeypatch.setattr(server, "REGISTRY_PATH", tmp_path / "registry.json")
    monkeypatch.setattr(server, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    server._save_registry({"tally-agent": {"session": "x", "tags": []}})

    def fake_converse(_target, prompt, *_args):
        return {"ok": True, "seen_prompt": prompt}

    monkeypatch.setattr(server, "converse", fake_converse)
    first = asyncio.run(server.tmux_ask("tally-agent", "do work", by_registry_name=True))
    second = asyncio.run(server.tmux_ask("tally-agent", "more work", by_registry_name=True))

    assert "EMUX CHANNEL CONTEXT" in first["seen_prompt"]
    assert first["channel_context_injected"] is True
    assert second["seen_prompt"] == "more work"
    assert second["channel_context_injected"] is False
