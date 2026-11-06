"""HTTP fetching with robots, rate limits, retries and browser escalation."""

from __future__ import annotations

import email.utils
import ipaddress
import random
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..config import Settings
from ..normalize import canonicalize_url, domain_from_url
from ..util import get_logger
from .browser import BrowserResult, browser_available, fetch_with_browser
from .rate_limit import ConcurrencyGate, DomainRateLimiter
from .robots import RobotsCache

log = get_logger(__name__)

BLOCKED_STATUSES = {401, 403, 407, 429, 503}
RETRY_STATUSES = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    text: str = ""
    content_type: str = ""
    strategy: str = "http"
    error: str | None = None
    elapsed: float = 0.0
    truncated: bool = False
    robots_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400

    @property
    def is_html(self) -> bool:
        ctype = self.content_type.lower()
        return "html" in ctype or (not ctype and self.text.lstrip().startswith("<"))

    @property
    def domain(self) -> str | None:
        return domain_from_url(self.final_url or self.url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "headers": self.headers,
            "content_type": self.content_type,
            "strategy": self.strategy,
            "error": self.error,
            "elapsed": self.elapsed,
            "truncated": self.truncated,
            "robots_reason": self.robots_reason,
            "bytes": len(self.body),
        }


class HttpFetcher:
    def __init__(
        self,
        settings: Settings,
        *,
        allow_private_hosts: bool = False,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ):
        self.settings = settings
        self.allow_private_hosts = allow_private_hosts
        self.timeout = timeout
        self.max_retries = max_retries
        self.robots = RobotsCache(settings.effective_user_agent, respect=settings.respect_robots)
        self.limiter = DomainRateLimiter(settings.domain_rate_seconds)
        self.gate = ConcurrencyGate(settings.global_concurrency)
        self._safety_cache: dict[str, tuple[bool, str]] = {}
        self._safety_lock = threading.Lock()
        self.client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            follow_redirects=True,
            headers={
                "User-Agent": settings.effective_user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.5",
                "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
                "Cache-Control": "no-cache",
            },
            limits=httpx.Limits(max_connections=max(4, settings.global_concurrency), max_keepalive_connections=4),
            transport=transport,
        )

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    # -- safety ------------------------------------------------------------
    def _host_allowed(self, url: str) -> tuple[bool, str]:
        if self.allow_private_hosts:
            return True, ""
        try:
            parts = urlsplit(url)
            host = (parts.hostname or "").strip().lower()
        except ValueError:
            return False, "invalid URL"
        if not host:
            return False, "missing host"
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            return False, "private host blocked"
        with self._safety_lock:
            cached = self._safety_cache.get(host)
            if cached:
                return cached
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror:
            # Let the HTTP client produce the DNS error with a useful message.
            return True, ""
        for info in infos:
            address = info[4][0]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError:
                continue
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                result = (False, f"private address blocked ({address})")
                with self._safety_lock:
                    self._safety_cache[host] = result
                return result
        result = (True, "")
        with self._safety_lock:
            self._safety_cache[host] = result
        return result

    # -- public API --------------------------------------------------------
    def fetch(
        self,
        url: str,
        *,
        strategy: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> FetchResult:
        started = time.monotonic()
        canonical = canonicalize_url(url)
        if canonical is None:
            return FetchResult(url=url, error="unsupported URL scheme or invalid URL")
        allowed, reason = self._host_allowed(canonical)
        if not allowed:
            return FetchResult(url=url, final_url=url, error=reason, robots_reason=reason)

        robots = self.robots.check(canonical)
        if not robots.allowed:
            return FetchResult(
                url=url,
                final_url=url,
                error=f"robots disallowed: {robots.reason}",
                robots_reason=robots.reason,
            )
        domain = domain_from_url(canonical) or ""
        if robots.crawl_delay:
            self.limiter.set_crawl_delay(domain, robots.crawl_delay)

        chosen = (strategy or "auto").lower()
        if chosen not in {"auto", "http", "curl_cffi", "browser", "scrapling"}:
            chosen = "auto"

        with self.gate:
            self.limiter.wait(domain)
            attempts = 0
            last: FetchResult | None = None
            while attempts < self.max_retries:
                attempts += 1
                if chosen == "browser":
                    last = self._fetch_browser(canonical, timeout or self.timeout)
                elif chosen == "scrapling":
                    last = self._fetch_scrapling(canonical, timeout or self.timeout)
                elif chosen == "curl_cffi":
                    last = self._fetch_curl_cffi(canonical, headers, timeout or self.timeout)
                else:
                    last = self._fetch_http(canonical, headers, timeout or self.timeout)

                if last.ok:
                    break
                # Auto mode escalates on bot-ish responses or transport errors.
                if chosen == "auto" and (
                    last.status in BLOCKED_STATUSES or (last.error and last.status == 0)
                ):
                    escalated = self._escalate(canonical, headers, timeout or self.timeout)
                    if escalated is not None and escalated.ok:
                        last = escalated
                        break
                # A definitive answer (404, 410, 400...) is never retried, for
                # explicit strategies as well as auto.
                if last.status not in RETRY_STATUSES and last.status < 500:
                    break
                if attempts >= self.max_retries:
                    break
                sleep_for = self._retry_delay(last, attempts)
                log.info("Retrying %s in %.1fs (attempt %d/%d, status=%s)", canonical, sleep_for, attempts, self.max_retries, last.status)
                time.sleep(sleep_for)

            result = last or FetchResult(url=url, error="fetch failed")
            result.url = url
            result.final_url = result.final_url or canonical
            result.elapsed = round(time.monotonic() - started, 3)
            result.robots_reason = robots.reason
            if result.ok:
                self._apply_size_limit(result)
            return result

    def fetch_text(self, url: str, **kwargs: Any) -> str:
        return self.fetch(url, **kwargs).text

    def fetch_json(self, url: str, **kwargs: Any) -> Any:
        result = self.fetch(url, **kwargs)
        if not result.ok:
            return None
        import json

        try:
            return json.loads(result.text)
        except ValueError:
            return None

    # -- strategies --------------------------------------------------------
    def _fetch_http(
        self,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> FetchResult:
        # Stream the body so a hostile or accidentally huge page cannot exhaust
        # memory: reading stops as soon as max_bytes is reached.
        limit = int(self.settings.max_bytes or 0)
        try:
            with self.client.stream("GET", url, headers=headers, timeout=timeout) as response:
                chunks: list[bytes] = []
                total = 0
                truncated = False
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    chunks.append(chunk)
                    total += len(chunk)
                    if limit and total >= limit:
                        truncated = True
                        break
                body = b"".join(chunks)
                if limit and len(body) > limit:
                    body = body[:limit]
                    truncated = True
                status = response.status_code
                final_url = str(response.url)
                content_type = response.headers.get("content-type", "")
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                encoding = response.encoding
        except httpx.HTTPError as exc:
            return FetchResult(url=url, error=f"{type(exc).__name__}: {exc}", strategy="http")
        return FetchResult(
            url=url,
            final_url=final_url,
            status=status,
            headers=response_headers,
            body=body,
            text=self._decode(body, content_type, encoding),
            content_type=content_type,
            strategy="http",
            truncated=truncated,
        )

    def _fetch_curl_cffi(
        self,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> FetchResult:
        try:
            from curl_cffi import requests as curl_requests  # type: ignore
        except ImportError:
            return FetchResult(url=url, error="curl_cffi is not installed", strategy="curl_cffi")
        try:
            request_headers = {"User-Agent": self.settings.effective_user_agent}
            request_headers.update(headers or {})
            response = curl_requests.get(
                url,
                impersonate="chrome",
                timeout=timeout,
                headers=request_headers,
                allow_redirects=True,
            )
            body = response.content or b""
            content_type = response.headers.get("content-type", "")
            return FetchResult(
                url=url,
                final_url=str(response.url),
                status=int(response.status_code),
                headers={k.lower(): v for k, v in response.headers.items()},
                body=body,
                text=self._decode(body, content_type),
                content_type=content_type,
                strategy="curl_cffi",
            )
        except Exception as exc:
            return FetchResult(url=url, error=f"curl_cffi: {exc}", strategy="curl_cffi")

    def _fetch_browser(self, url: str, timeout: float) -> FetchResult:
        result = fetch_with_browser(
            url,
            user_agent=self.settings.effective_user_agent,
            timeout=timeout,
            prefer="camoufox",
        )
        return self._from_browser(result)

    def _fetch_scrapling(self, url: str, timeout: float) -> FetchResult:
        result = fetch_with_browser(
            url,
            user_agent=self.settings.effective_user_agent,
            timeout=timeout,
            prefer="scrapling",
        )
        return self._from_browser(result)

    def _escalate(
        self,
        url: str,
        headers: dict[str, str] | None,
        timeout: float,
    ) -> FetchResult | None:
        curl_result = self._fetch_curl_cffi(url, headers, timeout)
        if curl_result.ok:
            return curl_result
        if self.settings.enable_browser:
            browser_result = self._fetch_browser(url, timeout)
            if browser_result.ok:
                return browser_result
            scrapling_result = self._fetch_scrapling(url, timeout)
            if scrapling_result.ok:
                return scrapling_result
        return curl_result if curl_result.status else None

    @staticmethod
    def _from_browser(result: BrowserResult) -> FetchResult:
        return FetchResult(
            url=result.url,
            final_url=result.url,
            status=result.status,
            headers={k.lower(): v for k, v in (result.headers or {}).items()},
            body=result.body,
            text=result.text or result.body.decode("utf-8", errors="replace"),
            content_type=(result.headers or {}).get("content-type", "text/html"),
            strategy=result.strategy,
            error=result.error,
        )

    # -- helpers -----------------------------------------------------------
    def _apply_size_limit(self, result: FetchResult) -> None:
        limit = self.settings.max_bytes
        if limit and len(result.body) > limit:
            result.body = result.body[:limit]
            result.truncated = True
            result.text = result.text[:limit]
            log.warning("Truncated %s at %d bytes", result.final_url, limit)

    @staticmethod
    def _decode(body: bytes, content_type: str, httpx_encoding: str | None = None) -> str:
        if not body:
            return ""
        charset = ""
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip().strip('"')
        candidates = [charset, httpx_encoding or "", "utf-8", "utf-8-sig", "windows-1252", "latin-1"]
        for candidate in candidates:
            if not candidate:
                continue
            try:
                return body.decode(candidate)
            except (UnicodeDecodeError, LookupError):
                continue
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(body).best()
            if best is not None:
                return str(best)
        except Exception:
            pass
        return body.decode("utf-8", errors="replace")

    def _retry_delay(self, result: FetchResult, attempt: int) -> float:
        retry_after = (result.headers or {}).get("retry-after")
        if retry_after:
            try:
                return min(60.0, max(0.0, float(retry_after)))
            except ValueError:
                try:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    delta = parsed.timestamp() - time.time()
                    return min(60.0, max(0.0, delta))
                except Exception:
                    pass
        # Exponential backoff with jitter.
        return min(30.0, (2 ** max(0, attempt - 1))) * random.uniform(0.5, 1.0)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "user_agent": self.settings.effective_user_agent,
            "respect_robots": self.settings.respect_robots,
            "domain_rate_seconds": self.settings.domain_rate_seconds,
            "global_concurrency": self.settings.global_concurrency,
            "browser": browser_available(),
        }
