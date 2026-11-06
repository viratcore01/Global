"""Soft risk penalties.  These subtract points and are always shown on alerts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, VetoConfig
from ..db import Database
from ..normalize import contact_domain, is_freemail
from ..util import get_logger

log = get_logger(__name__)


@dataclass
class RiskResult:
    flags: list[str] = field(default_factory=list)
    penalty_points: float = 0.0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "flags": self.flags,
            "penalty_points": round(self.penalty_points, 2),
            "details": self.details,
        }


def _value(opportunity: dict[str, Any] | Any, key: str, default: Any = None) -> Any:
    if isinstance(opportunity, dict):
        return opportunity.get(key, default)
    return getattr(opportunity, key, default)


def evaluate_risk(
    opportunity: dict[str, Any] | Any,
    veto_config: VetoConfig,
    *,
    db: Database | None = None,
    settings: Settings | None = None,
) -> RiskResult:
    result = RiskResult()
    text = f"{_value(opportunity, 'title', '')}\n{_value(opportunity, 'description', '')}"
    flags: dict[str, dict[str, Any]] = {}

    for rule in veto_config.penalties:
        if not rule.enabled:
            continue
        match = rule.matches(text)
        if match:
            weight = max(0.0, float(rule.weight or 1.0))
            flags[rule.id] = {
                "id": rule.id,
                "name": rule.name,
                "points": weight,
                "quote": match.group(0)[:300],
                "reason": rule.reason,
            }

    contact = str(_value(opportunity, "contact_value", "") or "")
    channel = str(_value(opportunity, "contact_channel", "unknown") or "unknown").lower()
    if channel == "whatsapp" or (contact and ("wa.me" in contact.lower() or "whatsapp" in contact.lower())):
        flags.setdefault(
            "whatsapp_only",
            {
                "id": "whatsapp_only",
                "name": "WhatsApp-only contact",
                "points": 4.0,
                "quote": contact or "whatsapp",
                "reason": "WhatsApp-only contact is common in India but harder to verify.",
            },
        )
    if contact and "@" in contact and is_freemail(contact):
        flags.setdefault(
            "freemail_contact",
            {
                "id": "freemail_contact",
                "name": "Free email contact",
                "points": 3.0,
                "quote": contact,
                "reason": "A freemail address is less verifiable than a company domain.",
            },
        )
    if not contact and channel == "unknown":
        flags.setdefault(
            "no_verifiable_contact",
            {
                "id": "no_verifiable_contact",
                "name": "No verifiable contact",
                "points": 3.0,
                "quote": "no contact extracted",
                "reason": "Without a contact channel the call cannot be verified.",
            },
        )

    # Domain age is a soft signal.  RDAP is often redacted; a missing answer is
    # not evidence of a problem.
    domain = contact_domain(contact)
    if not domain:
        from ..normalize import domain_from_url

        domain = domain_from_url(str(_value(opportunity, "canonical_url", "") or ""))
    if domain and db is not None and settings is not None and getattr(settings, "rdap_lookup", False):
        try:
            from .domain_intel import get_domain_intel

            intel = get_domain_intel(db, domain, settings=settings)
            if intel and intel.get("age_days") is not None and float(intel["age_days"]) < 90:
                flags.setdefault(
                    "new_domain",
                    {
                        "id": "new_domain",
                        "name": "Very new website domain",
                        "points": 4.0,
                        "quote": f"{domain}: {float(intel['age_days']):.0f} days old",
                        "reason": "Very new domains are weakly established.",
                    },
                )
        except Exception as exc:  # pragma: no cover - network soft failure
            log.debug("Domain intel unavailable for %s: %s", domain, exc)

    # De-duplicate and preserve a useful display order: high points first.
    ordered = sorted(flags.values(), key=lambda item: (-float(item["points"]), str(item["id"])))
    result.flags = [str(item["id"]) for item in ordered]
    result.details = ordered
    result.penalty_points = min(25.0, sum(float(item["points"]) for item in ordered))
    return result
