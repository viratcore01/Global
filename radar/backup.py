"""Nightly snapshots, verification and restore."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from .config import AppConfig
from .db import Database, DatabaseError, backup_database, prune_backups
from .util import get_logger, iso, now_utc

log = get_logger(__name__)


def snapshot(config: AppConfig, db: Database, *, prefix: str = "radar") -> dict[str, Any]:
    destination = backup_database(config.paths.db_path, config.paths.backup_dir, prefix=prefix)
    removed = prune_backups(config.paths.backup_dir, config.settings.backup_retention_days, prefix=prefix)
    db.set_setting("last_backup_at", iso(now_utc()))
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "removed": [str(path) for path in removed],
    }


def verify(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": f"backup not found: {path}"}
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            counts: dict[str, int] = {}
            for table in ("opportunities", "sources", "labels", "alerts"):
                if table in tables:
                    counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            migration = 0
            if "schema_migrations" in tables:
                migration = int(
                    conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
                )
        finally:
            conn.close()
        ok = bool(integrity and integrity[0] == "ok")
        return {
            "ok": ok,
            "path": str(path),
            "integrity": integrity[0] if integrity else "unknown",
            "counts": counts,
            "schema_version": migration,
            "bytes": path.stat().st_size,
        }
    except sqlite3.Error as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}


def restore(config: AppConfig, source: Path | str, *, force: bool = False) -> dict[str, Any]:
    """Restore a verified snapshot.

    The current database is first moved aside, never deleted.  Stop the worker
    before restoring.
    """
    source = Path(source)
    check = verify(source)
    if not check.get("ok"):
        return {"ok": False, "error": "backup failed verification", "verify": check}
    target = config.paths.db_path
    if target.exists() and not force:
        return {
            "ok": False,
            "error": "target exists; pass --force after stopping the worker",
            "target": str(target),
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        previous = target.with_name(target.name + ".pre-restore-" + now_utc().strftime("%Y%m%d%H%M%S"))
        shutil.move(str(target), str(previous))
    shutil.copy2(source, target)
    # Remove stale WAL/SHM from the old database so SQLite does not mix them.
    for suffix in ("-wal", "-shm"):
        stale = target.with_name(target.name + suffix)
        try:
            stale.unlink()
        except FileNotFoundError:
            pass
    return {"ok": True, "target": str(target), "verify": check}


def list_backups(config: AppConfig) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in sorted(config.paths.backup_dir.glob("radar-*.db"), reverse=True):
        stat = path.stat()
        result.append(
            {
                "path": str(path),
                "name": path.name,
                "bytes": stat.st_size,
                "modified": iso(now_utc().fromtimestamp(stat.st_mtime, tz=now_utc().tzinfo)),
            }
        )
    return result
