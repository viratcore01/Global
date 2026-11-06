"""Job functions used by the scheduler, worker and CLI."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import repo
from .alert.routing import AlertRouter
from .backup import snapshot as backup_snapshot
from .config import AppConfig, SourceConfig
from .db import Database
from .discovery.base import discover_source
from .health import ping_heartbeat, snapshot as health_snapshot
from .learning import run_weekly
from .pipeline import Pipeline
from .util import get_logger, iso, json_loads, now_utc

log = get_logger(__name__)

NO_EMPTY_DRIFT_TYPES = {"email", "google_alerts", "sitemap", "searxng", "wayback", "discord"}


def sync_sources(config: AppConfig, db: Database) -> dict[str, int]:
    return repo.sync_sources(db, config.sources)


def job_discover(
    config: AppConfig,
    db: Database,
    pipeline: Pipeline,
    *,
    force: bool = False,
    source_name: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    if source_name:
        source = repo.get_source_by_name(db, source_name)
        sources = [source] if source else []
        if not sources:
            return {"ok": False, "error": f"source not found: {source_name}", "sources": []}
    elif force:
        sources = repo.list_sources(db, enabled_only=True)
    else:
        sources = repo.due_sources(db)
    if limit:
        sources = sources[:limit]
    results: list[dict[str, Any]] = []
    queued_total = 0
    for source in sources:
        result = discover_source(config, db, source, fetcher=pipeline.fetcher)
        queued_total += result.queued
        empty_counts = str(source.get("type")) not in NO_EMPTY_DRIFT_TYPES
        status = repo.record_source_result(
            db,
            int(source["id"]),
            success=result.error is None,
            http_status=result.http_status,
            records=result.queued,
            error=result.error,
            empty_reason=result.empty_reason,
            empty_counts=empty_counts,
        )
        results.append(
            {
                "source": source.get("name"),
                "type": source.get("type"),
                "queued": result.queued,
                "error": result.error,
                "empty_reason": result.empty_reason,
                "health": status,
            }
        )
    repo.record_job_run(db, "discover", status="ok" if any(r.get("error") is None for r in results) or not results else "error")
    return {"ok": True, "queued": queued_total, "sources": results}


def job_ingest(config: AppConfig, db: Database, pipeline: Pipeline, *, limit: int = 40) -> dict[str, Any]:
    stats = pipeline.process_queue(limit=limit)
    repo.record_job_run(db, "ingest", status="ok")
    return stats


def job_score(config: AppConfig, db: Database, pipeline: Pipeline, *, limit: int = 300) -> dict[str, Any]:
    results = pipeline.ranker.score_pending(limit=limit)
    repo.record_job_run(db, "score", status="ok")
    return {
        "scored": len(results),
        "alerts_candidates": sum(1 for item in results if item.score >= config.instant_cutoff),
        "vetoed": sum(1 for item in results if item.vetoed),
        "ineligible": sum(1 for item in results if not item.eligible),
    }


def job_alerts(config: AppConfig, db: Database, *, limit: int = 100) -> dict[str, Any]:
    router = AlertRouter(config, db)
    rows = repo.list_opportunities(
        db,
        status="new",
        min_score=config.digest_cutoff,
        limit=limit,
        order="score",
    )
    decisions = []
    for opportunity in rows:
        try:
            decision = router.consider(opportunity)
        except Exception as exc:
            log.exception("Alert routing failed for %s: %s", opportunity.get("id"), exc)
            continue
        if decision.action in {"sent", "error", "digest"}:
            decisions.append({"id": opportunity.get("id"), **decision.to_dict()})
    repo.record_job_run(db, "alerts", status="ok")
    return {
        "considered": len(rows),
        "sent": sum(1 for item in decisions if item["action"] == "sent"),
        "digest_held": sum(1 for item in decisions if item["action"] == "digest"),
        "errors": sum(1 for item in decisions if item["action"] == "error"),
        "decisions": decisions,
    }


def job_digest(config: AppConfig, db: Database, *, dry_run: bool = False) -> dict[str, Any]:
    router = AlertRouter(config, db)
    result = router.send_digest(dry_run=dry_run)
    repo.record_job_run(db, "digest", status="ok" if result.get("sent") or dry_run else "skip")
    return result


def job_health(config: AppConfig, db: Database) -> dict[str, Any]:
    result = health_snapshot(config, db)
    repo.record_job_run(db, "health", status="ok")
    return result


def job_backup(config: AppConfig, db: Database) -> dict[str, Any]:
    result = backup_snapshot(config, db)
    repo.record_job_run(db, "backup", status="ok")
    return result


def job_weekly(config: AppConfig, db: Database, *, force: bool = False) -> dict[str, Any]:
    result = run_weekly(config, db, force=force)
    repo.record_job_run(db, "weekly", status="ok" if not result.get("skipped") else "skip")
    return result


def heartbeat(config: AppConfig, *, success: bool = True, message: str = "") -> dict[str, Any]:
    return ping_heartbeat(config, success=success, message=message)


def run_cycle(
    config: AppConfig,
    db: Database,
    pipeline: Pipeline,
    *,
    force_discovery: bool = False,
    digest: bool = False,
    limit_sources: int | None = None,
    queue_limit: int = 40,
) -> dict[str, Any]:
    sync_sources(config, db)
    discovery = job_discover(config, db, pipeline, force=force_discovery, limit=limit_sources)
    ingest = job_ingest(config, db, pipeline, limit=queue_limit)
    score = job_score(config, db, pipeline)
    alerts = job_alerts(config, db)
    digest_result = job_digest(config, db) if digest else None
    result = {
        "discovery": discovery,
        "ingest": ingest,
        "score": score,
        "alerts": alerts,
        "digest": digest_result,
        "finished_at": iso(now_utc()),
    }
    ok = discovery.get("ok") and not ingest.get("error")
    ping = heartbeat(config, success=bool(ok), message=f"cycle queued={discovery.get('queued', 0)} ingested={ingest.get('processed', 0)}")
    result["heartbeat"] = ping
    return result


def seed_demo(config: AppConfig, db: Database, pipeline: Pipeline, *, file: str | None = None) -> dict[str, Any]:
    path = Path(file) if file else config.paths.root / "tests" / "fixtures" / "demo_items.json"
    if not path.exists():
        return {"ok": False, "error": f"demo fixture not found: {path}"}
    payload = json_loads(path.read_text(encoding="utf-8"), {})
    if isinstance(payload, dict):
        entries = payload.get("items") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    source = repo.get_source_by_name(db, "Demo fixtures")
    if not source:
        source_id = repo.add_source(
            db,
            SourceConfig(
                name="Demo fixtures",
                type="demo",
                tier=3,
                enabled=False,
                poll_interval_minutes=1440,
                assume_opportunity=True,
                notes="Local fixture corpus for smoke tests.",
            ),
        )
        source = repo.get_source(db, source_id)
    ingested = 0
    skipped = 0
    errors: list[str] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or index + 1)
        result = pipeline.ingest(
            f"fixture://demo/{identifier}",
            source,
            kind=str(entry.get("kind") or "page"),
            payload={
                **entry,
                "text": entry.get("text") or "",
                "html": entry.get("html") or "",
            },
        )
        if result.error:
            errors.append(f"{identifier}: {result.error}")
        elif result.records:
            ingested += len(result.records)
        else:
            skipped += 1
    repo.record_job_run(db, "seed_demo", status="ok" if not errors else "partial")
    return {
        "ok": not errors,
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "file": str(path),
    }
