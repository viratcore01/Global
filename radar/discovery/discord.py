"""Discord read-only discovery via the official REST API.

Only channels where an administrator explicitly invited the bot are read.  The
connector never uses a user token and never sends messages.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..normalize import normalize_ws
from ..util import get_logger, parse_dt
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)

API = "https://discord.com/api/v10"


def _channels_for_guild(client: httpx.Client, guild_id: str, limit: int) -> tuple[list[str], str | None]:
    try:
        response = client.get(f"{API}/guilds/{guild_id}/channels")
        if response.status_code != 200:
            return [], f"guild {guild_id}: HTTP {response.status_code}"
        channels = response.json()
    except Exception as exc:
        return [], f"guild {guild_id}: {exc}"
    ids = [
        str(channel.get("id"))
        for channel in channels
        if isinstance(channel, dict) and channel.get("type") in {0, 5} and channel.get("id")
    ]
    return ids[:limit], None


def discover_discord(ctx: DiscoveryContext) -> DiscoveryResult:
    token = ctx.settings.discord_bot_token
    if not token:
        return DiscoveryResult(error="DISCORD_BOT_TOKEN is not configured", empty_reason="discord_not_configured")
    cfg = ctx.source.get("config") or {}
    channel_ids = [str(x) for x in (cfg.get("channel_ids") or ctx.settings.discord_channel_ids or [])]
    guild_ids = [str(x) for x in (cfg.get("guild_ids") or ctx.settings.discord_guild_ids or [])]
    limit = max(1, min(100, int(cfg.get("limit") or 25)))
    errors: list[str] = []
    items: list[DiscoveryItem] = []
    headers = {"Authorization": f"Bot {token}", "User-Agent": ctx.settings.effective_user_agent}
    with httpx.Client(timeout=25, follow_redirects=True, headers=headers) as client:
        if not channel_ids:
            for guild_id in guild_ids[:5]:
                discovered, error = _channels_for_guild(client, guild_id, 10)
                channel_ids.extend(discovered)
                if error:
                    errors.append(error)
        for channel_id in channel_ids:
            try:
                response = client.get(f"{API}/channels/{channel_id}/messages", params={"limit": limit})
                if response.status_code != 200:
                    errors.append(f"channel {channel_id}: HTTP {response.status_code}")
                    continue
                messages = response.json()
                if not isinstance(messages, list):
                    continue
                for message in messages:
                    content = normalize_ws(str(message.get("content") or ""))
                    if not content:
                        continue
                    author = message.get("author") or {}
                    published = parse_dt(message.get("timestamp"))
                    first = normalize_ws(content.splitlines()[0])[:200] or "Discord post"
                    items.append(
                        DiscoveryItem(
                            url=f"discord://{channel_id}/{message.get('id')}",
                            kind="message",
                            payload={
                                "title": first,
                                "text": content,
                                "author": author.get("username"),
                                "published_at": published.isoformat(timespec="seconds") if published else None,
                                "channel_id": channel_id,
                            },
                        )
                    )
            except Exception as exc:
                errors.append(f"channel {channel_id}: {type(exc).__name__}: {exc}")
    if not items:
        return DiscoveryResult(
            items=[],
            error="; ".join(errors[:3]) if errors else None,
            empty_reason="; ".join(errors[:3]) if errors else "no messages",
        )
    return DiscoveryResult(items=items, notes={"channels": len(channel_ids), "errors": errors[:3]})
