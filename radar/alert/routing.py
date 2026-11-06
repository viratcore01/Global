"""Alert formatting, instant routing, quiet hours, caps and the daily digest."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Sequence

from .. import repo
from ..config import AppConfig
from ..db import Database
from ..util import IST, clamp, days_until, get_logger, humanize_deadline, now_ist, now_utc, parse_dt
from .actions import actions_for
from .ntfy import NtfyClient, PublishResult

log = get_logger(__name__)

SAFETY = (
    "Safety: Never pay to apply. Verify the contact independently. "
    "Meet in public, tell someone, and do not share ID or financial documents."
)

PAY_LABELS = {
    "day_rate": "day rate",
    "flat": "flat fee",
    "deferred": "deferred pay",
    "credit_only": "credit only",
    "unpaid": "unpaid",
    "unknown": "not stated",
}


@dataclass
class RouteDecision:
    action: str
    reason: str
    alert_id: int | None = None
    message_id: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "alert_id": self.alert_id,
            "message_id": self.message_id,
            "error": self.error,
        }


def _cluster_source_count(db: Database, opportunity: dict[str, Any]) -> int:
    cluster_id = opportunity.get("cluster_id")
    if not cluster_id:
        return 1
    row = db.query_one("SELECT source_count FROM clusters WHERE id=?", (int(cluster_id),))
    return max(1, int(row.get("source_count") or 1)) if row else 1


def pay_line(opportunity: dict[str, Any]) -> str:
    comp = str(opportunity.get("comp_type") or "unknown").lower()
    amount = opportunity.get("pay_amount")
    currency = str(opportunity.get("currency") or "").strip()
    label = PAY_LABELS.get(comp, comp or "not stated")
    if amount not in (None, "", 0, 0.0):
        symbol = {"INR": "₹", "USD": "$", "GBP": "£", "EUR": "€"}.get(currency.upper(), "")
        prefix = symbol or (currency + " " if currency else "")
        try:
            amount_text = f"{float(amount):,.0f}"
        except (TypeError, ValueError):
            amount_text = str(amount)
        return f"Paid ({prefix}{amount_text}; {label})"
    if comp in {"unpaid", "credit_only", "deferred"}:
        return label.capitalize()
    if opportunity.get("fee_required"):
        return "Fee required (festival/entry)"
    return "Pay not stated"


def format_alert(opportunity: dict[str, Any], *, db: Database | None = None) -> tuple[str, str]:
    city = opportunity.get("city") or opportunity.get("country") or "Location not stated"
    deadline = humanize_deadline(parse_dt(opportunity.get("deadline")))
    title = str(opportunity.get("title") or "Opportunity").strip()
    score = float(opportunity.get("score") or 0.0)
    lines = [
        f"Match: {score:.0f}/100",
        f"Deadline: {deadline} (IST)",
        f"Location: {city}",
        f"Pay: {pay_line(opportunity)}",
    ]
    from ..normalize import domain_from_url

    domain = domain_from_url(str(opportunity.get("canonical_url") or ""))
    if domain:
        sources = _cluster_source_count(db, opportunity) if db else 1
        suffix = f" (also on {sources - 1} other source{'s' if sources > 2 else ''})" if sources > 1 else ""
        lines.append(f"Source: {domain}{suffix}")
    if opportunity.get("contact_value"):
        lines.append(f"Contact: {opportunity['contact_value']}")
    risk = opportunity.get("risk_flags") or []
    if risk:
        lines.append("Risk: " + ", ".join(str(item) for item in risk))
    lines.extend(["", SAFETY])
    return title[:150], "\n".join(lines)


def _actions(config: AppConfig, opportunity: dict[str, Any]) -> list[dict[str, Any]]:
    return actions_for(
        opportunity,
        reply_url=config.settings.reply_url,
        token=config.settings.ntfy_reply_token,
        secret=config.settings.reply_secret,
    )


def in_quiet_hours(current: datetime | None = None, *, start: int = 23, end: int = 7) -> bool:
    local = (current or now_ist()).astimezone(IST)
    hour = local.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


class AlertRouter:
    def __init__(self, config: AppConfig, db: Database, *, client: NtfyClient | None = None):
        self.config = config
        self.db = db
        self.client = client or NtfyClient(config.settings)

    # -- public ------------------------------------------------------------
    def _actions(self, opportunity: dict[str, Any]) -> list[dict[str, Any]]:
        """Signed Apply / Ignore / Scam buttons for this opportunity."""
        return _actions(self.config, opportunity)

    def consider(self, opportunity: dict[str, Any], *, now: datetime | None = None) -> RouteDecision:
        opp = dict(opportunity)
        if int(opp.get("id") or 0) <= 0:
            return RouteDecision("skip", "missing id")
        if str(opp.get("status") or "new") not in {"new", "reviewed"}:
            return RouteDecision("skip", f"status is {opp.get('status')}")
        if repo.alert_exists(self.db, f"instant:{opp['id']}"):
            return RouteDecision("skip", "instant alert already sent")
        score = float(opp.get("score") or 0.0)
        thresholds = self.config.profile.thresholds
        digest_cutoff = self.config.digest_cutoff
        instant_cutoff = self.config.instant_cutoff
        if score < digest_cutoff:
            return RouteDecision("below_threshold", f"score {score:.1f} below digest threshold")
        if not self.config.settings.ntfy_configured:
            return RouteDecision("not_configured", "NTFY_SERVER / NTFY_TOPIC not configured")

        complete = self._completeness(opp) >= float(thresholds.get("min_completeness", 0.55))
        clean = not (opp.get("risk_flags") or [])
        instant = score >= instant_cutoff and complete and clean
        if not instant:
            reason = "below instant threshold" if score < instant_cutoff else (
                "incomplete" if not complete else "has risk flags"
            )
            return RouteDecision("digest", reason)

        current = now or now_utc()
        if in_quiet_hours(
            current.astimezone(IST),
            start=self.config.settings.quiet_start,
            end=self.config.settings.quiet_end,
        ) and not self._deadline_near(opp, hours=6):
            return RouteDecision("digest", "quiet hours")
        if repo.count_instant_today(self.db) >= self.config.settings.daily_alert_cap:
            return RouteDecision("digest", "daily instant cap reached")
        return self._send_instant(opp)

    def send_digest(self, *, now: datetime | None = None, dry_run: bool = False) -> dict[str, Any]:
        if not self.config.settings.ntfy_configured and not dry_run:
            return {"sent": False, "reason": "ntfy_not_configured", "items": []}
        threshold = self.config.digest_cutoff
        rows = self.db.query(
            "SELECT o.* FROM opportunities o LEFT JOIN alerts a ON a.opportunity_id=o.id AND a.kind='digest' "
            "WHERE o.status IN ('new','reviewed') AND o.score >= ? AND a.id IS NULL "
            "ORDER BY o.score DESC, o.deadline ASC LIMIT ?",
            (threshold, self.config.settings.digest_max_items),
        )
        items = [repo.decode_opportunity(row) for row in rows]  # type: ignore[misc]
        items = [item for item in items if item]
        items = self._add_exploration(items)
        if not items:
            return {"sent": False, "reason": "no digest candidates", "items": []}

        quarantine = len(repo.list_quarantine(self.db, open_only=True, limit=500))
        if dry_run:
            return {
                "sent": False,
                "reason": "dry_run",
                "items": [self._digest_item(item) for item in items],
                "quarantine": quarantine,
            }
        if self.config.settings.digest_individual:
            sent = 0
            results = []
            for item in items:
                decision = self._send_digest_item(item)
                results.append(decision.to_dict())
                if decision.action == "sent":
                    sent += 1
            return {"sent": sent > 0, "reason": "individual", "items": results, "quarantine": quarantine}

        title = f"Daily opportunity digest - {len(items)} item{'s' if len(items) != 1 else ''}"
        lines = []
        for index, item in enumerate(items, start=1):
            item_title, body = format_alert(item, db=self.db)
            short = body.split("\nSafety:", 1)[0]
            lines.append(f"{index}. {item_title} — {float(item.get('score') or 0):.0f}/100\n{short}")
        if quarantine:
            lines.append(f"Quarantine: {quarantine} record(s) waiting for review.")
        if self.config.settings.dashboard_url:
            lines.append(f"Dashboard: {self.config.settings.dashboard_url}")
        message = "\n\n".join(lines)
        first = items[0]
        result = self.client.publish(
            title=title,
            message=message,
            priority=3,
            tags=["cinema", "clipboard"],
            actions=self._actions(first),
            click=str(first.get("canonical_url") or ""),
        )
        if not result.ok:
            return {"sent": False, "reason": result.error or "publish failed", "items": []}
        alert_ids = []
        for item in items:
            item_title, body = format_alert(item, db=self.db)
            alert_id = repo.insert_alert(
                self.db,
                opportunity_id=int(item["id"]),
                kind="digest",
                topic=self.config.settings.ntfy_topic,
                message_id=result.message_id,
                title=item_title,
                body=body,
                score=float(item.get("score") or 0.0),
                dedup_key=f"digest:{item['id']}",
            )
            if alert_id:
                alert_ids.append(alert_id)
        return {
            "sent": True,
            "reason": "digest",
            "message_id": result.message_id,
            "alert_ids": alert_ids,
            "count": len(items),
            "quarantine": quarantine,
        }

    # -- instant -----------------------------------------------------------
    def _send_instant(self, opportunity: dict[str, Any]) -> RouteDecision:
        title, body = format_alert(opportunity, db=self.db)
        ntfy_title = f"[{float(opportunity.get('score') or 0):.0f}] {title}"
        result = self.client.publish(
            title=ntfy_title,
            message=body,
            priority=5 if float(opportunity.get("score") or 0) >= 90 else 4,
            tags=["cinema", "rotating_light"],
            actions=self._actions(opportunity),
            click=str(opportunity.get("canonical_url") or ""),
        )
        if not result.ok:
            return RouteDecision("error", "publish failed", error=result.error)
        alert_id = repo.insert_alert(
            self.db,
            opportunity_id=int(opportunity["id"]),
            kind="instant",
            topic=self.config.settings.ntfy_topic,
            message_id=result.message_id,
            title=title,
            body=body,
            score=float(opportunity.get("score") or 0.0),
            dedup_key=f"instant:{opportunity['id']}",
        )
        return RouteDecision(
            "sent",
            "instant",
            alert_id=alert_id,
            message_id=result.message_id,
        )

    def _send_digest_item(self, opportunity: dict[str, Any]) -> RouteDecision:
        title, body = format_alert(opportunity, db=self.db)
        result = self.client.publish(
            title=f"[Digest] {title}",
            message=body,
            priority=3,
            tags=["cinema", "clipboard"],
            actions=self._actions(opportunity),
            click=str(opportunity.get("canonical_url") or ""),
        )
        if not result.ok:
            return RouteDecision("error", "publish failed", error=result.error)
        alert_id = repo.insert_alert(
            self.db,
            opportunity_id=int(opportunity["id"]),
            kind="digest",
            topic=self.config.settings.ntfy_topic,
            message_id=result.message_id,
            title=title,
            body=body,
            score=float(opportunity.get("score") or 0.0),
            dedup_key=f"digest:{opportunity['id']}",
        )
        return RouteDecision("sent", "digest", alert_id=alert_id, message_id=result.message_id)

    # -- helpers -----------------------------------------------------------
    def _completeness(self, opportunity: dict[str, Any]) -> float:
        breakdown = opportunity.get("score_breakdown") or {}
        features = breakdown.get("features") or {}
        try:
            return float(features.get("completeness", 0.0))
        except (TypeError, ValueError):
            return 0.0

    def _deadline_near(self, opportunity: dict[str, Any], *, hours: float) -> bool:
        remaining = days_until(parse_dt(opportunity.get("deadline")))
        return remaining is not None and 0 <= remaining <= hours / 24.0

    def _add_exploration(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if self.config.settings.exploration_slots <= 0:
            return items
        thresholds = self.config.profile.thresholds
        low = float(thresholds.get("exploration_min", 40))
        high = float(thresholds.get("exploration_max", 75))
        seen = {int(item["id"]) for item in items}
        rows = self.db.query(
            "SELECT o.* FROM opportunities o LEFT JOIN alerts a ON a.opportunity_id=o.id "
            "WHERE o.status IN ('new','reviewed') AND o.score BETWEEN ? AND ? AND a.id IS NULL "
            "ORDER BY RANDOM() LIMIT ?",
            (low, high, max(2, self.config.settings.exploration_slots * 3)),
        )
        exploration = []
        for row in rows:
            item = repo.decode_opportunity(row)
            if not item or int(item["id"]) in seen:
                continue
            exploration.append(item)
            seen.add(int(item["id"]))
            if len(exploration) >= self.config.settings.exploration_slots:
                break
        if not exploration:
            return items
        return [*items, *exploration]

    def _digest_item(self, item: dict[str, Any]) -> dict[str, Any]:
        title, body = format_alert(item, db=self.db)
        return {"id": item.get("id"), "title": title, "score": item.get("score"), "body": body}
