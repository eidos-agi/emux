"""Unit tests for emux.schedule (cron message jobs)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from emux import schedule as sched


@pytest.fixture()
def sched_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("EMUX_PRODUCT", "testprod")
    # Force config dir into tmp
    monkeypatch.setattr(sched, "config_dir", lambda: tmp_path)
    return tmp_path


def test_validate_cron_ok():
    sched.validate_cron("0 7 * * *")


def test_validate_cron_bad():
    with pytest.raises(ValueError):
        sched.validate_cron("not a cron")


def test_add_list_remove(sched_dir: Path):
    j = sched.add_job(
        cron="0 7 * * *",
        target="demo-seat",
        message="hello desk",
        timezone="UTC",
        job_id="demo",
    )
    assert j.id == "demo"
    assert (sched_dir / "schedule.json").is_file()
    rows = sched.list_jobs()
    assert len(rows) == 1
    assert rows[0]["id"] == "demo"
    assert rows[0]["next_run_at"]
    assert sched.remove_job("demo") is True
    assert sched.list_jobs() == []


def test_is_due_respects_last_run(sched_dir: Path):
    j = sched.add_job(
        cron="* * * * *",  # every minute
        target="x",
        message="m",
        timezone="UTC",
        job_id="minutely",
    )
    # just added → last_run_at = now → not due for previous minute
    assert sched.is_due(j) is False
    # pretend we never ran and previous slot is recent
    j.last_run_at = None
    now = datetime.now(timezone.utc)
    # force last far in past
    j.last_run_at = (now - timedelta(hours=2)).isoformat()
    # previous minute is within catchup? every minute prev is <60s ago so due
    # but last is 2h ago and prev is ~now-1min so prev > last → due
    assert sched.is_due(j, now) is True


def test_skipped_late_not_due(sched_dir: Path):
    j = sched.Job(
        id="old",
        cron="0 0 1 1 *",  # once a year Jan 1
        target="x",
        message="m",
        timezone="UTC",
        enabled=True,
        last_run_at=None,
    )
    # If "now" is mid-year, prev is months ago → not due (outside catchup)
    mid = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    assert sched.is_due(j, mid) is False
