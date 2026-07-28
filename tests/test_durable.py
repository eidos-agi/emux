"""Product durable SSOT (EID-1167) — disk works with web down."""

from __future__ import annotations

import json
from pathlib import Path

from emux import durable


def test_seed_and_inventory_product_paths(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Point Path.home() via HOME; also force product via env
    monkeypatch.setenv("EMUX_PRODUCT", "amux")
    monkeypatch.delenv("EMUX_REGISTRY", raising=False)
    monkeypatch.delenv("EMUX_STATE", raising=False)

    shared = home / ".config" / "emux"
    shared.mkdir(parents=True)
    (shared / "registry.json").write_text(
        json.dumps({"northstar-iran-daily": {"session": "northstar-iran-daily", "tags": []}}) + "\n",
        encoding="utf-8",
    )

    # Redirect config roots under tmp home by patching product_config
    monkeypatch.setattr(durable, "_config_root", lambda product=None: home / ".config" / (product or "amux"))
    monkeypatch.setattr(
        durable,
        "state_dir",
        lambda product=None: home / ".local" / "state" / (product or "amux"),
    )
    monkeypatch.setattr(durable, "shared_emux_registry", lambda: shared / "registry.json")

    seeded = durable.seed_product_store("amux")
    assert seeded["ok"] is True
    reg = Path(seeded["registry"])
    assert reg.is_file()
    data = json.loads(reg.read_text(encoding="utf-8"))
    assert "northstar-iran-daily" in data

    inv = durable.inventory("amux")
    assert inv["product"] == "amux"
    assert inv["registry"]["exists"] is True
    assert inv["registry"]["entries"] == 1
    assert "disk + tmux" in inv["ssot"]


def test_apply_env_sets_registry_and_state(monkeypatch, tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("EMUX_PRODUCT", "amux")
    monkeypatch.delenv("EMUX_REGISTRY", raising=False)
    monkeypatch.delenv("EMUX_STATE", raising=False)
    monkeypatch.setattr(durable, "_config_root", lambda product=None: home / ".config" / "amux")
    monkeypatch.setattr(durable, "state_dir", lambda product=None: home / ".local" / "state" / "amux")
    monkeypatch.setattr(durable, "shared_emux_registry", lambda: home / "missing.json")

    out = durable.apply_env_for_product("amux")
    assert "EMUX_REGISTRY" in out
    assert out["EMUX_REGISTRY"].endswith("registry.json")
    assert Path(out["EMUX_REGISTRY"]).is_file()
    assert Path(out["EMUX_STATE"]).is_dir()


def test_seed_copies_missing_historical_state(monkeypatch, tmp_path):
    home = tmp_path / "home"
    shared = home / ".local" / "state" / "emux"
    prod = home / ".local" / "state" / "amux"
    shared.mkdir(parents=True)
    (shared / "audit.jsonl").write_text('{"op":"old"}\n', encoding="utf-8")
    (shared / "signals.jsonl").write_text('{"t":1}\n', encoding="utf-8")
    (shared / "logs").mkdir()
    (shared / "logs" / "s1.log").write_text("pane\n", encoding="utf-8")
    (shared / "inbox").mkdir()
    (shared / "inbox" / "s1.jsonl").write_text('{"k":"IDLE"}\n', encoding="utf-8")

    monkeypatch.setenv("EMUX_PRODUCT", "amux")
    monkeypatch.setattr(durable, "_config_root", lambda product=None: home / ".config" / "amux")
    monkeypatch.setattr(durable, "state_dir", lambda product=None: prod)
    monkeypatch.setattr(durable, "shared_emux_state", lambda: shared)
    monkeypatch.setattr(durable, "shared_emux_registry", lambda: home / "no-reg.json")

    out = durable.seed_product_store("amux")
    assert "audit.jsonl" in out["state_files_copied"]
    assert out["logs_copied"] == 1
    assert out["inbox_copied"] == 1
    assert (prod / "audit.jsonl").read_text(encoding="utf-8").startswith('{"op":"old"}')
    assert (prod / "logs" / "s1.log").is_file()
    # second seed must not overwrite / re-copy
    (prod / "audit.jsonl").write_text('{"op":"product"}\n', encoding="utf-8")
    out2 = durable.seed_product_store("amux")
    assert out2["state_files_copied"] == []
    assert (prod / "audit.jsonl").read_text(encoding="utf-8").startswith('{"op":"product"}')
