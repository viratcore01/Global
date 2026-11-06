"""Per-domain token buckets and a global concurrency cap."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class DomainBucket:
    rate_seconds: float
    last_request: float = 0.0
    crawl_delay: float = 0.0

    def wait_time(self, now: float | None = None) -> float:
        current = now if now is not None else time.monotonic()
        minimum = max(self.rate_seconds, self.crawl_delay)
        return max(0.0, self.last_request + minimum - current)

    def mark(self, now: float | None = None) -> None:
        self.last_request = now if now is not None else time.monotonic()


class DomainRateLimiter:
    def __init__(self, default_rate_seconds: float = 2.0):
        self.default_rate_seconds = max(0.0, default_rate_seconds)
        self._buckets: dict[str, DomainBucket] = {}
        self._lock = threading.Lock()

    def set_crawl_delay(self, domain: str, seconds: float) -> None:
        with self._lock:
            bucket = self._buckets.setdefault(
                domain, DomainBucket(self.default_rate_seconds)
            )
            bucket.crawl_delay = max(0.0, float(seconds or 0.0))

    def wait(self, domain: str) -> float:
        """Sleep until this domain may be fetched. Returns slept seconds."""
        with self._lock:
            bucket = self._buckets.setdefault(
                domain, DomainBucket(self.default_rate_seconds)
            )
            delay = bucket.wait_time()
        if delay > 0:
            time.sleep(delay)
        with self._lock:
            bucket.mark()
        return delay


@dataclass
class ConcurrencyGate:
    limit: int = 4
    _semaphore: threading.Semaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._semaphore = threading.Semaphore(max(1, int(self.limit)))

    def __enter__(self) -> "ConcurrencyGate":
        self._semaphore.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self._semaphore.release()
