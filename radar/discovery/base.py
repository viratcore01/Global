"""Discovery adapters: turn sources into queued URLs / messages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import repo
from ..config import AppConfig, SourceConfig
from ..db import Database
from ..fetch.http import HttpFetcher
from ..util import get_logger

log = get_logger(__name__)


@dataclass
class DiscoveryItem:
    url: str
    kind: str = "page"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryResult:
    items: list[DiscoveryItem] = field(default_factory=list)
    queued: int = 0
    http_status: int | None = None
    empty_reason: str = ""
    error: str | None = None
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveryContext:
    config: AppConfig
    db: Database
    fetcher: HttpFetcher
    source: dict[str, Any]

    @property
    def settings(self):
        return self.config.settings

    @property
    def source_id(self) -> int:
        return int(self.source["id"])

    @property
    def source_config(self) -> SourceConfig:
        return SourceConfig.from_dict(
            {
                **{key: self.source.get(key) for key in (
                    "name",
                    "type",
                    "url",
                    "tier",
                    "enabled",
                    "poll_interval_minutes",
                    "fetch_strategy",
                    "extractor",
                    "extractor_config",
                    "cadence_hint",
                    "contact",
                    "notes",
                    "config",
                    "tags",
                    "assume_opportunity",
                )},
                "fetch": self.source.get("fetch_strategy") or "http",
            }
        )


def enqueue_items(ctx: DiscoveryContext, items: list[DiscoveryItem], *, limit: int | None = None) -> int:
    queued = 0
    seen: set[str] = set()
    for item in items:
        if limit is not None and queued >= limit:
            break
        if not item.url or item.url in seen:
            continue
        seen.add(item.url)
        queue_id = repo.enqueue(
            ctx.db,
            source_id=ctx.source_id,
            kind=item.kind,
            url=item.url,
            payload=item.payload,
        )
        if queue_id:
            queued += 1
    return queued


def discover_source(config: AppConfig, db: Database, source: dict[str, Any], *, fetcher: HttpFetcher | None = None) -> DiscoveryResult:
    from .adapters import Adapters  # local import avoids cycles

    fetcher = fetcher or HttpFetcher(config.settings)
    ctx = DiscoveryContext(config=config, db=db, fetcher=fetcher, source=source)
    stype = str(source.get("type") or "web").lower()
    adapter = Adapters.get(stype)
    if adapter is None:
        return DiscoveryResult(error=f"no discovery adapter for source type {stype!r}")
    try:
        result = adapter(ctx)
        result.queued = enqueue_items(ctx, result.items)
        return result
    except Exception as exc:
        log.exception("Discovery failed for source %s: %s", source.get("name"), exc)
        return DiscoveryResult(error=f"{type(exc).__name__}: {exc}")
