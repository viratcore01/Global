"""Repository layer: all SQL lives here, callers get plain dicts."""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .config import Profile, SourceConfig
from .db import Database
from .normalize import (
    canonical_hash,
    canonicalize_url,
    contact_domain,
    domain_from_url,
    extract_emails,
    registrable_domain,
)
from .util import (
    IST,
    clamp,
    date_only,
    get_logger,
    iso,
    json_dumps,
    json_loads,
    now_ist,
    now_utc,
    parse_dt,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Encoding / decoding
# ---------------------------------------------------------------------------


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json_dumps(value)


def decode_opportunity(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["languages"] = json_loads(out.get("language_json"), []) or []
    out["role_types"] = json_loads(out.get("role_type_json"), []) or []
    out["shoot_window"] = json_loads(out.get("shoot_window_json"), []) or []
    out["evidence"] = json_loads(out.get("fields_evidence_json"), {}) or {}
    out["risk_flags"] = json_loads(out.get("risk_flags_json"), []) or []
    out["score_breakdown"] = json_loads(out.get("score_breakdown_json"), {}) or {}
    for key in ("remote_ok", "fee_required", "travel_covered"):
        if out.get(key) is not None:
            out[key] = bool(out[key])
    return out


def decode_source(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["extractor_config"] = json_loads(out.get("extractor_config_json"), {}) or {}
    out["config"] = json_loads(out.get("config_json"), {}) or {}
    out["tags"] = json_loads(out.get("tags_json"), []) or []
    out["enabled"] = bool(out.get("enabled"))
    out["assume_opportunity"] = bool(out.get("assume_opportunity"))
    return out


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


SOURCE_COLUMNS = (
    "name",
    "type",
    "url",
    "tier",
    "enabled",
    "poll_interval_minutes",
    "fetch_strategy",
    "extractor",
    "extractor_config_json",
    "cadence_hint",
    "contact",
    "notes",
    "config_json",
    "tags_json",
    "assume_opportunity",
    "created_at",
    "updated_at",
)


# Tables whose rows would lose meaning if a source row disappeared.
_SOURCE_HISTORY_TABLES = (
    "raw_documents",
    "opportunities",
    "discovery_queue",
    "hub_candidates",
    "email_messages",
    "quarantine",
)


def _source_has_history(db: Database, source_id: int) -> bool:
    for table in _SOURCE_HISTORY_TABLES:
        if db.query_one(f"SELECT 1 FROM {table} WHERE source_id = ? LIMIT 1", (source_id,)):
            return True
    return False


def sync_sources(db: Database, sources: Sequence[SourceConfig]) -> dict[str, int]:
    """Upsert YAML sources, then retire sources that are no longer in the YAML.

    Existing rows keep their id so opportunity history stays attached. A source
    deleted from ``sources.yaml`` is removed when it never produced anything, or
    just disabled when it did, so history is never silently detached.
    """
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "disabled": 0, "removed": 0}
    now = iso(now_utc())
    for source in sources:
        existing = db.query_one("SELECT id FROM sources WHERE name = ?", (source.name,))
        values = (
            source.name,
            source.type,
            source.url,
            source.tier,
            int(source.enabled),
            source.poll_interval_minutes,
            source.fetch_strategy,
            source.extractor,
            _dumps(source.extractor_config),
            source.cadence_hint,
            source.contact,
            source.notes,
            _dumps(source.config),
            _dumps(source.tags),
            int(source.assume_opportunity),
        )
        if existing:
            db.execute(
                "UPDATE sources SET type=?, url=?, tier=?, enabled=?, poll_interval_minutes=?, "
                "fetch_strategy=?, extractor=?, extractor_config_json=?, cadence_hint=?, contact=?, "
                "notes=?, config_json=?, tags_json=?, assume_opportunity=?, updated_at=? WHERE id=?",
                (*values[1:], now, existing["id"]),
            )
            stats["updated"] += 1
            source_id = int(existing["id"])
        else:
            source_id = db.insert(
                "INSERT INTO sources(name, type, url, tier, enabled, poll_interval_minutes, "
                "fetch_strategy, extractor, extractor_config_json, cadence_hint, contact, notes, "
                "config_json, tags_json, assume_opportunity, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, now, now),
            )
            stats["inserted"] += 1
        _ensure_health(db, source_id, source.poll_interval_minutes)
        _ensure_stats(db, source_id)

    configured = [source.name for source in sources]
    if configured:
        placeholders = ",".join("?" for _ in configured)
        stale = db.query(
            f"SELECT id, name FROM sources WHERE name NOT IN ({placeholders})",
            tuple(configured),
        )
    else:
        stale = db.query("SELECT id, name FROM sources")
    for row in stale:
        stale_id = int(row["id"])
        if _source_has_history(db, stale_id):
            db.execute("UPDATE sources SET enabled = 0, updated_at = ? WHERE id = ?", (now, stale_id))
            stats["disabled"] += 1
        else:
            db.execute("DELETE FROM sources WHERE id = ?", (stale_id,))
            stats["removed"] += 1
    return stats


def _ensure_health(db: Database, source_id: int, interval: int = 60) -> None:
    exists = db.query_one("SELECT source_id FROM source_health WHERE source_id = ?", (source_id,))
    if not exists:
        now = iso(now_utc())
        next_run = iso(now_utc() + timedelta(minutes=interval))
        db.execute(
            "INSERT INTO source_health(source_id, next_run_at, status, updated_at) VALUES (?, ?, 'new', ?)",
            (source_id, next_run, now),
        )


def _ensure_stats(db: Database, source_id: int) -> None:
    exists = db.query_one("SELECT source_id FROM source_stats WHERE source_id = ?", (source_id,))
    if not exists:
        db.execute(
            "INSERT INTO source_stats(source_id, trust_score, updated_at) VALUES (?, 0.5, ?)",
            (source_id, iso(now_utc())),
        )


def list_sources(db: Database, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    where = "WHERE s.enabled = 1" if enabled_only else ""
    rows = db.query(
        f"SELECT s.*, h.status AS health_status, h.last_success_at, h.last_error, "
        f"h.consecutive_empty, h.consecutive_failures, h.next_run_at, h.selector_drift, "
        f"st.trust_score, st.applies, st.scams, st.labels_total "
        f"FROM sources s LEFT JOIN source_health h ON h.source_id=s.id "
        f"LEFT JOIN source_stats st ON st.source_id=s.id {where} ORDER BY s.tier, s.name"
    )
    return [decode_source(row) for row in rows]  # type: ignore[misc]


def get_source(db: Database, source_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM sources WHERE id = ?", (source_id,))
    return decode_source(row)


def get_source_by_name(db: Database, name: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM sources WHERE name = ?", (name,))
    return decode_source(row)


def add_source(db: Database, source: SourceConfig) -> int:
    now = iso(now_utc())
    source_id = db.insert(
        "INSERT INTO sources(name, type, url, tier, enabled, poll_interval_minutes, "
        "fetch_strategy, extractor, extractor_config_json, cadence_hint, contact, notes, "
        "config_json, tags_json, assume_opportunity, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source.name,
            source.type,
            source.url,
            source.tier,
            int(source.enabled),
            source.poll_interval_minutes,
            source.fetch_strategy,
            source.extractor,
            _dumps(source.extractor_config),
            source.cadence_hint,
            source.contact,
            source.notes,
            _dumps(source.config),
            _dumps(source.tags),
            int(source.assume_opportunity),
            now,
            now,
        ),
    )
    _ensure_health(db, source_id, source.poll_interval_minutes)
    _ensure_stats(db, source_id)
    return source_id


def set_source_enabled(db: Database, source_id: int, enabled: bool) -> None:
    db.execute(
        "UPDATE sources SET enabled=?, updated_at=? WHERE id=?",
        (int(enabled), iso(now_utc()), source_id),
    )


def due_sources(db: Database, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = iso(now or now_utc())
    rows = db.query(
        "SELECT s.*, h.next_run_at, h.status AS health_status FROM sources s "
        "JOIN source_health h ON h.source_id=s.id "
        "WHERE s.enabled=1 AND (h.next_run_at IS NULL OR h.next_run_at <= ?) "
        "ORDER BY COALESCE(h.next_run_at, '') ASC, s.tier ASC",
        (current,),
    )
    return [decode_source(row) for row in rows]  # type: ignore[misc]


def record_source_result(
    db: Database,
    source_id: int,
    *,
    success: bool,
    http_status: int | None = None,
    records: int = 0,
    error: str | None = None,
    empty_reason: str | None = None,
    empty_counts: bool = True,
) -> dict[str, Any]:
    now_dt = now_utc()
    now = iso(now_dt)
    source = get_source(db, source_id)
    interval = int(source["poll_interval_minutes"]) if source else 60
    health = db.query_one("SELECT * FROM source_health WHERE source_id = ?", (source_id,)) or {}
    empty = int(health.get("consecutive_empty", 0) or 0)
    failures = int(health.get("consecutive_failures", 0) or 0)
    ok = int(health.get("consecutive_ok", 0) or 0)
    avg_gap = health.get("avg_gap_hours")
    last_success = parse_dt(health.get("last_success_at"))
    status = health.get("status", "new")
    drift = int(health.get("selector_drift", 0) or 0)

    if success:
        if last_success:
            gap = (now_dt - last_success).total_seconds() / 3600.0
            avg_gap = gap if avg_gap is None else (float(avg_gap) * 0.8 + gap * 0.2)
        if records > 0:
            empty = 0
            failures = 0
            ok += 1
            drift = 0
            status = "active"
        elif not empty_counts:
            empty = 0
            failures = 0
            ok += 1
            drift = 0
            status = "active"
        else:
            empty += 1
            ok = 0
            failures = 0
            if empty >= 4:
                drift = 1
                status = "broken"
            elif empty >= 2:
                drift = 1
                status = "selector_drift"
            else:
                status = "empty"
        last_success_value = now
    else:
        failures += 1
        ok = 0
        empty = 0
        last_success_value = health.get("last_success_at")
        if http_status in (404, 410) and failures >= 2:
            status = "dead"
        elif failures >= 5:
            status = "blocked"
        else:
            status = "failing"

    next_run = iso(now_dt + timedelta(minutes=interval))
    db.execute(
        "UPDATE source_health SET last_run_at=?, last_success_at=?, last_error=?, last_http_status=?, "
        "consecutive_empty=?, consecutive_failures=?, consecutive_ok=?, avg_gap_hours=?, next_run_at=?, "
        "status=?, selector_drift=?, last_records=?, updated_at=? WHERE source_id=?",
        (
            now,
            last_success_value,
            error,
            http_status,
            empty,
            failures,
            ok,
            avg_gap,
            next_run,
            status,
            drift,
            records,
            now,
            source_id,
        ),
    )
    return {
        "source_id": source_id,
        "status": status,
        "records": records,
        "consecutive_empty": empty,
        "consecutive_failures": failures,
        "selector_drift": bool(drift),
        "error": error,
        "empty_reason": empty_reason,
        "next_run_at": next_run,
    }


def health_report(db: Database) -> list[dict[str, Any]]:
    return db.query(
        "SELECT s.id, s.name, s.type, s.url, s.tier, s.enabled, h.status, h.last_run_at, "
        "h.last_success_at, h.last_error, h.last_http_status, h.consecutive_empty, "
        "h.consecutive_failures, h.consecutive_ok, h.avg_gap_hours, h.next_run_at, h.selector_drift, "
        "st.trust_score, st.applies, st.scams, st.labels_total "
        "FROM sources s LEFT JOIN source_health h ON h.source_id=s.id "
        "LEFT JOIN source_stats st ON st.source_id=s.id ORDER BY s.tier, s.name"
    )


def mark_source_dead(db: Database, source_id: int) -> None:
    db.execute(
        "UPDATE source_health SET status='dead', updated_at=? WHERE source_id=?",
        (iso(now_utc()), source_id),
    )


def source_trust(db: Database, source_id: int, tier: int, cluster_sources: int = 1) -> float:
    tier_base = {1: 0.90, 2: 0.70, 3: 0.45}.get(int(tier or 3), 0.45)
    stats = db.query_one("SELECT trust_score, labels_total FROM source_stats WHERE source_id=?", (source_id,))
    learned = float(stats["trust_score"]) if stats else 0.5
    has_history = bool(stats and int(stats.get("labels_total") or 0) > 0)
    base = (tier_base * 0.7 + learned * 0.3) if has_history else tier_base
    corroboration = min(0.20, max(0, cluster_sources - 1) * 0.06)
    return clamp(base + corroboration)


def apply_source_label(db: Database, source_id: int | None, action: str) -> None:
    if not source_id:
        return
    _ensure_stats(db, source_id)
    column = {
        "apply": "applies",
        "ignore": "ignores",
        "scam": "scams",
        "replied": "replies",
        "booked": "bookings",
    }.get(action)
    if not column:
        return
    now = iso(now_utc())
    db.execute(
        f"UPDATE source_stats SET {column}={column}+1, labels_total=labels_total+1, "
        "last_label_at=?, updated_at=? WHERE source_id=?",
        (now, now, source_id),
    )
    stats = db.query_one("SELECT * FROM source_stats WHERE source_id=?", (source_id,))
    if not stats:
        return
    applies = int(stats["applies"] or 0)
    ignores = int(stats["ignores"] or 0)
    scams = int(stats["scams"] or 0)
    replies = int(stats["replies"] or 0)
    bookings = int(stats["bookings"] or 0)
    # Bayesian-ish trust: positives weigh more than weak ignores; scams are
    # strongly negative; replies/bookings are real-world confirmation.
    positives = applies + replies * 1.2 + bookings * 2.0
    negatives = ignores * 0.6 + scams * 1.5
    trust = (positives + 1.0) / (positives + negatives + 2.0)
    db.execute(
        "UPDATE source_stats SET trust_score=?, updated_at=? WHERE source_id=?",
        (round(clamp(trust, 0.02, 0.98), 4), now, source_id),
    )


# ---------------------------------------------------------------------------
# Raw documents
# ---------------------------------------------------------------------------


RAW_COLUMNS = (
    "source_id",
    "url",
    "canonical_url",
    "content_type",
    "http_status",
    "headers_json",
    "body",
    "body_text",
    "fetched_at",
    "content_hash",
    "kind",
    "meta_json",
    "compressed",
)


def insert_raw_document(
    db: Database,
    *,
    source_id: int | None,
    url: str,
    canonical_url: str,
    content_type: str | None,
    http_status: int | None,
    headers: dict[str, Any] | None,
    body: bytes | None,
    body_text: str,
    content_hash: str | None,
    kind: str,
    meta: dict[str, Any] | None,
    compressed: bool = False,
    fetched_at: str | None = None,
) -> int:
    return db.insert(
        "INSERT INTO raw_documents(source_id, url, canonical_url, content_type, http_status, "
        "headers_json, body, body_text, fetched_at, content_hash, kind, meta_json, compressed) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source_id,
            url,
            canonical_url,
            content_type,
            http_status,
            _dumps(headers),
            body,
            body_text,
            fetched_at or iso(now_utc()),
            content_hash,
            kind,
            _dumps(meta),
            int(compressed),
        ),
    )


def get_raw_document(db: Database, raw_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM raw_documents WHERE id=?", (raw_id,))
    if row:
        row["meta"] = json_loads(row.get("meta_json"), {}) or {}
        row["headers"] = json_loads(row.get("headers_json"), {}) or {}
        if row.get("compressed") and row.get("body"):
            import zlib

            try:
                row["body"] = zlib.decompress(row["body"])
            except (zlib.error, TypeError):
                pass
    return row


def recent_raw_for_url(db: Database, canonical_url: str, limit: int = 5) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM raw_documents WHERE canonical_url=? ORDER BY fetched_at DESC LIMIT ?",
        (canonical_url, limit),
    )


def delete_raw_document(db: Database, raw_id: int) -> None:
    """Drop a raw document that produced no opportunity (news pages).

    Only safe once the document has no stored opportunity; the tracked database
    is committed by the cloud runner, so keeping rejected news page bodies
    would bloat it by megabytes per scan.
    """
    if not raw_id:
        return
    db.execute("DELETE FROM raw_documents WHERE id=?", (int(raw_id),))


# ---------------------------------------------------------------------------
# Clusters / opportunities
# ---------------------------------------------------------------------------


# Statuses a person set by acting on the lead.  Re-extraction is not allowed
# to clear these.
HUMAN_STATUSES = {"applied", "replied", "booked", "ignored", "archived", "scam"}

OPPORTUNITY_COLUMNS = (
    "canonical_url",
    "canonical_hash",
    "cluster_id",
    "source_id",
    "source_type",
    "opportunity_type",
    "title",
    "description",
    "published_at",
    "first_seen_at",
    "last_seen_at",
    "deadline",
    "deadline_confidence",
    "city",
    "country",
    "remote_ok",
    "language_json",
    "role_type_json",
    "comp_type",
    "pay_amount",
    "currency",
    "fee_required",
    "usage_rights",
    "nudity",
    "travel_covered",
    "audition_format",
    "shoot_window_json",
    "age_min",
    "age_max",
    "gender_presentation",
    "contact_channel",
    "contact_value",
    "fields_evidence_json",
    "risk_flags_json",
    "trust_tier",
    "raw_document_id",
    "raw_text_hash",
    "minhash_signature",
    "structured_key",
    "score",
    "score_breakdown_json",
    "status",
    "extraction_method",
    "llm_model",
    "created_at",
    "updated_at",
)


def _opportunity_values(data: dict[str, Any], now: str) -> tuple[Any, ...]:
    canonical_url = data.get("canonical_url") or ""
    canonical_hash_value = data.get("canonical_hash") or canonical_hash(canonical_url)
    first_seen = data.get("first_seen_at") or now
    return (
        canonical_url,
        canonical_hash_value,
        data.get("cluster_id"),
        data.get("source_id"),
        data.get("source_type"),
        data.get("opportunity_type") or "other",
        data.get("title") or "Untitled opportunity",
        data.get("description"),
        data.get("published_at"),
        first_seen,
        data.get("last_seen_at") or now,
        data.get("deadline"),
        data.get("deadline_confidence") or "unknown",
        data.get("city"),
        data.get("country"),
        _bool_or_none(data.get("remote_ok")),
        _dumps(data.get("languages") or data.get("language") or []),
        _dumps(data.get("role_types") or data.get("role_type") or []),
        data.get("comp_type") or "unknown",
        data.get("pay_amount"),
        data.get("currency"),
        _bool_or_none(data.get("fee_required")),
        data.get("usage_rights") or "unknown",
        data.get("nudity") or "unknown",
        _bool_or_none(data.get("travel_covered")),
        data.get("audition_format") or "unknown",
        _dumps(data.get("shoot_window") or []),
        data.get("age_min"),
        data.get("age_max"),
        data.get("gender_presentation") or "any",
        data.get("contact_channel") or "unknown",
        data.get("contact_value"),
        _dumps(data.get("evidence") or data.get("fields_evidence") or {}),
        _dumps(data.get("risk_flags") or []),
        data.get("trust_tier") or 3,
        data.get("raw_document_id"),
        data.get("raw_text_hash"),
        data.get("minhash_signature"),
        data.get("structured_key"),
        float(data.get("score") or 0.0),
        _dumps(data.get("score_breakdown") or {}),
        data.get("status") or "new",
        data.get("extraction_method"),
        data.get("llm_model"),
        data.get("created_at") or now,
        now,
    )


def _bool_or_none(value: Any) -> int | None:
    if value is None:
        return None
    return int(bool(value))


def upsert_opportunity(db: Database, data: dict[str, Any], *, update_fields: bool = False) -> tuple[int, bool]:
    now = iso(now_utc())
    canonical_url = data.get("canonical_url") or ""
    canonical_hash_value = data.get("canonical_hash") or canonical_hash(canonical_url)
    existing = db.query_one("SELECT * FROM opportunities WHERE canonical_hash = ?", (canonical_hash_value,))
    if existing:
        if update_fields:
            # Re-extraction may report a fresh system status ("expired" for a
            # ended listing) but must never roll back what the user decided to
            # do with the lead.
            if existing["status"] in HUMAN_STATUSES:
                data = {**data, "status": existing["status"]}
            values = _opportunity_values(
                {
                    **data,
                    "created_at": existing["created_at"],
                    "first_seen_at": existing["first_seen_at"],
                },
                now,
            )
            assignments = ", ".join(f"{col}=?" for col in OPPORTUNITY_COLUMNS)
            db.execute(
                f"UPDATE opportunities SET {assignments} WHERE id=?",
                (*values, existing["id"]),
            )
        else:
            db.execute(
                "UPDATE opportunities SET last_seen_at=?, source_id=COALESCE(?, source_id), "
                "raw_document_id=COALESCE(?, raw_document_id), updated_at=? WHERE id=?",
                (now, data.get("source_id"), data.get("raw_document_id"), now, existing["id"]),
            )
        return int(existing["id"]), False

    values = _opportunity_values({**data, "canonical_hash": canonical_hash_value}, now)
    placeholders = ",".join("?" for _ in OPPORTUNITY_COLUMNS)
    opportunity_id = db.insert(
        f"INSERT INTO opportunities({','.join(OPPORTUNITY_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    return opportunity_id, True


def get_opportunity(db: Database, opportunity_id: int) -> dict[str, Any] | None:
    return decode_opportunity(db.query_one("SELECT * FROM opportunities WHERE id=?", (opportunity_id,)))


def get_opportunity_by_url(db: Database, url: str) -> dict[str, Any] | None:
    return decode_opportunity(
        db.query_one("SELECT * FROM opportunities WHERE canonical_hash=?", (canonical_hash(url),))
    )


def find_structured(db: Database, structured_key: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
    if not structured_key:
        return None
    if exclude_id:
        row = db.query_one(
            "SELECT * FROM opportunities WHERE structured_key=? AND id<>? ORDER BY score DESC LIMIT 1",
            (structured_key, exclude_id),
        )
    else:
        row = db.query_one(
            "SELECT * FROM opportunities WHERE structured_key=? ORDER BY score DESC LIMIT 1",
            (structured_key,),
        )
    return decode_opportunity(row)


def recent_for_dedup(db: Database, *, days: int = 120, limit: int = 6000) -> list[dict[str, Any]]:
    cutoff = iso(now_utc() - timedelta(days=days))
    rows = db.query(
        "SELECT id, canonical_url, structured_key, minhash_signature, title, description, city, deadline, contact_value "
        "FROM opportunities WHERE first_seen_at >= ? ORDER BY id DESC LIMIT ?",
        (cutoff, limit),
    )
    return rows


def set_cluster(db: Database, opportunity_id: int, cluster_id: int) -> None:
    db.execute(
        "UPDATE opportunities SET cluster_id=?, updated_at=? WHERE id=?",
        (cluster_id, iso(now_utc()), opportunity_id),
    )


def get_or_create_cluster(
    db: Database,
    *,
    cluster_key: str,
    title: str | None = None,
    city: str | None = None,
    deadline: str | None = None,
    contact_value: str | None = None,
) -> int:
    existing = db.query_one("SELECT id FROM clusters WHERE cluster_key=?", (cluster_key,))
    now = iso(now_utc())
    if existing:
        cluster_id = int(existing["id"])
        db.execute(
            "UPDATE clusters SET title=COALESCE(NULLIF(?, ''), title), city=COALESCE(NULLIF(?, ''), city), "
            "deadline=COALESCE(NULLIF(?, ''), deadline), contact_value=COALESCE(NULLIF(?, ''), contact_value), "
            "updated_at=? WHERE id=?",
            (title or "", city or "", deadline or "", contact_value or "", now, cluster_id),
        )
        return cluster_id
    return db.insert(
        "INSERT INTO clusters(cluster_key, title, city, deadline, contact_value, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (cluster_key, title, city, deadline, contact_value, now, now),
    )


def refresh_cluster_stats(db: Database, cluster_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM clusters WHERE id=?", (cluster_id,))
    if not row:
        return None
    stats = db.query_one(
        "SELECT COUNT(*) AS opportunity_count, COUNT(DISTINCT source_id) AS source_count FROM opportunities "
        "WHERE cluster_id=?",
        (cluster_id,),
    ) or {}
    opportunity_count = int(stats.get("opportunity_count") or 0)
    # Unknown source ids (manual/fixture imports) still count as one source;
    # a cluster with members must never report zero sources.
    source_count = max(1, int(stats.get("source_count") or 0)) if opportunity_count else 0
    rep = db.query_one(
        "SELECT id, title, city, deadline, contact_value, score FROM opportunities WHERE cluster_id=? "
        "ORDER BY score DESC, last_seen_at DESC LIMIT 1",
        (cluster_id,),
    )
    db.execute(
        "UPDATE clusters SET opportunity_count=?, source_count=?, representative_id=?, "
        "title=COALESCE(NULLIF(?, ''), title), city=COALESCE(NULLIF(?, ''), city), "
        "deadline=COALESCE(NULLIF(?, ''), deadline), contact_value=COALESCE(NULLIF(?, ''), contact_value), "
        "updated_at=? WHERE id=?",
        (
            opportunity_count,
            source_count,
            rep["id"] if rep else None,
            rep["title"] if rep else "",
            rep["city"] if rep else "",
            rep["deadline"] if rep else "",
            rep["contact_value"] if rep else "",
            iso(now_utc()),
            cluster_id,
        ),
    )
    return get_cluster(db, cluster_id)


def get_cluster(db: Database, cluster_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM clusters WHERE id=?", (cluster_id,))


def cluster_members(db: Database, cluster_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT o.*, s.name AS source_name, s.tier AS source_tier FROM opportunities o "
        "LEFT JOIN sources s ON s.id=o.source_id WHERE o.cluster_id=? ORDER BY o.score DESC",
        (cluster_id,),
    )
    return [decode_opportunity(row) for row in rows]  # type: ignore[misc]


def update_score(db: Database, opportunity_id: int, score: float, breakdown: dict[str, Any]) -> None:
    db.execute(
        "UPDATE opportunities SET score=?, score_breakdown_json=?, updated_at=? WHERE id=?",
        (round(float(score), 2), _dumps(breakdown), iso(now_utc()), opportunity_id),
    )


def update_fields(db: Database, opportunity_id: int, fields: dict[str, Any]) -> None:
    allowed = {
        "title",
        "description",
        "deadline",
        "deadline_confidence",
        "city",
        "country",
        "remote_ok",
        "language_json",
        "role_type_json",
        "comp_type",
        "pay_amount",
        "currency",
        "fee_required",
        "usage_rights",
        "nudity",
        "travel_covered",
        "audition_format",
        "shoot_window_json",
        "age_min",
        "age_max",
        "gender_presentation",
        "contact_channel",
        "contact_value",
        "fields_evidence_json",
        "risk_flags_json",
        "cluster_id",
        "structured_key",
        "status",
    }
    dirty = {k: v for k, v in fields.items() if k in allowed}
    if not dirty:
        return
    assignments = ", ".join(f"{key}=?" for key in dirty)
    db.execute(
        f"UPDATE opportunities SET {assignments}, updated_at=? WHERE id=?",
        (*dirty.values(), iso(now_utc()), opportunity_id),
    )


def set_status(db: Database, opportunity_id: int, status: str) -> None:
    db.execute(
        "UPDATE opportunities SET status=?, updated_at=? WHERE id=?",
        (status, iso(now_utc()), opportunity_id),
    )


def mark_risk_flags(db: Database, opportunity_id: int, flags: list[str]) -> None:
    db.execute(
        "UPDATE opportunities SET risk_flags_json=?, updated_at=? WHERE id=?",
        (_dumps(flags), iso(now_utc()), opportunity_id),
    )


def list_opportunities(
    db: Database,
    *,
    status: str | None = None,
    min_score: float | None = None,
    city: str | None = None,
    source_id: int | None = None,
    opportunity_type: str | None = None,
    query: str | None = None,
    risk_only: bool = False,
    limit: int = 100,
    offset: int = 0,
    order: str = "score",
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    joins = ""
    if query:
        tokens = [token for token in query.replace('"', " ").split() if token]
        if tokens:
            fts_query = " OR ".join(f'"{token}"' for token in tokens[:12])
            joins = "JOIN opportunities_fts f ON f.rowid=o.id"
            # MATCH must name the FTS table, not its alias, or SQLite reads
            # "f MATCH ?" as a comparison against a column named f.
            where.append("opportunities_fts MATCH ?")
            params.append(fts_query)
    if status:
        where.append("o.status=?")
        params.append(status)
    if min_score is not None:
        where.append("o.score >= ?")
        params.append(float(min_score))
    if city:
        where.append("LOWER(o.city) LIKE ?")
        params.append(f"%{city.lower()}%")
    if source_id:
        where.append("o.source_id=?")
        params.append(source_id)
    if opportunity_type:
        where.append("o.opportunity_type=?")
        params.append(opportunity_type)
    if risk_only:
        where.append("COALESCE(o.risk_flags_json,'[]') <> '[]'")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = {
        "score": "o.score DESC, o.deadline ASC",
        "deadline": "o.deadline ASC, o.score DESC",
        "newest": "o.first_seen_at DESC",
    }.get(order, "o.score DESC")
    sql = (
        f"SELECT o.*, s.name AS source_name, s.tier AS source_tier FROM opportunities o "
        f"LEFT JOIN sources s ON s.id=o.source_id {joins} {sql_where} "
        f"ORDER BY {order_sql} LIMIT ? OFFSET ?"
    )
    rows = db.query(sql, (*params, limit, offset))
    return [decode_opportunity(row) for row in rows]  # type: ignore[misc]


def count_opportunities(db: Database) -> dict[str, int]:
    rows = db.query("SELECT status, COUNT(*) AS n FROM opportunities GROUP BY status")
    counts = {row["status"]: int(row["n"]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts


def due_for_scoring(db: Database, *, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT o.* FROM opportunities o LEFT JOIN clusters c ON c.id=o.cluster_id "
        "WHERE o.status IN ('new','reviewed') AND (o.score_breakdown_json IS NULL OR o.score_breakdown_json='{}' "
        "OR o.score IS NULL) ORDER BY o.first_seen_at ASC LIMIT ?",
        (limit,),
    )
    return [decode_opportunity(row) for row in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Discovery queue
# ---------------------------------------------------------------------------


def enqueue(
    db: Database,
    *,
    source_id: int,
    kind: str,
    url: str,
    payload: dict[str, Any] | None = None,
    run_after: datetime | None = None,
) -> int | None:
    canonical = canonicalize_url(url)
    if canonical is None:
        if url.startswith(("email://", "fixture://", "discord://", "telegram://", "rss://", "imap://")):
            canonical = url
        else:
            log.debug("Skipping non-http URL: %s", url)
            return None
    now = iso(now_utc())
    after = iso(run_after or now_utc())
    existing = db.query_one(
        "SELECT id, status FROM discovery_queue WHERE source_id=? AND canonical_url=?",
        (source_id, canonical),
    )
    if existing:
        # Already pending/done/processing: not a new enqueue.  A failed item
        # is retried in place, keeping its id and history.
        if str(existing.get("status") or "").lower() != "failed":
            return None
        db.execute(
            "UPDATE discovery_queue SET status='pending', attempts=0, last_error=NULL, "
            "run_after=? WHERE id=?",
            (after, existing["id"]),
        )
        return int(existing["id"])
    try:
        return db.insert(
            "INSERT INTO discovery_queue(source_id, kind, url, canonical_url, payload_json, discovered_at, "
            "run_after, status) VALUES (?,?,?,?,?,?,?, 'pending')",
            (source_id, kind, url, canonical, _dumps(payload or {}), now, after),
        )
    except sqlite3.IntegrityError:
        return None


def claim_queue(db: Database, *, limit: int = 25, now: datetime | None = None) -> list[dict[str, Any]]:
    current = iso(now or now_utc())
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM discovery_queue WHERE status='pending' AND run_after <= ? "
            "ORDER BY id ASC LIMIT ?",
            (current, limit),
        ).fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            conn.executemany(
                "UPDATE discovery_queue SET status='processing', attempts=attempts+1 WHERE id=?",
                [(i,) for i in ids],
            )
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload"] = json_loads(item.get("payload_json"), {}) or {}
        result.append(item)
    return result


def mark_queue(
    db: Database,
    queue_id: int,
    status: str,
    *,
    error: str | None = None,
    retry_after: datetime | None = None,
) -> None:
    now = iso(now_utc())
    if status == "pending" and retry_after:
        db.execute(
            "UPDATE discovery_queue SET status=?, last_error=?, run_after=?, processed_at=NULL WHERE id=?",
            (status, error, iso(retry_after), queue_id),
        )
    else:
        db.execute(
            "UPDATE discovery_queue SET status=?, last_error=?, processed_at=? WHERE id=?",
            (status, error, now, queue_id),
        )


def queue_stats(db: Database) -> dict[str, int]:
    rows = db.query("SELECT status, COUNT(*) AS n FROM discovery_queue GROUP BY status")
    return {row["status"]: int(row["n"]) for row in rows}


def replay_queue(db: Database, queue_id: int) -> None:
    db.execute(
        "UPDATE discovery_queue SET status='pending', attempts=0, last_error=NULL, run_after=? WHERE id=?",
        (iso(now_utc()), queue_id),
    )


def requeue_stale_queue(db: Database, *, minutes: int = 30) -> int:
    """Return crashed ``processing`` claims to ``pending``.

    The worker is the only writer, so anything still marked processing at
    startup was interrupted by a crash or forced shutdown.  A run_after in the
    near future gives the rest of the system a moment to finish opening files.
    """
    cutoff = iso(now_utc() - timedelta(minutes=max(1, minutes)))
    cursor = db.execute(
        "UPDATE discovery_queue SET status='pending', run_after=?, "
        "last_error=COALESCE(last_error, 'recovered stale claim after restart') "
        "WHERE status='processing' AND COALESCE(processed_at, discovered_at) <= ?",
        (iso(now_utc()), cutoff),
    )
    count = int(cursor.rowcount or 0)
    if count:
        log.warning("Requeued %d stale discovery queue item(s)", count)
    return count


def list_raw_documents(db: Database, *, limit: int = 50, source_id: int | None = None) -> list[dict[str, Any]]:
    if source_id:
        rows = db.query(
            "SELECT id, source_id, url, canonical_url, content_type, http_status, fetched_at, "
            "content_hash, kind, compressed FROM raw_documents WHERE source_id=? "
            "ORDER BY fetched_at DESC LIMIT ?",
            (source_id, limit),
        )
    else:
        rows = db.query(
            "SELECT id, source_id, url, canonical_url, content_type, http_status, fetched_at, "
            "content_hash, kind, compressed FROM raw_documents ORDER BY fetched_at DESC LIMIT ?",
            (limit,),
        )
    return rows


def list_queue(db: Database, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if status:
        return db.query(
            "SELECT * FROM discovery_queue WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        )
    return db.query("SELECT * FROM discovery_queue ORDER BY id DESC LIMIT ?", (limit,))


def get_queue_item(db: Database, queue_id: int) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM discovery_queue WHERE id=?", (queue_id,))
    if row:
        row["payload"] = json_loads(row.get("payload_json"), {}) or {}
    return row


# ---------------------------------------------------------------------------
# Sitemap / hubs / Wayback
# ---------------------------------------------------------------------------


def sitemap_check_and_store(
    db: Database,
    *,
    domain: str,
    url: str,
    lastmod: str | None,
    content_hash: str | None = None,
) -> bool:
    """Return True when the URL is new or changed since last seen."""
    existing = db.query_one("SELECT * FROM sitemap_urls WHERE domain=? AND url=?", (domain, url))
    now = iso(now_utc())
    if not existing:
        db.execute(
            "INSERT INTO sitemap_urls(domain, url, lastmod, content_hash, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?)",
            (domain, url, lastmod, content_hash, now, now),
        )
        return True
    changed = (lastmod and lastmod != existing.get("lastmod")) or (
        content_hash and content_hash != existing.get("content_hash")
    )
    db.execute(
        "UPDATE sitemap_urls SET lastmod=?, content_hash=?, last_seen_at=? WHERE domain=? AND url=?",
        (lastmod or existing.get("lastmod"), content_hash or existing.get("content_hash"), now, domain, url),
    )
    return bool(changed)


def add_hub_candidate(
    db: Database,
    *,
    domain: str,
    link_count: int,
    example_url: str,
    source_id: int | None = None,
    auto_approve: bool = False,
) -> str:
    now = iso(now_utc())
    status = "approved" if auto_approve else "pending"
    existing = db.query_one("SELECT * FROM hub_candidates WHERE domain=?", (domain,))
    if existing:
        new_status = status if existing["status"] == "pending" and auto_approve else existing["status"]
        db.execute(
            "UPDATE hub_candidates SET link_count=MAX(link_count, ?), example_url=?, status=?, updated_at=? "
            "WHERE domain=?",
            (link_count, example_url, new_status, now, domain),
        )
        return str(new_status)
    db.execute(
        "INSERT INTO hub_candidates(domain, link_count, example_url, source_id, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (domain, link_count, example_url, source_id, status, now, now),
    )
    return status


def list_hubs(db: Database, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        return db.query("SELECT * FROM hub_candidates WHERE status=? ORDER BY link_count DESC", (status,))
    return db.query("SELECT * FROM hub_candidates ORDER BY link_count DESC")


def set_hub_status(db: Database, domain: str, status: str) -> None:
    db.execute(
        "UPDATE hub_candidates SET status=?, updated_at=? WHERE domain=?",
        (status, iso(now_utc()), domain),
    )


def save_cdx_run(
    db: Database,
    *,
    domain: str,
    url_pattern: str,
    resume_key: str | None,
    pages_done: int,
    urls_seen: int,
    status: str,
    error: str | None = None,
) -> None:
    db.execute(
        "INSERT INTO cdx_runs(domain, url_pattern, resume_key, pages_done, urls_seen, last_run_at, status, error) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (domain, url_pattern, resume_key, pages_done, urls_seen, iso(now_utc()), status, error),
    )


def last_cdx_run(db: Database, domain: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM cdx_runs WHERE domain=? ORDER BY id DESC LIMIT 1", (domain,)
    )


def email_seen(db: Database, message_id: str) -> bool:
    return db.query_one("SELECT message_id FROM email_messages WHERE message_id=?", (message_id,)) is not None


def mark_email(
    db: Database,
    *,
    message_id: str,
    source_id: int | None,
    uid: str | None,
    subject: str | None,
    sender: str | None,
    received_at: str | None,
    status: str = "processed",
) -> None:
    now = iso(now_utc())
    db.execute(
        "INSERT INTO email_messages(message_id, source_id, uid, subject, sender, received_at, processed_at, status) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(message_id) DO UPDATE SET processed_at=excluded.processed_at, status=excluded.status",
        (message_id, source_id, uid, subject, sender, received_at, now, status),
    )


# ---------------------------------------------------------------------------
# Labels / reputation / vetoes / quarantine
# ---------------------------------------------------------------------------


def insert_label(
    db: Database,
    *,
    opportunity_id: int,
    action: str,
    previous_status: str | None,
    new_status: str | None,
    score: float | None,
    features: dict[str, Any] | None,
    note: str | None = None,
) -> int:
    return db.insert(
        "INSERT INTO labels(opportunity_id, action, previous_status, new_status, score_at_decision, "
        "features_json, note, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            opportunity_id,
            action,
            previous_status,
            new_status,
            score,
            _dumps(features or {}),
            note,
            iso(now_utc()),
        ),
    )


def list_labels(db: Database, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT l.*, o.title, o.city, o.source_id FROM labels l JOIN opportunities o ON o.id=l.opportunity_id "
        "ORDER BY l.created_at DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["features"] = json_loads(row.get("features_json"), {}) or {}
    return rows


def label_counts(db: Database) -> dict[str, int]:
    rows = db.query("SELECT action, COUNT(*) AS n FROM labels GROUP BY action")
    return {row["action"]: int(row["n"]) for row in rows}


def training_rows(db: Database) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT l.action, l.features_json, l.score_at_decision, o.source_id, s.tier AS source_tier "
        "FROM labels l JOIN opportunities o ON o.id=l.opportunity_id "
        "LEFT JOIN sources s ON s.id=o.source_id ORDER BY l.id ASC"
    )
    for row in rows:
        row["features"] = json_loads(row.get("features_json"), {}) or {}
    return rows


def add_reputation(
    db: Database,
    *,
    kind: str,
    value: str,
    reason: str,
    opportunity_id: int | None = None,
) -> bool:
    kind = (kind or "").lower().strip()
    value = (value or "").strip().lower()
    if not kind or not value:
        return False
    now = iso(now_utc())
    try:
        db.execute(
            "INSERT INTO reputation(kind, value, reason, source_opportunity_id, hits, created_at, last_seen_at) "
            "VALUES (?,?,?,?,0,?,?) ON CONFLICT(kind, value) DO UPDATE SET hits=hits+1, last_seen_at=excluded.last_seen_at",
            (kind, value, reason, opportunity_id, now, now),
        )
        return True
    except sqlite3.Error as exc:
        log.warning("Could not add reputation %s=%s: %s", kind, value, exc)
        return False


def reputation_entries(db: Database, limit: int = 500) -> list[dict[str, Any]]:
    return db.query("SELECT * FROM reputation ORDER BY created_at DESC LIMIT ?", (limit,))


def reputation_match(db: Database, values: Iterable[tuple[str, str]]) -> dict[str, Any] | None:
    for kind, value in values:
        if not value:
            continue
        row = db.query_one(
            "SELECT * FROM reputation WHERE kind=? AND value=?",
            (kind, value.lower()),
        )
        if row:
            db.execute(
                "UPDATE reputation SET hits=hits+1, last_seen_at=? WHERE id=?",
                (iso(now_utc()), row["id"]),
            )
            return row
    return None


def values_from_opportunity(opp: dict[str, Any]) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    contact = (opp.get("contact_value") or "").strip()
    if contact:
        if "@" in contact:
            values.append(("email", contact.lower()))
            domain = contact_domain(contact)
            if domain:
                values.append(("domain", domain))
        elif contact.startswith("+"):
            values.append(("phone", contact))
        else:
            values.append(("contact", contact.lower()))
            domain = contact_domain(contact)
            if domain:
                values.append(("domain", domain))
    domain = registrable_domain(domain_from_url(opp.get("canonical_url")))
    if domain:
        values.append(("domain", domain))
    text = f"{opp.get('title','')} {opp.get('description','')}"
    for email in extract_emails(text):
        values.append(("email", email))
        d = contact_domain(email)
        if d:
            values.append(("domain", d))
    return list(dict.fromkeys(values))


def log_veto(
    db: Database,
    *,
    opportunity_id: int | None,
    cluster_id: int | None,
    stage: str,
    rule_id: str | None,
    reason: str,
    detail: str | None = None,
) -> int:
    return db.insert(
        "INSERT INTO veto_log(opportunity_id, cluster_id, stage, rule_id, reason, detail, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (opportunity_id, cluster_id, stage, rule_id, reason, detail, iso(now_utc())),
    )


def list_vetoes(db: Database, *, days: int = 7, limit: int = 100) -> list[dict[str, Any]]:
    cutoff = iso(now_utc() - timedelta(days=days))
    return db.query(
        "SELECT v.*, o.title, o.city, o.canonical_url, o.score FROM veto_log v "
        "LEFT JOIN opportunities o ON o.id=v.opportunity_id WHERE v.created_at>=? "
        "ORDER BY v.created_at DESC LIMIT ?",
        (cutoff, limit),
    )


def add_quarantine(
    db: Database,
    *,
    raw_document_id: int | None,
    source_id: int | None,
    url: str | None,
    errors: list[str] | dict[str, Any],
    payload: dict[str, Any] | None,
) -> int:
    return db.insert(
        "INSERT INTO quarantine(raw_document_id, source_id, url, errors_json, payload_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (raw_document_id, source_id, url, _dumps(errors), _dumps(payload or {}), iso(now_utc())),
    )


def list_quarantine(db: Database, *, open_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
    where = "WHERE resolved_at IS NULL" if open_only else ""
    rows = db.query(
        f"SELECT q.*, s.name AS source_name FROM quarantine q LEFT JOIN sources s ON s.id=q.source_id "
        f"{where} ORDER BY q.created_at DESC LIMIT ?",
        (limit,),
    )
    for row in rows:
        row["errors"] = json_loads(row.get("errors_json"), []) or []
        row["payload"] = json_loads(row.get("payload_json"), {}) or {}
    return rows


def resolve_quarantine(db: Database, quarantine_id: int, resolution: str) -> None:
    db.execute(
        "UPDATE quarantine SET resolved_at=?, resolution=? WHERE id=?",
        (iso(now_utc()), resolution, quarantine_id),
    )


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


def alert_exists(db: Database, dedup_key: str) -> bool:
    return db.query_one("SELECT id FROM alerts WHERE dedup_key=?", (dedup_key,)) is not None


def count_alerts_between(db: Database, start: datetime, end: datetime, kind: str | None = None) -> int:
    sql = "SELECT COUNT(*) AS n FROM alerts WHERE sent_at >= ? AND sent_at < ?"
    params: list[Any] = [iso(start), iso(end)]
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    row = db.query_one(sql, params)
    return int(row["n"]) if row else 0


def count_instant_today(db: Database) -> int:
    today = now_ist().date()
    start = datetime(today.year, today.month, today.day, tzinfo=IST)
    end = start + timedelta(days=1)
    return count_alerts_between(db, start, end, "instant")


def insert_alert(
    db: Database,
    *,
    opportunity_id: int,
    kind: str,
    topic: str | None,
    message_id: str | None,
    title: str,
    body: str,
    score: float,
    dedup_key: str,
) -> int | None:
    now = iso(now_utc())
    try:
        return db.insert(
            "INSERT INTO alerts(opportunity_id, kind, channel, topic, message_id, title, body, score, "
            "dedup_key, sent_at) VALUES (?,?, 'ntfy', ?,?,?,?,?,?,?)",
            (opportunity_id, kind, topic, message_id, title, body, score, dedup_key, now),
        )
    except sqlite3.IntegrityError:
        return None


def alert_history(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    return db.query(
        "SELECT a.*, o.title AS opportunity_title, o.canonical_url, o.city FROM alerts a "
        "JOIN opportunities o ON o.id=a.opportunity_id ORDER BY a.sent_at DESC LIMIT ?",
        (limit,),
    )


def record_alert_response(db: Database, opportunity_id: int, action: str) -> int:
    return db.execute(
        "UPDATE alerts SET response=?, responded_at=? WHERE opportunity_id=? AND response IS NULL",
        (action, iso(now_utc()), opportunity_id),
    ).rowcount


def seen_reply(db: Database, message_id: str) -> bool:
    return db.query_one("SELECT message_id FROM reply_messages WHERE message_id=?", (message_id,)) is not None


def store_reply(
    db: Database,
    *,
    message_id: str,
    topic: str | None,
    received_at: str | None,
    payload: dict[str, Any],
    processed: bool = False,
    result: str | None = None,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO reply_messages(message_id, topic, received_at, payload_json, processed_at, result) "
        "VALUES (?,?,?,?,?,?)",
        (
            message_id,
            topic,
            received_at or iso(now_utc()),
            _dumps(payload),
            iso(now_utc()) if processed else None,
            result,
        ),
    )


# ---------------------------------------------------------------------------
# Embeddings / settings / jobs / metrics
# ---------------------------------------------------------------------------


def store_embedding(db: Database, opportunity_id: int, model: str, vector: bytes, dim: int) -> None:
    db.execute(
        "INSERT INTO embeddings(opportunity_id, model, dim, vector, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(opportunity_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
        "vector=excluded.vector, updated_at=excluded.updated_at",
        (opportunity_id, model, dim, vector, iso(now_utc())),
    )


def get_embedding(db: Database, opportunity_id: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT model, dim, vector, updated_at FROM embeddings WHERE opportunity_id=?",
        (opportunity_id,),
    )


def embedding_coverage(db: Database) -> dict[str, int]:
    total = int(db.scalar("SELECT COUNT(*) FROM opportunities", default=0) or 0)
    embedded = int(db.scalar("SELECT COUNT(*) FROM embeddings", default=0) or 0)
    return {"total": total, "embedded": embedded, "missing": max(0, total - embedded)}


def job_last_run(db: Database, name: str) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM job_runs WHERE name=?", (name,))


def record_job_run(db: Database, name: str, *, status: str, error: str | None = None) -> None:
    now = iso(now_utc())
    db.execute(
        "INSERT INTO job_runs(name, last_run_at, last_status, last_error, run_count, updated_at) "
        "VALUES (?,?,?,?,1,?) ON CONFLICT(name) DO UPDATE SET last_run_at=excluded.last_run_at, "
        "last_status=excluded.last_status, last_error=excluded.last_error, run_count=run_count+1, "
        "updated_at=excluded.updated_at",
        (name, now, status, error, now),
    )


def save_weekly_report(db: Database, week_start_value: str, metrics: dict[str, Any]) -> None:
    db.execute(
        "INSERT INTO weekly_reports(week_start, metrics_json, created_at) VALUES (?,?,?) "
        "ON CONFLICT(week_start) DO UPDATE SET metrics_json=excluded.metrics_json, created_at=excluded.created_at",
        (week_start_value, _dumps(metrics), iso(now_utc())),
    )


def weekly_reports(db: Database, limit: int = 12) -> list[dict[str, Any]]:
    rows = db.query("SELECT * FROM weekly_reports ORDER BY week_start DESC LIMIT ?", (limit,))
    for row in rows:
        row["metrics"] = json_loads(row.get("metrics_json"), {}) or {}
    return rows


def domain_intel_get(db: Database, domain: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM domain_intel WHERE domain=?", (domain,))
    if row and row.get("expires_at"):
        expires = parse_dt(row["expires_at"])
        if expires and expires > now_utc():
            return row
    return None


def domain_intel_put(
    db: Database,
    *,
    domain: str,
    status: str,
    age_days: float | None,
    registrar: str | None,
    raw: dict[str, Any] | None,
    ttl_days: int = 30,
) -> None:
    now = now_utc()
    db.execute(
        "INSERT INTO domain_intel(domain, created_at, expires_at, age_days, registrar, status, raw_json, checked_at) "
        "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(domain) DO UPDATE SET created_at=excluded.created_at, "
        "expires_at=excluded.expires_at, age_days=excluded.age_days, registrar=excluded.registrar, "
        "status=excluded.status, raw_json=excluded.raw_json, checked_at=excluded.checked_at",
        (
            domain,
            iso(now),
            iso(now + timedelta(days=ttl_days)),
            age_days,
            registrar,
            status,
            _dumps(raw or {}),
            iso(now),
        ),
    )


def random_vetoed(db: Database, *, days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    rows = list_vetoes(db, days=days, limit=500)
    random.shuffle(rows)
    return rows[:limit]
