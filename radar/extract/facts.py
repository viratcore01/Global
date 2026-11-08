"""Parse structured fact tables from casting and festival pages.

Casting platforms publish a small label/value table above the free text:
"Payment terms / Not paid", "Casting dates / to 26 November 2026",
"Wanted / 13-16 years old, M".  Those values are written by the poster and are
far more trustworthy than anything guessed from prose, so they are parsed here
and used to fill only the fields the free text did not already answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..cities import find_city
from ..util import normalize_ws
from .dates import parse_absolute_date
from .pay import classify_compensation

# Label -> canonical fact name.  Matching is done on a normalised label line,
# so "Country, city" and "Country / City" and "Country & city" all work.
LABELS: dict[str, str] = {
    "payment terms": "comp",
    "payment": "comp",
    "pay": "comp",
    "paid": "comp",
    "compensation": "comp",
    "fee": "comp",
    "remuneration": "comp",
    "casting dates": "deadline",
    "audition dates": "deadline",
    "auditions dates": "deadline",
    "shoot dates": "shoot",
    "last date": "deadline",
    "deadline": "deadline",
    "apply before": "deadline",
    "applications close": "deadline",
    "closing date": "deadline",
    "country, city": "location",
    "country / city": "location",
    "country city": "location",
    "country": "location",
    "city": "location",
    "location": "location",
    "wanted": "wanted",
    "age": "wanted",
    "age range": "wanted",
    "age group": "wanted",
    "looking for": "wanted",
    "requirement": "wanted",
    "requirements": "wanted",
    "status": "status",
    "type of the project": "project_type",
    "project type": "project_type",
    "type of project": "project_type",
}

NORMALISE_LABEL_RE = re.compile(r"[^a-z0-9, /&]+")
AGE_RANGE_RE = re.compile(r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)?\b", re.IGNORECASE)
AGE_PLUS_RE = re.compile(r"\b(\d{1,2})\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*\d{2,4}"
    r"|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}"
    r"|\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)
GENDER_WORDS = (
    (re.compile(r"\b(?:m|male|man|boys?)\b", re.I), "male"),
    (re.compile(r"\b(?:f|female|woman|girls?)\b", re.I), "female"),
    (re.compile(r"\b(?:any\s+gender|both|all)\b", re.I), "any"),
)
AMOUNT_VALUE_RE = re.compile(
    r"(?:(?:₹|rs\.?|inr|\$|usd|£|€|aed)\s*)?"
    r"(\d{1,3}(?:,\d{2,3})+|\d{1,9}(?:\.\d+)?)\s*"
    r"(k|thousand|lakh|lakhs|lac|lacs|crore|crores|cr)?",
    re.IGNORECASE,
)
CURRENCY_VALUE_RE = re.compile(r"(₹|rs\.?|inr|\$|usd|£|€|aed)", re.IGNORECASE)
MULTIPLIERS = {"k": 1_000, "thousand": 1_000, "lakh": 100_000, "lakhs": 100_000,
               "lac": 100_000, "lacs": 100_000, "crore": 10_000_000, "crores": 10_000_000,
               "cr": 10_000_000}

CLOSED_VALUE_RE = re.compile(r"\b(?:ended|expired|closed|filled|no\s+longer)\b", re.I)
OPEN_VALUE_RE = re.compile(r"\b(?:open|active|live|accepting)\b", re.I)


@dataclass
class FactResult:
    fields: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def known(self) -> bool:
        return bool(self.fields)


def _label_key(line: str) -> str | None:
    cleaned = NORMALISE_LABEL_RE.sub(" ", (line or "").lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" :,-/")
    return LABELS.get(cleaned)


# Everything after one of these headings is another listing's copy, not the
# page's own facts.  Boards repeat whole cards ("Other current auditions") which
# otherwise leak a different role's age, pay and city into this record.
RELATED_HEADING_RE = re.compile(
    r"^(?:see\s+also|other\s+(?:current\s+)?auditions?|related\s+(?:auditions?|jobs?|posts?)"
    r"|you\s+may\s+also\s+like|more\s+auditions?|similar\s+(?:auditions?|jobs?)|recommended)\b",
    re.IGNORECASE,
)


def _iter_pairs(text: str) -> list[tuple[str, str]]:
    """Find label/value pairs in ``Label: value`` or ``Label`` / ``value`` form."""
    lines = [normalize_ws(line) for line in (text or "").splitlines()]
    lines = [line for line in lines if line]
    pairs: list[tuple[str, str]] = []
    for index, line in enumerate(lines):
        if RELATED_HEADING_RE.match(line):
            break
        if len(line) > 60:
            continue
        if ":" in line:
            label_part, _, value_part = line.partition(":")
            key = _label_key(label_part)
            if key and value_part.strip():
                pairs.append((key, value_part.strip()))
                continue
        key = _label_key(line)
        if key and index + 1 < len(lines):
            value = lines[index + 1]
            # Values such as "$" or "-" are split across lines ("$" then
            # "Fee + royalty"); join the next short line when that happens.
            if (len(value) <= 3 or value.endswith(",")) and index + 2 < len(lines):
                value = f"{value} {lines[index + 2]}".strip()
            if 0 < len(value) <= 120:
                pairs.append((key, value))
    return pairs


def _parse_comp(value: str) -> tuple[str | None, str]:
    text = value.lower()
    if re.search(r"\bnot\s+paid\b|\bunpaid\b|\bno\s+pay\b", text):
        return "unpaid", value
    if re.search(r"\bcredit\s*only\b", text):
        return "credit_only", value
    compensation = classify_compensation(value)
    if compensation.known:
        return compensation.comp_type, value
    return None, value


def _parse_amount(value: str) -> tuple[float | None, str | None]:
    """Money stated in a fact value, e.g. "6 lakh", "Rs 5,000/day"."""
    match = AMOUNT_VALUE_RE.search(value or "")
    if not match:
        return None, None
    raw = match.group(1).replace(",", "")
    try:
        amount = float(raw)
    except ValueError:
        return None, None
    suffix = (match.group(2) or "").lower()
    if re.search(r"years?\s*old|\bage\b|\b\d{1,2}\s*[-–]\s*\d{1,2}\b", value or "", re.I):
        return None, None
    has_currency = bool(CURRENCY_VALUE_RE.search(value or ""))
    has_suffix = suffix in MULTIPLIERS
    has_grouping = "," in match.group(1)
    if amount <= 0:
        return None, None
    # A bare small number is a count ("18-42" age, "3 roles"), not pay.
    if not (has_currency or has_suffix or has_grouping or amount >= 1000):
        return None, None
    if has_suffix:
        amount *= MULTIPLIERS[suffix]
    currency_token = CURRENCY_VALUE_RE.search(value or "")
    currency = "INR"
    if currency_token:
        token = currency_token.group(0).lower().rstrip(".")
        currency = {"$": "USD", "usd": "USD", "£": "GBP", "€": "EUR", "aed": "AED"}.get(token, "INR")
    return amount, currency


def _parse_deadline(value: str, base: datetime | None) -> str | None:
    """The last date mentioned wins: "1 Oct to 26 Nov 2026" closes in November."""
    tokens = DATE_TOKEN_RE.findall(value)
    for token in reversed(tokens):
        parsed = parse_absolute_date(token, base=base)
        if parsed:
            return parsed.date().isoformat()
    return None


def _parse_wanted(value: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    range_match = AGE_RANGE_RE.search(value)
    if range_match:
        low, high = int(range_match.group(1)), int(range_match.group(2))
        fields["age_min"], fields["age_max"] = min(low, high), max(low, high)
    else:
        plus = AGE_PLUS_RE.search(value)
        if plus:
            fields["age_min"] = int(plus.group(1))
    for pattern, gender in GENDER_WORDS:
        if pattern.search(value):
            fields["gender_presentation"] = gender
            break
    return fields


def extract_facts(text: str, *, base: datetime | None = None) -> FactResult:
    """Extract high-confidence facts from label/value blocks."""
    result = FactResult()
    for key, value in _iter_pairs(text):
        result.pairs.append((key, value))
        if key == "comp":
            comp, _ = _parse_comp(value)
            if comp and "comp_type" not in result.fields:
                result.fields["comp_type"] = comp
                result.evidence["comp_type"] = value[:200]
            amount, currency = _parse_amount(value)
            if amount is not None and "pay_amount" not in result.fields:
                result.fields["pay_amount"] = amount
                if currency:
                    result.fields["currency"] = currency
                result.fields.setdefault("comp_type", "flat")
                result.evidence["pay_amount"] = value[:200]
        elif key == "deadline" and "deadline" not in result.fields:
            deadline = _parse_deadline(value, base)
            if deadline:
                result.fields["deadline"] = deadline
                result.fields["deadline_confidence"] = "high"
                result.evidence["deadline"] = value[:200]
        elif key == "location" and "city" not in result.fields:
            hit = find_city(value)
            if hit:
                result.fields["city"] = hit.city.title()
                if hit.country:
                    result.fields["country"] = hit.country
                result.evidence["city"] = value[:200]
            else:
                parts = [part.strip() for part in re.split(r"[,/]", value) if part.strip()]
                if len(parts) >= 2:
                    result.fields["country"] = parts[0][:80]
                    result.fields["city"] = parts[1][:80]
                    result.evidence["city"] = value[:200]
        elif key == "wanted":
            for name, parsed in _parse_wanted(value).items():
                result.fields.setdefault(name, parsed)
            if result.fields.get("age_min") is not None:
                result.evidence["age_min"] = value[:200]
        elif key == "status":
            if CLOSED_VALUE_RE.search(value):
                result.fields["status"] = "expired"
                result.evidence["status"] = value[:200]
            elif OPEN_VALUE_RE.search(value):
                result.fields["status"] = "new"
        elif key == "project_type" and "project_type" not in result.fields:
            result.fields["project_type"] = value[:120]
    return result
