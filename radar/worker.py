"""Long-running worker: scheduler, reply listener and IMAP IDLE.

One process owns the SQLite writer connection.  The worker is deliberately
small: every unit of work already lives in :mod:`radar.jobs`, so the CLI can run
the same code paths once for tests and manual operation.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import Any

from . import jobs, repo
from .alert.ntfy import NtfyClient
from .alert.reply_listener import poll_once
from .config import AppConfig
from .db import Database
from .discovery.email_idle import listen_forever as imap_listen_forever
from .pipeline import Pipeline
from .scheduler import Scheduler
from .util import get_logger, now_utc

log = get_logger(__name__)


class Worker:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        pipeline: Pipeline | None = None,
        scheduler: Scheduler | None = None,
        poll_seconds: int = 30,
    ):
        self.config = config
        self.db = db
        self.pipeline = pipeline or Pipeline(config, db)
        self.scheduler = scheduler or Scheduler(config, db, pipeline=self.pipeline)
        self.poll_seconds = max(5, int(poll_seconds))
        self.stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._started_at = now_utc()
        self._last_heartbeat = 0.0
        self._own_pipeline = pipeline is None

    # -- lifecycle ---------------------------------------------------------
    def recover(self) -> dict[str, Any]:
        """Clean up state left behind by a crash. Safe to call repeatedly."""
        requeued = repo.requeue_stale_queue(self.db)
        synced = jobs.sync_sources(self.config, self.db)
        return {"requeued": requeued, "sources": synced, "at": self._started_at.isoformat()}

    def start_aux_threads(self, *, replies: bool = True, imap: bool = True) -> dict[str, bool]:
        started = {"replies": False, "imap": False}
        if replies and self.config.settings.reply_configured and self.config.settings.reply_secret:
            thread = threading.Thread(
                target=self._reply_loop,
                name="radar-reply-listener",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
            started["replies"] = True
        elif replies:
            log.info("Reply listener disabled (NTFY_REPLY_TOPIC/RADAR_REPLY_SECRET not configured)")

        if imap and self.config.settings.imap_enabled:
            thread = threading.Thread(
                target=imap_listen_forever,
                kwargs={
                    "config": self.config,
                    "db": self.db,
                    "fetcher": self.pipeline.fetcher,
                    "stop_event": self.stop_event,
                },
                name="radar-imap-idle",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
            started["imap"] = True
        elif imap:
            log.info("IMAP IDLE disabled (IMAP_ENABLED is false)")
        return started

    def stop(self) -> None:
        self.stop_event.set()

    def close(self) -> None:
        self.stop()
        for thread in self._threads:
            thread.join(timeout=5.0)
        if self._own_pipeline:
            self.pipeline.close()

    # -- work --------------------------------------------------------------
    def run_once(self, *, force: list[str] | None = None) -> dict[str, Any]:
        result = self.scheduler.run_due(force=force)
        self._maybe_heartbeat(success=not any(
            isinstance(item, dict) and item.get("ok") is False for item in result.get("results", {}).values()
        ), message="worker cycle")
        return result

    def run_forever(self, *, replies: bool = True, imap: bool = True) -> None:
        recovery = self.recover()
        log.info("Worker starting: %s", recovery)
        aux = self.start_aux_threads(replies=replies, imap=imap)
        log.info("Worker auxiliary listeners: %s", aux)
        previous_handlers = self._install_signal_handlers()
        last_cycle: dict[str, Any] | None = None
        try:
            while not self.stop_event.is_set():
                started = time.monotonic()
                try:
                    last_cycle = self.run_once()
                    if last_cycle.get("ran"):
                        log.info("Scheduled jobs ran: %s", ",".join(last_cycle["ran"]))
                except Exception as exc:  # keep the daemon alive; log loudly
                    log.exception("Worker cycle failed: %s", exc)
                    self._maybe_heartbeat(success=False, message=f"cycle error: {exc}")
                elapsed = time.monotonic() - started
                self.stop_event.wait(max(1.0, self.poll_seconds - elapsed))
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received; shutting down")
        finally:
            self.stop_event.set()
            self._restore_signal_handlers(previous_handlers)
            for thread in self._threads:
                thread.join(timeout=5.0)
            if self._own_pipeline:
                self.pipeline.close()
            log.info("Worker stopped")

    # -- helpers -----------------------------------------------------------
    def _reply_loop(self) -> None:
        client = NtfyClient(self.config.settings)
        backoff = 1.0
        while not self.stop_event.is_set():
            try:
                results = poll_once(client, self.db, self.config, timeout=10.0)
                backoff = 1.0
                for result in results:
                    log.info("Button action: %s", result.to_dict())
            except Exception as exc:
                log.warning("Reply listener error: %s; retrying in %.0fs", exc, backoff)
                self.stop_event.wait(backoff)
                backoff = min(60.0, backoff * 2)
                continue
            # ntfy long-poll already provides the wait; this only limits a
            # tight loop when the server returns immediately.
            self.stop_event.wait(1.0)
        log.info("Reply listener stopped")

    def _maybe_heartbeat(self, *, success: bool = True, message: str = "") -> None:
        interval = max(60, int(self.config.settings.heartbeat_interval_minutes) * 60)
        now = time.monotonic()
        if now - self._last_heartbeat < interval:
            return
        self._last_heartbeat = now
        try:
            jobs.heartbeat(self.config, success=success, message=message)
        except Exception as exc:
            log.debug("Heartbeat failed: %s", exc)

    def _install_signal_handlers(self) -> dict[int, Any]:
        previous: dict[int, Any] = {}

        def handle(signum: int, _frame: Any) -> None:
            log.info("Signal %s received; requesting graceful shutdown", signum)
            self.stop_event.set()

        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                previous[sig] = signal.getsignal(sig)
                signal.signal(sig, handle)
            except (ValueError, OSError):
                # Not the main thread (tests, embedded use).
                pass
        return previous

    @staticmethod
    def _restore_signal_handlers(previous: dict[int, Any]) -> None:
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def run_worker(config: AppConfig, db: Database, *, once: bool = False, poll_seconds: int = 30) -> int:
    worker = Worker(config, db, poll_seconds=poll_seconds)
    if once:
        worker.recover()
        result = worker.run_once()
        log.info("Worker once: %s", result)
        worker.close()
        return 0
    worker.run_forever()
    return 0
