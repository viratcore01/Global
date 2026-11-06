"""HMAC-signed ntfy action payloads."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from ..models import ActionPayload
from ..util import get_logger

log = get_logger(__name__)

ACTION_TTL_SECONDS = 7 * 24 * 3600


def _canonical(action_id: int, action: str, ts: int) -> bytes:
    return f"{int(action_id)}|{action}|{int(ts)}".encode("utf-8")


def sign_action(secret: str, opportunity_id: int, action: str, *, ts: int | None = None) -> str:
    if not secret:
        return ""
    stamp = int(ts if ts is not None else time.time())
    digest = hmac.new(secret.encode("utf-8"), _canonical(opportunity_id, action, stamp), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_payload(secret: str, opportunity_id: int, action: str, *, note: str | None = None) -> dict[str, Any]:
    stamp = int(time.time())
    return {
        "id": int(opportunity_id),
        "action": action,
        "sig": sign_action(secret, opportunity_id, action, ts=stamp),
        "ts": stamp,
        "note": note,
    }


def verify_action(secret: str, payload: dict[str, Any] | ActionPayload, *, ttl: int = ACTION_TTL_SECONDS) -> tuple[bool, str]:
    if not secret:
        return False, "reply secret is not configured"
    try:
        model = payload if isinstance(payload, ActionPayload) else ActionPayload.model_validate(payload)
    except Exception as exc:
        return False, f"invalid payload: {exc}"
    if not model.sig:
        return False, "missing signature"
    if model.ts is None:
        return False, "missing timestamp"
    age = abs(int(time.time()) - int(model.ts))
    if age > ttl:
        return False, f"payload expired ({age}s old)"
    expected = sign_action(secret, model.id, model.action, ts=int(model.ts))
    if not hmac.compare_digest(expected, model.sig):
        return False, "signature mismatch"
    return True, "ok"


def actions_for(opportunity: dict[str, Any], *, reply_url: str, token: str, secret: str) -> list[dict[str, Any]]:
    """Build ntfy HTTP action objects for Apply / Ignore / Scam."""
    if not (reply_url and secret):
        return []
    opportunity_id = int(opportunity.get("id") or 0)
    if not opportunity_id:
        return []
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    actions: list[dict[str, Any]] = []
    for label, action, clear in (("Apply", "apply", True), ("Ignore", "ignore", True), ("Scam", "scam", False)):
        body = json.dumps(make_payload(secret, opportunity_id, action), separators=(",", ":"))
        action_obj: dict[str, Any] = {
            "action": "http",
            "label": label,
            "url": reply_url,
            "method": "POST",
            "headers": headers,
            "body": body,
        }
        if clear:
            action_obj["clear"] = True
        actions.append(action_obj)
    return actions
