"""Demo fixture discovery (no network)."""

from __future__ import annotations

import json
from pathlib import Path

from ..normalize import canonicalize_url
from ..util import get_logger
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def discover_demo(ctx: DiscoveryContext) -> DiscoveryResult:
    cfg = ctx.source.get("config") or {}
    path = cfg.get("file") or "tests/fixtures/demo_items.json"
    file_path = Path(path)
    if not file_path.is_absolute():
        file_path = ctx.settings.root / file_path
    if not file_path.exists():
        return DiscoveryResult(error=f"demo fixture file not found: {file_path}")
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return DiscoveryResult(error=f"could not read demo fixtures: {exc}")
    if isinstance(payload, dict):
        entries = payload.get("items") or payload.get("records") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    items: list[DiscoveryItem] = []
    limit = int(cfg.get("limit") or 100)
    for index, entry in enumerate(entries[:limit]):
        if not isinstance(entry, dict):
            continue
        identifier = str(entry.get("id") or index + 1)
        url = f"fixture://demo/{identifier}"
        items.append(
            DiscoveryItem(
                url=url,
                kind=str(entry.get("kind") or "page"),
                payload={
                    **entry,
                    "title": entry.get("title"),
                    "text": entry.get("text") or "",
                    "html": entry.get("html") or "",
                    "published_at": entry.get("published_at"),
                    "source_url": entry.get("url"),
                },
            )
        )
    if not items:
        return DiscoveryResult(items=[], empty_reason="demo fixture contained no items")
    return DiscoveryResult(items=items, notes={"fixture": str(file_path)})
