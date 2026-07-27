"""emux web — a persistent local daemon with monitoring + head views.

Vocabulary (see docs/vocabulary.md):
  Sessions run. Heads are how you attach. CHATS are past transcripts.

`emux web` starts a long-running HTTP server (the daemon) that exposes the
same registry + tmux operations as the MCP server, plus a browser UI with
several views over the sessions emux knows about:

- head     — one session as a live head you can type into. The pane updates
             in place (rendered terminal, not a growing transcript);
             keystrokes are logged above the live screen. (legacy mode=chat)
- grid     — every session as a live mini-pane tile, all streaming at once.
- groups   — the same tiles sectioned by registry tag.
- activity — change-detection strips per session: which panes moved, when.
- flow     — agent topology: a layered hierarchy built from registry `manages`
             edges (orchestrators on top, the agents they drive below);
             sessions with no relationships sit in an "unconnected" row.
- orphans  — live tmux sessions emux does not know about yet (per host).
- chats    — CHATS: Claude/Grok transcripts on disk that are not live
             (past missions). Resume into a session, then open a head.

Design principles (same as the MCP server):
- Operates on EXISTING tmux sessions only. Never spawns, never kills.
- The registry is metadata; live truth comes from `tmux list-sessions`.
- Binds 127.0.0.1 by default. Localhost is NOT a security boundary — any web
  page in your browser can reach a localhost port — so the API rejects
  foreign Host headers (DNS-rebind defense) and cross-origin POSTs. There is
  still no authentication; only widen `--host` on a network you trust.

Performance: a single background thread captures every live pane on a timer
into a shared cache, so N browser tabs watching M sessions cost one capture
sweep, not N×M. The cache also evicts sessions once tmux reaps them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import threading
import time
from collections import deque
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from . import __version__
from . import help as _help
from . import server as _server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8689

# Background capture loop cadence, activity ring-buffer size, and how long a
# cached pane frame is considered fresh (covers the loop missing a tick).
_POLL_INTERVAL = 1.5
_SAMPLE_WINDOW = 60
_CACHE_TTL = 5.0

_LOCALHOSTS = {"127.0.0.1", "localhost", "::1", ""}

# Spinner / progress glyphs that animate without representing real work:
# braille (Claude Code's thinking spinner), block/quadrant spinners, and a
# handful of common dot/arc spinners. Stripped before change-detection so an
# idle session with a spinning cursor doesn't read as perpetually "active".
_SPINNER_RE = re.compile(
    r"[⠀-⣿▀-▟●○◐◑◒◓"
    r"◜◝◞◟◢◣◤◥"
    r"⠋⠹⠙⠸⠦⠇⠏]"
)

# Daemon-side state, shared across all clients and guarded by one lock.
_ACTIVITY: dict[str, dict[str, Any]] = {}   # session -> {norm, changed, last_change, samples}
_CACHE: dict[str, dict[str, Any]] = {}      # session -> {content, ts, lines}
_LOCK = threading.Lock()
# Expensive GET responses: short TTL + single-flight so concurrent tabs/stress
# do not stampede disk/tmux/remote probes (EID-1100 / 1107 / 1108 / 1109).
_EXPENSIVE_CACHE: dict[str, tuple[float, Any]] = {}
_EXPENSIVE_INFLIGHT: dict[str, threading.Event] = {}
_EXPENSIVE_LOCK = threading.Lock()
_EXPENSIVE_TTL = {
    "ai_md": 8.0,
    "ai_html": 8.0,
    # Managed probes remote planes — keep fresh but serve stale while revalidating
    # so concurrent room polls never block on 4× remote healthz (EID-1115).
    "managed": 8.0,
    "managed_stale": 45.0,
    "chats": 4.0,
    "sessions": 1.5,
}


def _expensive_get(key: str, ttl: float, factory, *, stale_ttl: float | None = None):
    """Return cached value or run factory once; concurrent waiters share the result.

    When ``stale_ttl`` is set and the cache is past ``ttl`` but within ``stale_ttl``,
    return the stale value immediately and refresh in a background thread (EID-1115).
    """
    now = time.time()
    with _EXPENSIVE_LOCK:
        hit = _EXPENSIVE_CACHE.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
        # Stale-while-revalidate: serve old value, kick background refresh.
        if (
            stale_ttl is not None
            and hit
            and (now - hit[0]) < stale_ttl
        ):
            stale_val = hit[1]
            ev = _EXPENSIVE_INFLIGHT.get(key)
            if ev is None:
                ev = threading.Event()
                _EXPENSIVE_INFLIGHT[key] = ev

                def _bg() -> None:
                    try:
                        value = factory()
                        with _EXPENSIVE_LOCK:
                            _EXPENSIVE_CACHE[key] = (time.time(), value)
                    except Exception:  # noqa: BLE001
                        pass
                    finally:
                        with _EXPENSIVE_LOCK:
                            _EXPENSIVE_INFLIGHT.pop(key, None)
                        ev.set()

                threading.Thread(target=_bg, name=f"emux-cache-{key[:24]}", daemon=True).start()
            return stale_val
        ev = _EXPENSIVE_INFLIGHT.get(key)
        owner = False
        if ev is None:
            ev = threading.Event()
            _EXPENSIVE_INFLIGHT[key] = ev
            owner = True
    if not owner:
        # Wait briefly for owner; on timeout prefer stale cache then factory.
        if not ev.wait(timeout=min(max(ttl, 2.0), 4.0)):
            with _EXPENSIVE_LOCK:
                hit = _EXPENSIVE_CACHE.get(key)
                if hit:
                    return hit[1]
            return factory()
        with _EXPENSIVE_LOCK:
            hit = _EXPENSIVE_CACHE.get(key)
            if hit:
                return hit[1]
        return factory()
    try:
        value = factory()
        with _EXPENSIVE_LOCK:
            _EXPENSIVE_CACHE[key] = (time.time(), value)
        return value
    finally:
        with _EXPENSIVE_LOCK:
            _EXPENSIVE_INFLIGHT.pop(key, None)
        ev.set()
# The Gist, cached server-side by content hash so it's not recomputed while the
# pane is unchanged; warmed proactively the moment a session stops (see
# _capture_and_observe) so its result is ready before you open it.
_GIST_CACHE: dict[str, dict[str, Any]] = {}   # session -> {hash, digest, suggestions, ts}
_GIST_INFLIGHT: set[str] = set()              # sessions being warmed right now (dedupe)
_SETTLED_STATES = frozenset({"idle", "asking", "waiting_human", "error"})
# how long a settled session must sit STILL (no pane change) before we treat it as
# "clearly stopped" and warm its gist — long enough to skip the brief running↔idle
# flicker between an agent's turns. Override with EMUX_GIST_PAUSE.
_GIST_PAUSE_SECS = float(os.environ.get("EMUX_GIST_PAUSE", "8"))


def _gist_hash(pane: str) -> str:
    """Cache key = a hash of the exact pane slice the gist model reads. When the
    pane changes, the hash changes, so the cache self-busts."""
    return hashlib.sha1(pane[-3500:].encode("utf-8", "ignore")).hexdigest()[:16]


def _should_warm_gist(state: str, age: float | None, norm: str | None,
                      prev_warm_norm: str | None, inflight: bool) -> bool:
    """Warm the gist iff the session is settled, has sat STILL for a real pause
    (age ≥ threshold — skips the brief between-turns flicker), its content is new
    since the last warm (norm changed), and no warm is already running."""
    return (state in _SETTLED_STATES and age is not None and age >= _GIST_PAUSE_SECS
            and norm is not None and norm != prev_warm_norm and not inflight)


# ---------------------------------------------------------------------------
# session listing
# ---------------------------------------------------------------------------

def sessions_payload() -> dict[str, Any]:
    """Merged registry + live view: registered entries first, then live unregistered.

    Live discovery walks every readable local tmux socket (see server._live_sessions).
    Returns `scope` describing what was scanned so UIs do not overclaim "all work".
    """
    if _server._resolve_tmux() is None:
        return {
            "ok": False,
            "error": "tmux_not_installed",
            "sessions": [],
            "scope": {
                "claim": "tmux not installed — no scan",
                "sockets": [],
            },
        }
    live = _server._live_sessions()
    scan = _server.tmux_scan_scope()
    registry = _server._load_registry()
    # Prefer matching live rows by name; keep socket metadata when present.
    live_by_name = {s["name"]: s for s in live}
    # a registered session may live on another machine — probe each distinct
    # remote host once so it shows LIVE, not "gone".
    remote_live: dict[str, set[str]] = {}
    for entry in registry.values():
        h = entry.get("host")
        if h and h not in remote_live:
            remote_live[h] = _remote_live_names(h)
    sessions = []
    for name, entry in sorted(registry.items()):
        target = entry.get("session")
        host = entry.get("host")
        live_row = live_by_name.get(target) if not host else None
        if host:
            is_live = target in remote_live.get(host, set())
        else:
            is_live = target in live_by_name
        cwd = (live_row or {}).get("cwd") or entry.get("cwd")
        sessions.append({
            "name": name,
            "session": target,
            "host": host,
            "description": entry.get("description"),
            "tags": entry.get("tags") or [],
            "manages": entry.get("manages") or [],
            "registered": True,
            "live": is_live,
            "state": "live" if is_live else "stale",
            "attached": (live_row or {}).get("attached", False),
            "created_unix": (live_row or {}).get("created_unix"),
            "cwd": cwd,
            "socket": (live_row or {}).get("socket") or "default",
            "socket_path": (live_row or {}).get("socket_path"),
            # explicit override (remote worker / manager) wins over cwd-derivation
            "company": _company_by_key(entry.get("company")) or _detect_company(cwd),
            "_co_explicit": bool(entry.get("company")),
        })
    registered_targets = {e.get("session") for e in registry.values()}
    for s in live:
        if s["name"] in registered_targets:
            continue
        sessions.append({
            "name": s["name"],
            "session": s["name"],
            "description": None,
            "tags": [],
            "manages": [],
            "registered": False,
            "live": True,
            "state": "live",
            "attached": s.get("attached", False),
            "created_unix": s.get("created_unix"),
            "cwd": s.get("cwd"),
            "socket": s.get("socket") or "default",
            "socket_path": s.get("socket_path"),
            "company": _detect_company(s.get("cwd")),
        })
    # Socket we could not read → synthetic "unknown" row (not silent absence).
    for sock in scan:
        if sock.get("status") == "unknown":
            label = sock.get("socket") or "?"
            sessions.append({
                "name": f"socket:{label}",
                "session": f"socket:{label}",
                "description": f"tmux socket not readable: {sock.get('error') or sock.get('path') or label}",
                "tags": ["scan", "unknown"],
                "manages": [],
                "registered": False,
                "live": False,
                "state": "unknown",
                "attached": False,
                "created_unix": None,
                "cwd": sock.get("dir") or sock.get("path"),
                "socket": label,
                "socket_path": sock.get("path"),
                "company": {},
            })
    # A manager belongs to the company of what it SUPERVISES, not where its
    # process runs. So if it manages workers that agree on one company, adopt it
    # — overriding a cwd-derived guess, but never an explicit override.
    by_name = {s["name"]: s for s in sessions}
    for s in sessions:
        if s.get("_co_explicit") or not s.get("manages"):
            continue
        managed_cos = {}
        for m in s["manages"]:
            c = (by_name.get(m) or {}).get("company") or {}
            if c.get("company"):
                managed_cos[c["company"]] = c
        if len(managed_cos) == 1:
            s["company"] = next(iter(managed_cos.values()))
    for s in sessions:
        s.pop("_co_explicit", None)
    import getpass
    try:
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid()) if hasattr(os, "getuid") else "?"
    ok_socks = [s for s in scan if s.get("status") == "ok"]
    unknown_socks = [s for s in scan if s.get("status") == "unknown"]
    claim = (
        f"tmux sessions on {len(ok_socks)} readable socket(s) for user {user} "
        f"— not all host processes; not other users' servers"
    )
    if unknown_socks:
        claim += f"; {len(unknown_socks)} socket(s) unknown/unreadable"
    return {
        "ok": True,
        "sessions": sessions,
        "scope": {
            "claim": claim,
            "user": user,
            "sockets": scan,
        },
    }


# ---------------------------------------------------------------------------
# capture + change detection
# ---------------------------------------------------------------------------

_RLIVE_CACHE: dict[str, tuple[float, set[str]]] = {}
_RLIVE_TTL = 5.0


def _remote_live_names(host: str) -> set[str]:
    """tmux session names live on a REMOTE host (cached briefly). This is what
    lets the control room show a rentamac worker as LIVE instead of 'gone' — the
    daemon only sees local tmux, so a remote session needs an ssh probe."""
    hit = _RLIVE_CACHE.get(host)
    if hit and (time.time() - hit[0]) < _RLIVE_TTL:
        return hit[1]
    names: set[str] = set()
    try:
        code, out, _ = _server._run_tmux(["ls", "-F", "#{session_name}"],
                                         host=host, timeout=15)
        if code == 0:
            names = {ln.strip() for ln in out.splitlines() if ln.strip()}
    except Exception:  # noqa: BLE001
        names = set()
    _RLIVE_CACHE[host] = (time.time(), names)
    return names


def _session_host(session: str) -> str | None:
    """The registered host for a session id (None = local). Lets host-unaware
    callers (the modal's capture/send/classify by session id) reach remote."""
    for e in _server._load_registry().values():
        if e.get("session") == session:
            return e.get("host")
    return None


def capture_payload(session: str, lines: int = 300,
                    host: str | None = None,
                    socket: str | None = None) -> dict[str, Any]:
    """Capture the active pane of `session` (raw tmux session name), local or —
    when `host` is set — over ssh. Always live; the chat/modal want fresh, deep
    scrollback for one session, which is cheap.

    `socket` is an optional absolute tmux server socket path when the session
    lives off the default server (`tmux -S`).

    Also returns ``history_size`` / ``pane_height`` so the UI knows whether
    tmux scrollback exists (shells) or the app owns the alt-screen (Claude).
    """
    if host is None and _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    lines = max(20, min(int(lines or 300), 8000))
    # Prefer joined wrapped lines (-J) so long agent output doesn't hard-clip mid-row.
    code, out, err = _server._run_tmux(
        ["capture-pane", "-t", session, "-p", "-J", "-S", f"-{lines}"],
        host=host, timeout=20, socket=socket)
    if code != 0:
        # older tmux without -J
        code, out, err = _server._run_tmux(
            ["capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
            host=host, timeout=20, socket=socket)
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed", "stderr": err}
    hist_size, pane_h, hist_lim = 0, 0, 0
    try:
        c2, meta, _e2 = _server._run_tmux(
            ["display-message", "-p", "-t", session,
             "#{history_size} #{pane_height} #{history_limit}"],
            host=host, timeout=10, socket=socket,
        )
        if c2 == 0 and meta.strip():
            parts = meta.strip().split()
            if len(parts) >= 1:
                hist_size = int(parts[0])
            if len(parts) >= 2:
                pane_h = int(parts[1])
            if len(parts) >= 3:
                hist_lim = int(parts[2])
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "session": session,
        "host": host,
        "content": out,
        "options": _parse_options(out),
        "thinking": _thinking(out),
        "history_size": hist_size,
        "pane_height": pane_h,
        "history_limit": hist_lim,
        # Claude/Codex full-screen TUIs: history_size stays 0 — scroll must go to the app.
        "scroll_mode": "tmux" if hist_size > 0 else "app",
    }


_THINK_TIME_RE = re.compile(r"(\d+m\s?\d+s|\d+m|\d+s)")


def _thinking(content: str) -> dict[str, Any]:
    """Is the agent GENERATING right now, and for how long? 'esc to interrupt' is
    the universal 'I'm working' tell (Claude & Codex), and its line carries the
    live elapsed timer ('Working (12s …)', 'Herding… (2m 47s …)')."""
    for ln in content.splitlines():
        if "esc to interrupt" in ln.lower():
            m = _THINK_TIME_RE.search(ln)
            return {"active": True, "for": m.group(1).replace(" ", "") if m else ""}
    return {"active": False, "for": ""}


# (What each agent looks like in a pane now lives in emux/adapters.py — that is
# part of the agent's contract, not a private table here.)

# Which company/context a session belongs to, from its cwd.
# (key, label, color, roots, keywords). `roots` are the repos-<x>/ trees and are
# authoritative; `keywords` catch a company's repos that live in the generic
# ~/repos/ tree (e.g. repos/greenmark-claude-toolkit). Roots win over keywords.
_COMPANY_TABLE = [
    ("eidos", "Eidos", "#7dd3fc", ("repos-eidos-agi", "repos-eidos-capital"), ("eidos",)),
    ("greenmark", "Greenmark Waste", "#7bd88f", ("repos-greenmark",), ("greenmark",)),
    # Reeves — Daniel's PERSONAL ecosystem (reeves-wealth/lens/cockpit/operator/…).
    # Lives under repos-personal/, so it must be listed BEFORE `personal` (and `aic`,
    # for repos-aic/reeves-view) so the "reeves" match wins over the generic root.
    ("reeves", "Reeves", "#8ea0ff", ("reeves",), ("reeves",)),
    ("aic", "AIC", "#143ca2", ("repos-aic", "repos-aic-holdings"), ("aic-",)),
    ("jetta", "Jetta", "#ffb27d", ("repos-jetta",), ("jetta",)),
    ("momentito", "Momentito", "#ff9ecf", ("repos-momentito",), ("momentito",)),
    ("rhea", "Rhea Impact", "#9ae6e6", ("repos-rheaimpact",), ("rheaimpact", "rhea-impact")),
    ("asmp", "ASMP", "#d0c0a0", ("repos-asmp",), ("asmp",)),
    ("boone", "Boone Voyage", "#4db6c9", ("repos-bv",), ("boonevoyage", "boone-voyage")),
    ("personal", "Personal", "#f0d060", ("repos-personal", "repos-local"), ()),
]


# Standing routing preferences — state a rule ONCE, it sticks. You should never
# have to re-explain "Eidos runs on the mac-mini" per session. Defaults live here;
# override/extend at ~/.config/emux/routing.json:
#   {"company_host": {"eidos": "daniels-mac-mini", "greenmark": "some-host"}}
_COMPANY_HOST_DEFAULT = {"eidos": "daniels-mac-mini", "reeves": "daniels-mac-mini"}


def _routing_prefs() -> dict[str, Any]:
    prefs: dict[str, Any] = {"company_host": dict(_COMPANY_HOST_DEFAULT)}
    p = Path(os.environ.get("EMUX_ROUTING")
             or Path.home() / ".config" / "emux" / "routing.json")
    if p.is_file():
        try:
            user = json.loads(p.read_text())
            prefs["company_host"].update(user.get("company_host") or {})
        except (OSError, json.JSONDecodeError):
            pass
    return prefs


# ---------------------------------------------------------------------------
# Plan failover facade — when a session (esp. the manager) runs its Claude
# account out of tokens, switch it to ANOTHER account in code: exit the agent,
# relaunch under that account's CLAUDE_CONFIG_DIR, and resume the conversation.
# Deterministic (tmux only, no LLM). Accounts are configured, never logged-in by
# emux — the human owns credentials.
#   ~/.config/emux/plans.json:
#   {"plans": [{"name":"acct-1","config_dir":"~/.claude"},
#              {"name":"acct-2","config_dir":"~/.claude-acct2"}]}
# ---------------------------------------------------------------------------
_PLANS_PATH = Path.home() / ".config" / "emux" / "plans.json"
_SESSION_PLAN: dict[str, str] = {}      # session -> plan name it's currently on
_PLAN_EXHAUSTED: dict[str, float] = {}  # plan name -> unix time it may be retried
_PLAN_COOLDOWN = 5 * 3600.0             # assume a Claude usage window is ~5h if unknown


def _plans() -> list[dict[str, str]]:
    """Configured Claude accounts to fail over between. Falls back to a single
    default plan (the current ~/.claude) so nothing breaks unconfigured."""
    default = [{"name": "default", "config_dir": str(Path.home() / ".claude")}]
    try:
        data = json.loads(_PLANS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return default
    out = []
    for p in data.get("plans") or []:
        name, cfg = str(p.get("name", "")).strip(), str(p.get("config_dir", "")).strip()
        if name and cfg:
            out.append({"name": name, "config_dir": os.path.expanduser(cfg)})
    return out or default


def _plan_available(name: str, now: float) -> bool:
    reset = _PLAN_EXHAUSTED.get(name)
    return reset is None or now >= reset


def _next_plan(current: str | None, now: float) -> dict[str, str] | None:
    """The next configured plan that isn't the current one and isn't still
    cooling down from its own exhaustion. Round-robin from the current."""
    plans = _plans()
    if not plans:
        return None
    order = plans
    if current:
        idx = next((i for i, p in enumerate(plans) if p["name"] == current), -1)
        if idx >= 0:
            order = plans[idx + 1:] + plans[:idx + 1]   # start after current, wrap
    for p in order:
        if p["name"] != current and _plan_available(p["name"], now):
            return p
    return None


def _company_by_key(key: str | None) -> dict[str, str] | None:
    """An explicit company override on a registry entry (e.g. a remote worker
    whose cwd we can't see locally, or a manager). Maps the key to its pill."""
    if not key:
        return None
    for k, label, color, _roots, _kw in _COMPANY_TABLE:
        if k == key:
            return {"company": k, "label": label, "color": color}
    return None


def _detect_company(cwd: str | None) -> dict[str, str]:
    """Best-effort: which company/context owns a session, from its working dir.
    Match the repos-<x>/ root first (authoritative); fall back to a company
    keyword anywhere in the path, so e.g. repos/greenmark-claude-toolkit lands."""
    low = (cwd or "").lower()
    if not low:
        return {"company": "", "label": "", "color": ""}
    for key, label, color, roots, _kw in _COMPANY_TABLE:
        if any(r in low for r in roots):
            return {"company": key, "label": label, "color": color}
    for key, label, color, _roots, kw in _COMPANY_TABLE:
        if any(k in low for k in kw):
            return {"company": key, "label": label, "color": color}
    return {"company": "", "label": "", "color": ""}


def _known_hosts() -> list[str]:
    """Machines you can spawn on: local + ~/.ssh/config aliases + hosts already
    used by registered sessions."""
    hosts: list[str] = []
    cfg = Path(os.path.expanduser("~/.ssh/config"))
    if cfg.is_file():
        try:
            for line in cfg.read_text(errors="replace").splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() == "host":
                    hosts.extend(h for h in parts[1:] if "*" not in h and "?" not in h)
        except OSError:
            pass
    try:
        for e in _server._load_registry().values():
            h = e.get("host")
            if isinstance(h, str) and h:
                hosts.append(h)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), ["local"]
    for h in hosts:
        if h not in seen and h != "local":
            seen.add(h)
            out.append(h)
    return out


_DIRS_CACHE: dict[str, tuple[float, list[str]]] = {}
_DIRS_TTL = 120.0


def _candidate_dirs(host: str | None = None, limit: int = 200) -> list[str]:
    """Working directories that ACTUALLY EXIST on `host` (None/local = this box).

    The choices cascade: which directories you can start a session in depends on
    which machine you picked, so this is always resolved against a real machine —
    never a local list handed to a remote spawn."""
    key = host or "local"
    hit = _DIRS_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _DIRS_TTL:
        return hit[1]
    dirs: list[str] = []
    if key == "local":
        for base in sorted(Path.home().glob("repos*")):
            if base.is_dir():
                dirs.extend(str(d) for d in sorted(base.iterdir())
                            if d.is_dir() and not d.name.startswith("."))
    else:
        import subprocess
        try:
            # one ssh round-trip; list the repo trees one level deep
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", key,
                 "for b in ~/repos*; do [ -d \"$b\" ] && "
                 "for d in \"$b\"/*/; do [ -d \"$d\" ] && echo \"${d%/}\"; done; done"],
                capture_output=True, text=True, timeout=25,
            )
            if proc.returncode == 0:
                dirs = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        except (subprocess.TimeoutExpired, OSError):
            dirs = []
    dirs = dirs[:limit]
    _DIRS_CACHE[key] = (time.time(), dirs)
    return dirs


def _running_sessions(host: str | None = None) -> list[dict[str, Any]]:
    """tmux sessions ALREADY RUNNING on `host` — the things you can RESUME.

    The other half of "what exists on that machine": you either start fresh in a
    directory, or you pick up something that's already there."""
    fmt = "#{session_name}|#{session_created}|#{session_attached}|#{session_windows}|#{session_path}"
    try:
        code, out, _ = _server._run_tmux(["ls", "-F", fmt], host=host, timeout=20)
    except (FileNotFoundError, Exception):  # noqa: BLE001
        return []
    if code != 0:
        return []
    known = {e.get("session") for e in _server._load_registry().values()}
    rows: list[dict[str, Any]] = []
    now = time.time()
    for ln in (out or "").splitlines():
        p = ln.strip().split("|")
        if len(p) < 5:
            continue
        try:
            created = int(p[1])
        except ValueError:
            continue
        rows.append({
            "name": p[0],
            "created_unix": created,
            "age_sec": max(0, int(now - created)),
            "attached": p[2] == "1",
            "windows": p[3],
            "path": p[4],
            "adopted": p[0] in known,   # already in the emux registry
        })
    rows.sort(key=lambda r: r["created_unix"], reverse=True)   # most recent first
    return rows


_UNSENT_RE = re.compile(r"^[❯>]\s+\S", re.MULTILINE)


def _peek_session(session: str, host: str | None = None, lines: int = 12) -> dict[str, Any]:
    """Look INSIDE a running session before you touch it.

    Resuming is not creating: the thing already has state. You need to see which
    one it is, whether it is holding an unsent prompt someone typed, and whether
    a terminal is already attached — all of which attaching could disturb."""
    try:
        code, out, err = _server._run_tmux(
            ["capture-pane", "-t", session, "-p", "-S", f"-{lines}"],
            host=host, timeout=20)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if code != 0:
        return {"ok": False, "error": (err or "capture failed").strip()}
    text = out or ""
    body = [ln for ln in text.splitlines() if ln.strip()]
    return {
        "ok": True, "session": session, "host": host,
        "content": "\n".join(body[-lines:]),
        # a line like "❯ do the thing" is a prompt typed but never submitted
        "unsent": bool(_UNSENT_RE.search(text)),
    }


def _adopt_session(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an ALREADY-RUNNING session (local or remote) into emux."""
    import asyncio
    session = (data.get("session") or "").strip()
    if not session:
        return {"ok": False, "error": "missing_session"}
    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    name = (data.get("name") or "").strip() or session
    try:
        r = asyncio.run(_server.tmux_register(
            name=name, session=session, host=host,
            description=(data.get("description") or "").strip() or None,
            tags=(data.get("tags") or ["adopted"]),
        ))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    if data.get("gui"):
        _iterm_attach(session, host)   # a real terminal on it — over ssh when remote
    return {"ok": True, "name": name, "session": session, "host": host, **(r or {})}


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply."""
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return {"_error": "no JSON in model reply", "raw": (text or "")[:300]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"_error": "unparseable JSON", "raw": m.group(0)[:300]}


def _agent_path_prefix() -> str:
    """Dirs launchd usually omits — prepend so which/spawn can find claude/grok."""
    home = Path.home()
    return os.pathsep.join(
        [
            str(home / ".local" / "bin"),
            str(home / "bin"),
            str(home / ".grok" / "bin"),
            "/opt/homebrew/bin",
            "/usr/local/bin",
        ]
    )


def _resolve_cli(name: str) -> str | None:
    """Absolute path to a CLI (claude/grok). launchd PATH is often /usr/bin:/bin only."""
    import shutil

    env_key = f"EMUX_{name.upper()}_BIN"
    override = (os.environ.get(env_key) or "").strip()
    if override and Path(override).is_file():
        return override
    # Prefer an augmented PATH over bare which() under launchd.
    search_path = _agent_path_prefix() + os.pathsep + (os.environ.get("PATH") or "")
    found = shutil.which(name, path=search_path)
    if found:
        return found
    for cand in (
        Path.home() / ".local" / "bin" / name,
        Path.home() / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def _ensure_agent_path_env() -> None:
    """Mutate process PATH once so gist/spawn children see claude (launchd-safe)."""
    prefix = _agent_path_prefix()
    cur = os.environ.get("PATH") or ""
    if prefix.split(os.pathsep)[0] in cur.split(os.pathsep):
        return
    os.environ["PATH"] = prefix + os.pathsep + cur if cur else prefix


def _claude_json(prompt: str, timeout: int = 90) -> dict[str, Any]:
    """One fixed-cost `claude -p` call that must answer with a JSON object.
    Never the API — the CLI only."""
    import subprocess

    _ensure_agent_path_env()
    claude = _resolve_cli("claude")
    if claude is None:
        return {"_error": "claude CLI not on PATH (checked ~/.local/bin, brew, EMUX_CLAUDE_BIN)"}
    try:
        proc = subprocess.run(
            [claude, "-p", prompt, "--model",
             os.environ.get("EMUX_NAV_MODEL", "claude-haiku-4-5-20251001")],
            capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PATH": os.environ.get("PATH", "")},
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"_error": f"claude -p failed: {e}"}
    return _extract_json(proc.stdout or "")


# --- model routing: send cheap high-volume tasks (The Gist, menu/reply) to a
# self-hosted NIM to spare the Claude subscription. NIM MUST be fixed-cost
# (local/self-hosted) — never a metered cloud endpoint. Falls back to claude -p
# whenever NIM is unset or unreachable, so nothing breaks. ---
_MODELS_PATH = Path.home() / ".config" / "emux" / "models.json"
_TASKS = ("gist", "placement")   # model-backed tasks that can be routed


def _model_config() -> dict[str, Any]:
    cfg = {"nim": {"base_url": "", "model": "", "api_key": ""},
           "routes": dict.fromkeys(_TASKS, "claude")}
    try:
        disk = json.loads(_MODELS_PATH.read_text())
        if isinstance(disk.get("nim"), dict):
            cfg["nim"].update({k: disk["nim"].get(k, "") for k in cfg["nim"]})
        if isinstance(disk.get("routes"), dict):
            for t in _TASKS:
                if disk["routes"].get(t) in ("claude", "nim"):
                    cfg["routes"][t] = disk["routes"][t]
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def _save_model_config(data: dict[str, Any]) -> dict[str, Any]:
    cfg = _model_config()
    nim = data.get("nim") or {}
    for k in ("base_url", "model", "api_key"):
        if isinstance(nim.get(k), str):
            cfg["nim"][k] = nim[k].strip()
    routes = data.get("routes") or {}
    for t in _TASKS:
        if routes.get(t) in ("claude", "nim"):
            cfg["routes"][t] = routes[t]
    _MODELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _MODELS_PATH.write_text(json.dumps(cfg, indent=2))
    return cfg


def _nim_json(prompt: str, timeout: int = 60) -> dict[str, Any]:
    """OpenAI-compatible chat call to a self-hosted NIM. Stdlib only."""
    import urllib.error
    import urllib.request
    nim = _model_config()["nim"]
    base, model = nim.get("base_url", ""), nim.get("model", "")
    if not base or not model:
        return {"_error": "nim_not_configured"}
    url = base.rstrip("/") + "/chat/completions"
    body = json.dumps({"model": model, "temperature": 0.2,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    headers = {"Content-Type": "application/json"}
    if nim.get("api_key"):
        headers["Authorization"] = "Bearer " + nim["api_key"]
    try:
        req = urllib.request.Request(url, data=body, headers=headers)  # noqa: S310 (user-configured local endpoint)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read())
        content = payload["choices"][0]["message"]["content"]
    except (urllib.error.URLError, KeyError, IndexError, ValueError, TimeoutError) as e:
        return {"_error": f"nim_failed: {e}"}
    return _extract_json(content)


def _llm_json(prompt: str, task: str = "", timeout: int = 90) -> dict[str, Any]:
    """Dispatch a JSON model call by the task's configured route. NIM for routed
    tasks (fixed-cost), else claude -p; NIM failures fall back to claude -p."""
    if task and _model_config()["routes"].get(task) == "nim":
        r = _nim_json(prompt, timeout=min(timeout, 60))
        if "_error" not in r:
            return r
        # NIM down/misconfigured → don't fail the feature, use the subscription CLI
    return _claude_json(prompt, timeout=timeout)


def _nim_ping() -> dict[str, Any]:
    """Check a configured NIM is reachable (GET /models). For the settings 'Test'."""
    import urllib.error
    import urllib.request
    nim = _model_config()["nim"]
    base = nim.get("base_url", "")
    if not base:
        return {"ok": False, "error": "no base_url set"}
    url = base.rstrip("/") + "/models"
    headers = {}
    if nim.get("api_key"):
        headers["Authorization"] = "Bearer " + nim["api_key"]
    try:
        req = urllib.request.Request(url, headers=headers)  # noqa: S310
        with urllib.request.urlopen(req, timeout=6) as resp:  # noqa: S310
            data = json.loads(resp.read())
        ids = [m.get("id") for m in data.get("data", []) if isinstance(m, dict)]
        return {"ok": True, "models": [i for i in ids if i][:20]}
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        return {"ok": False, "error": str(e)}


_NEW_VERBS = ("create", "new ", "start a new", "start new", "spin up", "spin-up",
              "build a", "build me", "set up", "set-up", "make a", "make me",
              "i want a", "i want an", "i need a", "i need an", "launch a",
              "fresh", "from scratch", "stand up", "spawn")
_RESUME_VERBS = ("resume", "pick up", "pick back up", "continue", "reattach",
                 "re-attach", "reconnect", "get back to", "back to the",
                 "the one i was", "that i had", "already running", "existing",
                 "still running", "left off")


def _bm25_rank(intent: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank running sessions by lexical relevance to the intent (BM25 over
    name+path+description+tags). Cheap, stdlib, no API — the resume path presents
    the LLM a RELEVANCE-ORDERED list instead of raw registration order, so
    "the greenmark reconcile work" floats the reconcile session to the top.

    Deliberately lexical-only for now: at this fleet size the LLM reads the whole
    (ranked) list, so dense embeddings + RRF would be scale-work for no gain.
    When the fleet outgrows what fits in one prompt, add a dense ranker and fuse
    with this via RRF — this function is the bm25 leg of that."""
    import math
    from collections import Counter

    def tok(s: str) -> list[str]:
        return re.findall(r"[a-z0-9]+", (s or "").lower())
    docs = [tok(" ".join([s.get("name", ""), s.get("path", ""),
                          s.get("description", ""), " ".join(s.get("tags") or [])]))
            for s in sessions]
    if not any(docs):
        return sessions
    n = len(docs)
    avgdl = sum(len(d) for d in docs) / n or 1.0
    df: Counter = Counter()
    for d in docs:
        df.update(set(d))
    q = tok(intent)
    k1, b = 1.5, 0.75
    scored = []
    for s, d in zip(docs, sessions):  # noqa: B905 (py3.9 compat, equal-length)
        tf = Counter(s)
        score = sum(
            math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            * (tf[t] * (k1 + 1)) / (tf[t] + k1 * (1 - b + b * len(s) / avgdl))
            for t in q if t in tf)
        scored.append((score, d))
    order = sorted(range(len(scored)), key=lambda i: scored[i][0], reverse=True)
    return [dict(scored[i][1], _relevance=round(scored[i][0], 2)) for i in order]


def _intent_company_hint(intent: str) -> tuple[str, str] | None:
    """If the wording names a company (by name or a company keyword), return it.
    Used to bias the directory choice — "an Eidos digest of the org" must not
    land in an AIC repo just because the model liked a repo name there."""
    low = f" {(intent or '').lower()} "
    for key, label, _color, _roots, kw in _COMPANY_TABLE:
        if f" {key} " in low or any(k in low for k in kw if len(k) > 3):
            return key, label
    return None


def _new_vs_resume_lean(intent: str) -> str:
    """A deterministic keyword lean, fed to the model as a HINT (not an override).

    The AI over-weighted a name match ("...manager" → resume the running
    ggo-manager) against an explicit "I want to create". This lifts the wording
    signal so the model weighs it, without hard-coding the decision."""
    low = f" {(intent or '').lower()} "
    n = sum(1 for v in _NEW_VERBS if v in low)
    r = sum(1 for v in _RESUME_VERBS if v in low)
    if n > r:
        return ('the wording leans NEW — they used create/new/build/"I want a" '
                "language, so treat a similarly-named running session as a "
                "coincidence, not a resume target")
    if r > n:
        return "the wording leans RESUME — they pointed at an existing session"
    return "the wording is neutral — decide from what they actually describe"


def _suggest_session(intent: str, host: str | None = None) -> dict[str, Any]:
    """Turn plain English into a session, by CLASSIFYING it down the cascade of
    real choices — never by inventing values.

    The choices depend on each other: the machine determines which directories
    exist, so we resolve the machine FIRST, enumerate that machine's real repo
    dirs, and only then choose among THOSE. The model picks by index from a list
    of things that actually exist, so it cannot hallucinate a path onto a box.

    `host` pins the machine (the user already chose it) and skips step 1.
    """
    hosts = _known_hosts()

    # a company named in the wording routes BOTH the machine and the directory.
    hint = _intent_company_hint(intent)
    # --- step 1: which machine? (skipped when the user already picked one) ---
    if host:
        chosen_host, host_why = host, ""
    else:
        # per-company home machine (a standing preference, not a per-session ask).
        pref = _routing_prefs()["company_host"].get(hint[0]) if hint else None
        pref = pref if pref in hosts else None
        mhint = (f"\nMACHINE PREFERENCE: {hint[1]} work runs on '{pref}' — pick it "
                 f"unless the intent explicitly names another machine.\n"
                 if (pref and hint) else "")
        r1 = _llm_json(
            "Pick the MACHINE to start a terminal session on.\n\n"
            f"WHAT THE USER WANTS TO DO: {intent}\n"
            f"{mhint}\n"
            "MACHINES (choose exactly one of these names):\n"
            + "\n".join(f"  {h}" for h in hosts) + "\n\n"
            'Reply with ONE line of JSON: {"host": "<one of the names above>", '
            '"why": "<short reason>"}\n'
            'Choose "local" unless a machine preference above applies, or the '
            "intent clearly names a remote machine or needs one."
        )
        if "_error" in r1:
            return r1
        chosen_host = r1.get("host") or "local"
        host_why = r1.get("why") or ""
        if chosen_host not in hosts:          # constrain to a real machine
            chosen_host = "local"

    # --- step 2: what actually exists ON that machine — BOTH kinds ---
    rhost = None if chosen_host == "local" else chosen_host
    dirs = _candidate_dirs(rhost)
    running = _running_sessions(rhost)      # things you could RESUME instead
    base = {"host": chosen_host, "whyHost": host_why,
            "dirs": dirs, "running": running}
    if not dirs and not running:
        return {**base, "action": "new", "name": "", "cwd": "", "command": "",
                "why": f"could not list anything on {chosen_host}", "verified": False}

    # --- step 3: resume something already there, or start fresh in a directory? ---
    lean = _new_vs_resume_lean(intent)
    # enrich with registry description/tags, then rank by BM25 relevance so the
    # session the intent describes floats to the top of what the LLM picks from.
    reg = _server._load_registry()
    reg_by_session = {e.get("session"): e for e in reg.values()}
    for s in running:
        e = reg_by_session.get(s["name"]) or {}
        s.setdefault("description", e.get("description") or "")
        s.setdefault("tags", e.get("tags") or [])
    running = _bm25_rank(intent, running)
    sess_list = "\n".join(
        f"  [{i}] {s['name']} — {_ago(s['age_sec'])} old, {s['path']}"
        f"{' (attached)' if s['attached'] else ''}"
        f"{'  [most relevant]' if i == 0 and s.get('_relevance', 0) > 0 else ''}"
        for i, s in enumerate(running)) or "  (none running)"
    # If the wording names a company, float ITS directories to the top and tell
    # the model — so "an Eidos org digest" doesn't land in an AIC repo.
    hint = _intent_company_hint(intent)
    co_hint = ""
    if hint:
        hk, hl = hint
        dirs = sorted(dirs, key=lambda d: 0 if _detect_company(d).get("company") == hk else 1)
        co_hint = (f"\nCOMPANY: the wording names {hl}. Prefer a {hl} directory "
                   f"(they are listed FIRST). Do NOT pick another company's repo "
                   f"unless the intent clearly points there.\n")
    dir_list = "\n".join(f"  [{i}] {d}" for i, d in enumerate(dirs)) or "  (none)"
    r2 = _llm_json(
        f"A developer wants a terminal session on machine '{chosen_host}'.\n\n"
        f"WHAT THEY SAID: {intent}\n\n"
        "They can either RESUME a session already running on that machine, or "
        "start a NEW one in a directory. Choose the one that matches what they said.\n\n"
        f"PHRASING HINT (from their wording, weigh it — do not obey blindly): {lean}\n"
        f"{co_hint}\n"
        f"ALREADY RUNNING ON {chosen_host} (resume one of these):\n{sess_list}\n\n"
        f"DIRECTORIES ON {chosen_host} (start a new session in one of these):\n{dir_list}\n\n"
        'Reply with ONE line of JSON, nothing else:\n'
        '{"action": "resume" | "new", "session_index": <int, only if resume>, '
        '"dir_index": <int, only if new>, "name": "<short-kebab-session-name>", '
        '"command": "<command for a NEW session; empty string for a plain shell>", '
        '"why": "<one short sentence>"}\n'
        'Resume ONLY when they point at an EXISTING session ("resume", "pick up", '
        '"continue", "reattach", "the one I was working on"). If they say CREATE / '
        'NEW / START / BUILD / SET UP / "I want a ___", it is a NEW session EVEN IF '
        'a running session has a similar name — naming the KIND of thing to make '
        '(a manager, a worker, a bot) is NOT a request to resume something. '
        'Indices MUST come from the '
        "lists above. If they ask for the most recent, note the lists are "
        "ordered newest-first."
    )
    if "_error" in r2:
        return {**base, **r2}

    action = "resume" if str(r2.get("action")) == "resume" else "new"
    why = r2.get("why") or ""

    if action == "resume":
        si = r2.get("session_index")
        if isinstance(si, int) and 0 <= si < len(running):
            s = running[si]
            return {**base, "action": "resume", "verified": True,
                    "session": s["name"], "cwd": s["path"],
                    "name": r2.get("name") or s["name"],
                    "command": "", "why": why}
        return {**base, "action": "resume", "verified": False,
                "session": "", "cwd": "", "name": "", "command": "",
                "why": why or "could not identify which session to resume"}

    di = r2.get("dir_index")
    cwd, verified = "", False
    if isinstance(di, int) and 0 <= di < len(dirs):
        cwd, verified = dirs[di], True   # a real dir on that machine, not invented

    # WHICH AGENT runs here is a routing decision with a registry behind it —
    # not something the model should free-style. Registry wins; the model's
    # command is only a fallback when nothing routes.
    from . import agents as _agents
    adv = _agents.advise(intent)
    command = r2.get("command") or ""
    agent_why = ""
    if adv.get("matched") and adv.get("command"):
        command = adv["command"]
        agent_why = f"{adv['agent']}: {adv['why']}"
    return {**base, "action": "new", "verified": verified,
            "session": "", "cwd": cwd,
            "name": r2.get("name") or "",
            "command": command, "why": why,
            "agent": adv.get("agent"), "agentWhy": agent_why}


def _ago(sec: int) -> str:
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _reply_suggestions(session: str, host: str | None, *, force: bool = False) -> dict[str, Any]:
    """The reader's-digest + 'what do I say' for a session you're looking at.

    Reads the recent pane and asks a model (claude -p / a self-hosted NIM, never a
    metered API) for a 1-2 sentence digest plus a few ready-to-send replies with
    confidences, so a human staring at a wall of output knows the gist AND what to
    do next. CACHED by content hash: while the pane is unchanged the cached result
    is returned instantly (no model call); the poll loop warms it the moment a
    session stops, so it's ready before you open the modal."""
    cap = capture_payload(session, 45, host=host)
    if not cap.get("ok"):
        return {"ok": False, "error": cap.get("error", "capture_failed")}
    pane = cap.get("content", "")
    h = _gist_hash(pane)
    if not force:
        with _LOCK:
            c = _GIST_CACHE.get(session)
        if c and c.get("hash") == h:   # pane unchanged since we last summarized → serve cache
            return {"ok": True, "digest": c["digest"], "suggestions": c["suggestions"],
                    "cached": True}
    out = _compute_gist(pane)
    if out.get("ok"):
        with _LOCK:
            _GIST_CACHE[session] = {"hash": h, "digest": out["digest"],
                                    "suggestions": out["suggestions"], "ts": time.time()}
    return out


def _warm_gist(session: str, host: str | None) -> None:
    """Populate the gist cache for a session in the background (dedupe concurrent
    warms). Fired when a session stops, so its digest is ready on open."""
    with _LOCK:
        if session in _GIST_INFLIGHT:
            return
        _GIST_INFLIGHT.add(session)
    try:
        _reply_suggestions(session, host)
    except Exception:  # noqa: BLE001, S110  (best-effort warm; never crash the poll)
        pass
    finally:
        with _LOCK:
            _GIST_INFLIGHT.discard(session)


def _compute_gist(pane: str) -> dict[str, Any]:
    """The actual model call + parse for the gist (no caching)."""
    r = _llm_json(
        "A human is looking at an AI agent's terminal session and may not know how "
        "to respond. Here is its recent output:\n<<<\n" + pane[-3500:] + "\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else:\n"
        '{"digest": "<1-2 plain sentences: what the agent is doing, or the '
        'decision/question it is waiting on>", "suggestions": [{"text": "<a short, '
        'ready-to-send reply>", "confidence": <0-100>}, ...]}\n'
        "Give 0-4 suggestions — each a concrete message the human could send RIGHT "
        "NOW to move it forward (approve, redirect, answer, ask a clarifier). Keep "
        "each under ~14 words, phrased as the human talking TO the agent. "
        "For each, set `confidence` = how likely THIS reply is the right move for the "
        "human right now (0-100). Make them genuinely comparative — the best option "
        "high, weaker ones lower; they need not sum to 100. Order best-first. "
        "IMPORTANT: phrase each as a DECISIVE instruction that authorises the agent "
        "to PROCEED and finish autonomously without coming back to ask again — e.g. "
        "'yes, switch it and proceed — don't re-confirm' rather than a bare 'yes'. "
        "The human clicking this wants it handled, not another round of questions. "
        "(A 'cancel/reverse/hold off' option is the one exception that may stop it.) "
        "If the agent is just working and needs nothing, return an empty suggestions "
        "list and say so in the digest.", task="gist", timeout=45)
    if "_error" in r:
        return {"ok": False, "error": r["_error"]}
    sugg = []
    for s in (r.get("suggestions") or [])[:4]:
        if isinstance(s, dict) and str(s.get("text", "")).strip():
            try:
                conf = max(0, min(100, int(round(float(s.get("confidence", 50))))))
            except (TypeError, ValueError):
                conf = 50
            sugg.append({"text": str(s["text"]).strip(), "confidence": conf})
        elif isinstance(s, str) and s.strip():   # tolerate a plain-string reply
            sugg.append({"text": s.strip(), "confidence": 50})
    return {"ok": True, "digest": (r.get("digest") or "").strip(), "suggestions": sugg}


def _spawn_session(data: dict[str, Any]) -> dict[str, Any]:
    """Create a new session (local or remote) — and KICKSTART it.

    A session created from "say what you want to do" should start DOING it, not
    boot to a blank composer. So when the command is a known agent and there's an
    intent, we hand that intent to the agent as its opening prompt (`claude
    <prompt>` / `codex <prompt>`) — the session comes up already working."""
    import asyncio
    import shlex
    name = (data.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "missing_name"}
    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    cwd = (data.get("cwd") or "").strip() or None
    command = (data.get("command") or "").strip() or None
    intent = (data.get("prompt") or data.get("description") or "").strip()

    kicked = False
    if command and intent:
        from . import adapters
        a = adapters.detect(command.split()[0])
        if a is not None:      # it's a real agent → launch it ON the task
            command = f"{command} {shlex.quote(intent)}"
            kicked = True
    try:
        r = asyncio.run(_server.tmux_spawn(
            name=name, command=command, host=host, cwd=cwd,
            gui=bool(data.get("gui", False)),
            description=(data.get("description") or "").strip() or None,
            tags=data.get("tags") or None,
        ))
        if isinstance(r, dict):
            r["kickstarted"] = kicked
        return r
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _normalize_chat_cwd(cwd: str | None) -> str | None:
    """Claude project dirs sometimes decode without a leading slash (Volumes/…)."""
    if not cwd:
        return None
    c = cwd.strip()
    if not c or c == "~":
        return None
    if c.startswith("Volumes/") or c.startswith("Users/") or c.startswith("private/"):
        return "/" + c
    return c


def _resume_chat_in_fleet(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an abandoned Claude/Grok *transcript* back as a live fleet session.

    CHATS rows are disk transcripts, not tmux. Resume = spawn tmux on this host
    (where the transcript lives), launch `claude --resume <id>` or
    `grok --resume <id>` (absolute bin via _resolve_cli / grok_control),
    register under a chat-* name. Distinct from /api/adopt (already-running
    tmux) and from copy-paste resume (no fleet registration).
    """
    import asyncio
    import shlex

    tool = (data.get("tool") or "").strip().lower()
    sid = (data.get("session_id") or data.get("id") or "").strip()
    if tool not in ("claude", "grok"):
        return {"ok": False, "error": "tool_must_be_claude_or_grok"}
    if not sid:
        return {"ok": False, "error": "missing_session_id"}

    host = (data.get("host") or "").strip()
    if host in ("", "local"):
        host = None
    # Transcripts are scanned on the daemon host only (v1). Remote resume would
    # need remote home stores — refuse rather than spawn a blank wrong-host agent.
    if host is not None:
        return {
            "ok": False,
            "error": "chat_resume_local_only",
            "hint": "transcripts live on this host; resume spawns a local tmux session",
        }

    try:
        from . import chats as chat_find
    except ImportError as exc:
        return {"ok": False, "error": f"chats_unavailable: {exc}"}

    force = bool(data.get("force"))
    if (
        tool == "claude"
        and not force
        and sid.lower() in chat_find._claude_live_ids()
    ):
        return {
            "ok": False,
            "error": "already_live",
            "hint": "CLI still holds this chat — attach/boss it, or pass force=true to re-spawn",
            "session_id": sid,
            "tool": tool,
        }
    if tool == "grok" and not force and sid in chat_find._grok_live_ids():
        return {
            "ok": False,
            "error": "already_live",
            "hint": "process still holds this chat — attach/boss it, or pass force=true",
            "session_id": sid,
            "tool": tool,
        }

    cwd = _normalize_chat_cwd((data.get("cwd") or "").strip() or None)
    name = (data.get("name") or "").strip()
    if not name:
        name = f"chat-{tool}-{sid[:8]}"
    # tmux session names: no dots/colons
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:48] or f"chat-{tool}"

    title = (data.get("title") or sid)[:100]
    tags = ["chat-resume", tool, "abandoned"]
    if data.get("greenmark") or (cwd and re.search(r"greenmark|gmw|cerebro|gms", cwd, re.I)):
        tags.append("greenmark")
    # Embed Grok/Claude transcript id so headless/ACP steer can find it later
    tags.append(f"gsid:{sid}" if tool == "grok" else f"csid:{sid}")
    desc = f"resumed {tool} transcript · {title}"

    _ensure_agent_path_env()
    # Absolute binary + exec: launchd/tmux often lack ~/.local/bin; bare `claude`
    # then fails and keys dump into zsh (looks "stuck" with a shell prompt).
    if tool == "claude":
        bin_path = _resolve_cli("claude")
        if not bin_path:
            return {
                "ok": False,
                "error": "claude_not_found",
                "hint": "install claude or set EMUX_CLAUDE_BIN to the absolute path",
            }
        command = f"exec {shlex.quote(bin_path)} --resume {shlex.quote(sid)}"
    else:
        # Prefer grok_control: absolute bin + documented `grok --resume <id>`.
        try:
            from . import grok_control as _gc
        except ImportError:
            _gc = None  # type: ignore[assignment]
        bin_path = (
            (_gc.resolve_grok_bin() if _gc is not None else None)
            or _resolve_cli("grok")
        )
        if not bin_path:
            return {
                "ok": False,
                "error": "grok_not_found",
                "hint": "install grok or set EMUX_GROK_BIN to the absolute path",
            }
        if _gc is not None:
            command = _gc.resume_shell_command(
                sid, bin_path=bin_path, cwd=None, use_exec=True
            )
        else:
            command = f"exec {shlex.quote(bin_path)} --resume {shlex.quote(sid)}"

    try:
        r = asyncio.run(
            _server.tmux_spawn(
                name=name,
                command=command,
                host=None,
                cwd=cwd,
                gui=bool(data.get("gui", False)),
                description=desc,
                tags=tags,
            )
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "tool": tool, "session_id": sid}

    if not isinstance(r, dict):
        return {"ok": False, "error": "spawn_failed", "tool": tool, "session_id": sid}
    if not r.get("ok", True):
        return {**r, "tool": tool, "session_id": sid, "fleet_name": name}

    session = str(r.get("session") or name)

    # Wait until the pane is actually the agent (not a leftover zsh prompt).
    agent_up = _wait_pane_command(session, want=("claude", "node", "grok"), timeout=12.0)
    if not agent_up:
        # One retry: clear line and re-exec absolute binary
        send_payload(session, "C-c", literal=False, enter=False, host=None)
        time.sleep(0.3)
        send_payload(session, command, literal=True, enter=True, host=None)
        agent_up = _wait_pane_command(session, want=("claude", "node", "grok"), timeout=10.0)

    # Durable memory: never re-discover this mission as "new abandoned" work
    try:
        from . import chats_store

        store = chats_store.open_store()
        try:
            store.mark_resumed(tool, sid, name, notes=f"fleet:{session}")
        finally:
            store.close()
    except Exception:  # noqa: BLE001
        pass

    out = {
        "ok": True,
        "name": name,
        "session": session,
        "tool": tool,
        "session_id": sid,
        "command": command,
        "bin": bin_path,
        "cwd": cwd,
        "tags": tags,
        "description": desc,
        "agent_up": agent_up,
        **{k: v for k, v in r.items() if k not in ("ok", "name", "session")},
    }
    if not agent_up:
        out["partial"] = True
        out["warning"] = (
            "spawned but pane not yet running agent binary — "
            "wait a few seconds or attach; do not send prompts into a bare shell"
        )
    return out


def _wait_pane_command(
    session: str,
    want: tuple[str, ...],
    timeout: float = 10.0,
    host: str | None = None,
) -> bool:
    """True if pane looks like an agent (not bare shell) within timeout.

    Claude Code sometimes reports pane_current_command as a version string
    (e.g. 2.1.218) rather than 'claude' — also accept capture containing
    'Claude Code'.
    """
    deadline = time.time() + timeout
    want_l = {w.lower() for w in want}
    shells = {"zsh", "bash", "sh", "-zsh", "-bash", "fish", ""}
    while time.time() < deadline:
        try:
            code, out, _err = _server._run_tmux(
                ["display-message", "-p", "-t", session, "#{pane_current_command}"],
                host=host,
            )
            cmd = (out or "").strip().lower()
            if code == 0 and any(w in cmd for w in want_l):
                return True
            # non-shell process name (versioned claude binary, node, …)
            if code == 0 and cmd and cmd not in shells and not cmd.startswith("-"):
                # confirm with a short capture when command name is odd
                c2, pane, _e2 = _server._run_tmux(
                    ["capture-pane", "-p", "-t", session, "-J"],
                    host=host,
                )
                blob = (pane or "")
                if "Claude Code" in blob or "claude" in cmd or "node" in cmd:
                    return True
                if c2 == 0 and cmd[0].isdigit():  # e.g. 2.1.218
                    return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


# which tool-calls are worth showing in the live feed — the fleet's meaningful
# moves, not the capture/poll/classify read-noise that runs every tick.
_FEED_OPS = {"tmux_spawn", "tmux_send", "tmux_register", "tmux_unregister",
             "move_to_emux", "tmux_signals", "tmux_wait"}


def _events(limit: int = 60) -> list[dict[str, Any]]:
    """Merge the fleet's recent activity into one time-ordered feed: up-channel
    signals (IDLE/DONE/NEED/ERROR/PROGRESS) from every session inbox, plus the
    meaningful tool-calls from the audit trail. This is what a human watches to
    see what the agents are doing as they do it."""
    ev: list[dict[str, Any]] = []
    inbox = getattr(_server, "_INBOX_DIR", None)
    if inbox and inbox.is_dir():
        for f in inbox.glob("*.jsonl"):
            try:
                lines = f.read_text(errors="replace").splitlines()[-25:]
            except OSError:
                continue
            for ln in lines:
                try:
                    r = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                ev.append({
                    "ts": r.get("t", 0), "kind": "signal",
                    "tag": r.get("kind", "?"),
                    "session": r.get("session") or f.stem,
                    "text": r.get("payload") or "",
                })
    audit = getattr(_server, "_AUDIT_PATH", None)
    if audit and audit.is_file():
        try:
            alines = audit.read_text(errors="replace").splitlines()[-400:]
        except OSError:
            alines = []
        for ln in alines:
            try:
                r = json.loads(ln)
            except json.JSONDecodeError:
                continue
            op, ok = r.get("op", ""), r.get("ok", True)
            if op not in _FEED_OPS and ok:
                continue          # skip read-noise; always keep errors
            ev.append({
                "ts": r.get("t", 0), "kind": "error" if not ok else "op",
                "tag": op,
                "session": r.get("target") or r.get("name") or "",
                "text": ("" if ok else f"error: {r.get('error', '')}"),
            })
    ev.sort(key=lambda e: e["ts"], reverse=True)
    return ev[:limit]


def _host_os() -> str:
    """The daemon host's OS (platform.system(), e.g. 'Darwin'/'Linux'). Stamped
    onto the page so the UI can gate macOS-only controls (iTerm2). Its own
    function so tests can pin it independently of the real host."""
    import platform
    return platform.system()


def _iterm_run(command: str, focus: bool = False) -> tuple[bool, str | None]:
    """Open a NEW iTerm2 window running `command`, driven by AppleScript — no
    `.command` file, so macOS Gatekeeper doesn't throw a quarantine prompt.

    `focus` is OFF by default and MUST stay off for anything that attaches to a
    session you did not just create. Activating steals OS keyboard focus, so a
    window that pops up mid-keystroke swallows what you were typing straight into
    a live agent's prompt — observed for real: adopting a 2-day-old Claude session
    running with bypass-permissions captured stray keys into its input box.
    """
    import platform
    import subprocess
    if platform.system() != "Darwin":
        return False, "macOS/iTerm2 only"
    esc = command.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "iTerm2"\n'
        " create window with default profile\n"
        " tell current session of current window to write text "
        f'"{esc}"\n'
        + (" activate\n" if focus else "")
        + "end tell"
    )
    try:
        g = subprocess.run(["osascript", "-e", script],
                           capture_output=True, text=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    if g.returncode != 0:
        return False, (g.stderr or g.stdout or "osascript failed").strip()
    return True, None


def _iterm_attach(session: str, host: str | None = None) -> tuple[bool, str | None]:
    """Open an iTerm2 window attached to `session` — over ssh when it's remote.

    Never takes focus: you are attaching to something that is ALREADY RUNNING and
    may hold an unsent prompt. The window appears; you click into it when you mean
    to type there."""
    if host:
        inner = (f"PATH=/opt/homebrew/bin:/usr/local/bin:$PATH "
                 f"tmux attach -t {shlex.quote(session)}")
        return _iterm_run(f"ssh -t {shlex.quote(host)} {shlex.quote(inner)}", focus=False)
    return _iterm_run(f"tmux attach -t {shlex.quote(session)}", focus=False)




def _pane_command(session: str) -> str:
    """The live foreground process name in the session's active pane."""
    code, out, _ = _server._run_tmux(
        ["display-message", "-p", "-t", session, "#{pane_current_command}"]
    )
    return out.strip() if code == 0 else ""


def _detect_agent(session: str, content: str) -> dict[str, str]:
    """Which AI agent is running in this pane.

    The ADAPTERS own this. What an agent looks like is part of that agent's
    contract — the semver-pane-title trick is a Claude fact and belongs with
    Claude, not in a private table in the web daemon."""
    from . import adapters
    cmd = _pane_command(session).lower()
    a = adapters.detect(cmd, content)
    if a is not None:
        return {"agent": a.key, "label": a.label, "glyph": a.glyph}
    if cmd in adapters.SHELLS:
        return {"agent": "shell", "label": "shell", "glyph": "$"}
    if cmd in adapters.EDITORS:
        return {"agent": "editor", "label": cmd, "glyph": "✎"}
    if cmd:
        return {"agent": cmd, "label": cmd, "glyph": "▸"}
    return {"agent": "unknown", "label": "—", "glyph": "·"}


def _normalize(content: str) -> str:
    """Reduce a pane capture to its meaningful content for change detection:
    drop per-line trailing whitespace, trailing blank lines, and spinner glyphs.
    Two captures that differ only by a cursor blink or a spinner frame normalize
    to the same string."""
    lines = [ln.rstrip() for ln in content.split("\n")]
    text = "\n".join(lines).rstrip("\n")
    return _SPINNER_RE.sub("", text)


def _observe(session: str, content: str) -> dict[str, Any]:
    """Fold one capture into the session's activity record. Returns its meta."""
    now = time.time()
    norm = _normalize(content)
    with _LOCK:
        st = _ACTIVITY.setdefault(session, {
            "norm": None, "changed": False, "last_change": None,
            "samples": deque(maxlen=_SAMPLE_WINDOW),
        })
        changed = st["norm"] is not None and st["norm"] != norm
        if changed:
            st["last_change"] = now
        st["norm"] = norm
        st["changed"] = changed
        st["samples"].append(1 if changed else 0)
        return _meta_locked(st, now)


def _meta_locked(st: dict[str, Any], now: float) -> dict[str, Any]:
    return {
        "changed": st["changed"],
        "last_change_age": (now - st["last_change"]) if st["last_change"] else None,
        "activity": list(st["samples"]),
    }


def _meta(session: str) -> dict[str, Any]:
    """Read the current activity meta for a session without re-observing."""
    with _LOCK:
        st = _ACTIVITY.get(session)
        if not st:
            return {"changed": False, "last_change_age": None, "activity": []}
        return _meta_locked(st, time.time())


_ESCALATED: dict[str, str] = {}      # session -> the gate we've already escalated
_AUTO_ANSWERED: dict[str, str] = {}  # session -> the gate we already auto-answered once
_GATE_CLEAR: dict[str, int] = {}     # session -> consecutive-clear count (rearm gate on 2 reads)

_GATE_POLICY_PATH = Path(os.environ.get("EMUX_GATE_POLICY")
                         or Path.home() / ".config" / "emux" / "gatepolicy.json")
_GATE_LOG_PATH = Path.home() / ".local" / "share" / "emux" / "gates.jsonl"

# Never auto-answer a gate guarding something destructive, whatever the rules
# say. Mirrors judge._DESTRUCTIVE_RE — a policy typo must not confirm an rm -rf.
_GATE_NEVER_RE = re.compile(
    r"rm\s+-rf|DROP\s+(?:TABLE|DATABASE)|force[- ]push|--force"
    r"|This will (?:delete|remove|overwrite|destroy)|permanently delete", re.I)


def _gate_policy_rules() -> list[dict[str, Any]]:
    """Operator-authored auto-answer rules. `{"rules": [{"pattern": <regex over
    the pane's live bottom>, "keys": ["Enter"], "note": "..."}]}`. Missing or
    malformed file = no rules; gates escalate to a human as before."""
    try:
        raw = json.loads(_GATE_POLICY_PATH.read_text())
        return [r for r in raw.get("rules", [])
                if isinstance(r, dict) and r.get("pattern") and r.get("keys")]
    except (OSError, ValueError):
        return []


def _log_gate_event(session: str, agent_key: str, gate: str, action: str,
                    keys: list[str] | None = None) -> None:
    """Append one line to the gate ledger — every gate sighting and how it was
    handled. This is the labeled dataset `emux gates` mines for policy rules."""
    try:
        _GATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {"t": int(time.time()), "session": session, "agent": agent_key,
               "gate": gate, "action": action}
        if keys:
            rec["keys"] = keys
        with open(_GATE_LOG_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:  # noqa: BLE001, S110 — the ledger must never break the poll
        pass


def _gate_tail(content: str, lines: int = 10) -> str:
    """The live bottom of the pane — where an actual modal gate lives. Gate
    text higher up is scrollback (an already-answered dialog, echoed menu
    text) and must not count. Same 10-nonblank-line window the needs-you
    flag uses — detection and escalation must agree on what 'gated' means."""
    nonblank = [ln for ln in (content or "").splitlines() if ln.strip()]
    return "\n".join(nonblank[-lines:])


def _try_gate_policy(session: str, agent_key: str, gate: str, content: str,
                     host: str | None) -> bool:
    """Answer a gate from the operator's policy rules. Returns True iff the
    gate was auto-answered (keystrokes sent). One attempt per gate — if the
    same gate is still up next poll, it escalates to a human instead."""
    tail = _gate_tail(content)
    if _GATE_NEVER_RE.search(tail):
        return False   # destructive guard — never auto-answer, whatever the rules
    for rule in _gate_policy_rules():
        try:
            if not re.search(rule["pattern"], tail, re.I):
                continue
        except re.error:
            continue
        keys = [str(k) for k in rule["keys"]][:4]
        try:
            _server._run_tmux(["send-keys", "-t", session, *keys], host=host)
        except Exception:  # noqa: BLE001
            return False
        _AUTO_ANSWERED[session] = gate
        _log_gate_event(session, agent_key, gate, "auto", keys)
        return True
    return False


def _escalate_if_gated(session: str, agent_key: str, content: str,
                       host: str | None = None) -> None:
    """A blocked worker must never be SILENT.

    An agent sitting on an approval gate is the fleet's worst failure mode: it
    looks alive, it changes nothing, and it waits forever for a human who does
    not know it is waiting. A human manager who is stuck for three hours and
    says nothing has failed at the job, whatever the reason.

    So the moment a session is gated, it escalates ITSELF up the same `NEED`
    channel a worker uses to ask for help — carrying the exact decision needed,
    not a vague "stuck". A parent blocked in `tmux_wait` wakes immediately; the
    control room shows it. Escalated once per gate (not per poll), and rearmed
    when the gate clears.
    """
    from . import adapters
    # Judge the LIVE BOTTOM only: after an answer (auto or human) the dialog's
    # text scrolls up but can linger in the capture — full-content matching
    # re-escalated an already-answered gate (found by the fraude live test).
    gate = adapters.gated(agent_key, _gate_tail(content))
    if not gate:
        # gate reads clear; increment the consecutive-clear counter
        n = _GATE_CLEAR.get(session, 0) + 1
        if n >= 2:
            # gate has read clear on 2 consecutive calls — rearm it
            _ESCALATED.pop(session, None)
            _AUTO_ANSWERED.pop(session, None)
            _GATE_CLEAR.pop(session, None)
        else:
            _GATE_CLEAR[session] = n
        return
    # a gate is up — any detection resets the consecutive-clear counter, so
    # rearm needs 2 clears IN A ROW, not 2 clears scattered around flaps
    _GATE_CLEAR.pop(session, None)
    if _ESCALATED.get(session) == gate:
        return                              # already escalated THIS gate
    # (0) the policy engine: a rule the operator wrote answers this gate with
    # deterministic keystrokes — no model, no human, no token. ONE attempt per
    # gate: if the same gate is still up next poll, fall through and escalate.
    if _AUTO_ANSWERED.get(session) != gate and \
            _try_gate_policy(session, agent_key, gate, content, host):
        return
    _ESCALATED[session] = gate
    _log_gate_event(session, agent_key, gate, "escalated")
    # (1) the up-channel: a parent blocked in tmux_wait wakes immediately.
    try:
        _server.inject_signal(
            session, "NEED",
            f"blocked on a {agent_key} gate: {gate!r} — needs a human decision")
    except Exception:  # noqa: BLE001, S110  — escalation must never break the poll
        pass
    # (2) open the worker's terminal via hancock. Filed at LOW risk — opening a
    # head is read-only for the worker — so hancock's `emux head` allow rule
    # auto-runs it and the terminal just appears; no signature to chase.
    _file_hancock_escalation(session, agent_key, gate)


def _hancock_db() -> Path:
    return Path(os.environ.get("HANCOCK_DB")
                or Path.home() / ".local" / "state" / "hancock" / "hancock.db")


def _hancock_pending() -> list[dict[str, Any]]:
    """Read Hancock's pending signing tray straight from its SQLite db (read-only,
    no CLI guard). Coalesces rows by command so identical requests are grouped.
    This is what the human must approve/deny."""
    import sqlite3
    db = _hancock_db()
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, command, cwd, reason, risk, created_at, meta, expires_at "
            "FROM request WHERE status='pending' AND deleted_at IS NULL "
            "ORDER BY created_at DESC LIMIT 50").fetchall()
        con.close()
    except sqlite3.Error:
        return []

    # Group rows by command (preserve newest-first order within each group)
    groups_by_cmd: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        cmd = r["command"]
        if cmd not in groups_by_cmd:
            groups_by_cmd[cmd] = []
        # parse meta JSON safely
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(r["meta"] or "{}")
        except json.JSONDecodeError:
            pass
        groups_by_cmd[cmd].append({
            "id": r["id"],
            "command": cmd,
            "cwd": r["cwd"],
            "why": r["reason"] or "",
            "risk": r["risk"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "meta": meta,
        })

    # Build result: one dict per command group, preserving newest-first order
    result = []
    seen_cmds = set()
    for r in rows:
        cmd = r["command"]
        if cmd in seen_cmds:
            continue
        seen_cmds.add(cmd)
        group = groups_by_cmd[cmd]
        # newest row in group is group[0] (DESC order); oldest is group[-1]
        newest = group[0]
        oldest = group[-1]
        result.append({
            "id": newest["id"],  # representative, for back-compat
            "ids": [g["id"] for g in group],
            "count": len(group),
            "command": cmd,
            "why": newest["why"],
            "risk": newest["risk"],
            "cwd": newest["cwd"],
            "source": newest["meta"].get("source", ""),
            "target": newest["meta"].get("target", ""),
            "requester": newest["meta"].get("requester", ""),
            "created_at": newest["created_at"],  # keep this key for existing UI
            "first_created": oldest["created_at"],
            "last_created": newest["created_at"],
        })

    return result


def _hancock_approve(req_id: str) -> dict[str, Any]:
    """Sign + RUN a request via the real hancock path (`hancock approve <id>`),
    with a scrubbed env so hancock's Claude-Code guard doesn't reject the daemon."""
    import shutil
    import subprocess
    hancock = shutil.which("hancock")
    if not hancock:
        return {"ok": False, "error": "hancock_not_found"}
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY")}
    try:
        p = subprocess.run([hancock, "approve", req_id], env=env,
                           capture_output=True, text=True, timeout=120, check=False)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": p.returncode == 0, "output": (p.stdout or p.stderr or "").strip()[:600]}


def _hancock_deny(req_id: str, reason: str = "denied from emux") -> dict[str, Any]:
    """Deny a request. Hancock has no CLI deny, so record the decision + mark the
    request denied directly (so the agent blocked in `wait` sees the verdict)."""
    import sqlite3
    import uuid
    db = _hancock_db()
    if not db.is_file():
        return {"ok": False, "error": "no_db"}
    try:
        con = sqlite3.connect(str(db), timeout=5)
        con.execute(
            "INSERT INTO decision (id, request_id, verdict, reason) VALUES (?,?,?,?)",
            (uuid.uuid4().hex[:16], req_id, "deny", reason))
        con.execute(
            "UPDATE request SET status='denied', updated_at=datetime('now') "
            "WHERE id=? AND status='pending'", (req_id,))
        con.commit()
        con.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _file_hancock_escalation(session: str, agent_key: str, gate: str) -> None:
    """Open a gated worker's terminal through Hancock. Filed at LOW risk so the
    `emux head` allow rule in hancock's license auto-approves and RUNS it — the
    head just opens. (It used to file at high risk, which always waits for a
    signature; the tray filled with head-openers nobody wanted to sign.) If the
    allow rule is missing, the request falls back to waiting in the tray.

    Best-effort and isolated: if hancock isn't installed or this fails, the NEED
    signal already fired — escalation degrades, it never breaks the poll."""
    import shutil
    import sqlite3
    import subprocess

    # Check for an existing pending request to avoid duplicate escalations
    db = _hancock_db()
    if db.is_file():
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
            row = con.execute(
                "SELECT 1 FROM request WHERE command=? AND status='pending' AND deleted_at IS NULL LIMIT 1",
                (f"emux head {session}",)).fetchone()
            con.close()
            if row:
                return  # identical pending request already queued
        except sqlite3.Error:
            pass  # on any error, fall through and file as before

    hancock = shutil.which("hancock")
    if not hancock:
        return
    # a clean env: the daemon may have inherited CLAUDECODE from its spawner,
    # which trips hancock's "don't drive the queue from Claude Code" guard.
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY")}
    try:
        subprocess.run(
            [hancock, "add", f"emux head {session}",
             "-why", f"{session} blocked on a {agent_key} gate: {gate} — "
                     "opening its terminal so a human can resolve",
             "-risk", "low", "--source", f"emux:{session}"],
            env=env, capture_output=True, text=True, timeout=15, check=False)
    except Exception:  # noqa: BLE001, S110
        pass


def _retract_stale_escalations() -> None:
    """Auto-retract emux self-escalations for sessions no longer gated.
    A session is stale and should be retracted if:
    1. It is NOT currently in _ESCALATED (no active gate).
    2. It HAS been observed this process (in _ACTIVITY or _CACHE).
    """
    import sqlite3
    db = _hancock_db()
    if not db.is_file():
        return
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        rows = con.execute(
            "SELECT id, command, meta FROM request WHERE status='pending' "
            "AND deleted_at IS NULL AND command LIKE 'emux head %'").fetchall()
        con.close()
    except sqlite3.Error:
        return

    # Identify which sessions have been observed
    with _LOCK:
        observed = set(_ACTIVITY.keys()) | set(_CACHE.keys())

    for row in rows:
        command = row[1]  # "emux head <sess>"
        meta_json = row[2]
        # Extract session from command
        parts = command.split()
        if len(parts) < 3 or parts[0] != "emux" or parts[1] != "head":
            continue
        sess = parts[2]
        # Parse meta for source
        try:
            meta = json.loads(meta_json or "{}")
        except json.JSONDecodeError:
            meta = {}
        source = meta.get("source", "")
        # Check: source matches "emux:<sess>" format
        if source != f"emux:{sess}":
            continue
        # Stale iff: NOT gated AND has been observed
        if sess not in _ESCALATED and sess in observed:
            _hancock_deny(row[0], reason="auto-retracted: session no longer gated")


def _capture_and_observe(session: str, lines: int, host: str | None = None) -> str:
    """Capture a pane (local or remote), fold it into activity state, detect the
    running agent, and refresh the cache."""
    cap = capture_payload(session, lines, host=host)
    content = cap.get("content", "") if cap.get("ok") else ""
    meta = _observe(session, content)
    agent = _detect_agent(session, content)
    agent_key = agent.get("agent", "")
    _escalate_if_gated(session, agent_key, content, host=host)
    # is this session WAITING ON THE HUMAN? A real gate on screen — an approval
    # menu or a y/n it can't answer itself. NOT merely "there's a ❯ prompt line"
    # (every Claude pane has those in scrollback); that over-fires on everything.
    # The UI marches ants around a genuinely-blocked session so you can't miss it.
    from . import adapters
    # a real gate/menu lives at the LIVE BOTTOM — the last NON-EMPTY lines (a
    # Claude dialog leaves blank space below it), so echoed menu text up in
    # scrollback isn't flagged, but a real modal dialog still is.
    tail = "\n".join([ln for ln in content.splitlines() if ln.strip()][-10:])
    needs_human = bool(adapters.gated(agent_key, tail))
    cost = _cost_overrun(content)
    state = _quick_state(agent_key, content, needs_human)
    summary = _summarize(agent.get("label", ""), state, content)
    # "clearly stopped" = settled AND the pane has sat STILL for a real pause (not
    # the brief flicker between turns). Warm the gist once per genuine stop, deduped
    # by content-norm so a still session doesn't re-warm every poll.
    age = meta.get("last_change_age")
    norm = (_ACTIVITY.get(session) or {}).get("norm")
    with _LOCK:
        prev = _CACHE.get(session, {})
        warm = _should_warm_gist(state, age, norm, prev.get("gist_warm_norm"),
                                 session in _GIST_INFLIGHT)
        _CACHE[session] = {"content": content, "ts": time.time(), "lines": lines,
                           "agent": agent, "needs_human": needs_human, "cost": cost,
                           "state": state, "summary": summary,
                           "gist_warm_norm": norm if warm else prev.get("gist_warm_norm")}
    if warm:
        threading.Thread(target=_warm_gist, args=(session, host), daemon=True).start()
    return content


_SUM_VERB = {"running": "working", "asking": "asks you", "error": "hit an error",
             "idle": "idle", "waiting_human": "needs you", "dead": "gone"}


# UI notices / status meters / tips that are NOT what the agent is doing
_NOISE_RE = re.compile(
    r"update available|brew upgrade|new task\?|/clear to save|/mcp\b|^tip:|"
    r"enter to select|to navigate|esc to (?:cancel|close)|for shortcuts|"
    r"bypass permissions|auto mode on|mcp server|need(?:s)? auth|paste images|"
    r"·\s*[↓↑].*token|\(\d+[ms].*token|\bfor \d+[ms]\b|ctrl\+b|shift\+tab|"   # spinner meters / tmux help
    r"^\s*\d+\.\s|^\s*[▘▝▗▖▀▄█░▚▞◜◝◞◟]|…\s*\+\d+ (?:completed|tool)|"          # menu items / box spinners
    r"^\S+…\s*(?:\(.*\))?$|@[\w.-]+\s.*[%$#]\s*$", re.I)                       # single-word spinner / shell prompt
_ACTION_RE = re.compile(r"^\s*⏺\s*(\S.*)")   # Claude marks its own actions with ⏺


def _headline(content: str) -> str:
    """The cheapest possible 'what is it doing' — read the bottom of the screen,
    no model, no GPU. Prefer the agent's last ACTION line (Claude marks these
    with ⏺); else the last line that isn't chrome/notice/spinner."""
    lines = content.splitlines()
    for ln in reversed(lines):        # 1st choice: the agent's last ⏺ action
        m = _ACTION_RE.match(ln)
        if m and not _NOISE_RE.search(ln):
            s = re.sub(r"\s+", " ", m.group(1).strip())
            if len(s) >= 6:
                return s
    for ln in reversed(lines):        # else: last real, non-noise line
        if not ln.strip() or _CHROME_RE.search(ln) or _NOISE_RE.search(ln):
            continue
        s = re.sub(r"\s+", " ", ln.strip().lstrip("⏺✻✢◇◆•·⎿│>❯▶▸ ").strip())
        if len(s) >= 6:
            return s
    return ""


_OPT_RE = re.compile(r"^\s*(❯|>|›|▶)?\s*(\d{1,2})[.)]\s+(\S.*?)\s*$")


def _parse_options(content: str) -> list[dict[str, Any]]:
    """Pull a numbered selection menu out of the live pane, so the UI can offer
    it as CLICKABLE BUBBLES. Only fires when a menu is genuinely on screen — a
    navigate/select hint near the bottom, or a ❯-cursored numbered list — not any
    stray numbered list in the agent's prose."""
    # NON-EMPTY lines only — a Claude dialog renders with blank space below it,
    # so the last raw lines are empty and the menu sits higher up.
    lines = [ln for ln in content.splitlines() if ln.strip()]
    tail = "\n".join(lines[-12:]).lower()
    has_menu_hint = ("to navigate" in tail or "enter to select" in tail
                     or "esc to cancel" in tail or "enter to confirm" in tail)
    opts, cursor = [], False
    for ln in lines[-14:]:
        m = _OPT_RE.match(ln)
        if m:
            sel = bool(m.group(1))
            cursor = cursor or sel
            opts.append({"n": int(m.group(2)), "label": m.group(3).strip()[:70],
                         "selected": sel})
    # need ≥2 options AND (a menu hint OR a ❯ cursor) to be sure it's a real menu
    if len(opts) >= 2 and (has_menu_hint or cursor):
        # de-dupe by number, keep order
        seen, out = set(), []
        for o in opts:
            if o["n"] not in seen:
                seen.add(o["n"])
                out.append(o)
        return out
    return []


def _summarize(agent_label: str, state: str, content: str) -> str:
    """A super-cheap, LOCAL, always-on one-liner of what a session is doing right
    now — the 'thin rail' text. Deterministic (state verb + the last real output
    line); this is what a model WOULD say, without a model."""
    head = _headline(content)
    verb = _SUM_VERB.get(state, state)
    if head:
        return f"{verb} — {head}"[:200]
    return verb


_QUESTION_PHRASES = re.compile(
    r"\b(do you want|would you like|should i|shall i|which (?:one|option|of)|"
    r"let me know|your call|say the word|approve|amend|confirm|"
    r"waiting (?:on|for) (?:you|your)|awaiting your|need(?:s)? (?:you|your)|"
    r"want me to|proceed\?|go ahead\?|"
    # requests that aren't literal questions but still need the human:
    r"decision needed|needs? (?:a |your )?decision|your (?:input|approval|sign-?off|go-?ahead)|"
    r"blocking the fleet|which do you (?:want|prefer)|pick (?:one|an option)|"
    r"enter to select|↑/↓ to navigate)\b", re.I)
# tmux chrome / composer lines to ignore when looking for the agent's own words
_CHROME_RE = re.compile(r"^\s*(❯|>|⏵|›|»|─+|\[PONYTAIL\]|▶▶|▸▸)|esc to interrupt|shift\+tab",
                        re.I)


def _looks_like_question(content: str) -> bool:
    """Is an IDLE agent asking the human something? Look at its last few real
    output lines (dropping the composer/chrome) for a trailing '?' or a question
    phrase — this catches "say the word on the proposal", "should I…?", etc.,
    which are NOT formal gates but still need you."""
    body = [ln.rstrip() for ln in content.splitlines()
            if ln.strip() and not _CHROME_RE.search(ln)]
    if not body:
        return False
    tail = " ".join(body[-6:]).strip()
    return tail.endswith("?") or bool(_QUESTION_PHRASES.search(tail))


# Cost / usage overrun — an agent that hit a usage limit, rate limit, or quota is
# burning your budget or stuck; emux catches it from the pane the same way it catches
# a gate. Deliberately specific phrases (matched at the live bottom) so ordinary
# mentions of "limit" in output don't false-fire.
_COST_RE = re.compile(
    r"usage limit|rate[ _-]?limit|rate.?limited|"
    r"quota (?:exceeded|reached|remaining|met)|"
    r"out of (?:credits|tokens|quota)|insufficient (?:credits|quota|balance|funds)|"
    r"429(?:\b|[^0-9])|too many requests|"
    r"reached your (?:usage |plan )?limit|limit (?:will )?reset|resets? (?:at|in)|"
    r"upgrade (?:your plan )?to (?:increase|continue)|overage|billing", re.I)


def _cost_overrun(content: str) -> bool:
    """True if the session's LIVE BOTTOM shows a usage/rate/quota/cost limit —
    the agent is throttled or over budget and needs you to decide."""
    tail = "\n".join([ln for ln in content.splitlines() if ln.strip()][-12:])
    return bool(_COST_RE.search(tail))


def _quick_state(agent_key: str, content: str, needs_human: bool) -> str:
    """A cheap, per-poll status for at-a-glance fleet tracking — running /
    waiting_human / asking / error / idle. Shown on every tile and flow box so a
    manager (or human) sees each agent's status AND its subs' without opening any.
    The full judge (`/api/classify`) is the precise on-demand read."""
    from . import adapters
    if needs_human:
        return "waiting_human"
    low = content.lower()
    if "traceback (most recent call last)" in low or "\nerror:" in low or " error:" in low:
        return "error"
    if any(s in low for s in adapters.busy_sigs_for(agent_key)):
        return "running"
    # idle — but is it asking YOU something? then it needs you, not resting.
    if _looks_like_question(content):
        return "asking"
    return "idle"


def _capture_many(items: list[tuple[str, str | None]], lines: int) -> None:
    """Capture (session, host) pairs CONCURRENTLY. A remote capture is an ssh
    round-trip (~1-2s); serial was O(n) latency — 7 remotes = 14s. In parallel
    it collapses to roughly one hop. I/O-bound, so threads are the right tool."""
    from concurrent.futures import ThreadPoolExecutor
    if not items:
        return
    with ThreadPoolExecutor(max_workers=min(10, len(items))) as ex:
        list(ex.map(lambda it: _capture_and_observe(it[0], lines, host=it[1]), items))


def poll_once(lines: int = 14) -> None:
    """One capture sweep over all live sessions (local AND remote), in parallel;
    evicts state for dead ones. Called on a timer by the background loop."""
    if _server._resolve_tmux() is None:
        return
    live = _server._live_sessions()
    live_names = {s["name"] for s in live}
    items: list[tuple[str, str | None]] = [(s["name"], None) for s in live]
    # registered REMOTE sessions never appear in the local list — poll them too,
    # so a rentamac worker is cached and the grid serves it fast (not re-ssh'd).
    rhosts = {h for e in _server._load_registry().values() if (h := e.get("host"))}
    for host in rhosts:
        for name in _remote_live_names(host):
            items.append((name, host))
            live_names.add(name)
    _capture_many(items, lines)
    with _LOCK:
        for dead in [k for k in _ACTIVITY if k not in live_names]:
            _ACTIVITY.pop(dead, None)
            _CACHE.pop(dead, None)
            _GIST_CACHE.pop(dead, None)      # cache-bust: a dead session's gist is void
            _GIST_INFLIGHT.discard(dead)
    # auto-retract stale escalations (never let this break the poll)
    try:
        _retract_stale_escalations()
    except Exception:  # noqa: BLE001, S110
        pass


def _probe_managed_planes(cfg: Any) -> dict[str, Any]:
    """Manager glance: probe **only** product.json allowlist (fail closed if empty).

    Prefer ``healthz_loopback`` when set (same-host operational truth). Public
    OIDC/Authentik redirects (3xx / non-JSON) are **auth_gated** degraded — still
    ok=True so workers_ok is not false for a gate, but degraded for honesty.
    """
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    planes = list(getattr(cfg, "managed_planes", ()) or [])
    if not planes:
        return {
            "role": "manager",
            "product": getattr(cfg, "product", None),
            "chats_match": getattr(cfg, "chats_match", None),
            "planes": [],
            "workers_ok": False,
            "probed": 0,
            "error": "no_managed_planes",
            "hint": "install ~/.config/<product>/product.json with managed_planes allowlist",
            "source": getattr(cfg, "source", None),
            "path": getattr(cfg, "path", None),
        }

    def _hit(url: str, *, timeout: float) -> dict[str, Any]:
        """Probe one URL. Returns partial out fields: ok/degraded/version/…/error/probe_url."""
        out: dict[str, Any] = {
            "ok": False,
            "degraded": True,
            "version": None,
            "live_sessions": None,
            "http_status": None,
            "error": None,
            "probe_url": url,
            "reason": None,
        }
        try:
            # No redirect-follow: Authentik/login HTML is auth_gated, not a hang.
            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
                    return None

            opener = urllib.request.build_opener(_NoRedirect)
            req = urllib.request.Request(
                url, headers={"User-Agent": f"emux-manager/{__version__}"}
            )
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read(65536).decode("utf-8", errors="replace")
                code = getattr(resp, "status", None) or resp.getcode()
                out["http_status"] = code
                try:
                    parsed = json.loads(body)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("ok"):
                    out["ok"] = True
                    out["degraded"] = False
                    out["version"] = parsed.get("version")
                    out["live_sessions"] = parsed.get("live_sessions")
                    out["reason"] = "healthy"
                elif code and 300 <= int(code) < 400:
                    out["ok"] = True
                    out["degraded"] = True
                    out["error"] = f"auth_gated ({code})"
                    out["reason"] = "auth_gated"
                elif code and int(code) < 500:
                    out["ok"] = True
                    out["degraded"] = True
                    out["error"] = f"non_json_health ({code})"
                    out["reason"] = "non_json"
                else:
                    out["error"] = f"http_{code}"
                    out["reason"] = "http_error"
        except urllib.error.HTTPError as exc:
            code = int(getattr(exc, "code", 0) or 0)
            out["http_status"] = code or None
            if 300 <= code < 400:
                out["ok"] = True
                out["degraded"] = True
                out["error"] = f"auth_gated ({code})"
                out["reason"] = "auth_gated"
            elif code and code < 500:
                out["ok"] = True
                out["degraded"] = True
                out["error"] = f"non_json_health ({code})"
                out["reason"] = "non_json"
            else:
                out["error"] = f"http_{code or 'err'}"
                out["reason"] = "http_error"
        except Exception as exc:  # noqa: BLE001
            out["error"] = str(exc)[:200]
            out["reason"] = "unreachable"
        return out

    def one(plane: Any) -> dict[str, Any]:
        row = plane.as_dict() if hasattr(plane, "as_dict") else dict(plane)
        loop = (row.get("healthz_loopback") or "").strip()
        public = (row.get("healthz") or "").strip()
        base = {
            **row,
            "ok": False,
            "degraded": True,
            "version": None,
            "live_sessions": None,
            "http_status": None,
            "error": None,
            "probe_url": None,
            "reason": None,
        }
        # Prefer loopback (operational truth) then public (may be OIDC-gated).
        candidates: list[tuple[str, float, str]] = []
        if loop:
            candidates.append((loop, 1.0, "loopback"))
        if public and public != loop:
            candidates.append((public, 1.5, "public"))
        if not candidates:
            base["error"] = "no_healthz_url"
            base["reason"] = "no_url"
            return base
        last = base
        for url, timeout, kind in candidates:
            hit = _hit(url, timeout=timeout)
            last = {**base, **hit, "probe_kind": kind}
            # Clean JSON health wins immediately.
            if hit.get("ok") and not hit.get("degraded"):
                return last
            # Loopback unreachable → try public; public auth_gated is still a result.
            if kind == "loopback" and hit.get("reason") in ("unreachable", "http_error", "probe_timeout"):
                continue
            # auth_gated / non_json on public is honest degraded — stop.
            if hit.get("ok"):
                return last
        return last

    results: list[dict[str, Any]] = []
    # Hard wall: never block the accept path more than ~3s total (EID-1115).
    wall = 3.0
    with ThreadPoolExecutor(max_workers=min(6, len(planes))) as pool:
        futs = {pool.submit(one, p): p for p in planes}
        try:
            for fut in as_completed(futs, timeout=wall):
                try:
                    results.append(fut.result(timeout=0.05))
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        {"id": "?", "ok": False, "degraded": True, "error": str(exc)[:200],
                         "reason": "probe_error"}
                    )
        except TimeoutError:
            pass
        done_ids = {r.get("id") for r in results}
        for p in planes:
            pid = getattr(p, "id", None) or (p.get("id") if isinstance(p, dict) else None)
            if pid not in done_ids:
                if hasattr(p, "as_dict"):
                    row = p.as_dict()
                elif isinstance(p, dict):
                    row = dict(p)
                else:
                    row = {"id": pid}
                results.append(
                    {**row, "ok": False, "degraded": True, "error": "probe_timeout",
                     "reason": "probe_timeout"}
                )
    order = {
        (getattr(p, "id", None) or (p.get("id") if isinstance(p, dict) else None)): i
        for i, p in enumerate(planes)
    }
    results.sort(key=lambda r: order.get(r.get("id"), 99))
    # workers_ok: reachable (ok) including auth_gated; only hard-down fails the fleet.
    workers_ok = all(r.get("ok") for r in results)
    auth_gated = sum(1 for r in results if r.get("reason") == "auth_gated")
    healthy = sum(1 for r in results if r.get("ok") and not r.get("degraded"))
    mids = getattr(cfg, "managed_ids", None)
    if callable(mids):
        mid_list = sorted(mids())
    else:
        mid_list = sorted(mids or [])
    return {
        "role": "manager",
        "product": getattr(cfg, "product", None),
        "chats_match": getattr(cfg, "chats_match", None),
        "planes": results,
        "workers_ok": workers_ok,
        "probed": len(results),
        "healthy": healthy,
        "auth_gated": auth_gated,
        "managed_ids": mid_list,
        "source": getattr(cfg, "source", None),
        "path": getattr(cfg, "path", None),
        "notes": (
            "ok=True+degraded+reason=auth_gated means public healthz is OIDC/login "
            "(reachable, not operational JSON). Prefer healthz_loopback for same-host truth."
        ),
    }


def chats_payload(q: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Claude Code + Grok chats — durable index first, disk scan only when stale.

    Query params (parse_qs style): match, status (comma-separated), tools,
    limit, recent_hours, q (text search), sort (priority|mtime),
    refresh=1 to force a full disk re-index into ~/.config/*/chats.db.
    Default match: greenmark when skin is gmux; personal when skin is reevux;
    aic when skin is amux; directrux-only when skin is directrux (meta — NOT all);
    all when bare emux.
    """
    q = q or {}
    try:
        from . import chats_store
        from . import skin as _skin
    except ImportError as exc:
        return {"ok": False, "error": f"chats_unavailable: {exc}"}

    sk = _skin.active_skin()
    match_raw = (q.get("match") or [""])[0].strip()
    if not match_raw:
        # Product config (worker vs manager) owns the default — managers never "all".
        try:
            from . import product_config as _pc

            match_raw = _pc.default_chats_match_for_skin(sk.id)
        except Exception:
            if sk.id == "gmux":
                match_raw = "greenmark"
            elif sk.id == "reevux":
                match_raw = "personal"
            elif sk.id == "amux":
                match_raw = "aic"
            elif sk.id == "directrux":
                match_raw = "directrux"
            else:
                match_raw = "all"
    status_raw = (q.get("status") or [""])[0].strip()
    statuses = [s.strip() for s in status_raw.split(",") if s.strip()] or None
    tools_raw = (q.get("tools") or ["claude,grok"])[0]
    tools = [t.strip() for t in tools_raw.split(",") if t.strip()] or ["claude", "grok"]
    q_text = (q.get("q") or [""])[0].strip()
    sort = (q.get("sort") or ["priority"])[0].strip() or "priority"
    if sort not in ("priority", "mtime", "age"):
        sort = "priority"
    refresh = (q.get("refresh") or ["0"])[0].strip().lower() in ("1", "true", "yes")
    try:
        limit = max(1, min(200, int((q.get("limit") or ["50"])[0])))
    except ValueError:
        limit = 50
    try:
        recent_hours = float((q.get("recent_hours") or ["24"])[0])
    except ValueError:
        recent_hours = 24.0

    try:
        bundle = chats_store.list_or_sync(
            refresh=refresh,
            tools=tools,
            match=match_raw,
            statuses=statuses,
            limit=limit,
            q=q_text or None,
            sort=sort,
            recent_hours=recent_hours,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    chats = bundle["hits"]
    # hits from store are already dicts; from legacy scan would be ChatHit
    out_chats = []
    for h in chats:
        if hasattr(h, "as_dict"):
            out_chats.append(h.as_dict())
        else:
            out_chats.append(h)

    return {
        "ok": True,
        "chats": out_chats,
        "count": bundle["returned"],
        "matched": bundle["matched"],
        "counts": bundle["counts"],
        "tools_counts": bundle["tools_counts"],
        "scan_ms": bundle["scan_ms"],
        "match": match_raw,
        "q": q_text,
        "sort": sort,
        "host": "local",
        "source": bundle.get("source") or "store",
        "did_sync": bool(bundle.get("did_sync")),
        "store_path": bundle.get("store_path"),
        "store_rows": bundle.get("store_rows"),
        "last_full_scan": bundle.get("last_full_scan"),
        "query_ms": bundle.get("query_ms"),
        "index_ms": bundle.get("index_ms"),
        "sync": bundle.get("sync"),
        "note": (
            "Durable chats.db index — disk is scanned only when the store is "
            "stale or ?refresh=1. POST /api/chats/resume spawns a fleet session."
        ),
    }


def chats_peek_payload(q: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Last user/assistant turns for one transcript (tile expand)."""
    q = q or {}
    try:
        from . import chats as chat_find
    except ImportError as exc:
        return {"ok": False, "error": f"chats_unavailable: {exc}"}
    tool = (q.get("tool") or [""])[0].strip()
    sid = (q.get("session_id") or q.get("id") or [""])[0].strip()
    try:
        max_turns = max(1, min(20, int((q.get("turns") or ["8"])[0])))
    except ValueError:
        max_turns = 8
    try:
        return chat_find.peek_chat(tool, sid, max_turns=max_turns)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def grid_payload(lines: int = 14) -> dict[str, Any]:
    """Session list with a mini pane capture + activity meta per live session.
    Serves from the daemon cache when fresh; captures on miss (cold start, or
    when the poll loop isn't running, e.g. under tests)."""
    base = sessions_payload()
    if not base["ok"]:
        return base
    now = time.time()
    # EID-881: prefer the durable ledger for sessions it has receipts for (driven
    # work) — the ledger knows a task is running even when the pane looks static.
    # Read-only: only open it if a writer (the drive path) has created it.
    # Soft import: greenmux may ship an emux wheel that predates mgmt_ledger;
    # grid must still load (FORBIDDEN_HOST-looking empty status was a 500 crash).
    try:
        from . import mgmt_ledger as _mgmt_ledger
    except ImportError:
        _mgmt_ledger = None  # type: ignore[assignment]
    _lpath = os.path.join(os.path.expanduser("~"), ".config", "emux", "ledger.db")
    _green_lpath = os.path.join(os.path.expanduser("~"), ".config", "greenmux", "ledger.db")
    _led = None
    if _mgmt_ledger is not None:
        for _cand in (_lpath, _green_lpath):
            if os.path.exists(_cand):
                _led = _mgmt_ledger.Ledger(_cand)
                break
    # capture every stale/missing live pane IN PARALLEL (remotes are ssh hops).
    misses = []
    for item in base["sessions"]:
        if not item["live"]:
            continue
        with _LOCK:
            ce = _CACHE.get(item["session"])
        if ce is None or (now - ce["ts"]) >= _CACHE_TTL:
            misses.append((item["session"], item.get("host")))
    _capture_many(misses, lines)
    for item in base["sessions"]:
        if item["live"]:
            with _LOCK:
                ce = _CACHE.get(item["session"])
            content = (ce or {}).get("content", "")
            item["content"] = content
            item["agent"] = (ce or {}).get("agent") or {"agent": "unknown", "label": "—", "glyph": "·"}
            item["needs_human"] = bool((ce or {}).get("needs_human"))
            item["cost"] = bool((ce or {}).get("cost"))
            item["state"] = (ce or {}).get("state") or "idle"
            item["summary"] = (ce or {}).get("summary") or ""
            item["state_source"] = "classifier"
            if _led is not None and _mgmt_ledger is not None:
                try:
                    _ls = _mgmt_ledger.ui_state(_led.state(item["name"]))
                    if _ls is not None:               # ledger only speaks for driven work
                        item["state"] = _ls
                        item["state_source"] = "ledger"
                except Exception:
                    pass                              # ledger read is best-effort
            item.update(_meta(item["session"]))
        else:
            item["content"] = ""
            item["needs_human"] = False
            item["cost"] = False
            item["state"] = "dead"
            item["state_source"] = "classifier"
            item["summary"] = ""
            item["changed"] = False
            item["last_change_age"] = None
            item["activity"] = []
            item["agent"] = {"agent": "gone", "label": "", "glyph": ""}
    if _led is not None:
        _led.close()
    return base


# ---------------------------------------------------------------------------
# send
# ---------------------------------------------------------------------------

# Web-composer image paste: browser has the laptop pasteboard; we write the file
# on the daemon host (where Claude/Grok run) and inject the path into the pane.
_CLIP_DIR = Path(os.environ.get("EMUX_CLIP_DIR") or "/tmp/gmux-clip")
_CLIP_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB decoded
_CLIP_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _clip_image_save(data: dict[str, Any]) -> dict[str, Any]:
    """Save a base64 image from the room composer onto this host's disk.

    Returns a path the remote agent can Read. Only runs when the operator pastes
    into the web UI — not a system-wide clipboard hijack.
    """
    import base64
    import re as _re

    raw_b64 = (data.get("data") or data.get("image") or "").strip()
    if not raw_b64:
        return {"ok": False, "error": "missing_image_data"}
    mime = (data.get("mime") or data.get("type") or "image/png").strip().lower()
    # data:image/png;base64,....
    if raw_b64.startswith("data:"):
        try:
            header, raw_b64 = raw_b64.split(",", 1)
            if ";" in header:
                mime = header.split(";")[0].split(":", 1)[1].strip().lower() or mime
        except ValueError:
            return {"ok": False, "error": "bad_data_url"}
    ext = _CLIP_MIME.get(mime)
    if not ext:
        return {"ok": False, "error": f"unsupported_mime:{mime}"}
    try:
        blob = base64.b64decode(raw_b64, validate=False)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"bad_base64:{exc}"}
    if not blob:
        return {"ok": False, "error": "empty_image"}
    if len(blob) > _CLIP_MAX_BYTES:
        return {
            "ok": False,
            "error": "image_too_large",
            "max_bytes": _CLIP_MAX_BYTES,
            "got_bytes": len(blob),
        }
    # light magic-byte check
    if ext == ".png" and not blob.startswith(b"\x89PNG"):
        return {"ok": False, "error": "not_a_png"}
    if ext == ".jpg" and not blob.startswith(b"\xff\xd8"):
        return {"ok": False, "error": "not_a_jpeg"}

    session = (data.get("session") or data.get("name") or "session").strip()
    safe = _re.sub(r"[^A-Za-z0-9._-]+", "-", session)[:48] or "session"
    _CLIP_DIR.mkdir(parents=True, exist_ok=True)
    # best-effort dir perms for multi-user hosts
    try:
        os.chmod(_CLIP_DIR, 0o700)
    except OSError:
        pass
    ts = int(time.time() * 1000)
    path = _CLIP_DIR / f"{safe}-{ts}{ext}"
    try:
        path.write_bytes(blob)
        os.chmod(path, 0o600)
    except OSError as exc:
        return {"ok": False, "error": f"write_failed:{exc}"}

    # prune old clips (keep last 40 files)
    try:
        files = sorted(_CLIP_DIR.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[40:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

    return {
        "ok": True,
        "path": str(path),
        "mime": mime,
        "bytes": len(blob),
        "session": session,
        "note": "Paste path into agent prompt or send with your message — Claude Read works on paths.",
    }


def _gsid_from_registry(session: str) -> tuple[str | None, str | None, list[str]]:
    """Return (tool_hint, transcript_session_id, tags) from registry if known."""
    try:
        reg = _server._load_registry()
    except Exception:  # noqa: BLE001
        return None, None, []
    entry = next(
        (e for e in reg.values() if e.get("session") == session or e.get("name") == session),
        None,
    )
    if not isinstance(entry, dict):
        return None, None, []
    tags = [str(t) for t in (entry.get("tags") or [])]
    tool = None
    gsid = None
    for t in tags:
        low = t.lower()
        if low == "grok":
            tool = "grok"
        elif low == "claude":
            tool = "claude"
        if low.startswith("gsid:"):
            gsid = t.split(":", 1)[1].strip()
            tool = tool or "grok"
        elif low.startswith("csid:"):
            gsid = t.split(":", 1)[1].strip()
            tool = tool or "claude"
    return tool, gsid, tags


def steer_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Steer a session: Grok headless/ACP when possible, else tmux send-keys (PTY).

    Body: prompt|keys, session (tmux name), mode=auto|headless|acp|pty,
    session_id (Grok/Claude transcript uuid), cwd, timeout, tool.

    Modes:
    - auto — Grok (tags/gsid) → **ACP then headless then PTY**; Claude → PTY
    - headless — ``grok -p`` one-shot (Phase B)
    - acp — ``grok agent stdio`` ACP prompt (Phase C); no fallback unless auto
    - pty — tmux send-keys into live pane
    """
    prompt = (data.get("prompt") or data.get("keys") or data.get("text") or "").strip()
    session = (data.get("session") or data.get("name") or "").strip()
    if not prompt:
        return {"ok": False, "error": "missing_prompt"}
    if not session:
        return {"ok": False, "error": "missing_session"}

    requested = (data.get("mode") or "auto").strip().lower()
    if requested not in ("auto", "headless", "acp", "pty"):
        requested = "auto"
    mode = requested

    tool_hint, gsid_reg, tags = _gsid_from_registry(session)
    tool = (data.get("tool") or tool_hint or "").strip().lower()
    gsid = (data.get("session_id") or data.get("grok_session_id") or gsid_reg or "").strip()
    cwd = (data.get("cwd") or "").strip() or None
    try:
        timeout = float(data.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120.0
    timeout = max(10.0, min(600.0, timeout))

    is_grok = tool == "grok" or bool(gsid) or "grok" in {str(t).lower() for t in tags}
    if mode == "auto":
        # Protocol-first for Grok: ACP → headless → PTY
        mode = "acp" if is_grok else "pty"

    fallbacks: list[str] = []

    if mode == "acp":
        try:
            from . import acp_grok as acp
            from . import grok_control as gc
        except ImportError as exc:
            if requested == "acp":
                return {"ok": False, "error": f"acp_unavailable:{exc}", "mode": "acp"}
            fallbacks.append(f"acp_unavailable:{exc}")
            mode = "headless"
        else:
            if not gc.resolve_grok_bin() and not data.get("bin_path"):
                if requested == "acp":
                    return {
                        "ok": False,
                        "error": "grok_not_found",
                        "mode": "acp",
                        "hint": "set EMUX_GROK_BIN or install grok",
                    }
                fallbacks.append("acp_grok_not_found")
                mode = "headless"
            else:
                result = acp.run_acp_prompt(
                    prompt,
                    session_id=gsid or None,
                    cwd=cwd,
                    bin_path=(data.get("bin_path") or None),
                    model=(data.get("model") or None),
                    timeout=timeout,
                    always_approve=bool(data.get("always_approve", True)),
                )
                result["tmux_session"] = session
                result["tool"] = "grok"
                if result.get("ok"):
                    if data.get("also_pty"):
                        send_payload(
                            session, prompt, literal=True, enter=True,
                            host=_session_host(session),
                        )
                    if fallbacks:
                        result["fallbacks_skipped"] = fallbacks
                    return result
                # ACP failed — cascade on auto only
                if requested == "acp":
                    result["tmux_session"] = session
                    result["tool"] = "grok"
                    return result
                fallbacks.append(f"acp:{result.get('error') or 'failed'}")
                mode = "headless"

    if mode == "headless":
        try:
            from . import grok_control as gc
        except ImportError as exc:
            if requested == "headless":
                return {"ok": False, "error": f"grok_control_unavailable:{exc}", "mode": "headless"}
            fallbacks.append(f"headless_unavailable:{exc}")
            mode = "pty"
        else:
            if not gc.resolve_grok_bin():
                if requested == "headless":
                    return {
                        "ok": False,
                        "error": "grok_not_found",
                        "mode": "headless",
                        "hint": "set EMUX_GROK_BIN or install grok",
                    }
                fallbacks.append("headless_grok_not_found")
                mode = "pty"
            else:
                result = gc.run_headless_steer(
                    prompt,
                    session_id=gsid or None,
                    continue_recent=bool(data.get("continue_recent")) and not gsid,
                    cwd=cwd,
                    model=(data.get("model") or None),
                    timeout=timeout,
                    always_approve=bool(data.get("always_approve", True)),
                )
                result["tmux_session"] = session
                result["tool"] = "grok"
                if result.get("ok"):
                    if data.get("also_pty"):
                        send_payload(
                            session, prompt, literal=True, enter=True,
                            host=_session_host(session),
                        )
                    if fallbacks:
                        result["via_fallback"] = True
                        result["fallbacks"] = fallbacks
                    return result
                if requested == "headless":
                    return result
                fallbacks.append(f"headless:{result.get('error') or 'failed'}")
                mode = "pty"

    # PTY path (Claude default, or Grok last resort)
    host = _session_host(session)
    r = send_payload(session, prompt, literal=True, enter=True, host=host)
    r["mode"] = "pty"
    r["tool"] = tool or "unknown"
    r["tmux_session"] = session
    if gsid:
        r["session_id"] = gsid
    if fallbacks:
        r["via_fallback"] = True
        r["fallbacks"] = fallbacks
    return r


def send_payload(session: str, keys: str, literal: bool = True, enter: bool = True,
                 host: str | None = None) -> dict[str, Any]:
    """Send keys to `session`, local or — when `host` is set — over ssh. literal=
    True sends text verbatim (`send-keys -l`), so chat input like "C-c" types
    those characters; literal=False interprets tmux key names (UI control chips)."""
    if host is None and _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    shell_warn = False
    try:
        code, cmd, _e = _server._run_tmux(
            ["display-message", "-p", "-t", session, "#{pane_current_command}"],
            host=host,
        )
        c = (cmd or "").strip().lower()
        if code == 0 and c in ("zsh", "bash", "sh", "-zsh", "-bash", "fish"):
            shell_warn = True
    except Exception:  # noqa: BLE001
        pass
    if literal:
        if keys:
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "-l", keys], host=host)
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
        if enter:
            # a paste-detecting TUI (Claude) needs text and Enter as SEPARATE
            # events with a beat between, or the Enter is swallowed into the paste
            # and the message never submits. Use the agent's measured settle.
            settle = _server._pane_settle(session, host)
            if keys and settle > 0:
                time.sleep(settle)
            code, _, err = _server._run_tmux(["send-keys", "-t", session, "Enter"], host=host)
            if code != 0:
                return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    else:
        args = ["send-keys", "-t", session, keys]
        if enter:
            args.append("Enter")
        code, _, err = _server._run_tmux(args, host=host)
        if code != 0:
            return {"ok": False, "error": "tmux_send_failed", "stderr": err}
    return {
        "ok": True,
        "session": session,
        "sent": keys,
        "literal": literal,
        "enter": enter,
        "shell_warn": shell_warn,
    }


def scroll_payload(
    session: str,
    direction: str = "up",
    *,
    amount: str = "page",
    host: str | None = None,
    mode: str | None = None,
) -> dict[str, Any]:
    """Scroll a live pane.

    Two realities:

    1. **Shell / normal buffer** (``history_size > 0``): use tmux copy-mode so
       capture-pane shows older lines.
    2. **Full-screen agent TUI** (Claude/Codex, ``history_size == 0``): the app
       owns the alternate screen — copy-mode is empty. Send PageUp/PageDown
       (or wheel keys) *into the app* so its own UI scrolls; then re-capture.

    ``mode`` force: ``tmux`` | ``app`` | ``auto`` (default).
    """
    if host is None and _server._resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    direction = (direction or "up").strip().lower()
    amount = (amount or "page").strip().lower()
    if direction not in ("up", "down"):
        return {"ok": False, "error": "bad_direction"}
    mode_f = (mode or "auto").strip().lower()
    hist_size = 0
    try:
        c0, meta, _ = _server._run_tmux(
            ["display-message", "-p", "-t", session, "#{history_size}"],
            host=host, timeout=10,
        )
        if c0 == 0 and meta.strip().isdigit():
            hist_size = int(meta.strip())
    except Exception:  # noqa: BLE001
        pass
    if mode_f == "auto":
        mode_f = "tmux" if hist_size > 0 else "app"

    if mode_f == "tmux":
        code, _, err = _server._run_tmux(["copy-mode", "-t", session], host=host)
        if code != 0:
            return {"ok": False, "error": "tmux_copy_mode_failed", "stderr": err}
        if amount in ("line", "lines", "1"):
            action = "scroll-up" if direction == "up" else "scroll-down"
        elif amount in ("half", "halfpage"):
            action = "halfpage-up" if direction == "up" else "halfpage-down"
        else:
            action = "page-up" if direction == "up" else "page-down"
        code, _, err = _server._run_tmux(
            ["send-keys", "-t", session, "-X", action],
            host=host,
        )
        if code != 0:
            return {
                "ok": False, "error": "tmux_scroll_failed",
                "stderr": err, "action": action, "mode": "tmux",
            }
        return {
            "ok": True, "session": session, "direction": direction,
            "amount": amount, "action": action, "mode": "tmux",
            "history_size": hist_size,
        }

    # App / alt-screen path — keystrokes the TUI should handle
    if amount in ("line", "lines", "1"):
        keys = "Up" if direction == "up" else "Down"
        # Some agents want mouse wheel for line scroll
        wheel = "WheelUp" if direction == "up" else "WheelDown"
    elif amount in ("half", "halfpage"):
        keys = "PageUp" if direction == "up" else "PageDown"
        wheel = keys
    else:
        keys = "PageUp" if direction == "up" else "PageDown"
        wheel = keys
    # Prefer PageUp/Down; also poke mouse-wheel for TUIs that only listen there.
    code, _, err = _server._run_tmux(
        ["send-keys", "-t", session, keys], host=host,
    )
    if code != 0:
        return {
            "ok": False, "error": "app_scroll_failed", "stderr": err,
            "keys": keys, "mode": "app", "history_size": hist_size,
        }
    # Best-effort second tick for mouse-only TUIs (ignore failure)
    if wheel not in (keys,):
        _server._run_tmux(["send-keys", "-t", session, wheel], host=host)
    # Burst of PageUp for "page" so Claude conversation actually moves
    if amount not in ("line", "lines", "1") and keys in ("PageUp", "PageDown"):
        for _ in range(2):
            _server._run_tmux(["send-keys", "-t", session, keys], host=host)
    return {
        "ok": True, "session": session, "direction": direction,
        "amount": amount, "action": keys, "mode": "app",
        "history_size": hist_size,
        "hint": "alt-screen agent — scrolled inside the app, not tmux history",
    }


def _switch_plan(session: str, host: str | None = None, to: str | None = None,
                 dry_run: bool = False) -> dict[str, Any]:
    """Fail a session over to another Claude account, in code: exit the agent,
    relaunch under the target account's CLAUDE_CONFIG_DIR, and resume (`claude -c`).
    Deterministic — tmux only, no LLM. `dry_run` returns the exact plan/commands
    without touching the pane."""
    now = time.time()
    plans = _plans()
    if len(plans) < 2 and to is None:
        return {"ok": False, "error": "need ≥2 accounts in ~/.config/emux/plans.json"}
    cur = _SESSION_PLAN.get(session) or (plans[0]["name"] if plans else None)
    if to is not None:
        target = next((p for p in plans if p["name"] == to), None)
        if target is None:
            return {"ok": False, "error": f"unknown plan {to!r}"}
    else:
        target = _next_plan(cur, now)
    if target is None:
        return {"ok": False, "error": "no available account (all cooling down)"}
    cfg = target["config_dir"]
    relaunch = f"CLAUDE_CONFIG_DIR={shlex.quote(cfg)} claude -c"
    info = {"from": cur, "to": target["name"], "config_dir": cfg, "relaunch": relaunch}
    if dry_run:
        return {"ok": True, "dry_run": True, **info}
    # 1. clear any partial input, then exit the agent cleanly to a shell
    send_payload(session, "Escape", literal=False, enter=False, host=host)
    send_payload(session, "/exit", literal=True, enter=True, host=host)
    time.sleep(2.0)   # let the agent tear down to the shell prompt
    # 2. relaunch under the target account, continuing the conversation
    r = send_payload(session, relaunch, literal=True, enter=True, host=host)
    if not r.get("ok"):
        return {"ok": False, "error": "relaunch_failed", **info}
    # 3. record the switch and cool down the account we just left
    _SESSION_PLAN[session] = target["name"]
    if cur:
        _PLAN_EXHAUSTED[cur] = now + _PLAN_COOLDOWN
    return {"ok": True, **info}


PAGE = r"""<!doctype html>
<html lang="en" data-os="__OS__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__ROOM_TITLE__</title>
<style id="skin-theme">__THEME_CSS__</style>
<link rel="icon" href="__FAVICON__">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=VT323&family=IBM+Plex+Mono:ital,wght@0,400;0,500;0,600;1,400&display=swap" rel="stylesheet">
<style>
/* Color tokens come from skin (light/dark). Layout only below. */
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{
  background:var(--bg);
  color:var(--text);
  font:14px/1.5 "IBM Plex Mono",ui-monospace,monospace;
  display:flex;
  overflow:hidden;
}
#side{
  width:280px;flex:none;height:100%;display:flex;flex-direction:column;
  background:var(--bg-raise);border-right:1px solid var(--line);
}
#brand{padding:18px 18px 10px}
#brand .brand-top{
  display:flex;align-items:center;gap:10px;flex-wrap:wrap;
}
#brand .brand-mark{
  display:flex;align-items:center;gap:10px;color:var(--amber);
}
#brand .skin-logo{flex:none;display:block}
#brand .brand-word{
  font-family:"VT323",monospace;font-size:40px;font-weight:400;letter-spacing:2px;
  color:var(--amber);line-height:1;
  text-shadow:0 0 18px color-mix(in srgb, var(--amber) 45%, transparent);
}
/* Manager vs worker — structural role mark (not color-alone) */
.rolechip{
  display:inline-block;font:700 10px/1 "IBM Plex Mono",monospace;
  letter-spacing:1.2px;text-transform:uppercase;
  padding:4px 8px;border-radius:999px;border:1px solid var(--amber);
  color:var(--on-accent);background:var(--amber);vertical-align:middle;
}
.rolechip.worker{
  background:transparent;color:var(--text-dim);border-color:var(--line);font-weight:600;
}
#brand small,#brand #brandtag{
  display:block;margin-top:6px;color:var(--text-dim);font-size:11px;
  letter-spacing:.5px;text-transform:none;line-height:1.35;
}
/* Manager allowlist — primary surface for manager products */
#managed{margin-top:10px;display:flex;flex-direction:column;gap:6px}
#managed[hidden]{display:none!important}
#managed .mhd{
  font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);
  margin:2px 0 0;
}
.mplane{
  border:1px solid var(--line);border-left:3px solid var(--amber);
  background:var(--bg-card);padding:8px 10px;border-radius:4px;
  display:grid;grid-template-columns:1fr auto;gap:2px 8px;align-items:center;
}
.mplane .mid{font-weight:700;color:var(--amber);font-size:12px;letter-spacing:.3px}
.mplane .mlane{font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.8px}
.mplane .mstat{font-size:10px;grid-column:1/-1;color:var(--text-dim)}
.mplane .mstat.up{color:var(--live)}
.mplane .mstat.down{color:var(--stale)}
.mplane .mstat.deg{color:var(--amber)}
.mplane a.mopen{
  grid-column:1/-1;font-size:10px;color:var(--amber);text-decoration:none;
  border-top:1px dashed var(--line);padding-top:5px;margin-top:2px;
}
.mplane a.mopen:hover{text-decoration:underline}
#tagbar{display:flex;flex-wrap:wrap;gap:4px;padding:4px 8px 0;max-height:26vh;overflow-y:auto}  /* EID-880: cap tag noise so the session list isn't buried */
#tagbar:empty{display:none}
.tagchip{font-size:10px;letter-spacing:.5px;padding:2px 7px;border:1px solid var(--line);
  border-radius:10px;color:var(--text-dim);cursor:pointer;user-select:none;white-space:nowrap}
.tagchip:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.tagchip.on{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.tagchip .cnt{opacity:.6;margin-left:3px}
.tagchip.clr{color:var(--text-dim)}
.cochip{font-size:10px;letter-spacing:.3px;padding:2px 7px;border:1px solid;border-radius:10px;
  cursor:pointer;user-select:none;white-space:nowrap;font-weight:700;opacity:.9}
.cochip:hover{opacity:1}
.cochip.on{color:var(--on-accent)}
.cochip .cnt{opacity:.7;margin-left:3px;font-weight:400}
.cco{font-size:9px;font-weight:700;letter-spacing:.3px;padding:1px 6px;border-radius:8px;
  color:var(--on-accent);white-space:nowrap}
#sessions{flex:1;overflow-y:auto;padding:8px}
.deadtoggle{font-size:11px;color:var(--text-dim);padding:8px 12px 4px;cursor:pointer;user-select:none;
  letter-spacing:.5px;text-transform:uppercase;border-top:1px dashed var(--line);margin-top:6px}
.deadtoggle:hover{color:var(--amber)}
.card{
  border:1px solid var(--line);border-left:3px solid var(--amber-faint);
  background:var(--bg-card);padding:10px 12px;margin-bottom:8px;cursor:pointer;
  transition:border-color .15s, transform .15s;
}
.card:hover{border-color:var(--amber-dim);transform:translateX(2px)}
.card.active{border-left-color:var(--amber);box-shadow:0 0 14px color-mix(in srgb, var(--amber) 12%, transparent) inset}
.card .nm{color:var(--amber);font-weight:600}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;vertical-align:1px}
.dot.live{background:var(--live);box-shadow:0 0 6px var(--live)}
.dot.warn{background:var(--amber);box-shadow:0 0 6px var(--amber)}
.dot.stale{background:var(--stale);box-shadow:0 0 6px var(--stale)}
.card .sub{color:var(--text-dim);font-size:11px;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card .badges{margin-top:4px;font-size:10px}
.card .badges span{border:1px solid var(--line);color:var(--text-dim);padding:0 5px;margin-right:4px}
#side footer{padding:10px 18px;border-top:1px solid var(--line);color:var(--text-dim);font-size:10px;letter-spacing:1px}
#main{flex:1;height:100%;display:flex;flex-direction:column;min-width:0}
/* live fleet feed — a collapsible right rail showing what agents do as they do it */
#feed{width:300px;flex:none;height:100%;border-left:1px solid var(--line);
  background:var(--bg-raise);display:flex;flex-direction:column;transition:width .18s ease}
#feed:not(.open){width:0;border-left:none;overflow:hidden}
#feedhead{flex:none;display:flex;align-items:center;gap:8px;padding:11px 14px;
  border-bottom:1px solid var(--line);font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--amber-dim)}
#feedcount{color:var(--text-dim);font-size:10px}
#feedclose{margin-left:auto;background:transparent;border:none;color:var(--text-dim);
  font-size:18px;cursor:pointer;line-height:1;padding:0 4px}
#feedclose:hover{color:var(--amber)}
#feedlist{flex:1;overflow-y:auto;padding:6px 0}
.fev{display:flex;gap:8px;padding:5px 12px;border-bottom:1px solid color-mix(in srgb, var(--amber) 5%, transparent);font-size:11px;align-items:baseline}
.fev .fage{color:var(--text-dim);font-size:9px;white-space:nowrap;min-width:26px}
.fev .ftag{font-weight:700;white-space:nowrap}
.fev .fsess{color:var(--amber-dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:90px}
.fev .ftext{color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.fev.k-NEED .ftag,.fev.k-ERROR .ftag,.fev.k-error .ftag{color:var(--stale)}
.fev.k-IDLE .ftag,.fev.k-DONE .ftag,.fev.k-READY .ftag{color:var(--live)}
.fev.k-PROGRESS .ftag{color:var(--user)}
.fev.k-op .ftag{color:var(--amber-dim)}
.fev.fresh{animation:fevin .5s ease}
@keyframes fevin{from{background:color-mix(in srgb, var(--amber) 18%, transparent)}to{background:transparent}}
#topbar{
  flex:none;display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  padding:10px 22px;border-bottom:1px solid var(--line);background:var(--bg-raise);
}
#topbar #title{font-family:"VT323",monospace;font-size:26px;color:var(--amber);letter-spacing:1px}
#topbar #status{font-size:11px;color:var(--text-dim);letter-spacing:2px;text-transform:uppercase}
#topbar #status.err{color:var(--stale)}
#tabs{margin-left:auto;display:flex;gap:6px}
.tab{
  font-family:"VT323",monospace;font-size:17px;letter-spacing:2px;padding:3px 14px;
  background:transparent;color:var(--text-dim);border:1px solid var(--line);cursor:pointer;
}
.tab:hover{color:var(--amber);border-color:var(--amber-dim)}
.tab.on{background:var(--amber);color:var(--on-accent);border-color:var(--amber)}
#views{flex:1;overflow-y:auto;padding:18px}
.tilegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.tile{
  border:1px solid var(--line);background:var(--bg-card);cursor:pointer;overflow:hidden;
  display:flex;flex-direction:column;transition:border-color .2s, box-shadow .4s;
}
.tile:hover{border-color:var(--amber-dim)}
.tile.hot{border-color:var(--amber);box-shadow:0 0 16px color-mix(in srgb, var(--amber) 25%, transparent)}
.tile.dead{opacity:.45}
/* WAITING ON YOU: marching ants around the edge + a slow breathing orb glow, so a
   session that needs your decision is impossible to miss on a wall of tiles. */
.needy{position:relative;animation:orb 1.6s ease-in-out infinite}
/* canonical marching ants: four dashed edges, each scrolling one dash-period.
   Reliable (moves visibly) where the masked-border trick was not. */
.needy::before{content:"";position:absolute;inset:0;pointer-events:none;z-index:3;
  background-image:
    linear-gradient(90deg, var(--amber) 50%, transparent 50%),
    linear-gradient(90deg, var(--amber) 50%, transparent 50%),
    linear-gradient(0deg,  var(--amber) 50%, transparent 50%),
    linear-gradient(0deg,  var(--amber) 50%, transparent 50%);
  background-size:14px 2px, 14px 2px, 2px 14px, 2px 14px;
  background-position:0 0, 0 100%, 0 0, 100% 0;
  background-repeat:repeat-x, repeat-x, repeat-y, repeat-y;
  animation:ants .55s infinite linear}
@keyframes ants{to{background-position:14px 0, -14px 100%, 0 -14px, 100% 14px}}
@keyframes orb{0%,100%{box-shadow:0 0 6px color-mix(in srgb, var(--amber) 35%, transparent)}50%{box-shadow:0 0 22px 3px color-mix(in srgb, var(--amber) 60%, transparent)}}
.card.needy{border-left-color:var(--amber)}
/* LOUD needs-you: a red ring + glow + a pulsing corner badge — red reads as
   "attention" against the amber theme, which amber-on-amber ants did not. */
.tile.needy{border:2px solid #e0483a;animation:needpulse 1.3s ease-in-out infinite}
.tile.needy::before{background-image:
  linear-gradient(90deg,#e0483a 50%,transparent 50%),linear-gradient(90deg,#e0483a 50%,transparent 50%),
  linear-gradient(0deg,#e0483a 50%,transparent 50%),linear-gradient(0deg,#e0483a 50%,transparent 50%)}
@keyframes needpulse{0%,100%{box-shadow:0 0 0 0 rgba(224,72,58,.55),0 0 14px rgba(224,72,58,.4)}
  50%{box-shadow:0 0 0 4px rgba(224,72,58,0),0 0 22px 3px rgba(224,72,58,.65)}}
/* COST / usage limit — gold, distinct from the red needs-you, since it's a
   money/throttle signal, not a decision gate. Still loud: ring + corner badge. */
.tile.costcap{border:2px solid #d99a00;animation:costpulse 1.5s ease-in-out infinite}
@keyframes costpulse{0%,100%{box-shadow:0 0 0 0 rgba(217,154,0,.5),0 0 14px rgba(217,154,0,.4)}
  50%{box-shadow:0 0 0 4px rgba(217,154,0,0),0 0 22px 3px rgba(217,154,0,.6)}}
.costbadge{position:absolute;top:7px;left:7px;z-index:5;background:#a06800;color:#fff;
  font-size:9.5px;font-weight:800;letter-spacing:.5px;padding:3px 7px;border-radius:5px;
  box-shadow:0 1px 5px rgba(0,0,0,.35)}
.card.costcap{border-left:4px solid #d99a00 !important;background:rgba(217,154,0,.07)}
/* z-index 50: must sit *under* topbar controls and under #newmodal (EID-1110 dogfood).
   When visible, shift chrome down so the banner never intercepts FEED/NEW/SETTINGS clicks. */
#costbanner{display:none;position:fixed;top:0;left:0;right:0;z-index:50;cursor:pointer;
  background:#a06800;color:#fff;text-align:center;padding:9px 12px;font-weight:600;
  box-shadow:0 2px 14px rgba(160,104,0,.5)}
#costbanner u{text-underline-offset:3px}
#modalswitch.hot{border-color:#d99a00;color:#d99a00;font-weight:700}
#modalswitch.armed{background:#a06800;color:#fff;border-color:#a06800;font-weight:700}
body.costalert #costbanner{display:block;animation:costbannerpulse 1.4s ease-in-out infinite}
body.costalert #topbar,
body.costalert #side,
body.costalert #feed,
body.costalert #main{padding-top:40px;box-sizing:border-box}
@keyframes costbannerpulse{0%,100%{background:#a06800}50%{background:#c48400}}
.needbadge{position:absolute;top:7px;right:7px;z-index:5;background:#c0392b;color:#fff;
  font-size:9.5px;font-weight:800;letter-spacing:.6px;padding:3px 7px;border-radius:5px;
  box-shadow:0 1px 5px rgba(0,0,0,.35);animation:needpulse 1.3s ease-in-out infinite}
.card.needy{border-left:4px solid #e0483a !important;background:rgba(224,72,58,.06)}
pre.gonecache{color:var(--text-dim);font-style:italic;opacity:.85;white-space:pre-wrap}
.tile header{
  display:flex;align-items:baseline;gap:8px;padding:6px 10px;
  background:var(--bg-card);border-bottom:1px solid var(--line);
}
.tile header .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tile header .age{margin-left:auto;font-size:10px;color:var(--text-dim);letter-spacing:1px;white-space:nowrap}
.tile pre{
  flex:1;font:9.5px/1.35 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:8px 10px;height:190px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-end;
}
.tile pre.empty{color:var(--text-dim);font-style:italic;justify-content:center;text-align:center}
.group{margin-bottom:26px}
.group h2{
  font-family:"VT323",monospace;font-size:22px;letter-spacing:2px;color:var(--amber-dim);
  border-bottom:1px solid var(--line);margin-bottom:12px;padding-bottom:4px;
}
.group h2 .cnt{color:var(--text-dim);font-size:14px}
.actrows{display:flex;flex-direction:column;gap:10px;max-width:980px}
.actrow{
  display:flex;align-items:center;gap:14px;border:1px solid var(--line);
  background:var(--bg-card);padding:10px 14px;cursor:pointer;
}
.actrow:hover{border-color:var(--amber-dim)}
.actrow .nm{color:var(--amber);font-weight:600;width:200px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:none}
.cells{display:flex;gap:2px;flex:1;min-width:0}
.cell{width:9px;height:18px;background:var(--bg-card);flex:none}
.cell.on{background:var(--amber);box-shadow:0 0 5px color-mix(in srgb, var(--amber) 60%, transparent)}
.cell.recent{background:var(--user)}
.actrow .age{font-size:11px;color:var(--text-dim);width:120px;text-align:right;flex:none;letter-spacing:1px}
/* flow view — live mini-pane boxes over an SVG edge layer */
#flowwrap{position:relative;margin:0 auto}
#flowsvg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.fbox{
  position:absolute;transform:translate(-50%,-50%);z-index:1;
  border:1px solid var(--line);background:var(--bg-card);cursor:pointer;overflow:hidden;
  transition:border-color .2s, box-shadow .4s;
}
.fbox:hover{border-color:var(--amber-dim)}
.fbox.hot{border-color:var(--amber);box-shadow:0 0 16px color-mix(in srgb, var(--amber) 25%, transparent)}
.fbox.dead{opacity:.45}
.fbox .ftitle{display:flex;align-items:center;gap:6px;padding:5px 9px;background:var(--bg-card);border-bottom:1px solid var(--line)}
.fbox .ftitle .nm{color:var(--amber);font-weight:600;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fbox .ftitle .ag{margin-left:auto;color:var(--amber-dim);font-size:10px;letter-spacing:.5px;white-space:nowrap}
/* per-agent status pip — at-a-glance state at EVERY level of the hierarchy, so
   the manager (and human) tracks all agents + their subs without opening them. */
.spip{font-size:8.5px;letter-spacing:.5px;text-transform:uppercase;padding:1px 5px;border-radius:7px;
  white-space:nowrap;font-weight:700;border:1px solid currentColor}
.spip .lsrc{font-size:7px;opacity:.75;margin-left:1px;vertical-align:top}  /* EID-881: ledger-sourced marker */
/* EID-880 stretch: is `send` the right move now? drive (send-ok) vs observe */
.sbadge{font-size:8px;letter-spacing:.3px;text-transform:uppercase;padding:0 4px;border-radius:6px;
  white-space:nowrap;border:1px solid currentColor;opacity:.85;margin-left:2px}
.sbadge.sb-drive{color:var(--live)}
.sbadge.sb-observe{color:var(--amber)}
.spip.st-running{color:var(--live)}
.spip.st-idle{color:var(--text-dim)}
.spip.st-error{color:var(--stale)}
.spip.st-waiting_human{color:var(--amber);animation:orb 1.8s ease-in-out infinite}
.spip.st-dead{color:var(--text-dim);opacity:.5}
.spip.st-asking{color:var(--amber);animation:orb 1.6s ease-in-out infinite}
/* EID-880 honest states — each visually distinct so stale ≠ stuck ≠ failed */
.spip.st-waiting_external{color:#3b6ea5}                 /* blocked on build/network — blue */
.spip.st-stuck{color:#c9761f;font-weight:800}            /* no change, not at a prompt — orange (needs unstick, NOT failed) */
.spip.st-failed{color:#c0392b;font-weight:800}           /* explicit failure — red */
.spip.st-offline{color:var(--text-dim);opacity:.55}      /* registered but not running — grey (stale, NOT failed) */
/* it's asking YOU — a pulsing question mark in place of the heartbeat */
.qmark{display:inline-flex;align-items:center;justify-content:center;
  width:14px;height:14px;margin-right:6px;border-radius:50%;
  background:var(--amber);color:var(--on-accent);font-weight:800;font-size:10px;
  vertical-align:middle;animation:qpulse 1.1s ease-in-out infinite}
@keyframes qpulse{0%,100%{box-shadow:0 0 0 0 color-mix(in srgb, var(--amber) 50%, transparent)}50%{box-shadow:0 0 0 4px color-mix(in srgb, var(--amber) 1%, transparent)}}
/* the summary rail — a thin always-on "what's happening", hover for the full text */
.rail{position:relative;font-size:9.5px;line-height:1.5;padding:2px 10px;
  background:var(--bg-raise);border-bottom:1px solid var(--line);color:var(--text-dim);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:default;flex:none}
.rail .rv{font-weight:700;color:var(--amber-dim);text-transform:uppercase;letter-spacing:.5px;font-size:8.5px}
.rail.st-running .rv{color:var(--live)}
.rail.st-error .rv{color:var(--stale)}
.rail.st-asking .rv,.rail.st-waiting_human .rv{color:var(--amber)}
.rfull{display:none;position:absolute;left:0;right:0;top:100%;z-index:30;
  background:var(--bg-card);border:1px solid var(--amber-faint);border-radius:0 0 3px 3px;
  padding:7px 11px;white-space:normal;font-size:11px;line-height:1.4;color:var(--text);
  box-shadow:0 6px 16px rgba(0,0,0,.45)}
.rail:hover{overflow:visible}
.rail:hover .rfull{display:block}
/* a RUNNING agent shows a hospital-monitor heartbeat instead of a static dot */
.ekg{width:30px;height:12px;flex:none;vertical-align:middle}
.ekg polyline{fill:none;stroke-width:1.5;stroke-linejoin:round;stroke-linecap:round}
.ekg .base{stroke:var(--live);opacity:.20}
.ekg .beat{stroke:var(--live);stroke-dasharray:8 48;animation:ekg 1.25s linear infinite}
@keyframes ekg{from{stroke-dashoffset:56}to{stroke-dashoffset:0}}
.fbox pre{
  font:8.5px/1.32 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:6px 9px;height:96px;overflow:hidden;
  display:flex;flex-direction:column;justify-content:flex-end;
}
.fbox pre.empty{color:var(--text-dim);font-style:italic;justify-content:center;text-align:center}
.agentbadge{color:var(--amber-dim);font-size:10px;letter-spacing:1px;margin-left:6px}
.agi{font-weight:700}
.ag-claude{color:#d97757}.ag-codex{color:#10a37f}.ag-gemini{color:#5a8dff}
.ag-grok{color:#e8e8e8}.ag-opencode{color:#b57bff}.ag-aider{color:#f0a020}
.ag-hermes{color:#b07de0}.ag-shell{color:#8a8a72}.ag-editor{color:#8a8a72}
.edge{stroke:var(--amber);stroke-width:2;fill:none;stroke-dasharray:7 5;animation:flow 1.1s linear infinite;
  filter:drop-shadow(0 0 4px color-mix(in srgb, var(--amber) 40%, transparent))}
@keyframes flow{to{stroke-dashoffset:-12}}
.rowlabel{fill:var(--text-dim);font:10px "IBM Plex Mono",monospace;letter-spacing:2px;text-transform:uppercase}
.sep{stroke:var(--line);stroke-width:1;stroke-dasharray:3 5}
#flowhint{color:var(--text-dim);font-size:11px;font-style:italic;margin-top:6px}
#flowhint code{color:var(--amber-dim);font-style:normal}
#head{flex:1;overflow-y:auto;padding:22px;display:none;flex-direction:column;gap:12px}
.bubble{max-width:88%;padding:10px 14px;border:1px solid var(--line);position:relative}
.bubble .who{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim);margin-bottom:5px}
.bubble.user{
  align-self:flex-end;background:var(--bg-raise);border-color:var(--amber-faint);
  color:var(--user);border-radius:10px 10px 0 10px;
}
.bubble.sys{align-self:center;color:var(--text-dim);font-size:11px;border:none;font-style:italic}
#screen-bubble{
  align-self:flex-start;width:100%;max-width:100%;
  background:var(--bg-card);border:1px solid var(--line);border-radius:10px 10px 10px 0;
}
#screen{
  font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:4px 2px 2px;
}
#screen.dimmed{opacity:.35}
.cursorblock{display:inline-block;width:8px;height:14px;background:var(--amber);
  vertical-align:-2px;animation:blink 1.1s steps(1) infinite;box-shadow:0 0 8px color-mix(in srgb, var(--amber) 80%, transparent)}
@keyframes blink{50%{opacity:0}}
#empty{display:flex;align-items:center;justify-content:center;flex-direction:column;gap:10px;color:var(--text-dim);height:100%}
#empty .glyph{font-family:"VT323",monospace;font-size:80px;color:var(--amber-faint);text-shadow:0 0 30px color-mix(in srgb, var(--amber) 15%, transparent)}
#composer{flex:none;border-top:1px solid var(--line);background:var(--bg-raise);padding:12px 22px 16px}
#chips{display:flex;gap:8px;margin-bottom:10px}
.chip{
  font:11px "IBM Plex Mono",monospace;color:var(--amber-dim);background:transparent;
  border:1px solid var(--line);padding:3px 10px;cursor:pointer;letter-spacing:1px;
}
.chip:hover{color:var(--amber);border-color:var(--amber-dim)}
#row{display:flex;gap:10px}
#input{
  flex:1;background:var(--bg-card);border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:11px 14px;outline:none;caret-color:var(--amber);
}
#input:focus{border-color:var(--amber-dim);box-shadow:0 0 12px color-mix(in srgb, var(--amber) 10%, transparent)}
#send{
  font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 26px;
  background:var(--amber);color:var(--on-accent);border:none;cursor:pointer;
}
#send:hover{box-shadow:0 0 18px color-mix(in srgb, var(--amber) 50%, transparent)}
/* recency-tiered age coloring (#17) */
.age.t-now{color:var(--amber)}
.age.t-min{color:var(--amber-dim)}
.age.t-old{color:var(--text-dim)}
/* stale sessions in the sidebar (#19) */
.card.gone{opacity:.5}
.card.gone .nm{text-decoration:line-through}
/* attached marker (#18) */
.att{color:var(--live);font-size:10px;letter-spacing:1px;margin-left:6px}
/* sidebar filter (#7) */
#filter{
  margin:0 8px 8px;width:calc(100% - 16px);background:var(--bg-card);border:1px solid var(--line);
  color:var(--text);font:11px "IBM Plex Mono",monospace;padding:6px 9px;outline:none;
}
#filter:focus{border-color:var(--amber-dim)}
/* topbar action buttons (#12 #15) */
.act{
  font:11px "IBM Plex Mono",monospace;color:var(--amber-dim);background:transparent;
  border:1px solid var(--line);padding:3px 9px;cursor:pointer;letter-spacing:1px;
  text-decoration:none;white-space:nowrap;
}
.act:hover{color:var(--amber);border-color:var(--amber-dim)}
/* word-wrap toggle off → horizontal scroll (#9) */
#screen.nowrap{white-space:pre;overflow-x:auto}
/* jump-to-bottom pill (#11) */
#jump{
  position:absolute;left:50%;transform:translateX(-50%);bottom:96px;display:none;
  font-family:"VT323",monospace;font-size:16px;letter-spacing:1px;
  background:var(--amber);color:var(--on-accent);border:none;padding:4px 16px;cursor:pointer;
  box-shadow:0 0 14px color-mix(in srgb, var(--amber) 50%, transparent);z-index:5;
}
/* zoom-in steer modal — fills most of the viewport (not a fixed 900×620 card) */
#modal{position:fixed;inset:0;z-index:200;display:none;align-items:stretch;justify-content:stretch;padding:1vh 1vw;box-sizing:border-box}
#modal.open{display:flex}
#modalback{position:absolute;inset:0;background:rgba(6,4,2,.72);backdrop-filter:blur(2px)}
#modalpanel{
  position:relative;flex:1 1 auto;width:100%;height:100%;min-height:0;min-width:0;
  display:flex;flex-direction:column;
  /* clip: children (esp. #modalscreen) must scroll inside, not grow the panel */
  overflow:hidden;
  background:var(--bg-raise);border:1px solid var(--amber-dim);
  box-shadow:0 0 50px color-mix(in srgb, var(--amber) 18%, transparent);
  animation:zoomin .16s ease-out;
}
@keyframes zoomin{from{transform:scale(.98);opacity:.4}to{transform:scale(1);opacity:1}}
#modalhead{display:flex;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--line);background:var(--bg-card);flex:0 0 auto}
#modalhead .nm{font-family:"VT323",monospace;font-size:22px;color:var(--amber);letter-spacing:1px}
#modalhead .ag{color:var(--amber-dim);font-size:12px;letter-spacing:1px}
#modalhead .st{margin-left:auto;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
#modalclose,#modalpop{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:13px;cursor:pointer;padding:2px 10px;margin-left:6px}
#modalclose:hover,#modalpop:hover{color:var(--amber);border-color:var(--amber-dim)}
/* solo tab: one session fills the Chrome tab — no fleet chrome */
body.solo-session #side,
body.solo-session #feed,
body.solo-session #topbar,
body.solo-session #costbanner,
body.solo-session #scrim,
body.solo-session #footer{display:none!important}
body.solo-session #main{margin:0;padding:0;width:100%;height:100vh;overflow:hidden}
body.solo-session #views,body.solo-session #head,body.solo-session #composer,body.solo-session #jump{display:none!important}
body.solo-session #modal{display:flex!important;padding:0}
body.solo-session #modalback{display:none}
body.solo-session #modalpanel{
  width:100vw;height:100vh;max-height:100vh;border:none;box-shadow:none;border-radius:0;animation:none;
  overflow:hidden;
}
body.solo-session #modalpop{display:none} /* already in a tab */
/* --- new-session modal --- */
/* Above costbanner (50) and room chrome; below steer #modal (200) is fine for spawn. */
#newmodal{display:none;position:fixed;inset:0;z-index:180}
#newmodal.open{display:block}
#newback{position:absolute;inset:0;background:rgba(0,0,0,.72)}
#newpanel{position:relative;margin:6vh auto;width:min(720px,92vw);background:var(--bg-card);
  border:1px solid var(--amber-faint);box-shadow:0 0 60px rgba(0,0,0,.7);display:flex;flex-direction:column}
#newhead{display:flex;align-items:center;gap:10px;padding:11px 16px;border-bottom:1px solid var(--line)}
#newhead .nm{font-family:"VT323",monospace;font-size:24px;color:var(--amber);letter-spacing:1px}
#newhead .st{margin-left:auto;font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
#newclose{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:14px;cursor:pointer;padding:2px 11px;margin-left:10px}
#newclose:hover{color:var(--amber);border-color:var(--amber-dim)}
#newbody{padding:16px;display:flex;flex-direction:column;gap:4px;max-height:68vh;overflow-y:auto}
#newbody input{background:var(--bg);border:1px solid var(--line);color:var(--amber);
  font-family:inherit;font-size:13px;padding:7px 10px;width:100%}
#newbody input:focus{outline:none;border-color:var(--amber-dim)}
#newbody input:disabled{opacity:.45}
.askrow{display:flex;gap:8px;margin-bottom:6px}
.askrow input{font-size:15px;padding:11px 12px}
/* THE ANSWER — the only thing you should have to read */
#result{display:none;padding:14px 2px 4px}
#result.show{display:block}
#rverb{font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--text-dim);margin-bottom:2px}
#rwhat{font-family:"VT323",monospace;font-size:30px;line-height:1.1;color:var(--amber);letter-spacing:1px}
#rwhere{font-size:12px;color:var(--amber-dim);margin-top:3px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#rflags{margin-top:6px}
#rflags .flag{font-size:9.5px;padding:2px 7px;border-radius:9px;margin-right:5px;
  background:rgba(255,95,86,.16);color:#ff8a80;border:1px solid rgba(255,95,86,.4)}
#rwhy{font-size:11px;font-style:italic;color:var(--text-dim);margin-top:8px}
#rpeek{margin-top:8px}
#rpeek summary{font-size:10px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);cursor:pointer}
#rpeek[hidden]{display:none}
#peek2{margin:6px 0 0;padding:7px 9px;border:1px solid var(--line);background:var(--bg);
  font-size:10.5px;line-height:1.35;color:var(--amber-dim);max-height:120px;overflow:auto;white-space:pre;opacity:.85}
/* the machinery — only when you ask for it */
#tree{display:none}
#tree.show{display:block}
.linkish{background:transparent;border:none;color:var(--text-dim);font-family:inherit;
  font-size:11px;cursor:pointer;margin-right:auto;text-decoration:underline;padding:0}
.linkish:hover{color:var(--amber-dim)}
/* a step is dependent on the one above it — locked until that one resolves */
.step{border-left:2px solid var(--line);padding:8px 0 10px 12px;margin-left:3px}
.step.locked{opacity:.38;pointer-events:none}
.step.done{border-left-color:var(--amber)}
.steph{display:flex;align-items:center;gap:8px;margin-bottom:6px}
.steph .num{width:16px;height:16px;border-radius:50%;border:1px solid var(--amber-faint);
  color:var(--amber-dim);font-size:10px;display:flex;align-items:center;justify-content:center}
.step.done .steph .num{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.steph .lbl{font-size:10px;letter-spacing:2px;text-transform:uppercase;color:var(--text-dim)}
.steph .sub{font-size:10px;color:var(--text-dim);opacity:.65}
.steph .why{margin-left:auto;font-size:10px;font-style:italic;color:var(--amber-dim);
  max-width:52%;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chips{display:flex;flex-wrap:wrap;gap:5px}
.hchip{font-size:11px;padding:3px 10px;border:1px solid var(--line);border-radius:11px;
  color:var(--text-dim);cursor:pointer;user-select:none}
.hchip:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.hchip.on{background:var(--amber);border-color:var(--amber);color:var(--on-accent);font-weight:700}
.hchip.ai::after{content:" ✦";opacity:.8}
.lanes{display:flex;gap:6px;margin-bottom:5px}
.lane{font-size:10px;letter-spacing:.5px;padding:3px 10px;border:1px solid var(--line);
  color:var(--text-dim);cursor:pointer;user-select:none;border-radius:3px}
.lane:hover{border-color:var(--amber-faint);color:var(--amber-dim)}
.lane.on{border-color:var(--amber);color:var(--amber);background:color-mix(in srgb, var(--amber) 9%, transparent)}
.lane b{font-weight:700;opacity:.7;margin-left:5px}
.dirchoices{max-height:150px;overflow-y:auto;border:1px solid var(--line);margin-top:5px}
.dirrow .meta{opacity:.55;margin-left:8px;font-size:10px}
.dirrow.on .meta{opacity:.8}
/* resuming is not creating — you're touching something that already has state, so look at it */
#peekwrap{display:none;margin-top:7px;border:1px solid var(--line);background:var(--bg)}
#peekwrap.show{display:block}
#peekhead{font-size:9px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-dim);
  padding:4px 8px;border-bottom:1px solid var(--line);display:flex;gap:8px;align-items:center}
#peekflags .flag{font-size:9px;padding:1px 6px;border-radius:8px;margin-left:4px;
  background:rgba(255,95,86,.16);color:#ff8a80;border:1px solid rgba(255,95,86,.4)}
#peek{margin:0;padding:7px 9px;font-size:10.5px;line-height:1.35;color:var(--amber-dim);
  max-height:112px;overflow:auto;white-space:pre;opacity:.85}
#guinote{font-size:10px;color:#ffb27d;margin-top:4px}
#guinote:empty{display:none}
.dirrow{padding:5px 9px;font-size:12px;color:var(--text-dim);cursor:pointer;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;border-bottom:1px solid color-mix(in srgb, var(--amber) 6%, transparent)}
.dirrow:hover{background:color-mix(in srgb, var(--amber) 7%, transparent);color:var(--amber-dim)}
.dirrow.on{background:var(--amber);color:var(--on-accent);font-weight:700}
.dirrow .ai{opacity:.75;margin-left:6px}
/* orphans view + machine facet */
#mvhosts{display:flex;gap:6px;flex-wrap:wrap;padding:10px 2px}
#mverr{color:var(--stale);font-size:11px;min-height:14px;padding:0 2px}
.hosttag{font-size:9px;color:var(--text-dim);border:1px solid var(--line);
  border-radius:8px;padding:1px 6px;margin-left:6px;white-space:nowrap;flex-shrink:0}
.tile.orph header{gap:6px}
.tile.chat{cursor:pointer}
.tile.chat.open{border-color:var(--amber);box-shadow:0 0 14px color-mix(in srgb, var(--amber) 20%, transparent)}
.tile.chat header{gap:6px;flex-wrap:wrap}
.tile.chat .tool{font-size:10px;letter-spacing:1px;text-transform:uppercase;padding:1px 6px;border:1px solid var(--line);border-radius:3px;color:var(--text-dim)}
.tile.chat .tool.claude{border-color:color-mix(in srgb, #d97757 50%, var(--line));color:#d97757}
.tile.chat .tool.grok{border-color:color-mix(in srgb, var(--amber) 50%, var(--line));color:var(--amber)}
.tile.chat .st-live{color:var(--ok,#3dba6e);font-size:10px;letter-spacing:1px;text-transform:uppercase}
.tile.chat .st-recent{color:var(--amber);font-size:10px;letter-spacing:1px;text-transform:uppercase}
.tile.chat .st-stale{color:var(--stale);font-size:10px;letter-spacing:1px;text-transform:uppercase}
.tile.chat .st-resumed{color:var(--ok,#3dba6e);font-size:10px;letter-spacing:1px;text-transform:uppercase}
.tile.chat .gm{font-size:9px;letter-spacing:1px;color:var(--ok,#3dba6e);border:1px solid color-mix(in srgb, var(--ok,#3dba6e) 40%, var(--line));padding:0 4px;border-radius:2px}
.tile.chat .sum{font-size:12px;color:var(--text);padding:4px 10px 6px;line-height:1.45;max-height:3.2em;overflow:hidden}
.tile.chat .cwd{font-size:10px;color:var(--text-dim);padding:0 10px 4px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* Linear issue keys → clickable (AIC-284, EID-1112, GMW-950, …) */
a.linlink{color:var(--amber);text-decoration:none;border-bottom:1px dotted color-mix(in srgb,var(--amber) 55%,transparent);font-weight:600}
a.linlink:hover{color:var(--amber);border-bottom-style:solid;filter:brightness(1.08)}
.tile.chat .sum a.linlink,.tile.chat .nm a.linlink,.tile.chat .peek a.linlink{font-weight:600}
.bubble a.linlink,.rail a.linlink,.fev a.linlink{font-weight:600}
/* Chat bubble after a task id — pursue that Linear issue */
.linwrap{display:inline;white-space:nowrap}
button.linchat{
  display:inline-flex;align-items:center;justify-content:center;
  margin:0 1px 0 3px;padding:0 4px;min-width:1.35em;height:1.35em;
  border:1px solid color-mix(in srgb, var(--amber) 45%, var(--line));
  border-radius:999px;background:color-mix(in srgb, var(--amber) 12%, transparent);
  color:var(--amber);font-size:11px;line-height:1;cursor:pointer;
  vertical-align:middle;font-family:inherit;
}
button.linchat:hover{background:color-mix(in srgb, var(--amber) 28%, transparent);border-color:var(--amber)}
button.linchat:disabled{opacity:.55;cursor:default}
.tile.chat .meta{font-size:10px;color:var(--text-dim);padding:0 10px 8px;display:flex;gap:10px;flex-wrap:wrap}
.tile.chat .resume{font-size:10px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;padding:0 10px 8px;color:var(--amber);word-break:break-all;opacity:.85}
.tile.chat .peek{margin:0 10px 10px;padding:8px;background:color-mix(in srgb, var(--bg) 70%, #000);border:1px solid var(--line);border-radius:4px;max-height:180px;overflow:auto;font-size:11px;line-height:1.4}
.tile.chat .peek .turn{margin-bottom:6px}
.tile.chat .peek .role{font-size:9px;letter-spacing:1px;text-transform:uppercase;color:var(--text-dim);margin-bottom:2px}
.tile.chat .peek .role.user{color:var(--amber)}
.tile.chat .actions{display:flex;gap:6px;padding:0 10px 10px;flex-wrap:wrap}
.tile.chat .actions .act{font-size:10px}
#chbar{display:flex;flex-direction:column;gap:8px;padding:10px 14px 6px;border-bottom:1px solid var(--line)}
#chbar .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
#chbar .hchip{cursor:pointer}
#chbar .hint{font-size:11px;color:var(--text-dim);margin-left:auto}
#chbar #chq{flex:1;min-width:140px;max-width:280px;background:var(--panel);border:1px solid var(--line);color:var(--text);padding:5px 8px;font-size:12px;border-radius:4px;font-family:inherit}
#chbar #chq:focus{outline:none;border-color:var(--amber-dim)}
#chbar .stat{font-size:11px;color:var(--text-dim);letter-spacing:.3px}
#chbar .stat b{color:var(--amber);font-weight:600}
/* ---- CALENDAR (Google-like week/month for cron message jobs) ---- */
#calroot{display:flex;flex-direction:column;height:100%;min-height:0;overflow:hidden}
#calbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line)}
#calbar .act{font-size:11px}
#calbar h2{margin:0;font-size:15px;font-weight:600;letter-spacing:.3px;flex:1;min-width:140px}
#calbody{display:flex;flex:1;min-height:0;overflow:hidden}
#calseries{width:240px;flex:none;border-right:1px solid var(--line);overflow:auto;padding:10px 8px;background:var(--bg-raise)}
#calseries h3{margin:0 0 8px;font-size:10px;letter-spacing:1px;color:var(--text-dim);text-transform:uppercase}
.calser{display:flex;align-items:flex-start;gap:8px;padding:8px;border-radius:6px;cursor:pointer;font-size:12px;margin-bottom:4px}
.calser:hover{background:var(--bg-card)}
.calser .dot{width:10px;height:10px;border-radius:3px;flex:none;margin-top:3px}
.calser .calser-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:2px}
.calser .calser-title{font-size:12px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text)}
.calser .calser-when{font-size:10px;line-height:1.3;color:var(--text-dim);white-space:normal}
.calser .calser-cron{font-size:9px;color:var(--text-dim);opacity:.55;font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.calser.off{opacity:.45}
.cal-skel{border-radius:4px;background:linear-gradient(90deg,var(--bg-card) 0%,var(--line) 50%,var(--bg-card) 100%);background-size:200% 100%;animation:calshimmer 1.1s ease-in-out infinite}
@keyframes calshimmer{0%{background-position:100% 0}100%{background-position:-100% 0}}
.cal-skel-ser{height:42px;margin-bottom:6px}
.cal-skel-cell{height:14px;margin:4px 2px;opacity:.55}
#calmain{flex:1;min-width:0;overflow:auto;padding:8px}
#calweek{display:grid;grid-template-columns:56px repeat(7,1fr);gap:1px;background:var(--line);border:1px solid var(--line);min-height:520px}
#calweek .hd{background:var(--bg-raise);padding:8px 4px;text-align:center;font-size:11px;font-weight:600;letter-spacing:.4px}
#calweek .hd.today{color:var(--amber)}
#calweek .gutter{background:var(--bg-raise);font-size:10px;color:var(--text-dim);text-align:right;padding:2px 6px;font-variant-numeric:tabular-nums}
#calweek .cell{background:var(--bg);position:relative;min-height:64px;padding:2px}
#calweek .cell.today{background:rgba(255,176,0,.06)}
#calmonth{display:grid;grid-template-columns:repeat(7,1fr);gap:1px;background:var(--line);border:1px solid var(--line)}
#calmonth .mhd{background:var(--bg-raise);padding:8px;text-align:center;font-size:11px;font-weight:600}
#calmonth .mcell{background:var(--bg);min-height:96px;padding:4px 6px;vertical-align:top}
#calmonth .mcell.out{opacity:.4}
#calmonth .mcell.today{outline:1px solid var(--amber);outline-offset:-1px}
#calmonth .mdn{font-size:11px;font-weight:700;margin-bottom:4px;color:var(--text-dim)}
.calev{display:block;font-size:10px;line-height:1.25;padding:2px 5px;margin:0 0 2px;border-radius:3px;color:#0e0e0e;cursor:pointer;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-left:3px solid rgba(0,0,0,.25)}
.calev:hover{filter:brightness(1.08)}
#caldrawer{position:fixed;top:0;right:0;width:min(380px,100vw);height:100%;background:var(--bg-raise);border-left:1px solid var(--line);z-index:80;display:none;flex-direction:column;box-shadow:-8px 0 24px rgba(0,0,0,.25)}
#caldrawer.open{display:flex}
#caldrawer .dh{display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid var(--line)}
#caldrawer .dh b{flex:1;font-size:14px}
#caldrawer .db{flex:1;overflow:auto;padding:14px;display:flex;flex-direction:column;gap:10px;font-size:12px}
#caldrawer label{display:flex;flex-direction:column;gap:4px;color:var(--text-dim);font-size:10px;letter-spacing:.6px;text-transform:uppercase}
#caldrawer input,#caldrawer textarea,#caldrawer select{background:var(--bg);border:1px solid var(--line);color:var(--text);padding:8px;border-radius:4px;font:inherit;font-size:12px;text-transform:none;letter-spacing:0}
#caldrawer textarea{min-height:100px;resize:vertical}
#caldrawer .df{display:flex;flex-wrap:wrap;gap:8px;padding:12px 14px;border-top:1px solid var(--line)}
#caldrawer .df .act.danger{border-color:#c0392b;color:#c0392b}
@media(max-width:760px){
  #calseries{display:none}
  #calweek{grid-template-columns:40px repeat(7,minmax(48px,1fr));min-height:400px}
}
.oattach{margin-left:auto;flex-shrink:0}
.owhy{font-size:9px;color:var(--text-dim);padding:4px 10px 8px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#newbody .chk{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--amber-dim);margin-top:8px}
#newbody .chk input{width:auto}
#newsummary{margin-right:auto;font-size:11px;color:var(--text-dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:75%}
#newsuggest{background:transparent;border:1px solid var(--amber-faint);color:var(--amber-dim);
  font-family:inherit;font-size:12px;cursor:pointer;padding:0 14px;white-space:nowrap}
#newsuggest:hover{color:var(--amber);border-color:var(--amber-dim)}
#newsuggest:disabled{opacity:.5;cursor:default}
#newwhy{font-size:11px;color:var(--amber-dim);font-style:italic;min-height:0}
#newwhy:empty{display:none}
#newerr{color:#ff5f56;font-size:11px}
#newerr:empty{display:none}
#newfoot{padding:12px 16px;border-top:1px solid var(--line);display:flex;justify-content:flex-end}
#newcreate{font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:5px 28px;
  background:var(--amber);border:none;color:var(--on-accent);cursor:pointer;font-weight:700}
#newcreate:hover{box-shadow:0 0 18px color-mix(in srgb, var(--amber) 50%, transparent)}
#newcreate:disabled{opacity:.5;cursor:default;box-shadow:none}
#modaliterm{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:13px;cursor:pointer;padding:2px 10px;margin-left:6px}
#modaliterm:hover{color:var(--amber);border-color:var(--amber-dim)}
#modaliterm:disabled{opacity:.6;cursor:default}
/* Body row: terminal head + right Linear tasks drawer */
#modalbody{
  flex:1 1 0;min-height:160px;min-width:0;display:flex;flex-direction:row;
  overflow:hidden;border-top:1px solid var(--line);
}
/* Shell owns the flex slot; screen is absolute-fill so height is always the
   free region between header/gist and composer — never content-sized, never 0. */
#modalshell{
  flex:1 1 0;min-height:0;min-width:0;position:relative;
  overflow:hidden;background:var(--bg-card);
}
/* Right drawer — every Linear TEAM-123 mentioned in this chat */
#modaltasks{
  flex:0 0 240px;width:240px;max-width:42vw;min-width:0;
  display:flex;flex-direction:column;
  border-left:1px solid var(--line);background:var(--bg-raise);
  overflow:hidden;
}
#modaltasks.collapsed{display:none}
#modaltasks .mthead{
  display:flex;align-items:center;gap:8px;padding:8px 10px;
  border-bottom:1px solid var(--line);flex:0 0 auto;
  font-size:10px;letter-spacing:1.2px;text-transform:uppercase;color:var(--text-dim);
}
#modaltasks .mthead b{color:var(--amber);font-weight:700;letter-spacing:.8px}
#modaltasks .mthead .mtcount{
  font-size:10px;padding:0 6px;border-radius:8px;
  background:color-mix(in srgb, var(--amber) 18%, transparent);color:var(--amber);
}
#modaltasks .mthead button{
  margin-left:auto;background:transparent;border:1px solid var(--line);
  color:var(--text-dim);font-size:12px;cursor:pointer;padding:1px 8px;
}
#modaltasks .mthead button:hover{color:var(--amber);border-color:var(--amber-dim)}
#modaltasklist{flex:1 1 0;min-height:0;overflow:auto;padding:8px}
#modaltasklist .mtempty{font-size:11px;color:var(--text-dim);line-height:1.45;padding:6px 4px}
#modaltasklist .mtask{
  display:flex;align-items:center;gap:8px;padding:8px 9px;margin-bottom:5px;
  border:1px solid var(--line);border-radius:6px;background:var(--bg-card);
  text-decoration:none;color:var(--text);font-size:12px;
}
#modaltasklist .mtask:hover{border-color:var(--amber-dim);background:color-mix(in srgb, var(--amber) 8%, var(--bg-card))}
#modaltasklist .mtkey{font-weight:700;color:var(--amber);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.3px;text-decoration:none}
#modaltasklist .mtgo{color:var(--text-dim);font-size:11px;text-decoration:none}
#modaltasksbtn{background:transparent;border:1px solid var(--line);color:var(--amber-dim);font-size:11px;cursor:pointer;padding:2px 8px;margin-left:6px;letter-spacing:.4px}
#modaltasksbtn:hover,#modaltasksbtn.on{color:var(--amber);border-color:var(--amber-dim)}
#modaltasksbtn .mtbadge{display:none;margin-left:4px;font-size:9px;padding:0 5px;border-radius:8px;background:var(--amber);color:var(--on-accent);font-weight:700}
#modaltasksbtn .mtbadge.on{display:inline}
@media(max-width:720px){
  #modaltasks{position:absolute;right:0;top:0;bottom:0;z-index:20;width:min(280px,88vw);max-width:none;
    box-shadow:-8px 0 24px rgba(0,0,0,.35)}
  #modalbody{position:relative}
}
#modalscreen{
  position:absolute;inset:0;
  overflow-x:auto;overflow-y:scroll; /* always show vertical rail */
  overscroll-behavior:contain;
  -webkit-overflow-scrolling:touch;
  touch-action:pan-x pan-y;
  font:12.5px/1.45 "IBM Plex Mono",ui-monospace,monospace;color:var(--text);
  white-space:pre-wrap;word-break:break-word;padding:8px 12px 36px;background:var(--bg-card);
  outline:none;
}
#modalscreen:focus{box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--amber) 35%, transparent)}
#modaljump{
  display:none;position:absolute;left:50%;transform:translateX(-50%);
  bottom:12px;z-index:12;padding:5px 14px;border-radius:14px;
  background:var(--amber);color:var(--on-accent);border:none;
  font:11px/1 "IBM Plex Mono",ui-monospace,monospace;letter-spacing:.6px;
  cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.35);
}
#modaljump.on{display:block}
#modaljump:hover{filter:brightness(1.08)}
/* live classifier strip (emux judge) */
#modaljudge{display:none;align-items:center;gap:10px;padding:4px 10px;flex:0 0 auto;
  border-bottom:1px solid var(--line);background:var(--bg-raise);font:11px "IBM Plex Mono",monospace}
#modaljudge.on{display:flex}
#modaljudge .jstate{font-weight:700;letter-spacing:1px;text-transform:uppercase;font-size:12px;white-space:nowrap}
#modaljudge .jconf{color:var(--text-dim);font-size:10px;white-space:nowrap}
#modaljudge .jsum{color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modaljudge .jflag{border:1px solid #7a3a1a;color:#ff9f43;padding:0 5px;font-size:10px;
  text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.s-running{color:#8fd88f}.s-done_idle{color:#8a8a72}.s-error{color:#ff5f56}
.s-thrashing{color:#ff9f43}.s-stuck{color:#ffb000}.s-waiting_human{color:#38d9ff}
.s-waiting_external{color:#7a8fd8}.s-planning{color:#d0b24a}.s-editing{color:#d0b24a}.s-dead{color:#666}
/* thinking indicator — movement + a live timer while the agent is generating */
#modalthink{display:none;align-items:center;gap:7px;margin-left:14px;font-size:11px;
  color:var(--live);letter-spacing:.5px}
#modalthink.on{display:inline-flex}
#modalthink b{color:var(--text);font-variant-numeric:tabular-nums}
.tdots{display:inline-flex;gap:3px}
.tdots i{width:4px;height:4px;border-radius:50%;background:var(--live);display:block;
  animation:tbounce 1s ease-in-out infinite}
.tdots i:nth-child(2){animation-delay:.16s}
.tdots i:nth-child(3){animation-delay:.32s}
@keyframes tbounce{0%,80%,100%{transform:translateY(0);opacity:.4}40%{transform:translateY(-4px);opacity:1}}
/* pending send — a message received by emux but not yet reflected in the chat */
#modalpending{display:none}
#modalpending.on{display:block;margin:0 16px 8px;padding:7px 11px;font-size:12px;
  background:var(--bg-card);border:1px dashed var(--amber-faint);border-radius:6px;color:var(--text-dim)}
#modalpending .plabel{color:var(--amber-dim);font-size:9px;letter-spacing:1.5px;text-transform:uppercase;margin-right:7px}
#modalpending .ptext{color:var(--text)}
#modalclips{display:none;flex-wrap:wrap;gap:8px;padding:0 16px 8px;align-items:center}
#modalclips.on{display:flex}
#modalclips .clip{
  display:flex;align-items:center;gap:8px;padding:4px 8px 4px 4px;
  border:1px solid var(--line);border-radius:6px;background:var(--bg-card);
  font-size:11px;color:var(--text-dim);max-width:100%;
}
#modalclips .clip img{width:40px;height:40px;object-fit:cover;border-radius:4px;background:#000}
#modalclips .clip .cp{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--amber);
  max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#modalclips .clip .rm{border:0;background:transparent;color:var(--text-dim);cursor:pointer;font-size:14px;padding:0 4px}
#modalclips .clip .rm:hover{color:var(--stale)}
#modalclips .cliphint{font-size:10px;color:var(--text-dim);letter-spacing:.3px}
/* the gist — reader's-digest + suggested replies, so you know what to do */
#modaldigest{display:none;flex:0 0 auto;max-height:22vh;overflow:hidden}
#modaldigest.on{display:block;padding:6px 10px;background:var(--bg-raise);
  max-height:22vh;overflow-y:auto; /* never starve #modalshell of height */
  border-bottom:1px solid var(--amber-faint)}
#modaldigest .dghead{display:flex;align-items:center;font-size:10px;letter-spacing:2px;
  text-transform:uppercase;color:var(--amber-dim);margin-bottom:3px}
#dgrefresh{margin-left:auto;background:transparent;border:none;color:var(--text-dim);
  font-size:13px;cursor:pointer}
#dgrefresh:hover{color:var(--amber)}
#modaldigest .dgtext{font-size:13px;line-height:1.5;color:var(--text)}
#modaldigest.loading .dgtext{color:var(--text-dim);font-style:italic}
#modaldigest .dgsugg{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
#modaldigest .dgsugg:empty{display:none}
.sgg{background:var(--amber);border:1.5px solid var(--amber);color:var(--on-accent);font-weight:650;
  font-family:inherit;font-size:12.5px;padding:7px 13px;border-radius:15px;cursor:pointer;
  transition:filter .12s,transform .08s;text-align:left;box-shadow:0 2px 6px rgba(0,0,0,.22)}
.sgg:hover{filter:brightness(1.12);transform:translateY(-1px)}
.sgg:active{transform:translateY(0)}
.sgg:disabled{opacity:.45;cursor:default;box-shadow:none}
/* clickable answer bubbles — overlay the chat when the agent shows a menu */
#modalopts{display:none}
#modalopts.on{display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 2px;
  border-top:1px solid var(--amber-faint);background:var(--bg-raise);flex:0 0 auto}
#modalopts .ohint{flex-basis:100%;font-size:10px;letter-spacing:1px;text-transform:uppercase;
  color:var(--amber-dim);margin-bottom:2px}
.obub{background:var(--amber);border:1.5px solid var(--amber);color:var(--on-accent);font-weight:650;
  font-family:inherit;font-size:13px;padding:8px 14px;border-radius:16px;cursor:pointer;
  transition:filter .12s,transform .08s;max-width:100%;text-align:left;box-shadow:0 2px 6px rgba(0,0,0,.22)}
.obub:hover{filter:brightness(1.12);transform:translateY(-1px)}
.obub:active{transform:translateY(0)}
.obub b{color:var(--on-accent);opacity:.75;margin-right:6px;font-weight:800}
.obub.sel{box-shadow:0 0 0 3px var(--bg),0 0 0 5px var(--amber)}
.obub:disabled{opacity:.45;cursor:default;box-shadow:none}
.obub:disabled{opacity:.5;cursor:default}
#modalchips{display:flex;gap:6px;padding:6px 10px 0;flex:0 0 auto}
#modalclips{padding:0 10px 4px}
#modalpending.on{margin:0 10px 4px;padding:5px 8px}
#modalrow{display:flex;gap:8px;padding:6px 10px 8px;flex:0 0 auto}
#modalinput{flex:1;background:var(--bg-card);border:1px solid var(--line);color:var(--text);
  font:14px "IBM Plex Mono",monospace;padding:8px 10px;outline:none;caret-color:var(--amber)}
#modalinput:focus{border-color:var(--amber-dim);box-shadow:0 0 12px color-mix(in srgb, var(--amber) 10%, transparent)}
#modalsend{font-family:"VT323",monospace;font-size:20px;letter-spacing:2px;padding:0 20px;
  background:var(--amber);color:var(--on-accent);border:none;cursor:pointer}
#modalsend:hover{box-shadow:0 0 18px color-mix(in srgb, var(--amber) 50%, transparent)}
::-webkit-scrollbar{width:8px}
::-webkit-scrollbar-thumb{background:var(--amber-faint)}
::-webkit-scrollbar-track{background:transparent}
/* --- Hancock approvals: browser-tab strip (in-flow, never covers the nav) --- */
#hbtn.hot{border-color:var(--amber);color:var(--amber);font-weight:700}
#hbadge{background:#c0392b;color:#fff;border-radius:9px;padding:0 6px;margin-left:5px;font-size:11px;font-weight:700}
#happrovals{border-bottom:1px solid var(--line);background:var(--bg-raise)}
#htabs{display:flex;gap:2px;padding:6px 8px 0;overflow-x:auto}
.htab{display:flex;align-items:center;gap:6px;padding:6px 10px;font-size:11px;cursor:pointer;
  color:var(--text-dim);background:var(--bg-card);border:1px solid var(--line);border-bottom:none;
  border-radius:7px 7px 0 0;white-space:nowrap;position:relative;top:1px}
.htab:hover{color:var(--text)}
.htab.active{color:var(--text);background:var(--bg-raise);border-color:var(--amber);font-weight:700}
.htab .hdot{width:7px;height:7px;border-radius:50%;background:var(--amber)}
.htab.r-high .hdot,.htab.r-critical .hdot{background:#c0392b}
.htab.r-low .hdot{background:var(--live)}
.htab .hcount{background:var(--stale);color:#fff;border-radius:8px;padding:0 5px;font-size:9px;font-weight:700}
.htab .hx{opacity:.5;font-size:11px;margin-left:2px}
.htab .hx:hover{opacity:1;color:#c0392b}
#hdetail{padding:12px 14px}
.hask{font-size:14px;color:var(--text);font-weight:600;margin-bottom:8px}
.hprov{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-bottom:6px}
.hchip2{font-size:11px;color:var(--amber-dim);border:1px solid var(--line);border-radius:9px;padding:2px 8px}
.harrow{color:var(--text-dim)}
.hmeta{font-size:11px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.hmeta.stale{color:#e05545;font-weight:700}
.hcmd2{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--text-dim);opacity:.75;margin-bottom:10px;word-break:break-all}
.hact2{display:flex;gap:8px}
.hpeek{padding:7px 12px;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px;
  background:var(--bg-raise);color:var(--text);border:1px solid var(--text-dim)}
.hpeek:hover{border-color:var(--amber);color:var(--amber)}
#htoast{position:fixed;top:12px;left:50%;transform:translateX(-50%) translateY(-20px);
  z-index:200;opacity:0;pointer-events:none;border-radius:8px;font-size:12px;font-weight:600;
  padding:9px 16px;transition:opacity .2s,transform .2s}
#htoast.show{opacity:1;transform:translateX(-50%) translateY(0)}
#htoast.ok{background:#1f3d2a;color:#7ee2a8;border:1px solid #2ea043}
#htoast.err{background:#3d1f1f;color:#f2a099;border:1px solid #c0392b}
.happrove,.hdeny{padding:7px 12px;border-radius:6px;cursor:pointer;font-weight:600;font-size:12px;border:1px solid}
.happrove{background:var(--amber);color:var(--on-accent);border-color:var(--amber)}
.hdeny{background:var(--bg-raise);color:var(--text);border-color:var(--text-dim);font-weight:600}
.hdeny:hover{background:#c0392b;color:#fff;border-color:#c0392b}
/* --- settings modal --- */
#setmodal{display:none;position:fixed;inset:0;z-index:140;background:rgba(0,0,0,.45);
  align-items:center;justify-content:center}
#setmodal.open{display:flex}
#setcard{background:var(--bg-raise);border:1px solid var(--line);border-radius:12px;width:560px;max-width:94vw;
  max-height:88vh;overflow:auto;padding:0 0 16px;box-shadow:0 12px 40px rgba(0,0,0,.4)}
#sethead{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;
  border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg-raise)}
#sethead b{color:var(--amber);letter-spacing:.5px}
.setx{cursor:pointer;color:var(--text-dim);font-size:16px}
.setnote{font-size:12px;color:var(--text-dim);padding:12px 18px 4px;line-height:1.5}
.setnote code{background:var(--bg-card);padding:1px 5px;border-radius:4px}
.setsec{padding:10px 18px}
.setsec h4{margin:6px 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:1px;color:var(--text)}
.setpill{font-size:10px;background:var(--amber-faint);color:var(--amber-dim);padding:2px 7px;border-radius:8px;
  text-transform:none;letter-spacing:0;margin-left:6px;font-weight:600}
.setroute{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:7px 0;
  border-bottom:1px solid var(--line);font-size:13px;color:var(--text)}
.setroute span{flex:1}
.setroute select,#setcard input{background:var(--bg-card);color:var(--text);border:1px solid var(--line);
  border-radius:6px;padding:6px 9px;font-family:inherit;font-size:12.5px}
#setcard label{display:block;font-size:12px;color:var(--text-dim);margin:8px 0}
#setcard label input{display:block;width:100%;margin-top:4px;box-sizing:border-box}
.setdim{opacity:.7}
.setrow{display:flex;align-items:center;gap:10px;margin-top:10px}
.setfoot{display:flex;align-items:center;gap:12px;padding:12px 18px 0}
#setsave,#nimtest{background:var(--amber);color:var(--on-accent);border:none;font-weight:650;
  padding:8px 18px;border-radius:7px;cursor:pointer;font-size:13px}
#nimtest{background:var(--bg-card);color:var(--text);border:1px solid var(--amber)}
#nimtestout.ok,#setsaveout.ok{color:#2ea043;font-size:12px}
#nimtestout.err,#setsaveout.err{color:#e05545;font-size:12px}
/* --- multi side-chats (reply + as many topic/task chats as you want) --- */
#tchatstack{
  position:fixed;right:14px;bottom:14px;z-index:220;
  display:none;flex-direction:row-reverse;align-items:flex-end;gap:10px;
  max-width:calc(100vw - 28px);overflow-x:auto;overflow-y:visible;
  padding:4px;pointer-events:none;
}
#modal.open #tchatstack{display:flex}
#tchatstack .tchat{
  pointer-events:auto;width:320px;max-width:min(320px,88vw);flex:0 0 auto;
  position:relative;
}
#tchatstack .tchat.collapsed{width:auto}
#tchatstack .tchat.collapsed .tchatpanel{display:none}
#tchatstack .tchat:not(.collapsed) .tchatlauncher{display:none}
#tchatstack .tchatlauncher{width:52px;height:52px;border-radius:50%;background:var(--amber);color:var(--on-accent);
  border:none;font-size:20px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.4);position:relative}
#tchatstack .tchatbadge{position:absolute;top:-3px;right:-3px;background:#c0392b;color:#fff;font-size:11px;
  min-width:19px;height:19px;border-radius:10px;display:none;align-items:center;justify-content:center;
  padding:0 4px;font-weight:800;box-shadow:0 1px 4px rgba(0,0,0,.35)}
#tchatstack .tchatpanel{background:var(--bg-raise);border:1px solid var(--amber);border-radius:14px;overflow:hidden;
  box-shadow:0 12px 44px rgba(0,0,0,.5);display:flex;flex-direction:column;max-height:64vh;width:320px;max-width:min(320px,88vw)}
#tchatstack .tchathead{background:var(--amber);color:var(--on-accent);padding:8px 10px;font-size:12px;font-weight:650;
  display:flex;justify-content:space-between;align-items:center;gap:6px}
#tchatstack .tchathead .th-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#tchatstack .tchathead b{font-weight:800}
#tchatstack .tchathead .th-acts{display:flex;gap:2px;flex:0 0 auto}
#tchatstack .tchathead button{background:transparent;border:none;color:var(--on-accent);font-size:14px;cursor:pointer;padding:0 5px;opacity:.9}
#tchatstack .tchathead button:hover{opacity:1}
#tchatstack .tchatlog{padding:9px 11px 0;display:flex;flex-direction:column;gap:6px;overflow:auto;max-height:22vh}
#tchatstack .tchatlog:empty{display:none}
.tcmsg{max-width:86%;padding:6px 10px;border-radius:11px;font-size:12px;line-height:1.35;white-space:pre-wrap;word-break:break-word}
.tcmsg.bot{align-self:flex-start;background:var(--bg-card);color:var(--text-dim);border:1px solid var(--line)}
.tcmsg.you{align-self:flex-end;background:var(--amber);color:var(--on-accent);font-weight:600}
#tchatstack .tchatchips{padding:11px;display:flex;flex-direction:column;gap:7px;overflow:auto;max-height:18vh}
.tc-opt,.tc-sug{border:none;border-radius:12px;padding:9px 13px;font-family:inherit;font-size:12.5px;
  font-weight:600;cursor:pointer;text-align:left;box-shadow:0 2px 5px rgba(0,0,0,.18);transition:filter .12s;
  display:flex;align-items:center;gap:8px}
.tc-opt{background:var(--amber);color:var(--on-accent)}
.tc-opt b{opacity:.8;font-weight:800;margin-right:6px}
.tc-sug{background:var(--bg-card);color:var(--text);border:1.5px solid var(--amber)}
.tc-txt{flex:1}
.cpie{flex:0 0 auto;width:16px;height:16px;border-radius:50%;box-shadow:inset 0 0 0 1px rgba(0,0,0,.15)}
.cpct{flex:0 0 auto;font-size:11px;font-weight:800;color:#2ea043;min-width:30px}
.tc-opt:hover,.tc-sug:hover{filter:brightness(1.1)}
.tc-opt:disabled,.tc-sug:disabled{opacity:.5;cursor:default}
.tc-opt.sel{box-shadow:0 0 0 2px var(--bg-raise),0 0 0 4px var(--amber)}
.tc-empty{color:var(--text-dim);font-size:12px;font-style:italic;padding:4px 2px}
#tchatstack .tchatrow{display:flex;gap:6px;padding:10px 11px;border-top:1px solid var(--line);background:var(--bg-card);align-items:center}
#tchatstack .tchatinput{flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);border-radius:9px;
  padding:8px 11px;font-family:inherit;font-size:13px;min-width:0}
#tchatstack .tchatsend{background:var(--amber);color:var(--on-accent);border:none;border-radius:9px;width:40px;
  font-size:15px;cursor:pointer;flex:0 0 auto}
#tchatstack .tchatfoot{display:flex;gap:6px;padding:0 11px 10px;background:var(--bg-card);flex-wrap:wrap}
#tchatstack .tchatfoot .act{font-size:10px}
#tchat-addfab{
  pointer-events:auto;width:44px;height:44px;border-radius:50%;
  background:var(--bg-raise);color:var(--amber);border:1px solid var(--amber);
  font-size:22px;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(0,0,0,.35);
  flex:0 0 auto;align-self:flex-end;margin-bottom:4px;
}
#tchat-addfab:hover{filter:brightness(1.08)}
#modaltasklist a.mtask .mtacts{display:flex;gap:4px;margin-left:auto;align-items:center}
#modaltasklist a.mtask .mtchat{
  border:1px solid var(--amber-dim);background:transparent;color:var(--amber);
  font-size:10px;padding:2px 7px;border-radius:4px;cursor:pointer;font-family:inherit;
}
#modaltasklist a.mtask .mtchat:hover{background:color-mix(in srgb, var(--amber) 18%, transparent)}
#modaltasklist a.mtask .mtgo{margin-left:0}
.happrove:disabled,.hdeny:disabled{opacity:.4;cursor:default}

/* ── macOS-only controls (iTerm2 driven by AppleScript) — hidden anywhere the
   daemon host isn't a Mac, since `emux head` / the gui checkbox can't work there.
   data-os is stamped on <html> from the server's platform.system(). ── */
html:not([data-os="Darwin"]) .maconly{display:none !important}

/* ── the off-canvas nav drawer + feed backdrop (mobile only; inert on desktop) ── */
#navtoggle{display:none}
#scrim{display:none;position:fixed;inset:0;z-index:150;background:rgba(0,0,0,.45)}

/* ── responsive: below this the fixed 3-column control room would clip sideways
   (280px side + 300px feed leave the main nav no room, pushing it off-screen).
   Side + feed become on-demand overlays so the main column — and its nav — always
   fit. The laptop control-room layout above this width is deliberately untouched. ── */
@media (max-width:760px){
  #side{position:fixed;top:0;left:0;height:100%;z-index:160;width:82vw;max-width:300px;
    transform:translateX(-100%);transition:transform .2s ease;box-shadow:6px 0 26px rgba(0,0,0,.4)}
  body.nav-open #side{transform:none}
  body.nav-open #scrim{display:block}
  #feed{position:fixed;top:0;right:0;z-index:160;width:82vw;max-width:320px;
    box-shadow:-6px 0 26px rgba(0,0,0,.4)}
  #feed:not(.open){width:0;box-shadow:none}
  #navtoggle{display:inline-flex;align-items:center}
  #topbar{flex-wrap:wrap;row-gap:8px;padding:10px 14px}
  #tabs{margin-left:0;flex-wrap:wrap}
  #views{padding:12px}
  .tilegrid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<div id="scrim"></div>
<div id="costbanner" onclick="focusCost()">💸 <b id="costn">0</b> <span id="costword">session</span> hit a usage / cost limit — <u>review</u></div>
<div id="setmodal" onclick="if(event.target===this)closeSettings()">
  <div id="setcard">
    <div id="sethead"><b>⚙ MODEL ROUTING</b><span onclick="closeSettings()" class="setx">✕</span></div>
    <p class="setnote">Route cheap, high-volume tasks to a <b>self-hosted, fixed-cost</b> NIM to spare the Claude subscription. If NIM is unset or unreachable, emux falls back to <code>claude -p</code> automatically — nothing breaks. <b>Do not point this at a metered cloud API.</b></p>
    <div class="setsec"><h4>Per-task backend</h4><div id="setroutes"></div></div>
    <div class="setsec"><h4>NIM endpoint <span class="setpill">self-hosted / local only</span></h4>
      <label>Base URL <input id="nimurl" placeholder="http://localhost:8000/v1" autocomplete="off" spellcheck="false"></label>
      <label>Model <input id="nimmodel" placeholder="meta/llama-3.1-8b-instruct" autocomplete="off" spellcheck="false"></label>
      <label>API key <span class="setdim">(only if your local NIM needs one)</span> <input id="nimkey" type="password" placeholder="(usually blank for local)" autocomplete="off"></label>
      <div class="setrow"><button id="nimtest" class="act" onclick="testNim()">Test connection</button><span id="nimtestout"></span></div>
    </div>
    <div class="setfoot"><button id="setsave" onclick="saveSettings()">Save</button><span id="setsaveout"></span></div>
  </div>
</div>
<aside id="side">
  <div id="brand">
    <div class="brand-top">__LOGO_HTML__<span id="rolechip" class="rolechip" hidden></span></div>
    <small id="brandtag">__TAGLINE__</small>
    <div id="managed" hidden></div>
  </div>
  <input id="filter" placeholder="filter sessions…" autocomplete="off" spellcheck="false">
  <div id="tagbar"></div>
  <div id="sessions"></div>
  <footer id="footer">__PRODUCT_LINE__ · v__VERSION__</footer>
</aside>
<main id="main">
  <div id="topbar">
    <button id="navtoggle" class="act" title="sessions" aria-label="toggle sessions">☰</button>
    <span id="title">grid</span>
    <span id="status">connecting…</span>
    <button id="attachbtn" class="act" style="display:none">⧉ copy attach</button>
    <a id="docsbtn" class="act" href="__PUBLIC_PATH__/docs" title="documentation">◇ DOCS</a>
    <button id="newbtn" class="act">+ NEW SESSION</button>
    <button id="feedbtn" class="act" title="live fleet activity">◫ FEED</button>
    <button id="hbtn" class="act" title="Hancock approvals" onclick="openHancock()">⧉ HANCOCK<span id="hbadge" style="display:none">0</span></button>
    <button id="refreshbtn" class="act">↻ refresh</button>
    <button id="themebtn" class="act" type="button" title="toggle light/dark">☾ dark</button>
    <button id="setbtn" class="act" title="model routing settings" onclick="openSettings()">⚙ SETTINGS</button>
    <div id="tabs">
      <button class="tab" data-mode="grid">GRID</button>
      <button class="tab" data-mode="groups">GROUPS</button>
      <button class="tab" data-mode="activity">ACTIVITY</button>
      <button class="tab" data-mode="flow">FLOW</button>
      <button class="tab" data-mode="orphans">ORPHANS</button>
      <button class="tab" data-mode="chats">CHATS</button>
      <button class="tab" data-mode="calendar">CALENDAR</button>
    </div>
  </div>
  <div id="happrovals" style="display:none"><div id="htabs"></div><div id="hdetail"></div></div>
  <div id="htoast"></div>
  <div id="views"></div>
  <div id="head"></div>
  <button id="jump">↓ jump to bottom</button>
  <div id="composer" style="display:none">
    <div id="chips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
      <button class="chip" id="wrapchip">WRAP: ON</button>
    </div>
    <div id="row">
      <input id="input" placeholder="type into the session… (Enter sends)" autocomplete="off" spellcheck="false">
      <button id="send">SEND</button>
    </div>
  </div>
</main>
<aside id="feed" class="open">
  <div id="feedhead"><span>◫ fleet</span><span id="feedcount"></span>
    <button id="feedclose" title="hide">›</button></div>
  <div id="feedlist"></div>
</aside>
<div id="modal">
  <div id="modalback"></div>
  <div id="modalpanel">
    <div id="modalhead">
      <span class="dot live"></span>
      <span class="nm" id="modalname"></span>
      <span class="ag" id="modalagent"></span>
      <span id="modalthink"><span class="tdots"><i></i><i></i><i></i></span>thinking <b>0s</b></span>
      <span class="st" id="modalstatus">live</span>
      <button id="modalswitch" title="fail this session over to another Claude account (exits + relaunches + resumes)" onclick="switchPlan()">⇄ switch account</button>
      <button id="modaliterm" class="maconly" title="open a head on this session in iTerm2 (emux head / tmux attach)">⧉ OPEN HEAD</button>
      <button id="modalpop" type="button" title="open this session as its own browser tab">↗ tab</button>
      <button id="modaltasksbtn" type="button" title="Linear tasks mentioned in this chat" onclick="toggleModalTasks()">☰ TASKS<span class="mtbadge" id="modaltaskbadge">0</span></button>
      <button id="modalclose">✕ close</button>
    </div>
    <div id="modaljudge"></div>
    <div id="modaldigest">
      <div class="dghead"><span>◆ the gist</span><button id="dgrefresh" title="re-read">↻</button></div>
      <div class="dgtext"></div>
      <div class="dgsugg"></div>
    </div>
    <div id="modalbody">
      <div id="modalshell">
        <div id="modalscreen" tabindex="0" role="log" aria-label="session head"></div>
        <button type="button" id="modaljump" title="jump to live bottom">↓ live</button>
      </div>
      <aside id="modaltasks" aria-label="Linear tasks in this chat">
        <div class="mthead">
          <b>Linear</b> <span>tasks</span>
          <span class="mtcount" id="modaltaskcount">0</span>
          <button type="button" id="modaltasks-hide" title="hide tasks drawer" onclick="toggleModalTasks(false)">›</button>
        </div>
        <div id="modaltasklist"></div>
      </aside>
    </div>
    <!-- Multi side-chats: default "reply to session" + N topic/task panels -->
    <div id="tchatstack" aria-label="side chats"></div>
    <div id="modalopts"></div>
    <div id="modalchips">
      <button class="chip" data-keys="C-c">^C</button>
      <button class="chip" data-keys="Escape">ESC</button>
      <button class="chip" data-keys="Enter">⏎</button>
      <button class="chip" data-keys="PageUp" title="scroll terminal up (tmux copy-mode)">PgUp</button>
      <button class="chip" data-keys="PageDown" title="scroll terminal down">PgDn</button>
      <button class="chip" data-keys="Up">↑</button>
      <button class="chip" data-keys="Tab">TAB</button>
    </div>
    <div id="modalpending"></div>
    <div id="modalclips" title="images pasted in this box land on the fleet host"></div>
    <div id="modalrow">
      <input id="modalinput" placeholder="prompt / paste screenshot here… (Enter sends)" autocomplete="off" spellcheck="false">
      <button id="modalsend">SEND</button>
    </div>
  </div>
</div>

<div id="newmodal">
  <div id="newback"></div>
  <div id="newpanel">
    <div id="newhead">
      <span class="nm">+ new session</span>
      <span class="st" id="newstatus"></span>
      <button id="newclose">✕ close</button>
    </div>
    <div id="newbody">
      <div class="askrow">
        <input id="newintent" placeholder="say what you want to do…" autocomplete="off">
        <button id="newsuggest">✦ go</button>
      </div>

      <!-- THE ANSWER: one readable card. The tree below is the machinery, hidden. -->
      <div id="result">
        <div id="rverb"></div>
        <div id="rwhat"></div>
        <div id="rwhere"></div>
        <div id="rflags"></div>
        <details id="rpeek"><summary>what's in it</summary><pre id="peek2"></pre></details>
        <div id="rwhy"></div>
      </div>

      <div id="tree">
      <div class="step" id="s-host">
        <div class="steph"><span class="num">1</span><span class="lbl">machine</span>
          <span class="sub">everything below depends on this</span>
          <span class="why" id="why-host"></span></div>
        <div class="chips" id="hostchips"></div>
      </div>

      <div class="step locked" id="s-dir">
        <div class="steph"><span class="num">2</span><span class="lbl">what's on it</span>
          <span class="sub" id="dirsub">pick a machine first</span>
          <span class="why" id="why-dir"></span></div>
        <div class="lanes">
          <span class="lane" data-lane="resume">↺ resume something running <b id="nrun">0</b></span>
          <span class="lane" data-lane="new">+ start fresh in a directory <b id="ndir">0</b></span>
        </div>
        <input id="dirfilter" placeholder="filter…" autocomplete="off">
        <div class="dirchoices" id="dirchoices"></div>
        <div id="peekwrap">
          <div id="peekhead">what's in it right now <span id="peekflags"></span></div>
          <pre id="peek"></pre>
        </div>
      </div>

      <div class="step locked" id="s-cmd">
        <div class="steph"><span class="num">3</span><span class="lbl">what runs there</span>
          <span class="sub" id="cmdsub"></span></div>
        <div class="chips" id="cmdchips"></div>
        <input id="newcmd" placeholder="custom command… (empty = plain shell)" autocomplete="off">
      </div>

      <div class="step locked" id="s-name">
        <div class="steph"><span class="num">4</span><span class="lbl" id="namelbl">name it</span>
          <span class="sub" id="namesub"></span></div>
        <input id="newname" placeholder="session name" autocomplete="off">
        <label class="chk maconly"><input type="checkbox" id="newgui" checked>
          <span id="guilbl">open an iTerm2 window attached to it</span></label>
        <div id="guinote"></div>
      </div>
      </div><!-- /tree -->
      <div id="newerr"></div>
    </div>
    <div id="newfoot">
      <button id="newchange" class="linkish">change…</button>
      <button id="newcreate" disabled>CREATE SESSION</button>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
const SVGNS="http://www.w3.org/2000/svg";
let mode="grid", current=null, grid=[], chatTimer=null, gridTimer=null, screenEl=null;
let filterStr="", flashOn=false, activeTag="", activeHost="";
const metaCache={};   // last-live summary/agent per session name, so a GONE session still shows what it last was
function cacheMeta(){grid.forEach(s=>{if(s.live&&s.summary)metaCache[s.name]={summary:s.summary,agent:s.agent,state:s.state,ts:Date.now()};});}
function lastSummary(s){return s.summary||(metaCache[s.name]&&metaCache[s.name].summary)||"";}
function goneAge(s){const m=metaCache[s.name];if(!m)return"";const sec=Math.round((Date.now()-m.ts)/1000);return sec<60?sec+"s":Math.round(sec/60)+"m";}
let activeCompany=localStorage.getItem("emux_company")||"";   // restore the skin you were in
let flowSig=null, flowPre={}, flowBox={};   // flow view: rebuild only on topology change, else update panes in place
const BASE_TAB={grid:"GRID",groups:"GROUPS",activity:"ACTIVITY",flow:"FLOW",orphans:"ORPHANS",chats:"CHATS",calendar:"CALENDAR"};

// ---- deep links: the view, filters, and open session live in the URL, so any
// state is bookmarkable/shareable and survives reload/back-forward. ----
let urlBooting=false;   // suppress syncURL while we APPLY a url (avoid loops)
function syncURL(){
  if(urlBooting)return;
  const p=new URLSearchParams();
  if(mode&&mode!=="grid")p.set("view",mode);
  if(activeCompany)p.set("company",activeCompany);
  if(activeTag)p.set("tag",activeTag);
  if(filterStr)p.set("q",filterStr);
  if(modalSession)p.set("session",modalSession.name);
  if(mode==="chats"){
    if(CH.status&&CH.status!=="stale,recent")p.set("ch_status",CH.status);
    if(CH.tool)p.set("ch_tool",CH.tool);
    if(CH.match&&CH.match!=="greenmark")p.set("ch_match",CH.match);
    if(CH.q)p.set("ch_q",CH.q);
    if(CH.sort&&CH.sort!=="priority")p.set("ch_sort",CH.sort);
  }
  if(soloMode)p.set("solo","1");
  const h=p.toString();
  if((location.hash.slice(1))!==h)
    history.replaceState(null,"",h?("#"+h):location.pathname+location.search);
}
let soloMode=false;
function applyURL(){
  urlBooting=true;
  const p=new URLSearchParams(location.hash.slice(1));
  soloMode=p.get("solo")==="1";
  document.body.classList.toggle("solo-session",soloMode);
  activeCompany=p.get("company")||"";
  activeTag=p.get("tag")||"";
  filterStr=(p.get("q")||"").toLowerCase();
  const f=$("#filter");if(f)f.value=p.get("q")||"";
  skinForCompany();
  // solo tabs still need a grid load for session metadata; default view stays grid
  // view=chat is a legacy alias for live head mode (see docs/vocabulary.md)
  setMode(soloMode?"grid":(function(v){return (v==="chat")?"head":v;})(p.get("view")||localStorage.getItem("emux_view")||"grid"));
  renderTagbar();renderSidebar();
  urlBooting=false;
  // deep-link to an open session modal — once the grid is loaded
  const sess=p.get("session");
  if(sess){const open=()=>{const s=grid.find(x=>x.name===sess);
    if(s)openModal(s);else if(!grid.length)setTimeout(open,400);
    else if(soloMode)setTimeout(open,800);};open();}
  else if(!modalSession){/* leave modal closed */}
}
function popOutSessionTab(){
  if(!modalSession)return;
  const p=new URLSearchParams();
  p.set("session",modalSession.name);
  p.set("solo","1");
  const url=location.pathname+location.search+"#"+p.toString();
  window.open(url,"_blank","noopener,noreferrer");
}
window.addEventListener("hashchange",()=>{if(!urlBooting)applyURL();});

// PUBLIC_PATH is injected by the daemon when published under a reverse-proxy
// path prefix (e.g. /gmux). Empty string keeps loopback root behavior.
const PUBLIC_PATH="__PUBLIC_PATH__";
async function api(path,opts){const r=await fetch(PUBLIC_PATH+path,opts);return r.json();}

function ageLabel(a){
  if(a===null||a===undefined)return "—";
  if(a<2)return "now";
  if(a<60)return Math.round(a)+"s ago";
  if(a<3600)return Math.round(a/60)+"m ago";
  return Math.round(a/3600)+"h ago";
}
function ageClass(a){            // recency tiers (#17)
  if(a===null||a===undefined)return "t-old";
  if(a<6)return "t-now";
  if(a<120)return "t-min";
  return "t-old";
}
function uptime(created){        // session age from tmux created_unix (#16)
  if(!created)return "";
  const s=Math.max(0,Math.floor(Date.now()/1000)-created);
  if(s<3600)return "up "+Math.round(s/60)+"m";
  if(s<86400)return "up "+Math.round(s/3600)+"h";
  return "up "+Math.round(s/86400)+"d";
}
function hot(s){return s.last_change_age!==null&&s.last_change_age!==undefined&&s.last_change_age<6;}
// does a session directly match the active filter (name / tag / company)?
function baseMatch(s){
  // filter now searches CONTENT too — name, target, description, and the live/last
  // gist — so typing "cloudflare" finds the session whose gist mentions it.
  const hay=filterStr?((s.name||"")+" "+(s.session||"")+" "+(s.description||"")+" "
    +lastSummary(s)+" "+(s.path||s.cwd||"")+" "+(s.tags||[]).join(" ")).toLowerCase():"";
  return (!filterStr||hay.includes(filterStr))
  &&(!activeTag||(s.tags||[]).includes(activeTag))
  &&(!activeHost||(s.host||"local")===activeHost)
  &&(!activeCompany||(s.company||{}).company===activeCompany);}

// TERMINALS grouped into connected components by the `manages` edges (undirected).
// Returns name -> component id. A "group of terminals" is one component.
function components(){
  const parent={};grid.forEach(s=>parent[s.name]=s.name);
  const find=x=>{while(parent[x]!==x){parent[x]=parent[parent[x]];x=parent[x];}return x;};
  const uni=(a,b)=>{if(parent[a]!==undefined&&parent[b]!==undefined)parent[find(a)]=find(b);};
  const byKey={};grid.forEach(s=>{byKey[s.name]=s.name;if(!(s.session in byKey))byKey[s.session]=s.name;});
  grid.forEach(s=>(s.manages||[]).forEach(t=>{const tn=byKey[t];if(tn)uni(s.name,tn);}));
  const comp={};grid.forEach(s=>comp[s.name]=find(s.name));return comp;
}

// shown: the filter, but CONNECTION-AWARE. If any terminal in a group matches,
// the WHOLE group shows — even members that aren't in the filtered set — so a
// manager and everything it manages stay together on screen.
//
// Manager products default to QUIET scope: only standing health / manager-tagged
// sessions — not the whole laptop + rentamac fleet (that noise belongs in worker rooms).
let managerFleet=false;  // false = quiet (default for role=manager); restored after SKIN_ID
function isManagerQuiet(){
  return PRODUCT_INFO.role==="manager" && !managerFleet;
}
function isManagerScoped(s){
  const name=(s.name||"").toLowerCase();
  const sess=(s.session||"").toLowerCase();
  const tags=(s.tags||[]).map(t=>String(t).toLowerCase());
  const desc=(s.description||"").toLowerCase();
  const hc=PRODUCT_INFO.health_chat||{};
  const vp=PRODUCT_INFO.vp||{};
  if(hc.name&&name===String(hc.name).toLowerCase())return true;
  if(hc.session&&(name===String(hc.session).toLowerCase()||sess===String(hc.session).toLowerCase()))return true;
  if(vp.name&&name===String(vp.name).toLowerCase())return true;
  if(vp.session&&(name===String(vp.session).toLowerCase()||sess===String(vp.session).toLowerCase()))return true;
  if(name.includes("directrux"))return true;
  if(tags.some(t=>t==="manager"||t==="directrux"||t==="health"||t==="vp"))return true;
  if(desc.includes("managed_planes")||desc.includes("directrux manager")||desc.includes("directrux vp"))return true;
  return false;
}
function setManagerFleet(on){
  managerFleet=!!on;
  try{localStorage.setItem("emux_manager_fleet_"+SKIN_ID,managerFleet?"1":"0");}catch(e){}
  activeTag="";activeCompany="";activeHost="";
  renderTagbar();renderSidebar();if(mode!=="head")render();syncURL();
}
function shown(){
  let pool=grid.slice();
  if(isManagerQuiet())pool=pool.filter(isManagerScoped);
  if(!(filterStr||activeTag||activeCompany||activeHost))return pool;
  // connection-aware within the (possibly quiet) pool only
  const parent={};pool.forEach(s=>parent[s.name]=s.name);
  const find=x=>{while(parent[x]!==x){parent[x]=parent[parent[x]];x=parent[x];}return x;};
  const uni=(a,b)=>{if(parent[a]!==undefined&&parent[b]!==undefined)parent[find(a)]=find(b);};
  const byKey={};pool.forEach(s=>{byKey[s.name]=s.name;if(!(s.session in byKey))byKey[s.session]=s.name;});
  pool.forEach(s=>(s.manages||[]).forEach(t=>{const tn=byKey[t];if(tn)uni(s.name,tn);}));
  const comp={};pool.forEach(s=>comp[s.name]=find(s.name));
  const hot=new Set();
  pool.forEach(s=>{if(baseMatch(s))hot.add(comp[s.name]);});
  return pool.filter(s=>baseMatch(s)||hot.has(comp[s.name]));
}

// ---- Brand + light/dark mode ----
// Source of truth for product skins is server-stamped <style id="skin-theme">
// from skin.py (dynamic). JS must NOT hardcode brand hex and stomp it.
//
// Product skins (gmux / reevux / amux / directrux / any non-emux):
//   - company pills FILTER only — never recolor the room
//   - light/dark only flips data-theme; CSS vars come from skin-theme
// Bare emux: company pills may still rebrand via THEMES fallback packs.
const SKIN_ID="__SKIN_ID__";
const DEFAULT_MODE="__DEFAULT_THEME__";
const PRODUCT_SKIN=SKIN_ID!=="emux" && SKIN_ID!=="";
const BRAND_DEFAULT=SKIN_ID==="gmux"?"greenmark"
  :(SKIN_ID==="reevux"?"reeves"
  :(SKIN_ID==="amux"?"aic"
  :(SKIN_ID==="directrux"?"directrux"
  :"eidos")));
const MODE_KEY="emux_mode_"+SKIN_ID;
const BRAND_KEY="emux_brand_"+SKIN_ID;
try{managerFleet=localStorage.getItem("emux_manager_fleet_"+SKIN_ID)==="1";}catch(e){}
// Vars that applyTheme may set/clear. Keep in sync with skin.Palette.css_block.
const THEME_VARS=["--bg","--bg-raise","--bg-card","--amber","--amber-dim","--amber-faint",
  "--text","--text-dim","--live","--stale","--line","--user","--on-accent",
  "--on","--ink","--dim","--card","--pill"];
// Bare-emux company rebrand packs only (product skins ignore these).
const THEMES={
  eidos:{
    light:{"--bg":"#f0ebe4","--bg-raise":"#e9e3db","--bg-card":"#e4ded6",
      "--amber":"#8e6129","--amber-dim":"#a9853f","--amber-faint":"#d8cdba",
      "--text":"#1e1a17","--text-dim":"#6b6159","--live":"#4a6a3a","--stale":"#ab5036",
      "--line":"#cabfae","--user":"#8e6129","--on-accent":"#f5efe6"},
    dark:{"--bg":"#15110f","--bg-raise":"#1a1613","--bg-card":"#1e1a17",
      "--amber":"#c4935a","--amber-dim":"#9a6d35","--amber-faint":"#3a2f22",
      "--text":"#dcd5cb","--text-dim":"#8b8179","--live":"#7a8c72","--stale":"#c4694f",
      "--line":"#332a20","--user":"#d4a870","--on-accent":"#1a1207"},
  },
  greenmark:{
    light:{"--bg":"#f4f7f4","--bg-raise":"#e8f0ea","--bg-card":"#ffffff",
      "--amber":"#1b7a4e","--amber-dim":"#2d6a4f","--amber-faint":"#d4edda",
      "--text":"#14261c","--text-dim":"#4d6356","--live":"#1a7a45","--stale":"#a67c2d",
      "--line":"#c5d6cb","--user":"#203C31","--on-accent":"#f4f7f4"},
    dark:{"--bg":"#0c1611","--bg-raise":"#132019","--bg-card":"#1a2a21",
      "--amber":"#5fbf8f","--amber-dim":"#3d9a6e","--amber-faint":"#1e3d30",
      "--text":"#e8f0ea","--text-dim":"#8aa396","--live":"#5fbf8f","--stale":"#c9a227",
      "--line":"#2a4034","--user":"#5fbf8f","--on-accent":"#0c1611"},
  },
  reeves:{
    light:{"--bg":"#eef1f6","--bg-raise":"#e6ebf2","--bg-card":"#dee4ee",
      "--amber":"#3b5ba5","--amber-dim":"#5a76bd","--amber-faint":"#ccd6e8",
      "--text":"#182030","--text-dim":"#5c6678","--live":"#3d7a5a","--stale":"#b3503a",
      "--line":"#c5cddd","--user":"#3b5ba5","--on-accent":"#f4f7fc"},
    dark:{"--bg":"#0e1218","--bg-raise":"#151b24","--bg-card":"#1b2330",
      "--amber":"#7aa2ff","--amber-dim":"#5a76bd","--amber-faint":"#243044",
      "--text":"#e8eef8","--text-dim":"#8b96ab","--live":"#5fbf8f","--stale":"#e07050",
      "--line":"#2a3444","--user":"#7aa2ff","--on-accent":"#0e1218"},
  },
  aic:{
    light:{"--bg":"#f3f5fa","--bg-raise":"#e8ecf5","--bg-card":"#ffffff",
      "--amber":"#143ca2","--amber-dim":"#16438a","--amber-faint":"#d4dcf0",
      "--text":"#151c36","--text-dim":"#5a6580","--live":"#1a7a45","--stale":"#a64b32",
      "--line":"#c5cde0","--user":"#143ca2","--on-accent":"#ffffff"},
    dark:{"--bg":"#151c36","--bg-raise":"#182148","--bg-card":"#1c2747",
      "--amber":"#6b8fd4","--amber-dim":"#4a6fbf","--amber-faint":"#243056",
      "--text":"#f0f3fa","--text-dim":"#9aa6c0","--live":"#5fbf8f","--stale":"#e07050",
      "--line":"#2a3558","--user":"#e8eefc","--on-accent":"#151c36"},
  },
};
const CO_BRAND={"":BRAND_DEFAULT,"eidos":"eidos","greenmark":"greenmark","reeves":"reeves","aic":"aic"};
let uiMode=(DEFAULT_MODE==="dark")?"dark":"light";
let uiBrand=BRAND_DEFAULT;
function clearInlineThemeVars(r){
  for(const k of THEME_VARS) r.style.removeProperty(k);
}
function applyTheme(){
  const r=document.documentElement;
  r.setAttribute("data-theme",uiMode);
  r.dataset.brand=uiBrand;
  r.dataset.skin=SKIN_ID;
  if(PRODUCT_SKIN){
    // Dynamic: let <style id="skin-theme"> (from skin.py) own the colors.
    // Clear any prior inline stomps (eidos/etc.) so CSS wins.
    clearInlineThemeVars(r);
  } else {
    // Bare emux only: company pill may rebrand via hardcoded THEMES packs.
    const pack=THEMES[uiBrand]||THEMES[BRAND_DEFAULT]||THEMES.eidos;
    const t=pack[uiMode]||pack.light;
    for(const k in t) r.style.setProperty(k,t[k]);
    r.style.setProperty("--on",t["--amber"]);
    r.style.setProperty("--ink",t["--text"]);
    r.style.setProperty("--dim",t["--text-dim"]);
    r.style.setProperty("--card",t["--bg-card"]);
    r.style.setProperty("--pill",t["--amber-faint"]);
  }
  try{localStorage.setItem(MODE_KEY,uiMode);localStorage.setItem(BRAND_KEY,uiBrand);}catch(e){}
  const b=document.getElementById("themebtn");
  if(b){b.textContent=uiMode==="dark"?"☀ light":"☾ dark";b.title="switch to "+(uiMode==="dark"?"light":"dark")+" mode";}
}
function toggleMode(){uiMode=uiMode==="dark"?"light":"dark";applyTheme();}
function skinForCompany(){
  // Product skins: company is a filter only — chrome stays the active skin.
  if(PRODUCT_SKIN){ uiBrand=BRAND_DEFAULT; }
  else { uiBrand=CO_BRAND[activeCompany]||BRAND_DEFAULT; }
  applyTheme();
}
(function(){
  try{
    const m=localStorage.getItem(MODE_KEY);
    if(m==="light"||m==="dark") uiMode=m;
    else if(DEFAULT_MODE==="dark"||DEFAULT_MODE==="light") uiMode=DEFAULT_MODE;
    else if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches) uiMode="dark";
    if(!PRODUCT_SKIN){
      const b=localStorage.getItem(BRAND_KEY);
      if(b&&THEMES[b]) uiBrand=b;
    } else {
      uiBrand=BRAND_DEFAULT;
    }
  }catch(e){}
  applyTheme();
})();

function applyFilters(){localStorage.setItem("emux_company",activeCompany);
  skinForCompany();renderTagbar();renderSidebar();if(mode!=="head")render();syncURL();}

function renderTagbar(){
  const box=$("#tagbar");if(!box)return;
  // Manager quiet: one line, not 40 company/tag chips.
  if(PRODUCT_INFO.role==="manager"&&!managerFleet){
    const n=grid.length;
    const scoped=grid.filter(isManagerScoped).length;
    box.innerHTML='<span class="tagchip on" title="manager default">manager scope · '
      +scoped+' shown</span>'
      +'<span class="tagchip" id="fleetnoise" title="Show every session on this host (noisy)">show full fleet · '
      +n+'…</span>';
    const b=$("#fleetnoise");if(b)b.onclick=()=>setManagerFleet(true);
    return;
  }
  if(PRODUCT_INFO.role==="manager"&&managerFleet){
    // prepend hide chip; fall through to normal chips on full grid
  }
  // companies (colored, from cwd) then tags — both filter the whole view
  const comp=new Map();   // key -> {label,color,n}
  grid.forEach(s=>{const c=s.company||{};if(c.company){
    const e=comp.get(c.company)||{label:c.label,color:c.color,n:0};e.n++;comp.set(c.company,e);}});
  const counts=new Map();
  grid.forEach(s=>(s.tags||[]).forEach(t=>counts.set(t,(counts.get(t)||0)+1)));
  // machines are a facet too — every session runs SOMEWHERE
  const hosts=new Map();
  grid.forEach(s=>{const h=s.host||"local";hosts.set(h,(hosts.get(h)||0)+1);});
  if(!comp.size&&!counts.size&&!activeTag&&!activeCompany&&!activeHost
      &&!(PRODUCT_INFO.role==="manager"&&managerFleet)){box.innerHTML="";return;}
  let html="";
  if(PRODUCT_INFO.role==="manager"&&managerFleet){
    html+='<span class="tagchip on" id="fleetquiet" title="Back to manager-only sessions">✕ hide fleet noise</span>';
  }
  if(activeTag||activeCompany||activeHost)html+='<span class="tagchip clr" data-clear="1">✕ all</span>';
  if(hosts.size>1||activeHost)[...hosts.keys()].sort().forEach(h=>{
    html+='<span class="tagchip hostchip'+(h===activeHost?" on":"")+'" data-host="'+esc(h)+'">⌨ '+esc(h)
      +'<span class="cnt">'+hosts.get(h)+'</span></span>';
  });
  [...comp.keys()].sort().forEach(k=>{const e=comp.get(k);
    const on=k===activeCompany;
    html+='<span class="cochip'+(on?" on":"")+'" data-co="'+k+'" '
      +'style="'+(on?'background:'+e.color+';border-color:'+e.color:'color:'+e.color+';border-color:'+e.color)+'">'
      +e.label+'<span class="cnt">'+e.n+'</span></span>';
  });
  [...counts.keys()].sort().forEach(t=>{
    html+='<span class="tagchip'+(t===activeTag?" on":"")+'" data-tag="'+t+'">#'+t
      +'<span class="cnt">'+counts.get(t)+'</span></span>';
  });
  box.innerHTML=html;
  const fq=$("#fleetquiet");if(fq)fq.onclick=()=>setManagerFleet(false);
  box.querySelectorAll("[data-clear]").forEach(el=>el.onclick=()=>{activeTag="";activeCompany="";activeHost="";applyFilters();});
  box.querySelectorAll(".hostchip").forEach(el=>el.onclick=()=>{
    activeHost=el.dataset.host===activeHost?"":el.dataset.host;applyFilters();});
  box.querySelectorAll(".cochip").forEach(el=>el.onclick=()=>{
    activeCompany=el.dataset.co===activeCompany?"":el.dataset.co;applyFilters();});
  box.querySelectorAll(".tagchip[data-tag]").forEach(el=>el.onclick=()=>{
    activeTag=el.dataset.tag===activeTag?"":el.dataset.tag;applyFilters();});
}

function setMode(m){
  if(m==="chat")m="head";  // legacy alias for live head
  if(m!=="head"&&!BASE_TAB[m])m="grid";   // a renamed/removed view saved in localStorage → blank screen
  mode=m;current=(m==="head")?current:null;
  if(m!=="head")localStorage.setItem("emux_view",m);   // remember last view (#6)
  $("#head").style.display=(m==="head")?"flex":"none";
  $("#views").style.display=(m==="head")?"none":"";
  $("#composer").style.display=(m==="head")?"":"none";
  $("#attachbtn").style.display=(m==="head")?"":"none";
  document.querySelectorAll(".tab").forEach(t=>t.classList.toggle("on",t.dataset.mode===m));
  document.querySelectorAll(".card").forEach(el=>el.classList.toggle("active",!!(current&&el.dataset.name===current.name)));
  clearInterval(chatTimer);chatTimer=null;
  if(m!=="head"){$("#title").textContent=m;$("#views").innerHTML="";flowSig=null;render();}
  syncURL();
}
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>setMode(t.dataset.mode));

function updateChrome(){         // title, footer, tab counts (#1 #3 #4)
  const liveN=grid.filter(s=>s.live).length;
  const actN=grid.filter(s=>s.live&&hot(s)).length;
  const isMgr=PRODUCT_INFO.role==="manager";
  if(!flashOn){
    document.title=isMgr
      ?("__PRODUCT__ · manager · "+liveN+" live")
      :("__PRODUCT__ · "+liveN+" live");
  }
  let foot="__PRODUCT_LINE__ · v__VERSION__ · "+grid.length+" sessions";
  if(isMgr){
    const n=(PRODUCT_INFO.managed_planes||[]).length;
    const scoped=grid.filter(isManagerScoped).filter(s=>s.live).length;
    foot="__PRODUCT__ · manager · v__VERSION__ · "+n+" plane"+(n===1?"":"s")
      +(isManagerQuiet()
        ?(" · quiet · "+scoped+" scoped live")
        :(" · full fleet · "+liveN+" live"));
  }
  $("#footer").textContent=foot;
  document.querySelectorAll(".tab").forEach(t=>{
    const m=t.dataset.mode;
    if(m==="activity"&&actN)t.textContent=BASE_TAB[m]+" · "+actN;
    else if(m==="chats"&&CH.counts&&(CH.counts.stale||0)>0)
      t.textContent=BASE_TAB[m]+" · "+CH.counts.stale;
    else t.textContent=BASE_TAB[m];
  });
}

// ---- Product role chrome (manager vs worker) — driven by /api/product + /api/managed
// Permanent seats (vp/health/engine) must reflect LIVE inventory truth, not only
// product.json intent. "Always" is the policy; grid liveness is the fact.
const PRODUCT_INFO={role:null,managed_planes:[],health:{},loaded:false};
function permanentSeatStatus(name){
  // Match registered name or tmux session name against current grid poll.
  const n=(name||"").trim();
  if(!n) return {cls:"deg", txt:"unconfigured"};
  const s=(typeof grid!=="undefined"&&grid||[]).find(x=>x&&(x.name===n||x.session===n));
  if(!s) return {cls:"down", txt:"DOWN · missing — ensure should restart ≤60s"};
  if(s.live) return {cls:"up", txt:"UP · live · tmux attach -t "+n};
  return {cls:"down", txt:"DOWN · session gone — permanent seat broken"};
}
function renderProductChrome(){
  const chip=$("#rolechip"), tag=$("#brandtag"), box=$("#managed");
  if(!chip||!box)return;
  if(PRODUCT_INFO.role==="manager"){
    chip.hidden=false;
    chip.textContent="MANAGER";
    chip.className="rolechip manager";
    const planes=PRODUCT_INFO.managed_planes||[];
    const ids=planes.map(p=>p.id);
    if(tag) tag.textContent=ids.length
      ?("manages "+ids.join(" · "))
      :"manager · no planes in product.json";
    box.hidden=false;
    const hc=PRODUCT_INFO.health_chat||{};
    const vp=PRODUCT_INFO.vp||{};
    const eng=PRODUCT_INFO.engine_seat||{};
    const hname=hc.name||hc.session||"directrux-health";
    const vname=vp.name||vp.session||"directrux-vp";
    const ename=eng.name||eng.session||"";
    const vst=permanentSeatStatus(vname);
    const hst=permanentSeatStatus(hname);
    const est=ename?permanentSeatStatus(ename):null;
    // If any permanent seat is DOWN, manager chrome must scream — not look "always fine".
    let html='<div class="mhd">Permanent seats <span class="dim">(intent from product.json · truth from live inventory)</span></div>'
      +'<div class="mplane" data-seat="'+esc(vname)+'">'
      +'<span class="mid">'+esc(vname)+'</span>'
      +'<span class="mlane">vp seat</span>'
      +'<span class="mstat '+vst.cls+'">'+esc(vst.txt)+'</span>'
      +'</div>'
      +'<div class="mplane" data-seat="'+esc(hname)+'">'
      +'<span class="mid">'+esc(hname)+'</span>'
      +'<span class="mlane">health</span>'
      +'<span class="mstat '+hst.cls+'">'+esc(hst.txt)+'</span>'
      +'<a class="mopen" href="__PUBLIC_PATH__/health.html" target="_blank" rel="noopener">/health →</a>'
      +'<a class="mopen" href="__PUBLIC_PATH__/ai.html" target="_blank" rel="noopener">/ai →</a>'
      +'</div>';
    if(ename){
      html+='<div class="mplane" data-seat="'+esc(ename)+'">'
        +'<span class="mid">'+esc(ename)+'</span>'
        +'<span class="mlane">engine</span>'
        +'<span class="mstat '+est.cls+'">'+esc(est.txt)+'</span>'
        +'</div>';
    }
    if(vst.cls==="down"||hst.cls==="down"||(est&&est.cls==="down")){
      html+='<div class="mplane"><span class="mid">!</span>'
        +'<span class="mstat down">permanent seat DOWN is a product failure — run ensure-vp / ensure-health-chat / ensure-engine</span></div>';
    }
    html+='<div class="mhd">Managed planes</div>';
    if(!planes.length){
      html+='<div class="mplane"><span class="mid">—</span>'
        +'<span class="mstat down">empty allowlist · edit product.json</span></div>';
    }else{
      planes.forEach(p=>{
        const h=PRODUCT_INFO.health[p.id]||{};
        const ok=!!h.ok, deg=!!h.degraded;
        let stCls="down", stTxt="unreachable";
        if(ok&&!deg){stCls="up";stTxt="up"+(h.live_sessions!=null?(" · "+h.live_sessions+" live"):"");}
        else if(ok&&deg){stCls="deg";stTxt="degraded"+(h.error?(" · "+h.error):"");}
        else if(h.error){stTxt=String(h.error).slice(0,48);}
        else if(!PRODUCT_INFO.health[p.id]){stCls="deg";stTxt="probing…";}
        const room=p.room||h.room||"";
        html+='<div class="mplane" data-plane="'+esc(p.id)+'">'
          +'<span class="mid">'+esc(p.id)+'</span>'
          +'<span class="mlane">'+esc(p.lane||"?")+'</span>'
          +'<span class="mstat '+stCls+'">'+esc(stTxt)+'</span>'
          +(room?('<a class="mopen" href="'+esc(room)+'" target="_blank" rel="noopener">open worker room →</a>'):'')
          +'</div>';
      });
    }
    box.innerHTML=html;
  }else if(PRODUCT_INFO.role==="worker"){
    chip.hidden=false;
    const lane=(PRODUCT_INFO.chats_match&&PRODUCT_INFO.chats_match!=="all")
      ?PRODUCT_INFO.chats_match:"";
    chip.textContent=lane?("WORKER · "+lane):"WORKER";
    chip.className="rolechip worker";
    box.hidden=true;box.innerHTML="";
  }else{
    chip.hidden=true;box.hidden=true;box.innerHTML="";
  }
  updateChrome();
}
async function loadProductChrome(){
  try{
    const r=await api("/api/product");
    if(!r||!r.ok)return;
    PRODUCT_INFO.role=r.role||null;
    PRODUCT_INFO.managed_planes=r.managed_planes||[];
    PRODUCT_INFO.chats_match=r.chats_match||"";
    PRODUCT_INFO.health_chat=r.health_chat||null;
    PRODUCT_INFO.vp=r.vp||null;
    PRODUCT_INFO.engine_seat=r.engine_seat||null;
    PRODUCT_INFO.loaded=true;
    renderProductChrome();
    // Apply quiet manager scope once role is known (avoid full-fleet flash sticking).
    renderTagbar();renderSidebar();if(mode!=="head")render();
    if(PRODUCT_INFO.role==="manager") await loadManagedHealth();
  }catch(_){}
}
async function loadManagedHealth(){
  if(PRODUCT_INFO.role!=="manager")return;
  try{
    const r=await api("/api/managed");
    if(!r||!r.ok)return;
    PRODUCT_INFO.health={};
    (r.planes||[]).forEach(p=>{if(p&&p.id)PRODUCT_INFO.health[p.id]=p;});
    renderProductChrome();
  }catch(_){}
}

async function poll(){
  if(document.hidden&&grid.length)return;  // pause steady-state polling on a backgrounded tab, but still do the first load (#13)
  try{
    const r=await api("/api/grid?lines=14");
    if(!r.ok){$("#status").textContent=r.error||"error";$("#status").className="err";return;}
    grid=r.sessions;cacheMeta();updateCostBanner();
    $("#status").textContent=grid.filter(s=>s.live).length+" live · polling";$("#status").className="";
    updateChrome();renderTagbar();renderSidebar();
    // Permanent seats in manager chrome must track grid truth every poll.
    if(PRODUCT_INFO.role==="manager") renderProductChrome();
    if(mode!=="head")render();
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

let showDead=false;   // EID-880 declutter: dead/offline registry rows collapse by default
function renderSidebar(){
  const box=$("#sessions");box.innerHTML="";
  const mkCard=s=>{
    const d=document.createElement("div");
    d.className="card"+(current&&current.name===s.name?" active":"")+(needsYou(s)?" needy":"")+(costHit(s)?" costcap":"")+(s.live?"":" gone");
    d.dataset.name=s.name;
    const att=s.attached?'<span class="att">●attached</span>':"";
    const up=s.live?uptime(s.created_unix):"";
    const ag=s.agent||{glyph:"",label:""};
    const agspan=(s.live&&ag.label&&ag.label!=="—")?'<span>'+agentHTML(s)+'</span>':"";
    const tagspans=(s.tags||[]).map(t=>'<span class="tagjump" data-tag="'+t+'">#'+t+'</span>').join("");
    const cobadge=companyHTML(s);
    const badges=(s.registered?"<span>registered</span>":"<span>unregistered</span>")
      +cobadge+agspan+(s.attached?"<span>attached</span>":"")+tagspans;
    d.innerHTML='<div class="nm"><span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name+' '+statePip(s)+sendBadge(s)+att+'</div>'
      +'<div class="sub">→ '+s.session+(up?" · "+up:"")+(s.description?" — "+s.description:"")+'</div>'
      +'<div class="badges">'+badges+'</div>';
    // a click on a nested tag chip filters — it must NOT also open the card. Guard
    // at the card so this holds regardless of child-handler timing/re-render order.
    d.onclick=ev=>{if(ev.target.closest(".tagjump"))return;openModal(s);};
    box.appendChild(d);
  };
  // Live work first, always visible. Dead/offline (live=false) registry rows collapse
  // behind one toggle by default — so state pips + drive/observe badges aren't buried
  // under 10-of-15 dead rows. Toggle reveals them (state persists across polls).
  const _items=shown();
  _items.filter(s=>s.live).forEach(mkCard);
  const _dead=_items.filter(s=>!s.live);
  if(_dead.length){
    const t=document.createElement("div");t.className="deadtoggle";
    t.textContent=(showDead?"▾ hide ":"▸ show ")+_dead.length+" offline / dead";
    t.onclick=()=>{showDead=!showDead;renderSidebar();};
    box.appendChild(t);
    if(showDead)_dead.forEach(mkCard);
  }
  document.querySelectorAll(".tagjump").forEach(el=>el.onclick=ev=>{   // click a card's tag → filter to it
    ev.stopPropagation();const tag=el.dataset.tag;
    activeTag=tag===activeTag?"":tag;
    renderTagbar();renderSidebar();if(mode!=="head")render();
  });
}

const STLABEL={running:"run",idle:"idle",error:"failed",failed:"failed",asking:"asks you",waiting_human:"needs you",waiting_external:"ext wait",stuck:"stuck",offline:"offline",dead:"offline"};
// Honest fleet state (EID-880): absence of liveness → OFFLINE (never "failed"); an
// explicit classifier error → FAILED; STUCK (no change, not at a prompt) and
// WAITING_EXTERNAL (blocked on build/network) are distinct from WAITING_HUMAN and
// from FAILED. stale ≠ stuck ≠ failed, never conflated.
function pipState(s){if(!s.live)return"offline";const st=s.state||"idle";return st==="error"?"failed":st;}
function statePip(s){const st=pipState(s);const tip=(s.summary||st).replace(/"/g,"'");
  // EID-881: mark state that came from the durable receipt ledger (authoritative for
  // driven work) vs the classifier — so the source is distinguishable at a glance.
  const src=s.state_source==="ledger"?'<sup class="lsrc" title="authoritative — from the durable receipt ledger">L</sup>':"";
  return '<span class="spip st-'+st+'" title="'+tip+'">'+(STLABEL[st]||st)+src+'</span>';}
// EID-880 stretch: is `send` the right action for this session RIGHT NOW? The
// send-is-transport rubric — drive an autonomous worker with a verifiable done-condition
// that is NOT at a gate; observe anything gated / busy / stuck / failed (send fails closed
// on a visible gate anyway). Live sessions only.
function sendVerdict(s){
  if(!s.live)return null;
  const st=pipState(s);
  if(st==="idle")return{cls:"drive",label:"▶ send-ok",tip:"idle — safe to dispatch a task"};
  const why={running:"a task is in flight — don't interrupt",waiting_human:"at a human gate — send fails closed",
    asking:"asking you — a human decision",stuck:"stuck — unstick, don't blind-send",
    waiting_external:"blocked on build/network — sending won't help",failed:"failed — needs attention"}[st]
    ||("state="+st+" — observe");
  return{cls:"observe",label:"◉ observe",tip:why};
}
function sendBadge(s){const v=sendVerdict(s);
  return v?' <span class="sbadge sb-'+v.cls+'" title="'+v.tip.replace(/"/g,"'")+'">'+v.label+'</span>':"";}
// a session waiting on YOU — a formal gate, it asked a question, OR its gist reads
// like it's parked on a human action (authorize / approve / on your desk / until you…).
// deliberately CONSERVATIVE — only phrases that mean "parked, waiting on the human",
// which don't show up in ordinary working output. (Broad words like verify/paste/login
// false-positived on normal agent chatter.)
const _NEEDY_GIST=/(on your desk|await(ing)? your|waiting (on|for) (you|your)|needs? your (approval|sign|decision|input|go)|your (approval|sign-?off|authoriz)|until you (authoriz|approv|confirm|sign|respond))/i;
function needsYou(s){return s.live&&(s.needs_human||s.state==="asking"||s.state==="waiting_human"
  ||_NEEDY_GIST.test(lastSummary(s)));}
// a session that hit a usage/rate/quota/cost limit — burning budget or throttled.
function costHit(s){return s.live&&!!s.cost;}
function updateCostBanner(){
  const n=(grid||[]).filter(costHit).length;
  $("#costn").textContent=n;$("#costword").textContent=n===1?"session":"sessions";
  document.body.classList.toggle("costalert",n>0);
}
// jump to a cost-limited session so you can act on it (the grid badges mark the rest)
function focusCost(){const hit=(grid||[]).filter(costHit);if(hit.length)openModal(hit[0]);}
// fail this session over to another Claude account — dry-run first (shows where it
// goes), a second click within 4s confirms and does the code switch.
let switchArmed=null;
async function switchPlan(){
  if(!modalSession)return;const sess=modalSession.session;const b=$("#modalswitch");
  const post=body=>api("/api/plan/switch",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(switchArmed!==sess){
    const r=await post({session:sess,dry_run:true});
    if(!r.ok){b.textContent="⇄ "+(r.error||"no plan");setTimeout(()=>b.textContent="⇄ switch account",3500);return;}
    switchArmed=sess;b.textContent="⇄ confirm → "+r.to+"?";b.classList.add("armed");
    setTimeout(()=>{if(switchArmed===sess){switchArmed=null;b.textContent="⇄ switch account";b.classList.remove("armed");}},4000);
    return;
  }
  switchArmed=null;b.classList.remove("armed");b.textContent="⇄ switching…";
  const r=await post({session:sess});
  b.textContent="⇄ switch account";
  const st=$("#modalstatus");
  if(r.ok){st.textContent="switched → "+r.to;st.style.color="var(--live)";tchatLog("bot","↻ failed over to account "+r.to+" — resuming.");}
  else{st.textContent="switch failed: "+(r.error||"?");st.style.color="var(--stale)";}
}
// the live indicator: heartbeat EKG when running, a QUESTION MARK when it's
// asking you something, else a colored dot by state.
const EKG='<svg class="ekg" viewBox="0 0 44 16" preserveAspectRatio="none">'
  +'<polyline class="base" points="0,8 15,8 18,8 20,3 23,13 26,8 44,8"/>'
  +'<polyline class="beat" points="0,8 15,8 18,8 20,3 23,13 26,8 44,8"/></svg>';
function liveDot(s){
  if(!s.live)return '<span class="dot stale"></span>';
  if(s.state==="running")return EKG;
  if(s.state==="asking")return '<span class="qmark">?</span>';   // it's asking YOU
  const cls=s.state==="error"?"stale":(s.state==="waiting_human"?"warn":"live");
  return '<span class="dot '+cls+'"></span>';
}
// the thin summary rail — a super-cheap always-on "what's happening now", with a
// hover overlay that shows it in full.
function railHTML(s){
  const sum=s.summary||(s.live?"…":"gone");
  const i=sum.indexOf(" — ");
  const verb=i>0?sum.slice(0,i):sum, rest=i>0?sum.slice(i+3):"";
  // linkifyLinear already escapes; keep Linear keys clickable in the rail
  return '<div class="rail st-'+(s.state||"idle")+'"><span class="rv">'+linkifyLinear(verb)+'</span>'
    +(rest?' <span class="rt">'+linkifyLinear(rest)+'</span>':'')
    +'<div class="rfull"><b>'+linkifyLinear(verb)+'</b>'+(rest?' — '+linkifyLinear(rest):'')+'</div></div>';
}

function makeTile(s){
  const t=document.createElement("div");
  t.className="tile"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(costHit(s)?" costcap":"")+(s.live?"":" dead");
  const h=document.createElement("header");
  const att=s.attached?'<span class="att">●</span>':"";
  const ag=s.agent||{glyph:"",label:""};
  const agbadge=(s.live&&ag.label&&ag.label!=="—")?'<span class="agentbadge">'+agentHTML(s)+'</span>':"";
  h.innerHTML='<span class="lind">'+liveDot(s)+'</span><span class="nm">'+s.name+att+'</span>'
    +(s.host?'<span class="hosttag">⌨ '+esc(s.host)+'</span>':"")   // remote: say where it lives
    +companyHTML(s)+agbadge
    +'<span class="age '+(s.live?ageClass(s.last_change_age):"t-old")+'">'+(s.live?ageLabel(s.last_change_age):"gone")+'</span>'
    +statePip(s);
  if(needsYou(s)){const b=document.createElement("div");b.className="needbadge";b.textContent="⚠ NEEDS YOU";t.appendChild(b);}
  if(costHit(s)){const c=document.createElement("div");c.className="costbadge";c.textContent="💸 USAGE / COST LIMIT";t.appendChild(c);}
  const p=document.createElement("pre");
  if(s.live&&s.content.trim()){
    p.textContent=s.content.replace(/\s+$/,"").split("\n").slice(-14).join("\n");
  }else if(!s.live&&lastSummary(s)){
    // GONE, but we remember what it was doing — show it instead of a blank ghost
    p.className="empty gonecache";
    p.textContent="⏹ session ended"+(goneAge(s)?" · "+goneAge(s)+" ago":"")+"\nlast: "+lastSummary(s);
  }else{
    p.className="empty";p.textContent=s.live?"(blank pane)":"tmux session gone";
  }
  const rail=document.createElement("div");rail.className="railwrap";rail.innerHTML=railHTML(s);
  t.appendChild(h);t.appendChild(rail.firstChild);t.appendChild(p);
  bindLinlinks(t);
  t.onclick=e=>{if(e.target.closest("a.linlink"))return;openModal(s);};
  return t;
}

function renderGrid(){
  const v=$("#views");v.innerHTML="";
  const list=shown();
  if(!list.length){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const g=document.createElement("div");g.className="tilegrid";
  list.forEach(s=>g.appendChild(makeTile(s)));
  v.appendChild(g);
}

function renderGroups(){
  const v=$("#views");v.innerHTML="";
  const groups=new Map();
  const put=(k,s)=>{if(!groups.has(k))groups.set(k,[]);groups.get(k).push(s);};
  shown().forEach(s=>{
    if(!s.registered)put("unregistered",s);
    else if(!(s.tags||[]).length)put("untagged",s);
    else s.tags.forEach(t=>put("#"+t,s));
  });
  if(!groups.size){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const order=[...groups.keys()].sort((a,b)=>{
    const w=k=>k==="unregistered"?2:(k==="untagged"?1:0);
    return w(a)-w(b)||a.localeCompare(b);
  });
  order.forEach(k=>{
    const sec=document.createElement("div");sec.className="group";
    const h=document.createElement("h2");
    if(k[0]==="#")h.id="grp-"+k.slice(1);   // anchor for clickable-tag jump (#8)
    h.innerHTML=k+' <span class="cnt">· '+groups.get(k).length+'</span>';
    const g=document.createElement("div");g.className="tilegrid";
    groups.get(k).forEach(s=>g.appendChild(makeTile(s)));
    sec.appendChild(h);sec.appendChild(g);v.appendChild(sec);
  });
}

function renderActivity(){
  const v=$("#views");v.innerHTML="";
  const list=shown();
  if(!list.length){v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  const wrap=document.createElement("div");wrap.className="actrows";
  list.forEach(s=>{
    const row=document.createElement("div");row.className="actrow";
    const nm=document.createElement("div");nm.className="nm";
    nm.innerHTML='<span class="dot '+(s.live?"live":"stale")+'"></span>'+s.name;
    const cells=document.createElement("div");cells.className="cells";
    const samples=s.activity||[];
    for(let i=0;i<Math.max(0,60-samples.length);i++){const c=document.createElement("div");c.className="cell";cells.appendChild(c);}
    samples.forEach((on,i)=>{
      const c=document.createElement("div");
      c.className="cell"+(on?(i>=samples.length-5?" recent":" on"):"");
      cells.appendChild(c);
    });
    const age=document.createElement("div");age.className="age "+(s.live?ageClass(s.last_change_age):"t-old");
    age.textContent=s.live?("active "+ageLabel(s.last_change_age)):"gone";
    row.appendChild(nm);row.appendChild(cells);row.appendChild(age);
    row.onclick=()=>openModal(s);
    wrap.appendChild(row);
  });
  v.appendChild(wrap);
}

function el(tag,attrs){const e=document.createElementNS(SVGNS,tag);for(const k in attrs)e.setAttribute(k,attrs[k]);return e;}

function paneText(s){return (s.live&&s.content.trim())?s.content.replace(/\s+$/,"").split("\n").slice(-9).join("\n"):"";}
function agentHTML(s){const ag=s.agent||{};if(!s.live)return "gone";
  const g=ag.glyph?'<span class="agi ag-'+(ag.agent||"x")+'">'+ag.glyph+'</span> ':'';
  return g+(ag.label||"—");}
function companyHTML(s){const c=s.company||{};if(!c.company)return "";
  return '<span class="cco" style="background:'+c.color+'">'+c.label+'</span>';}

// Update flow boxes in place (no DOM teardown) — keeps the panes LIVE and smooth.
function updateFlowPanes(){
  grid.forEach(s=>{
    const d=flowBox[s.name], pre=flowPre[s.name];
    if(!d||!pre)return;
    d.className="fbox"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(s.live?"":" dead");
    const ag=d.querySelector(".ag");if(ag)ag.innerHTML=agentHTML(s);
    const sp=d.querySelector(".spip");if(sp){const st=pipState(s);
      sp.className="spip st-"+st;sp.textContent=STLABEL[st]||st;sp.title=(s.summary||st).replace(/"/g,"'");}
    const li=d.querySelector(".lind");if(li)li.innerHTML=liveDot(s);   // idle↔run swaps dot↔heartbeat
    const rl=d.querySelector(".rail");if(rl)rl.outerHTML=railHTML(s);   // refresh the summary rail
    const txt=paneText(s);
    if(txt){pre.className="";if(pre.textContent!==txt)pre.textContent=txt;}
    else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
  });
}

function renderFlow(){
  const v=$("#views");
  if(!grid.length){flowSig=null;v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no tmux sessions found</div></div>';return;}
  // work on the connection-aware filtered set: whole chains stay together
  const G=shown();
  if(!G.length){flowSig=null;v.innerHTML='<div id="empty"><div class="glyph">▚▞</div><div>no matching sessions</div></div>';return;}
  // resolve manage targets by registry name OR underlying tmux session name
  const byKey={};G.forEach(s=>{byKey[s.name]=s;if(!(s.session in byKey))byKey[s.session]=s;});
  const children=new Map(),indeg=new Map();
  G.forEach(s=>indeg.set(s.name,0));
  const edges=[];
  G.forEach(s=>(s.manages||[]).forEach(t=>{
    const tg=byKey[t];if(!tg||tg.name===s.name)return;
    edges.push([s.name,tg.name]);
    if(!children.has(s.name))children.set(s.name,[]);
    children.get(s.name).push(tg.name);
    indeg.set(tg.name,(indeg.get(tg.name)||0)+1);
  }));
  const connected=new Set();edges.forEach(([a,b])=>{connected.add(a);connected.add(b);});
  // level assignment via BFS from roots; guard against cycles
  const level=new Map();
  [...connected].filter(n=>(indeg.get(n)||0)===0).forEach(r=>level.set(r,0));
  let q=[...level.keys()],guard=0,MAX=G.length*G.length+20;
  while(q.length&&guard++<MAX){
    const n=q.shift(),l=level.get(n);
    (children.get(n)||[]).forEach(c=>{
      if(l+1<G.length&&(!level.has(c)||level.get(c)<l+1)){level.set(c,l+1);q.push(c);}
    });
  }
  [...connected].forEach(n=>{if(!level.has(n))level.set(n,0);});
  const maxLvl=Math.max(0,...[...level.values()]);
  const unconnected=G.filter(s=>!connected.has(s.name));

  // Topology signature: rebuild the DOM only when the structure changes;
  // otherwise just stream new pane content into the existing boxes.
  const sig=JSON.stringify([
    [...connected].sort(), unconnected.map(s=>s.name).sort(),
    edges.map(e=>e.join(">")).sort(), [...level.entries()].sort(),
  ]);
  if(sig===flowSig && document.getElementById("flowwrap")){updateFlowPanes();return;}
  flowSig=sig;flowPre={};flowBox={};
  v.innerHTML="";

  // Each node is a live mini-pane box (title + tiny tmux preview), laid out by
  // level. Boxes are HTML positioned over an SVG edge layer.
  const BW=236,BH=150,COLGAP=40,ROWGAP=64,PAD=30;
  const W=1200;
  // Unconnected boxes wrap into a left-aligned GRID that never overlaps — the old
  // layout crammed every unconnected box onto a single fixed-width row (x =
  // W*(i+1)/(n+1)), so past ~4 boxes they printed on top of each other.
  const UNCW=BW+COLGAP;
  const uncCols=Math.max(1,Math.floor((W-2*PAD+COLGAP)/UNCW));
  const uncRows=unconnected.length?Math.ceil(unconnected.length/uncCols):0;
  const H=PAD*2+(maxLvl+1)*(BH+ROWGAP)+(uncRows?ROWGAP+uncRows*(BH+ROWGAP):0);
  const pos={};
  const rowY=lv=>PAD+BH/2+lv*(BH+ROWGAP);
  for(let lv=0;lv<=maxLvl;lv++){
    const row=[...connected].filter(n=>level.get(n)===lv);
    row.forEach((n,i)=>{pos[n]={x:W*(i+1)/(row.length+1),y:rowY(lv)};});
  }
  const uncY=rowY(maxLvl+1);
  unconnected.forEach((s,i)=>{
    const c=i%uncCols, r=Math.floor(i/uncCols);
    pos[s.name]={x:PAD+BW/2+c*UNCW, y:uncY+r*(BH+ROWGAP)};
  });

  // edge layer (SVG, behind the boxes)
  const svg=el("svg",{id:"flowsvg",viewBox:"0 0 "+W+" "+H});
  const defs=el("defs",{});
  const marker=el("marker",{id:"arrow",viewBox:"0 0 10 10",refX:"9",refY:"5",markerWidth:"7",markerHeight:"7",orient:"auto-start-reverse"});
  marker.appendChild(el("path",{d:"M0,0 L10,5 L0,10 z",fill:"var(--amber)"}));
  defs.appendChild(marker);svg.appendChild(defs);
  edges.forEach(([a,b])=>{
    const pa=pos[a],pb=pos[b];if(!pa||!pb)return;
    const y1=pa.y+BH/2,y2=pb.y-BH/2,my=(y1+y2)/2;
    svg.appendChild(el("path",{d:"M"+pa.x+","+y1+" C"+pa.x+","+my+" "+pb.x+","+my+" "+pb.x+","+y2,
      class:"edge","marker-end":"url(#arrow)"}));
  });
  if(unconnected.length){
    const sy=uncY-BH/2-ROWGAP/2;
    svg.appendChild(el("line",{x1:PAD,y1:sy,x2:W-PAD,y2:sy,class:"sep"}));
    const lab=el("text",{x:PAD+6,y:sy-7,class:"rowlabel"});lab.textContent="unconnected · not in any manages relationship";
    svg.appendChild(lab);
  }

  const wrap=document.createElement("div");wrap.id="flowwrap";
  wrap.style.width=W+"px";wrap.style.height=H+"px";
  wrap.appendChild(svg);

  // node boxes: title (name + agent icon) + tiny live pane
  function box(s){
    const p=pos[s.name];
    const d=document.createElement("div");
    d.className="fbox"+(hot(s)?" hot":"")+(needsYou(s)?" needy":"")+(s.live?"":" dead");
    d.style.left=p.x+"px";d.style.top=p.y+"px";d.style.width=BW+"px";
    const title='<div class="ftitle"><span class="lind">'+liveDot(s)+'</span>'
      +'<span class="nm">'+s.name+'</span>'+statePip(s)+'<span class="ag">'+agentHTML(s)+'</span></div>';
    const pre=document.createElement("pre");
    const txt=paneText(s);
    if(txt)pre.textContent=txt;else{pre.className="empty";pre.textContent=s.live?"(blank pane)":"tmux session gone";}
    d.innerHTML=title+railHTML(s);d.appendChild(pre);
    d.onclick=()=>openModal(s);                 // click a box → zoom-in modal to steer it
    flowBox[s.name]=d;flowPre[s.name]=pre;
    wrap.appendChild(d);
  }
  [...connected].forEach(n=>box(byKey[n]));
  unconnected.forEach(box);

  v.appendChild(wrap);
  const hint=document.createElement("div");hint.id="flowhint";
  hint.innerHTML="each box is a live tmux pane (click to zoom in & steer); title shows the session + detected AI. arrows = agent manages agent (<code>emux register &lt;name&gt; &lt;session&gt; --manages &lt;other&gt;</code>) — orchestrators on top, the agents they drive below.";
  v.appendChild(hint);
}

// ---- ORPHANS view: tmux sessions emux does NOT know about yet, per machine,
// in the grid look — pane preview + one-click ⇤ ATTACH. This is the un-f'ing
// tool: when sessions exist that the control room can't see, they show up here.
const MV={host:"",hosts:[],rows:[],loading:false,gen:0};
async function mvPickHost(h){
  MV.host=h;MV.rows=[];MV.loading=true;const gen=++MV.gen;renderOrphans();
  const r=await api("/api/dirs?host="+encodeURIComponent(h));
  if(gen!==MV.gen)return;                   // clicked away while ssh probed
  MV.rows=(r.ok&&r.running?r.running:[]).filter(s=>!s.adopted);   // orphans only
  MV.loading=false;renderOrphans();
  // fill in the pane previews (parallel; remotes are ssh hops)
  await Promise.all(MV.rows.slice(0,24).map(async s=>{
    const p=await api("/api/peek?session="+encodeURIComponent(s.name)
                      +"&host="+encodeURIComponent(h)+"&lines=14");
    if(gen!==MV.gen)return;
    s.content=p.ok?(p.content||""):("could not read: "+(p.error||""));
    s.unsent=!!p.unsent;
  }));
  if(gen===MV.gen)renderOrphans();
}
async function mvAdopt(s,btn){
  btn.disabled=true;btn.textContent="OPENING HEAD…";
  const r=await api("/api/adopt",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:s,host:MV.host,name:s,
                         description:"orphan adopted from "+MV.host,
                         tags:["adopted",MV.host]})});
  if(!r.ok){btn.disabled=false;btn.textContent="⇤ OPEN HEAD";
    $("#mverr").textContent=r.error||"adopt failed";return;}
  MV.rows=MV.rows.filter(x=>x.name!==s);    // no longer an orphan
  renderOrphans();refresh();                // grid/sidebar pick it up
}
function orphanTile(s){
  const t=document.createElement("div");t.className="tile orph";
  const h=document.createElement("header");
  h.innerHTML='<span class="lind"><span class="dot live"></span></span>'
    +'<span class="nm">'+esc(s.name)+(s.attached?'<span class="att">●</span>':"")+'</span>'
    +'<span class="hosttag">⌨ '+esc(MV.host)+'</span>'
    +'<span class="age t-old">'+ago(s.age_sec)+'</span>';
  const b=document.createElement("button");b.className="act oattach";b.textContent="⇤ OPEN HEAD";
  b.onclick=e=>{e.stopPropagation();mvAdopt(s.name,b);};
  h.appendChild(b);
  const p=document.createElement("pre");
  if(s.content===undefined){p.className="empty";p.textContent="reading…";}
  else if(s.content.trim()){p.textContent=s.content.replace(/\s+$/,"").split("\n").slice(-14).join("\n");}
  else{p.className="empty";p.textContent="(blank pane)";}
  const w=document.createElement("div");w.className="owhy";
  w.textContent=(s.path||"")+(s.unsent?"  ·  ⚠ holds an unsent prompt":"");
  t.appendChild(h);t.appendChild(p);t.appendChild(w);
  return t;
}
function renderOrphans(){
  const v=$("#views");
  if(mode!=="orphans")return;
  v.innerHTML='<div id="mvhosts">'+MV.hosts.map(h=>
    '<span class="hchip'+(h===MV.host?" on":"")+'" data-h="'+esc(h)+'">⌨ '+esc(h)+'</span>').join("")
    +'</div><div id="mverr"></div>';
  v.querySelectorAll("#mvhosts .hchip").forEach(el=>el.onclick=()=>mvPickHost(el.dataset.h));
  if(!MV.host){v.insertAdjacentHTML("beforeend",
    '<div id="empty"><div class="glyph">▚▞</div><div>pick a machine to hunt for orphans</div></div>');return;}
  if(MV.loading){v.insertAdjacentHTML("beforeend",
    '<div id="empty"><div class="glyph">▚▞</div><div>looking at '+esc(MV.host)+'…</div></div>');return;}
  if(!MV.rows.length){v.insertAdjacentHTML("beforeend",
    '<div id="empty"><div class="glyph">▚▞</div><div>no orphans on '+esc(MV.host)+' — every tmux is in emux ✓</div></div>');return;}
  const g=document.createElement("div");g.className="tilegrid";
  MV.rows.forEach(s=>g.appendChild(orphanTile(s)));
  v.appendChild(g);
}
async function openOrphans(){
  if(!MV.hosts.length){const r=await api("/api/hosts");if(r.ok)MV.hosts=r.hosts||[];}
  renderOrphans();
}

// ---- CHATS view: Claude Code + Grok Build transcripts on disk that are not
// live in a process. ORPHANS = unknown tmux; CHATS = dropped agent missions.
// Resume is copy-paste (spawn is a different path — do not auto-launch).
// Click a tile to peek last turns from the transcript.
const CH={rows:[],loading:false,gen:0,status:"stale,recent",match:"",tool:"",
  q:"",sort:"priority",counts:{},tools_counts:{},scan_ms:0,matched:0,openKey:"",peeks:{},
  source:"",store_rows:null,did_sync:false};
function chatAge(h){
  if(h==null)return "—";
  if(h<1)return Math.round(h*60)+"m";
  if(h<48)return Math.round(h)+"h";
  return Math.round(h/24)+"d";
}
function chatKey(c){return (c.tool||"")+"|"+(c.session_id||"");}
async function copyResume(text,btn){
  try{await navigator.clipboard.writeText(text||"");btn.textContent="✓ COPIED";
    setTimeout(()=>btn.textContent="⧉ COPY",1200);}
  catch(_){btn.textContent="select + copy";}
}
async function resumeChatInFleet(c,btn){
  if(!c||c.status==="live")return;
  const prev=btn.textContent;btn.disabled=true;btn.textContent="RESUMING…";
  const errEl=$("#mverr");
  try{
    const r=await api("/api/chats/resume",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({tool:c.tool,session_id:c.session_id,cwd:c.cwd,title:c.title,
                           greenmark:!!c.greenmark,gui:false})});
    if(!r.ok){
      btn.disabled=false;btn.textContent=prev;
      if(errEl)errEl.textContent=(r.error||"resume failed")+(r.hint?(" — "+r.hint):"");
      return;
    }
    btn.textContent="✓ IN FLEET · "+(r.name||"");
    if(errEl)errEl.textContent="resumed as “"+(r.name||"?")+"” · "+(r.command||"")
      +(r.partial?" · (spawn ok, agent may need a nudge)":"");
    // refresh grid so the new session shows; jump to GRID so you can steer it
    try{await poll();}catch(_){}
    setTimeout(()=>{
      setMode("grid");
      const s=grid.find(x=>x.name===r.name||x.session===r.session);
      if(s)openModal(s);
    },400);
  }catch(e){
    btn.disabled=false;btn.textContent=prev;
    if(errEl)errEl.textContent="daemon unreachable";
  }
}
async function toggleChatPeek(c,tile){
  const key=chatKey(c);
  if(CH.openKey===key){CH.openKey="";renderChats();return;}
  CH.openKey=key;renderChats();
  if(CH.peeks[key])return;
  try{
    const r=await api("/api/chats/peek?tool="+encodeURIComponent(c.tool)
      +"&session_id="+encodeURIComponent(c.session_id)+"&turns=8");
    CH.peeks[key]=r;
  }catch(e){CH.peeks[key]={ok:false,error:"unreachable"};}
  if(CH.openKey===key)renderChats();
}
function chatTile(c){
  const t=document.createElement("div");
  const key=chatKey(c);
  const open=CH.openKey===key;
  t.className="tile chat"+(open?" open":"");
  const st=c.status||"stale";
  const h=document.createElement("header");
  h.innerHTML='<span class="tool '+esc(c.tool||"")+'">'+esc(c.tool||"?")+'</span>'
    +(c.greenmark?'<span class="gm">GM</span>':"")
    +'<span class="nm" title="'+esc(c.session_id||"")+'">'+linkifyLinear(c.title||c.session_id||"")+'</span>'
    +'<span class="st-'+esc(st)+'">'+esc(st)+'</span>'
    +'<span class="age t-old">'+chatAge(c.age_hours)+'</span>';
  const sum=document.createElement("div");sum.className="sum";
  sum.innerHTML=linkifyLinear(c.summary||c.title||"(no summary)");
  const cwd=document.createElement("div");cwd.className="cwd";cwd.title=c.cwd||"";
  cwd.textContent=c.cwd||"";
  const meta=document.createElement("div");meta.className="meta";
  const bits=[];
  if(c.messages!=null)bits.push(c.messages+" lines");
  if(c.model)bits.push(String(c.model).split("/").pop());
  if(c.branch)bits.push("⎇ "+c.branch);
  if(c.mtime_iso)bits.push(c.mtime_iso.slice(0,10));
  if(c.priority!=null)bits.push("prio "+c.priority);
  if(c.fleet_name)bits.push("fleet "+c.fleet_name);
  if(c.on_disk===false)bits.push("off-disk");
  if(c.source)bits.push(c.source);
  // Surface linked Linear issue from registry/resume metadata when present
  if(c.linear&&c.linear.issue) bits.push(c.linear.issue);
  meta.innerHTML=linkifyLinear(bits.join(" · "));
  const res=document.createElement("div");res.className="resume";res.textContent=c.resume||"";
  const acts=document.createElement("div");acts.className="actions";
  const bFleet=document.createElement("button");bFleet.className="act oattach";
  if(c.status==="resumed"&&c.fleet_name){
    bFleet.textContent="↗ OPEN "+c.fleet_name;
    bFleet.title="already resumed into fleet as "+c.fleet_name;
    bFleet.onclick=e=>{e.stopPropagation();
      setMode("grid");const s=grid.find(x=>x.name===c.fleet_name);if(s)openModal(s);
      else{const err=$("#mverr");if(err)err.textContent="fleet session “"+c.fleet_name+"” not live — try RESUME again or check GRID";}};
  }else if(c.status==="live"){
    bFleet.textContent="● LIVE";bFleet.disabled=true;
    bFleet.title="process still holds this chat — attach/boss, do not double-resume";
  }else{
    bFleet.textContent="⇤ RESUME IN FLEET";
    bFleet.title="spawn tmux + register in fleet with "+(c.tool==="claude"?"claude --resume":"grok --resume");
    bFleet.onclick=e=>{e.stopPropagation();resumeChatInFleet(c,bFleet);};
  }
  const bCopy=document.createElement("button");bCopy.className="act";bCopy.textContent="⧉ COPY";
  bCopy.onclick=e=>{e.stopPropagation();copyResume(c.resume,bCopy);};
  const bPeek=document.createElement("button");bPeek.className="act";
  bPeek.textContent=open?"▾ CLOSE":"▸ PEEK";
  bPeek.onclick=e=>{e.stopPropagation();toggleChatPeek(c,t);};
  acts.appendChild(bFleet);acts.appendChild(bCopy);acts.appendChild(bPeek);
  t.appendChild(h);t.appendChild(sum);t.appendChild(cwd);t.appendChild(meta);
  t.appendChild(res);t.appendChild(acts);
  if(open){
    const peek=document.createElement("div");peek.className="peek";
    const cached=CH.peeks[key];
    if(!cached)peek.innerHTML='<div class="role">loading last turns…</div>';
    else if(!cached.ok)peek.innerHTML='<div class="role">peek failed</div><div>'
      +esc(cached.error||"error")+'</div>';
    else if(!(cached.turns||[]).length)peek.innerHTML='<div class="role">no turns found in tail</div>';
    else (cached.turns||[]).forEach(tr=>{
      const d=document.createElement("div");d.className="turn";
      d.innerHTML='<div class="role '+esc(tr.role||"")+'">'+esc(tr.role||"?")+'</div>'
        +'<div class="body">'+linkifyLinear(tr.text||"")+'</div>';
      peek.appendChild(d);
    });
    t.appendChild(peek);
  }
  bindLinlinks(t);
  t.onclick=e=>{if(e.target.closest("button,a.linlink"))return;toggleChatPeek(c,t);};
  return t;
}
function chChip(label,on,attrs){
  return '<span class="hchip'+(on?" on":"")+'" '+attrs+'>'+esc(label)+'</span>';
}
function renderChats(){
  const v=$("#views");
  if(mode!=="chats")return;
  const cnt=CH.counts||{};
  const tc=CH.tools_counts||{};
  const sts=[
    ["stale,recent","needs attention"],
    ["stale","stale"+(cnt.stale!=null?" · "+cnt.stale:"")],
    ["recent","recent"+(cnt.recent!=null?" · "+cnt.recent:"")],
    ["live","live"+(cnt.live!=null?" · "+cnt.live:"")],
    ["resumed","resumed"+(cnt.resumed!=null?" · "+cnt.resumed:"")],
    ["all","all"+(cnt.total!=null?" · "+cnt.total:"")],
  ];
  const tools=[
    ["","both tools"],
    ["claude","claude"+(tc.claude!=null?" · "+tc.claude:"")],
    ["grok","grok"+(tc.grok!=null?" · "+tc.grok:"")],
  ];
  // Match chips follow active product skin (dynamic). Default is lane-tight.
  const skinMatch=SKIN_ID==="gmux"?"greenmark"
    :(SKIN_ID==="reevux"?"personal"
    :(SKIN_ID==="amux"?"aic"
    :(SKIN_ID==="directrux"?"directrux":"all")));
  const matches=SKIN_ID==="directrux"
    ?[["directrux","directrux only"],["all","all paths (unsafe)"]]
    :(SKIN_ID==="gmux"
      ?[["greenmark","greenmark"],["all","all paths"]]
      :(SKIN_ID==="reevux"
        ?[["personal","personal"],["all","all paths"]]
        :(SKIN_ID==="amux"
          ?[["aic","aic"],["all","all paths"]]
          :[["all","all paths"],["aic","aic"],["personal","personal"],["greenmark","greenmark"],["directrux","directrux"]])));
  const sorts=[["priority","by urgency"],["mtime","by recency"]];
  const activeMatch=CH.match||skinMatch;
  v.innerHTML='<div id="chbar">'
    +'<div class="row" id="chstrow">'+sts.map(([k,l])=>chChip(l,CH.status===k,'data-st="'+k+'"')).join("")
    +'<span class="hint">disk → fleet: RESUME IN FLEET spawns tmux · click tile to peek</span></div>'
    +'<div class="row" id="chtoolrow">'+tools.map(([k,l])=>chChip(l,CH.tool===k,'data-tool="'+k+'"')).join("")
    +matches.map(([k,l])=>chChip(l,activeMatch===k,'data-match="'+k+'"')).join("")
    +sorts.map(([k,l])=>chChip(l,CH.sort===k,'data-sort="'+k+'"')).join("")
    +'</div>'
    +'<div class="row">'
    +'<input id="chq" type="search" placeholder="search title · cwd · id…" value="'+esc(CH.q)+'">'
    +'<button class="act" id="chrefresh" type="button" title="force re-index disk into chats.db">↻ re-index</button>'
    +'<span class="stat">'+(CH.loading?"loading…":
      ('showing <b>'+CH.rows.length+'</b>'
       +(CH.matched&&CH.matched!==CH.rows.length?(' of '+CH.matched):'')
       +(CH.scan_ms?(' · '+CH.scan_ms+'ms'):'')
       +(CH.source?(' · '+CH.source):'')
       +(CH.store_rows!=null?(' · db '+CH.store_rows):'')
       +(CH.did_sync?' · synced':'')
       +(cnt.stale!=null?(' · <b>'+cnt.stale+'</b> stale'):'')
       +(cnt.resumed?(' · '+cnt.resumed+' resumed'):'')))+'</span>'
    +'</div></div><div id="mverr"></div>';
  v.querySelectorAll("#chstrow .hchip").forEach(el=>el.onclick=()=>{CH.status=el.dataset.st;syncChatURL();loadChats(false);});
  v.querySelectorAll("#chtoolrow .hchip[data-tool]").forEach(el=>el.onclick=()=>{CH.tool=el.dataset.tool||"";syncChatURL();loadChats(false);});
  v.querySelectorAll("#chtoolrow .hchip[data-match]").forEach(el=>el.onclick=()=>{CH.match=el.dataset.match||"";syncChatURL();loadChats(false);});
  v.querySelectorAll("#chtoolrow .hchip[data-sort]").forEach(el=>el.onclick=()=>{CH.sort=el.dataset.sort||"priority";syncChatURL();loadChats(false);});
  const chq=$("#chq");
  if(chq){let tmo=null;chq.oninput=()=>{clearTimeout(tmo);tmo=setTimeout(()=>{CH.q=chq.value.trim();syncChatURL();loadChats(false);},280);};
    chq.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();CH.q=chq.value.trim();syncChatURL();loadChats(false);}};}
  const chrf=$("#chrefresh");if(chrf)chrf.onclick=()=>{CH.peeks={};loadChats(true);};
  if(CH.loading&&!CH.rows.length){v.insertAdjacentHTML("beforeend",
    '<div id="empty"><div class="glyph">💬</div><div>scanning Claude + Grok stores…</div></div>');return;}
  if(!CH.loading&&!CH.rows.length){v.insertAdjacentHTML("beforeend",
    '<div id="empty"><div class="glyph">💬</div><div>no matching chats for this skin'
    +(activeMatch?(' · match=<b>'+esc(activeMatch)+'</b>'):'')
    +'<br><span style="font-size:12px">'
    +(SKIN_ID==="directrux"
      ?'Directrux only shows meta/directrux work — not the whole machine. “all paths” is opt-in and unsafe.'
      :'try another match chip or clear search')
    +'</span></div></div>');return;}
  const g=document.createElement("div");g.className="tilegrid";
  CH.rows.forEach(c=>g.appendChild(chatTile(c)));
  v.appendChild(g);
}
function syncChatURL(){
  // fold chat filters into the hash when in chats view
  if(mode!=="chats")return;
  syncURL();
}
async function loadChats(forceRefresh){
  CH.loading=true;const gen=++CH.gen;renderChats();
  let q="/api/chats?limit=80&recent_hours=24&sort="+encodeURIComponent(CH.sort||"priority");
  if(CH.status&&CH.status!=="all")q+="&status="+encodeURIComponent(CH.status);
  // empty match → server defaults per skin (directrux → directrux only, not all)
  if(CH.match)q+="&match="+encodeURIComponent(CH.match);
  if(CH.tool)q+="&tools="+encodeURIComponent(CH.tool);
  if(CH.q)q+="&q="+encodeURIComponent(CH.q);
  if(forceRefresh)q+="&refresh=1";
  try{
    const r=await api(q);
    if(gen!==CH.gen)return;
    CH.rows=r.ok?(r.chats||[]):[];
    CH.counts=r.counts||{};
    CH.tools_counts=r.tools_counts||{};
    CH.scan_ms=r.scan_ms||0;
    CH.matched=r.matched||CH.rows.length;
    CH.source=r.source||"";
    CH.store_rows=r.store_rows;
    CH.did_sync=!!r.did_sync;
    if(r.ok&&r.match&&!CH.match)CH.match=r.match;  // surface server default (gmux→greenmark)
    if(!r.ok){const err=$("#mverr");if(err)err.textContent=r.error||"scan failed";}
  }catch(e){
    if(gen!==CH.gen)return;
    CH.rows=[];
    const err=$("#mverr");if(err)err.textContent="daemon unreachable";
  }
  CH.loading=false;renderChats();updateChrome();
}
async function openChats(){
  // restore filters from URL hash if present
  const p=new URLSearchParams(location.hash.slice(1));
  if(p.get("ch_status"))CH.status=p.get("ch_status");
  if(p.get("ch_tool")!=null)CH.tool=p.get("ch_tool")||"";
  if(p.get("ch_match"))CH.match=p.get("ch_match");
  if(p.get("ch_q"))CH.q=p.get("ch_q");
  if(p.get("ch_sort"))CH.sort=p.get("ch_sort");
  renderChats();
  loadChats();
}


// ---- CALENDAR: Google-like week/month for cron message jobs ----
const CAL={view:"week",anchor:new Date(), jobs:[], events:[], loading:false, gen:0, hidden:{}, selected:null};
const CAL_COLORS=["#7eb8da","#f0b429","#6bcb77","#e07a5f","#9b8cff","#4ecdc4","#ff8fab","#c9a227"];
function calColor(id){let h=0;const s=String(id||"");for(let i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))>>>0;return CAL_COLORS[h%CAL_COLORS.length];}
// Plain-English schedule (mirrors emux.schedule.humanize_cron) — sidebar speaks this, not raw cron.
const CAL_TZ_ABBREV={
  "America/Chicago":"CT","America/New_York":"ET","America/Denver":"MT",
  "America/Los_Angeles":"PT","America/Phoenix":"MST","UTC":"UTC","Etc/UTC":"UTC"
};
function calTzLabel(tz){
  const name=(tz||"").trim()||"America/Chicago";
  if(CAL_TZ_ABBREV[name]) return CAL_TZ_ABBREV[name];
  if(name.includes("/")) return name.split("/").pop().replace(/_/g," ");
  return name;
}
function calClockPhrase(minute, hour){
  if(hour.startsWith("*/") && /^\d+$/.test(minute)){
    const n=parseInt(hour.slice(2),10); if(!n) return null;
    const base="every "+n+" hour"+(n===1?"":"s");
    const m=parseInt(minute,10);
    return m===0?base:(base+" at :"+String(m).padStart(2,"0"));
  }
  if(minute.startsWith("*/") && hour==="*"){
    const n=parseInt(minute.slice(2),10); if(!n) return null;
    return n===1?"every minute":("every "+n+" minutes");
  }
  if(hour==="*" && /^\d+$/.test(minute)){
    const m=parseInt(minute,10);
    return m===0?"every hour":("every hour at :"+String(m).padStart(2,"0"));
  }
  if(/^\d+$/.test(minute) && /^\d+$/.test(hour)){
    const h=parseInt(hour,10), m=parseInt(minute,10);
    if(h<0||h>23||m<0||m>59) return null;
    const suffix=h<12?"AM":"PM";
    let h12=h%12; if(h12===0) h12=12;
    return m===0?(h12+":00 "+suffix):(h12+":"+String(m).padStart(2,"0")+" "+suffix);
  }
  return null;
}
function calDowPhrase(dow){
  if(dow==="*"||dow==="?") return "every day";
  if(dow==="1-5"||dow==="1,2,3,4,5") return "weekdays";
  if(dow==="0,6"||dow==="6,0") return "weekends";
  const names={0:"Sunday",1:"Monday",2:"Tuesday",3:"Wednesday",4:"Thursday",5:"Friday",6:"Saturday",7:"Sunday"};
  const toks=[];
  for(const piece of dow.split(",")){
    const p=piece.trim(); if(!p) continue;
    if(p.includes("-")){
      const [a,b]=p.split("-").map(x=>parseInt(x,10));
      if(Number.isNaN(a)||Number.isNaN(b)||a>b) return null;
      for(let i=a;i<=b;i++) toks.push(String(i));
    }else if(/^\d+$/.test(p)) toks.push(p);
    else return null;
  }
  if(!toks.length) return null;
  const uniq=[...new Set(toks.map(t=>t==="7"?"0":t))];
  const label=t=>names[t]||t;
  if(uniq.length===1) return label(uniq[0])+"s";
  if(uniq.length===2) return label(uniq[0])+"s and "+label(uniq[1])+"s";
  return uniq.slice(0,-1).map(t=>label(t)+"s").join(", ")+", and "+label(uniq[uniq.length-1])+"s";
}
function humanizeCron(expr, timezone){
  const raw=(expr||"").trim();
  if(!raw) return "no schedule";
  const parts=raw.split(/\s+/);
  if(parts.length!==5) return raw;
  const [minute,hour,dom,month,dow]=parts;
  const tz=calTzLabel(timezone);
  let dayPart=null;
  if((dom==="*"||dom==="?") && month==="*") dayPart=calDowPhrase(dow);
  else if(dom!=="*" && dom!=="?" && (dow==="*"||dow==="?")){
    dayPart=/^\d+$/.test(dom)?("on the "+parseInt(dom,10)+" of each month"):("on day "+dom+" each month");
  }else if(dom!=="*" && dom!=="?" && dow!=="*" && dow!=="?"){
    dayPart=(calDowPhrase(dow)||("DOW "+dow))+" or day "+dom;
  }else dayPart=(dow==="*"||dow==="?")?"every day":calDowPhrase(dow);
  const clock=calClockPhrase(minute, hour);
  if(!clock) return raw+" ("+tz+")";
  if(clock.startsWith("every")){
    if(!dayPart||dayPart==="every day") return clock.charAt(0).toUpperCase()+clock.slice(1)+" ("+tz+")";
    return clock.charAt(0).toUpperCase()+clock.slice(1)+" on "+dayPart+" ("+tz+")";
  }
  if(!dayPart||dayPart==="every day") return "Every day at "+clock+" "+tz;
  if(dayPart==="weekdays") return "Weekdays at "+clock+" "+tz;
  if(dayPart==="weekends") return "Weekends at "+clock+" "+tz;
  return dayPart.charAt(0).toUpperCase()+dayPart.slice(1)+" at "+clock+" "+tz;
}
function calWhen(job){
  if(job&&job.when) return job.when;
  return humanizeCron(job&&job.cron, job&&job.timezone);
}
function calStartOfWeek(d){const x=new Date(d);const day=(x.getDay()+6)%7;x.setHours(0,0,0,0);x.setDate(x.getDate()-day);return x;}
function calStartOfMonth(d){const x=new Date(d.getFullYear(),d.getMonth(),1);x.setHours(0,0,0,0);return x;}
function calAddDays(d,n){const x=new Date(d);x.setDate(x.getDate()+n);return x;}
function calIso(d){return new Date(d).toISOString();}
function calRange(){
  if(CAL.view==="month"){
    const start=calStartOfWeek(calStartOfMonth(CAL.anchor));
    return {start, end:calAddDays(start,42)};
  }
  const start=calStartOfWeek(CAL.anchor);
  return {start, end:calAddDays(start,7)};
}
function calTitle(){
  if(CAL.view==="month")return CAL.anchor.toLocaleString(undefined,{month:"long",year:"numeric"});
  const {start,end}=calRange();
  const e=calAddDays(end,-1);
  const a=start.toLocaleDateString(undefined,{month:"short",day:"numeric"});
  const b=e.toLocaleDateString(undefined,{month:"short",day:"numeric",year:"numeric"});
  return a+" – "+b;
}
function openCalendar(){
  const v=$("#views"); if(!v)return;
  v.innerHTML='<div id="calroot">'
    +'<div id="calbar">'
    +'<button class="act" id="calprev" type="button">‹</button>'
    +'<button class="act" id="caltoday" type="button">Today</button>'
    +'<button class="act" id="calnext" type="button">›</button>'
    +'<h2 id="caltitle"></h2>'
    +'<button class="act" id="calweekbtn" type="button">Week</button>'
    +'<button class="act" id="calmonthbtn" type="button">Month</button>'
    +'<button class="act" id="calnew" type="button">+ New</button>'
    +'<button class="act" id="calref" type="button">↻</button>'
    +'</div>'
    +'<div id="calbody"><aside id="calseries"><h3>Series</h3><div id="calserlist"></div></aside><div id="calmain"></div></div>'
    +'</div>'
    +'<div id="caldrawer"><div class="dh"><b id="caldt">Event</b><button class="act" id="caldx" type="button">✕</button></div>'
    +'<div class="db" id="caldb"></div><div class="df" id="caldf"></div></div>';
  $("#calprev").onclick=()=>{CAL.anchor=calAddDays(CAL.anchor,CAL.view==="month"?-28:-7);loadCalendar();};
  $("#calnext").onclick=()=>{CAL.anchor=calAddDays(CAL.anchor,CAL.view==="month"?28:7);loadCalendar();};
  $("#caltoday").onclick=()=>{CAL.anchor=new Date();loadCalendar();};
  $("#calweekbtn").onclick=()=>{CAL.view="week";loadCalendar();};
  $("#calmonthbtn").onclick=()=>{CAL.view="month";loadCalendar();};
  $("#calnew").onclick=()=>calOpenDrawer(null,true);
  $("#calref").onclick=()=>loadCalendar();
  $("#caldx").onclick=()=>calCloseDrawer();
  loadCalendar();
}
async function loadCalendar(){
  // Skeleton / stale-while-revalidate: paint chrome immediately (CHATS/ORPHANS pattern).
  // Prior jobs stay visible while the range fetch runs — never blank the rail on nav.
  CAL.loading=true;
  const gen=++CAL.gen;
  renderCalendar();
  const {start,end}=calRange();
  const q="/api/schedule?from="+encodeURIComponent(calIso(start))+"&to="+encodeURIComponent(calIso(end));
  try{
    const r=await api(q);
    if(gen!==CAL.gen) return; // superseded by a newer nav/refresh
    if(r&&r.ok){CAL.jobs=r.jobs||[];CAL.events=r.events||[];}
  }catch(e){
    if(gen!==CAL.gen) return;
    if(!CAL.jobs.length){CAL.jobs=[];CAL.events=[];}
  }
  if(gen!==CAL.gen) return;
  CAL.loading=false;
  renderCalendar();
}
function calVisibleEvents(){
  return (CAL.events||[]).filter(ev=>!CAL.hidden[ev.job_id]);
}
function calSkeletonSeries(){
  return [0,1,2,3,4,5].map(()=>'<div class="cal-skel cal-skel-ser"></div>').join("");
}
function calSkeletonMain(){
  // Lightweight grid placeholder — matches week columns so layout does not jump.
  let html='<div id="calweek"><div class="hd"></div>';
  for(let i=0;i<7;i++) html+='<div class="hd"><div class="cal-skel" style="height:12px;margin:4px auto;width:70%"></div></div>';
  html+='<div class="gutter">all</div>';
  for(let i=0;i<7;i++){
    html+='<div class="cell">';
    for(let j=0;j<3;j++) html+='<div class="cal-skel cal-skel-cell"></div>';
    html+='</div>';
  }
  html+='</div>';
  return html;
}
function renderCalendar(){
  const title=$("#caltitle"), main=$("#calmain"), ser=$("#calserlist");
  if(!main)return;
  if(title)title.textContent=calTitle()+(CAL.loading?" …":"");
  const wbtn=$("#calweekbtn"), mbtn=$("#calmonthbtn");
  if(wbtn)wbtn.classList.toggle("on",CAL.view==="week");
  if(mbtn)mbtn.classList.toggle("on",CAL.view==="month");
  // series checklist — title + plain-English when (cron de-emphasized)
  if(ser){
    if(CAL.loading&&!CAL.jobs.length){
      ser.innerHTML=calSkeletonSeries();
    }else if(!CAL.jobs.length){
      ser.innerHTML='<div style="font-size:12px;color:var(--text-dim);padding:8px">No series yet. Click <b>+ New</b>.</div>';
    }else{
      ser.innerHTML=CAL.jobs.map(j=>{
        const c=calColor(j.id), off=!!CAL.hidden[j.id]||!j.enabled;
        const when=calWhen(j);
        return '<div class="calser'+(off?" off":"")+'" data-id="'+esc(j.id)+'" title="'+esc((j.title||j.id)+" — "+when+(j.cron?" · "+j.cron:""))+'">'
          +'<span class="dot" style="background:'+c+'"></span>'
          +'<div class="calser-body">'
          +'<div class="calser-title">'+esc(j.title||j.id)+(j.enabled?"":' <span style="font-size:9px;opacity:.7;font-weight:400">off</span>')+'</div>'
          +'<div class="calser-when">'+esc(when)+'</div>'
          +'<div class="calser-cron">'+esc(j.cron||"")+'</div>'
          +'</div></div>';
      }).join("");
      ser.querySelectorAll(".calser").forEach(el=>el.onclick=()=>{
        const id=el.dataset.id; CAL.hidden[id]=!CAL.hidden[id]; renderCalendar();
      });
    }
  }
  // Main grid: skeleton only on cold load; keep prior events while refreshing a new range.
  if(CAL.loading&&!CAL.jobs.length&&!CAL.events.length){
    main.innerHTML=calSkeletonMain();
    return;
  }
  if(CAL.view==="month") renderCalMonth(main);
  else renderCalWeek(main);
}
function renderCalWeek(main){
  const {start}=calRange();
  const hours=[7,8,9,10,11,12,13,14,15,16,17,18];
  const days=[...Array(7)].map((_,i)=>calAddDays(start,i));
  const today=new Date(); today.setHours(0,0,0,0);
  let html='<div id="calweek"><div class="hd"></div>';
  days.forEach(d=>{
    const isT=d.getTime()===today.getTime();
    html+='<div class="hd'+(isT?" today":"")+'">'+d.toLocaleDateString(undefined,{weekday:"short",month:"numeric",day:"numeric"})+'</div>';
  });
  html+='<div class="gutter">all</div>';
  days.forEach(d=>{
    const isT=d.getTime()===today.getTime();
    const dayEv=calVisibleEvents().filter(ev=>{
      const s=new Date(ev.start); return s.toDateString()===d.toDateString();
    });
    html+='<div class="cell'+(isT?" today":"")+'" data-day="'+d.toISOString()+'">';
    dayEv.forEach(ev=>{
      const s=new Date(ev.start);
      const t=s.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});
      html+='<div class="calev" data-eid="'+esc(ev.id)+'" style="background:'+calColor(ev.job_id)+'" title="'+esc(ev.title+" · "+(ev.when||calWhen(ev)))+'">'
        +esc(t+" "+ev.title)+'</div>';
    });
    html+='</div>';
  });
  hours.forEach(h=>{
    html+='<div class="gutter">'+((h%12)||12)+(h<12?"a":"p")+'</div>';
    days.forEach(d=>{
      const isT=d.getTime()===today.getTime();
      html+='<div class="cell'+(isT?" today":"")+'" style="min-height:28px"></div>';
    });
  });
  html+='</div>';
  main.innerHTML=html;
  main.querySelectorAll(".calev").forEach(el=>el.onclick=e=>{e.stopPropagation();
    const ev=CAL.events.find(x=>x.id===el.dataset.eid); if(ev) calOpenDrawer(ev,false);});
}
function renderCalMonth(main){
  const {start}=calRange();
  const today=new Date(); today.setHours(0,0,0,0);
  const mon=CAL.anchor.getMonth();
  let html='<div id="calmonth">';
  ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"].forEach(n=>html+='<div class="mhd">'+n+'</div>');
  for(let i=0;i<42;i++){
    const d=calAddDays(start,i);
    const isT=d.getTime()===today.getTime();
    const out=d.getMonth()!==mon;
    const dayEv=calVisibleEvents().filter(ev=>new Date(ev.start).toDateString()===d.toDateString());
    html+='<div class="mcell'+(out?" out":"")+(isT?" today":"")+'"><div class="mdn">'+d.getDate()+'</div>';
    dayEv.slice(0,4).forEach(ev=>{
      html+='<div class="calev" data-eid="'+esc(ev.id)+'" style="background:'+calColor(ev.job_id)+'" title="'+esc(ev.when||calWhen(ev))+'">'
        +esc(ev.title)+'</div>';
    });
    if(dayEv.length>4) html+='<div style="font-size:10px;color:var(--text-dim)">+'+ (dayEv.length-4)+' more</div>';
    html+='</div>';
  }
  html+='</div>';
  main.innerHTML=html;
  main.querySelectorAll(".calev").forEach(el=>el.onclick=e=>{e.stopPropagation();
    const ev=CAL.events.find(x=>x.id===el.dataset.eid); if(ev) calOpenDrawer(ev,false);});
}
function calCloseDrawer(){const d=$("#caldrawer"); if(d)d.classList.remove("open"); CAL.selected=null;}
function calOpenDrawer(ev, isNew){
  const d=$("#caldrawer"), db=$("#caldb"), df=$("#caldf"), dh=$("#caldt");
  if(!d||!db||!df)return;
  d.classList.add("open");
  const job=ev?CAL.jobs.find(j=>j.id===ev.job_id):null;
  // Desk default: weekdays only (no Sat/Sun fires). Cron DOW 1-5 = Mon–Fri.
  const model=isNew?{id:"",title:"",cron:"0 7 * * 1-5",target:"",message:"",timezone:"America/Chicago",enabled:true}
    :{id:job?.id||ev.job_id, title:job?.title||ev.title, cron:job?.cron||ev.cron, target:job?.target||ev.target,
      message:job?.message||ev.message||"", timezone:job?.timezone||ev.timezone||"America/Chicago",
      enabled:job?!!job.enabled:true};
  CAL.selected=model;
  if(dh) dh.textContent=isNew?"New scheduled message":(model.title||model.id);
  db.innerHTML=
    '<label>Title<input id="cf_title" value="'+esc(model.title||"")+'"></label>'
    +'<label>Job id<input id="cf_id" value="'+esc(model.id||"")+'" '+(isNew?"":"readonly")+' placeholder="auto if blank"></label>'
    +'<div id="cf_when" style="font-size:13px;font-weight:600;color:var(--amber);padding:8px 10px;background:var(--bg);border:1px solid var(--line);border-radius:4px"></div>'
    +'<label>Cron (advanced)<input id="cf_cron" value="'+esc(model.cron||"")+'" placeholder="0 7 * * 1-5"></label>'
    +'<div style="font-size:11px;color:var(--text-dim);margin-top:-6px">Sidebar speaks the amber line above — not the cron. Desk default is <b>weekdays</b>.</div>'
    +'<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:2px">'
    +[["0 7 * * 1-5","Weekdays 7am"],["0 9 * * 1-5","Weekdays 9am"],["0 7 * * *","Daily 7am (7 days)"],["0 8 * * 1","Mondays 8am"],["0 */6 * * 1-5","Every 6h weekdays"]]
      .map(([c,l])=>'<button type="button" class="act cf_preset" data-c="'+c+'">'+l+'</button>').join("")
    +'</div>'
    +'<label>Timezone<input id="cf_tz" value="'+esc(model.timezone||"America/Chicago")+'"></label>'
    +'<label>Target session (registry name)<input id="cf_target" value="'+esc(model.target||"")+'" placeholder="northstar-iran-daily"></label>'
    +'<label>Message<textarea id="cf_msg">'+esc(model.message||"")+'</textarea></label>'
    +'<label style="flex-direction:row;align-items:center;gap:8px;text-transform:none;letter-spacing:0;font-size:12px;color:var(--text)">'
    +'<input type="checkbox" id="cf_en" '+(model.enabled?"checked":"")+'> Enabled</label>'
    +(ev&&ev.start?'<div style="color:var(--text-dim);font-size:11px">Occurrence: '+esc(new Date(ev.start).toLocaleString())+'</div>':'');
  const syncWhen=()=>{
    const el=$("#cf_when"); if(!el) return;
    el.textContent=humanizeCron(($("#cf_cron")||{}).value, ($("#cf_tz")||{}).value);
  };
  db.querySelectorAll(".cf_preset").forEach(b=>b.onclick=()=>{$("#cf_cron").value=b.dataset.c; syncWhen();});
  const cronIn=$("#cf_cron"), tzIn=$("#cf_tz");
  if(cronIn) cronIn.addEventListener("input", syncWhen);
  if(tzIn) tzIn.addEventListener("input", syncWhen);
  syncWhen();
  df.innerHTML=
    '<button class="act" id="cf_save" type="button">'+(isNew?"Create":"Save")+'</button>'
    +(isNew?"":'<button class="act" id="cf_run" type="button">Run now</button>')
    +(isNew?"":'<button class="act danger" id="cf_del" type="button">Delete series</button>')
    +'<button class="act" id="cf_cancel" type="button">Close</button>';
  $("#cf_cancel").onclick=()=>calCloseDrawer();
  $("#cf_save").onclick=async()=>{
    const body={
      id:($("#cf_id").value||"").trim()||undefined,
      title:($("#cf_title").value||"").trim(),
      cron:($("#cf_cron").value||"").trim(),
      timezone:($("#cf_tz").value||"America/Chicago").trim(),
      target:($("#cf_target").value||"").trim(),
      message:($("#cf_msg").value||""),
      enabled:!!$("#cf_en").checked,
    };
    if(!body.cron||!body.target||!body.message){alert("cron, target, and message are required");return;}
    let r;
    if(isNew) r=await api("/api/schedule",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    else r=await api("/api/schedule/update",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:model.id,...body})});
    if(!r||!r.ok){alert((r&&r.error)||"save failed");return;}
    calCloseDrawer(); loadCalendar();
  };
  const runB=$("#cf_run"); if(runB) runB.onclick=async()=>{
    const r=await api("/api/schedule/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:model.id})});
    alert(r&&r.ok?("Fired → "+(r.target||model.target)):("Fire failed: "+((r&&r.error)||"unknown")));
    loadCalendar();
  };
  const delB=$("#cf_del"); if(delB) delB.onclick=async()=>{
    if(!confirm("Delete series "+model.id+"?"))return;
    await api("/api/schedule/delete",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({id:model.id})});
    calCloseDrawer(); loadCalendar();
  };
}

function render(){
  if(mode==="grid")renderGrid();
  else if(mode==="groups")renderGroups();
  else if(mode==="activity")renderActivity();
  else if(mode==="flow")renderFlow();
  // orphans/chats/calendar are manual: the 2s poll must not rebuild them mid-click
  else if(mode==="orphans"){if(!document.getElementById("mvhosts"))openOrphans();}
  else if(mode==="chats"){if(!document.getElementById("chbar"))openChats();}
  else if(mode==="calendar"){if(!document.getElementById("calroot"))openCalendar();}
  // Manager quiet + empty grid: show a calm empty state (not 40 offline tiles).
  if(isManagerQuiet()&&mode==="grid"){
    const v=$("#views");
    if(v&&!shown().filter(s=>s.live).length&&!v.querySelector(".mgr-empty")){
      // renderGrid may have filled tiles; if zero live scoped, reinforce empty message
      if(!shown().length){
        v.innerHTML='<div class="mgr-empty" style="padding:2rem;max-width:36rem;color:var(--text-dim);font-size:13px;line-height:1.5">'
          +'<div style="color:var(--amber);font-weight:700;letter-spacing:1px;margin-bottom:8px">MANAGER SCOPE</div>'
          +'Primary surface is the <b>managed planes</b> strip (left). '
          +'Local session list is quiet on purpose — only standing health / manager-tagged work. '
          +'Worker fleets live in <b>amux / gmux / reevux</b> rooms. '
          +'<br><br><span class="tagchip" style="cursor:pointer" onclick="setManagerFleet(true)">show full fleet…</span>'
          +' · <a href="__PUBLIC_PATH__/ai.html" style="color:var(--amber)">diagnosis /ai</a>'
          +'</div>';
      }
    }
  }
}

function pinned(){const c=$("#head");return c.scrollHeight-c.scrollTop-c.clientHeight<60;}
function scrollBottom(){const c=$("#head");c.scrollTop=c.scrollHeight;$("#jump").style.display="none";}
function clockNow(){const d=new Date();return d.toTimeString().slice(0,5);}  // HH:MM (#10)

function addBubble(cls,who,text){
  const b=document.createElement("div");b.className="bubble "+cls;
  if(who){
    const w=document.createElement("div");w.className="who";
    w.textContent=(cls==="user")?who+" · "+clockNow():who;   // timestamp user bubbles (#10)
    b.appendChild(w);
  }
  const t=document.createElement("div");t.innerHTML=linkifyLinear(text);b.appendChild(t);
  bindLinlinks(b);
  const c=$("#head");
  if(screenEl&&screenEl.parentElement===c){c.insertBefore(b,screenEl);}else{c.appendChild(b);}
  scrollBottom();
}

async function refreshScreen(){
  if(!current)return;
  const wasPinned=pinned();
  try{
    const r=await api("/api/capture?session="+encodeURIComponent(current.session)+"&lines=400");
    const s=$("#screen");if(!s)return;
    if(r.ok){
      $("#status").textContent="live · polling";$("#status").className="";
      s.classList.remove("dimmed");
      if(s.dataset.last!==r.content){
        s.dataset.last=r.content;
        s.textContent=r.content.replace(/\s+$/,"")+"\n";
        const cur=document.createElement("span");cur.className="cursorblock";s.appendChild(cur);
        if(document.hidden){flashOn=true;document.title="● __PRODUCT__ — "+current.name;}  // title flash (#20)
        if(wasPinned)scrollBottom();else $("#jump").style.display="block";          // jump pill (#11)
      }
    }else{
      $("#status").textContent=r.error||"capture failed";$("#status").className="err";
      s.classList.add("dimmed");
    }
  }catch(e){$("#status").textContent="daemon unreachable";$("#status").className="err";}
}

function openHead(sess){
  current=sess;
  setMode("head");
  $("#title").textContent="HEAD · "+sess.name;
  $("#status").textContent="connecting…";$("#status").className="";
  const c=$("#head");c.innerHTML="";screenEl=null;
  screenEl=document.createElement("div");screenEl.id="screen-bubble";screenEl.className="bubble";
  screenEl.innerHTML='<div class="who"></div><div id="screen"></div>';
  screenEl.querySelector(".who").textContent=sess.name+" · live head (updates in place)";
  c.appendChild(screenEl);
  applyWrap();
  addBubble("sys",null,"head on session “"+sess.session+"”"+(sess.description?" — "+sess.description:"")+" — type to drive");
  document.querySelectorAll(".card").forEach(el2=>el2.classList.toggle("active",el2.dataset.name===sess.name));
  refreshScreen();chatTimer=setInterval(refreshScreen,1500);
  $("#input").focus();
}

async function sendText(){
  const inp=$("#input");const text=inp.value;
  if(!current||!text)return;
  inp.value="";addBubble("user","you",text);
  let ok=false;
  try{const r=await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:current.session,keys:text,literal:true,enter:true})});ok=!!(r&&r.ok);}catch(e){}
  if(!ok){addBubble("sys",null,"send failed — draft restored");if(!inp.value)inp.value=text;}
  setTimeout(refreshScreen,300);
}

// wrap toggle (#9)
function applyWrap(){
  const s=$("#screen");if(!s)return;
  const off=localStorage.getItem("emux_wrap")==="off";
  s.classList.toggle("nowrap",off);
  $("#wrapchip").textContent="WRAP: "+(off?"OFF":"ON");
}
$("#wrapchip").onclick=()=>{
  localStorage.setItem("emux_wrap",localStorage.getItem("emux_wrap")==="off"?"on":"off");applyWrap();
};

$("#send").onclick=sendText;
$("#input").addEventListener("keydown",e=>{if(e.key==="Enter")sendText();});
document.querySelectorAll("#chips .chip").forEach(ch=>{
  if(ch.id==="wrapchip")return;
  ch.onclick=async()=>{
    if(!current)return;
    addBubble("user","key",ch.textContent);
    await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:current.session,keys:ch.dataset.keys,literal:false,enter:false})});
    setTimeout(refreshScreen,300);
  };
});

// copy attach command (#12)
$("#attachbtn").onclick=()=>{
  if(!current)return;
  navigator.clipboard?.writeText("tmux attach -t "+current.session);
  const b=$("#attachbtn");const o=b.textContent;b.textContent="⧉ copied!";setTimeout(()=>b.textContent=o,1200);
};
// manual refresh (#15)
$("#refreshbtn").onclick=()=>{poll();if(current)refreshScreen();};
// jump to bottom pill (#11)
$("#jump").onclick=scrollBottom;
$("#head").addEventListener("scroll",()=>{if(pinned())$("#jump").style.display="none";});
// sidebar filter (#7)
$("#filter").addEventListener("input",e=>{filterStr=e.target.value.toLowerCase();renderSidebar();if(mode!=="head")render();syncURL();});
// ---------- zoom-in steer modal ----------
let modalSession=null, modalTimer=null;
let digestErr=false, digestRetries=0;   // The Gist: recover from a failed summarize when the pane changes, capped at 10
function modalScreenPinned(sc){
  if(!sc)return true;
  // 80px tolerance: live panes jitter; don't yank the user for a few pixels
  return sc.scrollHeight-sc.scrollTop-sc.clientHeight<80;
}
function updateModalJump(){
  const sc=$("#modalscreen"), j=$("#modaljump");
  if(!sc||!j)return;
  const show=modalSession&&!modalScreenPinned(sc)&&sc.scrollHeight>sc.clientHeight+40;
  j.classList.toggle("on",!!show);
}
function modalScrollBottom(){
  const sc=$("#modalscreen");if(!sc)return;
  sc.scrollTop=sc.scrollHeight;updateModalJump();
}
// Rolling book of pane snapshots so HTML can scroll even when a single capture
// is only one screen (Claude alt-screen has history_size=0).
let modalBook=[];          // oldest → newest unique captures
let modalScrollMode="app"; // "app" | "tmux" from last capture
let modalHistoryText="";   // durable chat transcript (disk) — makes HTML scroll real
const MODAL_BOOK_MAX=40;
function modalRenderBook(sc,atBottom,keepTop){
  const sep="\n\n─── earlier view ───\n\n";
  const live=modalBook.length?modalBook.join(sep):"";
  let body="";
  if(modalHistoryText){
    body+="══ conversation history (scroll up) ══\n\n"+modalHistoryText.trim()+"\n\n";
    body+="══ live pane ══\n\n";
  }
  body+=live+"\n";
  sc.textContent=body.replace(/\s+$/,"")+"\n";
  const cur=document.createElement("span");cur.className="cursorblock";sc.appendChild(cur);
  if(atBottom)sc.scrollTop=sc.scrollHeight;
  else sc.scrollTop=keepTop;
}
async function loadModalHistory(s){
  // Pull durable transcript so the modal is always HTML-scrollable even when
  // the agent is full-screen with zero tmux history.
  modalHistoryText="";
  try{
    const name=String(s.name||s.session||"");
    const tags=(s.tags||[]).map(t=>String(t));
    let tool=tags.some(t=>/^grok$/i.test(t)||/^gsid:/i.test(t))?"grok":"claude";
    if(/grok/i.test(name))tool="grok";
    let sid="";
    for(const t of tags){
      if(/^csid:/i.test(t)||/^gsid:/i.test(t)){sid=t.split(":").slice(1).join(":");break;}
    }
    // Full UUID in name/path
    if(!sid){
      const um=name.match(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
      if(um)sid=um[0];
    }
    // chat-claude-4149632f / chat-grok-… short prefix → search store
    let q=name;
    const sm=name.match(/chat-(?:claude|grok)-([0-9a-f]{6,})/i);
    if(sm)q=sm[1];
    if(!sid){
      // Try a few queries — store match is substring on title/cwd/id
      for(const qq of [q, sm?sm[1]:null, name.slice(0,24)].filter(Boolean)){
        const list=await api("/api/chats?match=all&limit=20&tools="+tool+","+(tool==="claude"?"grok":"claude")
          +"&q="+encodeURIComponent(qq));
        const rows=(list&&list.chats)||[];
        if(!rows.length)continue;
        const hit=rows.find(c=>c.fleet_name===name)
          ||rows.find(c=>c.session_id&&name.includes(String(c.session_id).slice(0,8)))
          ||rows.find(c=>String(c.session_id||"").startsWith(qq))
          ||rows[0];
        if(hit&&hit.session_id){
          sid=hit.session_id;
          if(hit.tool)tool=hit.tool;
          break;
        }
      }
    }
    if(!sid)return;
    const peek=await api("/api/chats/peek?tool="+encodeURIComponent(tool)
      +"&session_id="+encodeURIComponent(sid)+"&turns=16");
    if(!peek||!peek.ok)return;
    const turns=peek.turns||[];
    const lines=[];
    for(const t of turns){
      const role=(t.role||"?").toString().toUpperCase();
      const text=(t.text||"").toString().trim();
      if(!text)continue;
      lines.push(role+":\n"+text.slice(0,4000));
    }
    if(lines.length){
      modalHistoryText=lines.join("\n\n");
      // Bust live cache so next render includes history header
      const sc=$("#modalscreen");if(sc)sc.dataset.last="";
    }
  }catch(e){/* history is best-effort */}
}
function openModal(s){
  document.body.classList.remove("nav-open");   // mobile: dismiss the session drawer
  modalSession=s;
  digestErr=false;digestRetries=0;
  modalBook=[];modalScrollMode="app";modalHistoryText="";
  $("#modalname").textContent=s.name;
  $("#modalagent").innerHTML=agentHTML(s);
  $("#modalstatus").textContent="connecting…";$("#modalstatus").style.color="";
  const sc=$("#modalscreen");sc.textContent="";sc.dataset.last="";sc.dataset.userPinned="0";
  $("#modaldigest").className="";$("#modaldigest .dgtext").textContent="";$("#modaldigest .dgsugg").innerHTML="";
  setPending("");$("#modalthink").className="";clearModalClips();
  tOpts=[];tSugg=[];tLoggedDigest="";
  sideChats=[];sideChatSeq=0;
  switchArmed=null;$("#modalswitch").textContent="⇄ switch account";$("#modalswitch").className="";
  $("#modalswitch").classList.toggle("hot",!!s.cost);   // highlight when this session is throttled
  // Tasks drawer: prefer remembered open state; default open
  try{modalTasksOpen=localStorage.getItem("emux_modal_tasks")!=="0";}catch(_){modalTasksOpen=true;}
  toggleModalTasks(modalTasksOpen);
  renderModalTasks();
  ensureReplySideChat();
  renderSideChats();
  $("#modal").classList.add("open");
  updateModalJump();
  // Live poll + async history (when history lands, re-render with scrollable transcript)
  modalRefresh();clearInterval(modalTimer);modalTimer=setInterval(modalRefresh,1200);
  loadModalHistory(s).then(()=>{
    if(!modalSession)return;
    if(modalHistoryText){
      const sc2=$("#modalscreen");
      if(sc2){
        sc2.dataset.last=""; // force book re-render on next tick too
        modalRenderBook(sc2,true,0);
        updateModalJump();
        const st=$("#modalstatus");
        if(st&&!(st.textContent||"").includes("history")){
          st.textContent=(st.textContent||"live")+" · +history";
        }
      }
    }
    renderModalTasks(); // history often holds the issue keys
  });
  loadDigest();                                  // the gist + suggested replies, up front
  syncURL();                                      // deep-link the open session
  setTimeout(()=>$("#modalinput").focus(),40);
}
function closeModal(){
  $("#modal").classList.remove("open");
  const j=$("#modaljump");if(j)j.classList.remove("on");
  clearInterval(modalTimer);modalTimer=null;modalSession=null;clearModalClips();
  modalBook=[];modalHistoryText="";
  sideChats=[];sideChatSeq=0;
  const stack=$("#tchatstack");if(stack)stack.innerHTML="";
  syncURL();                                      // drop the session from the URL
}
async function modalRefresh(){
  if(!modalSession)return;
  const sc=$("#modalscreen");
  // Honor explicit scroll-up: once user leaves the bottom, stay there across
  // live captures until they jump back (or scroll to bottom themselves).
  const userPinned=sc.dataset.userPinned==="1";
  const atBottom=!userPinned&&modalScreenPinned(sc);
  const keepTop=sc.scrollTop;
  try{
    // Deep history when tmux has it; still fine for alt-screen (returns ~pane height).
    const r=await api("/api/capture?session="+encodeURIComponent(modalSession.session)+"&lines=4000");
    if(r.ok){
      modalScrollMode=r.scroll_mode|| (r.history_size>0?"tmux":"app");
      const hist=typeof r.history_size==="number"?r.history_size:null;
      const modeTag=modalScrollMode==="tmux"?" · tmux history":" · in-app scroll";
      $("#modalstatus").textContent="live"+(hist!=null?" · hist "+hist:"")+modeTag;
      $("#modalstatus").style.color="";
      const raw=(r.content||"").replace(/\s+$/,"");
      if(sc.dataset.last!==raw){
        sc.dataset.last=raw;
        // Accumulate unique snapshots → real HTML scroll depth over time
        if(raw&&(modalBook.length===0||modalBook[modalBook.length-1]!==raw)){
          modalBook.push(raw);
          if(modalBook.length>MODAL_BOOK_MAX)modalBook=modalBook.slice(-MODAL_BOOK_MAX);
        }
        modalRenderBook(sc,atBottom,keepTop);
        renderModalTasks(); // re-scan pane for new TEAM-123 keys
        // The Gist failed but the web changed — ok to try to recover, capped at 10
        if(digestErr&&digestRetries<10){digestRetries++;loadDigest();}
      }
      renderOptions(r.options);   // a menu on screen → clickable bubbles
      updateThinking(r.thinking); // movement + timer while it generates
      // pending send has landed once the pane echoes it → clear the holding bubble
      if(pendingText&&r.content&&r.content.indexOf(pendingText.trim().slice(0,40))>=0)setPending("");
    }else{$("#modalstatus").textContent=r.error||"error";$("#modalstatus").style.color="var(--stale)";}
  }catch(e){$("#modalstatus").textContent="unreachable";$("#modalstatus").style.color="var(--stale)";}
  updateModalJump();
  modalJudge();
}
// the gist: a reader's-digest of the session + clickable suggested replies, so
// you don't have to read a wall of text and invent a response.
async function loadDigest(){
  if(!modalSession)return;
  const el=$("#modaldigest");el.className="on loading";
  $("#modaldigest .dgtext").textContent="reading the session…";
  $("#modaldigest .dgsugg").innerHTML="";
  const sess=modalSession.session;
  const r=await api("/api/reply",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:sess})});
  if(!modalSession||modalSession.session!==sess)return;   // modal changed while thinking
  el.className="on";
  if(!r.ok){
    digestErr=true;   // stuck — modalRefresh will retry (≤10x) when the pane content next changes
    const more=digestRetries<10?" · will retry when the session moves":"";
    $("#modaldigest .dgtext").textContent="(couldn't summarize — "+(r.error||"")+")"+more;return;}
  digestErr=false;digestRetries=0;   // recovered
  const dgt=$("#modaldigest .dgtext");
  if(dgt){dgt.innerHTML=linkifyLinear(r.digest||"(nothing notable)");bindLinlinks(dgt);}
  renderModalTasks(); // gist often names the Linear issue
  if(r.digest&&r.digest!==tLoggedDigest){tchatLog("bot",r.digest);tLoggedDigest=r.digest;}  // the gist opens the chat
  setSuggestions(r.suggestions||[]);   // gist replies become chips (with confidence pies)
}
// Side chats: many floating panels per open session.
// - kind "reply": default "reply to <session>" with menu chips + gist suggestions
// - kind "task"/"topic": chat about a Linear issue or freeform topic (as many as you want)
// Messages inject into the parent head with a topic prefix (unless you spawn a fresh Claude seat).
let tOpts=[], tSugg=[], tLoggedDigest="";
let sideChats=[]; // {id, kind, title, topic, collapsed, messages:[{who,text}], seatName?}
let sideChatSeq=0;
function pieHTML(pct){const p=Math.round(pct);
  return '<span class="cpie" title="'+p+'% confidence" style="background:conic-gradient(#2ea043 '+p+'%,rgba(127,127,127,.26) 0)"></span>'
    +'<span class="cpct">'+p+'%</span>';}
function ensureReplySideChat(){
  if(!modalSession) return null;
  let sc=sideChats.find(c=>c.kind==="reply");
  if(!sc){
    sc={id:"reply",kind:"reply",title:"reply to "+modalSession.name,topic:null,collapsed:true,messages:[]};
    sideChats.unshift(sc);
  }else{
    sc.title="reply to "+modalSession.name;
  }
  return sc;
}
function openSideChat(opts){
  // opts: {kind, title, topic, seed?, focus?}
  if(!modalSession) return null;
  const kind=opts.kind||"topic";
  const topic=(opts.topic||"").trim()||null;
  // Reuse open panel for same task/topic
  if(topic){
    const existing=sideChats.find(c=>c.topic===topic);
    if(existing){
      existing.collapsed=false;
      renderSideChats();
      focusSideChat(existing.id);
      return existing;
    }
  }
  if(kind==="reply"){
    const sc=ensureReplySideChat();
    sc.collapsed=false;
    renderSideChats();
    focusSideChat(sc.id);
    return sc;
  }
  const id="sc-"+(++sideChatSeq);
  const title=opts.title||(topic?("about "+topic):("side chat "+sideChatSeq));
  const sc={id,kind,title,topic,collapsed:false,messages:[],seatName:null};
  if(opts.seed) sc.messages.push({who:"bot",text:opts.seed});
  else if(topic&&isLinearIssueKey(topic)){
    sc.messages.push({who:"bot",text:
      "Side chat about Linear "+topic+".\n"
      +linearIssueUrl(topic)+"\n\n"
      +"Messages you type are sent into the parent head ("+(modalSession.name||"?")
      +") tagged with this issue. Use “Fresh Claude” for a dedicated seat."});
  }else if(topic){
    sc.messages.push({who:"bot",text:"Side chat about “"+topic+"”. Messages go to the parent head with this topic tag."});
  }
  sideChats.push(sc);
  renderSideChats();
  focusSideChat(id);
  return sc;
}
function closeSideChat(id){
  if(id==="reply"){
    const sc=sideChats.find(c=>c.id==="reply");
    if(sc){sc.collapsed=true;renderSideChats();}
    return;
  }
  sideChats=sideChats.filter(c=>c.id!==id);
  renderSideChats();
}
function focusSideChat(id){
  const el=document.querySelector('.tchat[data-id="'+id+'"] .tchatinput');
  if(el) setTimeout(()=>el.focus(),40);
}
function sideChatById(id){return sideChats.find(c=>c.id===id);}
function tchatLog(who,text,chatId){
  // Back-compat: log into reply sidechat (or explicit id)
  const sc=sideChatById(chatId||"reply")||ensureReplySideChat();
  if(!sc||!text) return;
  sc.messages.push({who,text:String(text)});
  // live-append if panel is mounted
  const log=document.querySelector('.tchat[data-id="'+sc.id+'"] .tchatlog');
  if(log){
    const b=document.createElement("div");b.className="tcmsg "+who;
    b.innerHTML=linkifyLinear(text);bindLinlinks(b);
    log.appendChild(b);log.style.display="";log.scrollTop=log.scrollHeight;
  }else renderSideChats();
}
function renderOptions(opts){ tOpts=opts||[]; const sc=ensureReplySideChat(); if(sc&&(tOpts.length||tSugg.length)) sc.collapsed=false; renderSideChats(); }
function setSuggestions(sugg){ tSugg=sugg||[]; const sc=ensureReplySideChat(); if(sc&&(tOpts.length||tSugg.length)) sc.collapsed=false; renderSideChats(); }
async function sendOption(target){
  const cur=((tOpts.find(o=>o.selected))||tOpts[0]||{n:target}).n;
  const send=k=>api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:modalSession.session,keys:k,literal:false,enter:false})});
  const arrow=target>=cur?"Down":"Up";
  for(let i=0;i<Math.abs(target-cur);i++){await send(arrow);await new Promise(r=>setTimeout(r,90));}
  await send("Enter");
  setTimeout(()=>{modalRefresh();loadDigest();},600);
}
function composeSidePayload(sc, text){
  if(!sc||sc.kind==="reply"||!sc.topic) return text;
  if(isLinearIssueKey(sc.topic))
    return "[side chat · Linear "+sc.topic+"] "+text;
  return "[side chat · "+sc.topic+"] "+text;
}
function renderSideChats(){
  const stack=$("#tchatstack"); if(!stack) return;
  if(!modalSession){stack.innerHTML="";return;}
  ensureReplySideChat();
  let html="";
  // FAB: add another freeform side chat
  html+='<button type="button" id="tchat-addfab" title="New side chat about anything">+</button>';
  sideChats.forEach(sc=>{
    const isReply=sc.kind==="reply";
    html+='<div class="tchat'+(sc.collapsed?" collapsed":"")+'" data-id="'+esc(sc.id)+'">';
    // collapsed = floating bubble
    html+='<button type="button" class="tchatlauncher" data-act="expand" title="'+esc(sc.title)+'">'
      +(isReply?"💬":"◎")
      +'<span class="tchatbadge"'+(isReply&&(tOpts.length+tSugg.length)?"":' style="display:none"')+'>'
      +(isReply?(tOpts.length+tSugg.length):"")+'</span></button>';
    html+='<div class="tchatpanel">';
    html+='<div class="tchathead"><span class="th-title">'+(isReply?'💬 reply to <b>'+esc(modalSession.name)+'</b>':'◎ '+esc(sc.title))+'</span>'
      +'<span class="th-acts">'
      +(sc.topic&&isLinearIssueKey(sc.topic)?'<button type="button" data-act="linear" title="Open in Linear">↗</button>':"")
      +'<button type="button" data-act="min" title="minimize">▾</button>'
      +'<button type="button" data-act="close" title="'+(isReply?"minimize":"close side chat")+'">✕</button>'
      +'</span></div>';
    html+='<div class="tchatlog">';
    (sc.messages||[]).forEach(m=>{
      html+='<div class="tcmsg '+esc(m.who||"bot")+'">'+linkifyLinear(m.text||"")+'</div>';
    });
    html+='</div>';
    if(isReply){
      html+='<div class="tchatchips"></div>';
    }
    html+='<div class="tchatrow">'
      +'<input class="tchatinput" placeholder="'+(isReply?"choose one, or type your reply…":"message about "+esc(sc.topic||"this")+"…")+'" autocomplete="off" spellcheck="false">'
      +'<button type="button" class="tchatsend" data-act="send">➤</button></div>';
    if(!isReply){
      html+='<div class="tchatfoot">'
        +'<button type="button" class="act" data-act="fresh" title="Spawn a dedicated Claude Code seat for this topic">⧉ Fresh Claude</button>'
        +(sc.seatName?'<button type="button" class="act" data-act="openseat" title="Open the seat">↗ '+esc(sc.seatName)+'</button>':"")
        +'</div>';
    }
    html+='</div></div>';
  });
  stack.innerHTML=html;
  // reply chips
  const replyEl=stack.querySelector('.tchat[data-id="reply"] .tchatchips');
  if(replyEl){
    const parts=[];
    tOpts.forEach(o=>parts.push('<button class="tc-opt'+(o.selected?" sel":"")+'" data-n="'+o.n+'"><b>'+o.n+'</b> '+esc(o.label)+'</button>'));
    tSugg.forEach((s,i)=>{
      const c=Math.max(0,Math.min(100,s.confidence==null?50:s.confidence));
      parts.push('<button class="tc-sug" data-i="'+i+'">'+pieHTML(c)+'<span class="tc-txt">'+esc(s.text||"")+'</span></button>');
    });
    replyEl.innerHTML=parts.join("")||'<div class="tc-empty">no suggestions right now — type below, or + for another side chat</div>';
    replyEl.querySelectorAll(".tc-opt").forEach(b=>b.onclick=()=>{
      replyEl.querySelectorAll("button").forEach(x=>x.disabled=true);sendOption(+b.dataset.n);});
    replyEl.querySelectorAll(".tc-sug").forEach(b=>b.onclick=()=>{
      const t=(tSugg[+b.dataset.i]||{}).text||"";if(!t)return;
      setPending(t);tchatLog("you",t,"reply");modalKeys(t,true,true);
      tSugg=[];renderSideChats();setTimeout(()=>{modalRefresh();loadDigest();},1500);});
  }
  stack.querySelectorAll(".tchat").forEach(el=>{
    const id=el.dataset.id;
    const sc=sideChatById(id);
    if(!sc) return;
    el.querySelectorAll("[data-act]").forEach(btn=>{
      btn.onclick=e=>{
        e.stopPropagation();
        const act=btn.dataset.act;
        if(act==="expand"){sc.collapsed=false;renderSideChats();focusSideChat(id);}
        else if(act==="min"){sc.collapsed=true;renderSideChats();}
        else if(act==="close"){closeSideChat(id);}
        else if(act==="send"){sideChatSend(id);}
        else if(act==="linear"&&sc.topic){window.open(linearIssueUrl(sc.topic),"_blank","noopener,noreferrer");}
        else if(act==="fresh"){spawnSideChatClaude(id);}
        else if(act==="openseat"&&sc.seatName){
          setMode("grid");const s=grid.find(x=>x.name===sc.seatName);if(s)openModal(s);
        }
      };
    });
    const inp=el.querySelector(".tchatinput");
    if(inp){
      inp.onkeydown=e=>{if(e.key==="Enter"){e.preventDefault();sideChatSend(id);}};
      inp.onpaste=e=>{if(modalSession)handleComposerPaste(e,modalSession.session||modalSession.name);};
    }
    const log=el.querySelector(".tchatlog");
    if(log) log.scrollTop=log.scrollHeight;
  });
  const fab=stack.querySelector("#tchat-addfab");
  if(fab) fab.onclick=()=>{
    const topic=prompt("Side chat topic (Linear key like AIC-284, or any label):");
    if(topic==null) return;
    const t=topic.trim();
    if(!t) return;
    openSideChat({
      kind:isLinearIssueKey(t)?"task":"topic",
      topic:t,
      title:isLinearIssueKey(t)?("about "+t):t,
    });
  };
  bindLinlinks(stack);
}
function sideChatSend(id){
  const sc=sideChatById(id); if(!sc||!modalSession) return;
  const el=document.querySelector('.tchat[data-id="'+id+'"] .tchatinput');
  const t=(el&&el.value||"").trim(); if(!t) return;
  if(el) el.value="";
  const payload=composeSidePayload(sc,t);
  setPending(payload.length>180?payload.slice(0,180)+"…":payload);
  tchatLog("you",t,id);
  modalKeys(payload,true,true).then(ok=>{
    if(!ok){
      if(el&&!el.value) el.value=t;
      setPending("");
    }
  });
}
async function spawnSideChatClaude(id){
  const sc=sideChatById(id); if(!sc||!modalSession) return;
  const topic=sc.topic||sc.title||"topic";
  const slug=String(topic).toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-|-$/g,"").slice(0,24)||"side";
  const name=("side-"+slug+"-"+Date.now().toString(36).slice(-4)).slice(0,48);
  const url=isLinearIssueKey(topic)?linearIssueUrl(topic):"";
  const prompt=
    "You are a dedicated side seat for: "+topic+".\n"
    +(url?("Linear: "+url+"\n"):"")
    +"Parent session: "+(modalSession.name||"?")+" ("+(modalSession.session||"")+")\n"
    +"Cwd hint: "+(modalSession.cwd||modalSession.path||".")+"\n\n"
    +"Stay focused on this topic. Pull Linear context if you can. Report findings clearly.";
  tchatLog("bot","Spawning Fresh Claude seat “"+name+"”…",id);
  const st=$("#modalstatus");
  if(st){st.textContent="spawning side seat…";st.style.color="";}
  try{
    const r=await api("/api/spawn",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        name,
        command:"claude --dangerously-skip-permissions",
        cwd:modalSession.cwd||modalSession.path||undefined,
        prompt,
        description:"Side chat · "+topic+(modalSession.name?(" · from "+modalSession.name):""),
        tags:["sidechat", topic].concat(isLinearIssueKey(topic)?["linear",topic]:[]),
      })});
    if(r&&r.ok){
      sc.seatName=r.name||name;
      tchatLog("bot","Seat live: "+sc.seatName+(r.kickstarted?" (kickstarted on the task)":""),id);
      renderSideChats();
      try{await poll();}catch(_){}
      if(st) st.textContent="side seat "+sc.seatName;
    }else{
      tchatLog("bot","Spawn failed: "+((r&&r.error)||"unknown"),id);
      if(st){st.textContent="spawn failed";st.style.color="var(--stale)";}
    }
  }catch(e){
    tchatLog("bot","Spawn unreachable",id);
  }
}
// Back-compat aliases used elsewhere
function tchatToggle(){
  const sc=ensureReplySideChat();
  if(!sc) return;
  sc.collapsed=!sc.collapsed;
  renderSideChats();
  if(!sc.collapsed) focusSideChat("reply");
}
function tchatSend(){ sideChatSend("reply"); }
function renderTChat(){ renderSideChats(); }
async function modalJudge(){
  if(!modalSession)return;
  const el=$("#modaljudge");
  try{
    const r=await api("/api/classify?name="+encodeURIComponent(modalSession.name));
    if(r&&r.ok&&r.state){
      const flags=(r.flags||[]).map(f=>'<span class="jflag">'+f.replace(/_/g," ")+'</span>').join("");
      el.innerHTML='<span class="jstate s-'+r.state+'">'+r.state.replace(/_/g,"·")+'</span>'
        +'<span class="jconf">'+Math.round((r.confidence||0)*100)+'% · '+(r.recommended_action||"")+'</span>'
        +'<span class="jsum">'+(r.summary||"")+'</span>'+flags;
      el.className="on";
    }else{el.className="";el.innerHTML="";}
  }catch(e){el.className="";}
}
async function modalScrollTmux(direction,amount){
  if(!modalSession)return false;
  try{
    const r=await api("/api/scroll",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        session:modalSession.session,
        direction:direction||"up",
        amount:amount||"page",
        mode:modalScrollMode==="tmux"?"tmux":"auto",
      })});
    const sc=$("#modalscreen");
    // After in-app scroll, force a new snapshot into the book so HTML height grows
    if(sc)sc.dataset.last=""; // bust so modalRefresh always re-renders
    // Stay unpinned only if user was following live; else keep reading position
    if(direction==="up"&&sc)sc.dataset.userPinned="1";
    await modalRefresh();
    if(r&&r.ok&&r.mode==="app"&&sc){
      // Nudge status so operator knows we scrolled the agent, not empty tmux history
      const st=$("#modalstatus");
      if(st&&!(st.textContent||"").includes("scrolled")){
        st.textContent=(st.textContent||"live")+" · scrolled agent";
      }
    }
    return !!(r&&r.ok);
  }catch(e){return false;}
}
async function modalKeys(keys,literal,enter){
  if(!modalSession)return false;
  // PageUp/PageDown chips → real tmux scrollback (not keystrokes into the agent)
  if(!literal&&(keys==="PageUp"||keys==="PageDown"||keys==="PPage"||keys==="NPage")){
    return modalScrollTmux(keys==="PageUp"||keys==="PPage"?"up":"down","page");
  }
  try{
    const r=await api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:modalSession.session,keys,literal,enter})});
    if(r&&r.shell_warn){
      const st=$("#modalstatus");
      if(st){st.textContent="⚠ pane looks like a shell — agent may not be running";st.style.color="var(--stale)";}
    }
    setTimeout(modalRefresh,250);
    return !!(r&&r.ok);
  }catch(e){return false;}   // daemon down / network error
}
// Web image paste: laptop pasteboard → POST /api/clip-image → path on fleet host → send with prompt.
// Only intercepts paste when modalinput/tchatinput is focused — never system-wide.
let modalClips=[];   // {path, mime, bytes, preview}
function renderModalClips(){
  const box=$("#modalclips");if(!box)return;
  if(!modalClips.length){box.className="";box.innerHTML="";return;}
  box.className="on";
  box.innerHTML=modalClips.map((c,i)=>
    '<span class="clip" data-i="'+i+'">'
    +(c.preview?'<img src="'+c.preview+'" alt="">':'')
    +'<span class="cp" title="'+esc(c.path)+'">'+esc(c.path)+'</span>'
    +'<button type="button" class="rm" data-i="'+i+'" title="remove">×</button></span>'
  ).join("")+'<span class="cliphint">on fleet host · sent with your message</span>';
  box.querySelectorAll(".rm").forEach(b=>b.onclick=e=>{
    e.preventDefault();modalClips.splice(+b.dataset.i,1);renderModalClips();
  });
}
function clearModalClips(){modalClips=[];renderModalClips();}
function fileToDataUrl(file){
  return new Promise((resolve,reject)=>{
    const r=new FileReader();
    r.onload=()=>resolve(r.result);
    r.onerror=()=>reject(r.error||new Error("read failed"));
    r.readAsDataURL(file);
  });
}
async function uploadClipImage(file,session){
  if(!file||!String(file.type||"").startsWith("image/"))return null;
  if(file.size>8*1024*1024)throw new Error("image over 8MB");
  const dataUrl=await fileToDataUrl(file);
  const r=await api("/api/clip-image",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({data:dataUrl,mime:file.type||"image/png",session:session||""})});
  if(!r||!r.ok)throw new Error((r&&r.error)||"upload failed");
  return {path:r.path,mime:r.mime||file.type,bytes:r.bytes||file.size,preview:dataUrl};
}
async function handleComposerPaste(e,session){
  const items=e.clipboardData&&e.clipboardData.items;
  if(!items||!items.length)return false;
  const images=[];
  for(let i=0;i<items.length;i++){
    const it=items[i];
    if(it.kind==="file"&&it.type&&it.type.startsWith("image/")){
      const f=it.getAsFile();if(f)images.push(f);
    }
  }
  if(!images.length)return false;   // text paste — leave alone
  e.preventDefault();
  const st=$("#modalstatus");
  for(const f of images){
    try{
      if(st){st.textContent="uploading image…";st.style.color="";}
      const clip=await uploadClipImage(f,session);
      if(clip){modalClips.push(clip);renderModalClips();}
      if(st){st.textContent="image on fleet · "+(clip&&clip.path?clip.path.split("/").pop():"ok");st.style.color="var(--amber)";}
    }catch(err){
      if(st){st.textContent="image paste failed — "+(err.message||err);st.style.color="var(--stale)";}
    }
  }
  return true;
}
function modalIsGrok(){
  if(!modalSession)return false;
  const tags=(modalSession.tags||[]).map(t=>String(t).toLowerCase());
  if(tags.includes("grok"))return true;
  if(tags.some(t=>t.startsWith("gsid:")))return true;
  const ag=((modalSession.agent&&modalSession.agent.agent)||modalSession.agent||"")+"";
  if(/grok/i.test(ag))return true;
  if(/^chat-grok-/i.test(modalSession.name||""))return true;
  return false;
}
function modalGsid(){
  if(!modalSession)return "";
  for(const t of (modalSession.tags||[])){
    const s=String(t);
    if(s.toLowerCase().startsWith("gsid:"))return s.slice(5).trim();
  }
  return "";
}
function modalSubmit(){
  const i=$("#modalinput");
  const paths=modalClips.map(c=>c.path).filter(Boolean);
  const body=(i.value||"").trim();
  if(!body&&!paths.length)return;
  // Paths first so Claude/Grok can Read the files on this host; then operator text.
  const text=paths.length?(paths.join("\n")+(body?"\n\n"+body:"")):body;
  i.value="";                                   // optimistic clear for snappy UX…
  clearModalClips();
  setPending(text.length>180?text.slice(0,180)+"…":text);
  const st=$("#modalstatus");
  // Grok: control-plane cascade ACP → headless → PTY (server-side auto)
  if(modalIsGrok()){
    if(st){st.textContent="acp steer…";st.style.color="";}
    api("/api/steer",{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({session:modalSession.session,prompt:text,mode:"auto",
        session_id:modalGsid()||undefined,cwd:modalSession.cwd||undefined,
        tool:"grok",timeout:180})}).then(r=>{
      if(r&&r.ok){
        setPending("");
        const modeLabel=r.mode==="acp"?"acp ok":(r.mode==="headless"?"headless ok":"pty ok");
        const via=r.via_fallback?" · via fallback":"";
        if(st)st.textContent=modeLabel+via+(r.elapsed_ms?(" · "+r.elapsed_ms+"ms"):"");
        if(r.session_id&&!modalGsid()){
          // remember gsid on the live card for next steer
          modalSession.tags=modalSession.tags||[];
          if(!modalSession.tags.some(t=>String(t).startsWith("gsid:")))
            modalSession.tags.push("gsid:"+r.session_id);
        }
        if(r.text)setPending("↳ "+(r.text.length>160?r.text.slice(0,160)+"…":r.text));
        setTimeout(modalRefresh,400);
      }else{
        // last-ditch PTY (server already tried cascade; still try paste)
        if(st){st.textContent="steer cascade failed — trying pty…";st.style.color="";}
        modalKeys(text,true,true).then(ok=>{
          if(!ok){
            if(!i.value)i.value=body;
            if(paths.length)i.value=(paths.join("\n")+(i.value?"\n\n"+i.value:""));
            setPending("");
            if(st){st.textContent="steer failed — "+((r&&r.error)||"draft kept");st.style.color="var(--stale)";}
          }else if(st){st.textContent="pty ok";st.style.color="";}
        });
      }
    }).catch(()=>{
      modalKeys(text,true,true).then(ok=>{
        if(!ok){if(!i.value)i.value=body;setPending("");if(st){st.textContent="send failed";st.style.color="var(--stale)";}}
      });
    });
    return;
  }
  modalKeys(text,true,true).then(ok=>{
    if(!ok){                                    // …but if it didn't land, give the draft back
      if(!i.value)i.value=body;
      // cannot restore binary preview easily; path lines still useful
      if(paths.length)i.value=(paths.join("\n")+(i.value?"\n\n"+i.value:""));
      setPending("");
      if(st){st.textContent="send failed — draft kept";st.style.color="var(--stale)";}
    }
  });
}
// a message received by emux but not yet echoed by the (maybe ssh-laggy) session
let pendingText="";
function setPending(txt){pendingText=txt;const el=$("#modalpending");
  if(txt){el.className="on";el.innerHTML='<span class="plabel">received · sending</span><span class="ptext">'+esc(txt)+'</span>';}
  else{el.className="";el.innerHTML="";}}
// thinking indicator: movement + how long the agent has been generating
function updateThinking(t){const el=$("#modalthink");
  if(t&&t.active){el.className="on";if(t.for)el.querySelector("b").textContent=t.for;}
  else el.className="";}
$("#modalsend").onclick=modalSubmit;
$("#modalinput").addEventListener("keydown",e=>{if(e.key==="Enter")modalSubmit();});
$("#modalinput").addEventListener("paste",e=>{
  if(!modalSession)return;
  handleComposerPaste(e,modalSession.session||modalSession.name);
});
// Side-chat inputs are bound dynamically in renderSideChats()
$("#modalpop").onclick=()=>popOutSessionTab();
$("#modaliterm").onclick=async()=>{
  if(!modalSession)return;
  const b=$("#modaliterm");const was=b.textContent;b.disabled=true;b.textContent="opening…";
  const r=await api("/api/head",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({session:modalSession.session})});
  b.textContent=r.ok?"⧉ opened":("✕ "+(r.error||"failed"));
  setTimeout(()=>{b.textContent=was;b.disabled=false;},r.ok?1400:3000);
};
// ---- new session: a CASCADE. Each choice constrains the ones below it, so
// changing an upper choice INVALIDATES everything under it and re-derives. ----
// The tree: machine → (resume a running session | start new in a dir) → command → name.
// Its nodes are enumerated from REALITY; the LLM only classifies your sentence into
// a path through it and pre-fills the picks (marked ✦), which you can override.
const NS={hosts:[],host:"",dirs:[],running:[],lane:"new",session:"",cwd:"",cmd:"",name:"",
          aiHost:"",aiPick:"",whyHost:"",whyDir:"",loading:false,manual:false};

function nsReset(level){        // wipe every choice BELOW `level` (1=machine, 2=target)
  if(level<2){NS.dirs=[];NS.running=[];NS.session="";NS.cwd="";NS.whyDir="";NS.aiPick="";}
  if(level<3){NS.cmd="";}
  if(level<4){NS.name="";}
}
const isResume=()=>NS.lane==="resume";
const hasTarget=()=>isResume()?!!NS.session:!!NS.cwd;

function nsRender(){
  // ── node 1: machine
  $("#hostchips").innerHTML=NS.hosts.map(h=>
    '<span class="hchip'+(h===NS.host?" on":"")+(h===NS.aiHost&&h!==NS.host?" ai":"")+'" data-h="'+h+'">'+h+'</span>').join("");
  $("#hostchips").querySelectorAll(".hchip").forEach(el=>el.onclick=()=>pickHost(el.dataset.h));
  $("#why-host").textContent=NS.whyHost?("✦ "+NS.whyHost):"";
  $("#s-host").classList.toggle("done",!!NS.host);

  // ── node 2: what's ON that machine — resume something running, or start fresh
  const dl=$("#s-dir");
  dl.classList.toggle("locked",!NS.host);
  dl.classList.toggle("done",hasTarget());
  $("#nrun").textContent=NS.running.length;
  $("#ndir").textContent=NS.dirs.length;
  $("#dirsub").textContent=!NS.host?"pick a machine first"
    :NS.loading?("looking at "+NS.host+"…"):("on "+NS.host);
  $("#s-dir").querySelectorAll(".lane").forEach(el=>{
    el.classList.toggle("on",el.dataset.lane===NS.lane);
    el.onclick=()=>{NS.lane=el.dataset.lane;NS.session="";NS.cwd="";nsReset(2);
                    $("#dirfilter").value="";$("#newname").value="";nsRender();};
  });
  const f=($("#dirfilter").value||"").toLowerCase();
  let html="";
  if(isResume()){
    const rows=NS.running.filter(s=>!f||(s.name+" "+s.path).toLowerCase().includes(f));
    html=rows.map(s=>
      '<div class="dirrow'+(s.name===NS.session?" on":"")+'" data-s="'+s.name+'">'+s.name
      +'<span class="meta">'+ago(s.age_sec)+' · '+s.path+(s.attached?' · attached':'')
      +(s.adopted?' · in emux':'')+'</span>'
      +(s.name===NS.aiPick?'<span class="ai">✦</span>':'')+'</div>').join("");
  }else{
    const rows=NS.dirs.filter(d=>!f||d.toLowerCase().includes(f));
    html=rows.map(d=>
      '<div class="dirrow'+(d===NS.cwd?" on":"")+'" data-d="'+d+'">'+d
      +(d===NS.aiPick?'<span class="ai">✦</span>':"")+'</div>').join("");
  }
  $("#dirchoices").innerHTML=html||'<div class="dirrow" style="cursor:default;opacity:.5">— nothing here —</div>';
  $("#dirchoices").querySelectorAll(".dirrow[data-d]").forEach(el=>el.onclick=()=>pickDir(el.dataset.d));
  $("#dirchoices").querySelectorAll(".dirrow[data-s]").forEach(el=>el.onclick=()=>pickSession(el.dataset.s));
  $("#why-dir").textContent=NS.whyDir?((NS.aiPick?"✦ ":"")+NS.whyDir):"";

  // ── node 3: what runs there — only a NEW session needs one; a resumed one is already running
  const cs=$("#s-cmd");
  cs.classList.toggle("locked",!hasTarget());
  cs.classList.toggle("done",hasTarget());
  cs.style.display=isResume()?"none":"";
  const presets=[["","plain shell"],["claude","claude"],["claude --dangerously-skip-permissions","claude (skip perms)"]];
  $("#cmdchips").innerHTML=presets.map(([v,l])=>
    '<span class="hchip'+(NS.cmd===v?" on":"")+'" data-c="'+v+'">'+l+'</span>').join("");
  $("#cmdchips").querySelectorAll(".hchip").forEach(el=>el.onclick=()=>{NS.cmd=el.dataset.c;$("#newcmd").value=NS.cmd;nsRender();});

  // ── the preview: only meaningful when RESUMING (a new session has no state yet)
  $("#peekwrap").classList.toggle("show",isResume()&&!!NS.session);

  // ── node 4: naming means different things. New: christen a thing that doesn't
  //    exist. Resume: it already HAS a name — you're choosing its emux alias.
  const ns=$("#s-name");
  ns.classList.toggle("locked",!hasTarget());
  ns.classList.toggle("done",!!NS.name);
  $("#namelbl").textContent=isResume()?"adopt into emux as":"name it";
  $("#namesub").textContent=isResume()
    ? ("it is already called “"+NS.session+"” on "+NS.host)
    : "";
  $("#guilbl").textContent=isResume()
    ? "also open a terminal attached to it"
    : "open an iTerm2 window attached to it";
  // attaching to a LIVE session is not free — say so
  $("#guinote").textContent=(isResume()&&$("#newgui").checked)
    ? "⚠ it is already running; the window opens WITHOUT taking focus so it can't swallow your keystrokes"
    : "";

  // ── the answer card: one line you can actually read
  const ready=!!(NS.host&&hasTarget()&&NS.name);
  $("#result").classList.toggle("show",ready&&!NS.manual);
  if(ready){
    $("#rverb").textContent=isResume()?"↺ resume":"+ new session";
    $("#rwhat").textContent=isResume()?NS.session:NS.name;
    $("#rwhere").textContent=(isResume()?"on ":"on ")+NS.host+" · "+NS.cwd
      +(isResume()?"":(NS.cmd?" · "+NS.cmd:" · shell"));
    $("#rwhy").textContent=NS.whyDir||"";
    $("#rpeek").hidden=!isResume();
  }
  // the tree is the machinery — only on demand. Empty state = just the question.
  $("#tree").classList.toggle("show",NS.manual);
  $("#newchange").textContent=NS.manual?"← done"
    :(ready?"change…":"set it up by hand");

  const b=$("#newcreate");
  b.disabled=!ready;
  b.textContent=isResume()?"RESUME":"CREATE";
}
function ago(s){return s<3600?Math.floor(s/60)+"m":(s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d");}

async function pickHost(h){       // node 1 changed ⇒ everything under it is invalid
  NS.host=h;NS.whyHost="";nsReset(1);
  $("#newcmd").value="";$("#newname").value="";$("#dirfilter").value="";
  NS.loading=true;nsRender();
  const r=await api("/api/dirs?host="+encodeURIComponent(h));
  NS.loading=false;
  NS.dirs=(r.ok&&r.dirs)?r.dirs:[];
  NS.running=(r.ok&&r.running)?r.running:[];
  nsRender();
}
function pickDir(d){NS.cwd=d;NS.session="";if(!NS.name)NS.name=autoName(d);
  $("#newname").value=NS.name;nsRender();}
async function pickSession(s){
  const row=NS.running.find(x=>x.name===s)||{};
  NS.session=s;NS.cwd=row.path||"";NS.name=s;   // it already has a name — default to it
  $("#newname").value=NS.name;
  $("#peek").textContent="reading "+s+"…";$("#peekflags").innerHTML="";
  nsRender();
  // LOOK INSIDE before touching it: which one is this, and does it hold unsent input?
  const r=await api("/api/peek?session="+encodeURIComponent(s)
                    +"&host="+encodeURIComponent(NS.host));
  if(NS.session!==s)return;                     // selection moved on while we read
  const body=r.ok?(r.content||"(blank)"):("could not read: "+(r.error||""));
  $("#peek").textContent=body;
  $("#peek2").textContent=body;
  let flags="";
  if(r.ok&&r.unsent)flags+='<span class="flag">holds an unsent prompt</span>';
  if(row.attached)flags+='<span class="flag">already attached elsewhere</span>';
  $("#peekflags").innerHTML=flags;
  $("#rflags").innerHTML=flags;
  nsRender();
}
function autoName(d){const base=(d||"").split("/").filter(Boolean).pop()||"session";
  return base.toLowerCase().replace(/[^a-z0-9]+/g,"-").slice(0,28);}

async function openNew(){
  $("#newmodal").classList.add("open");
  $("#newerr").textContent="";$("#newstatus").textContent="";
  Object.assign(NS,{host:"",dirs:[],running:[],lane:"new",session:"",cwd:"",cmd:"",name:"",
                    aiHost:"",aiPick:"",whyHost:"",whyDir:"",manual:false});
  $("#rflags").innerHTML="";$("#peek2").textContent="";
  $("#newintent").value="";$("#newcmd").value="";$("#newname").value="";$("#dirfilter").value="";
  if(!NS.hosts.length){const r=await api("/api/hosts");if(r.ok)NS.hosts=r.hosts||[];}
  nsRender();
  setTimeout(()=>$("#newintent").focus(),40);
}
function closeNew(){$("#newmodal").classList.remove("open");}

// Plain English → the LLM CLASSIFIES it into a path through the tree and PRE-FILLS
// the nodes. Every pick is marked ✦ and stays overridable.
async function doSuggest(){
  const intent=$("#newintent").value.trim();
  if(!intent){$("#newerr").textContent="say what you want to do first";return;}
  const b=$("#newsuggest");b.disabled=true;b.textContent="thinking…";$("#newerr").textContent="";
  $("#newstatus").textContent=NS.host?("reading "+NS.host):"machine → what's on it";
  const r=await api("/api/suggest",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({intent,host:NS.host||""})});   // a machine you already picked pins node 1
  b.disabled=false;b.textContent="✦ figure it out";$("#newstatus").textContent="";
  if(!r.ok){$("#newerr").textContent=r.error||"suggest failed";return;}
  NS.host=r.host||NS.host;NS.aiHost=r.host||"";
  NS.dirs=r.dirs||[];NS.running=r.running||[];
  NS.whyHost=r.whyHost||"";NS.whyDir=r.why||"";
  NS.lane=(r.action==="resume")?"resume":"new";
  if(NS.lane==="resume"){
    NS.aiPick=r.session||"";NS.cmd="";
    nsRender();
    if(r.session){await pickSession(r.session);return;}   // selects it AND peeks inside
    NS.session="";NS.cwd="";
  }else{
    NS.cwd=r.cwd||"";NS.aiPick=r.cwd||"";NS.session="";
    NS.cmd=r.command||"";$("#newcmd").value=NS.cmd;
    if(!r.verified&&r.cwd)NS.whyDir="⚠ unverified — "+NS.whyDir;
  }
  NS.name=r.name||(NS.session||(r.cwd?autoName(r.cwd):""));
  $("#newname").value=NS.name;
  nsRender();
}
async function doCreate(){
  if(!NS.name){$("#newerr").textContent="a session needs a name";return;}
  const b=$("#newcreate");const was=b.textContent;
  b.disabled=true;b.textContent=isResume()?"RESUMING…":"CREATING…";$("#newerr").textContent="";
  const resume=isResume();
  const r=await api(resume?"/api/adopt":"/api/spawn",
    {method:"POST",headers:{"Content-Type":"application/json"},
     body:JSON.stringify(resume
       ? {session:NS.session,host:NS.host,name:NS.name,gui:$("#newgui").checked,
          description:$("#newintent").value.trim()||("resumed on "+NS.host),
          tags:["adopted",NS.host]}
       : {name:NS.name,host:NS.host,cwd:NS.cwd,command:NS.cmd,
          gui:$("#newgui").checked,description:$("#newintent").value.trim()||null,
          prompt:$("#newintent").value.trim()||null})});   // kickstart: the agent starts ON this
  b.disabled=false;b.textContent=was;
  if(!r.ok){$("#newerr").textContent=r.error||(resume?"resume failed":"spawn failed");return;}
  closeNew();refresh();
}
$("#newbtn").onclick=openNew;
$("#newclose").onclick=closeNew;
$("#newback").onclick=closeNew;
$("#newsuggest").onclick=doSuggest;
$("#newcreate").onclick=doCreate;
$("#dirfilter").addEventListener("input",nsRender);
$("#newgui").addEventListener("change",nsRender);
$("#newchange").onclick=()=>{NS.manual=!NS.manual;nsRender();};
$("#newcmd").addEventListener("input",()=>{NS.cmd=$("#newcmd").value;nsRender();});
$("#newname").addEventListener("input",()=>{NS.name=$("#newname").value.trim();nsRender();});
$("#newintent").addEventListener("keydown",e=>{if(e.key==="Enter")doSuggest();});

$("#dgrefresh").onclick=loadDigest;
$("#modalclose").onclick=closeModal;
$("#modalback").onclick=closeModal;
// Terminal scroll: HTML book when tall; else app/tmux scroll which feeds the book.
(function(){
  const sc=$("#modalscreen"), j=$("#modaljump");
  let wheelBusy=false, wheelAcc=0, wheelTimer=null;
  if(sc){
    sc.addEventListener("scroll",()=>{
      if(modalScreenPinned(sc))sc.dataset.userPinned="0";
      else sc.dataset.userPinned="1";
      updateModalJump();
    },{passive:true});
    sc.addEventListener("wheel",e=>{
      e.stopPropagation();
      const canUp=sc.scrollTop>2;
      const canDown=sc.scrollTop+sc.clientHeight<sc.scrollHeight-2;
      const tall=sc.scrollHeight>sc.clientHeight+8;
      // Native HTML scroll inside the accumulated book whenever possible.
      if(tall&&e.deltaY<0&&canUp){sc.dataset.userPinned="1";return;}
      if(tall&&e.deltaY>0&&canDown){return;}
      // At edge or single-screen capture → drive agent/tmux, which adds pages to the book.
      e.preventDefault();
      wheelAcc+=e.deltaY;
      if(wheelTimer)clearTimeout(wheelTimer);
      wheelTimer=setTimeout(()=>{wheelAcc=0;},180);
      if(wheelBusy)return;
      if(Math.abs(wheelAcc)<24)return;
      const dir=wheelAcc<0?"up":"down";
      const amount=Math.abs(wheelAcc)>120?"page":"half";
      wheelAcc=0;wheelBusy=true;
      modalScrollTmux(dir,amount).finally(()=>{wheelBusy=false;});
    },{passive:false});
  }
  if(j)j.onclick=()=>{
    const s=$("#modalscreen");if(s)s.dataset.userPinned="0";
    // Leave tmux copy-mode if we were in it; re-pin live bottom of the book
    if(modalSession){
      api("/api/send",{method:"POST",headers:{"Content-Type":"application/json"},
        body:JSON.stringify({session:modalSession.session,keys:"Escape",literal:false,enter:false})})
        .finally(()=>{modalScrollBottom();setTimeout(modalRefresh,200);});
    }else modalScrollBottom();
  };
})();
document.querySelectorAll("#modalchips .chip").forEach(ch=>ch.onclick=()=>modalKeys(ch.dataset.keys,false,false));

// keyboard: Esc closes the topmost modal first; otherwise 1-4 switch views
document.addEventListener("keydown",e=>{
  if($("#newmodal").classList.contains("open")){if(e.key==="Escape")closeNew();return;}
  if($("#modal").classList.contains("open")){if(e.key==="Escape")closeModal();return;}
  // EID stress: Esc must dismiss settings (was missing → setmodal stuck, blocked SETTINGS re-click)
  if($("#setmodal")&&$("#setmodal").classList.contains("open")){if(e.key==="Escape")closeSettings();return;}
  if(e.target.id==="filter"||e.target.id==="input"||e.target.id==="modalinput")return;
  if(e.target.closest&&e.target.closest("#newmodal"))return;
  const map={"1":"grid","2":"groups","3":"activity","4":"flow","5":"orphans","6":"chats"};
  if(map[e.key])setMode(map[e.key]);
});
// resume + clear title flash when tab refocuses (#13 #20)
document.addEventListener("visibilitychange",()=>{
  if(!document.hidden){flashOn=false;poll();if(modalSession)modalRefresh();}
});

// ---- live fleet feed ----
let feedSeen="";   // signature of the newest event, to flash only what's new
function fage(ts){const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  return s<60?s+"s":(s<3600?Math.floor(s/60)+"m":(s<86400?Math.floor(s/3600)+"h":Math.floor(s/86400)+"d"));}
function esc(x){return (x||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
// Linear issue keys (TEAM-123) → https://linear.app/<workspace>/issue/TEAM-123
// Pure regex client-side — no API. Workspace is eidos-agi across AIC/Eidos fleet.
const LINEAR_WS="eidos-agi";
const LINEAR_ISSUE_RE=/\b([A-Z]{2,12}-\d{1,7})\b/g;
// Common non-Linear TOKEN-n forms we must not turn into issue links.
const LINEAR_SKIP_PREFIX=/^(UTF|HTTP|HTTPS|ISO|RFC|CPU|GPU|RAM|API|SDK|CLI|SSH|TLS|SSL|XML|JSON|HTML|CSS|PDF|URL|URI|UUID|SHA|MD5|AES|RSA|VPN|CDN|DNS|TCP|UDP|IPv?|OK|ID|US|UK|EU|AI|V\d+)$/i;
function linearIssueUrl(key){
  return "https://linear.app/"+LINEAR_WS+"/issue/"+encodeURIComponent(key);
}
function isLinearIssueKey(key){
  const i=String(key||"").indexOf("-");
  if(i<2) return false;
  const pre=key.slice(0,i), num=key.slice(i+1);
  if(LINEAR_SKIP_PREFIX.test(pre)) return false;
  if(!/^\d{1,7}$/.test(num)) return false;
  // Team keys are letters (Linear); reject mixed like "H2O-1"
  if(!/^[A-Z]{2,12}$/.test(pre)) return false;
  return true;
}
/** HTML for one Linear key + chat bubble (bubble loads/pursues the task). */
function linearKeyHTML(key){
  return '<span class="linwrap">'
    +'<a class="linlink" href="'+linearIssueUrl(key)+'" target="_blank" rel="noopener noreferrer" title="Open '+key+' in Linear">'+key+'</a>'
    +'<button type="button" class="linchat" data-linear="'+key+'" title="Side chat + load '+key+' into this head so you can pursue it">💬</button>'
    +'</span>';
}
/** Escape + turn Linear keys (and bare http URLs) into links + pursue bubbles. */
function linkifyLinear(raw){
  const s=String(raw==null?"":raw);
  if(!s) return "";
  // Split on existing URLs so we don't nest issue keys inside linear.app/… paths.
  const parts=s.split(/(https?:\/\/[^\s<>"'`]+)/g);
  return parts.map((part,i)=>{
    if(i%2===1){
      const e=esc(part);
      // If the URL is a Linear issue page, also offer the pursue bubble.
      const m=part.match(/linear\.app\/[^/]+\/issue\/([A-Z]{2,12}-\d{1,7})/i);
      if(m&&isLinearIssueKey(m[1].toUpperCase())){
        const key=m[1].toUpperCase();
        return '<span class="linwrap"><a class="linlink" href="'+e+'" target="_blank" rel="noopener noreferrer">'+e+'</a>'
          +'<button type="button" class="linchat" data-linear="'+key+'" title="Side chat + load '+key+' so you can pursue it">💬</button></span>';
      }
      return '<a class="linlink" href="'+e+'" target="_blank" rel="noopener noreferrer">'+e+'</a>';
    }
    return esc(part).replace(LINEAR_ISSUE_RE,m=>
      isLinearIssueKey(m)?linearKeyHTML(m):m
    );
  }).join("");
}
/** Craft the message that primes a head to load Linear issue KEY and work it. */
function linearPursuePrompt(key){
  const url=linearIssueUrl(key);
  return (
    "Load Linear issue "+key+" and prepare to pursue it.\n"
    +"URL: "+url+"\n\n"
    +"Do this now:\n"
    +"1. Open/fetch the issue (Linear UI, CLI, or MCP if you have it) and restate: title, status, assignee, description, acceptance criteria, blockers, and linked PRs/branches if any.\n"
    +"2. Summarize what \"done\" means and the smallest next concrete step.\n"
    +"3. Stay on "+key+" unless I redirect you — treat this as the active work item for this conversation.\n"
    +"4. If you cannot reach Linear, say what you need; otherwise start executing the next step after the summary."
  );
}
/**
 * Chat bubble after a task id: open a side chat about KEY and inject a
 * "load this Linear issue so we can pursue it" prompt into the open head
 * (or spawn a fresh Claude if no modal is open).
 */
function pursueLinearTask(key, opts){
  key=String(key||"").trim().toUpperCase();
  if(!isLinearIssueKey(key)) return;
  const o=opts||{};
  // Prefer working inside an open session modal
  if(modalSession){
    const sc=openSideChat({
      kind:"task",
      topic:key,
      title:"about "+key,
      seed:o.seed||(
        "Pursuing Linear "+key+".\n"+linearIssueUrl(key)+"\n\n"
        +"I just asked the parent head to load this issue and treat it as active work. "
        +"Use this side chat for follow-ups about "+key+"; “Fresh Claude” spins a dedicated seat."
      ),
    });
    const prompt=linearPursuePrompt(key);
    if(sc) tchatLog("you","Load + pursue "+key,sc.id);
    setPending(prompt.length>160?prompt.slice(0,160)+"…":prompt);
    const st=$("#modalstatus");
    if(st){st.textContent="loading "+key+"…";st.style.color="";}
    modalKeys(prompt,true,true).then(ok=>{
      if(st){
        st.textContent=ok?("pursuing "+key):("send failed — try again");
        st.style.color=ok?"":"var(--stale)";
      }
      if(ok) setTimeout(()=>{modalRefresh();loadDigest();},800);
    });
    return;
  }
  // No modal: spawn a dedicated Claude seat for the issue
  const slug=key.toLowerCase().replace(/[^a-z0-9]+/g,"-");
  const name=("task-"+slug+"-"+Date.now().toString(36).slice(-4)).slice(0,48);
  api("/api/spawn",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({
      name,
      command:"claude --dangerously-skip-permissions",
      prompt:linearPursuePrompt(key),
      description:"Pursue Linear "+key,
      tags:["sidechat","linear",key,"pursue"],
    })}).then(async r=>{
      if(r&&r.ok){
        try{await poll();}catch(_){}
        const s=grid.find(x=>x.name===(r.name||name));
        if(s) openModal(s);
      }else{
        alert("Could not open seat for "+key+": "+((r&&r.error)||"unknown"));
      }
    });
}
/** Bind linlinks + chat bubbles so parent tile/card clicks do not fire. */
function bindLinlinks(root){
  if(!root) return;
  root.querySelectorAll("a.linlink").forEach(a=>{
    a.onclick=e=>{e.stopPropagation();};
  });
  root.querySelectorAll("button.linchat").forEach(btn=>{
    btn.onclick=e=>{
      e.preventDefault();
      e.stopPropagation();
      const key=btn.getAttribute("data-linear");
      if(!key) return;
      btn.disabled=true;
      pursueLinearTask(key);
      setTimeout(()=>{btn.disabled=false;},600);
    };
  });
}
/** Collect unique Linear keys from any blob of chat text. */
function extractLinearKeys(text){
  const out=[];
  const s=String(text==null?"":text);
  if(!s) return out;
  s.replace(LINEAR_ISSUE_RE,m=>{
    if(isLinearIssueKey(m)&&out.indexOf(m)<0) out.push(m);
    return m;
  });
  LINEAR_ISSUE_RE.lastIndex=0;
  return out;
}
/** All Linear tasks mentioned in the open session (name, summary, pane, history, gist). */
function collectModalTaskKeys(s){
  const blobs=[];
  if(s){
    blobs.push(s.name,s.description,s.summary,s.content);
    if(s.linear&&s.linear.issue) blobs.push(String(s.linear.issue));
    if(Array.isArray(s.tags)) blobs.push(s.tags.join(" "));
  }
  if(typeof modalHistoryText==="string"&&modalHistoryText) blobs.push(modalHistoryText);
  if(Array.isArray(modalBook)&&modalBook.length) blobs.push(modalBook.join("\n"));
  const dig=$("#modaldigest .dgtext"); if(dig&&dig.textContent) blobs.push(dig.textContent);
  const sc=$("#modalscreen"); if(sc&&sc.dataset.last) blobs.push(sc.dataset.last);
  const keys=[];
  for(const b of blobs){
    for(const k of extractLinearKeys(b)){
      if(keys.indexOf(k)<0) keys.push(k);
    }
  }
  // Stable order: alphabetical by team then number
  keys.sort((a,b)=>{
    const [ap,an]=a.split("-"),[bp,bn]=b.split("-");
    if(ap!==bp) return ap<bp?-1:1;
    return (parseInt(an,10)||0)-(parseInt(bn,10)||0);
  });
  return keys;
}
let modalTasksOpen=true;
function toggleModalTasks(force){
  const d=$("#modaltasks"), btn=$("#modaltasksbtn");
  if(!d) return;
  if(typeof force==="boolean") modalTasksOpen=force;
  else modalTasksOpen=!modalTasksOpen;
  d.classList.toggle("collapsed",!modalTasksOpen);
  if(btn) btn.classList.toggle("on",modalTasksOpen);
  try{localStorage.setItem("emux_modal_tasks",modalTasksOpen?"1":"0");}catch(_){}
}
function renderModalTasks(){
  const list=$("#modaltasklist"), countEl=$("#modaltaskcount"), badge=$("#modaltaskbadge");
  if(!list) return;
  const keys=collectModalTaskKeys(modalSession);
  if(countEl) countEl.textContent=String(keys.length);
  if(badge){
    badge.textContent=String(keys.length);
    badge.classList.toggle("on",keys.length>0);
  }
  if(!keys.length){
    list.innerHTML='<div class="mtempty">No Linear tasks mentioned yet. When the head cites <b>AIC-123</b>, <b>EID-45</b>, <b>GMW-9</b>… they land here. Use <b>💬 chat</b> for a side chat, or <b>+</b> bottom-right for any topic.</div>';
    return;
  }
  list.innerHTML=keys.map(k=>
    '<div class="mtask" data-key="'+esc(k)+'">'
    +'<a class="linlink mtkey" href="'+linearIssueUrl(k)+'" target="_blank" rel="noopener noreferrer" title="Open in Linear">'+esc(k)+'</a>'
    +'<span class="mtacts">'
    +'<button type="button" class="mtchat" data-act="pursue" title="Side chat + load this issue so the head can pursue it">💬 pursue</button>'
    +'<a class="mtgo linlink" href="'+linearIssueUrl(k)+'" target="_blank" rel="noopener noreferrer">↗</a>'
    +'</span></div>'
  ).join("");
  list.querySelectorAll(".mtchat").forEach(btn=>{
    btn.onclick=e=>{
      e.preventDefault();e.stopPropagation();
      const key=btn.closest(".mtask")&&btn.closest(".mtask").dataset.key;
      if(!key) return;
      // Same as the 💬 bubble after a task id: side chat + load/pursue prompt
      pursueLinearTask(key);
    };
  });
  bindLinlinks(list);
}
async function pollFeed(){
  if(!$("#feed").classList.contains("open"))return;
  const r=await api("/api/events?limit=60");
  if(!r.ok)return;
  const ev=r.events||[];
  const newest=ev.length?(ev[0].ts+"|"+ev[0].tag+"|"+ev[0].session):"";
  $("#feedcount").textContent=ev.length?(ev.length+" recent"):"quiet";
  $("#feedlist").innerHTML=ev.map((e,i)=>{
    const fresh=(i===0&&newest!==feedSeen)?" fresh":"";
    const label=e.kind==="signal"?e.tag:(e.kind==="error"?e.tag:e.tag);
    return '<div class="fev k-'+e.tag+(e.kind==="error"?" k-error":"")+fresh+'">'
      +'<span class="fage">'+fage(e.ts)+'</span>'
      +'<span class="ftag">'+esc(label)+'</span>'
      +(e.session?'<span class="fsess">'+esc(e.session)+'</span>':'')
      +'<span class="ftext">'+linkifyLinear(e.text)+'</span></div>';
  }).join("")||'<div class="fev"><span class="ftext">no activity yet</span></div>';
  bindLinlinks($("#feedlist"));
  feedSeen=newest;
}
function setFeed(open){
  $("#feed").classList.toggle("open",open);
  localStorage.setItem("emux_feed",open?"1":"0");
  if(open)pollFeed();
}
$("#feedbtn").onclick=()=>setFeed(!$("#feed").classList.contains("open"));
$("#feedclose").onclick=()=>setFeed(false);
// mobile: side + feed are overlays, so start them closed to keep the main nav in view
const isNarrow=()=>window.matchMedia("(max-width:760px)").matches;
$("#navtoggle").onclick=()=>document.body.classList.toggle("nav-open");
$("#scrim").onclick=()=>document.body.classList.remove("nav-open");
setFeed(!isNarrow()&&localStorage.getItem("emux_feed")!=="0");   // open by default on desktop only
// the iTerm2 gui checkbox is a no-op off macOS — don't send it as checked there
if(document.documentElement.dataset.os!=="Darwin"){const g=$("#newgui");if(g)g.checked=false;}
setInterval(pollFeed,2000);

// --- Hancock: pending approvals surfaced loud, opened async, cleared in-app ---
let hancock=[], hSelected=null, htoastTimer=null;   // hSelected = command of the open tab
function hsess(h){ const s=(h.source||""); return s.indexOf("emux:")===0?s.slice(5):""; }
async function pollHancock(){
  let r; try{ r=await api("/api/hancock"); }catch(e){ return; }
  if(!r||!r.ok)return;
  hancock=r.pending||[];
  const n=hancock.length;
  $("#hbadge").textContent=n;$("#hbadge").style.display=n?"inline-block":"none";
  $("#hbtn").classList.toggle("hot",n>0);
  if(!hancock.some(h=>h.command===hSelected)) hSelected=n?hancock[0].command:null;
  renderApprovals();
}
function renderApprovals(){
  const wrap=$("#happrovals");
  if(!hancock.length){ wrap.style.display="none"; return; }
  wrap.style.display="";
  $("#htabs").innerHTML=hancock.map(h=>{
    const risk=(h.risk||"medium");
    return '<div class="htab r-'+risk+(h.command===hSelected?" active":"")+'" data-cmd="'+esc(h.command)+'">'
      +'<span class="hdot"></span>'+esc(h.source||h.command)
      +(h.count>1?'<span class="hcount">×'+h.count+'</span>':'')
      +'<span class="hx" data-cmd="'+esc(h.command)+'" title="deny">✕</span></div>';
  }).join("");
  $("#htabs").querySelectorAll(".htab").forEach(el=>el.onclick=e=>{
    if(e.target.classList.contains("hx"))return;
    hSelected=el.dataset.cmd;renderApprovals();
  });
  $("#htabs").querySelectorAll(".hx").forEach(el=>el.onclick=e=>{
    e.stopPropagation();const h=hancock.find(x=>x.command===el.dataset.cmd);if(h)hancockDo(h,0);
  });
  renderApprovalDetail();
}
function renderApprovalDetail(){
  const box=$("#hdetail");
  const h=hancock.find(x=>x.command===hSelected)||hancock[0];
  if(!h){box.innerHTML="";return;}
  const risk=(h.risk||"medium");
  // age off the OLDEST filing (first_created), not the newest — a coalesced
  // storm's newest row is always seconds old and would never read as stale
  const s=h.first_created?Math.max(0,(Date.now()-Date.parse(h.first_created))/1000):null;
  const age=s==null?"":(s<60?Math.floor(s)+"s":ago(s));
  const stale=s!=null&&s>3600;
  const sess=hsess(h);
  box.innerHTML=
    '<div class="hask">'+esc(h.why||h.command)+'</div>'
    +'<div class="hprov">'
      +(h.source?'<span class="hchip2">from '+esc(h.source)+'</span>':'')
      +(h.target?'<span class="harrow">&#8594;</span><span class="hchip2">'+esc(h.target)+'</span>':'')
    +'</div>'
    +'<div class="hmeta'+(stale?' stale':'')+'" title="'+esc(h.first_created||'')+'">'+esc(risk)
      +(h.count>1?' · filed ×'+h.count:'')
      +(age?' · '+(h.count>1?'first ':'')+age+' ago':'')
      +(stale?' · stale?':'')+'</div>'
    +'<div class="hcmd2">'+esc(h.command)+'</div>'
    +'<div class="hact2">'
      +'<button class="happrove" id="hap">approve &amp; run</button>'
      +'<button class="hdeny" id="hdn">deny</button>'
      +(sess?'<button class="hpeek" id="hpk">peek '+esc(sess)+'</button>':'')
    +'</div>';
  $("#hap").onclick=()=>hancockDo(h,1);
  $("#hdn").onclick=()=>hancockDo(h,0);
  if(sess){const b=$("#hpk");if(b)b.onclick=()=>{const g=grid.find(x=>x.name===sess);if(g)openModal(g);else htoast("session "+sess+" not in the grid","err");};}
}
function htoast(msg,kind){
  const t=$("#htoast");t.textContent=msg;t.className="show "+(kind||"");
  clearTimeout(htoastTimer);htoastTimer=setTimeout(()=>{t.className="";},kind==="err"?5000:2600);
}
async function hancockDo(h,ok){
  const ids=h.ids&&h.ids.length?h.ids:[h.id];
  const path=ok?"/api/hancock/approve":"/api/hancock/deny";
  let r; try{ r=await api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({ids})}); }
  catch(e){ r={ok:false,error:"unreachable"}; }
  if(!r.ok){ htoast((ok?"Approve":"Deny")+" failed — "+(r.error||"unknown"),"err"); return; }
  htoast(ok?("Approved & ran ✓"+(r.output?" — "+String(r.output).split("\n")[0].slice(0,60):"")):"Denied ✓","ok");
  hancock=hancock.filter(x=>x.command!==h.command);
  if(hSelected===h.command)hSelected=hancock.length?hancock[0].command:null;
  renderApprovals();pollHancock();
}
function openHancock(){ if(hancock.length){hSelected=hancock[0].command;renderApprovals();var w=$("#happrovals");if(w)w.scrollIntoView({block:"nearest"});} }
pollHancock();setInterval(pollHancock,3000);

// --- model routing settings ---
const TASK_LABELS={gist:"The Gist (session digest + suggested replies)",placement:"Session placement (new-session machine + name)"};
async function openSettings(){
  // Open immediately so Esc works during /api/models fetch (stress open/esc race).
  $("#setmodal").classList.add("open");
  $("#nimtestout").textContent="loading…";$("#nimtestout").className="";
  const r=await api("/api/models");
  if(!r.ok){
    $("#nimtestout").textContent="failed to load models";$("#nimtestout").className="err";
    return;
  }
  const c=r.config;
  $("#nimurl").value=c.nim.base_url||"";$("#nimmodel").value=c.nim.model||"";$("#nimkey").value=c.nim.api_key||"";
  $("#setroutes").innerHTML=(r.tasks||[]).map(t=>{
    const cur=c.routes[t]||"claude";
    return '<label class="setroute"><span>'+esc(TASK_LABELS[t]||t)+'</span>'
      +'<select data-task="'+t+'">'
      +'<option value="claude"'+(cur==="claude"?" selected":"")+'>claude -p (subscription)</option>'
      +'<option value="nim"'+(cur==="nim"?" selected":"")+'>NIM (local)</option>'
      +'</select></label>';
  }).join("");
  $("#nimtestout").textContent="";$("#setsaveout").textContent="";
}
function closeSettings(){$("#setmodal").classList.remove("open");}
async function testNim(){
  const out=$("#nimtestout");out.textContent="testing…";out.className="";
  await saveSettings(true);   // persist first so the server tests what you typed
  const r=await api("/api/models/test",{method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
  if(r.ok){out.textContent="✓ reachable"+(r.models&&r.models.length?" · "+r.models.slice(0,3).join(", "):"");out.className="ok";}
  else{out.textContent="✗ "+(r.error||"unreachable");out.className="err";}
}
async function saveSettings(quiet){
  const routes={};document.querySelectorAll("#setroutes select").forEach(s=>routes[s.dataset.task]=s.value);
  const body={nim:{base_url:$("#nimurl").value.trim(),model:$("#nimmodel").value.trim(),api_key:$("#nimkey").value},routes};
  const r=await api("/api/models",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(!quiet){const o=$("#setsaveout");o.textContent=r.ok?"saved ✓":"save failed";o.className=r.ok?"ok":"err";
    setTimeout(()=>{o.textContent="";},1800);}
  return r.ok;
}

applyURL();   // restore view + filters + open session from the URL (falls back to localStorage)
loadProductChrome();                     // MANAGER chip + managed-planes strip (product.json)
poll();gridTimer=setInterval(poll,2000);
setInterval(loadManagedHealth,30000);    // refresh managed plane health on managers
</script>
<script id="emux-theme">
// Wire the topbar button to the real applyTheme() (brand × light/dark).
(function(){
  function wire(){
    var b=document.getElementById("themebtn");
    if(!b||b._emuxThemeWired) return;
    b._emuxThemeWired=true;
    b.addEventListener("click",function(e){
      e.preventDefault();
      if(typeof toggleMode==="function") toggleMode();
    });
  }
  wire();
  document.addEventListener("DOMContentLoaded",wire);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class EmuxWebHandler(BaseHTTPRequestHandler):
    server_version = f"emux/{__version__}"
    # The host the daemon was bound to, when non-localhost; lets a deliberate
    # --host 0.0.0.0 accept its own LAN address while still blocking foreign ones.
    extra_host: str | None = None
    # Optional canonical browser origin when published through a trusted proxy.
    # Default remains loopback-only.
    public_origin: str | None = None
    # Optional path prefix when the reverse proxy mounts the UI under a path
    # (e.g. "/gmux" behind go.greenmarkwaste.com). Empty string = root mount.
    # Caddy handle_path strips this before the request hits us; the SPA still
    # needs the prefix on absolute fetch/href so the browser hits the proxy path.
    public_path: str = ""
    # Disabled unless supplied by an embedding service with a trusted identity
    # boundary. Never infer controller trust from localhost or browser headers.
    remote_controller_api: Any = None

    def _with_public_path(self, html: str) -> str:
        """Stamp reverse-proxy path + active skin into HTML/JS shells."""
        from . import skin as _skin
        html = html.replace("__PUBLIC_PATH__", self.public_path or "")
        return _skin.active_skin().apply(html, __version__)

    def _controller_error(self, exc: Exception) -> None:
        from .remote_control.protocol import ProtocolError
        if not isinstance(exc, ProtocolError):
            raise exc
        status = {"unauthorized": 401, "identity_mismatch": 403,
                  "wrong_server": 409, "protocol_skew": 409, "replay": 409,
                  "not_cancellable": 409, "unknown_request": 404}.get(exc.code, 400)
        self._json({"ok": False, "error": exc.code}, status)

    def _controller_headers(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self.headers.items()}

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client gave up (stress tests, tab close) — not a server fault.
            return

    def _host_allowed(self) -> bool:
        """Defeat DNS-rebinding on loopback; allow the configured public origin.

        Production go.greenmarkwaste.com is fronted by Railway (Host can be
        *.up.railway.app). Caddy may also rewrite Host to 127.0.0.1. Trust:
          - loopback Host
          - Host / X-Forwarded-Host matching public_origin hostname
          - Origin or Referer matching public_origin (browser SPA on the
            real public URL) — only meaningful when public_origin is set;
            the daemon remains loopback-bound so this is not a WAN open.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host in _LOCALHOSTS:
            return True
        if self.extra_host is not None and host == self.extra_host:
            return True
        if not self.public_origin:
            return False
        public_host = urlparse(self.public_origin).hostname
        if not public_host:
            return False
        xfh = (self.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        xfh = xfh.rsplit(":", 1)[0].strip("[]") if xfh else ""
        if host == public_host or xfh == public_host:
            return True
        origin = (self.headers.get("Origin") or "").rstrip("/")
        if origin == self.public_origin.rstrip("/"):
            return True
        referer = self.headers.get("Referer") or ""
        pub = self.public_origin.rstrip("/")
        if referer == pub or referer.startswith(pub + "/"):
            return True
        return False

    def _origin_allowed(self) -> bool:
        """Block cross-site writes: a POST carrying an Origin from any non-local
        site is a forged request from another tab. Same-origin and non-browser
        (no Origin) requests pass."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
            host = parsed.hostname
        except ValueError:
            return False
        if host in _LOCALHOSTS or (self.extra_host is not None and host == self.extra_host):
            return True
        return self.public_origin is not None and origin.rstrip("/") == self.public_origin

    def _send_html(self, html: str, status: int = 200) -> None:
        body = html.encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def _send_text(self, text: str, content_type: str = "text/markdown; charset=utf-8",
                   status: int = 200) -> None:
        body = text.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001
        """Swallow client disconnect noise; log real handler errors."""
        import traceback
        err = traceback.format_exc()
        if "BrokenPipeError" in err or "ConnectionResetError" in err:
            return
        super().handle_error(request, client_address)

    def _control_room_html(self) -> str:
        html = _help.control_room_page(PAGE, __version__).replace("__OS__", _host_os())
        return self._with_public_path(html)

    def _wants_ai_format(self, url) -> bool:
        qs = parse_qs(url.query or "")
        fmt = (qs.get("format") or qs.get("mode") or [""])[0].lower()
        if fmt in ("ai", "md", "markdown", "text", "txt"):
            return True
        accept = (self.headers.get("Accept") or "").lower()
        if "text/markdown" in accept or "text/plain" in accept:
            if "text/html" not in accept.split(",")[0]:
                return True
        return False

    def _strip_public_path(self) -> None:
        """Rewrite self.path so /gmux/api/* is handled like /api/* without Caddy.

        The SPA stamps PUBLIC_PATH=/gmux and fetches absolute `/gmux/api/grid`.
        Caddy handle_path strips that prefix; loopback, SSH tunnels, and chrime
        do not. Without this strip the request never matches /api/* and the room
        shows a hard failure (often misread as FORBIDDEN_HOST).
        """
        prefix = (getattr(self, "public_path", None) or "").rstrip("/")
        if not prefix:
            return
        parts = urlparse(self.path)
        path = parts.path or "/"
        if path == prefix:
            new_path = "/"
        elif path.startswith(prefix + "/"):
            new_path = path[len(prefix):] or "/"
        else:
            return
        # rebuild path?query
        self.path = new_path + (("?" + parts.query) if parts.query else "")

    def _simple_status_from_url(self, url) -> str:
        """Parse filter/peek query params for the simple status page."""
        qs = parse_qs(url.query or "")

        def _flag(name: str) -> bool:
            vals = qs.get(name) or []
            if not vals:
                return False
            return vals[0].lower() in ("1", "true", "yes", "on")

        peek_vals = qs.get("peek") or []
        peek = (peek_vals[0].strip() if peek_vals else "") or None
        try:
            line_vals = qs.get("lines") or []
            lines = int(line_vals[0]) if line_vals else 24
        except (TypeError, ValueError):
            lines = 24
        # Default live-only. all=1 shows registry ghosts. live=1 is explicit same as default.
        show_all = _flag("all")
        if "live" in qs and not show_all:
            live_only = _flag("live")
        else:
            live_only = not show_all
        return simple_status_html(
            __version__,
            self.public_path or "",
            live_only=live_only,
            registered_only=_flag("registered"),
            peek=peek,
            peek_lines=lines,
        )

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._strip_public_path()
        url = urlparse(self.path)
        # AI mode: plain markdown diagnosis (also ?format=ai on status routes).
        # Per-product health page (standing, cheap). Distinct from /healthz (JSON
        # probe) and /ai (deep diagnosis with pane/chat scans). Criteria evolve.
        if url.path in ("/health", "/health/", "/health.md"):
            qs = parse_qs(url.query or "")
            want_html = (qs.get("view") or [""])[0].lower() in ("html", "1", "yes")
            if want_html:
                self._send_html(health_page_html(__version__, self.public_path or ""))
            else:
                self._send_text(health_page_markdown(__version__, self.public_path or ""))
            return
        if url.path in ("/health.html", "/health.html/"):
            self._send_html(health_page_html(__version__, self.public_path or ""))
            return
        if url.path in ("/ai", "/ai/", "/ai.md", "/diagnosis", "/diagnosis/"):
            qs = parse_qs(url.query or "")
            want_html = (qs.get("view") or [""])[0].lower() in ("html", "1", "yes")
            if want_html or url.path.rstrip("/").endswith(".html"):
                html = _expensive_get(
                    f"ai_html:{self.public_path or ''}",
                    _EXPENSIVE_TTL["ai_html"],
                    lambda: ai_diagnosis_html(__version__, self.public_path or ""),
                )
                self._send_html(html)
            else:
                md = _expensive_get(
                    f"ai_md:{self.public_path or ''}",
                    _EXPENSIVE_TTL["ai_md"],
                    lambda: ai_diagnosis_markdown(__version__, self.public_path or ""),
                )
                self._send_text(md)
            return
        if url.path in ("/ai.html", "/ai.html/"):
            html = _expensive_get(
                f"ai_html:{self.public_path or ''}",
                _EXPENSIVE_TTL["ai_html"],
                lambda: ai_diagnosis_html(__version__, self.public_path or ""),
            )
            self._send_html(html)
            return
        # Simple status first: under a public path mount, "/" is the testable
        # read-only table; full SPA lives at /room. Local loopback (no path)
        # keeps "/" as the full control room for existing muscle memory.
        if url.path in ("/simple", "/simple/", "/status", "/status/"):
            if self._wants_ai_format(url):
                self._send_text(ai_diagnosis_markdown(__version__, self.public_path or ""))
                return
            self._send_html(self._simple_status_from_url(url))
            return
        if url.path in ("/room", "/room/"):
            if self._wants_ai_format(url):
                self._send_text(ai_diagnosis_markdown(__version__, self.public_path or ""))
                return
            self._send_html(self._control_room_html())
            return
        if url.path == "/" or url.path == "/index.html":
            if self._wants_ai_format(url):
                self._send_text(ai_diagnosis_markdown(__version__, self.public_path or ""))
                return
            if self.public_path:
                self._send_html(self._simple_status_from_url(url))
            else:
                self._send_html(self._control_room_html())
            return
        if url.path in ("/docs", "/docs/"):
            self._send_html(self._with_public_path(_help.docs_page(__version__)))
            return
        if url.path == "/healthz":
            # Unguarded on purpose: leaks nothing, lets launchd/monitoring probe liveness.
            live = sessions_payload()
            n = len([s for s in live.get("sessions", []) if s.get("live")]) if live.get("ok") else 0
            body: dict[str, Any] = {"ok": True, "version": __version__, "live_sessions": n}
            try:
                from . import product_config as _pc
                from . import skin as _skin

                cfg = _pc.load_product_config(_skin.active_skin().id)
                body["product"] = cfg.product
                body["role"] = cfg.role
                if cfg.is_manager:
                    body["managed_planes"] = [p.id for p in cfg.managed_planes]
            except Exception:
                pass
            self._json(body)
            return
        if url.path.startswith("/api/"):
            if not self._host_allowed():
                self._json({"ok": False, "error": "forbidden_host"}, 403)
                return
        if url.path == "/api/controller/v1/capabilities":
            if self.remote_controller_api is None:
                self._json({"ok": False, "error": "controller_api_disabled"}, 503)
                return
            try:
                self._json(self.remote_controller_api.capabilities(self._controller_headers()))
            except Exception as exc:
                self._controller_error(exc)
            return
        if url.path == "/api/controller/v1/sessions":
            # Read-only server-scoped discovery: enumerate targetable local sessions so a
            # remote controller/MCP can list without prior knowledge (controller sufficiency).
            if self.remote_controller_api is None:
                self._json({"ok": False, "error": "controller_api_disabled"}, 503)
                return
            try:
                self._json(self.remote_controller_api.sessions(self._controller_headers()))
            except Exception as exc:
                self._controller_error(exc)
            return
        match = re.fullmatch(r"/api/controller/v1/requests/([^/]+)", url.path)
        if match:
            if self.remote_controller_api is None:
                self._json({"ok": False, "error": "controller_api_disabled"}, 503)
                return
            try:
                status, payload = self.remote_controller_api.status(
                    self._controller_headers(), match.group(1))
                self._json(payload, status)
            except Exception as exc:
                self._controller_error(exc)
            return
        if url.path == "/api/sessions":
            self._json(
                _expensive_get(
                    "sessions",
                    _EXPENSIVE_TTL["sessions"],
                    sessions_payload,
                )
            )
            return
        if url.path == "/api/help":
            query = (parse_qs(url.query).get("q") or [""])[0]
            payload = _help.answer(query)
            self._json(payload, 200 if payload.get("ok") else 400)
            return
        if url.path == "/api/grid":
            q = parse_qs(url.query)
            try:
                lines = max(1, min(100, int((q.get("lines") or ["14"])[0])))
            except ValueError:
                lines = 14
            self._json(grid_payload(lines))
            return
        if url.path == "/api/capture":
            q = parse_qs(url.query)
            session = (q.get("session") or [""])[0]
            if not session:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            try:
                lines = max(1, min(5000, int((q.get("lines") or ["300"])[0])))
            except ValueError:
                lines = 300
            # a registered remote session captures over ssh
            self._json(capture_payload(session, lines, host=_session_host(session)))
            return
        if url.path == "/api/classify":
            q = parse_qs(url.query)
            name = (q.get("name") or [""])[0]
            if not name:
                self._json({"ok": False, "error": "missing_name"}, 400)
                return
            from . import judge
            try:
                self._json({"ok": True, **judge.classify_session(name)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
            return
        if url.path == "/api/events":
            q = parse_qs(url.query)
            try:
                lim = max(1, min(200, int((q.get("limit") or ["60"])[0])))
            except ValueError:
                lim = 60
            self._json({"ok": True, "events": _events(lim)})
            return
        if url.path == "/api/hosts":
            self._json({"ok": True, "hosts": _known_hosts()})
            return
        if url.path == "/api/hancock":
            pend = _hancock_pending()
            self._json({"ok": True, "pending": pend, "count": len(pend)})
            return
        if url.path == "/api/models":
            self._json({"ok": True, "config": _model_config(), "tasks": list(_TASKS)})
            return
        if url.path == "/api/plans":
            now = time.time()
            plans = [{**p, "available": _plan_available(p["name"], now),
                      "resets_in": max(0, int((_PLAN_EXHAUSTED.get(p["name"], now)) - now))}
                     for p in _plans()]
            self._json({"ok": True, "plans": plans, "session_plan": dict(_SESSION_PLAN),
                        "count": len(plans)})
            return
        if url.path == "/api/agents":
            from . import agents as _agents
            q = (parse_qs(url.query).get("scenario") or [""])[0]
            self._json({"ok": True, **(_agents.advise(q) if q else _agents.table())})
            return
        if url.path == "/api/peek":
            q = parse_qs(url.query)
            sess = (q.get("session") or [""])[0]
            h = (q.get("host") or ["local"])[0]
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            try:
                lines = max(1, min(50, int((q.get("lines") or ["12"])[0])))
            except ValueError:
                lines = 12
            self._json(_peek_session(sess, None if h in ("", "local") else h,
                                     lines=lines))
            return
        if url.path == "/api/dirs":
            # what exists ON the chosen machine — the cascade's 2nd level.
            # Two kinds: sessions you can RESUME, and dirs you can start NEW in.
            h = (parse_qs(url.query).get("host") or ["local"])[0]
            rh = None if h in ("", "local") else h
            self._json({"ok": True, "host": h,
                        "dirs": _candidate_dirs(rh),
                        "running": _running_sessions(rh)})
            return
        if url.path == "/api/chats":
            # Claude Code + Grok Build transcripts on disk (this host). Not tmux.
            # Single-flight cache so concurrent Chats tab polls share one scan (EID-1109).
            qs = parse_qs(url.query)
            cache_key = "chats:" + urlencode(
                {k: (v[0] if v else "") for k, v in sorted(qs.items())}
            )
            self._json(
                _expensive_get(
                    cache_key,
                    _EXPENSIVE_TTL["chats"],
                    lambda: chats_payload(qs),
                )
            )
            return
        if url.path == "/api/chats/peek":
            self._json(chats_peek_payload(parse_qs(url.query)))
            return
        if url.path in ("/api/schedule", "/api/schedule/"):
            # In-process cron message jobs (product-scoped schedule.json).
            # Optional ?from=&to= ISO expand occurrences for calendar views.
            # Calendar paints skeleton immediately; keep this path light — skip
            # next_run_at when expanding a range (UI uses events, not next).
            try:
                from datetime import datetime, timedelta

                from . import schedule as _sched

                qs = parse_qs(url.query)
                fr = (qs.get("from") or [""])[0].strip()
                to = (qs.get("to") or [""])[0].strip()
                with_range = bool(fr and to)
                payload: dict[str, Any] = {
                    "ok": True,
                    "path": str(_sched.schedule_path()),
                    "product": _sched._product_id(),
                    "jobs": _sched.list_jobs(with_next=not with_range),
                }
                if with_range:
                    try:
                        start = datetime.fromisoformat(fr.replace("Z", "+00:00"))
                        end = datetime.fromisoformat(to.replace("Z", "+00:00"))
                        if start.tzinfo is None:
                            start = start.replace(tzinfo=UTC)
                        if end.tzinfo is None:
                            end = end.replace(tzinfo=UTC)
                    except ValueError:
                        start = datetime.now(UTC).replace(
                            hour=0, minute=0, second=0, microsecond=0
                        )
                        end = start + timedelta(days=7)
                    payload["from"] = start.isoformat()
                    payload["to"] = end.isoformat()
                    payload["events"] = _sched.occurrences(start, end)
                self._json(payload)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path in ("/api/product", "/api/product/"):
            # Worker vs manager role + managed plane registry (config-driven).
            try:
                from . import product_config as _pc
                from . import skin as _skin

                cfg = _pc.load_product_config(_skin.active_skin().id)
                self._json({"ok": True, **cfg.as_dict()})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path in ("/api/managed", "/api/managed/"):
            # Manager-only: probe configured planes (healthz URLs from product.json).
            try:
                from . import product_config as _pc
                from . import skin as _skin

                cfg = _pc.load_product_config(_skin.active_skin().id)
                if not cfg.is_manager:
                    self._json(
                        {
                            "ok": False,
                            "error": "not_a_manager",
                            "role": cfg.role,
                            "hint": "only manager products expose /api/managed",
                        },
                        400,
                    )
                    return
                # Cache + stale-while-revalidate: concurrent room tabs must not
                # re-probe every external healthz (EID-1107, EID-1115).
                probe = _expensive_get(
                    f"managed:{cfg.product}:{cfg.path}",
                    _EXPENSIVE_TTL["managed"],
                    lambda: _probe_managed_planes(cfg),
                    stale_ttl=_EXPENSIVE_TTL["managed_stale"],
                )
                self._json({"ok": True, **probe})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        self._json({"ok": False, "error": "not_found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        self._strip_public_path()
        url = urlparse(self.path)
        controller_submit = url.path == "/api/controller/v1/requests"
        controller_cancel = re.fullmatch(
            r"/api/controller/v1/requests/([^/]+)/cancel", url.path)
        if controller_submit or controller_cancel:
            if not self._host_allowed():
                self._json({"ok": False, "error": "forbidden_host"}, 403)
                return
            if self.remote_controller_api is None:
                self._json({"ok": False, "error": "controller_api_disabled"}, 503)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self._json({"ok": False, "error": "bad_json"}, 400)
                return
            try:
                if controller_submit:
                    status, payload = self.remote_controller_api.submit(
                        self._controller_headers(), data)
                else:
                    assert controller_cancel is not None
                    status, payload = self.remote_controller_api.cancel(
                        self._controller_headers(), controller_cancel.group(1), data)
                self._json(payload, status)
            except Exception as exc:
                self._controller_error(exc)
            return
        if url.path not in ("/api/send", "/api/head", "/api/spawn", "/api/suggest",
                            "/api/adopt", "/api/reply", "/api/chats/resume",
                            "/api/clip-image", "/api/steer", "/api/scroll",
                            "/api/hancock/approve", "/api/hancock/deny",
                            "/api/models", "/api/models/test", "/api/plan/switch",
                            "/api/schedule", "/api/schedule/",
                            "/api/schedule/run", "/api/schedule/delete",
                            "/api/schedule/update"):
            self._json({"ok": False, "error": "not_found"}, 404)
            return
        if not self._host_allowed():
            self._json({"ok": False, "error": "forbidden_host"}, 403)
            return
        if not self._origin_allowed():
            self._json({"ok": False, "error": "forbidden_origin"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            # image paste can be a few MB of base64
            if length > 12 * 1024 * 1024:
                self._json({"ok": False, "error": "body_too_large"}, 413)
                return
            data = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._json({"ok": False, "error": "bad_json"}, 400)
            return
        if url.path == "/api/clip-image":
            self._json(_clip_image_save(data if isinstance(data, dict) else {}))
            return
        if url.path in ("/api/schedule", "/api/schedule/"):
            # Add a cron message job: {cron, target, message, timezone?, id?}
            try:
                from . import schedule as _sched

                body = data if isinstance(data, dict) else {}
                job = _sched.add_job(
                    cron=str(body.get("cron") or ""),
                    target=str(body.get("target") or ""),
                    message=str(body.get("message") or ""),
                    timezone=str(body.get("timezone") or "America/Chicago"),
                    job_id=(str(body["id"]).strip() if body.get("id") else None),
                    title=(str(body.get("title") or "").strip() or None),
                    enabled=bool(body.get("enabled", True)),
                )
                self._json({"ok": True, "job": job.as_dict(), "path": str(_sched.schedule_path())})
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path == "/api/schedule/update":
            try:
                from . import schedule as _sched

                body = data if isinstance(data, dict) else {}
                jid = str(body.get("id") or "").strip()
                if not jid:
                    self._json({"ok": False, "error": "missing_id"}, 400)
                    return
                fields = {
                    k: body[k]
                    for k in ("cron", "target", "message", "title", "timezone", "enabled")
                    if k in body
                }
                job = _sched.update_job(jid, **fields)
                if not job:
                    self._json({"ok": False, "error": "unknown_job", "id": jid}, 404)
                    return
                self._json({"ok": True, "job": job.as_dict()})
            except ValueError as exc:
                self._json({"ok": False, "error": str(exc)}, 400)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path == "/api/schedule/run":
            try:
                from . import schedule as _sched

                jid = str((data or {}).get("id") or "").strip()
                if not jid:
                    self._json({"ok": False, "error": "missing_id"}, 400)
                    return
                self._json(_sched.fire_by_id(jid, force=True))
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path == "/api/schedule/delete":
            try:
                from . import schedule as _sched

                jid = str((data or {}).get("id") or "").strip()
                if not jid:
                    self._json({"ok": False, "error": "missing_id"}, 400)
                    return
                ok = _sched.remove_job(jid)
                self._json({"ok": ok, "id": jid, **({} if ok else {"error": "unknown_job"})})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return
        if url.path == "/api/suggest":
            intent = (data.get("intent") or "").strip()
            if not intent:
                self._json({"ok": False, "error": "missing_intent"}, 400)
                return
            # a machine the user already picked pins the cascade; else the model picks it
            pinned = (data.get("host") or "").strip()
            r = _suggest_session(intent, host=pinned or None)
            if "_error" in r:
                self._json({"ok": False, "error": r["_error"]})
            else:
                self._json({"ok": True, **r})
            return
        if url.path == "/api/spawn":
            self._json(_spawn_session(data))
            return
        if url.path == "/api/chats/resume":
            # Abandoned transcript → live registered fleet session (local host).
            self._json(_resume_chat_in_fleet(data))
            return
        if url.path == "/api/steer":
            # Grok headless (B) / ACP (C) / tmux PTY — control plane steer
            self._json(steer_payload(data if isinstance(data, dict) else {}))
            return
        if url.path == "/api/scroll":
            # Wheel / PgUp → tmux history OR app alt-screen (Claude), auto-detected
            sess = (data.get("session") or data.get("name") or "").strip()
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            self._json(scroll_payload(
                sess,
                direction=str(data.get("direction") or "up"),
                amount=str(data.get("amount") or "page"),
                host=_session_host(sess),
                mode=(str(data.get("mode") or "auto") or "auto"),
            ))
            return
        if url.path == "/api/reply":
            sess = (data.get("session") or "").strip()
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            self._json(_reply_suggestions(sess, _session_host(sess)))
            return
        if url.path == "/api/adopt":
            self._json(_adopt_session(data))
            return
        if url.path == "/api/models":
            self._json({"ok": True, "config": _save_model_config(data)})
            return
        if url.path == "/api/models/test":
            self._json(_nim_ping())
            return
        if url.path == "/api/plan/switch":
            sess = (data.get("session") or "").strip()
            if not sess:
                self._json({"ok": False, "error": "missing_session"}, 400)
                return
            self._json(_switch_plan(sess, host=_session_host(sess),
                                    to=(data.get("to") or None),
                                    dry_run=bool(data.get("dry_run"))))
            return
        if url.path in ("/api/hancock/approve", "/api/hancock/deny"):
            # Support both single id and ids list (for group-aware operations)
            ids = data.get("ids")
            if not isinstance(ids, list) or not ids:
                rid = (data.get("id") or "").strip()
                ids = [rid] if rid else []
            ids = [str(i).strip() for i in ids if str(i).strip()]
            if not ids:
                self._json({"ok": False, "error": "missing_id"}, 400)
                return
            if url.path.endswith("approve"):
                # Approve only the first (newest); deny the rest as redundant
                result = _hancock_approve(ids[0])
                for rid in ids[1:]:
                    _hancock_deny(rid, reason="redundant duplicate")
                self._json(result)
            else:
                # Deny all ids in the group
                errors = []
                for rid in ids:
                    deny_result = _hancock_deny(rid)
                    if not deny_result.get("ok"):
                        errors.append(deny_result.get("error", "unknown error"))
                if errors:
                    self._json({"ok": False, "error": errors[0]})
                else:
                    self._json({"ok": True})
            return
        session = data.get("session")
        if not isinstance(session, str) or not session:
            self._json({"ok": False, "error": "missing_session"}, 400)
            return
        if url.path == "/api/head":
            # a registered session may live on a remote box — attach over ssh
            reg = _server._load_registry()
            entry = next((e for e in reg.values() if e.get("session") == session), {})
            ok, err = _iterm_attach(session, entry.get("host"))
            self._json({"ok": ok, "error": err} if not ok else {"ok": True, "session": session})
            return
        keys = data.get("keys")
        if not isinstance(keys, str):
            self._json({"ok": False, "error": "missing_keys"}, 400)
            return
        self._json(send_payload(
            session,
            keys,
            literal=bool(data.get("literal", True)),
            enter=bool(data.get("enter", True)),
            host=_session_host(session),   # steer a remote worker over ssh
        ))

    def log_message(self, format: str, *args: Any) -> None:
        # Quiet by default; the poll traffic would otherwise flood the terminal.
        pass


def launchd_plist(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    public_origin: str | None = None,
    public_path: str = "",
    skin: str = "",
) -> str:
    """A ready-to-install launchd plist that keeps `emux web` running and
    restarts it on crash / login. Print with `emux web --print-launchd`.

    Includes launchd-safe PATH + absolute EMUX_*_BIN when resolvable (launchd
    otherwise ships PATH=/usr/bin:/bin only).
    """
    from pathlib import Path

    emux = sys.argv[0]
    extra = ""
    if public_origin:
        extra += f"\n    <string>--public-origin</string><string>{public_origin}</string>"
    if public_path:
        extra += f"\n    <string>--public-path</string><string>{public_path}</string>"
    if skin:
        extra += f"\n    <string>--skin</string><string>{skin}</string>"

    home = str(Path.home())
    path_parts = [
        f"{home}/.local/bin",
        f"{home}/.grok/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    path_val = ":".join(path_parts)
    skin_key = (skin or os.environ.get("EMUX_SKIN") or "").strip().lower()
    if skin_key in ("reevux", "reeves", "personal", "rvs"):
        label = "com.reeves.reevux-web"
        log_base = "reevux-web"
    elif skin_key in ("gmux", "greenmux", "greenmark"):
        label = "com.eidos.gmux-web"
        log_base = "gmux-web"
    elif skin_key in ("amux", "aic"):
        label = "com.eidos.amux-web"
        log_base = "amux-web"
    else:
        label = "com.eidos.emux-web"
        log_base = "emux-web"

    env_items: list[tuple[str, str]] = [
        ("HOME", home),
        ("PATH", path_val),
        ("EMUX_SKIN", skin_key or "emux"),
    ]
    grok = _resolve_cli("grok") or os.environ.get("EMUX_GROK_BIN") or ""
    claude = _resolve_cli("claude") or os.environ.get("EMUX_CLAUDE_BIN") or ""
    if grok:
        env_items.append(("EMUX_GROK_BIN", grok))
    if claude:
        env_items.append(("EMUX_CLAUDE_BIN", claude))
    if skin_key in ("reevux", "reeves", "personal", "rvs"):
        env_items.append(("EMUX_CONNECT_SSH", os.environ.get("EMUX_CONNECT_SSH") or "mac-mini-01"))
    env_xml = "\n".join(
        f"    <key>{k}</key><string>{v}</string>" for k, v in env_items
    )

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{emux}</string>
    <string>web</string>
    <string>--host</string><string>{host}</string>
    <string>--port</string><string>{port}</string>{extra}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}
  </dict>
  <key>WorkingDirectory</key><string>{home}</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/{log_base}.log</string>
  <key>StandardErrorPath</key><string>/tmp/{log_base}.err.log</string>
</dict>
</plist>
"""


def normalize_public_path(path: str | None) -> str | None:
    """Return a normalized path prefix ('' or '/gmux') or None if invalid.

    Rules: empty/None → ''; must start with /; no trailing slash; no //, ., ..,
    query, fragment, or whitespace. Used by Caddy handle_path mounts where the
    proxy strips the prefix before the daemon sees the request.
    """
    if path is None or path == "":
        return ""
    p = path.strip()
    if not p.startswith("/") or p == "/":
        return None
    if p.endswith("/") or "//" in p or "?" in p or "#" in p or " " in p:
        return None
    segments = [s for s in p.split("/") if s]
    if not segments or any(s in (".", "..") for s in segments):
        return None
    return "/" + "/".join(segments)


def _fmt_age(seconds: float | int | None) -> str:
    """Human age for uptime / last-activity columns."""
    if seconds is None:
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "—"
    if s < 0:
        s = 0
    if s < 2:
        return "now"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def connect_command(
    session: str,
    *,
    socket_path: str | None = None,
    socket_name: str = "default",
    ssh_host: str | None = None,
) -> str:
    """One shell line: attach to this tmux session (optionally over ssh).

    From a laptop to rentamac greenmux this is typically:
      ssh -t rentamac 'tmux attach -t <session>'
    Non-default sockets use -S path or -L name. Local (no ssh_host) is bare tmux.
    """
    import shlex

    sess = shlex.quote(session)
    # Prefer short form on the default server; pin -S/-L only when non-default.
    if socket_name and socket_name not in ("default", "", "local"):
        if socket_path:
            tmux = f"tmux -S {shlex.quote(socket_path)} attach -t {sess}"
        else:
            tmux = f"tmux -L {shlex.quote(socket_name)} attach -t {sess}"
    elif socket_path and not str(socket_path).endswith("/default"):
        tmux = f"tmux -S {shlex.quote(socket_path)} attach -t {sess}"
    else:
        tmux = f"tmux attach -t {sess}"
    if ssh_host:
        return f"ssh -t {shlex.quote(ssh_host)} {shlex.quote(tmux)}"
    return tmux


def health_page_markdown(version: str, public_path: str = "") -> str:
    """Standing per-product health page — cheap, product-stamped, evolvable.

    Distinct from:
      - /healthz  — machine JSON liveness probe
      - /ai       — deep diagnosis (pane peeks, chat scans, full playbook)

    Each emux product instance (emux / amux / gmux / reevux / directrux) serves
    its own page under its public_path. Health *criteria* start minimal and grow.
    """
    from datetime import datetime

    from . import skin as _skin

    sk = _skin.active_skin()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    base = public_path or ""

    # Cheap liveness — same core as /healthz (no pane peeks, no chat disk scan).
    live_n = 0
    sessions_ok = False
    sessions_err = ""
    try:
        payload = sessions_payload()
        sessions_ok = bool(payload.get("ok"))
        sessions_err = str(payload.get("error") or "")
        if sessions_ok:
            live_n = len([s for s in (payload.get("sessions") or []) if s.get("live")])
    except Exception as exc:  # noqa: BLE001
        sessions_err = str(exc)[:200]

    role = "worker"
    product = sk.product
    chats_match = ""
    cfg_path = ""
    managed_section = ""
    mgr_verdict = ""
    try:
        from . import product_config as _pc

        cfg = _pc.load_product_config(sk.id)
        product = cfg.product or product
        role = cfg.role or role
        chats_match = cfg.chats_match or ""
        cfg_path = str(cfg.path or "")
        if cfg.is_manager:
            probe = _probe_managed_planes(cfg)
            planes = probe.get("planes") or []
            down = [p for p in planes if not p.get("ok")]
            deg = [p for p in planes if p.get("ok") and p.get("degraded")]
            if not planes:
                mgr_verdict = "FAIL"
            elif down:
                mgr_verdict = "FAIL"
            elif deg:
                mgr_verdict = "DEGRADED"
            else:
                mgr_verdict = "HEALTHY"
            rows = [
                "",
                "## Managed planes",
                "",
                "| plane | lane | host | ok | degraded | reason | live | probe |",
                "|---|---|---|---|---|---|---|---|",
            ]
            for p in planes:
                rows.append(
                    f"| {p.get('id') or '—'} | {p.get('lane') or '—'} | {p.get('host') or '—'} | "
                    f"{p.get('ok')} | {p.get('degraded')} | {p.get('reason') or '—'} | "
                    f"{p.get('live_sessions') if p.get('live_sessions') is not None else '—'} | "
                    f"{p.get('probe_kind') or '—'} |"
                )
            if not planes:
                rows.append("| _(empty allowlist)_ | | | | | | | |")
            rows += [
                "",
                f"- managed_verdict: **{mgr_verdict}**",
                f"- workers_ok: {probe.get('workers_ok')} (auth_gated counts as reachable)",
                f"- healthy / auth_gated: {probe.get('healthy')} / {probe.get('auth_gated')}",
                "- reason=auth_gated means public OIDC/login, not plane DOWN — use healthz_loopback",
                "",
            ]
            managed_section = "\n".join(rows)
    except Exception as exc:  # noqa: BLE001
        managed_section = f"\n## Managed planes\n\n_(probe failed: {exc})_\n"

    # Minimal evolving criteria (v0). Expand over time — do not pretend completeness.
    reasons: list[str] = []
    if not sessions_ok and not mgr_verdict:
        verdict = "FAIL"
        reasons.append(f"session inventory not ok: {sessions_err or 'unknown'}")
    elif mgr_verdict == "FAIL":
        verdict = "FAIL"
        reasons.append("one or more managed worker planes DOWN or allowlist empty")
    elif mgr_verdict == "DEGRADED":
        verdict = "DEGRADED"
        reasons.append(
            "managed plane(s) degraded — auth_gated public healthz (OIDC) and/or "
            "non-JSON; set healthz_loopback for operational truth (not the same as DOWN)"
        )
    elif not sessions_ok:
        verdict = "DEGRADED"
        reasons.append(f"local session inventory flaky: {sessions_err or 'unknown'}")
    else:
        verdict = "HEALTHY"
        if mgr_verdict == "HEALTHY":
            reasons.append("all managed worker planes healthy")
        reasons.append(f"daemon up · {live_n} live local session(s) counted")

    lines = [
        f"# {product} health",
        "",
        f"- generated: {now}",
        f"- product: **{product}** (skin=`{sk.id}`)",
        f"- role: **{role}**",
        f"- engine: emux {version}",
        f"- public_path: `{base or '/'}`",
        f"- chats_match: `{chats_match or '(engine default)'}`",
        f"- product_config: `{cfg_path or '(none)'}`",
        f"- live_sessions: {live_n}",
        f"- verdict: **{verdict}**",
        "",
        "## Why this verdict",
        "",
    ]
    for r in reasons:
        lines.append(f"- {r}")
    if managed_section:
        lines.append(managed_section)

    lines += [
        "",
        "## Health criteria (evolving)",
        "",
        "These are the **current** checks. Expect this list to grow; absence of a",
        "check does not mean the concern is irrelevant — only that we have not",
        "automated it on this page yet.",
        "",
        "| # | criterion | status on this page |",
        "|---|-----------|---------------------|",
        "| 1 | Daemon answers HTTP | **checked** (you are reading this) |",
        f"| 2 | Session inventory ok | **checked** → sessions_ok={sessions_ok} |",
        f"| 3 | Live session count (informational) | **reported** → {live_n} |",
        (
            f"| 4 | Managed worker healthz (manager only) | "
            f"**{'checked → ' + mgr_verdict if mgr_verdict else 'n/a (worker)'}** |"
        ),
        "| 5 | Abandoned / stale agent chats | *not yet — use `/ai` or `emux chats`* |",
        "| 6 | Pane gates / stuck sessions | *not yet — use `/ai` or room* |",
        "| 7 | Disk / launchd / Tailscale serve | *not yet* |",
        "| 8 | Cross-lane isolation (no hat mix) | *policy — not automated here* |",
        "",
        "## Links",
        "",
        f"- health (this page, markdown): `{base}/health`",
        f"- health (HTML): `{base}/health.html`",
        f"- healthz (JSON probe): `{base}/healthz`",
        f"- deep diagnosis (AI): `{base}/ai` · `{base}/ai.html`",
        f"- status table: `{base}/` or `{base}/status`",
        f"- control room: `{base}/room`",
        "",
        "## How to use",
        "",
        "1. Bookmark **this product's** `/health` — do not use another plane's page.",
        "2. For managers: fix managed planes before fretting about local session noise.",
        "3. For deep signal (panes, chats): open `/ai` — slower on purpose.",
        "4. For machines/launchd: hit `/healthz` (JSON, unguarded).",
        "",
        f"— end {product} health —",
        "",
    ]
    return "\n".join(lines)


def health_page_html(version: str, public_path: str = "") -> str:
    """Human-readable wrapper for the standing health page."""
    import html as _html

    from . import skin as _skin

    sk = _skin.active_skin()
    md = health_page_markdown(version, public_path)
    base = public_path or ""
    return sk.apply(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PRODUCT__ health</title>
<style id="skin-theme">__THEME_CSS__</style>
<link rel="icon" href="__FAVICON__">
<style>
body{{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}}
header{{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);
 display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}}
.brand-row{{display:flex;align-items:center;gap:10px}}
.brand-row .skin-logo{{color:var(--amber)}}
.brand-word{{font-weight:600;color:var(--ink)}}
.meta{{font-size:12px;color:var(--dim)}}
.meta a{{color:var(--ink)}}
#themebtn,button.copy{{font:12px system-ui;cursor:pointer;border:1px solid var(--line);
 background:var(--card);color:var(--ink);padding:6px 12px;border-radius:6px}}
main{{padding:16px 20px 40px;max-width:900px}}
pre{{white-space:pre-wrap;word-break:break-word;background:var(--bg-raise);border:1px solid var(--line);
 border-radius:8px;padding:16px;font:12px/1.45 ui-monospace,Menlo,monospace;color:var(--ink)}}
.hint{{font-size:12px;color:var(--dim);margin:0 0 12px}}
</style>
</head><body>
<header>
  <div>
    <div class="brand-row">__LOGO_HTML__</div>
    <div class="meta">standing health · product-scoped ·
      <a href="{base}/">status</a> ·
      <a href="{base}/health">raw markdown</a> ·
      <a href="{base}/ai.html">deep diagnosis</a> ·
      <a href="{base}/room">__TAGLINE__</a> ·
      <a href="{base}/healthz">healthz</a>
    </div>
  </div>
  <div style="display:flex;gap:8px">
    <button type="button" class="copy" id="copyhealth">copy all</button>
    <button type="button" id="themebtn">☾ dark</button>
  </div>
</header>
<main>
  <p class="hint">This is <b>__PRODUCT__</b>'s health page — not another plane's.
  Criteria evolve. Raw for agents: <code>{base}/health</code> (text/markdown).
  Deep scan: <code>{base}/ai</code>.</p>
  <pre id="health">{_html.escape(md)}</pre>
</main>
<script>
document.getElementById("copyhealth").onclick=function(){{
  var t=document.getElementById("health").textContent;
  var b=this;
  if(navigator.clipboard&&navigator.clipboard.writeText){{
    navigator.clipboard.writeText(t).then(function(){{b.textContent="copied";setTimeout(function(){{b.textContent="copy all"}},1200)}});
  }}
}};
(function(){{
  var KEY="__THEME_STORAGE_KEY__";
  var def="__DEFAULT_THEME__";
  function pref(){{
    try{{var s=localStorage.getItem(KEY); if(s==="light"||s==="dark")return s;}}catch(e){{}}
    if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return def==="dark"?"dark":"light";
  }}
  function apply(t){{
    document.documentElement.setAttribute("data-theme", t);
    var b=document.getElementById("themebtn");
    if(b) b.textContent=t==="dark"?"☀ light":"☾ dark";
  }}
  function toggle(){{
    var next=(document.documentElement.getAttribute("data-theme")||pref())==="dark"?"light":"dark";
    try{{localStorage.setItem(KEY,next);}}catch(e){{}}
    apply(next);
  }}
  apply(pref());
  var b=document.getElementById("themebtn");
  if(b) b.onclick=toggle;
}})();
</script>
</body></html>
""",
        version,
    )


def ai_diagnosis_markdown(
    version: str,
    public_path: str = "",
    *,
    include_panes: bool = True,
    pane_lines: int = 12,
) -> str:
    """Plain markdown diagnosis of the fleet — for AIs and humans who want signal.

    No HTML chrome. Designed so a model can answer "is anything broken?" from one
    document: scope honesty, live vs ghost, unknown sockets, connect commands,
    optional short pane samples from LIVE sessions only.
    """
    from datetime import datetime

    from . import skin as _skin

    sk = _skin.active_skin()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = sessions_payload()
    sessions = payload.get("sessions") or []
    scope = payload.get("scope") or {}
    ok = bool(payload.get("ok"))
    err = payload.get("error") or ""

    live = [s for s in sessions if s.get("live")]
    ghosts = [
        s for s in sessions
        if s.get("registered") and not s.get("live") and s.get("state") != "unknown"
    ]
    unknown = [s for s in sessions if s.get("state") == "unknown"]
    unregistered_live = [s for s in live if not s.get("registered")]

    # Manager products: managed-plane health is the primary diagnosis signal.
    mgr_section = ""
    mgr_verdict = ""
    try:
        from . import product_config as _pc

        cfg = _pc.load_product_config(sk.id)
        if cfg.is_manager:
            # Share the same short-TTL cache as /api/managed (avoid double probe on cold /ai).
            probe = _expensive_get(
                f"managed:{cfg.product}:{cfg.path}",
                _EXPENSIVE_TTL["managed"],
                lambda: _probe_managed_planes(cfg),
                stale_ttl=_EXPENSIVE_TTL["managed_stale"],
            )
            planes = probe.get("planes") or []
            down = [p for p in planes if not p.get("ok")]
            deg = [p for p in planes if p.get("ok") and p.get("degraded")]
            if not planes:
                mgr_verdict = "FAIL"
            elif down:
                mgr_verdict = "FAIL"
            elif deg:
                mgr_verdict = "DEGRADED"
            else:
                mgr_verdict = "HEALTHY"
            lines_m = [
                "",
                "## Managed planes (manager allowlist — primary job)",
                "",
                "- product role: **manager**",
                f"- config: `{cfg.path or 'default (no product.json)'}`",
                f"- chats_match: `{cfg.chats_match}` (never worker dumps)",
                f"- managed_ids: {', '.join(sorted(cfg.managed_ids())) or '(empty)'}",
                f"- workers_ok: {probe.get('workers_ok')}",
                f"- managed_verdict: **{mgr_verdict}**",
                "",
                "| plane | lane | host | ok | degraded | live | version | healthz | fix |",
                "|---|---|---|---|---|---|---|---|---|",
            ]
            for p in planes:
                fix = "—"
                pid = p.get("id") or ""
                if not p.get("ok"):
                    if pid == "amux":
                        fix = "`launchctl kickstart -k gui/$(id -u)/com.eidos.amux-web`; curl Tailscale `/amux/healthz`"
                    elif pid == "gmux":
                        fix = "`ssh rentamac 'curl -sS http://127.0.0.1:8689/healthz'`; public URL may be OIDC-gated"
                    elif pid == "reevux":
                        fix = "`ssh mac-mini-01 'launchctl kickstart -k gui/$(id -u)/com.reeves.reevux-web'`"
                    else:
                        fix = f"check host={p.get('host')} healthz={p.get('healthz')}"
                elif p.get("degraded"):
                    fix = f"reachable but degraded: {p.get('error') or 'see healthz'}; prefer loopback/SSH over auth-gated public"
                room = p.get("room") or ""
                room_cell = room if room else "—"
                lines_m.append(
                    f"| {pid} | {p.get('lane') or '—'} | {p.get('host') or '—'} | "
                    f"{p.get('ok')} | {p.get('degraded')} | "
                    f"{p.get('live_sessions') if p.get('live_sessions') is not None else '—'} | "
                    f"{p.get('version') or '—'} | {p.get('healthz') or '—'} | {fix} |"
                )
                if room:
                    lines_m.append(f"| | | | | | | | room | {room_cell} |")
            lines_m += [
                "",
                "### Manager fix playbook",
                "",
                "1. Prefer **managed_verdict** over local laptop session noise.",
                "2. DOWN plane → kick that product's launchd on its host; re-probe healthz.",
                "3. Do **not** resume personal chats under amux or AIC under reevux.",
                "4. Open the **worker room** URL from the table to steer that plane.",
                "5. Standing health chat session name: `directrux-health` (manager only).",
                "6. Refresh report: `curl -sS http://127.0.0.1:8691/ai` (or public `/directrux/ai`).",
                "",
            ]
            mgr_section = "\n".join(lines_m)
    except Exception as exc:  # noqa: BLE001
        mgr_section = f"\n## Managed planes\n\n_(probe failed: {exc})_\n"

    # Verdict — conservative, explicit reasons
    reasons: list[str] = []
    if mgr_verdict == "FAIL":
        verdict = "FAIL"
        reasons.append("one or more managed worker planes are DOWN or allowlist empty")
    elif not ok:
        verdict = "FAIL"
        reasons.append(f"sessions_payload not ok: {err or 'unknown'}")
    elif mgr_verdict == "DEGRADED":
        verdict = "DEGRADED"
        reasons.append(
            "managed plane(s) degraded — auth_gated public healthz (OIDC) and/or "
            "non-JSON; set healthz_loopback for operational truth (not the same as DOWN)"
        )
    elif any(s.get("status") == "unknown" for s in (scope.get("sockets") or [])):
        verdict = "DEGRADED"
        reasons.append("one or more tmux sockets unreadable (unknown)")
    elif not live and ghosts:
        verdict = "DEGRADED"
        reasons.append(f"no LIVE sessions; {len(ghosts)} registry ghost(s)")
    elif not live and not mgr_verdict:
        verdict = "DEGRADED"
        reasons.append("no LIVE tmux sessions on scanned sockets")
    else:
        verdict = "HEALTHY"
        if mgr_verdict == "HEALTHY":
            reasons.append("all managed worker planes healthy")
        reasons.append(f"{len(live)} live local session(s) on scanned sockets")
    if unregistered_live:
        reasons.append(
            f"{len(unregistered_live)} live session(s) not in registry "
            f"(visible but unmanaged): "
            + ", ".join(str(s.get("name")) for s in unregistered_live[:8])
        )
    if ghosts:
        reasons.append(f"{len(ghosts)} stale registry row(s) kept (not reaped)")

    # Claude Code + Grok Build chats on disk (dropped missions)
    chat_section = ""
    try:
        from . import chats as chat_find
        from . import product_config as _pc2

        _chat_match = _pc2.default_chats_match_for_skin(sk.id)
        chat_hits = chat_find.find_chats(
            match=_chat_match,
            recent_hours=24.0,
            statuses=["stale", "recent"],
            limit=25,
        )
        stale_chats = [h for h in chat_hits if h.status == "stale"]
        if stale_chats and not (sk.id == "directrux" or _pc2.load_product_config(sk.id).is_manager):
            reasons.append(
                f"{len(stale_chats)} stale agent chat(s) on disk (Claude/Grok) — "
                "may be dropped missions; resume before starting new agents"
            )
            if verdict == "HEALTHY":
                verdict = "DEGRADED"
        if chat_hits and not _pc2.load_product_config(sk.id).is_manager:
            chat_section = "\n" + chat_find.format_text(
                chat_hits,
                title="Abandoned / nonoperative agent chats (Claude + Grok)",
            )
            chat_section += f"\nCLI: `emux chats --match {_chat_match} --abandoned-only`\n"
    except Exception as exc:  # noqa: BLE001
        chat_section = f"\n## Abandoned agent chats\n\n_(scan failed: {exc})_\n"

    ssh_host = resolve_connect_ssh_host(public_path)
    lines: list[str] = [
        f"# {sk.product} system diagnosis",
        "",
        f"- generated: {now}",
        f"- product: {sk.product} (skin={sk.id})",
        f"- engine: {sk.engine_label} {version}",
        f"- public_path: {public_path or '/'}",
        f"- verdict: **{verdict}**",
        "",
        "## Why this verdict",
    ]
    for r in reasons:
        lines.append(f"- {r}")
    if mgr_section:
        lines.append(mgr_section)
    lines += [
        "",
        "## Local host scan scope (secondary for managers)",
        "",
        f"{scope.get('claim') or 'scope not scanned yet'}",
        "",
        "This is NOT all host processes, NOT other users' tmux, NOT bare ssh/nohup.",
        "For **manager** products, prefer **Managed planes** above over local noise.",
        "",
        "### Sockets probed",
        "",
    ]
    socks = scope.get("sockets") or []
    if not socks:
        lines.append("- (none recorded)")
    else:
        for skt in socks:
            lines.append(
                f"- `{skt.get('socket')}` status={skt.get('status')} "
                f"n={skt.get('n')} path={skt.get('path') or '—'} "
                f"err={skt.get('error') or '—'}"
            )

    lines += [
        "",
        "## Counts",
        "",
        f"- live: {len(live)}",
        f"- ghosts (stale registered): {len(ghosts)}",
        f"- unknown sockets: {len(unknown)}",
        f"- registered total: {sum(1 for s in sessions if s.get('registered'))}",
        f"- rows total: {len(sessions)}",
        "",
        "## Sessions",
        "",
        "| name | tmux | host | socket | state | registered | tags | description |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in sessions:
        tags = ",".join(s.get("tags") or []) or "—"
        desc = (s.get("description") or "—").replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {s.get('name')} | {s.get('session')} | {s.get('host') or 'local'} | "
            f"{s.get('socket') or 'default'} | {s.get('state') or ('live' if s.get('live') else 'stale')} | "
            f"{'yes' if s.get('registered') else 'no'} | {tags} | {desc} |"
        )

    lines += ["", "## Connect commands (LIVE only)", ""]
    if not live:
        lines.append("- (none — no live sessions)")
    else:
        for s in live:
            if str(s.get("session") or "").startswith("socket:"):
                continue
            cmd = connect_command(
                str(s.get("session")),
                socket_path=s.get("socket_path"),
                socket_name=str(s.get("socket") or "default"),
                ssh_host=ssh_host,
            )
            lines.append(f"- `{s.get('name')}`: `{cmd}`")

    if include_panes and live:
        # Cold /ai used to capture EVERY live pane serially (~0.5s × N → 15–30s).
        # Cap + prioritize permanent seats + parallelize so diagnosis stays < budget.
        from concurrent.futures import ThreadPoolExecutor, as_completed

        max_pane_samples = int(os.environ.get("EMUX_AI_PANE_SAMPLES", "8"))
        max_pane_samples = max(0, min(max_pane_samples, 24))
        priority_names = (
            "directrux-vp",
            "directrux-engine",
            "directrux-health",
            "fleet-vp",
        )

        def _pane_rank(s: dict[str, Any]) -> tuple:
            name = str(s.get("name") or s.get("session") or "")
            try:
                prio = priority_names.index(name)
            except ValueError:
                prio = 100
            # registered first, then name for stability
            return (prio, 0 if s.get("registered") else 1, name)

        candidates = [
            s
            for s in live
            if not str(s.get("session") or "").startswith("socket:")
        ]
        candidates.sort(key=_pane_rank)
        sample_sessions = candidates[:max_pane_samples]
        skipped = max(0, len(candidates) - len(sample_sessions))

        lines += [
            "",
            f"## Pane samples (last {pane_lines} lines, LIVE only)",
            "",
            "Use these to see if agents are stuck on gates, idle shells, or errors.",
            f"Showing **{len(sample_sessions)}** of {len(candidates)} live"
            + (f" (skipped {skipped} for latency; set EMUX_AI_PANE_SAMPLES)" if skipped else "")
            + ".",
            "",
        ]

        def _one_pane(s: dict[str, Any]) -> tuple[str, str, str]:
            tmux_name = str(s.get("session") or "")
            label = str(s.get("name") or tmux_name)
            try:
                cap = capture_payload(
                    tmux_name,
                    lines=max(5, min(pane_lines, 80)),
                    host=s.get("host"),
                    socket=s.get("socket_path"),
                )
            except Exception as exc:  # noqa: BLE001
                return label, tmux_name, f"(capture failed: {exc})"
            if not cap.get("ok"):
                return label, tmux_name, f"(capture failed: {cap.get('error')})"
            content = (cap.get("content") or "").rstrip("\n")
            sample = "\n".join(content.splitlines()[-pane_lines:])
            return label, tmux_name, sample if sample else "(empty pane)"

        pane_rows: list[tuple[str, str, str]] = []
        if sample_sessions:
            workers = min(6, len(sample_sessions))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_one_pane, s): s for s in sample_sessions}
                try:
                    for fut in as_completed(futs, timeout=8.0):
                        try:
                            pane_rows.append(fut.result(timeout=0.1))
                        except Exception as exc:  # noqa: BLE001
                            s = futs[fut]
                            pane_rows.append(
                                (
                                    str(s.get("name") or "?"),
                                    str(s.get("session") or "?"),
                                    f"(capture failed: {exc})",
                                )
                            )
                except TimeoutError:
                    done_names = {r[0] for r in pane_rows}
                    for s in sample_sessions:
                        nm = str(s.get("name") or s.get("session") or "?")
                        if nm not in done_names:
                            pane_rows.append(
                                (nm, str(s.get("session") or "?"), "(capture timeout)")
                            )
            # Keep priority order in the report
            order = {
                str(s.get("name") or s.get("session") or ""): i
                for i, s in enumerate(sample_sessions)
            }
            pane_rows.sort(key=lambda r: order.get(r[0], 99))

        for label, tmux_name, sample in pane_rows:
            lines.append(f"### {label} (`{tmux_name}`)")
            lines.append("```")
            lines.append(sample)
            lines.append("```")
            lines.append("")

    lines += [
        "## How an AI should use this",
        "",
        "1. Read **verdict** first — FAIL/DEGRADED/HEALTHY.",
        "2. If this product is a **manager**: fix **Managed planes** first (worker healthz/launchd/room).",
        "3. If DEGRADED with only ghosts: work died; registry still has names — do not treat as running.",
        "4. If unknown sockets: inventory is incomplete; do not claim full host coverage.",
        "5. Unregistered LIVE rows are real processes — consider registering for governance.",
        "6. Connect commands are for a human laptop (ssh → tmux attach), not for inventing new sessions.",
        "7. Pane samples beat guessing — look for gates, errors, idle shells.",
        "8. Stale Claude/Grok chats are often dropped missions — resume those before spawning new agents.",
        "9. Do not dump personal into AIC/Greenmark while fixing manager health.",
        "",
        f"— end {sk.product} diagnosis —",
        "",
    ]
    body = "\n".join(lines)
    if chat_section:
        # insert chat section before "How an AI should use this"
        marker = "## How an AI should use this"
        if marker in body:
            body = body.replace(marker, chat_section + "\n" + marker, 1)
        else:
            body = body + "\n" + chat_section
    return body


def ai_diagnosis_html(version: str, public_path: str = "") -> str:
    """Human-readable wrapper around the AI markdown (copy-friendly)."""
    import html as _html

    from . import skin as _skin

    sk = _skin.active_skin()
    # Reuse the markdown cache so /ai.html after /ai (or concurrent) is free.
    md = _expensive_get(
        f"ai_md:{public_path or ''}",
        _EXPENSIVE_TTL["ai_md"],
        lambda: ai_diagnosis_markdown(version, public_path),
    )
    base = public_path or ""
    return sk.apply(
        f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__PRODUCT__ AI diagnosis</title>
<style id="skin-theme">__THEME_CSS__</style>
<link rel="icon" href="__FAVICON__">
<style>
body{{margin:0;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--ink)}}
header{{padding:16px 20px;border-bottom:1px solid var(--line);background:var(--card);
 display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between}}
.brand-row{{display:flex;align-items:center;gap:10px}}
.brand-row .skin-logo{{color:var(--amber)}}
.brand-word{{font-weight:600;color:var(--ink)}}
.meta{{font-size:12px;color:var(--dim)}}
.meta a{{color:var(--ink)}}
#themebtn,button.copy{{font:12px system-ui;cursor:pointer;border:1px solid var(--line);
 background:var(--card);color:var(--ink);padding:6px 12px;border-radius:6px}}
main{{padding:16px 20px 40px;max-width:900px}}
pre{{white-space:pre-wrap;word-break:break-word;background:var(--bg-raise);border:1px solid var(--line);
 border-radius:8px;padding:16px;font:12px/1.45 ui-monospace,Menlo,monospace;color:var(--ink)}}
.hint{{font-size:12px;color:var(--dim);margin:0 0 12px}}
</style>
</head><body>
<header>
  <div>
    <div class="brand-row">__LOGO_HTML__</div>
    <div class="meta">AI mode · plain diagnosis ·
      <a href="{base}/">status UI</a> ·
      <a href="{base}/health.html">health</a> ·
      <a href="{base}/ai">raw markdown</a> ·
      <a href="{base}/room">__TAGLINE__</a>
    </div>
  </div>
  <div style="display:flex;gap:8px">
    <button type="button" class="copy" id="copyai">copy all</button>
    <button type="button" id="themebtn">☾ dark</button>
  </div>
</header>
<main>
  <p class="hint">Paste this whole page into an AI to diagnose the fleet.
  Raw URL for agents: <code>{base}/ai</code> (Content-Type: text/markdown).</p>
  <pre id="diag">{_html.escape(md)}</pre>
</main>
<script>
document.getElementById("copyai").onclick=function(){{
  var t=document.getElementById("diag").textContent;
  var b=this;
  if(navigator.clipboard&&navigator.clipboard.writeText){{
    navigator.clipboard.writeText(t).then(function(){{b.textContent="copied";setTimeout(function(){{b.textContent="copy all"}},1200)}});
  }}
}};
(function(){{
  var KEY="__THEME_STORAGE_KEY__";
  var def="__DEFAULT_THEME__";
  function pref(){{
    try{{var s=localStorage.getItem(KEY); if(s==="light"||s==="dark")return s;}}catch(e){{}}
    if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return def==="dark"?"dark":"light";
  }}
  function apply(t){{
    document.documentElement.setAttribute("data-theme", t);
    var b=document.getElementById("themebtn");
    if(b) b.textContent=t==="dark"?"☀ light":"☾ dark";
  }}
  function toggle(){{
    var next=(document.documentElement.getAttribute("data-theme")||pref())==="dark"?"light":"dark";
    try{{localStorage.setItem(KEY,next);}}catch(e){{}}
    apply(next);
  }}
  apply(pref());
  var b=document.getElementById("themebtn");
  if(b) b.onclick=toggle;
}})();
</script>
</body></html>
""",
        version,
    )


def resolve_connect_ssh_host(public_path: str = "") -> str | None:
    """SSH destination for connect-copy.

    Env wins. Else skin defaults: gmux → rentamac, reevux → mac-mini-01,
    amux → local (no remote ssh prefix).
    public_path without skin still defaults to rentamac (gmux go-door legacy).
    """
    env = (os.environ.get("EMUX_CONNECT_SSH") or os.environ.get("GREENMUX_HOST") or "").strip()
    if env:
        return env
    try:
        from .skin import active_skin
        sid = active_skin().id
        if sid == "gmux":
            return "rentamac"
        if sid == "reevux":
            return "mac-mini-01"
        if sid == "amux":
            return None  # local laptop — plain tmux attach
    except Exception:
        pass
    if public_path:  # reverse-proxied status without skin ⇒ attach on mux host
        return "rentamac"
    return None


def simple_status_html(
    version: str,
    public_path: str = "",
    *,
    live_only: bool = True,
    registered_only: bool = False,
    peek: str | None = None,
    peek_lines: int = 24,
) -> str:
    """Server-rendered, read-only session table — the first testable view.

    Default filter is live-only (ghosts stay available via "all"). Filters, age,
    and pane peek are query-string driven. Scope stamp states what tmux sockets
    were actually scanned — not "all work on the host".
    """
    import html as _html
    from datetime import datetime
    from urllib.parse import urlencode

    base = public_path or ""
    ssh_host = resolve_connect_ssh_host(public_path)
    now_ts = time.time()
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    payload = sessions_payload()
    all_sessions = payload.get("sessions") or []
    scope = payload.get("scope") or {}
    live_n = sum(1 for s in all_sessions if s.get("live"))
    reg_n = sum(1 for s in all_sessions if s.get("registered"))
    ghost_n = sum(
        1 for s in all_sessions
        if s.get("registered") and not s.get("live") and s.get("state") != "unknown"
    )
    unknown_n = sum(1 for s in all_sessions if s.get("state") == "unknown")
    ok = bool(payload.get("ok"))
    err = payload.get("error") or ""

    sessions = list(all_sessions)
    if live_only:
        # keep unknown scan rows even in live-only — they are not ghosts, they are gaps
        sessions = [s for s in sessions if s.get("live") or s.get("state") == "unknown"]
    if registered_only:
        sessions = [s for s in sessions if s.get("registered") or s.get("state") == "unknown"]

    def _qs(**extra: Any) -> str:
        """Build query string. Default view is live-only → use all=1 for ghosts."""
        q: dict[str, str] = {}
        # start from current mode
        show_all = (not live_only) if "all" not in extra else bool(extra.get("all"))
        if extra.get("all") is False:
            show_all = False
        if extra.get("live") is True:
            show_all = False
        if extra.get("live") is False and "all" not in extra:
            show_all = True
        if show_all:
            q["all"] = "1"
        if registered_only and "registered" not in extra:
            q["registered"] = "1"
        if peek and "peek" not in extra:
            q["peek"] = peek
        for k, v in extra.items():
            if k in ("live", "all") and v is False:
                continue
            if v is None or v is False or v == "":
                q.pop(k, None)
                continue
            if v is True:
                if k == "live":
                    q.pop("all", None)
                    continue  # live is default — no query needed
                q[k] = "1"
            else:
                q[k] = str(v)
        if extra.get("registered") is False:
            q.pop("registered", None)
        if extra.get("peek") is None and "peek" in extra:
            q.pop("peek", None)
        return ("?" + urlencode(q)) if q else ""

    def _href(**extra: Any) -> str:
        return f"{base}/{_qs(**extra)}"

    # Filter chips (server-rendered links — no JS)
    def chip(label: str, active: bool, **extra: Any) -> str:
        cls = "chip on" if active else "chip"
        return f'<a class="{cls}" href="{_html.escape(_href(**extra))}">{_html.escape(label)}</a>'

    filters_html = (
        chip("live", live_only and not registered_only, live=True, all=False, registered=False)
        + chip(
            f"all ({ghost_n} ghost{'s' if ghost_n != 1 else ''})",
            not live_only and not registered_only,
            all=True,
            registered=False,
        )
        + chip("registered", registered_only and not live_only, all=True, registered=True)
        + chip("live · registered", live_only and registered_only, live=True, registered=True)
    )

    rows: list[str] = []
    peek_block = ""
    peek_found = False
    for s in sessions:
        name_raw = str(s.get("name") or "")
        tmux_raw = str(s.get("session") or "")
        name = _html.escape(name_raw)
        tmux = _html.escape(tmux_raw)
        host = _html.escape(str(s.get("host") or "local"))
        sock = _html.escape(str(s.get("socket") or "default"))
        desc = _html.escape(str(s.get("description") or "—"))
        is_live = bool(s.get("live"))
        state = s.get("state") or ("live" if is_live else "stale")
        if state == "unknown":
            live, live_cls = "unknown", "unknown"
        elif is_live:
            live, live_cls = "LIVE", "live"
        else:
            live, live_cls = "stale", "stale"
        reg = "yes" if s.get("registered") else "no"
        tags = _html.escape(", ".join(s.get("tags") or []) or "—")

        created = s.get("created_unix")
        uptime = _fmt_age((now_ts - created) if created else None) if is_live else "—"
        # activity is keyed by tmux session name in the poller cache
        meta = _meta(tmux_raw) if is_live else {}
        act_age = meta.get("last_change_age")
        if state == "unknown":
            active = "—"
        elif is_live:
            active = _fmt_age(act_age) if act_age is not None else "quiet"
        else:
            active = "gone"

        is_peek = peek is not None and peek in (name_raw, tmux_raw)
        if is_peek:
            peek_found = True
        open_href = _html.escape(_href(peek=name_raw))
        close_href = _html.escape(_href(peek=None))
        if state == "unknown":
            name_cell = f"<code>{name}</code>"
        elif is_peek:
            name_cell = (
                f'<a class="name" href="{close_href}" title="close peek"><code>{name}</code> ▾</a>'
            )
        else:
            name_cell = (
                f'<a class="name" href="{open_href}" title="peek pane"><code>{name}</code></a>'
            )
        # Connect: laptop → ssh → tmux attach (live only). Ghosts have nothing to attach to.
        if is_live and not tmux_raw.startswith("socket:"):
            cmd = connect_command(
                tmux_raw,
                socket_path=s.get("socket_path"),
                socket_name=str(s.get("socket") or "default"),
                ssh_host=ssh_host,
            )
            cmd_esc = _html.escape(cmd)
            cmd_attr = _html.escape(cmd, quote=True)
            connect_cell = (
                f'<div class="connect">'
                f'<code class="cmd" title="run on your laptop">{cmd_esc}</code>'
                f'<button type="button" class="copy" data-cmd="{cmd_attr}">copy</button>'
                f"</div>"
            )
        else:
            connect_cell = '<span class="dim">—</span>'
        row_cls = f"{live_cls}{' open' if is_peek else ''}"
        rows.append(
            f"<tr class='{row_cls}'>"
            f"<td>{name_cell}</td>"
            f"<td><code>{tmux}</code></td>"
            f"<td>{host}</td>"
            f"<td><code>{sock}</code></td>"
            f"<td><span class='pill {live_cls}'>{live}</span></td>"
            f"<td class='age' title='session uptime'>{uptime}</td>"
            f"<td class='age' title='time since last pane change'>{active}</td>"
            f"<td>{reg}</td>"
            f"<td>{tags}</td>"
            f"<td class='connect-td'>{connect_cell}</td>"
            f"<td>{desc}</td>"
            f"</tr>"
        )
        if is_peek:
            if not is_live:
                pane_html = (
                    "<p class='peek-err'>Session is not live — no pane to capture "
                    "(ghosts are kept on purpose; they are not reaped).</p>"
                )
            else:
                cap = capture_payload(
                    tmux_raw,
                    lines=max(5, min(int(peek_lines), 200)),
                    host=s.get("host"),
                    socket=s.get("socket_path"),
                )
                if not cap.get("ok"):
                    pane_html = (
                        f"<p class='peek-err'>capture failed: "
                        f"{_html.escape(str(cap.get('error') or 'unknown'))}</p>"
                    )
                else:
                    content = (cap.get("content") or "").rstrip("\n")
                    lines = content.splitlines()
                    if len(lines) > peek_lines:
                        lines = lines[-peek_lines:]
                    content = "\n".join(lines)
                    pane_html = f"<pre class='pane'>{_html.escape(content) if content else '(empty pane)'}</pre>"
            rows.append(
                f"<tr class='peek-row'><td colspan='11'>"
                f"<div class='peek'>"
                f"<div class='peek-bar'>pane peek · <code>{name}</code> · last {peek_lines} lines · "
                f"<a href='{close_href}'>close</a></div>"
                f"{pane_html}"
                f"</div></td></tr>"
            )

    if peek and not peek_found:
        peek_block = (
            f"<div class='summary bad'>No session named "
            f"<code>{_html.escape(peek)}</code> in the current filter.</div>"
        )

    body_rows = "\n".join(rows) if rows else (
        "<tr><td colspan='11' class='empty'>No sessions match this filter.</td></tr>"
    )
    connect_hint = (
        f"copy · run on your laptop → ssh { _html.escape(ssh_host) } → tmux attach"
        if ssh_host
        else "copy · run locally → tmux attach"
    )
    shown = len([s for s in sessions if s.get("state") != "unknown"])
    status_line = (
        f"ok · showing {shown} · {live_n} live · {ghost_n} ghost · "
        f"{reg_n} registered · {unknown_n} unknown socket"
        if ok else f"error · {_html.escape(str(err))}"
    )
    claim = _html.escape(str(scope.get("claim") or "scope not yet scanned"))
    sock_bits = []
    for sk in scope.get("sockets") or []:
        st = sk.get("status") or "?"
        nm = _html.escape(str(sk.get("socket") or "?"))
        n = sk.get("n")
        sock_bits.append(f"<code>{nm}</code>:{st}" + (f"({n})" if n else ""))
    scope_html = " · ".join(sock_bits) if sock_bits else "—"
    # meta-refresh keeps filters + peek
    refresh_url = _html.escape(f"{base}/{_qs()}")

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15;url={refresh_url}">
<title>__STATUS_TITLE__</title>
<style id="skin-theme">__THEME_CSS__</style>
<link rel="icon" href="__FAVICON__">
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.45 system-ui,sans-serif; background:var(--bg); color:var(--ink); }}
  header {{ padding:20px 24px 12px; border-bottom:1px solid var(--line); background:var(--card);
            display:flex; flex-wrap:wrap; align-items:flex-start; justify-content:space-between; gap:12px; }}
  .hdr-left {{ min-width:0; }}
  .brand-row {{ display:flex; align-items:center; gap:10px; margin:0 0 4px; }}
  .brand-row .skin-logo {{ color:var(--amber); flex:none; }}
  .brand-row .brand-word {{ font:600 18px/1.2 system-ui,sans-serif; color:var(--ink); letter-spacing:.02em; }}
  h1 {{ margin:0; font-size:18px; font-weight:600; color:var(--ink); }}
  .meta {{ color:var(--dim); font-size:12px; }}
  .meta a {{ color:var(--ink); }}
  #themebtn {{ font:12px system-ui,sans-serif; cursor:pointer; border:1px solid var(--line);
               background:var(--card); color:var(--ink); padding:6px 12px; border-radius:6px; }}
  #themebtn:hover {{ border-color:var(--amber); color:var(--amber); }}
  main {{ padding:16px 24px 40px; max-width:1200px; }}
  .summary {{ margin:0 0 12px; padding:10px 12px; background:var(--card);
              border:1px solid var(--line); border-radius:6px; font-size:13px; }}
  .summary.bad {{ border-color:#c45; color:#822; }}
  .filters {{ display:flex; flex-wrap:wrap; gap:6px; margin:0 0 14px; }}
  .chip {{ display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px;
           text-decoration:none; border:1px solid var(--line); color:var(--dim); background:var(--card); }}
  .chip.on {{ background:var(--on); color:var(--on-accent); border-color:var(--on); }}
  table {{ width:100%; border-collapse:collapse; background:var(--card);
           border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
  th, td {{ text-align:left; padding:8px 10px; border-bottom:1px solid var(--line);
            vertical-align:top; font-size:13px; }}
  th {{ font-size:11px; text-transform:uppercase; letter-spacing:.04em; color:var(--dim);
       background:var(--bg-raise); }}
  tr:last-child td {{ border-bottom:0; }}
  tr.open td {{ background:var(--pill); }}
  a.name {{ color:var(--ink); text-decoration:none; }}
  a.name:hover {{ color:var(--live); text-decoration:underline; }}
  code {{ font:12px/1.4 ui-monospace,Menlo,monospace; }}
  .age {{ white-space:nowrap; color:var(--dim); font-variant-numeric:tabular-nums; }}
  .pill {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:11px;
           font-weight:600; letter-spacing:.03em; }}
  .pill.live {{ background:var(--pill); color:var(--live); }}
  .pill.stale {{ background:var(--bg-raise); color:var(--stale); }}
  .pill.unknown {{ background:var(--bg-raise); color:var(--text-dim); }}
  .empty {{ color:var(--dim); text-align:center; padding:24px; }}
  .dim {{ color:var(--dim); }}
  .scope {{ margin:0 0 14px; padding:8px 12px; background:var(--bg-raise); border:1px dashed var(--line);
            border-radius:6px; font-size:12px; color:var(--dim); }}
  .scope strong {{ color:var(--ink); font-weight:600; }}
  .connect {{ display:flex; flex-direction:column; gap:4px; max-width:28rem; }}
  .connect .cmd {{ display:block; font:11px/1.35 ui-monospace,Menlo,monospace;
                   white-space:pre-wrap; word-break:break-all; color:var(--ink);
                   background:var(--bg-raise); padding:6px 8px; border-radius:4px; border:1px solid var(--line); }}
  .connect .copy {{ align-self:flex-start; font:11px system-ui,sans-serif; cursor:pointer;
                    border:1px solid var(--line); background:var(--card); color:var(--ink);
                    padding:3px 10px; border-radius:4px; }}
  .connect .copy:hover {{ border-color:var(--live); color:var(--live); }}
  .connect .copy.ok {{ background:var(--pill); color:var(--live); border-color:var(--live); }}
  .peek {{ background:#0f1411; color:#d7e0d9; border-radius:6px; overflow:hidden; }}
  [data-theme=dark] .peek {{ background:#0a100c; }}
  .peek-bar {{ padding:8px 12px; font-size:12px; color:#9aab9f; border-bottom:1px solid #243028; }}
  .peek-bar a {{ color:#9fd6b0; }}
  .peek-bar code {{ color:#e8f5ee; }}
  pre.pane {{ margin:0; padding:12px 14px; font:12px/1.45 ui-monospace,Menlo,monospace;
              white-space:pre-wrap; word-break:break-word; max-height:420px; overflow:auto; }}
  .peek-err {{ margin:0; padding:12px 14px; color:#e8b4b4; }}
  tr.peek-row td {{ padding:0; border-bottom:1px solid var(--line); }}
  footer {{ margin-top:14px; color:var(--dim); font-size:12px; }}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <div class="brand-row">__LOGO_HTML__</div>
    <h1>__STATUS_TITLE__</h1>
    <div class="meta">
      __PRODUCT_LINE__ · read-only · auto-refresh 15s ·
      <a href="{base}/room">__TAGLINE__</a> ·
      <a href="{base}/health.html">health</a> ·
      <a href="{base}/ai.html">AI mode</a> ·
      <a href="{base}/ai">AI markdown</a> ·
      <a href="{base}/healthz">healthz</a>
      <span class="dim"> · __FOOTER_NOTE__</span>
    </div>
  </div>
  <button type="button" id="themebtn" title="toggle light/dark">☾ dark</button>
</header>
<main>
  <div class="summary{' bad' if not ok else ''}" id="summary">{status_line} · checked {now}</div>
  <div class="scope"><strong>scan scope</strong> — {claim}<br>sockets: {scope_html}<br>
    <strong>connect</strong> — {connect_hint}</div>
  <div class="filters">{filters_html}</div>
  {peek_block}
  <table>
    <thead>
      <tr>
        <th>name</th><th>tmux</th><th>host</th><th>socket</th><th>state</th>
        <th>uptime</th><th>active</th>
        <th>registered</th><th>tags</th><th>connect</th><th>description</th>
      </tr>
    </thead>
    <tbody>
{body_rows}
    </tbody>
  </table>
  <footer>
    Default is live tmux only; ghosts stay under <strong>all</strong> (not reaped).
    Click a name to peek. <strong>copy</strong> the connect line, paste in your laptop terminal.
    Path prefix: <code>{_html.escape(base or "/")}</code>
  </footer>
</main>
<script>
document.querySelectorAll("button.copy").forEach(function(btn){{
  btn.addEventListener("click", function(){{
    var t = btn.getAttribute("data-cmd") || "";
    function done(ok){{
      btn.textContent = ok ? "copied" : "select+copy";
      btn.classList.toggle("ok", !!ok);
      setTimeout(function(){{ btn.textContent = "copy"; btn.classList.remove("ok"); }}, 1400);
    }}
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(t).then(function(){{ done(true); }}).catch(function(){{ done(false); }});
    }} else {{
      done(false);
    }}
  }});
}});
</script>
<script id="emux-theme">
(function(){{
  var KEY="__THEME_STORAGE_KEY__";
  var def="__DEFAULT_THEME__";
  function pref(){{
    try{{var s=localStorage.getItem(KEY); if(s==="light"||s==="dark")return s;}}catch(e){{}}
    if(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
    return (def==="dark")?"dark":"light";
  }}
  function apply(t){{
    document.documentElement.setAttribute("data-theme", t);
    var b=document.getElementById("themebtn");
    if(b){{b.textContent=t==="dark"?"☀ light":"☾ dark";}}
  }}
  function toggle(){{
    var cur=document.documentElement.getAttribute("data-theme")||pref();
    var next=cur==="dark"?"light":"dark";
    try{{localStorage.setItem(KEY,next);}}catch(e){{}}
    apply(next);
  }}
  apply(pref());
  document.addEventListener("DOMContentLoaded",function(){{
    apply(pref());
    var b=document.getElementById("themebtn");
    if(b) b.addEventListener("click",toggle);
  }});
}})();
</script>
</body>
</html>
"""
    from . import skin as _skin
    return _skin.active_skin().apply(html, version)


def _remote_controller_from_env() -> Any:
    """Build the optional remote API from an explicit, fail-closed environment."""
    token = os.environ.get("EMUX_REMOTE_CONTROLLER_TOKEN")
    if not token:
        return None
    required = {
        "EMUX_REMOTE_CONTROLLER_HUMAN_UID": os.environ.get("EMUX_REMOTE_CONTROLLER_HUMAN_UID"),
        "EMUX_REMOTE_CONTROLLER_DEVICE_ID": os.environ.get("EMUX_REMOTE_CONTROLLER_DEVICE_ID"),
        "EMUX_REMOTE_CONTROLLER_ID": os.environ.get("EMUX_REMOTE_CONTROLLER_ID"),
        "EMUX_REMOTE_CONTROLLER_SERVER_ID": os.environ.get("EMUX_REMOTE_CONTROLLER_SERVER_ID"),
        "EMUX_REMOTE_CONTROLLER_ALIASES": os.environ.get("EMUX_REMOTE_CONTROLLER_ALIASES"),
        "EMUX_REMOTE_CONTROLLER_STATE": os.environ.get("EMUX_REMOTE_CONTROLLER_STATE"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            "remote controller is partially configured; missing " + ", ".join(missing)
        )
    from .remote_control.api import (
        DenyConsequentialGate,
        RemoteConfig,
        RemoteControllerAPI,
        StaticTokenBoundary,
        TrustedIdentity,
    )

    identity = TrustedIdentity(
        str(required["EMUX_REMOTE_CONTROLLER_HUMAN_UID"]),
        str(required["EMUX_REMOTE_CONTROLLER_DEVICE_ID"]),
        str(required["EMUX_REMOTE_CONTROLLER_ID"]),
    )
    aliases = frozenset(
        value.strip()
        for value in str(required["EMUX_REMOTE_CONTROLLER_ALIASES"]).split(",")
        if value.strip()
    )
    return RemoteControllerAPI(
        RemoteConfig(
            str(required["EMUX_REMOTE_CONTROLLER_SERVER_ID"]),
            aliases,
            os.environ.get("EMUX_REMOTE_CONTROLLER_REVISION", "emux-0.67.2"),
            Path(str(required["EMUX_REMOTE_CONTROLLER_STATE"])),
        ),
        StaticTokenBoundary(token, identity),
        DenyConsequentialGate(),
        _server._load_registry,
        lambda session, lines: capture_payload(session, lines),
        lambda session, text, literal, enter: send_payload(
            session, text, literal=literal, enter=enter
        ),
        gate_probe=lambda session: _server._gate_snapshot(session, None),
    )


def run_web(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, open_browser: bool = False,
            public_origin: str | None = None, public_path: str | None = None,
            skin: str | None = None) -> int:
    """Start the emux web daemon. Blocks until Ctrl-C.

    `skin` is product chrome only (e.g. gmux) — see emux.skin. Same engine.
    """
    from . import skin as _skin

    active = _skin.set_active_skin(skin)  # None → $EMUX_SKIN → emux
    # launchd often ships PATH=/usr/bin:/bin — gist + resume need ~/.local/bin/claude
    _ensure_agent_path_env()
    if _server._resolve_tmux() is None:
        print("emux web: tmux not found on PATH — the UI will load but show nothing.", file=sys.stderr)
    claude_bin = _resolve_cli("claude")
    if claude_bin:
        print(f"emux web: claude → {claude_bin}", file=sys.stderr)
    else:
        print("emux web: claude CLI not found — gist/resume may degrade.", file=sys.stderr)
    if public_origin:
        parsed = urlparse(public_origin)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            print("emux web: --public-origin must be a bare http(s) origin.", file=sys.stderr)
            return 2
        public_origin = public_origin.rstrip("/")
    normalized_path = normalize_public_path(public_path)
    if normalized_path is None:
        print("emux web: --public-path must look like /gmux (leading slash, no trail).", file=sys.stderr)
        return 2
    EmuxWebHandler.extra_host = host if host not in _LOCALHOSTS else None
    EmuxWebHandler.public_origin = public_origin
    EmuxWebHandler.public_path = normalized_path
    EmuxWebHandler.remote_controller_api = _remote_controller_from_env()

    class EmuxThreadingHTTPServer(ThreadingHTTPServer):
        """Threading HTTP with a real listen backlog (EID-1100/1107).

        CPython's default request_queue_size is 5 — concurrent stress (16 clients)
        overflows the accept queue and surfaces as ConnectionReset / refused.
        """

        allow_reuse_address = True
        daemon_threads = True
        request_queue_size = int(os.environ.get("EMUX_HTTP_BACKLOG", "256"))

        def handle_error(self, request, client_address) -> None:  # noqa: ANN001
            import traceback

            err = traceback.format_exc()
            if "BrokenPipeError" in err or "ConnectionResetError" in err:
                return
            super().handle_error(request, client_address)

    try:
        server = EmuxThreadingHTTPServer((host, port), EmuxWebHandler)
    except OSError as e:
        if "address already in use" in str(e).lower():
            print(f"emux web: port {port} is already in use — try `emux web --port {port + 1}`.", file=sys.stderr)
            return 2
        raise

    # Single background capture loop feeds the cache that every browser tab
    # reads from. One sweep per tick regardless of how many tabs are open.
    stop = threading.Event()

    def poll_loop() -> None:
        # schedule tick every ~15s (not every pane poll) — cheap, message-only cron
        schedule_every = max(1, int(15 / max(_POLL_INTERVAL, 0.5)))
        n = 0
        while not stop.is_set():
            try:
                poll_once(14)
            except Exception:  # noqa: BLE001 — a transient tmux error must not kill the loop
                pass
            n += 1
            if n % schedule_every == 0:
                try:
                    from . import schedule as _sched

                    _sched.tick_once()
                except Exception:  # noqa: BLE001 — schedule must not kill the daemon
                    pass
            stop.wait(_POLL_INTERVAL)

    poller = threading.Thread(target=poll_loop, daemon=True)
    poller.start()

    url = f"http://{host}:{port}"
    print(f"emux web daemon → {url}  (Ctrl-C to stop)")
    print(f"  skin: {active.id} ({active.brand} · {active.tagline})")
    if normalized_path:
        print(f"  public path prefix: {normalized_path}  (proxy should strip before us)")
    if public_origin:
        print(f"  public origin: {public_origin}")
    if host not in _LOCALHOSTS:
        print("  WARNING: bound beyond localhost. The API blocks foreign Host/Origin", file=sys.stderr)
        print("  requests, but there is still NO authentication — anyone who can reach", file=sys.stderr)
        print("  this port and forge a matching Host header can type into your sessions.", file=sys.stderr)
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nemux web: stopped.")
    finally:
        stop.set()
        server.server_close()
    return 0
