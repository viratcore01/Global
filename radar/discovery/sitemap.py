"""Sitemap discovery: robots.txt, sitemap indexes, gzip, lastmod diffs."""

from __future__ import annotations

import gzip
import io
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from ..normalize import canonicalize_url, domain_from_url
from ..repo import sitemap_check_and_store
from ..util import get_logger
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1].lower()


def _parse_sitemap(body: bytes, content_type: str = "") -> tuple[str, list[dict[str, str]]]:
    if body[:2] == b"\x1f\x8b" or "gzip" in content_type.lower() or body.lstrip().startswith(b"\x1f\x8b"):
        try:
            body = gzip.decompress(body)
        except OSError:
            pass
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", []
    kind = _local(root.tag)
    entries: list[dict[str, str]] = []
    if kind == "sitemapindex":
        for node in root:
            if _local(node.tag) != "sitemap":
                continue
            item = {}
            for child in node:
                name = _local(child.tag)
                if name in {"loc", "lastmod"} and child.text:
                    item[name] = child.text.strip()
            if item.get("loc"):
                entries.append(item)
        return "index", entries
    if kind == "urlset":
        for node in root:
            if _local(node.tag) != "url":
                continue
            item = {}
            for child in node:
                name = _local(child.tag)
                if name in {"loc", "lastmod", "changefreq"} and child.text:
                    item[name] = child.text.strip()
            if item.get("loc"):
                entries.append(item)
        return "urlset", entries
    return kind, entries


def _matches(url: str, include: list[str], exclude: list[str]) -> bool:
    lower = url.lower()
    if include and not any(token.lower() in lower for token in include):
        return False
    if exclude and any(token.lower() in lower for token in exclude):
        return False
    return True


def discover_sitemap(ctx: DiscoveryContext) -> DiscoveryResult:
    source = ctx.source
    cfg = source.get("config") or {}
    base = source.get("url") or ""
    if not base:
        return DiscoveryResult(error="sitemap source has no url")
    base = base if base.startswith(("http://", "https://")) else f"https://{base}"
    include = [str(x) for x in (cfg.get("include") or [])]
    exclude = [str(x) for x in (cfg.get("exclude") or [])]
    max_urls = int(cfg.get("max_urls_per_run") or 300)
    max_sitemaps = int(cfg.get("max_sitemaps") or 30)
    status = None
    errors: list[str] = []
    queue: list[str] = []
    if base.lower().endswith((".xml", ".xml.gz", ".txt")):
        queue.append(base)
    else:
        robots_url = urljoin(base, "/robots.txt")
        robots = ctx.fetcher.fetch(robots_url)
        if robots.ok:
            for line in robots.text.splitlines():
                if line.lower().startswith("sitemap:"):
                    loc = line.split(":", 1)[1].strip()
                    if loc:
                        queue.append(loc)
        if not queue:
            queue.append(urljoin(base, "/sitemap.xml"))

    seen_sitemaps: set[str] = set()
    results: list[DiscoveryItem] = []
    entries_seen = 0
    while queue and len(seen_sitemaps) < max_sitemaps and len(results) < max_urls:
        sitemap_url = queue.pop(0)
        if sitemap_url in seen_sitemaps:
            continue
        seen_sitemaps.add(sitemap_url)
        response = ctx.fetcher.fetch(sitemap_url)
        status = response.status or status
        if not response.ok:
            errors.append(f"{sitemap_url}: {response.error or response.status}")
            continue
        kind, entries = _parse_sitemap(response.body or response.text.encode("utf-8"), response.content_type)
        if kind == "index":
            for entry in entries:
                loc = entry.get("loc")
                if loc and loc not in seen_sitemaps:
                    queue.append(loc)
            continue
        if kind != "urlset":
            errors.append(f"{sitemap_url}: unrecognised XML root")
            continue
        domain = domain_from_url(base) or ""
        for entry in entries:
            entries_seen += 1
            raw_url = entry.get("loc")
            if not raw_url:
                continue
            url = canonicalize_url(raw_url)
            if not url or not _matches(url, include, exclude):
                continue
            changed = sitemap_check_and_store(
                ctx.db,
                domain=domain,
                url=url,
                lastmod=entry.get("lastmod"),
            )
            if changed:
                results.append(DiscoveryItem(url=url, kind="page"))
            if len(results) >= max_urls:
                break

    if not results:
        reason = "; ".join(errors[:3]) if errors else "no new or changed URLs"
        return DiscoveryResult(items=[], http_status=status, empty_reason=reason)
    return DiscoveryResult(
        items=results,
        http_status=status,
        notes={"sitemaps": len(seen_sitemaps), "urls_seen": entries_seen},
    )
