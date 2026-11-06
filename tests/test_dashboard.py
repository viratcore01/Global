"""Headless tests for the Streamlit dashboard.

``streamlit.testing.v1.AppTest`` runs the real ``dashboard/app.py`` without a
server, so every page is exercised against a seeded database, an empty
database and a missing database.  The app swallows per-page exceptions and
renders them as ``<page> failed: ...`` errors, so both failure modes are
asserted on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP = PROJECT_ROOT / "dashboard" / "app.py"
PAGES = ["Overview", "Opportunities", "Detail", "Sources", "Quarantine", "Metrics"]


def _open_db():
    from radar.config import AppConfig, Paths
    from radar.db import Database

    paths = Paths.discover()
    config = AppConfig.load(paths)
    return config, Database(paths.db_path)


def _seed(db, config) -> None:
    from radar.jobs import seed_demo
    from radar.pipeline import Pipeline

    pipe = Pipeline(config, db)
    try:
        result = seed_demo(config, db, pipe)
        assert result["ok"], result
        assert result.get("ingested"), result
    finally:
        pipe.close()


def _run() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=120).run()


def _assert_pages_ok(at: AppTest) -> None:
    assert not at.exception, [str(item.value) for item in at.exception]
    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page}: {[str(item.value) for item in at.exception]}"
        failures = [item.value for item in at.error if "failed:" in str(item.value)]
        assert not failures, f"{page} raised: {failures}"


def test_dashboard_renders_every_page_with_data(demo_home) -> None:
    config, db = _open_db()
    try:
        _seed(db, config)
    finally:
        db.close()

    at = _run()
    _assert_pages_ok(at)

    at.sidebar.radio[0].set_value("Overview").run()
    metrics = {metric.label: metric.value for metric in at.metric}
    assert int(metrics["Opportunities"]) > 0
    assert int(metrics["Quarantine open"]) == 0

    at.sidebar.radio[0].set_value("Detail").run()
    assert at.subheader, "detail page should show a selected opportunity"
    assert any("Evidence" == header.value for header in at.subheader)


def test_dashboard_renders_every_page_with_empty_database(demo_home) -> None:
    from radar.config import Paths
    from radar.db import Database

    Database(Paths.discover().db_path).close()  # migrations only, no rows

    at = _run()
    _assert_pages_ok(at)

    # Empty states are explained instead of rendering a blank page.
    at.sidebar.radio[0].set_value("Opportunities").run()
    assert any("Nothing matches" in str(item.value) for item in at.info)
    at.sidebar.radio[0].set_value("Sources").run()
    assert any("No sources synced" in str(item.value) for item in at.info)
    at.sidebar.radio[0].set_value("Quarantine").run()
    assert any("Quarantine is empty" in str(item.value) for item in at.success)

    at.sidebar.radio[0].set_value("Overview").run()
    metrics = {metric.label: metric.value for metric in at.metric}
    assert int(metrics["Opportunities"]) == 0


def test_dashboard_explains_missing_database(demo_home) -> None:
    at = _run()
    assert not at.exception
    assert not any("failed:" in str(item.value) for item in at.error)
    messages = [str(item.value) for item in at.error]
    assert any("Database not found" in message for message in messages), messages
