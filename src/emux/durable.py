"""Product durable state — disk is SSOT; the web process is optional.

When the web daemon restarts or is down, nothing important should live only in
memory. Product-scoped paths (amux → ~/.config/amux, ~/.local/state/amux) hold:

  registry.json     session metadata (names, tags, linear, …)
  chats.db          abandoned chat index
  schedule.json     cron jobs + last_run markers
  schedule-log.jsonl fire receipts
  missions/         mission briefs (emux new)
  logs/missions.jsonl mission ledger
  state/            stream logs, signals, audit, inbox, index

Live agents live in **tmux** (independent of emux web). The room UI is a
projection: disk + tmux → browser.

Env overrides (highest priority):
  EMUX_REGISTRY, EMUX_STATE, EMUX_PRODUCT / EMUX_SKIN
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any


def product_id() -> str:
    env = (os.environ.get("EMUX_PRODUCT") or os.environ.get("EMUX_SKIN") or "").strip().lower()
    if env:
        return env
    try:
        from .product_config import _product_id

        return _product_id()
    except Exception:
        return "emux"


def _config_root(product: str | None = None) -> Path:
    pid = (product or product_id()).strip().lower() or "emux"
    try:
        from .product_config import config_dir_for

        return config_dir_for(pid)
    except Exception:
        if pid in ("gmux", "greenmux", "greenmark"):
            return Path.home() / ".config" / "greenmux"
        if pid in ("", "emux"):
            return Path.home() / ".config" / "emux"
        return Path.home() / ".config" / pid


def _state_name(product: str | None = None) -> str:
    pid = (product or product_id()).strip().lower() or "emux"
    if pid in ("gmux", "greenmux", "greenmark"):
        return "greenmux"
    if pid in ("directmux", "direct-mux"):
        return "directrux"
    if pid in ("", "emux"):
        return "emux"
    return pid


def registry_path(product: str | None = None) -> Path:
    env = (os.environ.get("EMUX_REGISTRY") or os.environ.get("TMUX_MCP_REGISTRY") or "").strip()
    if env:
        return Path(env).expanduser()
    return _config_root(product) / "registry.json"


def state_dir(product: str | None = None) -> Path:
    env = (os.environ.get("EMUX_STATE") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".local" / "state" / _state_name(product)


def shared_emux_registry() -> Path:
    return Path.home() / ".config" / "emux" / "registry.json"


def shared_emux_state() -> Path:
    return Path.home() / ".local" / "state" / "emux"


def seed_product_store(product: str | None = None) -> dict[str, Any]:
    """Ensure product config/state dirs exist; seed registry from shared emux once.

    Safe to call repeatedly. Does not overwrite a non-empty product registry.
    """
    pid = (product or product_id()).strip().lower() or "emux"
    cfg = _config_root(pid)
    st = state_dir(pid)
    cfg.mkdir(parents=True, exist_ok=True)
    st.mkdir(parents=True, exist_ok=True)
    (cfg / "missions").mkdir(parents=True, exist_ok=True)
    (cfg / "logs").mkdir(parents=True, exist_ok=True)
    (st / "logs").mkdir(parents=True, exist_ok=True)
    (st / "inbox").mkdir(parents=True, exist_ok=True)

    reg = registry_path(pid)
    seeded_reg = False
    if not reg.is_file() and pid not in ("emux", ""):
        shared = shared_emux_registry()
        if shared.is_file() and shared.resolve() != reg.resolve():
            shutil.copy2(shared, reg)
            seeded_reg = True
        else:
            reg.write_text("{}\n", encoding="utf-8")
            seeded_reg = True

    return {
        "ok": True,
        "product": pid,
        "config_dir": str(cfg),
        "state_dir": str(st),
        "registry": str(reg),
        "registry_seeded": seeded_reg,
        "registry_exists": reg.is_file(),
        "registry_bytes": reg.stat().st_size if reg.is_file() else 0,
    }


def apply_env_for_product(product: str | None = None) -> dict[str, str]:
    """Set EMUX_REGISTRY / EMUX_STATE for this process when not already set.

    Product wrappers (amux) and launchd should export these; this is the library
    equivalent so CLI/tools inherit product-owned paths without a full reinstall.
    """
    pid = (product or product_id()).strip().lower() or "emux"
    seed_product_store(pid)
    out: dict[str, str] = {"EMUX_PRODUCT": pid, "EMUX_SKIN": pid}
    if not (os.environ.get("EMUX_REGISTRY") or "").strip():
        os.environ["EMUX_REGISTRY"] = str(registry_path(pid))
        out["EMUX_REGISTRY"] = os.environ["EMUX_REGISTRY"]
    if not (os.environ.get("EMUX_STATE") or "").strip():
        os.environ["EMUX_STATE"] = str(state_dir(pid))
        out["EMUX_STATE"] = os.environ["EMUX_STATE"]
    # Rebind server module paths if already imported (tests / long-lived processes).
    try:
        from . import server as _server

        _server.rebind_durable_paths()
    except Exception:
        pass
    return out


def inventory(product: str | None = None) -> dict[str, Any]:
    """Describe durable paths and whether they exist — no web required."""
    pid = (product or product_id()).strip().lower() or "emux"
    cfg = _config_root(pid)
    st = state_dir(pid)
    reg = registry_path(pid)

    def _info(p: Path) -> dict[str, Any]:
        if not p.exists():
            return {"path": str(p), "exists": False}
        try:
            stt = p.stat()
            return {
                "path": str(p),
                "exists": True,
                "bytes": stt.st_size,
                "mtime": int(stt.st_mtime),
            }
        except OSError as e:
            return {"path": str(p), "exists": True, "error": str(e)}

    n_reg = 0
    if reg.is_file():
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                n_reg = len(data)
        except Exception:
            n_reg = -1

    missions_dir = cfg / "missions"
    n_missions = 0
    if missions_dir.is_dir():
        n_missions = sum(1 for _ in missions_dir.glob("*.md"))

    return {
        "ok": True,
        "product": pid,
        "ssot": "disk + tmux (web is optional projection)",
        "config_dir": str(cfg),
        "state_dir": str(st),
        "registry": {**_info(reg), "entries": n_reg},
        "chats_db": _info(cfg / "chats.db"),
        "schedule": _info(cfg / "schedule.json"),
        "schedule_log": _info(cfg / "schedule-log.jsonl"),
        "missions_dir": {**_info(missions_dir), "briefs": n_missions},
        "missions_log": _info(cfg / "logs" / "missions.jsonl"),
        "stream_logs": _info(st / "logs"),
        "inbox": _info(st / "inbox"),
        "audit": _info(st / "audit.jsonl"),
        "signals": _info(st / "signals.jsonl"),
        "index": _info(st / "index.json"),
        "shared_emux_registry": _info(shared_emux_registry()),
        "env": {
            "EMUX_PRODUCT": os.environ.get("EMUX_PRODUCT") or "",
            "EMUX_REGISTRY": os.environ.get("EMUX_REGISTRY") or "",
            "EMUX_STATE": os.environ.get("EMUX_STATE") or "",
        },
    }
