"""emux MCP server.

Exposes MCP tools for attaching to and driving existing tmux sessions: list
live sessions, send keys, capture panes, run commands. Maintains a registry
of named sessions with metadata so an agent can refer to "claude-prod" or
"test-shell" without remembering tmux's underlying session ids.

Design principles:
- Primarily observes and drives EXISTING tmux sessions (send, capture, run). The
  user owns most session lifecycles; this MCP just watches and steers them.
- `tmux_spawn` is the one deliberate exception: it creates (and re-creates) a
  named, driveable session on demand — local or, with a `host`, on a remote
  machine over ssh — because "start something I can drive and a human can watch"
  is itself a first-class primitive. It kills a stale same-named session before
  creating, and nothing else.
- Remote is a single injection point: any operation carrying a `host` runs its
  tmux command over ssh, so one local emux can reach through to a remote box's
  Claude Code / shell and drive it identically. Sessions nest — a spawned
  session's command can ssh onward and spawn again.
- The registry is metadata only. Live state always comes from `tmux list-sessions`.
  If a registered session no longer exists, the registry entry is marked stale
  but not deleted — the user decides whether to re-register or unregister.
- All operations are best-effort capture. tmux output may include ANSI escapes;
  the caller is responsible for parsing if they need clean text.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import difflib
import fcntl
import functools
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import channels as channel_store
from . import linear as linear_store

mcp = FastMCP("emux")


REGISTRY_PATH = Path(
    os.environ.get("EMUX_REGISTRY")
    or os.environ.get("TMUX_MCP_REGISTRY")  # back-compat with prior name
    or (Path.home() / ".config" / "emux" / "registry.json")
)


_STATE_DIR = Path(os.environ.get("EMUX_STATE") or (Path.home() / ".local" / "state" / "emux"))
_LOG_DIR = _STATE_DIR / "logs"
_AUDIT_PATH = _STATE_DIR / "audit.jsonl"
_GATE_AUDIT_PATH = _STATE_DIR / "gate-approvals.jsonl"
_GATE_LOCK_PATH = _STATE_DIR / "gate-approvals.lock"
_GATE_FINGERPRINT_TTL = 60


def _audit(op: str, args: dict[str, Any], result: Any = None) -> None:
    """Append one line to the operation audit trail — every emux tool call, in
    order, with its salient args and outcome. Complements the session index
    (which jobs exist) and stream logs (what a job printed) with a per-CALL record
    (what emux was asked to do, when). Append-only; best-effort, never raises."""
    try:
        rec: dict[str, Any] = {"t": int(time.time()), "op": op}
        for k, v in args.items():
            if k == "self" or v is None:
                continue
            rec[k] = v if not isinstance(v, str) else v[:200]  # cap, never unbounded
        if isinstance(result, dict):
            rec["ok"] = result.get("ok")
            if result.get("ok") is False and result.get("error"):
                rec["error"] = result["error"]
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUDIT_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass  # an audit failure must never break a tool


def audited(fn):
    """Wrap an MCP tool so every call is recorded to the audit trail. Put it
    UNDER @mcp.tool() so FastMCP still introspects the real signature."""

    @functools.wraps(fn)
    async def wrap(*a, **k):
        result = await fn(*a, **k)
        _audit(fn.__name__, k, result)
        return result

    return wrap


def _log_path(name: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "session"
    return _LOG_DIR / f"{safe}.log"


def _start_stream_log(session: str, name: str | None = None) -> bool:
    """Stream every character emux watches on this pane to a durable append-only
    log (tmux pipe-pane). This is emux's memory: complete and replayable, and far
    faster to read than re-capturing the pane. One pipe per pane — re-arming
    replaces it; we append so history is never truncated. Best-effort, never raises.

    pipe-pane only captures forward from when it's armed (arms on register/drive),
    not retroactively — the point is durable memory from that moment on."""
    if _resolve_tmux() is None:
        return False
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    path = _log_path(name or session)
    code, _, _ = _run_tmux(["pipe-pane", "-t", session, f"cat >> {shlex.quote(str(path))}"])
    return code == 0


def _read_log(name: str, lines: int | None = None, strip: bool = True) -> str:
    """Read a session's durable log. strip=True removes ANSI for reading/grep;
    strip=False returns the raw byte stream for exact replay."""
    path = _log_path(name)
    if not path.exists():
        return ""
    # newline="" so terminal carriage-returns survive raw (a bare \r is a cursor
    # move, not a line break); universal-newline mode would mangle the stream.
    with path.open(encoding="utf-8", errors="ignore", newline="") as f:
        data = f.read()
    if strip:
        data = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", data)  # CSI
        data = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", data)  # OSC
        data = data.replace("\r", "")
    if lines:
        data = "\n".join(data.splitlines()[-lines:])
    return data


# ── up-channel signals ─────────────────────────────────────────────────────────
# A worker cannot call emux — emux only ever sees a pane's OUTPUT. So a worker
# talks UP to its manager by echoing a sentinel line, which lands in the stream
# log like any other output; emux extracts it. `@@EMUX@@ <KIND> <payload>`,
# KIND ∈ IDLE | READY | DONE | NEED | PROGRESS | ERROR.  IDLE/READY = "finished
# that task, HOLDING for the next" (a warm worker to keep + feed, not exit); DONE
# = "my whole purpose is finished, I may exit". e.g. echo "@@EMUX@@ IDLE".
_SIGNAL_RE = re.compile(r"@@EMUX@@[ \t]+(IDLE|READY|DONE|NEED|PROGRESS|ERROR)\b[ \t]*(.*)")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
_SIGNAL_OFFSETS = _STATE_DIR / "signal_offsets.json"
_SIGNAL_LEDGER = _STATE_DIR / "signals.jsonl"
# The ROBUST up-channel: a hook (Claude Code Stop/Notification) or the `emux
# signal` CLI writes a signal DIRECTLY here, per session — no echoing into the
# pane, no scraping a redraw-heavy TUI. Read alongside the output sentinel so a
# hook-injected signal and a scraped one are indistinguishable to a manager.
_INBOX_DIR = _STATE_DIR / "inbox"


_SIGNAL_SEEN = _STATE_DIR / "signal_seen.json"  # per-session id dedup: {name: [ids]}
_INBOX_RELPATH = ".local/state/emux/inbox"  # a REMOTE box's inbox, relative to its $HOME


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "session"


def _inbox_path(name: str) -> Path:
    return _INBOX_DIR / f"{_safe_name(name)}.jsonl"


def remote_inbox_relpath(name: str) -> str:
    """A session's inbox path on a REMOTE box, relative to that box's $HOME — the
    push target and the pull source for a remote child."""
    return f"{_INBOX_RELPATH}/{_safe_name(name)}.jsonl"


def inject_signal(
    session: str, kind: str, payload: str = "", sid: str | None = None
) -> dict[str, Any] | None:
    """Append a signal to a session's LOCAL inbox and return the written record
    (carrying its dedup `id`). Called by the `emux signal` CLI, which a worker's
    Stop/Notification hook runs. Best-effort; returns None on failure."""
    rec = {
        "id": sid or uuid.uuid4().hex[:12],
        "t": int(time.time()),
        "session": session,
        "kind": kind.upper(),
        "payload": payload,
    }
    try:
        p = _inbox_path(session)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps(rec) + "\n")
        return rec
    except Exception:
        return None


def _parse_inbox_text(name: str, text: str) -> list[dict[str, Any]]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        d.setdefault("id", hashlib.sha256(line.encode()).hexdigest()[:12])  # legacy id-less
        out.append(
            {
                "id": d["id"],
                "t": d.get("t"),
                "session": name,
                "kind": d.get("kind"),
                "payload": d.get("payload", ""),
            }
        )
    return out


# ── remote pull via a persistent tail follower (the efficiency/immediacy upgrade)
# For a remote session we don't `ssh cat` on every poll. Instead ONE persistent
# `ssh host tail -F` streams the remote inbox's new lines into a LOCAL mirror
# file, so reads stay local (correct peek + id-dedup) and a remote signal arrives
# the instant it is written, not on the next poll tick. Falls back to a one-shot
# `ssh cat` if a follower can't be started.
_tail_lock = threading.Lock()
_tail_procs: dict[str, subprocess.Popen] = {}


def _remote_mirror_path(name: str) -> Path:
    return _INBOX_DIR / f"{_safe_name(name)}.remote.jsonl"


def _ensure_mirror_tail(host: str, name: str) -> None:
    """Guarantee a live `ssh tail -F` follower mirroring this remote session's
    inbox into the local mirror file. Idempotent; restarts a dead follower and
    re-seeds the backlog (id-dedup absorbs the overlap)."""
    key = f"{host}::{name}"
    with _tail_lock:
        proc = _tail_procs.get(key)
        if proc and proc.poll() is None:
            return  # follower already live
        mirror = _remote_mirror_path(name)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        # cap the mirror so reconnect-replays don't grow it without bound (dedup
        # keeps it correct regardless; this just bounds disk).
        try:
            if mirror.is_file() and mirror.stat().st_size > 262_144:
                tail_lines = mirror.read_text(errors="ignore").splitlines()[-1000:]
                mirror.write_text("\n".join(tail_lines) + "\n")
        except Exception:
            pass
        rel = shlex.quote(remote_inbox_relpath(name))
        # `tail -F -n +1` streams the WHOLE file then follows — no startup gap for
        # a line written while the follower attaches (id-dedup absorbs the replay).
        # `stdbuf -oL` line-buffers so a single new signal flushes across ssh at
        # once instead of waiting for a full block buffer.
        remote = (
            f"touch {rel} 2>/dev/null; "
            f"if command -v stdbuf >/dev/null 2>&1; then exec stdbuf -oL tail -F -n +1 {rel}; "
            f"else exec tail -F -n +1 {rel}; fi"
        )
        try:
            fh = open(mirror, "a")  # noqa: SIM115 — owned by the child proc
            proc = subprocess.Popen(
                ["ssh", host, remote], stdout=fh, stderr=subprocess.DEVNULL, text=True
            )
            _tail_procs[key] = proc
        except Exception:
            pass


def _stop_tails() -> None:
    with _tail_lock:
        for proc in _tail_procs.values():
            try:
                proc.terminate()
            except Exception:
                pass
        _tail_procs.clear()


atexit.register(_stop_tails)


def _read_inbox(name: str, host: str | None) -> list[dict[str, Any]]:
    """Every signal in a session's inbox — the LOCAL file, or, for a remote
    session, the LOCAL MIRROR kept fresh by a persistent `ssh tail -F` follower
    (so reads stay local; no ssh per poll)."""
    if host:
        _ensure_mirror_tail(host, name)
        m = _remote_mirror_path(name)
        return _parse_inbox_text(name, m.read_text()) if m.is_file() else []
    p = _inbox_path(name)
    return _parse_inbox_text(name, p.read_text()) if p.is_file() else []


def _load_seen() -> dict[str, list[str]]:
    try:
        return json.loads(_SIGNAL_SEEN.read_text())
    except Exception:
        return {}


def _save_seen(d: dict[str, list[str]]) -> None:
    try:
        _SIGNAL_SEEN.parent.mkdir(parents=True, exist_ok=True)
        _SIGNAL_SEEN.write_text(json.dumps(d))
    except Exception:
        pass


def _new_inbox_signals(name: str, ack: bool, host: str | None = None) -> list[dict[str, Any]]:
    """NEW signals across BOTH delivery channels — the LOCAL inbox (where a child
    PUSHES) and, if the session is remote, the REMOTE inbox (which the parent
    PULLS over ssh) — deduped by signal `id`. Both channels may carry the same
    signal; id-dedup collapses them to one. `ack` records returned ids as seen so
    they are never returned twice (the dedup memory is capped)."""
    seen_all = _load_seen()
    seen_list = list(seen_all.get(name, []))
    seen = set(seen_list)
    fresh = []
    for d in _read_inbox(name, host=None) + (_read_inbox(name, host=host) if host else []):
        if d["id"] in seen:
            continue
        seen.add(d["id"])
        seen_list.append(d["id"])
        fresh.append(d)
    if ack and fresh:
        seen_all[name] = seen_list[-1000:]  # keep most-recent ids, bounded
        _save_seen(seen_all)
        for signal in fresh:
            _append_signal_ledger(signal)
    return fresh


def _append_signal_ledger(signal: dict[str, Any]) -> None:
    try:
        _SIGNAL_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        with _SIGNAL_LEDGER.open("a") as f:
            f.write(json.dumps(signal) + "\n")
    except Exception:
        pass


def _load_offsets() -> dict[str, int]:
    try:
        return json.loads(_SIGNAL_OFFSETS.read_text())
    except Exception:
        return {}


def _save_offsets(offs: dict[str, int]) -> None:
    try:
        _SIGNAL_OFFSETS.parent.mkdir(parents=True, exist_ok=True)
        _SIGNAL_OFFSETS.write_text(json.dumps(offs))
    except Exception:
        pass


def _new_signals(name: str, ack: bool) -> list[dict[str, Any]]:
    """Sentinel signals in a session's stream log SINCE the last read. Consumes
    only complete lines, so a signal straddling a read boundary is never lost.
    Advances (and persists) the byte offset when ack=True, and appends acked
    signals to the durable ledger."""
    # A REMOTE session's signals live in ITS box's inbox — pull them over ssh.
    host = _load_registry().get(name, {}).get("host")
    path = _log_path(name)
    if not path.is_file():
        # No local stream log (a remote or hook-only worker) — drain the inbox
        # (local push-destination + remote pull), deduped by id.
        return _new_inbox_signals(name, ack, host=host)
    offs = _load_offsets()
    start = offs.get(name, 0)
    size = path.stat().st_size
    if start > size:  # log truncated/rotated — restart
        start = 0
    with path.open("rb") as f:
        f.seek(start)
        raw = f.read()
    last_nl = raw.rfind(b"\n")
    consumed = raw[: last_nl + 1] if last_nl != -1 else b""
    text = _ANSI_RE.sub("", consumed.decode("utf-8", "ignore")).replace("\r", "")
    out = [
        {"t": int(time.time()), "session": name, "kind": m.group(1), "payload": m.group(2).strip()}
        for m in _SIGNAL_RE.finditer(text)
    ]
    if ack and consumed:
        offs[name] = start + len(consumed)
        _save_offsets(offs)
        for sig in out:
            _append_signal_ledger(sig)
    # Union the scraped sentinels (above) with the robust hook/CLI inbox (local
    # push-destination + remote pull). A manager can't tell — and shouldn't have
    # to — which channel or machine a signal came from.
    return out + _new_inbox_signals(name, ack, host=host)


def _resolve_watch_targets(targets: list[str] | None, under: str | None) -> list[str]:
    """Which sessions to watch: an explicit list, or a manager's `manages`
    subtree (`under`), or — by default — every registered session."""
    if targets:
        return list(targets)
    reg = _load_registry()
    if under:
        return list((reg.get(under) or {}).get("manages") or [])
    return list(reg.keys())


def _name_live(name: str) -> bool:
    e = _load_registry().get(name)
    return bool(e) and _session_exists(e["session"], e.get("host"))


def _log_size(name: str) -> int:
    p = _log_path(name)
    return p.stat().st_size if p.is_file() else 0


def _pane_text_for(name: str, lines: int = 50) -> str:
    """Live pane text for a watch target (registry name or raw session id),
    host-aware via the registry. '' on any failure — callers treat it as
    'nothing observable', never as proof of quiet."""
    entry = _load_registry().get(name)
    session = entry["session"] if entry else name
    host = entry.get("host") if entry else None
    try:
        code, out, _ = _run_tmux(
            ["capture-pane", "-t", session, "-p", "-S", f"-{lines}"], host=host
        )
    except FileNotFoundError:
        return ""
    return _strip_ansi(out or "") if code == 0 else ""


def _resolve_tmux() -> str | None:
    """Return path to the `tmux` binary, or None if not found.

    PATH first, then well-known install locations: a launchd daemon gets
    PATH=/usr/bin:/bin:/usr/sbin:/sbin, which made `which` fail and silently
    blinded the web daemon to every local session."""
    found = shutil.which("tmux")
    if found:
        return found
    for p in ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"):
        if os.access(p, os.X_OK):
            return p
    return None


def _run_tmux(args: list[str], timeout: int = 10, host: str | None = None) -> tuple[int, str, str]:
    """Run `tmux <args>` and return (returncode, stdout, stderr).

    When `host` is given (any ssh destination — `user@ip` or a `~/.ssh/config`
    alias, so per-host port/user/key live in ssh config, not here), the tmux
    command runs on that remote machine over ssh. This one injection point is
    what makes EVERY emux operation remote-capable: send, capture, spawn — all
    of them go remote for free just by carrying a host. The remote command is
    shell-quoted so keystrokes with spaces/metacharacters survive the ssh hop.

    Raises FileNotFoundError if tmux is not installed (local only).
    """
    if host:
        # A non-interactive ssh shell does NOT source the login profile, so
        # Homebrew's bin (where tmux lives on most Macs) is off PATH and a bare
        # `ssh host tmux ...` fails even though tmux is installed — verified
        # against a real box. Prepend the standard install locations so the
        # remote just works without the user configuring anything.
        # ponytail: covers homebrew (arm+intel) and /usr/local; if a host puts
        # tmux somewhere exotic, set it on that host's ssh-config or PATH.
        remote = "PATH=/opt/homebrew/bin:/usr/local/bin:$PATH tmux " + " ".join(
            shlex.quote(a) for a in args
        )
        cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=8",
            host,
            remote,
        ]
    else:
        tmux = _resolve_tmux()
        if tmux is None:
            raise FileNotFoundError("tmux not found on PATH")
        cmd = [tmux] + args
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_target(
    target: str, by_registry_name: bool
) -> tuple[str | None, str | None, str | None]:
    """Return (session, host, error). Resolves a registry name to its tmux
    session id AND its host (None = local), so a single call drives a session
    whether it lives on this machine or a remote one."""
    if not by_registry_name:
        return target, None, None
    registry = _load_registry()
    if target not in registry:
        return None, None, "not_registered"
    entry = registry[target]
    return entry["session"], entry.get("host"), None


def _session_exists(session: str, host: str | None = None) -> bool:
    """True iff a tmux session by this id is actually running — on the local
    machine or, with `host`, on the remote one. This is a REAL check (`tmux
    has-session`), not an assumption: hooking into an existing session must
    confirm it exists, or 'session_live' is a false green."""
    try:
        code, _out, _err = _run_tmux(["has-session", "-t", session], host=host)
    except FileNotFoundError:
        return False
    return code == 0


_SHELLS = {"zsh", "bash", "sh", "fish", "tcsh", "csh"}


def _classify(command: str) -> str:
    """What KIND of session this is, so you can find the resumable work: an
    agent (claude/codex/…), a bare shell, or something else (a build, a python
    process, an idle TUI)."""
    c = (command or "").lower()
    if "claude" in c:
        return "claude"
    if any(a in c for a in ("codex", "grok", "aider", "goose")):
        return "agent"
    if c in _SHELLS:
        return "shell"
    return "other"


def _ago(unix: int | None, now: int) -> str | None:
    if not unix:
        return None
    d = max(0, now - unix)
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    return f"{d // 86400}d ago"


def _live_sessions(host: str | None = None) -> list[dict[str, Any]]:
    """Return currently-running tmux sessions with RICH, rankable metadata — on
    the local machine, or on `host` over ssh. Each session carries its last
    activity, working directory, and current command, which is what lets you
    FIND the right existing session to hook into (most-recent, in this project,
    running claude), not just enumerate raw names."""
    try:
        code, out, _err = _run_tmux(
            [
                "list-sessions",
                "-F",
                "#{session_name}\t#{session_windows}\t#{session_created}\t"
                "#{session_attached}\t#{session_activity}\t#{pane_current_path}\t"
                "#{pane_current_command}",
            ],
            host=host,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []  # one unavailable host must not take down fleet discovery
    if code != 0:
        return []  # nonzero (incl. "no server running") → no sessions
    now = int(time.time())
    sessions = []
    for line in (out or "").strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        while len(parts) < 7:
            parts.append("")
        name, windows, created, attached, activity, cwd, command = parts[:7]
        act = int(activity) if activity.isdigit() else None
        sessions.append(
            {
                "name": name,
                "windows": int(windows) if windows.isdigit() else windows,
                "created_unix": int(created) if created.isdigit() else created,
                "activity_unix": act,
                "last_active_ago": _ago(act, now),
                "attached": attached != "0",
                "cwd": cwd or None,
                "command": command or None,
                "kind": _classify(command),
            }
        )
    return sessions


def _load_registry() -> dict[str, dict[str, Any]]:
    """Load the named-session registry from disk. Returns empty dict if missing."""
    if not REGISTRY_PATH.exists():
        return {}
    try:
        return json.loads(REGISTRY_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_registry(registry: dict[str, dict[str, Any]]) -> None:
    """Atomically write the registry to disk."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\n")
    tmp.replace(REGISTRY_PATH)


