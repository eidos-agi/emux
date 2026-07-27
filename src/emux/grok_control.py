"""Grok Build control-plane helpers (emux side).

Grok is more than a disk scan + bare `grok` spawn. Documented surfaces we use:

* Session index under ``~/.grok/sessions`` — ``summary.json`` (+ optional
  ``updates.jsonl`` / ``chat_history.jsonl`` for enrichment)
* CLI resolution — absolute path (``EMUX_GROK_BIN``, ``~/.grok/bin/grok``, PATH)
* Interactive / headless resume — ``grok --resume <id>`` and ``grok -p … -r <id>``
* ACP entry — ``grok agent stdio`` (command list only; full protocol later)
* Hooks — file-based JSON under ``~/.grok/hooks/`` or ``<project>/.grok/hooks/``

Pure helpers: no network, no tmux, no heavy deps. See docs/grok-control-plane.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote

# Session id shape used by Grok (UUID-ish, often UUID v7).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.I,
)

_ENV_BIN = "EMUX_GROK_BIN"
_DEFAULT_HOOK_NAME = "emux-bridge.json"


# ---------------------------------------------------------------------------
# Paths / CLI resolution
# ---------------------------------------------------------------------------


def grok_home(home: Path | None = None) -> Path:
    """Grok state root (override with GROK_HOME if set)."""
    env = (os.environ.get("GROK_HOME") or "").strip()
    if env:
        return Path(env).expanduser()
    return (home or Path.home()) / ".grok"


def sessions_root(home: Path | None = None) -> Path:
    return grok_home(home) / "sessions"


def hooks_global_dir(home: Path | None = None) -> Path:
    return grok_home(home) / "hooks"


def hooks_project_dir(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser().resolve() / ".grok" / "hooks"


def resolve_grok_bin(
    *,
    env: dict[str, str] | None = None,
    home: Path | None = None,
) -> str | None:
    """Absolute path to the ``grok`` binary.

    Order: ``EMUX_GROK_BIN`` → ``~/.grok/bin/grok`` → augmented PATH which →
    common install locations. launchd often has PATH=/usr/bin:/bin only, so
    we never rely on bare ``which grok`` alone.
    """
    e = env if env is not None else os.environ
    home = home or Path.home()
    override = (e.get(_ENV_BIN) or "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return str(p.resolve())

    candidates: list[Path] = [
        home / ".grok" / "bin" / "grok",
        home / ".local" / "bin" / "grok",
        home / "bin" / "grok",
        Path("/opt/homebrew/bin/grok"),
        Path("/usr/local/bin/grok"),
    ]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand.resolve())

    prefix = os.pathsep.join(
        str(p)
        for p in (
            home / ".grok" / "bin",
            home / ".local" / "bin",
            home / "bin",
            Path("/opt/homebrew/bin"),
            Path("/usr/local/bin"),
        )
    )
    search = prefix + os.pathsep + (e.get("PATH") or "")
    found = shutil.which("grok", path=search)
    if found:
        return str(Path(found).resolve())
    return None


# ---------------------------------------------------------------------------
# Session index enrichment
# ---------------------------------------------------------------------------


@dataclass
class GrokSessionIndex:
    """Normalized view of one Grok session directory."""

    session_id: str
    cwd: str
    path: str  # session directory
    title: str = ""
    summary: str = ""
    model: str | None = None
    branch: str | None = None
    messages: int | None = None
    chat_messages: int | None = None
    mtime_iso: str | None = None
    last_active_at: str | None = None
    agent_name: str | None = None
    project_cwd: str = ""
    last_user_snippet: str = ""
    session_kind: str | None = None
    parent_session_id: str | None = None
    hidden: bool = False
    summary_mtime: float = 0.0
    updates_mtime: float = 0.0
    source_files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def activity_mtime(self) -> float:
        """Best watermark for dirty detection / age ranking."""
        return max(self.summary_mtime or 0.0, self.updates_mtime or 0.0)

    @property
    def is_subagent(self) -> bool:
        kind = (self.session_kind or "").lower()
        if kind.startswith("subagent"):
            return True
        return bool(self.hidden)


def _iso_to_epoch(raw: str | None) -> float | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _text_from_content(c: Any) -> str:
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, dict):
        if c.get("text"):
            return str(c["text"]).strip()
        return _text_from_content(c.get("content") or c.get("message") or "")
    if isinstance(c, list):
        parts = [_text_from_content(x) for x in c]
        return " ".join(p for p in parts if p).strip()
    return str(c or "").strip()


def project_cwd_from_key(project_key: str) -> str:
    """Decode percent-encoded session parent dir name (e.g. %2FUsers%2F…)."""
    try:
        return unquote(project_key)
    except Exception:
        return project_key


def load_summary_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    return data if isinstance(data, dict) else None


def last_user_from_chat_history(session_dir: Path, *, max_lines: int = 80) -> str:
    """Best-effort last non-trivial user text from chat_history.jsonl."""
    hist = session_dir / "chat_history.jsonl"
    if not hist.is_file():
        return ""
    try:
        lines = hist.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines[-max_lines:]):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        if str(o.get("type") or o.get("role") or "").lower() not in ("user", "human"):
            continue
        text = _text_from_content(o.get("content") or o.get("text") or o.get("message"))
        if not text:
            continue
        # skip system-reminder-only blobs
        if text.lstrip().startswith("<system-reminder") and "user_query" not in text:
            continue
        if len(text) > 400:
            text = text[:399] + "…"
        return text
    return ""


def _tail_file_lines(path: Path, *, max_bytes: int = 256_000, max_lines: int = 200) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
            raw = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return raw.splitlines()[-max_lines:]


def last_agent_snippet_from_updates(session_dir: Path, *, max_lines: int = 120) -> str:
    """Tail updates.jsonl for a short agent_message_chunk (resume context)."""
    upd = session_dir / "updates.jsonl"
    if not upd.is_file():
        return ""
    last = ""
    for line in _tail_file_lines(upd, max_bytes=64_000, max_lines=max_lines):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        params = o.get("params") if isinstance(o, dict) else None
        if not isinstance(params, dict):
            continue
        u = params.get("update")
        if not isinstance(u, dict):
            continue
        if u.get("sessionUpdate") != "agent_message_chunk":
            continue
        text = _text_from_content(u.get("content"))
        if text:
            last = text
    if len(last) > 240:
        last = last[:239] + "…"
    return last


def last_user_from_updates(session_dir: Path, *, max_lines: int = 200) -> str:
    """Tail updates.jsonl for last user_message_chunk (best abandoned-mission title)."""
    upd = session_dir / "updates.jsonl"
    if not upd.is_file():
        return ""
    last = ""
    for line in _tail_file_lines(upd, max_bytes=256_000, max_lines=max_lines):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        params = o.get("params") if isinstance(o, dict) else None
        if not isinstance(params, dict):
            continue
        u = params.get("update")
        if not isinstance(u, dict):
            continue
        if u.get("sessionUpdate") != "user_message_chunk":
            continue
        text = _text_from_content(u.get("content"))
        if text:
            last = text
    if len(last) > 240:
        last = last[:239] + "…"
    return last


def session_file_mtimes(session_dir: Path) -> tuple[float, float]:
    """(summary_mtime, updates_mtime) — 0 if missing."""
    d = Path(session_dir)
    s_m = u_m = 0.0
    try:
        s_m = (d / "summary.json").stat().st_mtime
    except OSError:
        pass
    try:
        u_m = (d / "updates.jsonl").stat().st_mtime
    except OSError:
        pass
    return s_m, u_m


def enrich_session_dir(
    session_dir: Path | str,
    *,
    deep: bool = True,
) -> GrokSessionIndex | None:
    """Load summary.json (+ optional transcript tails) for one session directory.

    deep=False: summary only (cheap dirty check / refresh of clean rows).
    deep=True: also tail chat_history / updates for last-user summary.
    """
    d = Path(session_dir)
    summary_path = d / "summary.json"
    data = load_summary_json(summary_path)
    if data is None:
        return None

    s_mtime, u_mtime = session_file_mtimes(d)

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    sid = str(info.get("id") or d.name)
    cwd = str(info.get("cwd") or "")
    project_key = d.parent.name if d.parent else ""
    project_cwd = project_cwd_from_key(project_key) if project_key else ""

    title = str(data.get("generated_title") or "").strip()
    summary = str(data.get("session_summary") or "").strip()
    sources = ["summary.json"] if summary_path.is_file() else []

    kind = str(data.get("session_kind") or "").strip() or None
    parent = str(data.get("parent_session_id") or "").strip() or None
    hidden = bool(data.get("hidden"))
    if not hidden and kind and kind.lower().startswith("subagent"):
        # Grok UI hides these by default when kind starts with subagent
        hidden = True

    last_user = ""
    if deep:
        if (d / "chat_history.jsonl").is_file():
            last_user = last_user_from_chat_history(d)
            if last_user:
                sources.append("chat_history.jsonl")
        if (d / "updates.jsonl").is_file():
            sources.append("updates.jsonl")
            if not last_user:
                last_user = last_user_from_updates(d)
            if not summary:
                snip = last_agent_snippet_from_updates(d)
                if snip:
                    summary = snip
        # Prefer last human prompt for control-room triage (over agent tail / title echo)
        if last_user:
            gen_title = str(data.get("generated_title") or "").strip()
            if not title or title == gen_title or title == summary:
                title = last_user[:100]
            summary = last_user[:240]

    if not title and last_user:
        title = last_user[:100]
    if not summary and last_user:
        summary = last_user[:240]

    mtime_iso = (
        data.get("last_active_at")
        or data.get("updated_at")
        or data.get("created_at")
    )
    if not isinstance(mtime_iso, str):
        mtime_iso = None

    msgs = data.get("num_messages")
    chat_msgs = data.get("num_chat_messages")
    try:
        msgs_i = int(msgs) if msgs is not None else None
    except (TypeError, ValueError):
        msgs_i = None
    try:
        chat_i = int(chat_msgs) if chat_msgs is not None else None
    except (TypeError, ValueError):
        chat_i = None

    return GrokSessionIndex(
        session_id=sid,
        cwd=cwd or project_cwd or "",
        path=str(d),
        title=title or sid[:12],
        summary=summary or title or sid[:12],
        model=str(data.get("current_model_id") or "") or None,
        branch=str(data.get("head_branch") or "") or None,
        messages=msgs_i,
        chat_messages=chat_i,
        mtime_iso=mtime_iso,
        last_active_at=str(data.get("last_active_at") or "") or None,
        agent_name=str(data.get("agent_name") or "") or None,
        project_cwd=project_cwd,
        last_user_snippet=last_user,
        session_kind=kind,
        parent_session_id=parent,
        hidden=hidden,
        summary_mtime=s_mtime,
        updates_mtime=u_mtime,
        source_files=sources,
    )


def iter_session_dirs(root: Path | None = None) -> Iterable[Path]:
    """Yield session directories that contain summary.json under sessions_root."""
    base = root or sessions_root()
    if not base.is_dir():
        return
    for summary in base.rglob("summary.json"):
        # skip lock siblings if any misnamed
        if summary.name != "summary.json":
            continue
        yield summary.parent


def mtime_from_index(idx: GrokSessionIndex, fallback_stat: Path | None = None) -> float:
    """Epoch seconds for age ranking — prefer content activity, then file watermarks."""
    for raw in (idx.last_active_at, idx.mtime_iso):
        ts = _iso_to_epoch(raw)
        if ts is not None:
            return ts
    if idx.activity_mtime > 0:
        return idx.activity_mtime
    if fallback_stat is not None:
        try:
            return fallback_stat.stat().st_mtime
        except OSError:
            pass
    return 0.0


# ---------------------------------------------------------------------------
# CLI command builders
# ---------------------------------------------------------------------------


def resume_argv(
    session_id: str,
    *,
    bin_path: str | None = None,
    cwd: str | None = None,
) -> list[str]:
    """Argv to open Grok TUI resumed on *session_id* (``grok --resume <id>``)."""
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id required")
    grok = bin_path or resolve_grok_bin() or "grok"
    argv = [grok, "--resume", sid]
    if cwd:
        argv[1:1] = ["--cwd", cwd]
    return argv


def resume_shell_command(
    session_id: str,
    *,
    bin_path: str | None = None,
    cwd: str | None = None,
    use_exec: bool = True,
) -> str:
    """Shell one-liner for fleet spawn / copy-paste resume."""
    import shlex

    argv = resume_argv(session_id, bin_path=bin_path, cwd=None)
    # Prefer cd + resume without --cwd so interactive cwd matches transcript.
    body = " ".join(shlex.quote(a) for a in argv)
    if use_exec:
        body = "exec " + body
    resume_cwd = (cwd or "").strip()
    if resume_cwd and resume_cwd not in (".", "~"):
        return f"cd {shlex.quote(resume_cwd)} && {body}"
    return body


def headless_steer_argv(
    prompt: str,
    *,
    session_id: str | None = None,
    continue_recent: bool = False,
    bin_path: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    output_format: str = "json",
    always_approve: bool = True,
) -> list[str]:
    """Argv for headless single-turn steer: ``grok -p "…"`` [``-r`` id | ``-c``].

    Does not run the process — caller decides subprocess / dry-run.
    Default output_format=json for structured fleet parsing.
    """
    text = (prompt or "").strip()
    if not text:
        raise ValueError("prompt required")
    grok = bin_path or resolve_grok_bin() or "grok"
    argv: list[str] = [grok, "-p", text, "--output-format", output_format]
    if always_approve:
        argv.append("--always-approve")
    if cwd:
        argv.extend(["--cwd", cwd])
    if model:
        argv.extend(["-m", model])
    sid = (session_id or "").strip()
    if sid:
        argv.extend(["-r", sid])
    elif continue_recent:
        argv.append("-c")
    return argv


def run_headless_steer(
    prompt: str,
    *,
    session_id: str | None = None,
    continue_recent: bool = False,
    bin_path: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    timeout: float = 120.0,
    always_approve: bool = True,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run one headless Grok turn; return structured result (no tmux).

    Uses ``grok -p`` with ``--output-format json``. Does not speak ACP — that is
    Phase C. Suitable for room SEND when the operator prefers protocol-ish
    control over paste-into-TUI.
    """
    import subprocess
    import time as _time

    t0 = _time.time()
    try:
        argv = headless_steer_argv(
            prompt,
            session_id=session_id,
            continue_recent=continue_recent,
            bin_path=bin_path,
            cwd=cwd,
            model=model,
            output_format="json",
            always_approve=always_approve,
        )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "mode": "headless"}

    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    # launchd-safe: ensure ~/.grok/bin on PATH for child tools
    home = Path.home()
    extra = str(home / ".grok" / "bin")
    run_env["PATH"] = extra + os.pathsep + (run_env.get("PATH") or "")

    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=max(5.0, float(timeout)),
            cwd=cwd if cwd and Path(cwd).is_dir() else None,
            env=run_env,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "grok_not_found",
            "mode": "headless",
            "argv": argv,
            "hint": "set EMUX_GROK_BIN to absolute path of grok",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": "timeout",
            "mode": "headless",
            "timeout": timeout,
            "argv": argv,
            "partial_stdout": (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else "",
        }
    except OSError as exc:
        return {"ok": False, "error": f"spawn_failed:{exc}", "mode": "headless", "argv": argv}

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    parsed: Any = None
    text = out
    if out:
        try:
            parsed = json.loads(out)
            if isinstance(parsed, dict):
                # common shapes: {text|result|message|output}
                for k in ("text", "result", "message", "output", "content", "response"):
                    if isinstance(parsed.get(k), str) and parsed[k].strip():
                        text = parsed[k].strip()
                        break
        except json.JSONDecodeError:
            parsed = None
            text = out

    ok = proc.returncode == 0
    return {
        "ok": ok,
        "mode": "headless",
        "exit_code": proc.returncode,
        "text": text[:8000] if text else "",
        "json": parsed if isinstance(parsed, (dict, list)) else None,
        "stderr": err[:2000],
        "argv": argv,
        "session_id": (session_id or "").strip() or None,
        "cwd": cwd,
        "elapsed_ms": int((_time.time() - t0) * 1000),
        "error": None if ok else (err.splitlines()[-1] if err else f"exit_{proc.returncode}"),
    }


