"""Tiered, topic-scoped memory over emux's existing registry and logs."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(os.environ.get("EMUX_CONFIG") or (Path.home() / ".config" / "emux"))
_STATE_DIR = Path(os.environ.get("EMUX_STATE") or (Path.home() / ".local" / "state" / "emux"))
CHANNELS_PATH = _CONFIG_DIR / "channels.json"
CHANNEL_OKF_DIR = _CONFIG_DIR / "channel-okf"
CHANNEL_NOTES_DIR = _STATE_DIR / "channels"
CHANNEL_INFORMED_PATH = _STATE_DIR / "channel_informed.json"

_GENERIC_TAGS = {
    "agent",
    "claude",
    "codex",
    "hermes",
    "local",
    "remote",
    "worker",
    "manager",
    "mission",
    "adopted",
    "shell",
    "tmux",
    "emux",
}
NOTE_KINDS = frozenset({"decision", "outcome", "failure", "policy", "fact"})
_SENSITIVE = re.compile(
    r"(?i)\b(item(?:_id)?|account(?:_id|_number| number)?|token(?:_reference)?)"
    r"\s*[:=]?\s*[A-Za-z0-9_-]{8,}|\b\d{12,19}\b"
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def load_channels() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(CHANNELS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_channels(channels: dict[str, dict[str, Any]]) -> None:
    CHANNELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CHANNELS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(channels, indent=2, sort_keys=True) + "\n")
    tmp.replace(CHANNELS_PATH)


def _write_okf(channel: str, spec: dict[str, Any]) -> Path:
    """Materialize one channel as the smallest useful OKF v0.1 bundle."""
    bundle = CHANNEL_OKF_DIR / _slug(channel)
    bundle.mkdir(parents=True, exist_ok=True)
    title = channel.replace("-", " ").title()
    created = dt.datetime.fromtimestamp(spec["created_at"], dt.UTC).isoformat()
    parent = spec.get("parent") or "none"
    matchers = ", ".join(spec.get("matchers") or []) or "none"
    tags = json.dumps(["emux", "channel", f"tier-{spec['tier']}"])
    (bundle / "index.md").write_text(
        '---\nokf_version: "0.1"\n---\n\n'
        f"# {title} Channel\n\n{spec.get('description') or ''}\n\n"
        "## Concepts\n\n* [Channel contract](channel.md)\n"
        "* [Learning log](log.md)\n"
    )
    (bundle / "channel.md").write_text(
        "---\n"
        "type: emux-channel\n"
        f"title: {json.dumps(title + ' Channel')}\n"
        f"description: {json.dumps(spec.get('description') or '')}\n"
        f"resource: {json.dumps('emux://channel/' + channel)}\n"
        f"tags: {tags}\n"
        f"timestamp: {json.dumps(created)}\n"
        f"emux_tier: {spec['tier']}\n"
        f"emux_parent: {json.dumps(parent)}\n"
        "---\n\n"
        f"# {title} Channel\n\n{spec.get('description') or ''}\n\n"
        f"- Parent: `{parent}`\n- Matchers: `{matchers}`\n"
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    # ponytail: cap exported history; paginate concepts if a channel exceeds 10k learnings.
    for note in _notes(channel, limit=10_000):
        date = dt.datetime.fromtimestamp(note["t"], dt.UTC).date().isoformat()
        grouped.setdefault(date, []).append(note)
    lines = ["# Channel Learning Log"]
    for date in sorted(grouped, reverse=True):
        lines.extend(["", f"## {date}"])
        for note in grouped[date]:
            source_text = " ".join(str(note.get("source") or "").split())
            source = f" [{source_text}]" if source_text else ""
            text = " ".join(str(note["text"]).split())
            lines.append(f"* **{note['kind']}**{source}: {text}")
    (bundle / "log.md").write_text("\n".join(lines) + "\n")
    return bundle


def refresh_okf() -> list[Path]:
    return [_write_okf(name, spec) for name, spec in load_channels().items()]


def create_channel(
    name: str,
    tier: int,
    description: str,
    parent: str | None = None,
    matchers: list[str] | None = None,
) -> dict[str, Any]:
    """Create one channel manifest. Tier 0=canon, 1=domain, 2=workstream, 3=mission."""
    slug = _slug(name)
    if not slug:
        raise ValueError("channel name must contain a letter or number")
    if tier not in range(4):
        raise ValueError("tier must be 0, 1, 2, or 3")
    channels = load_channels()
    if slug in channels:
        raise ValueError(f"channel already exists: {slug}")
    parent = _slug(parent) if parent else None
    if parent and parent not in channels:
        raise ValueError(f"parent channel does not exist: {parent}")
    entry = {
        "tier": tier,
        "description": description.strip(),
        "parent": parent,
        "matchers": sorted({_slug(m) for m in (matchers or [slug]) if _slug(m)}),
        "created_at": int(time.time()),
    }
    channels[slug] = entry
    _save_channels(channels)
    _write_okf(slug, entry)
    return {"name": slug, **entry}


def _haystack(name: str, entry: dict[str, Any]) -> str:
    linear = entry.get("linear") or {}
    values = [
        name,
        str(entry.get("session") or ""),
        str(entry.get("description") or ""),
        " ".join(str(x) for x in entry.get("tags") or []),
        " ".join(str(x) for x in entry.get("manages") or []),
        str(entry.get("cwd") or ""),
        str(entry.get("command") or ""),
        str(linear.get("issue") or ""),
        str(linear.get("project") or ""),
        str(linear.get("team") or ""),
    ]
    return _slug(" ".join(values))


def resolve_channels(
    name: str,
    entry: dict[str, Any],
    definitions: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Explicit channels + deterministic matcher hits + canon, with parents inherited."""
    definitions = definitions if definitions is not None else load_channels()
    # Explicit channels survive even without a definition — remote targeting
    # (emux-remote/1.0) matches on entry channels, so silently dropping an
    # explicit channel makes the session unreachable (EID-869).
    found = set(entry.get("channels") or [])
    text = _haystack(name, entry)
    for channel, spec in definitions.items():
        if spec.get("tier") == 0 or any(m in text for m in spec.get("matchers") or []):
            found.add(channel)
    pending = list(found)
    while pending:
        parent = (definitions.get(pending.pop()) or {}).get("parent")
        if parent and parent in definitions and parent not in found:
            found.add(parent)
            pending.append(parent)
    return sorted(found, key=lambda c: (definitions.get(c, {}).get("tier", 9), c))


