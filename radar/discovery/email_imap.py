"""IMAP email discovery with BODY.PEEK semantics (never marks mail read)."""

from __future__ import annotations

import re

from ..normalize import extract_urls, normalize_ws
from ..repo import email_seen, mark_email
from ..util import get_logger, parse_dt
from .base import DiscoveryContext, DiscoveryItem, DiscoveryResult

log = get_logger(__name__)


def _address(value) -> str:
    if not value:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _html_to_text(value: str) -> str:
    try:
        from ..extract.maintext import html_to_text

        return html_to_text(value)
    except Exception:
        return re.sub(r"<[^>]+>", " ", value or "")


def discover_email(ctx: DiscoveryContext) -> DiscoveryResult:
    settings = ctx.settings
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        return DiscoveryResult(
            error="IMAP is not configured (set IMAP_HOST, IMAP_USER, IMAP_PASSWORD)",
            empty_reason="imap_not_configured",
        )
    cfg = ctx.source.get("config") or {}
    source_type = str(ctx.source.get("type") or "email").lower()
    folder = str(cfg.get("folder") or settings.imap_folder_effective or "INBOX")
    limit = max(1, int(cfg.get("limit") or settings.imap_fetch_limit))
    senders = [str(x).lower() for x in (cfg.get("senders") or []) if str(x).strip()]
    subject_contains = [str(x).lower() for x in (cfg.get("subject_contains") or []) if str(x).strip()]
    include_links = source_type == "google_alerts" or bool(cfg.get("enqueue_links", source_type == "google_alerts"))

    try:
        from imap_tools import MailBox
    except ImportError:
        return DiscoveryResult(error="imap-tools is not installed")

    items: list[DiscoveryItem] = []
    try:
        with MailBox(settings.imap_host, port=settings.imap_port, timeout=30).login(
            settings.imap_user, settings.imap_password, initial_folder=folder
        ) as mailbox:
            # mark_seen=False is BODY.PEEK: the user's inbox stays read/unread
            # exactly as it was.
            for message in mailbox.fetch(limit=limit, mark_seen=False, reverse=True, bulk=True):
                message_id = message.headers.get("message-id", [""])[0] if isinstance(message.headers.get("message-id"), (list, tuple)) else message.headers.get("message-id")
                message_id = str(message_id or message.uid or "")
                message_id = message_id.strip("<> ")
                if not message_id:
                    message_id = f"uid-{message.uid}"
                if email_seen(ctx.db, message_id):
                    continue
                sender = _address(message.from_)
                subject = message.subject or ""
                if senders and not any(token in sender.lower() for token in senders):
                    continue
                if subject_contains and not any(token in subject.lower() for token in subject_contains):
                    continue
                text = message.text or ""
                html = message.html or ""
                if not text and html:
                    text = _html_to_text(html)
                text = normalize_ws(text) if text else ""
                if not text and not html:
                    continue
                published = parse_dt(message.date)
                items.append(
                    DiscoveryItem(
                        url=f"email://{message_id}",
                        kind="email",
                        payload={
                            "title": subject or "Email opportunity",
                            "text": text,
                            "html": html,
                            "sender": sender,
                            "published_at": published.isoformat(timespec="seconds") if published else None,
                            "folder": folder,
                        },
                    )
                )
                if include_links:
                    for link in extract_urls(text or html):
                        if any(host in link for host in ("google.com/url", "google.co", "googleusercontent")):
                            # Google Alerts wraps links; keep the wrapper as a
                            # page candidate because it redirects to the source.
                            pass
                        items.append(DiscoveryItem(url=link, kind="page", payload={"email_subject": subject}))
                mark_email(
                    ctx.db,
                    message_id=message_id,
                    source_id=ctx.source_id,
                    uid=str(message.uid),
                    subject=subject,
                    sender=sender,
                    received_at=published.isoformat(timespec="seconds") if published else None,
                    status="processed",
                )
    except Exception as exc:
        log.warning("IMAP poll failed for %s: %s", settings.imap_host, exc)
        return DiscoveryResult(error=f"{type(exc).__name__}: {exc}")

    if not items:
        return DiscoveryResult(items=[], empty_reason="no new matching email")
    return DiscoveryResult(items=items, notes={"folder": folder, "messages": len(items)})