def acp_stdio_argv(
    *,
    bin_path: str | None = None,
    model: str | None = None,
    always_approve: bool = False,
    leader: bool | None = None,
    leader_socket: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Command list for a future ACP client: ``grok agent stdio`` [options].

    Full agent-client-protocol handshake is not implemented here — only the
    process argv so callers can spawn and speak JSON-RPC on the pipes later.

    Shape: ``grok agent [agent-opts] stdio [stdio-opts]`` matching ``grok agent
    --help`` (model / always-approve / leader on ``agent``; leader-socket also
    on ``stdio``).
    """
    grok = bin_path or resolve_grok_bin() or "grok"
    argv: list[str] = [grok, "agent"]
    if model:
        argv.extend(["-m", model])
    if always_approve:
        argv.append("--always-approve")
    if leader is True:
        argv.append("--leader")
    elif leader is False:
        argv.append("--no-leader")
    argv.append("stdio")
    if leader_socket:
        argv.extend(["--leader-socket", leader_socket])
    if extra:
        argv.extend(extra)
    return argv


# ---------------------------------------------------------------------------
# Hooks bridge (file-based)
# ---------------------------------------------------------------------------


def emux_hook_command(python: str | None = None, module: str = "emux.hook_delegation") -> str:
    """Shell command string for a Grok hook entry pointing at emux."""
    py = python or "python3"
    return f"{py} -m {module}"


def hook_bridge_payload(
    *,
    command: str | None = None,
    events: Iterable[str] = ("PreToolUse", "Stop"),
    timeout: int = 30,
) -> dict[str, Any]:
    """Grok-style hooks JSON body (same schema as ~/.grok/hooks/*.json)."""
    cmd = command or emux_hook_command()
    hooks: dict[str, Any] = {}
    for event in events:
        hooks[event] = [
            {
                "hooks": [
                    {"type": "command", "command": cmd, "timeout": timeout},
                ]
            }
        ]
    return {"hooks": hooks}


def write_hooks_bridge(
    *,
    scope: str = "global",
    project_dir: str | Path | None = None,
    home: Path | None = None,
    name: str = _DEFAULT_HOOK_NAME,
    command: str | None = None,
    events: Iterable[str] = ("PreToolUse", "Stop"),
    dry_run: bool = False,
) -> dict[str, Any]:
    """Write (or plan) an emux bridge hook JSON for Grok.

    * ``scope=global`` → ``~/.grok/hooks/<name>``
    * ``scope=project`` → ``<project>/.grok/hooks/<name>`` (needs folder trust)

    Does not enable trust or start Grok. Returns path + payload for logging.
    """
    if scope == "project":
        if not project_dir:
            raise ValueError("project_dir required for scope=project")
        dest_dir = hooks_project_dir(project_dir)
    elif scope == "global":
        dest_dir = hooks_global_dir(home)
    else:
        raise ValueError("scope must be 'global' or 'project'")

    dest = dest_dir / name
    payload = hook_bridge_payload(command=command, events=events)
    result: dict[str, Any] = {
        "ok": True,
        "scope": scope,
        "path": str(dest),
        "payload": payload,
        "written": False,
        "note": (
            "Grok loads hooks from ~/.grok/hooks/*.json (global, always trusted) "
            "or <project>/.grok/hooks/*.json (requires /hooks-trust). "
            "PreToolUse can deny tools; Stop can block turn completion. "
            "emux.hook_delegation currently speaks Claude Code stdin JSON — "
            "adapt adapters before relying on deny semantics with Grok."
        ),
    }
    if dry_run:
        return result
    dest_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    tmp = dest.with_suffix(dest.suffix + ".emux-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dest)
    result["written"] = True
    return result


def is_session_id(value: str) -> bool:
    return bool(value and _UUID_RE.match(value.strip()))