# ── durable session index: TRACK everything emux touches, so it stays findable
# after it ends. Control follows tracking — you can only search/resume a session
# emux once saw. This is what turns emux from a driver of LIVE sessions into a
# tracker of ALL of them (running or ended). Scope is deliberate: emux tracks
# TERMINAL sessions; searching Claude CONVERSATIONS by content is resume-resume's
# job — compose them, don't duplicate.
_INDEX_PATH = _STATE_DIR / "index.json"


def _load_index() -> dict[str, dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return {}
    try:
        return json.loads(_INDEX_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(index: dict[str, dict[str, Any]]) -> None:
    try:
        _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _INDEX_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, sort_keys=True) + "\n")
        tmp.replace(_INDEX_PATH)
    except OSError:
        pass  # tracking is best-effort; never break a discovery on a write failure


def _index_key(name: str, host: str | None) -> str:
    return f"{host or 'local'}::{name}"


def _track(sessions: list[dict[str, Any]], host: str | None) -> None:
    """Upsert every session emux just saw into the durable index (last_seen=now).
    Called on discovery, so any session emux ever looked at survives its own end
    and stays searchable."""
    if not sessions:
        return
    idx = _load_index()
    now = int(time.time())
    for s in sessions:
        key = _index_key(s["name"], host)
        prev = idx.get(key, {})
        idx[key] = {
            "name": s["name"],
            "host": host,
            "cwd": s.get("cwd") or prev.get("cwd"),
            "kind": s.get("kind") or prev.get("kind"),
            "created_unix": s.get("created_unix") or prev.get("created_unix"),
            "first_seen_unix": prev.get("first_seen_unix", now),
            "last_seen_unix": now,
        }
    _save_index(idx)


