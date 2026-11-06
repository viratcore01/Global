"""Adapter registry."""

from __future__ import annotations

from typing import Callable

from .base import DiscoveryContext, DiscoveryResult
from .demo import discover_demo
from .discord import discover_discord
from .email_imap import discover_email
from .rss import discover_rss
from .searxng import discover_searxng
from .sitemap import discover_sitemap
from .telegram import discover_telegram
from .wayback import discover_wayback
from .web import discover_web


def discover_google_alerts(ctx: DiscoveryContext) -> DiscoveryResult:
    url = str(ctx.source.get("url") or "")
    if url.startswith("http"):
        return discover_rss(ctx)
    return discover_email(ctx)


class Adapters:
    _map: dict[str, Callable[[DiscoveryContext], DiscoveryResult]] = {
        "rss": discover_rss,
        "atom": discover_rss,
        "google_alerts": discover_google_alerts,
        "sitemap": discover_sitemap,
        "telegram": discover_telegram,
        "email": discover_email,
        "searxng": discover_searxng,
        "wayback": discover_wayback,
        "discord": discover_discord,
        "web": discover_web,
        "demo": discover_demo,
    }

    @classmethod
    def get(cls, source_type: str) -> Callable[[DiscoveryContext], DiscoveryResult] | None:
        return cls._map.get(source_type)

    @classmethod
    def register(cls, source_type: str, adapter: Callable[[DiscoveryContext], DiscoveryResult]) -> None:
        cls._map[source_type] = adapter

    @classmethod
    def types(cls) -> list[str]:
        return sorted(cls._map)
