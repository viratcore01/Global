"""Poll the private ntfy reply topic and apply signed actions."""

from __future__ import annotations

import json
import time
from typing import Any

from ..config import AppConfig
from ..db import Database
from ..models import ActionPayload
from ..repo import seen_reply, store_reply
from ..tracking import TrackingResult, apply_action
from ..util import get_logger, json_loads, now_utc, sha256_text
from .actions import verify_action
from .ntfy import NtfyClient

log = get_logger(__name__)


def _extract_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    body = event.get("message") or event.get("body") or ""
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None
    body = body.strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def process_event(db: Database, config: AppConfig, event: dict[str, Any]) -> TrackingResult | None:
    message_id = str(event.get("id") or "")
    payload = _extract_payload(event)
    if not payload:
        if message_id and not seen_reply(db, message_id):
            store_reply(
                db,
                message_id=message_id,
                topic=event.get("topic"),
                received_at=event.get("time"),
                payload=event,
                processed=True,
                result="not an action payload",
            )
        return None
    if not message_id:
        message_id = "sha256:" + sha256_text(json.dumps(payload, sort_keys=True))[:32]
    if seen_reply(db, message_id):
        return None
    valid, reason = verify_action(config.settings.reply_secret, payload)
    if not valid:
        store_reply(
            db,
            message_id=message_id,
            topic=event.get("topic"),
            received_at=event.get("time"),
            payload=payload,
            processed=True,
            result=f"rejected: {reason}",
        )
        log.warning("Rejected ntfy action %s: %s", message_id, reason)
        return None
    try:
        model = ActionPayload.model_validate(payload)
    except Exception as exc:
        store_reply(
            db,
            message_id=message_id,
            topic=event.get("topic"),
            received_at=event.get("time"),
            payload=payload,
            processed=True,
            result=f"invalid: {exc}",
        )
        return None
    result = apply_action(db, model)
    store_reply(
        db,
        message_id=message_id,
        topic=event.get("topic"),
        received_at=event.get("time"),
        payload=payload,
        processed=True,
        result=result.message,
    )
    return result


def poll_once(
    client: NtfyClient,
    db: Database,
    config: AppConfig,
    *,
    timeout: float = 30.0,
) -> list[TrackingResult]:
    since = db.get_setting("reply_since", "all") or "all"
    events = client.poll(since=since, timeout=timeout)
    results: list[TrackingResult] = []
    for event in events:
        result = process_event(db, config, event)
        if result:
            results.append(result)
        event_id = event.get("id")
        if event_id:
            db.set_setting("reply_since", str(event_id))
    return results


def listen_forever(
    config: AppConfig,
    db: Database,
    *,
    stop_after: int | None = None,
    stop_event: Any | None = None,
) -> None:
    client = NtfyClient(config.settings)
    if not config.settings.reply_configured:
        raise RuntimeError("reply listener requires NTFY_REPLY_TOPIC")
    if not config.settings.reply_secret:
        raise RuntimeError("reply listener requires RADAR_REPLY_SECRET")
    count = 0
    backoff = 1.0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        try:
            results = poll_once(client, db, config, timeout=10.0)
            backoff = 1.0
            for result in results:
                log.info("Action: %s", result.to_dict())
                count += 1
            if stop_after is not None and count >= stop_after:
                return
            if stop_event is not None:
                stop_event.wait(1.0)
            else:
                time.sleep(1.0)
        except KeyboardInterrupt:
            return
        except Exception as exc:
            log.warning("Reply listener error: %s; reconnecting in %.1fs", exc, backoff)
            if stop_event is not None:
                stop_event.wait(backoff)
            else:
                time.sleep(backoff)
            backoff = min(60.0, backoff * 2)
