"""SearXNG metasearch sweeps (JSON API)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from ..normalize import canonicalize_url
from ..util import get_logger, now_utc, parse_dt
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def discover_searxng(ctx: DiscoveryContext) -> DiscoveryResult:
    if not ctx.settings.searxng_enabled:
        return DiscoveryResult(error="SEARXNG_ENABLED is false; set it to true in .env to run this source")
    cfg = ctx.source.get("config") or {}
    base = (ctx.source.get("url") or ctx.settings.searxng_url or "").rstrip("/")
    if not base:
        return DiscoveryResult(error="SearXNG URL is not configured")
    queries = [str(q) for q in (cfg.get("queries") or []) if str(q).strip()]
    if not queries:
        return DiscoveryResult(error="SearXNG source has no config.queries")
    max_results = max(1, int(cfg.get("max_results_per_query") or 20))
    language = str(cfg.get("language") or "en")
    time_range = str(cfg.get("time_range") or ctx.settings.searxng_time_range or "day")
    max_age_days = cfg.get("max_age_days")
    cutoff = None
    if max_age_days:
        try:
            cutoff = now_utc() - timedelta(days=float(max_age_days))
        except (TypeError, ValueError):
            cutoff = None

    headers = {"User-Agent": ctx.settings.effective_user_agent, "Accept": "application/json"}
    items: list[DiscoveryItem] = []
    errors: list[str] = []
    status = None
    seen: set[str] = set()
    with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
        for query in queries:
            params = {
                "q": query,
                "format": "json",
                "language": language,
                "safesearch": 0,
            }
            if time_range and time_range not in {"", "any"}:
                params["time_range"] = time_range
            try:
                response = client.get(f"{base}/search", params=params)
                status = response.status_code
                if response.status_code != 200:
                    errors.append(f"{query}: HTTP {response.status_code}")
                    continue
                payload = response.json()
            except Exception as exc:
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
                continue
            for result in (payload.get("results") or [])[:max_results]:
                url = canonicalize_url(str(result.get("url") or ""))
                if not url or url in seen:
                    continue
                published = parse_dt(result.get("publishedDate") or result.get("pubdate") or result.get("published"))
                if cutoff and published and published < cutoff:
                    continue
                seen.add(url)
                items.append(
                    DiscoveryItem(
                        url=url,
                        kind="page",
                        payload={
                            "title": result.get("title"),
                            "text": result.get("content") or "",
                            "search_query": query,
                            "engine": result.get("engine"),
                            "published_at": published.isoformat(timespec="seconds") if published else None,
                        },
                    )
                )
    if not items:
        return DiscoveryResult(
            items=[],
            http_status=status,
            error="; ".join(errors[:3]) if errors and status not in (200, None) else None,
            empty_reason="; ".join(errors[:3]) if errors else "no results",
        )
    return DiscoveryResult(items=items, http_status=status, notes={"queries": len(queries), "results": len(items)})