def suggest_channels(registry: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Suggest a domain channel after the same meaningful tag appears twice."""
    existing = load_channels()
    counts = Counter(
        _slug(str(tag))
        for entry in registry.values()
        for tag in (entry.get("tags") or [])
        if _slug(str(tag)) and _slug(str(tag)) not in _GENERIC_TAGS
    )
    return [
        {"name": tag, "tier": 1, "reason": f"tag appears on {count} sessions"}
        for tag, count in sorted(counts.items())
        if count >= 2 and tag not in existing
    ]


def refresh_registry(registry: dict[str, dict[str, Any]]) -> list[str]:
    """Backfill matcher-derived channel tags; return names whose tags changed."""
    changed = []
    definitions = load_channels()
    for name, entry in registry.items():
        resolved = resolve_channels(name, entry, definitions)
        if entry.get("channels") != resolved:
            entry["channels"] = resolved
            changed.append(name)
    return changed


def append_note(channel: str, kind: str, text: str, source: str | None = None) -> dict[str, Any]:
    definitions = load_channels()
    if channel not in definitions:
        raise ValueError(f"unknown channel: {channel}")
    if kind not in NOTE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(NOTE_KINDS))}")
    rec = {
        "t": int(time.time()),
        "kind": kind,
        "text": _SENSITIVE.sub(lambda m: f"{m.group(1) or ''} [REDACTED]".strip(), text.strip())[
            :2000
        ],
        "source": source,
    }
    CHANNEL_NOTES_DIR.mkdir(parents=True, exist_ok=True)
    with (CHANNEL_NOTES_DIR / f"{_slug(channel)}.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    _write_okf(channel, definitions[channel])
    return rec


def _notes(channel: str, limit: int = 8) -> list[dict[str, Any]]:
    path = CHANNEL_NOTES_DIR / f"{_slug(channel)}.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def channel_context(
    channel: str,
    registry: dict[str, dict[str, Any]],
    definitions: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    definitions = definitions if definitions is not None else load_channels()
    if channel not in definitions:
        raise ValueError(f"unknown channel: {channel}")
    members = [
        name
        for name, entry in registry.items()
        if channel in resolve_channels(name, entry, definitions)
    ]
    return {
        "name": channel,
        **definitions[channel],
        "okf": str(CHANNEL_OKF_DIR / channel / "index.md"),
        "sessions": members,
        "notes": _notes(channel),
    }


def session_context(name: str, registry: dict[str, dict[str, Any]]) -> str:
    entry = registry.get(name)
    if not entry:
        return ""
    definitions = load_channels()
    names = resolve_channels(name, entry, definitions)
    linear = entry.get("linear") or {}
    if not names and not linear.get("issue"):
        return ""
    lines = []
    if linear.get("issue"):
        detail = ", ".join(f"{key}={linear[key]}" for key in ("team", "project") if linear.get(key))
        suffix = f" ({detail})" if detail else ""
        lines.extend(
            [
                "LINEAR WORK CONTRACT (Linear is the system of record):",
                f"- issue={linear['issue']}{suffix}",
            ]
        )
        for i, criterion in enumerate(linear.get("acceptance") or [], 1):
            lines.append(f"- acceptance {i}: {criterion}")
        lines.append(
            "Emit PROGRESS/NEED/DONE through emux. DONE is a worker claim, not closure; "
            "the manager records acceptance evidence and may recommend In Review."
        )
    if names:
        lines.append("EMUX CHANNEL CONTEXT (durable operational memory; newer evidence wins):")
    for channel in names:
        spec = definitions[channel]
        lines.append(f"- T{spec['tier']} {channel}: {spec.get('description') or ''}")
        lines.append(f"  OKF v0.1: {CHANNEL_OKF_DIR / channel / 'index.md'}")
        for note in _notes(channel, limit=4):
            source = f" [{note['source']}]" if note.get("source") else ""
            lines.append(f"  {note['kind']}{source}: {note['text']}")
    if names:
        lines.append(
            "Use this context before rediscovering history. Record a durable result with "
            "`emux channel note <channel> <decision|outcome|failure|policy|fact> <text> --source <session>`."
        )
    return "\n".join(lines)


def agent_prelude_once(name: str, registry: dict[str, dict[str, Any]]) -> str:
    """Return changed channel context once per session; unchanged calls cost zero tokens."""
    context = session_context(name, registry)
    if not context:
        return ""
    signature = hashlib.sha256(context.encode()).hexdigest()
    try:
        informed = json.loads(CHANNEL_INFORMED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        informed = {}
    if informed.get(name) == signature:
        return ""
    informed[name] = signature
    CHANNEL_INFORMED_PATH.parent.mkdir(parents=True, exist_ok=True)
    CHANNEL_INFORMED_PATH.write_text(json.dumps(informed, sort_keys=True) + "\n")
    return context
