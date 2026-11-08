"""HTML listing discovery and outbound-link hub candidates."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import urljoin

from ..normalize import canonicalize_url, domain_from_url
from ..util import get_logger
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def _matches(url: str, include: list[str], exclude: list[str]) -> bool:
    lower = url.lower()
    if include and not any(token.lower() in lower for token in include):
        return False
    if exclude and any(token.lower() in lower for token in exclude):
        return False
    return True


def discover_web(ctx: DiscoveryContext) -> DiscoveryResult:
    source = ctx.source
    cfg = source.get("config") or {}
    base_url = source.get("url") or ""
    if not base_url:
        return DiscoveryResult(error="web source has no url")
    response = ctx.fetcher.fetch(base_url)
    if not response.ok:
        return DiscoveryResult(
            http_status=response.status,
            error=response.error or f"HTTP {response.status}",
        )

    selector = cfg.get("link_selector")
    discover_links = bool(cfg.get("discover_links")) or bool(selector)
    if not discover_links:
        # Without a configured selector, look for a repeated-card casting board
        # and queue each card's detail page.  A single opportunity page still
        # falls through to "ingest this page".
        from ..extract.board import detect_board_items

        if cfg.get("auto_links", True):
            board = detect_board_items(
                response.text or "",
                base_url,
                max_items=int(cfg.get("max_links") or 40),
            )
            if len(board) >= 3:
                # Ended cards keep their place on the board; fetching them costs
                # a request and never yields a live lead.
                live = [item for item in board if not item.closed]
                if len(live) >= 3:
                    board = live
                return DiscoveryResult(
                    items=[
                        DiscoveryItem(
                            url=item.url,
                            kind="page",
                            payload={"title": item.title, "detail": True, "closed": item.closed},
                        )
                        for item in board
                    ],
                    http_status=response.status,
                    notes={"auto_links": len(board)},
                )
        return DiscoveryResult(items=[DiscoveryItem(url=base_url, kind="page")], http_status=response.status)

    from ..htmlutil import parse_html

    tree = parse_html(response.text or "")
    include = [str(x) for x in (cfg.get("link_include") or cfg.get("include") or [])]
    exclude = [str(x) for x in (cfg.get("link_exclude") or cfg.get("exclude") or [])]
    max_links = int(cfg.get("max_links") or 80)
    items: list[DiscoveryItem] = []
    seen: set[str] = set()
    domains: dict[str, list[str]] = defaultdict(list)
    base_domain = domain_from_url(base_url)

    for node in tree.css(str(selector or "a[href]"))[: max_links * 4]:
        href = node.attributes.get("href")
        if not href:
            continue
        url = canonicalize_url(urljoin(base_url, href), base=base_url)
        if not url or url in seen:
            continue
        if not _matches(url, include, exclude):
            continue
        seen.add(url)
        domain = domain_from_url(url)
        if domain:
            domains[domain].append(url)
        items.append(DiscoveryItem(url=url, kind="page"))
        if len(items) >= max_links:
            break

    # Outbound-link graph: sites linked three or more times by a seed are hub
    # candidates.  They are never crawled automatically; a human approves them.
    if cfg.get("graph", True):
        for domain, urls in domains.items():
            if domain == base_domain or len(urls) < 3:
                continue
            from ..repo import add_hub_candidate

            add_hub_candidate(
                ctx.db,
                domain=domain,
                link_count=len(urls),
                example_url=urls[0],
                source_id=ctx.source_id,
                auto_approve=bool(cfg.get("graph_auto_approve", False)),
            )

    if not items:
        # If the selector finds nothing, still treat the page itself as a
        # potential direct opportunity rather than reporting a false empty.
        items = [DiscoveryItem(url=base_url, kind="page")]
        empty_reason = "listing selector matched no links; queued page itself"
    else:
        empty_reason = ""
    return DiscoveryResult(
        items=items,
        http_status=response.status,
        empty_reason=empty_reason,
        notes={"links_found": len(items), "domains": len(domains)},
    )
