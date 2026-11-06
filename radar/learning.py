"""Weekly learning loop, quality metrics and recall audit."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from . import repo
from .config import AppConfig
from .db import Database
from .score.learning import refit
from .util import IST, date_only, get_logger, iso, now_ist, now_utc, parse_dt, week_start

log = get_logger(__name__)


def _window(days: int = 7, *, now: datetime | None = None) -> tuple[datetime, datetime]:
    end = now or now_utc()
    start = end - timedelta(days=days)
    return start, end


def metrics(db: Database, *, days: int = 7, now: datetime | None = None) -> dict[str, Any]:
    start, end = _window(days, now=now)
    start_iso, end_iso = iso(start), iso(end)

    instant_alerts = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE kind='instant' AND sent_at>=? AND sent_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    digest_alerts = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE kind='digest' AND sent_at>=? AND sent_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    instant_apply = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE kind='instant' AND response='apply' AND sent_at>=? AND sent_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    instant_ignore = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE kind='instant' AND response='ignore' AND sent_at>=? AND sent_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    instant_scam = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE kind='instant' AND response='scam' AND sent_at>=? AND sent_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0,
    )
    labels = db.query("SELECT action, COUNT(*) n FROM labels WHERE created_at>=? AND created_at<? GROUP BY action", (start_iso, end_iso))
    label_counts = {row["action"]: int(row["n"]) for row in labels}
    applied = int(label_counts.get("apply", 0))
    replied = int(label_counts.get("replied", 0))
    booked = int(label_counts.get("booked", 0))
    scams = int(label_counts.get("scam", 0))
    ignores = int(label_counts.get("ignore", 0))
    outcome_rate = (replied + booked) / applied if applied else None
    precision = instant_apply / instant_alerts if instant_alerts else None

    quarantine = int(
        db.scalar(
            "SELECT COUNT(*) FROM quarantine WHERE created_at>=? AND created_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    vetoes = int(
        db.scalar(
            "SELECT COUNT(*) FROM veto_log WHERE created_at>=? AND created_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    new_opps = int(
        db.scalar(
            "SELECT COUNT(*) FROM opportunities WHERE first_seen_at>=? AND first_seen_at<?",
            (start_iso, end_iso),
            default=0,
        )
        or 0
    )
    time_to_alert = db.scalar(
        "SELECT AVG((julianday(a.sent_at)-julianday(o.first_seen_at))*24) FROM alerts a "
        "JOIN opportunities o ON o.id=a.opportunity_id WHERE a.sent_at>=? AND a.sent_at<?",
        (start_iso, end_iso),
    )
    unresolved_48h = int(
        db.scalar(
            "SELECT COUNT(*) FROM alerts WHERE response IS NULL AND sent_at < ?",
            (iso(end - timedelta(hours=48)),),
            default=0,
        )
        or 0,
    )
    return {
        "window": {"start": start_iso, "end": end_iso, "days": days},
        "new_opportunities": new_opps,
        "alerts": {"instant": instant_alerts, "digest": digest_alerts, "unresolved_48h": unresolved_48h},
        "responses": {
            "apply": instant_apply,
            "ignore": instant_ignore,
            "scam": instant_scam,
        },
        "labels": label_counts,
        "precision_at_15": round(precision, 4) if precision is not None else None,
        "outcome_rate": round(outcome_rate, 4) if outcome_rate is not None else None,
        "vetoes": vetoes,
        "quarantine_new": quarantine,
        "time_to_alert_hours": round(float(time_to_alert), 2) if time_to_alert is not None else None,
        "scam_labels": scams,
        "manual_rejections": ignores + scams,
    }


def threshold_recommendation(config: AppConfig, metric: dict[str, Any]) -> dict[str, Any]:
    precision = metric.get("precision_at_15")
    alerts = metric.get("alerts", {}).get("instant", 0)
    current = config.instant_cutoff
    if precision is None:
        return {"action": "wait", "reason": "not enough instant alerts to judge precision", "current": current}
    if alerts < 5:
        return {"action": "wait", "reason": "fewer than 5 instant alerts this week", "current": current}
    if precision < 0.25:
        return {"action": "raise", "by": 8.0, "current": current, "reason": "precision@15 below 25%: stop adding sources and fix scoring"}
    if precision < 0.40:
        return {"action": "raise", "by": 5.0, "current": current, "reason": "precision@15 below 40%"}
    if precision > 0.70:
        return {"action": "lower", "by": 2.0, "current": current, "reason": "precision@15 above 70%; may be missing good items"}
    return {"action": "hold", "current": current, "reason": "precision in target band"}


def recall_audit(
    db: Database,
    *,
    sample: int = 10,
    days: int = 7,
    low_cutoff: float = 55.0,
) -> dict[str, Any]:
    vetoed = repo.random_vetoed(db, days=days, limit=max(1, sample // 2))
    low = db.query(
        "SELECT o.* FROM opportunities o LEFT JOIN alerts a ON a.opportunity_id=o.id "
        "WHERE o.status IN ('new','reviewed') AND o.score < ? AND a.id IS NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (float(low_cutoff), max(1, sample - len(vetoed))),
    )
    low_items = [repo.decode_opportunity(row) for row in low]
    result_vetoed = [
        {
            "opportunity_id": row.get("opportunity_id"),
            "title": row.get("title"),
            "reason": row.get("reason"),
            "rule_id": row.get("rule_id"),
            "score": row.get("score"),
            "canonical_url": row.get("canonical_url"),
            "stage": row.get("stage"),
        }
        for row in vetoed
    ]
    result_low = [
        {
            "opportunity_id": item.get("id"),
            "title": item.get("title"),
            "score": item.get("score"),
            "city": item.get("city"),
            "canonical_url": item.get("canonical_url"),
            "risk_flags": item.get("risk_flags"),
        }
        for item in low_items
        if item
    ]
    return {
        "sampled_at": iso(now_utc()),
        "vetoed": result_vetoed,
        "low_ranked": result_low,
        "instructions": "Open each item. If a good one is hidden, add a rule exception or fix extraction/scoring.",
    }


def run_weekly(config: AppConfig, db: Database, *, days: int = 7, force: bool = False) -> dict[str, Any]:
    start = week_start(now_ist())
    week_key = start.isoformat()
    existing = db.query_one("SELECT week_start FROM weekly_reports WHERE week_start=?", (week_key,))
    if existing and not force:
        row = db.query_one("SELECT * FROM weekly_reports WHERE week_start=?", (week_key,))
        from .util import json_loads

        return {"skipped": True, "reason": "weekly report already generated", "report": json_loads(row.get("metrics_json"), {}) if row else {}}
    metric = metrics(db, days=days)
    audit = recall_audit(db, days=days, low_cutoff=config.digest_cutoff)
    fit = refit(db)
    recommendation = threshold_recommendation(config, metric)
    report = {
        **metric,
        "generated_at": iso(now_utc()),
        "refit": fit,
        "threshold_recommendation": recommendation,
        "recall_audit": audit,
        "source_warnings": __import__("radar.health", fromlist=["source_warnings"]).source_warnings(config, db),
    }
    repo.save_weekly_report(db, week_key, report)
    return {"skipped": False, "week_start": week_key, "report": report}
