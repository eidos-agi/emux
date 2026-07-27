"""Emux skins — same engine, different face.

Greenmark (and other hosts) should not fork emux to rename the UI. A skin is
product chrome only: brand string, tagline, status title, optional accent.
Behavior, registry, tmux, MCP, and APIs stay emux.

Resolution order for `load_skin()`:
  1. explicit argument (e.g. CLI --skin)
  2. $EMUX_SKIN
  3. built-in default ``emux``

Built-ins: ``emux`` (upstream), ``gmux`` (Greenmark fleet face).
Unknown names fall back to emux with a warning on stderr.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Skin:
    """Visual / product identity for web + status surfaces."""

    id: str
    brand: str  # short mark in headers, e.g. EMUX / GMUX
    product: str  # lowercase product word, e.g. emux / gmux
    tagline: str  # under the brand, e.g. "control room"
    status_title: str  # simple status <h1>
    room_title: str  # full SPA document title
    docs_title: str  # docs page title
    accent: str  # CSS color for brand accents (hex)
    engine_label: str  # "emux" always — truth of the binary
    footer_note: str  # small honesty line, e.g. "powered by emux"

    def placeholders(self, version: str = "") -> dict[str, str]:
        """Map of __TOKEN__ → value for stamping HTML/JS shells."""
        eng = f"{self.engine_label} {version}".strip() if version else self.engine_label
        product_line = (
            f"{self.product} · {eng}" if self.id != "emux" else eng
        )
        return {
            "__SKIN_ID__": self.id,
            "__BRAND__": self.brand,
            "__PRODUCT__": self.product,
            "__TAGLINE__": self.tagline,
            "__STATUS_TITLE__": self.status_title,
            "__ROOM_TITLE__": self.room_title,
            "__DOCS_TITLE__": self.docs_title,
            "__ACCENT__": self.accent,
            "__ENGINE__": eng,
            "__PRODUCT_LINE__": product_line,
            "__FOOTER_NOTE__": self.footer_note,
        }

    def apply(self, text: str, version: str = "") -> str:
        out = text
        for key, val in self.placeholders(version).items():
            out = out.replace(key, val)
        return out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_SKINS: dict[str, Skin] = {
    "emux": Skin(
        id="emux",
        brand="EMUX",
        product="emux",
        tagline="control room",
        status_title="emux status",
        room_title="emux — control room",
        docs_title="Emux documentation",
        accent="#8e6129",  # classic amber
        engine_label="emux",
        footer_note="",
    ),
    "gmux": Skin(
        id="gmux",
        brand="GMUX",
        product="gmux",
        tagline="greenmark fleet",
        status_title="gmux status",
        room_title="gmux — greenmark fleet",
        docs_title="gmux documentation",
        accent="#203C31",  # Greenmark deep green (matches go door)
        engine_label="emux",
        footer_note="powered by emux",
    ),
}


def list_skins() -> list[str]:
    return sorted(_SKINS)


def get_skin(name: str | None) -> Skin:
    """Return a known skin; unknown → emux (never raises)."""
    if not name:
        return _SKINS["emux"]
    key = name.strip().lower()
    # aliases
    if key in ("greenmux", "greenmark", "gmw"):
        key = "gmux"
    if key in _SKINS:
        return _SKINS[key]
    print(f"emux skin: unknown skin {name!r} — using emux", file=sys.stderr)
    return _SKINS["emux"]


def load_skin(explicit: str | None = None) -> Skin:
    """Resolve skin from explicit arg or $EMUX_SKIN."""
    if explicit is not None and str(explicit).strip() != "":
        return get_skin(explicit)
    return get_skin(os.environ.get("EMUX_SKIN"))


# Process-wide active skin (set at web daemon start / CLI web entry).
_ACTIVE: Skin = _SKINS["emux"]


def set_active_skin(skin: Skin | str | None) -> Skin:
    global _ACTIVE
    if isinstance(skin, Skin):
        _ACTIVE = skin
    else:
        _ACTIVE = load_skin(skin)
    return _ACTIVE


def active_skin() -> Skin:
    return _ACTIVE
