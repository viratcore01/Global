"""Fetch layer: honest UA, SSRF guard, robots, retries and the size cap."""

from __future__ import annotations

import httpx
import pytest

from radar.config import Settings
from radar.fetch.http import FetchResult, HttpFetcher
from radar.fetch.robots import RobotsDecision


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "root": tmp_path,
        "respect_robots": False,
        "domain_rate_seconds": 0.0,
        "global_concurrency": 2,
        "max_bytes": 4096,
    }
    values.update(overrides)
    return Settings(**values)


def make_fetcher(tmp_path, handler, **overrides) -> HttpFetcher:
    settings = make_settings(tmp_path, **overrides)
    return HttpFetcher(
        settings,
        allow_private_hosts=True,
        timeout=5.0,
        transport=httpx.MockTransport(handler),
    )


def test_contact_url_is_appended_to_user_agent(tmp_path) -> None:
    settings = make_settings(tmp_path, user_agent="RadarBot/1.0", contact_url="https://example.com/about")
    assert settings.effective_user_agent == "RadarBot/1.0 (+https://example.com/about)"
    # Never duplicated when the UA already carries the contact.
    settings = make_settings(tmp_path, user_agent="RadarBot/1.0 (+https://example.com/about)", contact_url="https://example.com/about")
    assert settings.effective_user_agent.count("https://example.com/about") == 1
    # No contact configured: unchanged.
    settings = make_settings(tmp_path, user_agent="RadarBot/1.0", contact_url="")
    assert settings.effective_user_agent == "RadarBot/1.0"


def test_fetch_returns_text_and_content_type(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><body>Casting in Delhi</body></html>".encode(),
        )

    fetcher = make_fetcher(tmp_path, handler)
    result = fetcher.fetch("https://example.com/casting")
    assert result.ok, result.error
    assert result.status == 200
    assert "Casting in Delhi" in result.text
    assert result.content_type.startswith("text/html")
    assert result.strategy == "http"


def test_private_hosts_are_blocked(tmp_path) -> None:
    fetcher = HttpFetcher(
        make_settings(tmp_path),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret")),
    )
    result = fetcher.fetch("http://127.0.0.1/internal")
    assert result.error is not None
    assert "private" in result.error.lower()
    assert result.status == 0


def test_size_cap_truncates_streamed_body(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, content=b"x" * 50_000)

    fetcher = make_fetcher(tmp_path, handler, max_bytes=200)
    result = fetcher.fetch("https://example.com/huge.txt")
    assert result.ok, result.error
    assert result.truncated is True
    assert len(result.body) <= 200


def test_retries_transient_status_then_succeeds(tmp_path, monkeypatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, text="ok")

    monkeypatch.setattr("radar.fetch.http.time.sleep", lambda *_: None)
    fetcher = make_fetcher(tmp_path, handler)
    result = fetcher.fetch("https://example.com/x", strategy="http")
    assert result.ok, result.error
    assert calls["n"] == 3


def test_permanent_status_is_not_retried(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404, text="missing")

    fetcher = make_fetcher(tmp_path, handler)
    result = fetcher.fetch("https://example.com/missing", strategy="http")
    assert result.status == 404
    assert result.ok is False
    assert calls["n"] == 1


def test_robots_denial_stops_the_fetch(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("robots-denied URL must not be fetched")

    fetcher = make_fetcher(tmp_path, handler)
    fetcher.robots.check = lambda url: RobotsDecision(False, "disallowed by robots.txt", 0.0)
    result = fetcher.fetch("https://example.com/private")
    assert result.ok is False
    assert "robots" in (result.error or "")


def test_retry_after_header_is_honoured(tmp_path) -> None:
    fetcher = make_fetcher(tmp_path, lambda request: httpx.Response(200))
    delay = fetcher._retry_delay(FetchResult(url="https://example.com", headers={"retry-after": "7"}), 1)
    assert delay == pytest.approx(7.0)
    # Backoff stays bounded even after many attempts.
    assert 0.0 <= fetcher._retry_delay(FetchResult(url="https://example.com"), 20) <= 30.0


def test_unsupported_scheme_is_rejected(tmp_path) -> None:
    fetcher = make_fetcher(tmp_path, lambda request: httpx.Response(200))
    result = fetcher.fetch("javascript:alert(1)")
    assert result.ok is False
    assert "scheme" in (result.error or "").lower()


def test_diagnostics_reports_effective_identity(tmp_path) -> None:
    fetcher = make_fetcher(tmp_path, lambda request: httpx.Response(200), contact_url="https://example.com/about")
    diagnostics = fetcher.diagnostics()
    assert "https://example.com/about" in diagnostics["user_agent"]
    assert diagnostics["respect_robots"] is False
