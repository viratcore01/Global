"""Stage 2: hard vetoes.  A veto removes an item from ranking and alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ..config import Profile, VetoConfig
from ..db import Database
from ..util import IST, get_logger, now_utc, parse_dt

log = get_logger(__name__)


@dataclass
class VetoResult:
    vetoed: bool
    rule_id: str = ""
    reason: str = ""
    stage: str = "veto"
    detail: str = ""
    all_matches: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "vetoed": self.vetoed,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "stage": self.stage,
            "detail": self.detail,
            "matches": self.all_matches,
        }


def _value(opportunity: dict[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(opportunity, dict):
        return opportunity.get(key, default)
    return getattr(opportunity, key, default)


def opportunity_text(opportunity: dict[str, Any] | Any) -> str:
    parts = [
        str(_value(opportunity, "title", "") or ""),
        str(_value(opportunity, "description", "") or ""),
        str(_value(opportunity, "usage_rights", "") or ""),
    ]
    return "\n".join(part for part in parts if part)


def evaluate_vetoes(
    opportunity: dict[str, Any] | Any,
    profile: Profile,
    veto_config: VetoConfig,
    *,
    db: Database | None = None,
    now: datetime | None = None,
) -> VetoResult:
    current = now or now_utc()
    text = opportunity_text(opportunity)
    op_type = str(_value(opportunity, "opportunity_type", "other") or "other").lower()

    # Expiry ---------------------------------------------------------------
    deadline = parse_dt(_value(opportunity, "deadline"))
    if deadline is not None:
        if deadline < current:
            return VetoResult(
                vetoed=True,
                rule_id="expired",
                reason="deadline has passed",
                stage="expiry",
                detail=str(_value(opportunity, "deadline")),
            )
    else:
        anchor = parse_dt(_value(opportunity, "published_at")) or parse_dt(_value(opportunity, "first_seen_at"))
        if anchor is not None:
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=IST)
            if anchor + timedelta(days=14) < current:
                return VetoResult(
                    vetoed=True,
                    rule_id="stale_unknown_deadline",
                    reason="unknown deadline older than 14 days",
                    stage="expiry",
                    detail=f"published/first seen {anchor.isoformat()}",
                )

    # Deterministic pattern vetoes ----------------------------------------
    matches: list[dict[str, str]] = []
    for rule in veto_config.vetoes:
        if not rule.applies(op_type) or rule.exempt(op_type):
            continue
        match = rule.matches(text)
        if match:
            matches.append(
                {
                    "rule_id": rule.id,
                    "name": rule.name,
                    "reason": rule.reason,
                    "text": match.group(0)[:500],
                }
            )
    if matches:
        first = matches[0]
        return VetoResult(
            vetoed=True,
            rule_id=first["rule_id"],
            reason=first["reason"] or first["name"],
            stage="veto",
            detail=first["text"],
            all_matches=matches,
        )

    # Profile-driven unpaid veto -------------------------------------------
    comp = str(_value(opportunity, "comp_type", "unknown") or "unknown").lower()
    if profile.paid_only and comp in {"unpaid", "credit_only", "deferred"}:
        return VetoResult(
            vetoed=True,
            rule_id="unpaid_paid_only",
            reason="profile requires paid work",
            stage="eligibility",
            detail=comp,
        )

    # Reputation -----------------------------------------------------------
    if db is not None:
        try:
            from ..repo import reputation_match, values_from_opportunity

            match = reputation_match(db, values_from_opportunity(dict(opportunity) if not isinstance(opportunity, dict) else opportunity))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Reputation check failed: %s", exc)
            match = None
        if match:
            return VetoResult(
                vetoed=True,
                rule_id="reputation",
                reason=f"previously marked as {match.get('reason') or 'scam'}",
                stage="reputation",
                detail=f"{match.get('kind')}={match.get('value')}",
            )

    return VetoResult(vetoed=False)
