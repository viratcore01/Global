"""SQLite access, migrations, backups. One writer process, many readers."""

from __future__ import annotations

import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .util import get_logger, now_utc, iso, json_dumps, json_loads

log = get_logger(__name__)


class DatabaseError(RuntimeError):
    pass


class Database:
    """Thin, thread-safe SQLite wrapper.

    The worker owns the write connection. The dashboard opens the same file in
    read-only mode. WAL lets readers continue while the worker writes.
    """

    def __init__(self, path: Path | str, *, readonly: bool = False, migrate: bool = True):
        self.path = Path(path) if str(path) != ":memory:" else Path(":memory:")
        self.readonly = readonly
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._closed = False
        if self.path != Path(":memory:"):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._open()
        if migrate and not readonly:
            self.migrate()

    # -- connection --------------------------------------------------------
    def _open(self) -> None:
        if self.readonly:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, timeout=30, check_same_thread=False)
        else:
            self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if not self.readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA temp_store=MEMORY")
            self._conn.execute("PRAGMA cache_size=-20000")
        self._conn.execute("PRAGMA query_only=OFF")

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise DatabaseError("database is closed")
        return self._conn

    # -- migrations --------------------------------------------------------
    def migrate(self, migrations_dir: Path | None = None) -> list[str]:
        if self.readonly:
            return []
        directory = migrations_dir or Path(__file__).resolve().parent / "migrations"
        applied: list[str] = []
        with self._lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            self.conn.commit()
            done = {
                int(row[0])
                for row in self.conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            for path in sorted(directory.glob("*.sql")):
                version_text = path.stem.split("_", 1)[0]
                try:
                    version = int(version_text)
                except ValueError:
                    log.warning("Skipping migration with non-numeric prefix: %s", path.name)
                    continue
                if version in done:
                    continue
                sql = path.read_text(encoding="utf-8")
                try:
                    self.conn.executescript(sql)
                    self.conn.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, path.name, iso(now_utc())),
                    )
                    self.conn.commit()
                    applied.append(path.name)
                    log.info("Applied migration %s", path.name)
                except sqlite3.Error as exc:
                    self.conn.rollback()
                    raise DatabaseError(f"Migration {path.name} failed: {exc}") from exc
        return applied

    # -- basic helpers -----------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.executemany(sql, rows)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if not row:
            return default
        return next(iter(row.values()), default)

    def insert(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        cur = self.execute(sql, params)
        return int(cur.lastrowid or 0)

    def table_exists(self, name: str) -> bool:
        row = self.query_one(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        )
        return row is not None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise

    # -- settings ----------------------------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, "" if value is None else str(value), iso(now_utc())),
        )

    def get_json_setting(self, key: str, default: Any = None) -> Any:
        return json_loads(self.get_setting(key), default)

    def set_json_setting(self, key: str, value: Any) -> None:
        self.set_setting(key, json_dumps(value))

    # -- backups -----------------------------------------------------------
    def backup(self, destination: Path | str, *, verify: bool = True) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.path == Path(":memory:"):
            raise DatabaseError("cannot back up an in-memory database")
        target = sqlite3.connect(destination)
        try:
            with self._lock:
                self.conn.backup(target)
            target.commit()
        finally:
            target.close()
        if verify:
            check = sqlite3.connect(destination)
            try:
                result = check.execute("PRAGMA integrity_check").fetchone()
            finally:
                check.close()
            if not result or result[0] != "ok":
                raise DatabaseError(f"backup integrity check failed: {result!r}")
        return destination

    def integrity_check(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def optimize(self) -> None:
        with self._lock:
            self.conn.execute("PRAGMA optimize")
            self.conn.execute("INSERT INTO opportunities_fts(opportunities_fts) VALUES('optimize')")
            self.conn.commit()

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            if self._conn is not None and not self._closed:
                self._conn.close()
                self._closed = True
                self._conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def open_reader(path: Path | str) -> Database:
    return Database(path, readonly=True, migrate=False)


def backup_database(db_path: Path | str, backup_dir: Path | str, *, prefix: str = "radar") -> Path:
    """Create a dated snapshot and prune old ones by prefix."""
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Timestamp (not just date) so manual snapshots and the nightly job never
    # overwrite each other; restores need a specific point in time.
    stamp = now_utc().astimezone().strftime("%Y-%m-%d-%H%M%S")
    destination = backup_dir / f"{prefix}-{stamp}.db"
    suffix = 2
    while destination.exists():
        destination = backup_dir / f"{prefix}-{stamp}-{suffix}.db"
        suffix += 1
    # Open a short-lived connection so the caller's writer is not disturbed.
    src = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(destination)
        try:
            src.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        src.close()
    check = sqlite3.connect(destination)
    try:
        result = check.execute("PRAGMA integrity_check").fetchone()
    finally:
        check.close()
    if not result or result[0] != "ok":
        raise DatabaseError(f"snapshot integrity check failed: {result!r}")
    return destination


def prune_backups(backup_dir: Path | str, retention_days: int, *, prefix: str = "radar") -> list[Path]:
    import re
    from datetime import datetime, timedelta

    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return []
    cutoff = now_utc().date() - timedelta(days=retention_days)
    removed: list[Path] = []
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d{{4}}-\d{{2}}-\d{{2}})(?:-\d{{6}}(?:-\d+)?)?\.db$")
    for path in backup_dir.glob(f"{prefix}-*.db"):
        match = pattern.match(path.name)
        if not match:
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        if day < cutoff:
            try:
                path.unlink()
                removed.append(path)
            except OSError as exc:
                log.warning("Could not remove old backup %s: %s", path, exc)
    return removed
