"""IMAP IDLE listener.

The scheduler already polls mail every few minutes, but IMAP IDLE gives the
inbox near-instant delivery without opening an inbound port.  The listener keeps
one long-lived IDLE connection per configured mailbox and opens a short-lived
BODY.PEEK connection only when a new message is announced (or on a periodic
fallback poll, because servers occasionally drop IDLE notifications).

Nothing here marks mail as read: :mod:`radar.discovery.email_imap` uses
``mark_seen=False`` for every fetch.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from .. import repo
from ..config import AppConfig
from ..db import Database
from ..fetch.http import HttpFetcher
from ..util import get_logger
from .base import DiscoveryResult, discover_source

log = get_logger(__name__)

EMAIL_TYPES = {"email", "google_alerts"}
IDLE_WAIT_SECONDS = 55.0  # RFC 2177 recommends re-issuing IDLE before 29 min.
MAX_IDLE_WAIT = 29 * 60.0


def idle_announces_mail(responses: Iterable[bytes | str]) -> bool:
    """Return True when an IDLE response contains EXISTS or RECENT."""
    for response in responses:
        if isinstance(response, bytes):
            text = response.upper()
            if b"EXISTS" in text or b"RECENT" in text:
                return True
        else:
            text = str(response).upper()
            if "EXISTS" in text or "RECENT" in text:
                return True
    return False


def email_sources(db: Database) -> list[dict[str, Any]]:
    return [
        source
        for source in repo.list_sources(db, enabled_only=True)
        if str(source.get("type") or "").lower() in EMAIL_TYPES
    ]


def poll_email_sources(
    config: AppConfig,
    db: Database,
    *,
    fetcher: HttpFetcher | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Poll each enabled email source and record health. Returns per-source results."""
    selected = sources if sources is not None else email_sources(db)
    results: list[dict[str, Any]] = []
    for source in selected:
        result: DiscoveryResult = discover_source(config, db, source, fetcher=fetcher)
        status = repo.record_source_result(
            db,
            int(source["id"]),
            success=result.error is None,
            http_status=result.http_status,
            records=result.queued,
            error=result.error,
            empty_reason=result.empty_reason,
            empty_counts=False,
        )
        results.append(
            {
                "source": source.get("name"),
                "queued": result.queued,
                "error": result.error,
                "empty_reason": result.empty_reason,
                "health": status,
            }
        )
    if results:
        repo.record_job_run(db, "email_idle", status="ok")
    return results


def listen_forever(
    config: AppConfig,
    db: Database,
    *,
    fetcher: HttpFetcher | None = None,
    stop_event: threading.Event | None = None,
    reconnect_delay: float = 30.0,
) -> None:
    """Listen for new mail until ``stop_event`` is set.

    The function is safe to run in a daemon thread.  It reconnects with backoff
    after network or authentication failures.
    """
    settings = config.settings
    stop = stop_event or threading.Event()
    if not settings.imap_enabled:
        log.info("IMAP IDLE disabled (set IMAP_ENABLED=true to enable)")
        return
    if not (settings.imap_host and settings.imap_user and settings.imap_password):
        log.warning("IMAP IDLE requested but IMAP_HOST/IMAP_USER/IMAP_PASSWORD are incomplete")
        return

    try:
        from imap_tools import MailBox  # type: ignore
    except ImportError:
        log.warning("IMAP IDLE requested but imap-tools is not installed")
        return

    folder = settings.imap_folder_effective or "INBOX"
    fallback_seconds = max(60, settings.imap_poll_seconds)
    wait_seconds = min(IDLE_WAIT_SECONDS, MAX_IDLE_WAIT)
    backoff = reconnect_delay

    while not stop.is_set():
        try:
            log.info("Connecting IMAP IDLE listener to %s (%s)", settings.imap_host, folder)
            with MailBox(settings.imap_host, port=settings.imap_port, timeout=60).login(
                settings.imap_user, settings.imap_password, initial_folder=folder
            ) as mailbox:
                backoff = reconnect_delay
                log.info("IMAP IDLE active on %s", folder)
                next_full_poll = time.monotonic() + fallback_seconds
                # Catch anything that arrived while we were disconnected.
                poll_email_sources(config, db, fetcher=fetcher)
                while not stop.is_set():
                    try:
                        responses = mailbox.idle.wait(timeout=wait_seconds)
                    except Exception as exc:
                        # The connection is no longer usable; reconnect.
                        log.warning("IMAP IDLE wait failed: %s", exc)
                        break
                    if idle_announces_mail(responses):
                        log.info("IMAP IDLE announced new mail; polling")
                        poll_email_sources(config, db, fetcher=fetcher)
                        next_full_poll = time.monotonic() + fallback_seconds
                        continue
                    if time.monotonic() >= next_full_poll:
                        poll_email_sources(config, db, fetcher=fetcher)
                        next_full_poll = time.monotonic() + fallback_seconds
        except Exception as exc:
            if stop.is_set():
                break
            log.warning("IMAP IDLE connection failed: %s; reconnecting in %.0fs", exc, backoff)
            stop.wait(backoff)
            backoff = min(300.0, backoff * 2)

    log.info("IMAP IDLE listener stopped")
