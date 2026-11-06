"""Optional JS rendering and adaptive-fetch backends.

These are escalation paths. The core radar never depends on a browser being
installed; if Playwright/Camoufox/Scrapling are absent the caller gets a clear
error and can decide to skip the source.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from ..util import get_logger

log = get_logger(__name__)

_BROWSER_LOCK = threading.Lock()


@dataclass
class BrowserResult:
    ok: bool
    url: str
    status: int = 0
    body: bytes = b""
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    strategy: str = "browser"
    error: str | None = None


def fetch_with_browser(
    url: str,
    *,
    user_agent: str,
    timeout: float = 30.0,
    prefer: str = "playwright",
) -> BrowserResult:
    """Render one URL in a real browser. At most one browser at a time."""
    with _BROWSER_LOCK:
        if prefer == "camoufox":
            result = _try_camoufox(url, user_agent, timeout)
            if result.ok:
                return result
        result = _try_playwright(url, user_agent, timeout)
        if result.ok:
            return result
        return _try_scrapling(url, user_agent, timeout) if prefer != "camoufox" else result


def _try_playwright(url: str, user_agent: str, timeout: float) -> BrowserResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return BrowserResult(False, url, error="playwright is not installed")
    try:
        with sync_playwright() as p:
            browser = None
            last_error: Exception | None = None
            for launcher in (p.chromium, p.firefox):
                try:
                    browser = launcher.launch(headless=True)
                    break
                except Exception as exc:  # browser binary missing
                    last_error = exc
            if browser is None:
                return BrowserResult(False, url, error=f"no playwright browser available: {last_error}")
            try:
                context = browser.new_context(
                    user_agent=user_agent,
                    java_script_enabled=True,
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(8000, int(timeout * 1000)))
                except Exception:
                    pass
                html = page.content()
                status = response.status if response else 200
                final_url = page.url
                headers = dict(response.headers) if response else {}
                return BrowserResult(
                    True,
                    final_url,
                    status=status,
                    body=html.encode("utf-8", errors="replace"),
                    text=html,
                    headers=headers,
                    strategy="playwright",
                )
            finally:
                browser.close()
    except Exception as exc:
        log.debug("Playwright fetch failed for %s: %s", url, exc)
        return BrowserResult(False, url, error=str(exc))


def _try_camoufox(url: str, user_agent: str, timeout: float) -> BrowserResult:
    try:
        from camoufox.sync_api import Camoufox  # type: ignore
    except ImportError:
        return BrowserResult(False, url, error="camoufox is not installed")
    try:
        with Camoufox(headless=True, humanize=True) as browser:
            page = browser.new_page()
            response = page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=min(8000, int(timeout * 1000)))
            except Exception:
                pass
            html = page.content()
            return BrowserResult(
                True,
                page.url,
                status=response.status if response else 200,
                body=html.encode("utf-8", errors="replace"),
                text=html,
                headers=dict(response.headers) if response else {},
                strategy="camoufox",
            )
    except Exception as exc:
        log.debug("Camoufox fetch failed for %s: %s", url, exc)
        return BrowserResult(False, url, error=str(exc))


def _try_scrapling(url: str, user_agent: str, timeout: float) -> BrowserResult:
    try:
        from scrapling.fetchers import StealthyFetcher  # type: ignore
    except ImportError:
        return BrowserResult(False, url, error="scrapling is not installed")
    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            timeout=int(timeout * 1000),
            google_search=False,
        )
        html = getattr(page, "html_content", "") or str(page)
        return BrowserResult(
            True,
            getattr(page, "url", url),
            status=int(getattr(page, "status", 200) or 200),
            body=html.encode("utf-8", errors="replace"),
            text=html,
            headers=dict(getattr(page, "headers", {}) or {}),
            strategy="scrapling",
        )
    except Exception as exc:
        log.debug("Scrapling fetch failed for %s: %s", url, exc)
        return BrowserResult(False, url, error=str(exc))


def browser_available() -> dict[str, bool]:
    availability: dict[str, bool] = {}
    for name, module in (
        ("playwright", "playwright"),
        ("camoufox", "camoufox"),
        ("scrapling", "scrapling"),
    ):
        try:
            __import__(module)
            availability[name] = True
        except Exception:
            availability[name] = False
    return availability
