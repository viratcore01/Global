"""Telegram public preview (t.me/s/) parser."""

from __future__ import annotations

import re
from urllib.parse import urljoin

from ..normalize import canonicalize_url, extract_urls
from ..util import get_logger, normalize_ws, parse_dt
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def _channel_name(url: str) -> str:
    match = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", url or "")
    return match.group(1) if match else ""


def _preview_url(channel: str) -> str:
    return f"https://t.me/s/{channel}"


def discover_telegram(ctx: DiscoveryContext) -> DiscoveryResult:
    source = ctx.source
    cfg = source.get("config") or {}
    channel = _channel_name(source.get("url") or "")
    if not channel:
        return DiscoveryResult(error="telegram source url must contain a channel name")
    max_pages = max(1, int(cfg.get("max_pages") or 3))
    enqueue_external = bool(cfg.get("enqueue_external_links", True))
    items: list[DiscoveryItem] = []
    status = None
    before: str | None = None
    for page in range(max_pages):
        url = _preview_url(channel)
        if before:
            url = f"{url}?before={before}"
        response = ctx.fetcher.fetch(url)
        status = response.status or status
        if not response.ok:
            return DiscoveryResult(
                items=items,
                http_status=status,
                error=response.error or f"HTTP {response.status}",
            )
        from ..htmlutil import parse_html

        tree = parse_html(response.text or "")
        nodes = tree.css(".tgme_widget_message")
        if not nodes:
            break
        post_ids: list[int] = []
        for node in nodes:
            data_post = node.attributes.get("data-post") or ""
            match = re.search(r"(\d+)$", data_post)
            if not match:
                continue
            post_id = int(match.group(1))
            post_ids.append(post_id)
            text_node = node.css_first(".tgme_widget_message_text")
            text = normalize_ws(text_node.text(separator="\n")) if text_node else ""
            if not text:
                continue
            time_node = node.css_first(".tgme_widget_message_date time") or node.css_first("time")
            published = None
            if time_node is not None:
                stamp = time_node.attributes.get("datetime")
                parsed = parse_dt(stamp)
                published = parsed.isoformat(timespec="seconds") if parsed else None
            first = normalize_ws(text.splitlines()[0])[:200] or f"Telegram post {post_id}"
            items.append(
                DiscoveryItem(
                    url=f"telegram://{channel}/{post_id}",
                    kind="post",
                    payload={
                        "title": first,
                        "text": text,
                        "published_at": published,
                        "channel": channel,
                        "post_id": post_id,
                    },
                )
            )
            if enqueue_external:
                for link in extract_urls(text, base=url):
                    if "t.me/" in link:
                        continue
                    canon = canonicalize_url(link)
                    if canon:
                        items.append(DiscoveryItem(url=canon, kind="page", payload={"referrer_post": post_id}))
        if not post_ids:
            break
        before = str(min(post_ids))
    if not items:
        return DiscoveryResult(items=[], http_status=status, empty_reason="no public messages with text")
    return DiscoveryResult(items=items, http_status=status, notes={"channel": channel})
