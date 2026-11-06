"""Snapshot, verify and restore behaviour."""

from __future__ import annotations

from pathlib import Path

from radar import repo
from radar.backup import list_backups, restore, snapshot, verify
from radar.config import AppConfig
from radar.db import Database


def test_snapshot_verify_restore(config, db, tmp_path: Path) -> None:
    db.set_setting("marker", "before-backup")
    result = snapshot(config, db)
    assert Path(result["path"]).exists()
    assert result["bytes"] > 0

    checked = verify(result["path"])
    assert checked["ok"], checked
    assert checked["integrity"] == "ok"
    assert checked["schema_version"] >= 2

    backups = list_backups(config)
    assert backups and backups[0]["name"].endswith(".db")

    # Restore into a separate config/database so the live one is untouched.
    from tests.conftest import make_paths

    other = AppConfig.load(make_paths(tmp_path / "restore"))
    restored = restore(other, result["path"], force=True)
    assert restored["ok"], restored
    database = Database(other.paths.db_path)
    try:
        assert database.get_setting("marker") == "before-backup"
        assert database.integrity_check() == "ok"
    finally:
        database.close()


def test_restore_refuses_unverified_file(config, tmp_path: Path) -> None:
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(b"not a database")
    result = restore(config, bogus, force=True)
    assert result["ok"] is False
    assert "verification" in result["error"]


def test_restore_requires_force_when_target_exists(config, db, tmp_path: Path) -> None:
    db.set_setting("marker", "x")
    snapshot_result = snapshot(config, db)
    blocked = restore(config, snapshot_result["path"], force=False)
    assert blocked["ok"] is False
    assert "force" in blocked["error"]


def test_verify_missing_file(tmp_path: Path) -> None:
    result = verify(tmp_path / "missing.db")
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_snapshot_named_by_timestamp(config, db) -> None:
    first = snapshot(config, db)["path"]
    second = snapshot(config, db)["path"]
    assert first != second
    assert Path(first).name.startswith("radar-")
