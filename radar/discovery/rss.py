"""RSS / Atom / Google Alerts feeds."""

from __future__ import annotations

import hashlib
import time
from calendar import timegm
from datetime import datetime, timezone

from ..normalize import normalize_ws
from ..util import get_logger
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def _entry_text(entry) -> str:
    parts: list[str] = []
    summary = entry.get("summary") or entry.get("description")
    if summary:
        parts.append(str(summary))
    for content in entry.get("content") or []:
        value = content.get("value") if isinstance(content, dict) else None
        if value:
            parts.append(str(value))
    text = "\n".join(parts)
    if "<" in text and ">" in text:
        from ..extract.maintext import html_to_text

        text = html_to_text(text)
    return text


def _published(entry, fallback: datetime | None = None) -> str | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.fromtimestamp(timegm(value), tz=timezone.utc).isoformat(timespec="seconds")
            except (TypeError, ValueError, OverflowError):
                pass
    for key in ("published", "updated", "created"):
        if entry.get(key):
            from ..util import parse_dt

            parsed = parse_dt(entry.get(key))
            if parsed:
                return parsed.isoformat(timespec="seconds")
    return (fallback or datetime.now(timezone.utc)).isoformat(timespec="seconds")


def discover_rss(ctx: DiscoveryContext) -> DiscoveryResult:
    source = ctx.source
    urls = source.get("config", {}).get("urls") or []
    if isinstance(urls, str):
        urls = [urls]
    urls = [source.get("url"), *urls]
    max_entries = int(source.get("config", {}).get("max_entries") or 60)
    items: list[DiscoveryItem] = []
    errors: list[str] = []
    status: int | None = None
    for feed_url in [url for url in urls if url]:
        response = ctx.fetcher.fetch(feed_url)
        status = response.status or status
        if not response.ok:
            errors.append(f"{feed_url}: {response.error or response.status}")
            continue
        try:
            import feedparser

            feed = feedparser.parse(response.body or response.text)
        except Exception as exc:
            errors.append(f"{feed_url}: {exc}")
            continue
        for entry in feed.entries[:max_entries]:
            link = (entry.get("link") or "").strip()
            text = _entry_text(entry)
            title = normalize_ws(entry.get("title") or "")
            if not title and text:
                title = normalize_ws(text.splitlines()[0])[:200]
            if not link:
                digest = hashlib.sha256(
                    f"{entry.get('id') or entry.get('title')}|{text[:500]}".encode("utf-8")
                ).hexdigest()[:24]
                link = f"rss://{ctx.source_id}/{digest}"
            items.append(
                DiscoveryItem(
                    url=link,
                    kind="article",
                    payload={
                        "title": title,
                        "text": text,
                        "published_at": _published(entry),
                        "author": entry.get("author"),
                        "feed_url": feed_url,
                    },
                )
            )
    if not items:
        return DiscoveryResult(
            items=[],
            http_status=status,
            error="; ".join(errors) if errors and status not in (200, 304) else None,
            empty_reason="feed contained no entries" if not errors else "; ".join(errors),
        )
    return DiscoveryResult(items=items, http_status=status, notes={"feeds": [u for u in urls if u]})
