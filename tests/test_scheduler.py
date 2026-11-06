"""Scheduler due-time logic: interval jobs, daily jobs and weekday jobs."""

from __future__ import annotations

from datetime import datetime, timedelta

from radar.db import Database
from radar.scheduler import JobSpec, Scheduler, is_due
from radar.util import IST

MONDAY = datetime(2026, 11, 2, 12, 0, tzinfo=IST)
WEDNESDAY = datetime(2026, 11, 4, 12, 0, tzinfo=IST)


def set_last(db: Database, name: str, when: datetime) -> None:
    stamp = when.isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO job_runs(name, last_run_at, last_status, run_count, updated_at) VALUES (?,?,?,1,?) "
        "ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at, updated_at=excluded.updated_at",
        (name, stamp, "ok", stamp),
    )


def test_interval_job_runs_on_first_cycle(db: Database) -> None:
    spec = JobSpec("ingest", interval_minutes=5)
    assert is_due(db, spec, now=WEDNESDAY) is True


def test_interval_job_waits_for_its_interval(db: Database) -> None:
    spec = JobSpec("ingest", interval_minutes=5)
    set_last(db, "ingest", WEDNESDAY - timedelta(minutes=2))
    assert is_due(db, spec, now=WEDNESDAY) is False
    set_last(db, "ingest", WEDNESDAY - timedelta(minutes=6))
    assert is_due(db, spec, now=WEDNESDAY) is True


def test_daily_job_waits_for_scheduled_time(db: Database) -> None:
    spec = JobSpec("digest", daily_at=(8, 0))
    early = WEDNESDAY.replace(hour=7, minute=30)
    assert is_due(db, spec, now=early) is False


def test_daily_job_runs_once_per_day(db: Database) -> None:
    spec = JobSpec("digest", daily_at=(8, 0))
    now = WEDNESDAY.replace(hour=8, minute=5)
    assert is_due(db, spec, now=now) is True
    set_last(db, "digest", now)
    assert is_due(db, spec, now=now + timedelta(minutes=10)) is False
    set_last(db, "digest", now - timedelta(days=1))
    assert is_due(db, spec, now=now) is True


def test_weekly_job_only_runs_on_its_weekday(db: Database) -> None:
    spec = JobSpec("weekly", daily_at=(9, 0), weekday=0)  # Monday
    assert is_due(db, spec, now=WEDNESDAY.replace(hour=10)) is False
    monday = MONDAY.replace(hour=9, minute=30)
    assert is_due(db, spec, now=monday) is True
    set_last(db, "weekly", monday)
    assert is_due(db, spec, now=monday + timedelta(minutes=10)) is False


def test_due_jobs_lists_everything_on_a_fresh_database(config, db: Database) -> None:
    scheduler = Scheduler(config, db)
    try:
        due = scheduler.due_jobs(now=WEDNESDAY.replace(hour=9, minute=30))
    finally:
        scheduler.close()
    assert "ingest" in due and "discover" in due and "alerts" in due


def test_status_reports_last_run(config, db: Database) -> None:
    set_last(db, "health", WEDNESDAY)
    scheduler = Scheduler(config, db)
    try:
        status = {row["name"]: row for row in scheduler.status()}
    finally:
        scheduler.close()
    assert status["health"]["last"]["last_status"] == "ok"
    assert status["health"]["interval_minutes"] == 60
