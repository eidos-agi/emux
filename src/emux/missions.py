"""Mission briefs for `emux new` / product `*mux new`.

Broken old contract: embed the full task in `claude 'long prompt…'`.
That fails on quoting, length, and send-keys fragility.

Correct contract:
  1. Write a markdown brief under ~/.config/<product>/missions/
  2. Append one durable jsonl line under ~/.config/<product>/logs/missions.jsonl
  3. Launch the agent with a short "load this file" instruction

The brief (and the log entry pointing at it) is what the fired-up agent consults.
Product-scoped so amux / gmux / reevux / directrux keep separate mission ledgers.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import uuid
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


def config_dir(product: str | None = None) -> Path:
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


def missions_dir(product: str | None = None) -> Path:
    d = config_dir(product) / "missions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def missions_log_path(product: str | None = None) -> Path:
    """Durable consult log — one jsonl line per mission under product logs/."""
    logs = config_dir(product) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "missions.jsonl"


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "mission").lower()).strip("-")
    return (s or "mission")[:48]


def _agent_bin(command: str | None) -> str:
    """First token of a plan command, or 'claude' default for empty."""
    first = ((command or "").strip().split() or ["claude"])[0]
    return first or "claude"


def is_agent_command(command: str | None) -> bool:
    """True when the launch target is an interactive agent that should load a brief."""
    bin_name = Path(_agent_bin(command)).name.lower()
    if bin_name in {"claude", "grok", "codex", "cursor-agent", "aider"}:
        return True
    try:
        from . import adapters

        return adapters.detect(bin_name) is not None
    except Exception:
        return False


def render_brief(
    *,
    mission_id: str,
    name: str,
    summary: str,
    intent: str,
    host: str | None,
    cwd: str | None,
    permission_mode: str | None,
    product: str,
    log_path: str,
    transcript: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Markdown the agent must load — full mission source of truth."""
    lines = [
        f"# Mission: {name}",
        "",
        f"- **id:** `{mission_id}`",
        f"- **product:** `{product}`",
        f"- **session:** `{name}`",
        f"- **host:** `{host or 'local'}`",
        f"- **cwd:** `{cwd or '(default)'}`",
        f"- **permission_mode:** `{permission_mode or 'default'}`",
        f"- **log:** `{log_path}`",
        "",
        "## Summary",
        "",
        (summary or intent or "(no summary)").strip(),
        "",
        "## What to do",
        "",
        (intent or summary or "(no intent recorded)").strip(),
        "",
        "## Operating rules",
        "",
        "1. This file is the mission source of truth for this session.",
        "2. Do the work described above. Do not invent a different mission.",
        "3. Prefer autonomous progress; surface blockers clearly.",
        "4. When done, leave a short status in the session (what shipped / what's blocked).",
        "",
    ]
    if transcript:
        lines.extend(["## Planning transcript", ""])
        for row in transcript:
            lines.append(f"- {row}")
        lines.append("")
    if extra:
        lines.extend(["## Extra", "", "```json", json.dumps(extra, indent=2, default=str), "```", ""])
    return "\n".join(lines)


def kickstart_prompt(*, brief_path: Path, log_path: Path, mission_id: str) -> str:
    """Short instruction only — never the full mission body."""
    return (
        f"Read and execute the mission brief at {brief_path}. "
        f"That markdown file is the full mission (id={mission_id}). "
        f"The durable system record is the latest matching entry in {log_path}. "
        "Do not invent a different task."
    )


def launch_command(
    *,
    agent: str,
    brief_path: Path,
    log_path: Path,
    mission_id: str,
    permission_mode: str | None = None,
) -> str:
    """Shell command that starts the agent ON the brief (path only in argv)."""
    prompt = kickstart_prompt(brief_path=brief_path, log_path=log_path, mission_id=mission_id)
    cmd = f"{agent} {shlex.quote(prompt)}"
    mode = (permission_mode or "").strip()
    if (
        mode
        and mode != "default"
        and mode in {"acceptEdits", "bypassPermissions"}
        and Path(agent).name == "claude"
        and "--permission-mode" not in cmd
    ):
        cmd = f"{cmd} --permission-mode {mode}"
    return cmd


def register_mission(
    *,
    name: str,
    summary: str = "",
    intent: str = "",
    host: str | None = None,
    cwd: str | None = None,
    permission_mode: str | None = None,
    command: str | None = None,
    product: str | None = None,
    transcript: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write brief.md + append missions.jsonl. Returns paths + launch command.

    For non-agent commands (e.g. htop), still records the mission but leaves
    `launch_command` as the original command string.
    """
    pid = (product or product_id()).strip().lower() or "emux"
    mission_id = uuid.uuid4().hex[:12]
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    brief_name = f"{stamp}-{_slug(name)}-{mission_id}.md"
    brief_path = missions_dir(pid) / brief_name
    log_path = missions_log_path(pid)

    body = render_brief(
        mission_id=mission_id,
        name=name,
        summary=summary,
        intent=intent,
        host=host,
        cwd=cwd,
        permission_mode=permission_mode,
        product=pid,
        log_path=str(log_path),
        transcript=transcript,
        extra=extra,
    )
    brief_path.write_text(body, encoding="utf-8")

    agent = _agent_bin(command)
    agentish = is_agent_command(command if command is not None else agent)
    if agentish:
        launch = launch_command(
            agent=agent,
            brief_path=brief_path,
            log_path=log_path,
            mission_id=mission_id,
            permission_mode=permission_mode,
        )
    else:
        launch = (command or "").strip()

    record: dict[str, Any] = {
        "id": mission_id,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "product": pid,
        "name": name,
        "summary": summary,
        "intent": intent,
        "host": host,
        "cwd": cwd,
        "permission_mode": permission_mode or "default",
        "brief_path": str(brief_path),
        "log_path": str(log_path),
        "agent": agent if agentish else None,
        "launch_command": launch,
        "kind": "agent" if agentish else "shell",
    }
    if extra:
        record["extra"] = extra

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    return {
        "ok": True,
        "id": mission_id,
        "product": pid,
        "brief_path": str(brief_path),
        "log_path": str(log_path),
        "launch_command": launch,
        "record": record,
        "agentish": agentish,
    }


def intent_from_transcript(transcript: list[str] | None) -> str:
    """Best-effort first user ask from the planner transcript."""
    if not transcript:
        return ""
    for row in transcript:
        s = (row or "").strip()
        if s.lower().startswith("user:"):
            return s.split(":", 1)[1].strip()
    # fall back to first line
    return (transcript[0] or "").strip()
