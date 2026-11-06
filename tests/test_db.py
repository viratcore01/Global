"""SQLite schema, migrations, settings, FTS and backups."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from radar import repo
from radar.db import Database, DatabaseError, backup_database, prune_backups

EXPECTED_TABLES = {
    "raw_documents",
    "opportunities",
    "clusters",
    "sources",
    "source_health",
    "labels",
    "reputation",
    "veto_log",
    "quarantine",
    "alerts",
    "cdx_runs",
    "discovery_queue",
    "sitemap_urls",
    "hub_candidates",
    "embeddings",
    "source_stats",
    "reply_messages",
    "job_runs",
    "domain_intel",
    "weekly_reports",
    "settings",
    "opportunities_fts",
}


def test_migrations_create_schema(db: Database) -> None:
    tables = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {sorted(missing)}"
    assert db.table_exists("opportunities_fts")
    assert db.integrity_check() == "ok"
    assert str(db.scalar("PRAGMA journal_mode")).lower() == "wal"
    assert int(db.scalar("SELECT COALESCE(MAX(version),0) FROM schema_migrations")) >= 2


def test_migrations_are_idempotent(db: Database) -> None:
    assert db.migrate() == []


def test_settings_roundtrip(db: Database) -> None:
    assert db.get_setting("missing", "fallback") == "fallback"
    db.set_setting("hello", "world")
    assert db.get_setting("hello") == "world"
    db.set_json_setting("payload", {"a": [1, 2, 3]})
    assert db.get_json_setting("payload") == {"a": [1, 2, 3]}


def test_transaction_rolls_back(db: Database) -> None:
    db.set_setting("tx", "before")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute("UPDATE settings SET value='inside' WHERE key='tx'")
            raise RuntimeError("boom")
    assert db.get_setting("tx") == "before"


def test_readonly_connection(tmp_path: Path) -> None:
    path = tmp_path / "read.db"
    writer = Database(path)
    writer.set_setting("k", "v")
    writer.close()
    reader = Database(path, readonly=True, migrate=False)
    try:
        assert reader.get_setting("k") == "v"
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("INSERT INTO settings(key, value) VALUES ('x','y')")
    finally:
        reader.close()


def test_backup_and_prune(tmp_path: Path) -> None:
    path = tmp_path / "radar.db"
    database = Database(path)
    database.set_setting("k", "v")
    backup = backup_database(path, tmp_path / "backups", prefix="radar")
    database.close()
    assert backup.exists()
    conn = sqlite3.connect(backup)
    try:
        assert conn.execute("SELECT value FROM settings WHERE key='k'").fetchone()[0] == "v"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
    second = backup_database(path, tmp_path / "backups", prefix="radar")
    assert second != backup, "snapshots must not overwrite each other"
    removed = prune_backups(tmp_path / "backups", retention_days=-1, prefix="radar")
    assert removed


def test_repo_enqueue_and_queue_stats(db: Database) -> None:
    source_id = repo.add_source(
        db,
        __import__("radar.config", fromlist=["SourceConfig"]).SourceConfig(
            name="Queue source", type="web", url="https://queue.example", enabled=False
        ),
    )
    first = repo.enqueue(db, source_id=source_id, kind="page", url="https://queue.example/1")
    second = repo.enqueue(db, source_id=source_id, kind="page", url="https://queue.example/1")
    assert first is not None
    assert second is None, "duplicate pending queue URLs must not be enqueued"
    assert repo.queue_stats(db)["pending"] == 1
    claimed = repo.claim_queue(db, limit=5)
    assert len(claimed) == 1
    assert claimed[0]["url"] == "https://queue.example/1"
    # A failed item is retried in place rather than duplicated.
    repo.mark_queue(db, int(claimed[0]["id"]), "failed", error="boom")
    retried = repo.enqueue(db, source_id=source_id, kind="page", url="https://queue.example/1")
    assert retried == int(claimed[0]["id"])
    assert repo.queue_stats(db)["pending"] == 1


def test_quarantine_roundtrip(db: Database) -> None:
    quarantine_id = repo.add_quarantine(
        db,
        raw_document_id=None,
        source_id=None,
        url="https://bad.example/job",
        errors=["title missing"],
        payload={"title": "broken"},
    )
    assert quarantine_id
    rows = repo.list_quarantine(db, limit=10)
    assert rows and rows[0]["id"] == quarantine_id
    repo.resolve_quarantine(db, quarantine_id, "fixed")
    assert repo.list_quarantine(db, open_only=True, limit=10) == []


def test_reputation_match(db: Database) -> None:
    assert repo.add_reputation(db, kind="email", value="scam@example.com", reason="user marked scam")
    match = repo.reputation_match(db, [("email", "scam@example.com")])
    assert match and match["kind"] == "email"
