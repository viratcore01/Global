"""Scheduler with persisted last-run bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from . import jobs, repo
from .config import AppConfig
from .db import Database
from .pipeline import Pipeline
from .util import IST, get_logger, now_ist, now_utc, parse_dt

log = get_logger(__name__)


@dataclass
class JobSpec:
    name: str
    interval_minutes: int | None = None
    daily_at: tuple[int, int] | None = None
    weekday: int | None = None  # Monday=0


JOBS = [
    JobSpec("ingest", interval_minutes=5),
    JobSpec("discover", interval_minutes=15),
    JobSpec("alerts", interval_minutes=5),
    JobSpec("health", interval_minutes=60),
    JobSpec("digest", daily_at=(8, 0)),
    JobSpec("backup", daily_at=(3, 0)),
    JobSpec("weekly", daily_at=(9, 0), weekday=0),
]


def _last_run(db: Database, name: str) -> datetime | None:
    row = repo.job_last_run(db, name)
    return parse_dt(row.get("last_run_at")) if row else None


def is_due(db: Database, spec: JobSpec, *, now: datetime | None = None) -> bool:
    current = (now or now_utc()).astimezone(IST)
    last = _last_run(db, spec.name)
    if last is not None:
        last = last.astimezone(IST)
    if spec.interval_minutes is not None:
        if last is None:
            return True
        return current >= last + timedelta(minutes=spec.interval_minutes)
    if spec.daily_at is not None:
        if spec.weekday is not None and current.weekday() != spec.weekday:
            return False
        hour, minute = spec.daily_at
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if current < scheduled:
            return False
        if last is None:
            return True
        return last.date() < current.date()
    return False


class Scheduler:
    def __init__(self, config: AppConfig, db: Database, pipeline: Pipeline | None = None):
        self.config = config
        self.db = db
        self.pipeline = pipeline or Pipeline(config, db)
        self._closable_pipeline = pipeline is None

    def close(self) -> None:
        if self._closable_pipeline:
            self.pipeline.close()

    def due_jobs(self, *, now: datetime | None = None) -> list[str]:
        return [spec.name for spec in JOBS if is_due(self.db, spec, now=now)]

    def run_job(self, name: str, *, force: bool = False) -> dict[str, Any]:
        if name == "discover":
            return jobs.job_discover(self.config, self.db, self.pipeline, force=force)
        if name == "ingest":
            return jobs.job_ingest(self.config, self.db, self.pipeline)
        if name == "alerts":
            return jobs.job_alerts(self.config, self.db)
        if name == "digest":
            return jobs.job_digest(self.config, self.db)
        if name == "health":
            return jobs.job_health(self.config, self.db)
        if name == "backup":
            return jobs.job_backup(self.config, self.db)
        if name == "weekly":
            return jobs.job_weekly(self.config, self.db, force=force)
        raise ValueError(f"unknown job {name!r}")

    def run_due(self, *, now: datetime | None = None, force: list[str] | None = None) -> dict[str, Any]:
        forced = set(force or [])
        due = self.due_jobs(now=now)
        names = [spec.name for spec in JOBS if spec.name in forced] + [name for name in due if name not in forced]
        # Keep a deterministic order that lets discovery feed ingest within the
        # same cycle.
        order = {name: index for index, name in enumerate(["discover", "ingest", "alerts", "digest", "health", "backup", "weekly"])}
        names = sorted(dict.fromkeys(names), key=lambda value: order.get(value, 99))
        results: dict[str, Any] = {}
        for name in names:
            try:
                results[name] = self.run_job(name, force=name in forced)
            except Exception as exc:
                log.exception("Scheduled job %s failed: %s", name, exc)
                repo.record_job_run(self.db, name, status="error", error=str(exc))
                results[name] = {"ok": False, "error": str(exc)}
        # Heartbeat once per non-empty cycle.
        if results:
            jobs.heartbeat(
                self.config,
                success=not any(isinstance(result, dict) and result.get("ok") is False for result in results.values()),
                message="scheduled: " + ",".join(results),
            )
        return {"ran": names, "results": results, "at": (now or now_utc()).isoformat()}

    def status(self) -> list[dict[str, Any]]:
        output = []
        for spec in JOBS:
            row = repo.job_last_run(self.db, spec.name)
            output.append(
                {
                    "name": spec.name,
                    "interval_minutes": spec.interval_minutes,
                    "daily_at": spec.daily_at,
                    "weekday": spec.weekday,
                    "due": is_due(self.db, spec),
                    "last": row,
                }
            )
        return output
