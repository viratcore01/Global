"""Structured duplicate keys.

Exact keys catch identical facts.  ``fuzzy_match`` is used for rewritten posts
whose title changed slightly but whose city, deadline and contact still agree.
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from ..normalize import normalize_email, normalize_phone, normalize_ws
from ..util import parse_dt, sha256_text

GENERIC_TITLE_WORDS = {
    "casting",
    "call",
    "audition",
    "auditions",
    "open",
    "urgent",
    "required",
    "wanted",
    "need",
    "needed",
    "looking",
    "for",
    "the",
    "a",
    "an",
    "role",
    "actor",
    "actress",
    "male",
    "female",
    "fresher",
    "freshers",
    "paid",
    "unpaid",
    "project",
    "film",
    "short",
}


def _title_core(title: str) -> str:
    text = normalize_ws(title or "").lower()
    text = re.sub(r"[^\w\u0900-\u097F ]+", " ", text, flags=re.UNICODE)
    tokens = [token for token in text.split() if token and token not in GENERIC_TITLE_WORDS]
    # Preserve order but de-duplicate to reduce boilerplate inflation.
    return " ".join(list(dict.fromkeys(tokens))[:14])


def normalize_contact(value: str | None) -> str:
    if not value:
        return ""
    value = normalize_ws(value)
    if "@" in value:
        return normalize_email(value) or value.lower()
    phone = normalize_phone(value)
    if phone:
        return phone
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def deadline_day(value: Any) -> str:
    parsed = parse_dt(value)
    return parsed.date().isoformat() if parsed else ""


def make_structured_key(
    *,
    title: str,
    city: Any = None,
    deadline: Any = None,
    contact: Any = None,
) -> str:
    raw = "|".join(
        (
            _title_core(str(title or "")),
            normalize_ws(str(city or "")).lower(),
            deadline_day(deadline),
            normalize_contact(str(contact or "")),
        )
    )
    return sha256_text(raw)[:32]


def fuzzy_match(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    threshold: float = 0.72,
) -> bool:
    """Whether two opportunity dicts describe the same underlying call.

    A shared contact is strong evidence.  Otherwise title + city + deadline must
    agree, because generic titles such as "casting call" are extremely common.
    """
    l_title = _title_core(str(left.get("title") or ""))
    r_title = _title_core(str(right.get("title") or ""))
    title_score = fuzz.token_set_ratio(l_title, r_title) / 100.0 if l_title and r_title else 0.0
    l_contact = normalize_contact(str(left.get("contact_value") or ""))
    r_contact = normalize_contact(str(right.get("contact_value") or ""))
    same_contact = bool(l_contact and l_contact == r_contact)
    l_city = normalize_ws(str(left.get("city") or "")).lower()
    r_city = normalize_ws(str(right.get("city") or "")).lower()
    same_city = bool(l_city and l_city == r_city)
    l_deadline = deadline_day(left.get("deadline"))
    r_deadline = deadline_day(right.get("deadline"))
    same_deadline = bool(l_deadline and l_deadline == r_deadline)

    # Same contact plus a plausible title is enough even if a repost omitted a
    # city or deadline.
    if same_contact and title_score >= 0.45:
        return True
    if same_contact and same_city and title_score >= 0.30:
        return True

    score = title_score * 0.55
    score += 0.20 if same_contact else 0.0
    score += 0.15 if same_city else 0.0
    score += 0.10 if same_deadline else 0.0
    if same_city and same_deadline and title_score >= 0.55:
        return score >= threshold - 0.10
    return score >= threshold and (same_city or same_deadline or same_contact)
