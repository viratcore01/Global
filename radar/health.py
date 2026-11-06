"""Source health, heartbeat, doctor checks and operational diagnostics."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import repo
from .config import AppConfig
from .db import Database
from .util import get_logger, iso, now_utc, parse_dt

log = get_logger(__name__)


def source_warnings(config: AppConfig, db: Database, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or now_utc()
    warnings: list[dict[str, Any]] = []
    for row in repo.health_report(db):
        if not row.get("enabled"):
            continue
        status = str(row.get("status") or "new")
        interval = int(row.get("poll_interval_minutes") or 60) if "poll_interval_minutes" in row else 60
        # health_report does not include interval; load it from source when needed.
        if "poll_interval_minutes" not in row:
            source = repo.get_source(db, int(row["id"]))
            interval = int(source.get("poll_interval_minutes") or 60) if source else 60
        last_run = parse_dt(row.get("last_run_at"))
        if status in {"dead", "blocked", "broken", "failing"}:
            warnings.append(
                {
                    "source_id": row["id"],
                    "name": row.get("name"),
                    "severity": "error" if status in {"dead", "blocked", "broken"} else "warning",
                    "status": status,
                    "message": row.get("last_error") or f"source status is {status}",
                }
            )
        elif row.get("selector_drift") or status in {"selector_drift", "empty"}:
            warnings.append(
                {
                    "source_id": row["id"],
                    "name": row.get("name"),
                    "severity": "warning",
                    "status": status,
                    "message": f"{row.get('consecutive_empty', 0)} consecutive empty runs; possible selector drift",
                }
            )
        elif last_run and current - last_run > timedelta(minutes=max(180, interval * 3)):
            warnings.append(
                {
                    "source_id": row["id"],
                    "name": row.get("name"),
                    "severity": "warning",
                    "status": "silent",
                    "message": f"no run for {(current - last_run).total_seconds() / 3600:.1f} hours",
                }
            )
    return warnings


def snapshot(config: AppConfig, db: Database) -> dict[str, Any]:
    counts = repo.count_opportunities(db)
    qstats = repo.queue_stats(db)
    alert_count = int(db.scalar("SELECT COUNT(*) FROM alerts", default=0) or 0)
    labels = repo.label_counts(db)
    quarantine = len(repo.list_quarantine(db, open_only=True, limit=500))
    warnings = source_warnings(config, db)
    size = config.paths.db_path.stat().st_size if config.paths.db_path.exists() else 0
    disk = shutil.disk_usage(config.paths.data_dir)
    return {
        "generated_at": iso(now_utc()),
        "db_bytes": size,
        "disk_free_bytes": disk.free,
        "opportunities": counts,
        "queue": qstats,
        "alerts": alert_count,
        "labels": labels,
        "quarantine_open": quarantine,
        "source_warnings": warnings,
        "integrity": db.integrity_check(),
        "jobs": db.query("SELECT * FROM job_runs ORDER BY updated_at DESC"),
        "embedding_coverage": repo.embedding_coverage(db),
    }


def ping_heartbeat(config: AppConfig, *, success: bool = True, message: str = "") -> dict[str, Any]:
    url = config.settings.healthchecks_url
    if not url:
        return {"configured": False, "ok": False, "reason": "HEALTHCHECKS_URL not set"}
    target = url.rstrip("/")
    if not success:
        target = target + "/fail"
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            if message:
                response = client.post(target, params={"msg": message[:500]})
            else:
                response = client.get(target)
        return {"configured": True, "ok": response.status_code < 400, "status": response.status_code}
    except httpx.HTTPError as exc:
        return {"configured": True, "ok": False, "error": str(exc)}


def doctor(config: AppConfig, db: Database) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", severity: str = "error") -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": severity})

    add("config: profile", (config.paths.config_dir / "profile.yaml").exists(), str(config.paths.config_dir / "profile.yaml"))
    add("config: sources", (config.paths.config_dir / "sources.yaml").exists(), f"{len(config.sources)} configured")
    add("config: veto patterns", bool(config.vetoes.vetoes), f"{len(config.vetoes.vetoes)} vetoes, {len(config.vetoes.penalties)} penalties")
    add("database integrity", db.integrity_check() == "ok", db.integrity_check())
    add("database FTS5", db.table_exists("opportunities_fts"), "opportunities_fts")
    add("database WAL", str(db.scalar("PRAGMA journal_mode", default="")).lower() == "wal", str(db.scalar("PRAGMA journal_mode")))
    enabled = [source for source in repo.list_sources(db, enabled_only=True)]
    add("sources enabled", bool(enabled), f"{len(enabled)} enabled", severity="warning")
    add("ntfy instant", config.settings.ntfy_configured, "NTFY_TOPIC configured" if config.settings.ntfy_configured else "optional: alerts disabled", severity="warning")
    add("ntfy reply", config.settings.reply_configured and bool(config.settings.reply_secret), "reply topic + secret" if config.settings.reply_configured and config.settings.reply_secret else "optional: buttons disabled", severity="warning")
    add("IMAP", config.settings.imap_enabled, "enabled" if config.settings.imap_enabled else "disabled", severity="warning")
    add("LLM", config.settings.llm_enabled, config.settings.llm_provider, severity="warning")
    add("heartbeat", bool(config.settings.healthchecks_url), "HEALTHCHECKS_URL set" if config.settings.healthchecks_url else "optional", severity="warning")
    add(
        "alert thresholds",
        True,
        f"instant {config.instant_cutoff:.0f} (" + ("RADAR_INSTANT_THRESHOLD" if config.settings.instant_threshold is not None else "profile.yaml") + ")"
        f", digest {config.digest_cutoff:.0f} (" + ("RADAR_DIGEST_THRESHOLD" if config.settings.digest_threshold is not None else "profile.yaml") + ")",
        severity="info",
    )
    ist_named = config.settings.timezone.strip().lower() in {"asia/kolkata", "asia/calcutta", "ist", ""}
    add(
        "timezone",
        ist_named,
        "scheduling uses a fixed IST offset (UTC+05:30)" if ist_named else f"RADAR_TIMEZONE={config.settings.timezone} is display-only; scheduling stays on IST",
        severity="warning",
    )

    try:
        import sqlite3

        add("sqlite version", True, sqlite3.sqlite_version, severity="info")
    except Exception:
        pass
    for package, label in (
        ("curl_cffi", "curl_cffi fetch escalation"),
        ("trafilatura", "trafilatura main text"),
        ("extruct", "extruct JSON-LD"),
        ("datasketch", "datasketch MinHash"),
        ("imap_tools", "IMAP email"),
    ):
        try:
            __import__(package)
            add(label, True, "installed", severity="info")
        except Exception:
            add(label, False, "not installed (optional unless that source is enabled)", severity="warning")
    disk = shutil.disk_usage(config.paths.data_dir)
    add("disk space", disk.free > 200 * 1024 * 1024, f"{disk.free / (1024**3):.1f} GiB free")
    placeholder = config.profile.name.strip().lower() in {"", "your name"}
    add("profile personalisation", not placeholder, "profile still has placeholder name" if placeholder else "name set", severity="warning")

    failed = [check for check in checks if not check["ok"] and check["severity"] == "error"]
    warnings = [check for check in checks if not check["ok"] and check["severity"] == "warning"]
    return {"ok": not failed, "checks": checks, "failed": len(failed), "warnings": len(warnings)}
