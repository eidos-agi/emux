"""Emux skins — same engine, different face.

A skin is product chrome only: brand, logo, Greenmark (or other) colors,
light/dark palettes, status/room titles. Registry, tmux, MCP, and APIs stay emux.

Resolution: CLI ``--skin`` → ``$EMUX_SKIN`` → ``emux``.
Theme (light/dark) is client-side (localStorage); default comes from the skin.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote


@dataclass(frozen=True)
class Palette:
    """CSS tokens for one appearance (light or dark)."""

    bg: str
    bg_raise: str
    bg_card: str
    accent: str
    accent_dim: str
    accent_faint: str
    text: str
    text_dim: str
    live: str
    stale: str
    line: str
    on_accent: str
    user: str = ""  # defaults to accent

    def css_block(self, selector: str) -> str:
        user = self.user or self.accent
        return (
            f"{selector}{{"
            f"--bg:{self.bg};--bg-raise:{self.bg_raise};--bg-card:{self.bg_card};"
            f"--amber:{self.accent};--amber-dim:{self.accent_dim};--amber-faint:{self.accent_faint};"
            f"--text:{self.text};--text-dim:{self.text_dim};"
            f"--live:{self.live};--stale:{self.stale};--line:{self.line};"
            f"--user:{user};--on-accent:{self.on_accent};"
            f"--on:{self.accent};--ink:{self.text};--dim:{self.text_dim};"
            f"--card:{self.bg_card};--pill:{self.accent_faint};"
            f"}}"
        )


# Compact wordmark logos — fill=currentColor so theme accent paints them.
_LOGO_EMUX = (
    '<svg class="skin-logo" viewBox="0 0 64 64" width="36" height="36" '
    'aria-hidden="true" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="6" y="6" width="52" height="52" rx="10" fill="currentColor" opacity=".15"/>'
    '<rect x="14" y="14" width="36" height="36" rx="6" fill="none" stroke="currentColor" stroke-width="3"/>'
    '<path d="M22 40 V24 h6 l6 10 6-10 h6 v16 h-5 V30 l-7 11 h-2 l-7-11 v10 z" fill="currentColor"/>'
    "</svg>"
)

_LOGO_GMUX = (
    '<svg class="skin-logo" viewBox="0 0 64 64" width="36" height="36" '
    'aria-hidden="true" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="4" y="4" width="56" height="56" rx="12" fill="currentColor" opacity=".12"/>'
    # G-mark: open ring + stem (reads as G, Greenmark-adjacent without a custom font)
    '<path fill="none" stroke="currentColor" stroke-width="5" stroke-linecap="round" '
    'd="M44 28a14 14 0 1 0 2 8"/>'
    '<path fill="currentColor" d="M32 36h16v5H37v7h-5z"/>'
    "</svg>"
)

# R-mark: slate/navy personal lane (Reeves + emux → reevux)
_LOGO_REEVUX = (
    '<svg class="skin-logo" viewBox="0 0 64 64" width="36" height="36" '
    'aria-hidden="true" xmlns="http://www.w3.org/2000/svg">'
    '<rect x="4" y="4" width="56" height="56" rx="12" fill="currentColor" opacity=".12"/>'
    '<path fill="currentColor" d="M22 44 V20 h12.5 c6.2 0 10.5 3.2 10.5 8.4 0 3.6-2 6.4-5.2 '
    '7.6 L46 44 h-6.2 l-5.4-7.2 H27.5 V44 z M27.5 25.2 v6.8 h6.4 c2.8 0 4.5-1.3 4.5-3.4 '
    '0-2.1-1.7-3.4-4.5-3.4 z"/>'
    "</svg>"
)


@dataclass(frozen=True)
class Skin:
    """Visual / product identity for web + status surfaces."""

    id: str
    brand: str
    product: str
    tagline: str
    status_title: str
    room_title: str
    docs_title: str
    engine_label: str
    footer_note: str
    light: Palette
    dark: Palette
    logo_svg: str
    default_theme: str = "light"  # light | dark

    @property
    def accent(self) -> str:
        return self.light.accent

    def theme_css(self) -> str:
        """:root + [data-theme=…] variable blocks for light/dark."""
        light = self.light.css_block(":root,[data-theme=light]")
        dark = self.dark.css_block("[data-theme=dark]")
        return light + dark

    def favicon_data_uri(self) -> str:
        """Tiny favicon using skin accent (URL-encoded SVG)."""
        # Prefer light accent for tab icon contrast on most OS chrome
        fill = self.light.accent.lstrip("#")
        if self.id == "emux":
            bg = "0c0a07"
        elif self.id == "reevux":
            bg = "0e1218"
        else:
            bg = "0f1a14"
        svg = (
            f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
            f"<rect width='16' height='16' rx='3' fill='%23{bg}'/>"
            f"<rect x='3' y='3' width='10' height='10' rx='2' fill='%23{fill}'/>"
            f"</svg>"
        )
        return "data:image/svg+xml," + quote(svg, safe="")

    def logo_html(self) -> str:
        return (
            f'<span class="brand-mark" title="{self.brand}">'
            f"{self.logo_svg}"
            f'<span class="brand-word">{self.brand}</span>'
            f"</span>"
        )

    def placeholders(self, version: str = "") -> dict[str, str]:
        eng = f"{self.engine_label} {version}".strip() if version else self.engine_label
        product_line = f"{self.product} · {eng}" if self.id != "emux" else eng
        note = self.footer_note or ""
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
            "__FOOTER_NOTE__": note,
            "__THEME_CSS__": self.theme_css(),
            "__DEFAULT_THEME__": self.default_theme if self.default_theme in ("light", "dark") else "light",
            "__FAVICON__": self.favicon_data_uri(),
            "__LOGO_HTML__": self.logo_html(),
            "__THEME_STORAGE_KEY__": f"emux.theme.{self.id}",
        }

    def apply(self, text: str, version: str = "") -> str:
        out = text
        for key, val in self.placeholders(version).items():
            out = out.replace(key, val)
        return out

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- palettes ----------------------------------------------------------------

_EMUX_LIGHT = Palette(
    bg="#f0ebe4",
    bg_raise="#e9e3db",
    bg_card="#e4ded6",
    accent="#8e6129",
    accent_dim="#a9853f",
    accent_faint="#d8cdba",
    text="#1e1a17",
    text_dim="#6b6159",
    live="#4a6a3a",
    stale="#ab5036",
    line="#cabfae",
    on_accent="#f5efe6",
)
_EMUX_DARK = Palette(
    bg="#0c0a07",
    bg_raise="#14110c",
    bg_card="#1a1610",
    accent="#ffb000",
    accent_dim="#d4940a",
    accent_faint="#3d3018",
    text="#f0ebe4",
    text_dim="#9a8f7e",
    live="#6dbf5a",
    stale="#e07050",
    line="#2a2418",
    on_accent="#0c0a07",
)

# Greenmark go door family: cool green (not emux amber/brown) + cream paper
# Light accent is a clear forest green — pure #203C31 on cream can read muddy/brown.
_GMUX_LIGHT = Palette(
    bg="#f4f7f4",
    bg_raise="#e8f0ea",
    bg_card="#ffffff",
    accent="#1b7a4e",       # interactive green (tabs, brand, hot tiles)
    accent_dim="#2d6a4f",
    accent_faint="#d4edda",
    text="#14261c",
    text_dim="#4d6356",
    live="#1a7a45",
    stale="#a67c2d",
    line="#c5d6cb",
    on_accent="#f4f7f4",
    user="#203C31",         # deep brand green (go door)
)
_GMUX_DARK = Palette(
    bg="#0c1611",
    bg_raise="#132019",
    bg_card="#1a2a21",
    accent="#5fbf8f",       # mint on forest (go door pulse)
    accent_dim="#3d9a6e",
    accent_faint="#1e3d30",
    text="#e8f0ea",
    text_dim="#8aa396",
    live="#5fbf8f",
    stale="#c9a227",
    line="#2a4034",
    on_accent="#0c1611",
    user="#5fbf8f",
)

# Reeves personal lane — cool slate/navy (deliberately unlike gmux green / emux amber)
_REEVUX_LIGHT = Palette(
    bg="#eef1f6",
    bg_raise="#e6ebf2",
    bg_card="#dee4ee",
    accent="#3b5ba5",
    accent_dim="#5a76bd",
    accent_faint="#ccd6e8",
    text="#182030",
    text_dim="#5c6678",
    live="#3d7a5a",
    stale="#b3503a",
    line="#c5cddd",
    on_accent="#f4f7fc",
    user="#3b5ba5",
)
_REEVUX_DARK = Palette(
    bg="#0e1218",
    bg_raise="#151b24",
    bg_card="#1b2330",
    accent="#7aa2ff",
    accent_dim="#5a76bd",
    accent_faint="#243044",
    text="#e8eef8",
    text_dim="#8b96ab",
    live="#5fbf8f",
    stale="#e07050",
    line="#2a3444",
    on_accent="#0e1218",
    user="#7aa2ff",
)

_SKINS: dict[str, Skin] = {
    "emux": Skin(
        id="emux",
        brand="EMUX",
        product="emux",
        tagline="control room",
        status_title="emux status",
        room_title="emux — control room",
        docs_title="Emux documentation",
        engine_label="emux",
        footer_note="",
        light=_EMUX_LIGHT,
        dark=_EMUX_DARK,
        logo_svg=_LOGO_EMUX,
        default_theme="light",
    ),
    "gmux": Skin(
        id="gmux",
        brand="GMUX",
        product="gmux",
        tagline="greenmark fleet",
        status_title="gmux status",
        room_title="gmux — greenmark fleet",
        docs_title="gmux documentation",
        engine_label="emux",
        footer_note="powered by emux",
        light=_GMUX_LIGHT,
        dark=_GMUX_DARK,
        logo_svg=_LOGO_GMUX,
        default_theme="light",
    ),
    "reevux": Skin(
        id="reevux",
        brand="REEVUX",
        product="reevux",
        tagline="personal / Reeves fleet",
        status_title="reevux status",
        room_title="reevux — personal fleet",
        docs_title="reevux documentation",
        engine_label="emux",
        footer_note="powered by emux · personal lane only",
        light=_REEVUX_LIGHT,
        dark=_REEVUX_DARK,
        logo_svg=_LOGO_REEVUX,
        default_theme="light",
    ),
}


def list_skins() -> list[str]:
    return sorted(_SKINS)


def get_skin(name: str | None) -> Skin:
    if not name:
        return _SKINS["emux"]
    key = name.strip().lower()
    if key in ("greenmux", "greenmark", "gmw"):
        key = "gmux"
    if key in ("reeves", "personal", "rvs"):
        key = "reevux"
    if key in _SKINS:
        return _SKINS[key]
    print(f"emux skin: unknown skin {name!r} — using emux", file=sys.stderr)
    return _SKINS["emux"]


def load_skin(explicit: str | None = None) -> Skin:
    if explicit is not None and str(explicit).strip() != "":
        return get_skin(explicit)
    return get_skin(os.environ.get("EMUX_SKIN"))


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


# Shared client snippet: light/dark toggle + persist. Placeholders stamped by Skin.apply.
THEME_TOGGLE_SCRIPT = r"""
<script id="emux-theme">
(function(){
  var KEY="__THEME_STORAGE_KEY__";
  var def="__DEFAULT_THEME__";
  function pref(){
    try{var s=localStorage.getItem(KEY); if(s==="light"||s==="dark")return s;}catch(e){}
    if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches) return def==="dark"?"dark":"dark";
    return def==="dark"?"dark":"light";
  }
  function apply(t){
    document.documentElement.setAttribute("data-theme", t);
    var b=document.getElementById("themebtn");
    if(b){b.textContent=t==="dark"?"☀ light":"☾ dark"; b.setAttribute("aria-label","switch to "+(t==="dark"?"light":"dark")+" mode");}
  }
  function toggle(){
    var cur=document.documentElement.getAttribute("data-theme")||pref();
    var next=cur==="dark"?"light":"dark";
    try{localStorage.setItem(KEY,next);}catch(e){}
    apply(next);
  }
  apply(pref());
  document.addEventListener("DOMContentLoaded",function(){
    apply(pref());
    var b=document.getElementById("themebtn");
    if(b) b.addEventListener("click",toggle);
  });
  // early paint before DOMContentLoaded
  apply(pref());
})();
</script>
"""


def theme_toggle_script() -> str:
    return THEME_TOGGLE_SCRIPT
