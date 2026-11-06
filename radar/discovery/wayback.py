"""Wayback CDX URL-pattern discovery with resume keys."""

from __future__ import annotations

import time
from urllib.parse import quote

import httpx

from ..normalize import canonicalize_url, domain_from_url
from ..repo import last_cdx_run, save_cdx_run
from ..util import get_logger
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def discover_wayback(ctx: DiscoveryContext) -> DiscoveryResult:
    source = ctx.source
    cfg = source.get("config") or {}
    raw = str(source.get("url") or "")
    domain = domain_from_url(raw if "://" in raw else f"https://{raw}") or raw
    if not domain:
        return DiscoveryResult(error="wayback source has no domain")
    patterns = [str(x).lower() for x in (cfg.get("patterns") or ["casting", "audition", "open-call"])]
    max_pages = max(1, int(cfg.get("max_pages") or 3))
    page_size = min(2000, max(50, int(cfg.get("page_size") or 500)))
    max_urls = max(1, int(cfg.get("max_urls") or 1000))
    prior = last_cdx_run(ctx.db, domain)
    resume_key = prior.get("resume_key") if prior and prior.get("status") == "running" else None

    items: list[DiscoveryItem] = []
    seen: set[str] = set()
    pages = 0
    urls_seen = 0
    status_code: int | None = None
    error: str | None = None
    base = "https://web.archive.org/cdx/search/cdx"
    with httpx.Client(timeout=40, follow_redirects=True, headers={"User-Agent": ctx.settings.effective_user_agent}) as client:
        while pages < max_pages and len(items) < max_urls:
            params = {
                "url": f"{domain}/*",
                "output": "json",
                "fl": "original,timestamp,statuscode,mimetype",
                "filter": "statuscode:200",
                "collapse": "urlkey",
                "limit": page_size,
                "showResumeKey": "true",
            }
            if resume_key:
                params["resumeKey"] = resume_key
            try:
                response = client.get(base, params=params)
                status_code = response.status_code
                if response.status_code != 200:
                    error = f"HTTP {response.status_code}"
                    break
                rows = response.json()
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                break
            pages += 1
            if not isinstance(rows, list) or not rows:
                resume_key = None
                break
            # Header row is normally absent when fl is supplied, but tolerate it.
            if rows and isinstance(rows[0], list) and rows[0] and str(rows[0][0]).lower() in {"urlkey", "original"}:
                rows = rows[1:]
            next_resume = None
            for row in rows:
                if not isinstance(row, list) or not row:
                    continue
                original = str(row[0])
                if original.startswith("resumeKey="):
                    next_resume = original.split("=", 1)[1]
                    continue
                urls_seen += 1
                lower = original.lower()
                if not any(pattern in lower for pattern in patterns):
                    continue
                url = canonicalize_url(original)
                if not url or url in seen:
                    continue
                seen.add(url)
                items.append(
                    DiscoveryItem(
                        url=url,
                        kind="page",
                        payload={"wayback_timestamp": row[1] if len(row) > 1 else None, "wayback_domain": domain},
                    )
                )
                if len(items) >= max_urls:
                    break
            # CDX emits the resume key as the final line/row.  If unavailable,
            # stop rather than re-fetching the same page forever.
            resume_key = next_resume or (rows[-1][0] if rows and isinstance(rows[-1], list) and str(rows[-1][0]).startswith("resumeKey=") else None)
            if not resume_key or len(rows) < page_size:
                break
            time.sleep(max(1.0, ctx.settings.domain_rate_seconds))

    save_cdx_run(
        ctx.db,
        domain=domain,
        url_pattern=",".join(patterns),
        resume_key=resume_key if error is None else None,
        pages_done=pages,
        urls_seen=urls_seen,
        status="running" if resume_key and error is None else ("error" if error else "complete"),
        error=error,
    )
    if not items:
        return DiscoveryResult(
            items=[],
            http_status=status_code,
            error=error,
            empty_reason="no matching archived URLs" if not error else error,
            notes={"pages": pages, "urls_seen": urls_seen},
        )
    return DiscoveryResult(
        items=items,
        http_status=status_code,
        notes={"pages": pages, "urls_seen": urls_seen},
    )
