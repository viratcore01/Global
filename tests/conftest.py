"""Shared fixtures for the radar test suite.

Tests never touch the real database or the network.  Every test gets a fresh
SQLite file in ``tmp_path`` and a config rooted at the project's real
``config/`` directory.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture()
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture()
def demo_items() -> list[dict[str, Any]]:
    payload = json.loads((FIXTURES / "demo_items.json").read_text(encoding="utf-8"))
    return payload["items"]


def make_paths(tmp_path: Path) -> Any:
    from radar.config import Paths

    data_dir = tmp_path / "data"
    paths = Paths(
        root=PROJECT_ROOT,
        config_dir=PROJECT_ROOT / "config",
        data_dir=data_dir,
        db_path=data_dir / "radar.db",
        backup_dir=data_dir / "backups",
        log_dir=data_dir / "logs",
        tmp_dir=data_dir / "tmp",
        exports_dir=data_dir / "exports",
        migrations_dir=PROJECT_ROOT / "radar" / "migrations",
    )
    paths.ensure()
    return paths


@pytest.fixture()
def config(tmp_path: Path):
    """Real profile/veto rules, isolated data paths, deterministic settings."""
    from radar.config import AppConfig

    cfg = AppConfig.load(make_paths(tmp_path))
    cfg.settings.llm_provider = "none"
    cfg.settings.llm_api_key = ""
    cfg.settings.ntfy_topic = ""
    cfg.settings.ntfy_reply_topic = ""
    cfg.settings.instant_threshold = 80.0
    cfg.settings.digest_threshold = 55.0
    cfg.settings.enable_browser = False
    cfg.settings.enable_ocr = False
    cfg.settings.rdap_lookup = False
    return cfg


@pytest.fixture()
def db(config):
    from radar.db import Database

    database = Database(config.paths.db_path)
    try:
        yield database
    finally:
        database.close()


@pytest.fixture()
def pipeline(config, db):
    from radar.pipeline import Pipeline

    pipe = Pipeline(config, db)
    try:
        yield pipe
    finally:
        pipe.close()


@pytest.fixture()
def demo_home(tmp_path: Path, monkeypatch):
    """A temporary RADAR_HOME with the real config but its own database."""
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    for name in ("profile.yaml", "sources.yaml", "veto_patterns.yaml"):
        shutil.copy2(PROJECT_ROOT / "config" / name, home / "config" / name)
    (home / "tests" / "fixtures").mkdir(parents=True)
    shutil.copy2(FIXTURES / "demo_items.json", home / "tests" / "fixtures" / "demo_items.json")
    monkeypatch.setenv("RADAR_HOME", str(home))
    monkeypatch.setenv("RADAR_DB", str(home / "data" / "radar.db"))
    for key in (
        "NTFY_TOPIC",
        "NTFY_REPLY_TOPIC",
        "NTFY_TOKEN",
        "RADAR_REPLY_SECRET",
        "IMAP_ENABLED",
        "LLM_API_KEY",
        "HEALTHCHECKS_URL",
        "SEARXNG_ENABLED",
        "RADAR_ENABLE_BROWSER",
        "RADAR_ENABLE_OCR",
    ):
        monkeypatch.delenv(key, raising=False)
    return home


@pytest.fixture()
def clean_env(monkeypatch):
    """Keep ambient developer environment variables out of assertions."""
    for key in list(os.environ):
        if key.startswith(("RADAR_", "NTFY_", "IMAP_", "DISCORD_", "LLM_", "SEARXNG_")):
            monkeypatch.delenv(key, raising=False)
    return monkeypatch
