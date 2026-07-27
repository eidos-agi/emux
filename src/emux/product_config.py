"""Per-product config — worker vs manager.

Most emux products are **workers** (one company/personal lane).
**Managers** (today: directrux) only manage the emuxes listed in their
``product.json`` — they are not a peer worker chat dump.

Source of truth for a manager's world::

    ~/.config/<product>/product.json

Override path with ``$EMUX_PRODUCT_CONFIG``.

Workers need no file (built-in role=worker + lane chats_match).
Managers **must** ship a product.json with ``managed_planes`` (allowlist).
Engine defaults do **not** invent Tailscale URLs — those live only in config.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VALID_ROLES = frozenset({"worker", "manager"})

# Worker skins only — chats_match when no product.json.
# Managers are not listed here with plane URLs (that would re-hardcode the fleet).
_WORKER_DEFAULTS: dict[str, dict[str, str]] = {
    "emux": {"role": "worker", "chats_match": "all"},
    "gmux": {"role": "worker", "chats_match": "greenmark"},
    "greenmux": {"role": "worker", "chats_match": "greenmark"},
    "reevux": {"role": "worker", "chats_match": "personal"},
    "amux": {"role": "worker", "chats_match": "aic"},
}

# Manager products: role + chats_match only if product.json is missing.
# managed_planes stays empty until config is installed (fail closed on "who can I manage?").
_MANAGER_DEFAULTS: dict[str, dict[str, str]] = {
    "directrux": {"role": "manager", "chats_match": "directrux"},
}


@dataclass(frozen=True)
class ManagedPlane:
    id: str
    lane: str = ""
    role: str = "worker"
    healthz: str = ""
    # Same-host operational truth (prefer over public/OIDC healthz when set).
    healthz_loopback: str = ""
    room: str = ""
    host: str = ""
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "lane": self.lane,
            "role": self.role,
            "healthz": self.healthz,
            "room": self.room,
            "host": self.host,
            "notes": self.notes,
        }
        if self.healthz_loopback:
            d["healthz_loopback"] = self.healthz_loopback
        return d


@dataclass(frozen=True)
class ProductConfig:
    product: str
    role: str  # worker | manager
    chats_match: str
    managed_planes: tuple[ManagedPlane, ...] = ()
    path: str | None = None
    source: str = "default"  # default | file
    notes: str = ""
    health_chat: dict[str, Any] | None = None
    vp: dict[str, Any] | None = None
    engine_seat: dict[str, Any] | None = None
    web_tester: dict[str, Any] | None = None

    @property
    def is_manager(self) -> bool:
        return self.role == "manager"

    @property
    def is_worker(self) -> bool:
        return self.role == "worker"

    def managed_ids(self) -> frozenset[str]:
        return frozenset(p.id for p in self.managed_planes)

    def can_manage(self, plane_id: str) -> bool:
        """Allowlist check — managers only manage listed planes."""
        if not self.is_manager:
            return False
        return (plane_id or "").strip().lower() in self.managed_ids()

    def plane(self, plane_id: str) -> ManagedPlane | None:
        key = (plane_id or "").strip().lower()
        for p in self.managed_planes:
            if p.id.lower() == key:
                return p
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "role": self.role,
            "chats_match": self.chats_match,
            "managed_planes": [p.as_dict() for p in self.managed_planes],
            "managed_ids": sorted(self.managed_ids()),
            "health_chat": self.health_chat or {},
            "vp": self.vp or {},
            "engine_seat": self.engine_seat or {},
            "web_tester": self.web_tester or {},
            "path": self.path,
            "source": self.source,
            "is_manager": self.is_manager,
            "notes": self.notes,
        }


def _product_id() -> str:
    env = (os.environ.get("EMUX_PRODUCT") or os.environ.get("EMUX_SKIN") or "").strip().lower()
    if env:
        return env
    try:
        from . import skin as _skin

        return (_skin.active_skin().id or "emux").strip().lower()
    except Exception:
        return "emux"


def config_dir_for(product: str) -> Path:
    try:
        from .chats_store import _config_root_for_skin

        return _config_root_for_skin(product)
    except Exception:
        if product in ("gmux", "greenmux", "greenmark"):
            return Path.home() / ".config" / "greenmux"
        if product in ("", "emux"):
            return Path.home() / ".config" / "emux"
        return Path.home() / ".config" / product


def config_path_candidates(product: str | None = None) -> list[Path]:
    prod = (product or _product_id()).strip().lower() or "emux"
    out: list[Path] = []
    env = (os.environ.get("EMUX_PRODUCT_CONFIG") or "").strip()
    if env:
        out.append(Path(env).expanduser())
    out.append(config_dir_for(prod) / "product.json")
    return out


def _planes_from(raw: Any) -> tuple[ManagedPlane, ...]:
    if not isinstance(raw, list):
        return ()
    planes: list[ManagedPlane] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pid = str(item.get("id") or "").strip()
        if not pid:
            continue
        planes.append(
            ManagedPlane(
                id=pid,
                lane=str(item.get("lane") or ""),
                role=str(item.get("role") or "worker"),
                healthz=str(item.get("healthz") or ""),
                healthz_loopback=str(
                    item.get("healthz_loopback") or item.get("loopback_healthz") or ""
                ),
                room=str(item.get("room") or ""),
                host=str(item.get("host") or ""),
                notes=str(item.get("notes") or ""),
            )
        )
    return tuple(planes)


def _from_mapping(prod: str, data: dict[str, Any], *, path: str | None, source: str) -> ProductConfig:
    role = str(data.get("role") or "worker").strip().lower()
    if role not in VALID_ROLES:
        role = "worker"
    chats = str(data.get("chats_match") or "").strip()
    if not chats:
        if role == "manager":
            chats = prod if prod != "emux" else "all"
        else:
            chats = _WORKER_DEFAULTS.get(prod, {}).get("chats_match", "all")
    # Managers never default to scanning the whole machine.
    if role == "manager" and chats == "all":
        chats = prod if prod not in ("", "emux") else "directrux"
    hc = data.get("health_chat")
    if not isinstance(hc, dict):
        hc = None
    vp = data.get("vp")
    if not isinstance(vp, dict):
        vp = None
    eng = data.get("engine_seat")
    if not isinstance(eng, dict):
        eng = None
    wt = data.get("web_tester")
    if not isinstance(wt, dict):
        wt = None
    return ProductConfig(
        product=str(data.get("product") or prod),
        role=role,
        chats_match=chats,
        managed_planes=_planes_from(data.get("managed_planes")),
        path=path,
        source=source,
        notes=str(data.get("notes") or ""),
        health_chat=hc,
        vp=vp,
        engine_seat=eng,
        web_tester=wt,
    )


def load_product_config(product: str | None = None) -> ProductConfig:
    """Load product.json or fall back to lean built-in role defaults."""
    prod = (product or _product_id()).strip().lower() or "emux"
    for path in config_path_candidates(prod):
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            return _from_mapping(prod, data, path=str(path), source="file")
        except Exception:
            continue

    if prod in _MANAGER_DEFAULTS:
        base = dict(_MANAGER_DEFAULTS[prod])
        base["product"] = prod
        # Empty managed_planes until product.json exists — fail closed.
        return _from_mapping(prod, base, path=None, source="default")

    base = dict(_WORKER_DEFAULTS.get(prod) or {"role": "worker", "chats_match": "all"})
    base["product"] = prod
    return _from_mapping(prod, base, path=None, source="default")


def default_chats_match_for_skin(skin_id: str) -> str:
    return load_product_config(skin_id).chats_match


def require_manageable(product: str | None, plane_id: str) -> ManagedPlane:
    """Raise ValueError if this product may not manage plane_id."""
    cfg = load_product_config(product)
    if not cfg.is_manager:
        raise ValueError(f"{cfg.product} is role={cfg.role}, not a manager")
    plane = cfg.plane(plane_id)
    if plane is None:
        allowed = ", ".join(sorted(cfg.managed_ids())) or "(none — install product.json)"
        raise ValueError(
            f"{cfg.product} cannot manage {plane_id!r}; allowlist: {allowed}"
        )
    return plane
