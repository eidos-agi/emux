"""In-process cron schedule for emux — fire messages into sessions.

Package: **croniter** parses standard 5-field cron expressions and next/prev fire
times. We own the tick (web daemon poll loop) and the actuator (tmux_send).

Why not APScheduler: also fine for in-process cron, but heavier. v1 only needs
"next/prev from expression + fire message" — croniter is enough.

Storage (product-scoped):
  ~/.config/<product>/schedule.json   (amux → ~/.config/amux/schedule.json)

Job shape::

    {
      "id": "iran-daily",
      "cron": "0 7 * * *",
      "timezone": "America/Chicago",
      "target": "northstar-iran-daily",
      "message": "…prompt text…",
      "enabled": true,
      "last_run_at": null,
      "last_status": null,
      "last_error": null
    }

On add, last_run_at is set to now so the first fire is the *next* cron tick,
not a catch-up of history.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_lock = threading.RLock()

# How late a fire may be and still run (daemon slept / laptop lid).
_CATCHUP_SECONDS = 15 * 60


def _product_id() -> str:
    try:
        from .product_config import _product_id as pid

        return pid()
    except Exception:
        env = (os.environ.get("EMUX_PRODUCT") or os.environ.get("EMUX_SKIN") or "emux").strip()
        return env.lower() or "emux"


def config_dir() -> Path:
    try:
        from .product_config import config_dir_for

        return config_dir_for(_product_id())
    except Exception:
        p = _product_id()
        return Path.home() / ".config" / (p if p else "emux")


def schedule_path() -> Path:
    return config_dir() / "schedule.json"


def log_path() -> Path:
    return config_dir() / "schedule-log.jsonl"


@dataclass
class Job:
    id: str
    cron: str
    target: str
    message: str
    timezone: str = "America/Chicago"
    enabled: bool = True
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: str = field(default_factory=lambda: _utc_now_iso())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d
    except ValueError:
        return None


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def validate_cron(expr: str) -> None:
    """Raise ValueError if expr is not a valid 5-field cron string."""
    try:
        from croniter import croniter
    except ImportError as e:
        raise RuntimeError(
            "croniter is required for emux schedule — pip/uv install croniter"
        ) from e
    if not croniter.is_valid(expr):
        raise ValueError(f"invalid cron expression: {expr!r}")


def load_jobs() -> list[Job]:
    path = schedule_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items = raw.get("jobs") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out: list[Job] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("id") or "").strip()
        cron = str(item.get("cron") or "").strip()
        target = str(item.get("target") or "").strip()
        message = str(item.get("message") or "")
        if not jid or not cron or not target:
            continue
        out.append(
            Job(
                id=jid,
                cron=cron,
                target=target,
                message=message,
                timezone=str(item.get("timezone") or "America/Chicago"),
                enabled=bool(item.get("enabled", True)),
                last_run_at=item.get("last_run_at"),
                last_status=item.get("last_status"),
                last_error=item.get("last_error"),
                created_at=str(item.get("created_at") or _utc_now_iso()),
            )
        )
    return out


def save_jobs(jobs: list[Job]) -> Path:
    path = schedule_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "product": _product_id(),
        "jobs": [j.as_dict() for j in jobs],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def list_jobs(*, with_next: bool = True) -> list[dict[str, Any]]:
    jobs = load_jobs()
    now_utc = datetime.now(UTC)
    rows: list[dict[str, Any]] = []
    for j in jobs:
        row = j.as_dict()
        if with_next and j.enabled:
            try:
                row["next_run_at"] = _next_fire_iso(j, now_utc)
            except Exception as e:
                row["next_run_at"] = None
                row["next_error"] = str(e)
        else:
            row["next_run_at"] = None
        rows.append(row)
    return rows


def add_job(
    *,
    cron: str,
    target: str,
    message: str,
    timezone: str = "America/Chicago",
    job_id: str | None = None,
    enabled: bool = True,
) -> Job:
    validate_cron(cron)
    _ = _tz(timezone)  # fail early on bad tz
    jid = (job_id or "").strip() or f"job-{uuid.uuid4().hex[:8]}"
    with _lock:
        jobs = load_jobs()
        if any(j.id == jid for j in jobs):
            raise ValueError(f"job id already exists: {jid}")
        job = Job(
            id=jid,
            cron=cron.strip(),
            target=target.strip(),
            message=message,
            timezone=timezone,
            enabled=enabled,
            # Seed so we do not catch up the previous cron slot on add.
            last_run_at=_utc_now_iso(),
            last_status="added",
        )
        jobs.append(job)
        save_jobs(jobs)
        return job


def remove_job(job_id: str) -> bool:
    with _lock:
        jobs = load_jobs()
        n = len(jobs)
        jobs = [j for j in jobs if j.id != job_id]
        if len(jobs) == n:
            return False
        save_jobs(jobs)
        return True


def set_enabled(job_id: str, enabled: bool) -> Job | None:
    with _lock:
        jobs = load_jobs()
        for j in jobs:
            if j.id == job_id:
                j.enabled = enabled
                save_jobs(jobs)
                return j
        return None


def _next_fire_iso(job: Job, now_utc: datetime) -> str:
    from croniter import croniter

    tz = _tz(job.timezone)
    now_local = now_utc.astimezone(tz)
    itr = croniter(job.cron, now_local)
    nxt = itr.get_next(datetime)
    if nxt.tzinfo is None:
        nxt = nxt.replace(tzinfo=tz)
    return nxt.astimezone(UTC).replace(microsecond=0).isoformat()


def _prev_fire(job: Job, now_utc: datetime) -> datetime:
    from croniter import croniter

    tz = _tz(job.timezone)
    now_local = now_utc.astimezone(tz)
    itr = croniter(job.cron, now_local)
    prev = itr.get_prev(datetime)
    if prev.tzinfo is None:
        prev = prev.replace(tzinfo=tz)
    return prev.astimezone(UTC)


def is_due(job: Job, now_utc: datetime | None = None) -> bool:
    if not job.enabled:
        return False
    now = now_utc or datetime.now(UTC)
    try:
        prev = _prev_fire(job, now)
    except Exception:
        return False
    last = _parse_iso(job.last_run_at)
    if last is not None and prev <= last:
        return False
    # Too late to catch up (daemon was down for hours) — skip and advance marker.
    if (now - prev).total_seconds() > _CATCHUP_SECONDS:
        return False
    return True


def _log_fire(rec: dict[str, Any]) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def fire_job(job: Job, *, force: bool = False) -> dict[str, Any]:
    """Send job.message into job.target (registry name preferred)."""
    from .server import tmux_send

    result: dict[str, Any] = {
        "ok": False,
        "id": job.id,
        "target": job.target,
        "at": _utc_now_iso(),
    }
    try:
        # Prefer registry name; fall back to raw tmux session.
        send = __import__("asyncio").run(
            tmux_send(job.target, job.message, enter=True, by_registry_name=True)
        )
        if not send.get("ok") and send.get("error") in (
            "not_registered",
            "unknown_name",
            "session_not_found",
        ):
            send = __import__("asyncio").run(
                tmux_send(job.target, job.message, enter=True, by_registry_name=False)
            )
        result.update(send)
        result["ok"] = bool(send.get("ok"))
        if not result["ok"]:
            result["error"] = send.get("error") or "send_failed"
    except Exception as e:
        result["error"] = str(e)

    with _lock:
        jobs = load_jobs()
        for j in jobs:
            if j.id == job.id:
                j.last_run_at = _utc_now_iso()
                j.last_status = "ok" if result.get("ok") else "error"
                j.last_error = None if result.get("ok") else str(result.get("error") or "")
                break
        save_jobs(jobs)

    _log_fire({**result, "force": force, "cron": job.cron})
    return result


def fire_by_id(job_id: str, *, force: bool = True) -> dict[str, Any]:
    jobs = load_jobs()
    for j in jobs:
        if j.id == job_id:
            return fire_job(j, force=force)
    return {"ok": False, "error": "unknown_job", "id": job_id}


def tick_once() -> list[dict[str, Any]]:
    """Check due jobs and fire them. Called from the web daemon poll loop."""
    now = datetime.now(UTC)
    fired: list[dict[str, Any]] = []
    with _lock:
        jobs = load_jobs()
        due_ids = [j.id for j in jobs if is_due(j, now)]
    for jid in due_ids:
        # Re-load each fire so last_run updates serialize cleanly.
        jobs = load_jobs()
        job = next((j for j in jobs if j.id == jid), None)
        if job is None:
            continue
        # Re-check due (another tick may have won).
        if not is_due(job, datetime.now(UTC)):
            continue
        fired.append(fire_job(job, force=False))
    # Advance markers for jobs that missed the catch-up window so they don't
    # stick forever as "due" once the laptop wakes after a long sleep.
    with _lock:
        jobs = load_jobs()
        changed = False
        now2 = datetime.now(UTC)
        for j in jobs:
            if not j.enabled:
                continue
            try:
                prev = _prev_fire(j, now2)
            except Exception:
                continue
            last = _parse_iso(j.last_run_at)
            if last is not None and prev <= last:
                continue
            if (now2 - prev).total_seconds() > _CATCHUP_SECONDS:
                j.last_run_at = prev.isoformat()
                j.last_status = "skipped_late"
                j.last_error = f"missed by >{_CATCHUP_SECONDS}s"
                changed = True
        if changed:
            save_jobs(jobs)
    return fired
