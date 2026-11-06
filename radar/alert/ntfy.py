"""Thin ntfy publishing client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Settings
from ..util import get_logger

log = get_logger(__name__)


class NtfyError(RuntimeError):
    pass


@dataclass
class PublishResult:
    ok: bool
    message_id: str = ""
    status: int = 0
    error: str = ""
    response: dict[str, Any] | None = None


class NtfyClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    def publish(
        self,
        *,
        title: str,
        message: str,
        priority: int = 3,
        tags: list[str] | None = None,
        actions: list[dict[str, Any]] | None = None,
        topic: str | None = None,
        token: str | None = None,
        click: str | None = None,
    ) -> PublishResult:
        topic = topic if topic is not None else self.settings.ntfy_topic
        if not self.settings.ntfy_server or not topic:
            return PublishResult(ok=False, error="ntfy is not configured (NTFY_SERVER / NTFY_TOPIC)")
        body: dict[str, Any] = {
            "topic": topic,
            "title": title[:150],
            "message": message[:3500],
            "priority": int(priority),
        }
        if tags:
            body["tags"] = tags
        if actions:
            body["actions"] = actions
        if click:
            body["click"] = click
        headers = {"User-Agent": self.settings.effective_user_agent}
        auth = token if token is not None else self.settings.ntfy_token
        if auth:
            headers["Authorization"] = f"Bearer {auth}"
        try:
            with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
                response = client.post(self.settings.ntfy_server, json=body)
            payload: dict[str, Any] = {}
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code >= 400:
                return PublishResult(
                    ok=False,
                    status=response.status_code,
                    error=payload.get("error") or response.text[:500],
                    response=payload,
                )
            return PublishResult(
                ok=True,
                status=response.status_code,
                message_id=str(payload.get("id") or ""),
                response=payload,
            )
        except httpx.HTTPError as exc:
            return PublishResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    def poll(self, *, since: str | None = None, timeout: float = 30.0) -> list[dict[str, Any]]:
        """Fetch new messages from the private reply topic.

        This is an outbound long-poll; no inbound port is opened.
        """
        if not self.settings.reply_configured:
            return []
        params: dict[str, Any] = {"poll": "1"}
        if since:
            params["since"] = since
        headers = {"User-Agent": self.settings.effective_user_agent}
        if self.settings.ntfy_reply_token:
            headers["Authorization"] = f"Bearer {self.settings.ntfy_reply_token}"
        url = self.settings.reply_url
        messages: list[dict[str, Any]] = []
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
                with client.stream("GET", url, params=params) as response:
                    if response.status_code >= 400:
                        log.warning("ntfy poll HTTP %s", response.status_code)
                        return []
                    for line in response.iter_lines():
                        line = (line or "").strip()
                        if not line:
                            continue
                        import json

                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if event.get("event") == "message":
                            messages.append(event)
        except httpx.HTTPError as exc:
            log.debug("ntfy poll failed: %s", exc)
        return messages
