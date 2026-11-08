"""Turn a stored opportunity into a concrete application plan.

Ranking answers "is this worth my time?".  This module answers the next
question: "what do I actually send, to whom, and by when?".  Everything is
derived from the stored record and the profile, so the advice stays explainable
and never invents a contact or a deadline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .cities import distance_between
from .config import Profile
from .util import clamp, days_until, normalize_ws, parse_dt

# What a casting call asks the applicant to send / prepare.
REQUIREMENT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bself[\s-]?tape|\bself[\s-]?test|\bvideo\s+(?:audition|submission)\b", re.I), "self-tape"),
    (re.compile(r"\bshow[\s-]?reel\b|\bshowreel\b|\breel\b", re.I), "showreel"),
    (re.compile(r"\bhead[\s-]?shot\b|\bclose[\s-]?up\b|\bfull[\s-]?body\b|\bphotographs?\b|\bphotos?\b", re.I), "photos"),
    (re.compile(r"\bresume\b|\bcv\b|\bbiodata\b|\bbio[\s-]?data\b", re.I), "resume"),
    (re.compile(r"\bmonologue\b|\bsoliloquy\b", re.I), "monologue"),
    (re.compile(r"\bdance\b|\bdancing\b|\bchoreograph", re.I), "dance"),
    (re.compile(r"\bsing(?:ing)?\b|\bsong\b", re.I), "singing"),
    (re.compile(r"\bportfolio\b", re.I), "portfolio"),
    (re.compile(r"\bpassport\b|\baadhaar\b|\baadhar\b|\bid\s+proof\b|\bgovernment\s+id\b", re.I), "id-proof"),
    (re.compile(r"\bhard\s+copy\b|\bprint(?:ed)?\s+(?:photos?|copies)\b", re.I), "printed-photos"),
]

REQUIREMENT_LABELS = {
    "self-tape": "Record a self-tape (30-60s, natural light, clean background, slate your name/age/city)",
    "showreel": "Share your showreel link",
    "photos": "Attach a recent headshot plus one full-body photo",
    "resume": "Attach your acting CV / biodata",
    "monologue": "Prepare a monologue",
    "dance": "Prepare a short dance clip",
    "singing": "Prepare a song or vocal sample",
    "portfolio": "Attach your portfolio",
    "id-proof": "Carry government ID proof",
    "printed-photos": "Carry printed photos",
}

CHANNEL_ACTIONS = {
    "email": "Email {contact} with your profile, photos and the role name in the subject",
    "whatsapp": "WhatsApp {contact} with your profile and a short intro",
    "phone": "Call {contact} during working hours and ask for the audition slot",
    "instagram": "DM {contact} with your profile and showreel link",
    "dm": "DM {contact} with your profile and showreel link",
    "telegram": "Message {contact} on Telegram with your profile",
    "form": "Fill the application form with your details and links",
}

PAY_LABELS = {
    "day_rate": "Day rate",
    "flat": "Flat fee",
    "deferred": "Deferred / revenue share",
    "credit_only": "Credit only (TFP)",
    "unpaid": "Unpaid",
    "unknown": "Not stated",
}


@dataclass
class ApplyBrief:
    opportunity_id: int
    title: str
    score: float = 0.0
    deadline: str | None = None
    days_left: float | None = None
    pay: str = ""
    location: str = ""
    distance_km: float | None = None
    contact: str | None = None
    requirements: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    next_action: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "title": self.title,
            "score": self.score,
            "deadline": self.deadline,
            "days_left": self.days_left,
            "pay": self.pay,
            "location": self.location,
            "distance_km": self.distance_km,
            "contact": self.contact,
            "requirements": self.requirements,
            "checklist": self.checklist,
            "blockers": self.blockers,
            "notes": self.notes,
            "next_action": self.next_action,
        }

    def render(self) -> str:
        lines = [f"#{self.opportunity_id}  {self.title}", f"Score: {self.score:.0f}/100"]
        if self.deadline:
            suffix = "" if self.days_left is None else f" ({_days_label(self.days_left)})"
            lines.append(f"Deadline: {self.deadline}{suffix}")
        else:
            lines.append("Deadline: not stated")
        lines.append(f"Pay: {self.pay}")
        lines.append(f"Location: {self.location}")
        if self.contact:
            lines.append(f"Contact: {self.contact}")
        if self.blockers:
            lines.append("")
            lines.append("Blockers:")
            lines.extend(f"  ! {item}" for item in self.blockers)
        if self.requirements:
            lines.append("")
            lines.append("What they ask for:")
            lines.extend(f"  - {REQUIREMENT_LABELS.get(item, item)}" for item in self.requirements)
        if self.checklist:
            lines.append("")
            lines.append("Your checklist:")
            lines.extend(f"  [ ] {item}" for item in self.checklist)
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"  · {item}" for item in self.notes)
        if self.next_action:
            lines.append("")
            lines.append(f"Next action: {self.next_action}")
        return "\n".join(lines)


def _days_label(days: float) -> str:
    if days < 0:
        return f"passed {abs(int(round(days)))} day(s) ago"
    if days < 1:
        return "closes today"
    return f"{int(round(days))} day(s) left"


def _pay_label(opportunity: dict[str, Any]) -> str:
    comp = str(opportunity.get("comp_type") or "unknown")
    label = PAY_LABELS.get(comp, comp)
    amount = opportunity.get("pay_amount")
    currency = opportunity.get("currency") or "INR"
    if amount:
        return f"{label}: {currency} {int(amount):,}"
    return label


def _requirements(text: str) -> list[str]:
    body = normalize_ws(text or "")
    if not body:
        return []
    found = [key for pattern, key in REQUIREMENT_PATTERNS if pattern.search(body)]
    return list(dict.fromkeys(found))


def _eligibility_reasons(opportunity: dict[str, Any]) -> list[str]:
    breakdown = opportunity.get("score_breakdown") or {}
    if not isinstance(breakdown, dict):
        return []
    details = breakdown.get("details") or {}
    eligibility = details.get("eligibility") or breakdown.get("eligibility") or {}
    reasons = eligibility.get("reasons") if isinstance(eligibility, dict) else None
    if reasons:
        return [str(item) for item in reasons]
    if breakdown.get("eligible") is False:
        return [str(item) for item in (breakdown.get("reasons") or [])]
    return []


def build_brief(
    opportunity: dict[str, Any],
    profile: Profile,
    *,
    now: datetime | None = None,
) -> ApplyBrief:
    """Build an application plan from a stored opportunity row."""
    title = str(opportunity.get("title") or "Untitled opportunity")
    brief = ApplyBrief(
        opportunity_id=int(opportunity.get("id") or 0),
        title=title,
        score=float(opportunity.get("score") or 0.0),
    )
    description = str(opportunity.get("description") or "")
    brief.requirements = _requirements(description)

    # Deadline ---------------------------------------------------------
    deadline_dt = parse_dt(opportunity.get("deadline"))
    if deadline_dt:
        brief.deadline = deadline_dt.date().isoformat()
        brief.days_left = round(days_until(deadline_dt, now=now) or 0.0, 1)
        if brief.days_left < 0:
            brief.blockers.append("Deadline has passed — only apply if the post is still up")
        elif brief.days_left <= 2:
            brief.notes.append("Closes in under 48 hours; send a tight, prepared submission")

    # Pay --------------------------------------------------------------
    brief.pay = _pay_label(opportunity)
    comp = str(opportunity.get("comp_type") or "unknown")
    if profile.paid_only and comp in {"credit_only", "unpaid", "unknown"}:
        brief.blockers.append(f"Pay is {brief.pay.lower()}, and your profile is paid-only")
    if comp == "deferred":
        brief.notes.append("Payment is deferred; agree the split in writing before shooting")
    if comp == "credit_only" and "showreel" in brief.requirements:
        brief.notes.append("TFP here is defensible only if the footage is usable for your reel")

    if opportunity.get("fee_required"):
        brief.blockers.append("Asks for an entry/registration fee — treat as a red flag")

    # Location ---------------------------------------------------------
    city = str(opportunity.get("city") or "").strip()
    remote = bool(opportunity.get("remote_ok"))
    if city:
        brief.location = f"{city} (remote/self-tape possible)" if remote else city
        distance = distance_between(profile.base_city, city)
        if distance is not None:
            brief.distance_km = round(distance, 0)
            brief.location += f", ~{int(distance)} km from {profile.base_city}"
            radius = profile.travel_radius_km
            if radius is not None and distance > radius and not remote:
                brief.blockers.append(
                    f"{int(distance)} km away, beyond your {int(radius)} km travel radius"
                )
    elif remote:
        brief.location = "Remote / self-tape"

    # Contact ----------------------------------------------------------
    contact = str(opportunity.get("contact_value") or "").strip()
    channel = str(opportunity.get("contact_channel") or "unknown")
    if contact:
        brief.contact = contact
    elif channel not in {"form", "unknown"}:
        brief.contact = channel
    else:
        # The listing still exists and may be reachable through its own form, so
        # this is a gap to work around, not a reason to drop the lead.
        brief.notes.append(
            "Contact is hidden behind a login or an apply form — the source page is the way in"
        )

    # Eligibility ------------------------------------------------------
    already_checked_distance = any("km away" in item for item in brief.blockers)
    for reason in _eligibility_reasons(opportunity):
        if reason in brief.blockers:
            continue
        if already_checked_distance and "travel radius" in reason:
            continue
        brief.blockers.append(reason)
    if opportunity.get("status") == "expired":
        brief.blockers.append("Listing is marked ended by the source")
    flags = [str(flag) for flag in (opportunity.get("risk_flags") or [])]
    for flag in flags:
        brief.blockers.append(f"Risk flag: {flag}")

    # Checklist --------------------------------------------------------
    checklist: list[str] = []
    if contact:
        action = CHANNEL_ACTIONS.get(channel)
        if action:
            checklist.append(action.format(contact=contact))
        else:
            checklist.append(f"Send your profile to {contact}")
    elif channel == "form":
        checklist.append("Apply through the form linked on the source page")
    if "self-tape" in brief.requirements:
        checklist.append(REQUIREMENT_LABELS["self-tape"])
    elif profile.selftape_ready:
        checklist.append("Have a 60s self-tape ready in case they ask for one")
    for key in ("showreel", "photos", "resume", "monologue", "dance", "singing", "portfolio", "id-proof", "printed-photos"):
        if key in brief.requirements:
            checklist.append(REQUIREMENT_LABELS[key])
    if "photos" not in brief.requirements:
        checklist.append("Keep a headshot and full-body photo ready to attach")
    languages = [str(item) for item in (opportunity.get("languages") or [])]
    if languages:
        checklist.append(f"Prepare a short intro in {' / '.join(languages[:2])}")
    if opportunity.get("audition_format") == "in_person":
        checklist.append("Plan travel to the in-person audition and arrive 20 minutes early")
    if brief.deadline and (brief.days_left or 0) >= 0:
        checklist.append(f"Submit before {brief.deadline}")
    brief.checklist = list(dict.fromkeys(checklist))

    # Next action ------------------------------------------------------
    if brief.blockers:
        brief.next_action = f"Skip unless something changes: {brief.blockers[0]}"
    elif contact:
        brief.next_action = CHANNEL_ACTIONS.get(channel, "Contact {contact}").format(contact=contact)
    elif channel == "form":
        brief.next_action = "Open the source page and apply through its form"
    else:
        brief.next_action = "Open the source page and apply there"
    if brief.days_left is not None and brief.days_left >= 0 and not brief.blockers:
        brief.next_action += f" (before {brief.deadline})"
    return brief


def brief_for_id(
    db: Any,
    profile: Profile,
    opportunity_id: int,
    *,
    now: datetime | None = None,
) -> ApplyBrief | None:
    from . import repo

    opportunity = repo.get_opportunity(db, opportunity_id)
    if not opportunity:
        return None
    return build_brief(opportunity, profile, now=now)


def urgency_band(days_left: float | None) -> str:
    """Coarse urgency label used by the dashboard."""
    if days_left is None:
        return "unscheduled"
    if days_left < 0:
        return "expired"
    if days_left <= 2:
        return "critical"
    if days_left <= 7:
        return "soon"
    return "open"


def confidence_score(brief: ApplyBrief) -> float:
    """0-100 readiness: how much of an application we can assemble today."""
    total = 4.0
    earned = 0.0
    if brief.contact:
        earned += 1.0
    if brief.deadline:
        earned += 1.0
    if not brief.blockers:
        earned += 1.0
    if brief.requirements:
        earned += 1.0
    return round(clamp(earned / total) * 100, 0)