@mcp.tool()
@audited
async def tmux_sessions(
    host: str | None = None,
    sort_by: str = "activity",
    limit: int | None = None,
    match: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """Discover, RANK, and FILTER running tmux sessions — a queryable inventory
    of work in flight, local or remote, so you can find the right EXISTING
    session to hook into instead of enumerating raw names by hand.

    Each session carries last-activity, working directory, current command, and
    a `kind` (claude / agent / shell / other). Combine the filters to answer a
    real question in one call — e.g. "the 5 most recent claude sessions in the
    GREENMARK project on rentamac":

        tmux_sessions(host="rentamac", match="GREENMARK", kind="claude", limit=5)

    Then adopt one with `tmux_register` (same host) and drive it with
    `tmux_send`/`tmux_capture`. You did not have to spawn it; emux hooks into
    what is already there.

    Args:
        host: ssh destination to list a remote machine's sessions; omit for local.
        sort_by: "activity" (most recent first, default), "created", or "name".
        limit: keep only the first N after sorting.
        match: case-insensitive substring matched against name + cwd + command
            (e.g. a project path or repo name).
        kind: keep only sessions of this kind — "claude", "agent", "shell", "other".

    Returns:
        {ok, host, count, live: [...], registry: {...}}. Registry entries are
        marked `stale: true` only when checked against the machine they live on.
    """
    if host is None and _resolve_tmux() is None:
        return {
            "ok": False,
            "error": "tmux_not_installed",
            "hint": "Install tmux: `brew install tmux` (macOS) or `apt install tmux` (Debian).",
        }
    all_live = _live_sessions(host=host)  # one discovery call (one ssh hop)
    _track(all_live, host)  # remember them — findable after they end
    live_names = {s["name"] for s in all_live}
    live = list(all_live)

    if match:
        m = match.lower()
        live = [
            s
            for s in live
            if m in f"{s['name']} {s.get('cwd') or ''} {s.get('command') or ''}".lower()
        ]
    if kind:
        live = [s for s in live if s.get("kind") == kind]

    keys = {
        "activity": (lambda s: s.get("activity_unix") or 0, True),
        "created": (lambda s: s.get("created_unix") or 0, True),
        "name": (lambda s: s["name"], False),
    }
    keyfn, rev = keys.get(sort_by, keys["activity"])
    live.sort(key=keyfn, reverse=rev)
    if limit is not None and limit >= 0:
        live = live[:limit]

    registry = _load_registry()
    annotated = {}
    for name, entry in registry.items():
        entry_host = entry.get("host")
        stale: bool | None = (
            (entry.get("session") not in live_names) if entry_host == host else None
        )
        annotated[name] = {**entry, "stale": stale}
    return {"ok": True, "host": host, "count": len(live), "live": live, "registry": annotated}


@mcp.tool()
@audited
async def tmux_search(
    query: str | None = None,
    host: str | None = None,
    kind: str | None = None,
    status: str | None = None,
    limit: int = 20,
    refresh_host: bool = True,
) -> dict[str, Any]:
    """Search ANY session emux has ever tracked — running OR ended.

    `tmux_sessions` shows only what's live right now. This searches the durable
    index of everything emux has ever seen (spawned, hooked, or discovered), and
    marks each RUNNING or ENDED by checking the live set. So you can find work in
    flight and work that has already finished, and decide what to resume.

    Control follows tracking: emux can only find a session it once saw. A session
    on a machine emux never looked at is not here — run `tmux_sessions(host=...)`
    on that machine once and it becomes tracked forever after.

        tmux_search(query="greenmark", kind="claude")          # all, running+ended
        tmux_search(query="greenmark", status="ended")          # only finished ones
        tmux_search(host="rentamac", status="running", limit=5) # live on one box

    Resuming: a RUNNING match → adopt with tmux_register + drive. An ENDED match →
    its durable log (emux memory) is available to inspect; if it was a `claude`
    session, resume the conversation with resume-resume and spawn a fresh terminal
    for it via tmux_spawn. emux tracks the terminal; resume-resume tracks the chat.

    Args:
        query: case-insensitive substring over name + cwd (e.g. a project/repo).
        host: restrict to one machine (an ssh destination, or omit for all tracked).
        kind: "claude" | "agent" | "shell" | "other".
        status: "running" | "ended".
        limit: max results (most-recently-seen first).
        refresh_host: if a host is given, re-discover it first so its index +
            running/ended status are current (one ssh hop). Default True.

    Returns:
        {ok, count, results: [{name, host, status, kind, cwd, created_unix,
        last_seen_unix, last_seen_ago}]}, most-recently-seen first.
    """
    if refresh_host and host is not None:
        _track(_live_sessions(host=host), host)  # make this host's index current

    idx = _load_index()
    now = int(time.time())
    live_by_host: dict[str | None, set[str]] = {}

    def _running(name: str, h: str | None) -> bool:
        if h not in live_by_host:
            live_by_host[h] = {s["name"] for s in _live_sessions(host=h)}
        return name in live_by_host[h]

    results = []
    for e in idx.values():
        h = e.get("host")
        if host is not None and h != host:
            continue
        st = "running" if _running(e["name"], h) else "ended"
        if status and st != status:
            continue
        if kind and e.get("kind") != kind:
            continue
        if query and query.lower() not in f"{e['name']} {e.get('cwd') or ''}".lower():
            continue
        results.append(
            {
                **e,
                "status": st,
                "last_seen_ago": _ago(e.get("last_seen_unix"), now),
            }
        )

    results.sort(key=lambda r: r.get("last_seen_unix") or 0, reverse=True)
    return {"ok": True, "count": len(results), "results": results[: max(0, limit)]}


@mcp.tool()
@audited
async def tmux_spawn(
    name: str,
    command: str | None = None,
    host: str | None = None,
    gui: bool = False,
    cwd: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    manages: list[str] | None = None,
    channels: list[str] | None = None,
    linear_issue: str | None = None,
    linear_project: str | None = None,
    linear_team: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Spawn a fresh, driveable tmux session and register it — in one call.

    This is the "start something I can drive, and optionally watch" primitive:
    create → (optionally launch a command) → (optionally open a GUI window so a
    human can watch) → register under `name`. After this, drive it with
    `tmux_send`/`tmux_capture`/`tmux_run` using `target=name, by_registry_name=True`.

    REMOTE (the point): pass `host` (any ssh destination — `user@ip` or a
    `~/.ssh/config` alias). The session is created ON THAT MACHINE over ssh, and
    every later send/capture/run for this name transparently runs over ssh too.
    So: spawn a local session, or reach through to a remote box and spawn a
    session there, and drive its Claude Code / shell exactly the same way. Nest
    freely — a session's command can itself `ssh` onward and spawn again.

    NESTED MANAGER (context offloading): spawn a session whose `command` is a
    `claude` that will ITSELF spawn and drive a sub-session, and pass
    `manages=["<sub-name>"]` to declare the edge up front. The manager runs the
    tight capture/send loop against its sub, so that drive-churn lives in the
    MANAGER's context — the parent that spawned it only exchanges high-level
    goals/status with the manager. See the `nested-manager` skill for the recipe.

    Args:
        name: friendly registry name (also the tmux session id).
        command: optional command to launch in the session (e.g. `claude "..."`).
        host: ssh destination for a REMOTE session; omit for local.
        gui: if True, open a local GUI terminal attached to the session so a
             human can watch live (macOS/iTerm2; remote sessions attach via
             `ssh -t`). Best-effort — a failure here does not fail the spawn.
        cwd: working directory to start the session in.
        description, tags: registry metadata.
        manages: registry names this session drives (declared, not observed) —
            rendered as directed edges in `emux web` flow view. Use it to record
            a manager→sub tree at spawn time; the sub need not exist yet.

    Returns:
        {ok, name, host, session, gui_opened, launched, drive_hint}.
    """
    if linear_issue:
        try:
            linear_store.metadata(linear_issue, linear_project, linear_team, acceptance_criteria)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    elif linear_project or linear_team or acceptance_criteria:
        return {"ok": False, "error": "linear_issue is required with Linear metadata"}
    session = name
    # 1) create the session (kill any stale one of the same name first)
    _run_tmux(["kill-session", "-t", session], host=host)  # ignore result
    new_args = ["new-session", "-d", "-s", session]
    if cwd:
        new_args += ["-c", cwd]
    code, _out, err = _run_tmux(new_args, host=host, timeout=15)
    if code != 0:
        return {
            "ok": False,
            "error": "spawn_failed",
            "stderr": err,
            "host": host,
            "hint": "check ssh reachability + tmux on the host",
        }

    # 2) register FIRST so the stream log is armed (pipe-pane) BEFORE the command
    #    runs — otherwise a worker that signals early (or exits fast) emits into a
    #    pane no one is recording yet and its @@EMUX@@ up-channel line is lost.
    registration = await tmux_register(
        name=name,
        session=session,
        description=description,
        tags=tags,
        host=host,
        manages=manages,
        channels=channels,
        linear_issue=linear_issue,
        linear_project=linear_project,
        linear_team=linear_team,
        acceptance_criteria=acceptance_criteria,
    )
    if not registration.get("ok"):
        _run_tmux(["kill-session", "-t", session], host=host)
        return registration

    # 3) launch the command, if any — its output now flows into the armed log
    launched = False
    if command:
        c2, _o2, _e2 = _run_tmux(["send-keys", "-t", session, command, "Enter"], host=host)
        launched = c2 == 0

    # 4) optional GUI window so a human can watch (best-effort, macOS/iTerm2)
    gui_opened = False
    if gui:
        attach = (
            f"ssh -t {shlex.quote(host)} tmux attach -t {shlex.quote(session)}"
            if host
            else f"tmux attach -t {shlex.quote(session)}"
        )
        try:
            script = (
                'tell application "iTerm2"\n'
                " create window with default profile\n"
                " tell current session of current window to write text "
                f'"{attach}"\n'
                " activate\n"
                "end tell"
            )
            g = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=15
            )
            gui_opened = g.returncode == 0
        except Exception:
            gui_opened = False

    return {
        "ok": True,
        "name": name,
        "host": host,
        "session": session,
        "gui_opened": gui_opened,
        "launched": launched,
        "channels": registration.get("entry", {}).get("channels", []),
        "linear": registration.get("entry", {}).get("linear"),
        "channel_suggestions": registration.get("channel_suggestions", []),
        "drive_hint": f"tmux_send/tmux_capture with target='{name}', by_registry_name=True",
    }


@mcp.tool()
@audited
async def tmux_register(
    name: str,
    session: str,
    description: str | None = None,
    tags: list[str] | None = None,
    manages: list[str] | None = None,
    host: str | None = None,
    channels: list[str] | None = None,
    linear_issue: str | None = None,
    linear_project: str | None = None,
    linear_team: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Register a tmux session under a friendly name with metadata.

    Use this to remember "this is the session running my claude prod loop" or
    "this is the test shell" so future calls can refer to it by `name` rather
    than the raw tmux `session` identifier. The registry persists at
    ~/.config/emux/registry.json (override with $EMUX_REGISTRY).

    Args:
        name: The friendly name to register under (e.g., "claude-prod").
        session: The actual tmux session name as shown by `tmux list-sessions`.
        description: Optional human-readable note about what this session is for.
        tags: Optional list of tags for filtering.
        manages: Optional list of other registered names (or tmux session ids)
            that the agent in THIS session manages/drives. Rendered as directed
            edges in the `emux web` flow view.

    Returns:
        The registry entry that was saved, plus whether the underlying tmux
        session is currently live.
    """
    registry = _load_registry()
    entry = {
        "session": session,
        "description": description,
        "tags": tags or [],
        "manages": manages or [],
        "registered_at": int(time.time()),
    }
    if linear_issue:
        try:
            entry["linear"] = linear_store.metadata(
                linear_issue, linear_project, linear_team, acceptance_criteria
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
    elif linear_project or linear_team or acceptance_criteria:
        return {"ok": False, "error": "linear_issue is required with Linear metadata"}
    entry["channels"] = channel_store.resolve_channels(name, {**entry, "channels": channels or []})
    if host:
        entry["host"] = host  # remote session: all ops for this name run over ssh
    registry[name] = entry
    _save_registry(registry)
    # REAL liveness — hooking into an existing session must confirm it exists,
    # local or remote. Assuming a remote session is live because a host was named
    # is a false green (you could "adopt" a session that isn't there).
    session_live = _session_exists(session, host)
    if session_live and not host:
        _start_stream_log(session, name)  # local durable logging; remote streams over ssh on demand
    return {
        "ok": True,
        "name": name,
        "entry": entry,
        "session_live": session_live,
        "channel_suggestions": channel_store.suggest_channels(registry),
    }


@mcp.tool()
@audited
async def tmux_channels(session: str | None = None) -> dict[str, Any]:
    """List tiered channels, or return the compact learning context for one session.

    Agents should call this before rediscovering domain history. Channel tiers are
    0=canon, 1=domain, 2=workstream, 3=mission. Raw transcripts stay in existing
    session logs; channels index sessions and durable structured notes.
    """
    registry = _load_registry()
    definitions = channel_store.load_channels()
    if session:
        if session not in registry:
            return {"ok": False, "error": "not_registered", "session": session}
        names = channel_store.resolve_channels(session, registry[session], definitions)
        return {
            "ok": True,
            "session": session,
            "channels": names,
            "context": channel_store.session_context(session, registry),
        }
    return {
        "ok": True,
        "channels": [
            channel_store.channel_context(name, registry, definitions)
            for name in sorted(definitions, key=lambda n: (definitions[n].get("tier", 9), n))
        ],
        "suggestions": channel_store.suggest_channels(registry),
    }


@mcp.tool()
@audited
async def tmux_channel_note(
    channel: str,
    kind: str,
    text: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Record a durable learned decision/outcome/failure/policy/fact in a channel."""
    try:
        note = channel_store.append_note(channel, kind, text, source)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "channel": channel, "note": note}


def _linear_signal_history(names: set[str]) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    try:
        for line in _SIGNAL_LEDGER.read_text(errors="ignore").splitlines():
            try:
                signal = json.loads(line)
            except json.JSONDecodeError:
                continue
            if signal.get("session") in names:
                signals.append(signal)
    except OSError:
        pass
    for name in names:
        signals.extend(_new_signals(name, ack=False))
    deduped: dict[str, dict[str, Any]] = {}
    for signal in signals:
        key = str(
            signal.get("id")
            or (signal.get("session"), signal.get("t"), signal.get("kind"), signal.get("payload"))
        )
        deduped[key] = signal
    return list(deduped.values())


@mcp.tool()
@audited
async def tmux_linear_link(
    session: str,
    issue: str,
    project: str | None = None,
    team: str | None = None,
    acceptance_criteria: list[str] | None = None,
) -> dict[str, Any]:
    """Link one managed session to a Linear work contract; does not write Linear."""
    registry = _load_registry()
    try:
        contract = linear_store.link_session(
            registry, session, issue, project, team, acceptance_criteria
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    registry[session]["channels"] = channel_store.resolve_channels(session, registry[session])
    _save_registry(registry)
    return {
        "ok": True,
        "session": session,
        "linear": contract,
        "channels": registry[session]["channels"],
    }


@mcp.tool()
@audited
async def tmux_linear_evidence(
    session: str,
    criterion: int,
    proof: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Append manager-verified proof for one acceptance criterion."""
    try:
        evidence = linear_store.record_evidence(_load_registry(), session, criterion, proof, source)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "evidence": evidence}


@mcp.tool()
@audited
async def tmux_linear_status(
    session: str | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    """Reconcile Linear-linked workers; recommends review but never closes work."""
    registry = _load_registry()
    names = {name for name, entry in registry.items() if entry.get("linear", {}).get("issue")}
    if session:
        if session not in registry:
            return {"ok": False, "error": "not_registered", "session": session}
        names &= {session}
    if channel:
        definitions = channel_store.load_channels()
        if channel not in definitions:
            return {"ok": False, "error": f"unknown channel: {channel}"}
        names = {
            name
            for name in names
            if channel in channel_store.resolve_channels(name, registry[name], definitions)
        }
    signals = _linear_signal_history(names)
    rows = linear_store.reconcile(
        registry,
        signals,
        lambda _name, entry: _session_exists(entry["session"], entry.get("host")),
        names,
    )
    return {
        "ok": True,
        "count": len(rows),
        "work": rows,
        "policy": "Emux may recommend In Review; it never closes Linear issues.",
    }


@mcp.tool()
@audited
async def tmux_unregister(name: str) -> dict[str, Any]:
    """Remove a named session from the registry. Does NOT touch tmux itself."""
    registry = _load_registry()
    if name not in registry:
        return {"ok": False, "error": "not_registered", "name": name}
    removed = registry.pop(name)
    _save_registry(registry)
    return {"ok": True, "name": name, "removed_entry": removed}


_PANE_AGENT_CACHE: dict[str, tuple[float, str | None]] = {}
_PANE_AGENT_TTL = 30.0


def _pane_agent(session: str, host: str | None = None) -> str | None:
    """Which agent is running in this pane — asks the adapters.

    Cached briefly: a session's agent almost never changes, and this is on the
    hot path of every send, so it must not cost a tmux round-trip per keystroke.
    """
    key = f"{host or ''}/{session}"
    hit = _PANE_AGENT_CACHE.get(key)
    if hit and (time.time() - hit[0]) < _PANE_AGENT_TTL:
        return hit[1]
    from . import adapters

    code, out, _ = _run_tmux(
        ["display-message", "-p", "-t", session, "#{pane_current_command}"], host=host
    )
    if code != 0:
        return None
    a = adapters.detect(out.strip())
    val = a.key if a else None
    _PANE_AGENT_CACHE[key] = (time.time(), val)
    return val


def _pane_settle(session: str, host: str | None = None) -> float:
    """The paste-detection settle for whatever agent is in this pane. 0 if we
    haven't measured that agent — better than borrowing another agent's number."""
    from . import adapters

    return adapters.settle_for(_pane_agent(session, host))


def _gate_type(agent: str | None, screen: str, signature: str) -> str:
    """Classify a visible gate without returning its potentially sensitive text."""
    low = screen.lower()
    if "trust" in low and any(word in low for word in ("directory", "folder", "workspace")):
        return "trusted_workspace"
    if "mcp" in low and any(word in low for word in ("allow", "approve", "permission")):
        return "mcp_approval"
    if "bash" in low or "do you want to proceed" in low:
        return "command_approval"
    if "hooks need review" in low or "review hooks" in low:
        return "hook_review"
    if "update available" in low:
        return "software_update"
    if "press enter to confirm" in low:
        return "confirmation"
    return f"{agent or 'unknown'}_approval" if signature else "approval"


def _gate_snapshot(session: str, host: str | None) -> dict[str, Any]:
    """Capture and identify the current gate; pane content never leaves this helper."""
    from . import adapters

    if not _session_exists(session, host):
        return {"ok": False, "error": "session_gone"}
    code, screen, _err = _run_tmux(
        ["capture-pane", "-t", session, "-p", "-S", "-20"], host=host
    )
    if code != 0:
        return {"ok": False, "error": "tmux_capture_failed"}
    clean = _strip_ansi(screen or "")
    agent = _pane_agent(session, host)
    # Signature match first (instant); ML fallback only for suspicious-but-
    # unmatched screens, fail-closed (EID-871).
    kind, signature = adapters.detect_gate(agent, clean)
    if kind is None:
        return {"ok": False, "error": "no_active_gate"}
    # Whitespace normalization makes redraw-only changes stable. Target identity
    # is included so two panes displaying the same menu never share a challenge.
    normalized = "\n".join(" ".join(line.split()) for line in clean.splitlines()[-20:] if line.strip())
    identity = json.dumps(
        {"host": host or "local", "session": session, "agent": agent, "screen": normalized},
        sort_keys=True,
        separators=(",", ":"),
    )
    gate_type = "ml_detected" if kind == "ml" else _gate_type(agent, clean, signature or "")
    return {
        "ok": True,
        "fingerprint": hashlib.sha256(identity.encode()).hexdigest(),
        "gate_type": gate_type,
        "detected_by": kind,
        "agent": agent,
    }


@contextlib.contextmanager
def _gate_transaction_lock():
    """Serialize observe/approve records across independent CLI processes."""
    _GATE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_GATE_LOCK_PATH, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _gate_records() -> list[dict[str, Any]]:
    try:
        records = []
        for line in _GATE_AUDIT_PATH.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records
    except OSError:
        return []


def _write_gate_record(record: dict[str, Any]) -> None:
    """Durably append a redacted gate record. Approval fails closed if this fails."""
    _GATE_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_GATE_AUDIT_PATH, "a", encoding="utf-8") as audit:
        audit.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        audit.flush()
        os.fsync(audit.fileno())


def _gate_audit_record(
    *, request_id: str, subject: str, device: str, target: str,
    session: str | None, host: str | None, fingerprint: str,
    gate_type: str, action: str, outcome: str, error: str | None = None,
) -> dict[str, Any]:
    def label(value: str | None, fallback: str = "") -> str:
        # Audit fields are identifiers/enums only. Never allow arbitrary prompt
        # text to be smuggled into the durable record through MCP arguments.
        clean = re.sub(r"[^A-Za-z0-9_.:@/+-]", "_", value or "")[:160]
        return clean or fallback

    record: dict[str, Any] = {
        "t": int(time.time()), "op": "tmux_gate_approval",
        "request_id": label(request_id, "unknown"),
        "subject": label(subject), "device": label(device), "target": label(target),
        "resolved_session": label(session), "host": label(host),
        "gate_fingerprint": fingerprint if re.fullmatch(r"[0-9a-f]{64}", fingerprint) else "invalid",
        "gate_type": label(gate_type, "unknown"),
        "action": action if action in ("observe", "approve", "reject") else "unsupported",
        "outcome": label(outcome, "unknown"),
    }
    if error:
        record["error"] = error
    return record


@mcp.tool()
async def tmux_gate(target: str, by_registry_name: bool = False) -> dict[str, Any]:
    """Observe a live approval gate and issue a short-lived opaque fingerprint."""
    session, host, err = _resolve_target(target, by_registry_name)
    if err or session is None:
        return {"ok": False, "error": err or "not_registered", "target": target}
    snapshot = _gate_snapshot(session, host)
    if not snapshot.get("ok"):
        return {"ok": False, "error": snapshot["error"], "target": target,
                "resolved_session": session, "host": host}
    request_id = str(uuid.uuid4())
    record = _gate_audit_record(
        request_id=request_id, subject=os.environ.get("EMUX_SUBJECT") or os.environ.get("USER", ""),
        device=os.environ.get("EMUX_DEVICE") or os.uname().nodename, target=target,
        session=session, host=host, fingerprint=snapshot["fingerprint"],
        gate_type=snapshot["gate_type"], action="observe", outcome="observed",
    )
    try:
        with _gate_transaction_lock():
            _write_gate_record(record)
    except OSError:
        return {"ok": False, "error": "gate_audit_failed", "target": target}
    return {"ok": True, "target": target, "resolved_session": session, "host": host,
            "gate_fingerprint": snapshot["fingerprint"], "gate_type": snapshot["gate_type"],
            "allowed_actions": ["approve", "reject"], "expires_in": _GATE_FINGERPRINT_TTL,
            "request_id": request_id}


@mcp.tool()
async def tmux_approve_gate(
    target: str,
    gate_fingerprint: str,
    action: str = "approve",
    key: str | None = None,
    by_registry_name: bool = False,
    subject: str | None = None,
    device: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Resolve exactly the observed gate, once, with an allowlisted named key."""
    request_id = request_id or str(uuid.uuid4())
    subject = subject or os.environ.get("EMUX_SUBJECT") or os.environ.get("USER", "")
    device = device or os.environ.get("EMUX_DEVICE") or os.uname().nodename
    session, host, err = _resolve_target(target, by_registry_name)

    def denial_record(error: str, gate_type: str = "unknown") -> dict[str, Any]:
        return _gate_audit_record(
            request_id=request_id, subject=subject, device=device, target=target,
            session=session, host=host, fingerprint=gate_fingerprint,
            gate_type=gate_type, action=action, outcome="denied", error=error,
        )

    if err or session is None:
        try:
            with _gate_transaction_lock():
                _write_gate_record(denial_record(err or "not_registered"))
        except OSError:
            return {"ok": False, "error": "gate_audit_failed", "request_id": request_id}
        return {"ok": False, "error": err or "not_registered", "request_id": request_id}
    if not re.fullmatch(r"[0-9a-f]{64}", gate_fingerprint):
        try:
            with _gate_transaction_lock():
                _write_gate_record(denial_record("invalid_gate_fingerprint"))
        except OSError:
            return {"ok": False, "error": "gate_audit_failed", "request_id": request_id}
        return {"ok": False, "error": "invalid_gate_fingerprint", "request_id": request_id}
    expected = {"approve": "Enter", "reject": "Escape"}
    if action not in expected:
        try:
            with _gate_transaction_lock():
                _write_gate_record(denial_record("unsupported_gate_action"))
        except OSError:
            return {"ok": False, "error": "gate_audit_failed", "request_id": request_id}
        return {"ok": False, "error": "unsupported_gate_action", "request_id": request_id}
    send_key = key or expected[action]
    if send_key != expected[action]:
        try:
            with _gate_transaction_lock():
                _write_gate_record(denial_record("unsupported_gate_key"))
        except OSError:
            return {"ok": False, "error": "gate_audit_failed", "request_id": request_id}
        return {"ok": False, "error": "unsupported_gate_key", "request_id": request_id,
                "allowed_key": expected[action]}
    try:
        with _gate_transaction_lock():
            records = _gate_records()
            now = int(time.time())
            observations = [r for r in records
                            if r.get("outcome") == "observed"
                            and r.get("gate_fingerprint") == gate_fingerprint
                            and r.get("target") == target
                            and r.get("resolved_session") == session]
            if not observations or now - int(observations[-1].get("t", 0)) > _GATE_FINGERPRINT_TTL:
                _write_gate_record(denial_record("expired_or_unobserved_gate"))
                return {"ok": False, "error": "expired_or_unobserved_gate",
                        "request_id": request_id}
            if any(r.get("gate_fingerprint") == gate_fingerprint
                   and r.get("outcome") == "sent" for r in records):
                _write_gate_record(denial_record("gate_replay"))
                return {"ok": False, "error": "gate_replay", "request_id": request_id}
            if any(r.get("request_id") == request_id and r.get("action") != "observe"
                   for r in records):
                _write_gate_record(denial_record("request_replay"))
                return {"ok": False, "error": "request_replay", "request_id": request_id}
            snapshot = _gate_snapshot(session, host)  # mandatory TOCTOU recapture
            if not snapshot.get("ok"):
                error = snapshot["error"]
                _write_gate_record(_gate_audit_record(
                    request_id=request_id, subject=subject, device=device, target=target,
                    session=session, host=host, fingerprint=gate_fingerprint,
                    gate_type="unknown", action=action, outcome="denied", error=error))
                return {"ok": False, "error": error, "request_id": request_id}
            if snapshot["fingerprint"] != gate_fingerprint:
                _write_gate_record(_gate_audit_record(
                    request_id=request_id, subject=subject, device=device, target=target,
                    session=session, host=host, fingerprint=gate_fingerprint,
                    gate_type=snapshot["gate_type"], action=action, outcome="denied",
                    error="stale_gate"))
                return {"ok": False, "error": "stale_gate", "request_id": request_id}
            attempt = _gate_audit_record(
                request_id=request_id, subject=subject, device=device, target=target,
                session=session, host=host, fingerprint=gate_fingerprint,
                gate_type=snapshot["gate_type"], action=action, outcome="attempted")
            _write_gate_record(attempt)  # durable before the state-changing key
            sent = _run_tmux(["send-keys", "-t", session, send_key], host=host)
            outcome = "sent" if sent[0] == 0 else "failed"
            _write_gate_record(_gate_audit_record(
                request_id=request_id, subject=subject, device=device, target=target,
                session=session, host=host, fingerprint=gate_fingerprint,
                gate_type=snapshot["gate_type"], action=action, outcome=outcome,
                error=None if sent[0] == 0 else "tmux_send_failed"))
            if sent[0] != 0:
                return {"ok": False, "error": "tmux_send_failed", "request_id": request_id}
    except OSError:
        return {"ok": False, "error": "gate_audit_failed", "request_id": request_id}
    return {"ok": True, "target": target, "resolved_session": session, "host": host,
            "gate_fingerprint": gate_fingerprint, "gate_type": snapshot["gate_type"],
            "action": action, "sent_key": send_key, "request_id": request_id}


# The existing approve path only ever presses Enter — it accepts whatever option
# is highlighted and never checks WHICH option that is. For a human pressing
# approve that's fine (they read the screen). For a delegated auto-answer it is
# not: we must VERIFY the highlighted option is the safe one before pressing
# Enter, or a reordered/variant menu could land Enter on "No" (harmless) or,
# worse, a differently-built gate on something destructive (EID-874).
_HIGHLIGHT_LINE = re.compile(r"^\s*[❯›▶>]\s*\d+[.)]\s*(?P<label>.+?)\s*$")
_TRUST_AFFIRM = re.compile(r"trust|\byes\b|continue|proceed", re.IGNORECASE)
_TRUST_DENY = re.compile(r"\bno\b|exit|don'?t|cancel|reject|deny|skip", re.IGNORECASE)


def _highlighted_option(clean_screen: str) -> str | None:
    """Label of the option the selector cursor (❯/›/▶/>) sits on, if any."""
    for line in clean_screen.splitlines():
        m = _HIGHLIGHT_LINE.match(line)
        if m:
            return m.group("label").strip()
    return None


def _affirms_trust(label: str | None) -> bool:
    """True only if the highlighted label clearly AFFIRMS trust and carries no
    denial word — so a grant-driven Enter can never land on 'No'/'Skip'/'Exit'."""
    if not label:
        return False
    return bool(_TRUST_AFFIRM.search(label)) and not _TRUST_DENY.search(label)


@mcp.tool()
async def tmux_grant_answer(
    target: str,
    identity: str,
    by_registry_name: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Answer a session's permission gate ONLY when a scoped delegation grant
    pre-authorizes it — the authority-memory bridge (EID-874). Four independent
    conditions, deny-by-default; any miss leaves the gate frozen for a human:

      1. the gate type is in delegation.GRANTABLE_GATE_TYPES (hard allowlist);
      2. an exact, unexpired grant authorizes <identity> for
         {this server, this workspace, this gate type};
      3. the highlighted option affirms the safe answer (label-verified —
         never a blind Enter);
      4. tmux_approve_gate's own fingerprint/TOCTOU/replay/audit all pass.

    Reuses tmux_approve_gate for the actual keypress, so every guarantee there
    (single-use fingerprint, screen-unchanged recheck, receipt) is inherited."""
    from . import delegation

    if not (isinstance(identity, str) and identity):
        return {"ok": False, "error": "no_identity"}
    obs = await tmux_gate(target, by_registry_name=by_registry_name)
    if not obs.get("ok"):
        return obs
    gate_type = obs["gate_type"]
    session = obs["resolved_session"]
    host = obs["host"]

    # 1) hard allowlist — some gate types are never grantable, whatever a grant says
    if gate_type not in delegation.GRANTABLE_GATE_TYPES:
        return {"ok": False, "error": "gate_type_not_grantable", "gate_type": gate_type}

    # 2) scope — this server + this session's workspace must have an exact grant
    server_id = os.environ.get("EMUX_SERVER_ID") or os.uname().nodename
    registry = _load_registry()
    entry = (
        registry.get(target, {})
        if by_registry_name
        else next((e for e in registry.values() if e.get("session") == session), {})
    )
    workspace = str(entry.get("workspace") or Path(str(entry.get("cwd") or "")).name)
    if not workspace:
        return {"ok": False, "error": "no_workspace"}
    if not delegation.may_answer_gate(identity, server_id, workspace, gate_type):
        return {"ok": False, "error": "no_grant",
                "identity": identity, "server": server_id, "workspace": workspace,
                "gate_type": gate_type}

    # 3) label verify — confirm the highlighted option is the safe answer
    code, screen, _err = _run_tmux(
        ["capture-pane", "-t", session, "-p", "-S", "-20"], host=host
    )
    label = _highlighted_option(_strip_ansi(screen or "")) if code == 0 else None
    if not _affirms_trust(label):
        return {"ok": False, "error": "unsafe_highlighted_option", "gate_type": gate_type}

    # 4) delegate to the audited approve machinery, attributed to the identity
    result = await tmux_approve_gate(
        target, obs["gate_fingerprint"], action="approve",
        by_registry_name=by_registry_name, subject=identity, device="grant",
        request_id=request_id,
    )
    if result.get("ok"):
        result["answered_via"] = "grant"
        result["identity"] = identity
    return result


@mcp.tool()
@audited
async def tmux_send(
    target: str,
    keys: str | list[str],
    enter: bool = True,
    by_registry_name: bool = False,
    settle: float | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Send keystrokes to a tmux session.

    Use this to type a command into the session, send a control sequence, or
    inject any input. Does NOT capture the response — pair with `tmux_capture`
    or use `tmux_run` if you need send-then-read.

    Args:
        target: The tmux session to target. By default this is a tmux session
            name as shown by `tmux list-sessions`. If `by_registry_name=True`,
            it's looked up in the registry first.
        keys: The keystrokes to send. A string is ONE unit: literal text, or a
            single named key like "C-c", "Escape", "Enter". To send SEVERAL
            named keys pass a LIST — ["BSpace", "BSpace", "Enter"] — each
            element is its own key/literal ("BSpace BSpace" as one string is
            typed as literal text, not two backspaces).
        enter: If True (default), append "Enter" to submit the command.
        by_registry_name: If True, resolve `target` via the registry.
        settle: Seconds to wait AFTER typing the text and BEFORE pressing Enter,
            sending Enter as a separate keystroke. A paste-detecting TUI (Claude
            Code) reads a fast text+Enter as a multi-line PASTE and never submits
            it. DEFAULT (None) = ask the ADAPTER for the agent actually running in
            that pane — Claude gets its measured 0.4s, an agent we haven't measured
            gets 0. Pass a number to override; pass 0 for the classic single send.
        force: Deprecated compatibility argument. It never bypasses a detected
            gate. Use `tmux_gate` followed by `tmux_approve_gate` so approval is
            explicit, fingerprint-bound, single-use, and durably audited.

    Returns:
        {ok, target, resolved_session, sent, submitted?, resubmitted?} on
        success — `submitted: false` means the text is still sitting in the
        pane's composer even after a retry Enter (look before sending more).
        {ok: False, error: "blocked_on_gate", gate, hint} when a gate is up.
    """
    session, host, err = _resolve_target(target, by_registry_name)
    if err or session is None:
        return {"ok": False, "error": err or "not_registered", "name": target}
    if host is None and _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    agent = _pane_agent(session, host)
    if settle is None:
        # The paste-detection quirk belongs to the AGENT, not to the caller.
        from . import adapters

        settle = adapters.settle_for(agent)
    # A GATE on screen eats keystrokes and persists the answer — typing a prompt
    # into a gated pane is a config write, not a no-op. Even the deprecated
    # `force` argument cannot bypass this; use the fingerprint-bound transaction.
    from . import adapters

    code, screen, _ = _run_tmux(["capture-pane", "-t", session, "-p", "-S", "-12"], host=host)
    gate = adapters.gated(agent, screen if code == 0 else "")
    if gate:
        return {
            "ok": False,
            "error": "blocked_on_gate",
            "gate": gate,
            "agent": agent,
            "session": session,
            "hint": "This pane is showing a modal gate. Observe it with tmux_gate, "
            "then resolve that exact fingerprint once with tmux_approve_gate.",
        }
    key_args = keys if isinstance(keys, list) else [keys]
    if enter and settle and settle > 0:
        # Type the text, let the TUI leave paste-mode, THEN submit with a
        # separate Enter — otherwise a paste-detecting TUI swallows the newline.
        c1 = _run_tmux(["send-keys", "-t", session, *key_args], host=host)
        if c1[0] != 0:
            return {
                "ok": False,
                "error": "tmux_send_failed",
                "stderr": c1[2],
                "session": session,
                "host": host,
            }
        time.sleep(settle)
        result = _run_tmux(["send-keys", "-t", session, "Enter"], host=host)
    else:
        args = ["send-keys", "-t", session, *key_args]
        if enter:
            args.append("Enter")
        result = _run_tmux(args, host=host)
    if result[0] != 0:
        return {
            "ok": False,
            "error": "tmux_send_failed",
            "stderr": result[2],
            "session": session,
            "host": host,
        }
    out: dict[str, Any] = {
        "ok": True,
        "target": target,
        "resolved_session": session,
        "host": host,
        "sent": keys,
        "enter": enter,
    }
    # Verify submission: a paste-guarded composer sometimes eats the Enter and
    # the text just SITS there (observed live — the send reports ok, nothing
    # runs). When we submitted literal text into a known AI's composer, look
    # once; if the composer box still holds the tail of what we typed, press
    # Enter again and re-check. Skipped for shells/unknown panes (no composer).
    literal = (
        enter
        and agent is not None
        and len(key_args) == 1
        and " " in key_args[0]
        and len(key_args[0]) > 3
    )
    if literal:
        needle = key_args[0][-24:]

        def _composing() -> bool | None:
            code, screen, _ = _run_tmux(
                ["capture-pane", "-t", session, "-p", "-S", "-12"], host=host
            )
            if code != 0:
                return None
            box = _composer_text(_strip_ansi(screen or ""))
            return None if box is None else needle in box

        time.sleep(max(0.6, settle or 0.0))
        c = _composing()
        if c:
            _run_tmux(["send-keys", "-t", session, "Enter"], host=host)
            out["resubmitted"] = True
            time.sleep(0.6)
            out["submitted"] = _composing() is False
        elif c is False:
            out["submitted"] = True
        # c is None → composer box not parseable; report nothing rather than guess
    return out


def _composer_text(screen: str) -> str | None:
    """The text inside a TUI's composer box — the region between the LAST two
    full-width horizontal rules (Claude Code drops the composer there; the
    transcript echo of a submitted message lives ABOVE it). None if the screen
    doesn't have two rules to parse."""
    lines = screen.splitlines()
    rules = [i for i, ln in enumerate(lines) if len(ln.strip()) >= 8 and set(ln.strip()) == {"─"}]
    if len(rules) < 2:
        return None
    return "\n".join(lines[rules[-2] + 1 : rules[-1]])


@mcp.tool()
@audited
async def tmux_capture(
    target: str,
    lines: int = 200,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Capture the visible content of a tmux session's active pane.

    Use this to read what's currently on screen — both the live state and the
    last N lines of scrollback. Output may contain ANSI escape sequences; the
    caller is responsible for stripping them if they need clean text.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        lines: How many lines of scrollback to include (default 200). Pass a
            larger number to see more history; tmux scrollback retention
            depends on the session's configured `history-limit`.
        by_registry_name: If True, resolve `target` via the registry.

    Returns:
        {ok, target, resolved_session, content, lines_captured}
    """
    session, host, rerr = _resolve_target(target, by_registry_name)
    if rerr or session is None:
        return {"ok": False, "error": rerr or "not_registered", "name": target}
    if host is None and _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    code, out, err = _run_tmux(
        [
            "capture-pane",
            "-t",
            session,
            "-p",
            "-S",
            f"-{lines}",
        ],
        host=host,
    )
    if code != 0:
        return {
            "ok": False,
            "error": "tmux_capture_failed",
            "stderr": err,
            "session": session,
            "host": host,
        }
    return {
        "ok": True,
        "target": target,
        "resolved_session": session,
        "host": host,
        "content": out,
        "lines_captured": len((out or "").splitlines()),
    }


@mcp.tool()
@audited
async def tmux_classify(
    target: str,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Classify what a session is CURRENTLY doing — a deterministic, no-LLM read.

    Turns the session's stream log, pending up-channel signals, and live pane
    command into a single labelled state with a confidence, a one-line summary,
    the evidence behind it, any warning flags, and a recommended action. This is
    the Tier-0 signal layer (`docs/emux-smart-classifiers.md`): rules + counters
    only, no model calls — so it is cheap enough to poll and safe to trust as
    "facts about the screen".

    States: running, planning, editing, waiting_external, waiting_human,
    thrashing, stuck, error, done_idle, dead.
    Flags: token_waste, possible_exhaustion, hidden_wait, false_busy,
    dangerous_blocked.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        by_registry_name: If True, resolve `target` via the registry (this also
            lets the classifier read that session's durable log and signals).

    Returns:
        {ok, target, state, confidence, summary, evidence, flags,
         recommended_action}
    """
    # Imported lazily to avoid an import cycle (judge imports from this module).
    from . import judge

    name = target
    host = None
    if by_registry_name:
        entry = _load_registry().get(target)
        if entry is None:
            return {"ok": False, "error": "not_registered", "name": target}
        host = entry.get("host")
    try:
        result = judge.classify_session(name, host=host)
    except Exception as e:  # never let a classifier bug take down the caller
        return {"ok": False, "error": "classify_failed", "detail": str(e), "target": target}
    return {"ok": True, "target": target, **result}


@mcp.tool()
@audited
async def tmux_run(
    target: str,
    command: str,
    wait_seconds: float = 2.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Send a command, wait, then capture — the convenience send-then-read.

    Use this for the common "run a command and observe the result" pattern.
    The wait is a simple sleep; for long-running commands, send+capture
    separately and poll capture until you see the prompt return.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        command: The command to type into the session (Enter is auto-appended).
        wait_seconds: How long to sleep before capturing. 2.0s catches most
            interactive responses; bump higher for slow commands. For commands
            taking >10s, prefer separate `tmux_send` + polling `tmux_capture`.
        capture_lines: How many scrollback lines to return after the wait.
        by_registry_name: If True, resolve `target` via the registry.

    Returns:
        {ok, target, command, wait_seconds, content, lines_captured}
    """
    send_result = await tmux_send(
        target=target, keys=command, enter=True, by_registry_name=by_registry_name
    )
    if not send_result.get("ok"):
        return {"ok": False, "stage": "send", "send_result": send_result}
    await asyncio.sleep(wait_seconds)
    capture_result = await tmux_capture(
        target=target, lines=capture_lines, by_registry_name=by_registry_name
    )
    if not capture_result.get("ok"):
        return {
            "ok": False,
            "stage": "capture",
            "send_result": send_result,
            "capture_result": capture_result,
        }
    return {
        "ok": True,
        "target": target,
        "resolved_session": send_result.get("resolved_session"),
        "command": command,
        "wait_seconds": wait_seconds,
        "content": capture_result["content"],
        "lines_captured": capture_result["lines_captured"],
    }


# ---- converse: talk to another AI through its TUI --------------------------
#
# tmux_run waits a FIXED number of seconds — useless for a streaming AI whose
# reply takes an unknown amount of time. converse() instead polls the pane and
# waits until it stops changing (quiesces), so it works for railway.new's agent,
# a `claude`/`codex`/`aider` REPL, or any other terminal AI. It returns the
# reply (the new text since the prompt was sent) plus the full settled screen.

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _capture_text(session: str, lines: int) -> str:
    code, out, err = _run_tmux(["capture-pane", "-t", session, "-p", "-S", f"-{lines}"])
    if code != 0:
        raise RuntimeError(err or "capture-pane failed")
    return out or ""


def _reply_delta(before: str, after: str) -> str:
    """Best-effort: the non-blank lines present in `after` but not `before`."""
    sm = difflib.SequenceMatcher(a=before.splitlines(), b=after.splitlines())
    added: list[str] = []
    after_lines = after.splitlines()
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(after_lines[j1:j2])
    return "\n".join(line for line in added if line.strip())


# Generic "the TUI AI is actively generating" signals. Used to avoid sending
# Escape (which would interrupt generation) when clearing a stale composer draft.
# Caller-supplied busy_markers extend these; these patterns catch a bare spinner
# with no marker — a live elapsed timer, or Claude Code's "esc to interrupt" hint.
_GENERATING_PATTERNS = (
    re.compile(r"esc to interrupt", re.I),
    re.compile(r"\b\d+s\s*·"),  # live elapsed timer, e.g. "12s ·" / "(9s · thinking)"
    re.compile(r"·\s*\d+s\b"),  # "· 12s"
)


def _looks_generating(screen: str, busy_markers: list[str] | None = None) -> bool:
    """True if the pane looks like an AI mid-generation — must NOT be Escaped."""
    if any(m.lower() in screen.lower() for m in (busy_markers or [])):
        return True
    return any(p.search(screen) for p in _GENERATING_PATTERNS)


def converse(
    target: str,
    prompt: str,
    submit: bool = True,
    settle_seconds: float = 2.5,
    poll_interval: float = 1.0,
    max_seconds: float = 90.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
    strip_ansi: bool = True,
    busy_markers: list[str] | None = None,
    clear_first: bool = True,
) -> dict[str, Any]:
    """Send `prompt` to a session running a TUI AI, wait for it to stop
    responding, and return the reply. Synchronous core shared by the MCP tool
    and `emux ask`. Operates on an EXISTING session (emux never spawns).

    busy_markers: substrings that mean the AI is still working (e.g. "thinking").
    While any appears on screen, the pane is never considered settled — this
    prevents a static "thinking…" indicator from being mistaken for the reply."""
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]

    _start_stream_log(session, target)  # ensure this driven pane is being logged
    try:
        before = _capture_text(session, capture_lines)
    except (RuntimeError, FileNotFoundError) as e:
        return {"ok": False, "error": "capture_failed", "stderr": str(e), "session": session}

    # Clear a stale/restored composer draft before typing a fresh prompt — but ONLY
    # when the pane is idle. Escape mid-generation would interrupt the AI's thinking
    # (verified: the wrong-time Escape kills it). A resumed session (e.g. `claude
    # --resume`) restores an unsent draft that otherwise swallows the submit Enter,
    # so without this the prompt never lands.
    if clear_first and not _looks_generating(before, busy_markers):
        _run_tmux(["send-keys", "-t", session, "Escape"])
        time.sleep(0.15)
    # Type the prompt literally (-l so a sentence is never parsed as key names),
    # then Enter as a real key.
    if _run_tmux(["send-keys", "-t", session, "-l", prompt])[0] != 0:
        return {"ok": False, "error": "send_failed", "session": session}
    if submit:
        # Same paste-detection quirk tmux_send handles: a paste-detecting TUI
        # (Claude Code) reads a fast text+Enter as a multi-line PASTE and never
        # submits it. Wait the pane agent's measured settle AFTER typing and
        # BEFORE Enter; an agent we haven't measured (or a shell) gets 0.
        from . import adapters

        settle = adapters.settle_for(_pane_agent(session))
        if settle > 0:
            time.sleep(settle)
        _run_tmux(["send-keys", "-t", session, "Enter"])

    # Wait until the pane is unchanged for `settle_seconds` (or time out). If the
    # session dies mid-wait, stop and say so rather than reporting a false settle.
    last: str | None = None
    stable_for = 0.0
    elapsed = 0.0
    died = False
    while elapsed < max_seconds:
        time.sleep(poll_interval)
        elapsed += poll_interval
        if not _session_alive(session):
            died = True
            break
        try:
            cur = _capture_text(session, capture_lines)
        except (RuntimeError, FileNotFoundError):
            died = not _session_alive(session)
            break
        busy = bool(busy_markers) and any(m in cur for m in busy_markers)
        if cur == last and not busy:
            stable_for += poll_interval
            if stable_for >= settle_seconds:
                break
        else:
            stable_for = 0.0
            last = cur

    if died:
        return {
            "ok": False,
            "error": "session_gone",
            "target": target,
            "resolved_session": session,
            "prompt": prompt,
            "detail": "tmux session vanished mid-reply",
        }

    after = last if last is not None else before
    reply = _reply_delta(before, after)
    if strip_ansi:
        reply = _strip_ansi(reply)
        after = _strip_ansi(after)
    return {
        "ok": True,
        "target": target,
        "resolved_session": session,
        "prompt": prompt,
        "settled": elapsed < max_seconds,
        "waited_seconds": round(elapsed, 1),
        "reply": reply.strip(),
        "screen": after,
    }


@mcp.tool()
@audited
async def tmux_ask(
    target: str,
    prompt: str,
    submit: bool = True,
    settle_seconds: float = 2.5,
    poll_interval: float = 1.0,
    max_seconds: float = 90.0,
    capture_lines: int = 200,
    by_registry_name: bool = False,
    busy_markers: list[str] | None = None,
) -> dict[str, Any]:
    """Talk to another AI running in a tmux session and get its reply.

    The strategy for driving TUI-based AIs (railway.new's agent, a `claude` /
    `codex` / `aider` REPL, etc.): type a prompt, then wait until the pane STOPS
    changing before reading — because an AI's reply streams in over an unknown
    duration, a fixed sleep (like `tmux_run`) either cuts it off or wastes time.

    The session must already be running the AI and be at its input prompt. emux
    never spawns sessions — create it first (`tmux new-session -d -s x '<ai cmd>'`)
    and navigate it to the prompt, then call this.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        prompt: The message to send (typed literally, then Enter unless submit=False).
        submit: Append Enter to send the prompt (default True).
        settle_seconds: Consider the reply done once the pane is unchanged this
            long (default 2.5). Raise for choppy streamers.
        poll_interval: Seconds between screen captures (default 1.0).
        max_seconds: Hard cap on total wait (default 90).
        capture_lines: Scrollback lines to read each poll (default 200).
        by_registry_name: Resolve `target` via the registry.
        busy_markers: Substrings meaning "still working" (e.g. ["thinking"]).
            While one is on screen the pane is never treated as settled — stops
            a static busy indicator from being read back as the reply.

    Returns:
        {ok, reply, screen, settled, waited_seconds, resolved_session}. `reply`
        is the best-effort new text since the prompt; `screen` is the full
        settled pane (ANSI stripped) if you need more context. `settled=False`
        means it hit `max_seconds` — the AI may still be responding.
    """
    effective_prompt = prompt
    injected = False
    if by_registry_name:
        prelude = channel_store.agent_prelude_once(target, _load_registry())
        if prelude:
            effective_prompt = f"{prelude}\n\nCURRENT REQUEST:\n{prompt}"
            injected = True
    result = await asyncio.to_thread(
        converse,
        target,
        effective_prompt,
        submit,
        settle_seconds,
        poll_interval,
        max_seconds,
        capture_lines,
        by_registry_name,
        True,  # strip_ansi
        busy_markers,
    )
    result["channel_context_injected"] = injected
    if injected:
        result["prompt"] = prompt
    return result


# ---- navigate: model-driven TUI navigation --------------------------------
#
# converse() assumes you're already at the AI's input prompt. Getting THERE
# through an arbitrary menu (railway.new's Chat→workspace→project walk, a
# settings screen, an installer) is the hard part. navigate() drives it with a
# model: capture the screen, ask the model which keys to press to move toward a
# stated goal, send them, repeat until the model says done (or `until` appears).
#
# The model call goes through the `claude -p` CLI (fixed-cost subscription tool)
# — NEVER the Anthropic API. A fast model is plenty for "which key next".

# Fast model for the per-keystroke decision; escalate to a stronger one only
# when a step stalls (model error or no usable action). Intelligence when it
# matters, cheap the rest of the time.
_NAV_MODEL_DEFAULT = os.environ.get("EMUX_NAV_MODEL", "claude-haiku-4-5-20251001")
_NAV_MODEL_ESCALATE = os.environ.get("EMUX_NAV_MODEL_ESCALATE", "claude-sonnet-5")

# tmux key names the model is allowed to emit (guards against it inventing keys).
_NAV_ALLOWED_KEYS = {
    "Up",
    "Down",
    "Left",
    "Right",
    "Enter",
    "Escape",
    "Tab",
    "BTab",
    "Space",
    "BSpace",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "C-c",
    "C-d",
    "C-u",
    "C-k",
    "C-a",
    "C-e",
}

# ---- destructive-action gate -----------------------------------------------
#
# navigate/goal send whatever keystrokes the model picks — including confirming
# a "Delete? [y]" prompt. This gate blocks two cases (unless allow_dangerous):
#   1. the model types a destructive command (rm -rf, DROP TABLE, force-push…);
#   2. a destructive confirmation is on screen AND the action would confirm it.
# ponytail: heuristic denylist, not a sandbox — the upgrade path is an
# interactive confirm callback. It errs toward blocking; opt out with --yolo.

# ponytail: a denylist for arbitrary typed text is best-effort, never complete —
# the real backstop is that autonomous mode is opt-in (allow_dangerous) and
# reviewable. But it was missing whole destructive-verb FAMILIES: it caught
# `rm -rf` and missed `kubectl delete namespace production`, `terraform destroy`,
# `DELETE FROM users`, `git branch -D main` — all of which it then typed and
# submitted into a live pane. Those families are enumerated here now; treat any
# addition to this list as closing a hole someone already found the hard way.
_DANGER_TEXT = re.compile(
    r"(?i)(\brm\s+-[rf]{1,2}\b|\bdrop\s+(table|database)\b|\btruncate\s+table\b|"
    r"git\s+push\b.*--force|git\s+reset\s+--hard|\bmkfs\b|\bdd\s+if=|>\s*/dev/sd|"
    r"\bshutdown\b|\breboot\b|\bformat\s+[a-z]:|"
    r"kubectl\s+delete|terraform\s+(destroy|apply)|\bDELETE\s+FROM\b|"
    r"git\s+branch\s+-D\b|git\s+clean\s+-[a-z]*f|helm\s+(delete|uninstall)|"
    r"docker\s+(rm\b|system\s+prune))"
)
_DANGER_SCREEN = re.compile(
    r"(?i)(permanently\s+delete|cannot\s+be\s+undone|are\s+you\s+sure|"
    r"this\s+will\s+delete|\birreversible\b|force[- ]?push|\boverwrite\b|"
    r"\bdestroy\b|delete\s+all|drop\s+the\b|\bpermanent(ly)?\b)"
)
_CONFIRM_KEYS = {"Enter", "Space", "y", "Y"}


def _danger_reason(screen: str, text: str, keys: list[str], submit: bool) -> str | None:
    """Why an action is destructive, or None if safe."""
    if text and _DANGER_TEXT.search(text):
        return f"types a destructive command: {text!r}"
    confirming = (
        submit
        or any(k in _CONFIRM_KEYS for k in keys)
        or (text.strip().lower() in {"y", "yes"} if text else False)
    )
    if confirming and (m := _DANGER_SCREEN.search(screen or "")):
        return f"would confirm a destructive prompt (screen shows {m.group(0)!r})"
    return None


def _claude_decide(model: str, goal: str, screen: str, history: list[str]) -> dict[str, Any]:
    """Ask `claude -p` for the next navigation step. Returns the parsed JSON
    decision, or an {_error} dict. Fixed-cost CLI — not the API."""
    claude = shutil.which("claude")
    if claude is None:
        return {"_error": "claude CLI not on PATH (needed for model-driven navigate)"}
    hist = "\n".join(f"  - {h}" for h in history[-8:]) or "  (none yet)"
    prompt = (
        "You are navigating a terminal UI by choosing keystrokes. "
        "Given the GOAL and the current SCREEN, decide the next step.\n\n"
        f"GOAL: {goal}\n\n"
        f"ACTIONS ALREADY TAKEN:\n{hist}\n\n"
        f"CURRENT SCREEN:\n<<<\n{screen}\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else:\n"
        '{"thought": "<brief>", "done": <bool>, '
        '"text": "<literal text to type, or empty>", '
        '"keys": [<tmux key names to send after the text>]}\n'
        f"Allowed key names: {sorted(_NAV_ALLOWED_KEYS)}. "
        'Use "text" to type into a filter/input box; use "keys" for navigation '
        '(e.g. ["Down","Enter"]). Set done=true ONLY when the GOAL is already '
        "satisfied by the current screen — then keys/text are ignored. "
        "Prefer the fewest keys. Never guess a key not in the allowed list."
    )
    try:
        proc = subprocess.run(
            [claude, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"_error": f"claude -p failed: {e}"}
    out = (proc.stdout or "").strip()
    m = re.search(r"\{.*\}", out, re.DOTALL)  # tolerate stray prose around the JSON
    if not m:
        return {"_error": "no JSON in model reply", "raw": out[:400]}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"_error": "unparseable JSON from model", "raw": out[:400]}


def _decide_step(models: list[str], goal: str, screen: str, history: list[str]) -> dict[str, Any]:
    """Get a usable navigation step, escalating through `models` on a stall.

    A "stall" is a model/parse error OR a valid reply with no action (no text and
    no allowed keys) and not done. Returns the first usable decision with the
    validated `keys`/`text` attached and `model` used, or the last failure."""
    last: dict[str, Any] = {}
    for model in models:
        decision = _claude_decide(model, goal, screen, history)
        if "_error" in decision:
            last = {"stall": decision["_error"], "raw": decision.get("raw"), "model": model}
            continue
        if decision.get("done"):
            return {"done": True, "thought": decision.get("thought"), "model": model}
        text = (decision.get("text") or "").strip()
        keys = [k for k in (decision.get("keys") or []) if k in _NAV_ALLOWED_KEYS]
        if text or keys:
            return {
                "done": False,
                "text": text,
                "keys": keys,
                "thought": decision.get("thought"),
                "model": model,
            }
        last = {"stall": "no_action", "thought": decision.get("thought"), "model": model}
    return {"stall": last.get("stall", "unknown"), **last}


def navigate(
    target: str,
    goal: str,
    until: str | None = None,
    max_steps: int = 12,
    step_pause: float = 1.5,
    capture_lines: int = 200,
    model: str | None = None,
    by_registry_name: bool = False,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """Drive a tmux session's TUI toward `goal` using a model to pick keystrokes.

    Loops: capture screen → `claude -p` decides the next keys → send them →
    repeat, until the model reports done, `until` appears on screen, or
    `max_steps` is hit. emux never spawns — the session must already exist.
    """
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    # Escalation chain: fast model, then the stronger one on a stall. A caller-
    # pinned `model` overrides both (no escalation — they asked for that model).
    chain = [model] if model else [_NAV_MODEL_DEFAULT, _NAV_MODEL_ESCALATE]

    history: list[str] = []
    for step in range(max_steps):
        # Observe with recovery from a dead session or a transient blank frame.
        screen = _observe(session, capture_lines)
        if screen is None:
            return {
                "ok": False,
                "error": "session_gone",
                "detail": "tmux session vanished (ssh dropped or killed)",
                "steps": history,
            }
        if not screen.strip():
            return {
                "ok": False,
                "error": "blank_screen",
                "detail": "session alive but rendered nothing",
                "steps": history,
            }
        # Hard stop: caller-supplied target string already visible.
        if until and until in screen:
            return {"ok": True, "reached": "until", "steps": history, "screen": screen}

        decision = _decide_step(chain, goal, screen, history)
        if "stall" in decision:
            # Transient stall: re-observe once and retry before giving up.
            time.sleep(1.0)
            rescreen = _observe(session, capture_lines)
            if rescreen is None:
                return {"ok": False, "error": "session_gone", "steps": history}
            if rescreen.strip():
                decision = _decide_step(chain, goal, rescreen, history)
                screen = rescreen
        if "stall" in decision:
            return {
                "ok": False,
                "error": "model_stalled",
                "detail": decision["stall"],
                "thought": decision.get("thought"),
                "raw": decision.get("raw"),
                "steps": history,
                "screen": screen,
            }
        if decision.get("done"):
            return {
                "ok": True,
                "reached": "model_done",
                "steps": history,
                "thought": decision.get("thought"),
                "screen": screen,
            }

        text, keys = decision["text"], decision["keys"]
        if not allow_dangerous and (danger := _danger_reason(screen, text, keys, submit=False)):
            return {
                "ok": False,
                "error": "blocked_dangerous",
                "detail": danger,
                "thought": decision.get("thought"),
                "steps": history,
                "screen": screen,
            }
        if text:
            _run_tmux(["send-keys", "-t", session, "-l", text])
        for k in keys:
            _run_tmux(["send-keys", "-t", session, k])
        esc = "" if decision["model"] == chain[0] else f" [escalated:{decision['model']}]"
        history.append(
            f"step {step + 1}: {decision.get('thought', '')!r} text={text!r} keys={keys}{esc}"
        )
        time.sleep(step_pause)

    final = _observe(session, capture_lines) or ""
    return {"ok": False, "error": "max_steps_reached", "steps": history, "screen": final}


@mcp.tool()
@audited
async def tmux_navigate(
    target: str,
    goal: str,
    until: str | None = None,
    max_steps: int = 12,
    step_pause: float = 1.5,
    by_registry_name: bool = False,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """Drive a tmux session's TUI toward a goal, letting a model pick keystrokes.

    Use this to get through an arbitrary menu/wizard to where you actually want
    to be (e.g. the free-text prompt of railway.new's agent, past a project
    picker, through an installer). Each step captures the screen and asks a model
    (`claude -p`, fixed-cost — never the API) which keys to press next.

    Pair with `tmux_ask`: navigate() gets you to the input prompt, tmux_ask()
    holds the conversation. emux never spawns sessions — create it first.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        goal: Plain-English description of where to get to, e.g. "reach the
            free-text chat input for the Railway agent; at any workspace/project
            picker choose the first option".
        until: Optional substring; if it appears on screen, stop and report
            success immediately (a cheap hard-stop alongside the model's own
            done signal).
        max_steps: Max navigation steps before giving up (default 12).
        step_pause: Seconds to wait after each keystroke batch for the UI to
            update (default 1.5).
        by_registry_name: Resolve `target` via the registry.
        allow_dangerous: Off by default — the run is blocked (`blocked_dangerous`)
            if it would type a destructive command or confirm a destructive
            on-screen prompt. Set True to disable the gate.

    Returns:
        {ok, reached: "model_done"|"until", steps: [...], screen} on success;
        {ok: false, error, steps, screen} if it stalls, hits max_steps, or is
        blocked (`blocked_dangerous`).
    """
    return await asyncio.to_thread(
        navigate,
        target,
        goal,
        until,
        max_steps,
        step_pause,
        200,
        None,
        by_registry_name,
        allow_dangerous,
    )


# ---- pursue: goal mode -----------------------------------------------------
#
# navigate() reaches a SCREEN; pursue() reaches a GOAL. It's the full loop:
# observe → decide one action (navigate keys / type a message or field value /
# wait for a streamed reply / declare done) → act → observe again, until the
# model judges the goal met (or impossible) or max_steps is hit. Same escalation
# (Haiku → Sonnet on a stall) and the same keystroke allowlist as navigate.
#
# It's still keystrokes-into-a-TUI — no shell exec, no new powers — just a longer
# leash and a done-judgment about the goal rather than a single screen.

_PURSUE_ACTIONS = ("keys", "type", "wait", "done")


def _wait_stable(
    session: str, capture_lines: int, settle_seconds: float, poll: float, cap: float
) -> str:
    """Poll the pane until it's unchanged for settle_seconds (or cap). Returns
    the settled, ANSI-stripped screen. Used so the model always judges a settled
    frame, and for the explicit 'wait' action after sending a message to an AI."""
    last: str | None = None
    stable = 0.0
    waited = 0.0
    while waited < cap:
        time.sleep(poll)
        waited += poll
        try:
            cur = _strip_ansi(_capture_text(session, capture_lines))
        except (RuntimeError, FileNotFoundError):
            break
        if cur == last:
            stable += poll
            if stable >= settle_seconds:
                break
        else:
            stable = 0.0
            last = cur
    return last if last is not None else ""


def _pursue_decide(chain: list[str], goal: str, screen: str, history: list[str]) -> dict[str, Any]:
    """Ask the model for the next goal-mode action, escalating on a stall.

    Returns a validated action dict {action, ...} or {stall: <reason>}."""
    claude = shutil.which("claude")
    if claude is None:
        return {"stall": "claude CLI not on PATH (needed for goal mode)"}
    hist = "\n".join(f"  - {h}" for h in history[-10:]) or "  (none yet)"
    prompt = (
        "You are pursuing a GOAL by operating a terminal UI. Each turn, look at "
        "the current SCREEN and history, and choose ONE action to make progress.\n\n"
        f"GOAL: {goal}\n\n"
        f"HISTORY (your prior actions + what you observed):\n{hist}\n\n"
        f"CURRENT SCREEN:\n<<<\n{screen}\n>>>\n\n"
        "Reply with ONE line of JSON, nothing else. One of:\n"
        '  {"thought":"..","action":"keys","keys":[<tmux key names>]}\n'
        '  {"thought":"..","action":"type","text":"..","submit":true}\n'
        '  {"thought":"..","action":"wait"}\n'
        '  {"thought":"..","action":"done","success":true,"summary":".."}\n\n'
        f"Allowed key names: {sorted(_NAV_ALLOWED_KEYS)}.\n"
        '- \'keys\' to navigate menus (e.g. ["Down","Enter"]).\n'
        "- 'type' to enter a message, answer, or field value; submit=true presses Enter.\n"
        "- 'wait' if a reply is still streaming / the screen is mid-update.\n"
        "- 'done' when the GOAL is achieved (success=true) or clearly impossible "
        "(success=false); summary states the outcome / answer.\n"
        "Prefer the fewest actions. Never invent a key outside the allowed list."
    )
    last: dict[str, Any] = {}
    for model in chain:
        try:
            proc = subprocess.run(
                [claude, "-p", prompt, "--model", model], capture_output=True, text=True, timeout=90
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            last = {"stall": f"claude -p failed: {e}", "model": model}
            continue
        m = re.search(r"\{.*\}", proc.stdout or "", re.DOTALL)
        if not m:
            last = {
                "stall": "no JSON in model reply",
                "raw": (proc.stdout or "")[:400],
                "model": model,
            }
            continue
        try:
            d = json.loads(m.group(0))
        except json.JSONDecodeError:
            last = {"stall": "unparseable JSON", "raw": (proc.stdout or "")[:400], "model": model}
            continue
        action = d.get("action")
        if action not in _PURSUE_ACTIONS:
            last = {"stall": f"bad action {action!r}", "model": model}
            continue
        if action == "keys":
            d["keys"] = [k for k in (d.get("keys") or []) if k in _NAV_ALLOWED_KEYS]
            if not d["keys"]:
                last = {"stall": "no valid keys", "model": model}
                continue
        if action == "type" and not (d.get("text") or "").strip():
            last = {"stall": "type with empty text", "model": model}
            continue
        d["model"] = model
        return d
    return {"stall": last.get("stall", "unknown"), **last}


def _session_alive(session: str) -> bool:
    """True if the tmux session still exists (detects an ssh drop / kill)."""
    try:
        return _run_tmux(["has-session", "-t", session])[0] == 0
    except FileNotFoundError:
        return False


def _observe(session: str, capture_lines: int, retries: int = 1) -> str | None:
    """Capture a settled, ANSI-stripped screen, retrying a transient blank/error.

    Returns the text, or None if the session is gone. An empty string means the
    session is alive but rendered nothing after the retries (a distinct signal)."""
    for _ in range(retries + 1):
        if not _session_alive(session):
            return None
        try:
            txt = _strip_ansi(_capture_text(session, capture_lines))
        except (RuntimeError, FileNotFoundError):
            txt = ""
        if txt.strip():
            return txt
        time.sleep(0.5)  # transient blank (mid-redraw / just-cleared) — try once more
    return ""


# How many consecutive no-effect actions before goal mode gives up. The model is
# also TOLD it's stuck (via a history note) well before this, so it can recover
# itself; this is only the backstop against an infinite ineffective loop.
_STUCK_LIMIT = 3


# ---- telos: drift-guard for the goal loop ----------------------------------
#
# When enabled, pursue() reports each iteration to telos-md (the eidos drift-
# guard). telos records the whole run — a north star, a tick per step, a close —
# and returns a signal; if it ever says "stop" (drift or no-progress by telos's
# own judgment) the goal loop aborts. This is the first weld between emux (the
# hands) and telos (the conscience): an autonomous loop watched by an external
# guard, and a durable record of what the agent did.
#
# Opt-in and best-effort: if telos-md isn't on PATH or a call fails, the loop
# proceeds unguarded — telos never breaks a run. All emux goal runs are tracked
# in one telos home ($EMUX_TELOS_HOME, default ~/.local/share/emux/telos) so
# `telos-md traffic --repo-path <that>` shows every autonomous run.

_TELOS_METRIC = "reach the goal through the TUI without drifting or stalling"


def _telos_available() -> bool:
    return shutil.which("telos-md") is not None


def _telos_home() -> str:
    home = os.environ.get("EMUX_TELOS_HOME") or str(
        Path.home() / ".local" / "share" / "emux" / "telos"
    )
    p = Path(home)
    p.mkdir(parents=True, exist_ok=True)
    if not (p / ".git").exists():  # telos anchors .telos/ inside a git repo
        subprocess.run(["git", "init", "-q"], cwd=home, capture_output=True)
    return home


def _telos_call(args: list[str], home: str) -> dict[str, Any] | None:
    """Run a telos-md subcommand with --json; parsed dict or None on any failure."""
    try:
        proc = subprocess.run(
            ["telos-md", *args, "--repo-path", home, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"\{.*\}", proc.stdout or "", re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def pursue(
    target: str,
    goal: str,
    max_steps: int = 15,
    settle_seconds: float = 2.5,
    wait_cap: float = 60.0,
    capture_lines: int = 200,
    model: str | None = None,
    by_registry_name: bool = False,
    telos: bool = False,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """Pursue `goal` in a tmux TUI: observe → act → observe until done.

    The autonomous goal loop over navigate/ask primitives. Each step the model
    picks one action (keys / type / wait / done); after acting, emux waits for the
    screen to settle before the next observation, so the model reasons over a
    stable frame and can read an agent's reply. emux never spawns — the session
    must already exist and be running whatever UI the goal concerns.

    telos=True routes the run through the telos-md drift-guard: a north star is
    opened for `goal`, each step ticks telos, a telos `stop` signal aborts the
    loop (`telos_stop`), and the north star is closed (reached/abandoned) on exit.
    Best-effort — a missing telos-md just means the loop runs unguarded."""
    if _resolve_tmux() is None:
        return {"ok": False, "error": "tmux_not_installed"}
    session = target
    if by_registry_name:
        registry = _load_registry()
        if target not in registry:
            return {"ok": False, "error": "not_registered", "name": target}
        session = registry[target]["session"]
    chain = [model] if model else [_NAV_MODEL_DEFAULT, _NAV_MODEL_ESCALATE]

    # Open a telos north star for this run, if requested and available.
    ns_id: str | None = None
    thome: str | None = None
    if telos and _telos_available():
        thome = _telos_home()
        opened = _telos_call(["set-north-star", "--goal", goal, "--metric", _TELOS_METRIC], thome)
        ns_id = (opened or {}).get("north_star_id")

    result = _pursue_core(
        session,
        goal,
        chain,
        max_steps,
        settle_seconds,
        wait_cap,
        capture_lines,
        ns_id,
        thome,
        allow_dangerous,
    )

    if ns_id and thome:  # close the north star with a terminal outcome
        outcome = "reached" if result.get("ok") and result.get("success") else "abandoned"
        _telos_call(["close", "--north-star-id", ns_id, "--outcome", outcome], thome)
        result["telos"] = {"north_star_id": ns_id, "outcome": outcome}
    return result


def _pursue_core(
    session: str,
    goal: str,
    chain: list[str],
    max_steps: int,
    settle_seconds: float,
    wait_cap: float,
    capture_lines: int,
    ns_id: str | None = None,
    thome: str | None = None,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """The observe→act→judge loop. Ticks telos each step when ns_id is set."""
    history: list[str] = []
    no_progress = 0  # consecutive actions that changed nothing on screen
    for step in range(max_steps):
        # --- observe, recovering from a dead session or a transient blank ---
        screen = _observe(session, capture_lines)
        if screen is None:
            return {
                "ok": False,
                "error": "session_gone",
                "detail": "tmux session vanished (ssh dropped or killed)",
                "steps": history,
            }
        if not screen.strip():
            return {
                "ok": False,
                "error": "blank_screen",
                "detail": "session alive but rendered nothing",
                "steps": history,
            }

        # Tell the model when its recent actions aren't moving the UI, so it can
        # change tactics (Escape, a different item) before we give up.
        ctx = history
        if no_progress:
            ctx = history + [
                f"NOTE: the last {no_progress} action(s) did not change the "
                "screen — you appear stuck; try a DIFFERENT action (e.g. Escape, "
                "a different menu item), or 'done' with success=false if impossible."
            ]

        # --- decide, retrying once on a transient stall (re-observe first) ---
        d = _pursue_decide(chain, goal, screen, ctx)
        if "stall" in d:
            time.sleep(1.0)
            rescreen = _observe(session, capture_lines)
            if rescreen is None:
                return {"ok": False, "error": "session_gone", "steps": history}
            if rescreen and rescreen.strip():
                d = _pursue_decide(chain, goal, rescreen, ctx)
                screen = rescreen
        if "stall" in d:
            return {
                "ok": False,
                "error": "model_stalled",
                "detail": d["stall"],
                "raw": d.get("raw"),
                "steps": history,
                "screen": screen,
            }

        action = d["action"]
        esc = "" if d.get("model") == chain[0] else f" [escalated:{d.get('model')}]"
        thought = d.get("thought", "")

        if action == "done":
            return {
                "ok": True,
                "reached": "done",
                "success": bool(d.get("success", True)),
                "summary": d.get("summary", ""),
                "steps": history,
                "screen": screen,
            }
        if action == "wait":
            screen = _wait_stable(session, capture_lines, settle_seconds, 0.8, wait_cap)
            history.append(f"step {step + 1}: wait{esc} — observed: {_tail(screen)}")
            continue
        # --- destructive-action gate (unless explicitly allowed) ---
        if not allow_dangerous:
            danger = (
                _danger_reason(screen, "", d["keys"], submit=False)
                if action == "keys"
                else _danger_reason(screen, d["text"], [], submit=bool(d.get("submit")))
            )
            if danger:
                history.append(f"step {step + 1}: BLOCKED ({thought!r}) — {danger}")
                return {
                    "ok": False,
                    "error": "blocked_dangerous",
                    "detail": danger,
                    "steps": history,
                    "screen": screen,
                }

        if action == "keys":
            for k in d["keys"]:
                _run_tmux(["send-keys", "-t", session, k])
            history.append(f"step {step + 1}: keys={d['keys']} ({thought!r}){esc}")
        elif action == "type":
            _run_tmux(["send-keys", "-t", session, "-l", d["text"]])
            if d.get("submit"):
                _run_tmux(["send-keys", "-t", session, "Enter"])
            history.append(
                f"step {step + 1}: type={d['text']!r} submit={bool(d.get('submit'))} ({thought!r}){esc}"
            )

        # Let the UI react, then re-observe a settled frame.
        settled = _wait_stable(session, capture_lines, settle_seconds, 0.8, wait_cap)

        # --- stuck detection: an active action that changed nothing ---
        if settled == screen:
            no_progress += 1
            history[-1] += f" -> {_tail(settled)} [NO CHANGE x{no_progress}]"
            if no_progress >= _STUCK_LIMIT:
                return {
                    "ok": False,
                    "error": "stuck_no_progress",
                    "detail": f"{no_progress} consecutive actions changed nothing",
                    "steps": history,
                    "screen": settled,
                }
        else:
            no_progress = 0
            history[-1] += f" -> {_tail(settled)}"

        # --- telos drift-guard: report this step; honor a stop signal ---
        if ns_id and thome:
            tick = _telos_call(
                [
                    "tick",
                    "--north-star-id",
                    ns_id,
                    "--action-summary",
                    (thought or action)[:200],
                    # measurement is the settled UI signature: it stays constant
                    # when the agent is stuck, so telos's zero-delta guard fires.
                    "--measurement",
                    _tail(settled, 2) or "(blank)",
                ],
                thome,
            )
            if tick and tick.get("signal") == "stop":
                why = tick.get("drift_category") or tick.get("heading") or "drift"
                history[-1] += f" [telos STOP: {why}]"
                return {
                    "ok": False,
                    "error": "telos_stop",
                    "detail": f"telos halted the run: {why}",
                    "steps": history,
                    "screen": settled,
                }

    final = _observe(session, capture_lines) or ""
    return {"ok": False, "error": "max_steps_reached", "steps": history, "screen": final}


def _tail(screen: str, n: int = 3) -> str:
    """Last n non-blank lines of a screen, one line, for compact history."""
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[:240]


@mcp.tool()
@audited
async def tmux_goal(
    target: str,
    goal: str,
    max_steps: int = 15,
    by_registry_name: bool = False,
    telos: bool = False,
    allow_dangerous: bool = False,
) -> dict[str, Any]:
    """Pursue a GOAL in a tmux TUI autonomously — observe, act, repeat until done.

    Goal mode: the level above tmux_navigate. Where navigate reaches a screen,
    this keeps going until the whole task is done — walking menus, typing
    messages/answers/field values, waiting for streamed replies, and judging
    completion. Each step a model (`claude -p`, fixed-cost — never the API) picks
    one action: navigate keys, type text, wait, or done. Escalates Haiku→Sonnet
    on a stall; keystrokes are allowlist-restricted.

    Still keystrokes-into-a-TUI — it cannot exec shell or do anything a person at
    that terminal couldn't. emux never spawns; create the session first.

    Recovers from: a flaky model step (escalates Haiku→Sonnet), a transient
    stall or blank capture (re-observe + retry once), a no-progress loop (warns
    the model, then aborts `stuck_no_progress` at the backstop), and a dropped
    session (`session_gone`).

    Safety: by default a **destructive-action gate** blocks the run
    (`blocked_dangerous`) if a step would type a destructive command (rm -rf,
    DROP TABLE, force-push…) or confirm a destructive on-screen prompt
    ("Delete? [y]"). It's a heuristic denylist, not a sandbox — set
    `allow_dangerous=True` to disable it. Still keystrokes-into-a-TUI — it cannot
    exec shell or do anything a person at that terminal couldn't. emux never
    spawns; create the session first.

    Args:
        target: tmux session name, or registry name if `by_registry_name=True`.
        goal: What to accomplish, e.g. "Ask the Railway agent to list my services,
            then tell me which have a cron schedule."
        max_steps: Max observe/act cycles before giving up (default 15).
        by_registry_name: Resolve `target` via the registry.
        telos: Route the run through the telos-md drift-guard — record it as a
            north star, tick each step, and abort (`telos_stop`) if telos signals
            drift/no-progress. Best-effort; no-op if telos-md isn't installed.
        allow_dangerous: Disable the destructive-action gate (default off).

    Returns:
        {ok, reached:"done", success, summary, steps:[...], screen} on completion;
        {ok:false, error, steps, screen} on stall / max_steps / blocked_dangerous;
        `telos` block when the drift-guard was engaged.
    """
    return await asyncio.to_thread(
        pursue,
        target,
        goal,
        max_steps,
        2.5,
        60.0,
        200,
        None,
        by_registry_name,
        telos,
        allow_dangerous,
    )


# ---- login: drive a Claude Code login/logout sequence ------------------------
#
# When a managed session hits a LOGIN gate (logged out, wrong account, /login
# mid-sequence), supervision dead-ends: the OAuth hop needs a browser, and the
# TUI steps need keystrokes. login_flow does everything BUT the browser hop —
# it starts/advances the sequence, surfaces the OAuth URL for a human (or a
# browser-capable agent), then finishes with the pasted code and verifies from
# the screen. The sequence is a fixed TUI, so the steps are deterministic —
# no model calls. Host-aware via the registry, like every other emux op.

_OAUTH_URL_RE = re.compile(r"https://(?:claude\.ai|console\.anthropic\.com)/oauth[^\s\"'`)\]]*")
_LOGIN_SUCCESS_RE = re.compile(r"Login successful|Logged in as", re.I)
_LOGIN_FAIL_RE = re.compile(r"Invalid (?:code|authorization)|Login failed|OAuth error", re.I)


def _login_step(
    screen: str, code_pending: bool = False, sent_login: bool = False
) -> dict[str, Any]:
    """Decide the next login-flow action from a settled screen. Pure — this is
    the testable core of login_flow.

    Returns {"action": ...}: "done" (logged in), "paste" (paste-code prompt is
    up), "url" (OAuth URL on screen — the human/browser hop, with "url" key),
    "enter" (a menu/confirm step Enter advances), "send_login" (start the
    sequence), or "unknown" (not a login screen we recognise)."""
    low = screen.lower()
    if _LOGIN_SUCCESS_RE.search(screen):
        return {"action": "done"}
    if code_pending:
        # Finishing: only accept if the paste prompt (or its URL) is still up.
        if "paste code" in low or _OAUTH_URL_RE.search(screen):
            return {"action": "paste"}
        return {"action": "unknown"}
    if m := _OAUTH_URL_RE.search(screen):
        return {"action": "url", "url": m.group(0)}
    if "select login method" in low:
        return {"action": "enter"}  # Enter picks the highlighted (default) method
    if re.search(r"press enter to (?:log ?in|open|continue|retry)", low):
        return {"action": "enter"}
    if not sent_login:
        return {"action": "send_login"}
    return {"action": "unknown"}


def login_flow(
    target: str,
    code: str | None = None,
    switch: bool = False,
    by_registry_name: bool = False,
    max_steps: int = 8,
    settle_seconds: float = 1.5,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Drive a Claude Code login sequence in a tmux session, local or remote.

    Three entry shapes:
      login_flow(t)               — start (or resume) the sequence; returns the
                                    OAuth URL once it is on screen.
      login_flow(t, code="…")     — paste the authorization code into the
                                    waiting prompt and verify success.
      login_flow(t, switch=True)  — change account: /logout first, then start.

    The code is sent as keystrokes and never persisted anywhere by emux.
    """
    session, host, err = _resolve_target(target, by_registry_name)
    if err is not None or session is None:
        return {"ok": False, "error": err or "unresolved_target", "name": target}
    if not _session_exists(session, host=host):
        return {"ok": False, "error": "session_not_found", "session": session}
    if _pane_agent(session, host) != "claude":
        return {
            "ok": False,
            "error": "not_a_claude_pane",
            "detail": "the pane is not running claude — if it exited, "
            "restart claude in the session first",
        }

    def cap() -> str:
        try:
            # -J joins wrapped lines so the OAuth URL survives narrow panes.
            c, out, _ = _run_tmux(
                ["capture-pane", "-t", session, "-p", "-J", "-S", "-150"], host=host
            )
        except FileNotFoundError:
            return ""
        return _strip_ansi(out or "") if c == 0 else ""

    def send(text: str | None, *keys: str) -> None:
        if text:
            _run_tmux(["send-keys", "-t", session, "-l", text], host=host)
        for k in keys:
            _run_tmux(["send-keys", "-t", session, k], host=host)

    def settled(cap_seconds: float = 12.0) -> str:
        """Poll until the screen is unchanged for settle_seconds (or cap)."""
        last, stable, waited = cap(), 0.0, 0.0
        while waited < cap_seconds:
            time.sleep(0.8)
            waited += 0.8
            cur = cap()
            if cur == last:
                stable += 0.8
                if stable >= settle_seconds:
                    break
            else:
                last, stable = cur, 0.0
        return last

    # ---- finish: paste the code into the waiting prompt, verify success ----
    if code is not None:
        screen = settled()
        if _login_step(screen, code_pending=True)["action"] != "paste":
            return {
                "ok": False,
                "error": "no_paste_prompt",
                "detail": "the session is not waiting for a login code",
                "screen": _tail(screen, 12),
            }
        send(code, "Enter")
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(1.5)
            screen = cap()
            if _LOGIN_SUCCESS_RE.search(screen):
                send(None, "Enter")  # dismiss "Press Enter to continue"
                time.sleep(1.0)
                return {"ok": True, "logged_in": True, "screen": _tail(cap(), 6)}
            if _LOGIN_FAIL_RE.search(screen):
                return {"ok": False, "error": "login_failed", "screen": _tail(screen, 12)}
        return {
            "ok": False,
            "error": "login_not_confirmed",
            "detail": "no success marker before timeout",
            "screen": _tail(cap(), 12),
        }

    # ---- start: (optionally /logout), step through the TUI to the OAuth URL ----
    if switch:
        send("/logout", "Enter")
        settled()

    finish = f"emux login {target}{' -n' if by_registry_name else ''} --code <paste>"
    sent_login = False
    for _ in range(max_steps):
        screen = settled()
        if not screen.strip():
            if not _session_exists(session, host=host):
                return {"ok": False, "error": "session_gone"}
            return {"ok": False, "error": "blank_screen"}
        step = _login_step(screen, sent_login=sent_login)
        if step["action"] == "done":
            return {"ok": True, "logged_in": True, "screen": _tail(screen, 6)}
        if step["action"] == "url":
            return {
                "ok": True,
                "logged_in": False,
                "url": step["url"],
                "next": f"open the url in a browser, sign in, then finish with: {finish}",
            }
        if step["action"] == "enter":
            send(None, "Enter")
        elif step["action"] == "send_login":
            send("/login", "Enter")
            sent_login = True
        else:
            return {
                "ok": False,
                "error": "unrecognized_screen",
                "detail": "not a known login screen — drive it manually "
                "with emux navigate / tmux_navigate",
                "screen": _tail(screen, 12),
            }
        time.sleep(0.5)
    return {"ok": False, "error": "max_steps_reached", "screen": _tail(cap(), 12)}


@mcp.tool()
async def tmux_login(
    target: str,
    code: str | None = None,
    switch: bool = False,
    by_registry_name: bool = False,
) -> dict[str, Any]:
    """Drive a Claude Code LOGIN sequence in a managed session — the unblock for
    a `login_gate` flag from tmux_classify.

    Two-call shape (the browser hop is yours):
      1. tmux_login(target) — starts/advances the sequence (sends /login, picks
         the default method) and returns the OAuth `url`. Open it, sign in, and
         copy the authorization code.
      2. tmux_login(target, code="…") — pastes the code into the waiting prompt
         and verifies success from the screen.

    `switch=True` changes account: /logout first, then the sequence above.

    Deterministic keystrokes only (no model calls); host-aware via the registry
    like every other emux op. The code is sent as keystrokes and never persisted
    or written to the audit trail.

    Returns:
        {ok, logged_in, url?, next?} — `url` present when the browser hop is
        needed; {ok: false, error, screen?} on session/screen problems
        (`not_a_claude_pane`, `no_paste_prompt`, `unrecognized_screen`, …).
    """
    result = await asyncio.to_thread(login_flow, target, code, switch, by_registry_name)
    _audit(
        "tmux_login",
        {
            "target": target,
            "switch": switch,
            "by_registry_name": by_registry_name,
            "code": "<redacted>" if code else None,
        },
        result,
    )
    return result


# ---- doctor: diagnose a session's ENVIRONMENT, not its output ---------------
#
# Born from a real hour lost: a long-running tmux server silently lost macOS
# TCC access to a volume, every pane got EPERM on file ops, and the sessions
# looked "broken" while the disk was fine. The tell was comparing the tmux
# server's own access (run-shell) against a fresh process's (direct/ssh) —
# doctor automates exactly that comparison plus the basic health probes.


def doctor(target: str, by_registry_name: bool = False) -> dict[str, Any]:
    """Environment health for one session: liveness, pane, capture, stream log,
    gate, and a tmux-server-vs-fresh-process filesystem probe of the pane's cwd."""
    session, host, err = _resolve_target(target, by_registry_name)
    if err is not None or session is None:
        return {"ok": False, "error": err or "unresolved_target", "name": target}
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool | None, detail: str) -> None:
        checks.append({"check": name, "ok": ok, "detail": detail})

    live = _session_exists(session, host=host)
    check(
        "session_live",
        live,
        f"tmux has-session {session}" + (f" on {host}" if host else " locally"),
    )
    if by_registry_name:
        entry = _load_registry().get(target) or {}
        targetable = bool(entry.get("cwd")) and bool(entry.get("channels"))
        check(
            "remote_target",
            targetable if entry else None,
            "cwd + channels present"
            if targetable
            else "missing cwd/channels — unreachable via emux-remote/1.0; re-register to stamp (EID-869)",
        )
    if not live:
        return {
            "ok": False,
            "target": target,
            "session": session,
            "host": host,
            "checks": checks,
            "diagnosis": "session is gone — clean up or respawn",
        }

    code, out, _ = _run_tmux(
        ["display-message", "-p", "-t", session, "#{pane_current_command}|#{pane_current_path}"],
        host=host,
    )
    pane_cmd, _, pane_cwd = out.strip().partition("|") if code == 0 else ("", "", "")
    check("pane", code == 0, f"command={pane_cmd or '?'} cwd={pane_cwd or '?'}")

    cap = _pane_text_for(target if by_registry_name else session, lines=30)
    check("capture", bool(cap.strip()), f"{len(cap.splitlines())} lines captured")

    log_sz = _log_size(target if by_registry_name else session)
    check(
        "stream_log",
        log_sz > 0,
        f"{log_sz} bytes"
        if log_sz
        else "no armed stream log — idle-wait falls back to pane polling",
    )

    from . import adapters

    gate = adapters.gated(_pane_agent(session, host), cap)
    check("gate", None, f"modal gate up: {gate!r}" if gate else "no modal gate")

    diagnosis = "healthy"
    if pane_cwd:
        # THE probe: can the tmux server's process tree read the pane's cwd,
        # and can a FRESH process (this one, or a fresh sshd child) read it?
        q = shlex.quote(pane_cwd)
        code_srv, out_srv, err_srv = _run_tmux(
            ["run-shell", f"ls {q} >/dev/null 2>&1 && echo OK || echo DENIED"], host=host
        )
        server_ok = code_srv == 0 and "OK" in out_srv
        if host:
            fresh = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"ls {q}"],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            fresh_ok = fresh.returncode == 0
        else:
            fresh_ok = os.access(pane_cwd, os.R_OK | os.X_OK)
        check(
            "fs_tmux_server",
            server_ok,
            f"tmux server reads {pane_cwd}"
            if server_ok
            else f"tmux server CANNOT read {pane_cwd} ({(out_srv or err_srv).strip()[:80]})",
        )
        check(
            "fs_fresh_process",
            fresh_ok,
            f"fresh process reads {pane_cwd}"
            if fresh_ok
            else f"fresh process cannot read {pane_cwd}",
        )
        if fresh_ok and not server_ok:
            diagnosis = (
                "tmux server lost filesystem access to the pane's cwd while the "
                "disk is fine (macOS TCC grants don't reach running processes). "
                "Fix: grant Full Disk Access to the tmux binary and RESTART the "
                "tmux server at the desk — this kills its sessions, so time it."
            )
        elif not fresh_ok and not server_ok:
            diagnosis = "the path is unreadable from everywhere — check the volume/mount itself"
    if gate and diagnosis == "healthy":
        diagnosis = (
            "healthy, but a modal gate is up — answer it (tmux_send force with the exact key)"
        )
    return {
        "ok": True,
        "target": target,
        "session": session,
        "host": host,
        "checks": checks,
        "diagnosis": diagnosis,
    }


@mcp.tool()
@audited
async def tmux_doctor(target: str, by_registry_name: bool = False) -> dict[str, Any]:
    """Diagnose a session's ENVIRONMENT — the failures classify can't see.

    Probes: liveness, pane command/cwd, capture, armed stream log, modal gate,
    and — the one that finds the invisible failure — whether the tmux SERVER's
    process tree can read the pane's cwd vs a FRESH process (run-shell vs
    direct/ssh). A mismatch means the long-running tmux server lost a macOS
    TCC/Full-Disk-Access grant (grants don't reach running processes) and the
    whole session tree gets EPERM while the disk is healthy; the fix is a
    desk-side tmux-server restart, and doctor says so instead of leaving the
    session to look mysteriously broken.

    Returns {ok, checks: [{check, ok, detail}], diagnosis}.
    """
    return await asyncio.to_thread(doctor, target, by_registry_name)


@mcp.tool()
@audited
async def tmux_signals(
    targets: list[str] | None = None,
    under: str | None = None,
    ack: bool = True,
) -> dict[str, Any]:
    """Read NEW up-channel signals workers have emitted since you last read.

    The up-channel. A worker cannot call emux — emux only sees its output — so a
    worker talks UP to its manager by echoing a sentinel line:

        @@EMUX@@ <KIND> <payload>          KIND ∈ DONE | NEED | PROGRESS | ERROR

    e.g. `echo "@@EMUX@@ NEED approve deploy to prod? (y/n)"`. emux lifts these
    out of each session's stream log, so a manager learns "worker 4 finished,
    worker 7 needs a decision" WITHOUT reading a single screen — the clean way
    for workers to talk up the tree instead of the manager guessing from pixels.

    Args:
        targets: registry names to check; omit to check every registered session.
        under:   a manager's name — check the sessions IT manages (its subtree).
        ack:     True (default) marks what it returns as consumed, so you only
                 ever see NEW signals; False peeks without consuming.

    Returns {ok, signals:[{t, session, kind, payload}], count}, oldest→newest.
    Acked signals are also appended to ~/.local/state/emux/signals.jsonl.
    """
    names = _resolve_watch_targets(targets, under)
    sigs = [sig for n in names for sig in _new_signals(n, ack)]
    return {"ok": True, "signals": sigs, "count": len(sigs)}


_PROMPT_RE = re.compile(r"(\(y/n\)|\[y/n\]|\?[ \t]*$|:[ \t]*$|>[ \t]*$)", re.IGNORECASE)


@mcp.tool()
@audited
async def tmux_wait(
    targets: list[str] | None = None,
    under: str | None = None,
    until: str = "signal",
    timeout: float = 60.0,
    quiet: float = 4.0,
) -> dict[str, Any]:
    """Block until one or more sessions NEED you — then return which, and why.

    The poll→event shift. Instead of capturing N workers in a loop and filling
    your context with screens you didn't need, make ONE call and be woken when
    something actually happens. emux runs the watch loop internally — cheaply: it
    stats each session's stream log and only looks closer when one grew — so your
    context stays empty until there is something to handle. This is what lets one
    mind manage many: react, don't poll.

    until — what counts as an event:
      "signal" — a worker emitted a new @@EMUX@@ signal (see tmux_signals). The
                 reliable path; the ready entry carries the signal. Default.
      "idle"   — a session's output went quiet for `quiet` seconds (likely done
                 or blocked). Quiet is judged from the armed stream log; a
                 session with NO armed log falls back to live pane comparison —
                 a missing log is never read as quiet (that was the false-idle
                 bug: unarmed sessions reported ready-idle instantly).
      "exit"   — a session ended.
      "change" — any new output since the call began.
      "prompt" — the last line looks like it is waiting for input. A HEURISTIC
                 guess — prefer "signal" when a worker can emit one.

    Args:
        targets/under: which sessions (as in tmux_signals).
        until: event type above. timeout: seconds to wait (clamped 1..600).
        quiet: seconds of no output that count as idle (for until="idle").

    Returns {ok, ready:[{name, why, signal?, last_line, state, flags, summary,
    gate?}], still_working:[names], timed_out}. Each ready entry carries the
    Tier-0 classification and any modal gate text, so you can usually act from
    this one call. Returns the moment ANY target is ready, or at timeout.
    """
    names = _resolve_watch_targets(targets, under)
    timeout = max(1.0, min(float(timeout), 600.0))
    deadline = time.time() + timeout
    size = {n: _log_size(n) for n in names}
    # A session registered after spawn may have NO armed stream log; its log
    # silence means NOTHING. For those, quiet/change is judged by comparing the
    # live pane instead — a missing log must never be read as quiet-forever
    # (the false-idle bug: 8 running sessions reported ready-idle instantly).
    has_log = {n: _log_path(n).is_file() for n in names}
    pane_seen: dict[str, str] = {}
    if until in ("idle", "change", "prompt"):
        # Baseline the pane now so the first capture doesn't read as "change".
        pane_seen = {n: _pane_text_for(n) for n in names if not has_log[n]}
    last_grow = {n: time.time() for n in names}
    while True:
        ready = []
        for n in names:
            why, extra = None, {}
            if until == "signal":
                sigs = _new_signals(n, ack=True)
                if sigs:
                    why, extra["signal"] = "signal", sigs[-1]
            elif until == "exit":
                if not _name_live(n):
                    why = "exit"
            else:
                if not has_log[n]:  # a log may get armed mid-wait — upgrade
                    has_log[n] = _log_path(n).is_file()
                if has_log[n]:
                    sz = _log_size(n)
                    grew = sz != size.get(n, 0)
                    if grew:
                        size[n] = sz
                else:
                    # ponytail: pane capture per poll for log-less sessions
                    # only; if many REMOTE log-less sessions show up, arm
                    # their logs instead of polling ssh once a second.
                    cur = _pane_text_for(n)
                    grew = pane_seen.get(n) != cur
                    pane_seen[n] = cur
                if grew:
                    last_grow[n] = time.time()
                    if until == "change":
                        why = "change"
                if (
                    until == "idle"
                    and time.time() - last_grow.get(n, 0.0) >= quiet
                    and _name_live(n)
                ):
                    why = "idle"
                elif until == "prompt":
                    line = _read_log(n, lines=1).strip()
                    if line and _PROMPT_RE.search(line):
                        why = "prompt"
            if why:
                line = _read_log(n, lines=1).strip()
                if not line and not has_log.get(n, True):
                    # no log to quote — use the live pane's last non-blank line
                    pane = pane_seen.get(n) or _pane_text_for(n)
                    line = next((ln for ln in reversed(pane.splitlines()) if ln.strip()), "")
                extra.setdefault("last_line", line.strip()[-200:])
                # Carry the DECISION, not an invitation to poll: classify the
                # ready session and extract any modal gate text, so the caller
                # can act from this one call (no wait→classify→capture triple).
                try:
                    from . import adapters
                    from .judge import classify_session

                    verdict = classify_session(n)
                    extra["state"] = verdict.get("state")
                    extra["flags"] = verdict.get("flags", [])
                    extra["summary"] = verdict.get("summary")
                    entry = _load_registry().get(n) or {}
                    sess, hst = entry.get("session", n), entry.get("host")
                    gate = adapters.gated(_pane_agent(sess, hst), _pane_text_for(n, lines=15))
                    if gate:
                        extra["gate"] = gate
                except Exception:  # noqa: BLE001, S110 — enrichment must never break the wait
                    pass
                ready.append({"name": n, "why": why, **extra})
        if ready or time.time() >= deadline:
            done = {r["name"] for r in ready}
            return {
                "ok": True,
                "ready": ready,
                "still_working": [n for n in names if n not in done],
                "timed_out": not ready,
            }
        await asyncio.sleep(1.0)


@mcp.tool()
@audited
async def agent_advice(scenario: str = "") -> dict[str, Any]:
    """Which AI agent should run for this kind of work — and why.

    emux spawns sessions that run coding agents; WHICH agent is a real decision.
    This is the registry of that decision, kept in `emux/agents.py` and
    overridable at ~/.config/emux/agents.json.

    The axis is CAPABILITY, not price: the operator subscribes to both Claude
    Code and Codex (flat fee), and the metered API is a hard no. So "route cheap
    tokens to a cheap tier" saves nothing here — route on what each agent is
    actually good at.

    Args:
        scenario: plain English — what the session is FOR ("refactor the auth
            module", "leave a long build running overnight", "second opinion on
            this design"). Omit to get the whole table.

    Returns:
        With a scenario: {agent, command, why, evidence, matched, installed, access}.
        Without: the full registry — agents, routes, rejected claims, what's installed.
    """
    from . import agents as _agents

    if not (scenario or "").strip():
        return {"ok": True, **_agents.table()}
    return {"ok": True, **_agents.advise(scenario)}


def _session_cwd(sid: str) -> str | None:
    """The working directory a Claude Code session ran in, read from its own
    transcript (~/.claude/projects/<slug>/<sid>.jsonl). Robust where slug→path
    reversal is not (a project dir with a hyphen breaks the slug)."""
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return None
    for d in base.iterdir():
        f = d / f"{sid}.jsonl"
        if not f.is_file():
            continue
        try:
            with f.open() as fh:
                for line in fh:
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(o, dict) and o.get("cwd"):
                        return str(o["cwd"])
        except Exception:
            return None
    return None


@mcp.tool()
@audited
async def move_to_emux(
    name: str | None = None, session_id: str | None = None, cwd: str | None = None, gui: bool = True
) -> dict[str, Any]:
    """Move the CURRENT Claude Code session INTO emux — resume this conversation
    inside a driveable, emux-registered tmux window so emux can watch, classify,
    and steer it from the web control room.

    Called from inside Claude Code with NO args, it moves THIS chat: the session
    id comes from `$CLAUDE_CODE_SESSION_ID` and the working dir from the session's
    own transcript. It spawns a tmux session running `claude --resume <id>` (a GUI
    window by default) and registers it.

    CAVEAT: resuming makes a SECOND live copy of the conversation. Switch to the
    new window and CLOSE the original terminal so only one instance stays live —
    two instances writing the same transcript can conflict.

    Args:
        name: registry name for the moved session (default `chat-<id8>`).
        session_id: session to move (default `$CLAUDE_CODE_SESSION_ID`).
        cwd: dir to resume in (default: the session's own recorded cwd).
        gui: open a visible terminal window (default True).

    Returns:
        {ok, name, session_id, cwd, attach, note} — or {ok:false, error}.
    """
    sid = session_id or os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return {
            "ok": False,
            "error": "no_session_id",
            "hint": "not inside a Claude Code session; pass session_id=<id>",
        }
    workdir = cwd or _session_cwd(sid) or os.environ.get("HOME") or "."
    reg_name = name or f"chat-{sid[:8]}"
    resume = (
        f"cd {shlex.quote(workdir)} && "
        f"claude --dangerously-skip-permissions --resume {shlex.quote(sid)}"
    )
    r = await tmux_spawn(
        name=reg_name,
        gui=gui,
        command=resume,
        description=f"Claude Code chat {sid[:8]} — moved into emux",
        tags=["claude", "chat", "moved"],
    )
    if not r.get("ok"):
        return {"ok": False, "error": "spawn_failed", "detail": r}
    return {
        "ok": True,
        "name": reg_name,
        "session_id": sid,
        "cwd": workdir,
        "attach": f"tmux attach -t {reg_name}",
        "note": (
            "A window opened with this conversation resumed. Switch to it and "
            "CLOSE the original terminal so only one copy stays live."
        ),
    }


def run_mcp_server() -> None:
    """Start the emux MCP server (stdio transport).

    Invoked by `emux mcp`. The CLI dispatcher in `emux.cli` calls this.
    """
    mcp.run()


if __name__ == "__main__":
    run_mcp_server()
