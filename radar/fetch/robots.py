"""robots.txt handling. Fail open on network errors, fail closed on explicit denial."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..util import get_logger

log = get_logger(__name__)


@dataclass
class RobotsDecision:
    allowed: bool
    reason: str = ""
    crawl_delay: float = 0.0


@dataclass
class _Entry:
    parser: RobotFileParser | None
    deny_all: bool
    fetched_at: float
    crawl_delay: float


class RobotsCache:
    def __init__(
        self,
        user_agent: str,
        *,
        respect: bool = True,
        ttl_seconds: int = 86400,
        timeout: float = 10.0,
    ):
        self.user_agent = user_agent
        self.respect = respect
        self.ttl_seconds = ttl_seconds
        self.timeout = timeout
        self._cache: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _key(self, url: str) -> tuple[str, str] | None:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        return parts.scheme, parts.netloc.lower()

    def _load(self, scheme: str, netloc: str) -> _Entry:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(f"{scheme}://{netloc}")
            if cached and now - cached.fetched_at < self.ttl_seconds:
                return cached
        robots_url = f"{scheme}://{netloc}/robots.txt"
        parser: RobotFileParser | None = None
        deny_all = False
        crawl_delay = 0.0
        try:
            with httpx.Client(
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            ) as client:
                response = client.get(robots_url)
            if response.status_code in (401, 403):
                deny_all = True
            elif response.status_code == 200:
                text = response.text[:512_000]
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(text.splitlines())
                delay = parser.crawl_delay(self.user_agent)
                if delay is None:
                    delay = parser.crawl_delay("*")
                crawl_delay = float(delay or 0.0)
                rate = parser.request_rate(self.user_agent) or parser.request_rate("*")
                if rate and rate.requests and rate.seconds:
                    crawl_delay = max(crawl_delay, rate.seconds / rate.requests)
            else:
                # 404 or other: no restrictions published.
                parser = None
        except Exception as exc:  # fail open
            log.debug("robots.txt fetch failed for %s: %s", robots_url, exc)
            parser = None

        entry = _Entry(parser=parser, deny_all=deny_all, fetched_at=now, crawl_delay=crawl_delay)
        with self._lock:
            self._cache[f"{scheme}://{netloc}"] = entry
        return entry

    def check(self, url: str) -> RobotsDecision:
        if not self.respect:
            return RobotsDecision(True, "robots disabled", 0.0)
        key = self._key(url)
        if not key:
            return RobotsDecision(False, "unsupported URL scheme", 0.0)
        scheme, netloc = key
        entry = self._load(scheme, netloc)
        if entry.deny_all:
            return RobotsDecision(False, "robots.txt denies access", entry.crawl_delay)
        if entry.parser is None:
            return RobotsDecision(True, "no robots.txt restrictions", entry.crawl_delay)
        try:
            allowed = entry.parser.can_fetch(self.user_agent, url)
        except Exception:
            allowed = True
        return RobotsDecision(
            allowed,
            "allowed by robots.txt" if allowed else "disallowed by robots.txt",
            entry.crawl_delay,
        )

    def can_fetch(self, url: str) -> bool:
        return self.check(url).allowed

    def crawl_delay(self, url: str) -> float:
        return self.check(url).crawl_delay

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()
