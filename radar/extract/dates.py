"""Date extraction for casting deadlines, shoot windows and post dates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..util import IST, end_of_day_ist, now_ist, now_utc, parse_dt

MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
    "जनवरी": 1,
    "फरवरी": 2,
    "मार्च": 3,
    "अप्रैल": 4,
    "मई": 5,
    "जून": 6,
    "जुलाई": 7,
    "अगस्त": 8,
    "सितंबर": 9,
    "अक्टूबर": 10,
    "नवंबर": 11,
    "दिसंबर": 12,
}

MONTH_PATTERN = "|".join(sorted((re.escape(m) for m in MONTHS), key=len, reverse=True))

WEEKDAYS = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

DEADLINE_KEYWORDS = re.compile(
    r"(?:deadline|last\s+date|apply\s+(?:by|before|on)|applications?\s+(?:close|deadline)|"
    r"submissions?\s+(?:close|deadline|open)|submit\s+(?:by|before|on)|closes?\s+on|due\s+(?:by|on)|"
    r"cut[\s-]?off|final\s+date|अंतिम\s+तिथि|आवेदन\s+(?:की\s+)?अंतिम|जमा\s+करने\s+की\s+तिथि)",
    re.IGNORECASE,
)

DATE_DMY = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/\.]+({MONTH_PATTERN})[\s\-/\.]+(\d{{2,4}})\b",
    re.IGNORECASE,
)
DATE_MDY = re.compile(
    rf"\b({MONTH_PATTERN})[\s\-/\.]+(\d{{1,2}})(?:st|nd|rd|th)?,?[\s\-/\.]+(\d{{2,4}})\b",
    re.IGNORECASE,
)
DATE_NUMERIC = re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b")
DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
DATE_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-/\.]+({MONTH_PATTERN})\b", re.IGNORECASE)
DATE_MONTH_DAY = re.compile(rf"\b({MONTH_PATTERN})[\s\-/\.]+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
RELATIVE = re.compile(
    r"\b(today|tomorrow|day\s+after\s+tomorrow|in\s+(\d{1,3})\s+(day|days|week|weeks|month|months)|"
    r"(?:by|before|on|next|this)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun))\b",
    re.IGNORECASE,
)


@dataclass
class DeadlineHit:
    dt: datetime
    confidence: str
    quote: str
    keyword: str = ""
    method: str = "rule"

    @property
    def iso(self) -> str:
        return self.dt.isoformat(timespec="seconds")


@dataclass
class WindowHit:
    start: datetime
    end: datetime
    quote: str


def _with_year(day: int, month: int, year: int, base: datetime) -> datetime | None:
    if year < 100:
        year += 2000 if year < 70 else 1900
    # A deadline without a year that already passed is probably next year.
    try:
        candidate = datetime(year, month, day, 23, 59, 59, tzinfo=IST)
    except ValueError:
        return None
    return candidate


def _infer_year(day: int, month: int, base: datetime) -> datetime | None:
    local = base.astimezone(IST) if base.tzinfo else base.replace(tzinfo=IST)
    for year in (local.year, local.year + 1):
        try:
            candidate = datetime(year, month, day, 23, 59, 59, tzinfo=IST)
        except ValueError:
            continue
        if candidate.date() >= local.date():
            return candidate
    return None


def _month_number(value: str) -> int | None:
    return MONTHS.get(value.lower().strip())


def parse_absolute_date(text: str, *, base: datetime | None = None) -> datetime | None:
    base = base or now_ist()
    text = text.strip()
    if not text:
        return None

    # ISO first (unambiguous).
    match = DATE_ISO.search(text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), 23, 59, 59, tzinfo=IST)
        except ValueError:
            pass

    match = DATE_DMY.search(text)
    if match:
        day, month_name, year = match.groups()
        month = _month_number(month_name)
        if month:
            return _with_year(int(day), month, int(year), base)

    match = DATE_MDY.search(text)
    if match:
        month_name, day, year = match.groups()
        month = _month_number(month_name)
        if month:
            return _with_year(int(day), month, int(year), base)

    match = DATE_NUMERIC.search(text)
    if match:
        first, second, year = (int(x) for x in match.groups())
        # Prefer DMY (India/UK), fall back to MDY if the first value cannot be a day.
        day, month = (first, second) if first <= 31 and second <= 12 else (second, first)
        return _with_year(day, month, year, base)

    match = DATE_DAY_MONTH.search(text)
    if match:
        day, month_name = match.groups()
        month = _month_number(month_name)
        if month:
            return _infer_year(int(day), month, base)

    match = DATE_MONTH_DAY.search(text)
    if match:
        month_name, day = match.groups()
        month = _month_number(month_name)
        if month:
            return _infer_year(int(day), month, base)

    # Fall back to dateparser for unusual expressions.
    try:
        import dateparser

        parsed = dateparser.parse(
            text,
            settings={
                "TIMEZONE": "Asia/Kolkata",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "DATE_ORDER": "DMY",
                "RELATIVE_BASE": base,
            },
        )
        if parsed:
            return end_of_day_ist(parsed) if parsed.hour == 0 and parsed.minute == 0 else parsed
    except Exception:
        pass
    return None


def parse_relative(text: str, *, base: datetime | None = None) -> DeadlineHit | None:
    base = base or now_ist()
    local = base.astimezone(IST)
    match = RELATIVE.search(text)
    if not match:
        return None
    phrase = match.group(0).lower()
    if "day after tomorrow" in phrase:
        return DeadlineHit(end_of_day_ist(local + timedelta(days=2)), "medium", match.group(0), "relative")
    if "tomorrow" in phrase:
        return DeadlineHit(end_of_day_ist(local + timedelta(days=1)), "medium", match.group(0), "relative")
    if phrase.startswith("today"):
        return DeadlineHit(end_of_day_ist(local), "high", match.group(0), "relative")
    if match.group(2) and match.group(3):
        count = int(match.group(2))
        unit = match.group(3).lower()
        if unit.startswith("week"):
            delta = timedelta(weeks=count)
        elif unit.startswith("month"):
            delta = timedelta(days=30 * count)
        else:
            delta = timedelta(days=count)
        return DeadlineHit(end_of_day_ist(local + delta), "medium", match.group(0), "relative")
    weekday_name = (match.group(4) or "").lower()
    if weekday_name in WEEKDAYS:
        target = WEEKDAYS[weekday_name]
        days_ahead = (target - local.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        if phrase.startswith("this"):
            days_ahead = (target - local.weekday()) % 7
        return DeadlineHit(end_of_day_ist(local + timedelta(days=days_ahead)), "low", match.group(0), "relative")
    return None


def extract_deadline(text: str, *, base: datetime | None = None) -> DeadlineHit | None:
    """Find the most likely application deadline and its supporting quote."""
    if not text:
        return None
    base = base or now_ist()
    best: DeadlineHit | None = None
    best_score = -1
    for keyword in DEADLINE_KEYWORDS.finditer(text):
        start = max(0, keyword.start() - 30)
        end = min(len(text), keyword.end() + 140)
        window = text[start:end]
        # First absolute date in the window.
        absolute = _first_absolute(window, base)
        relative = parse_relative(window, base=base)
        candidate: DeadlineHit | None = None
        if absolute:
            candidate = DeadlineHit(
                absolute,
                "high",
                _quote_at(text, keyword.start(), keyword.end() + 130),
                keyword.group(0),
            )
        elif relative:
            candidate = relative
            candidate.keyword = keyword.group(0)
        if candidate:
            score = 3 if candidate.confidence == "high" else 2
            if candidate.dt >= base.astimezone(IST) - timedelta(days=1):
                score += 1
            if score > best_score:
                best_score = score
                best = candidate
    if best:
        return best

    # No explicit keyword: look for a date near "apply"/"send"/"submit".
    for anchor in re.finditer(r"\b(apply|send|submit|email|whatsapp|contact)\b", text, re.IGNORECASE):
        start = max(0, anchor.start() - 40)
        end = min(len(text), anchor.end() + 120)
        window = text[start:end]
        absolute = _first_absolute(window, base)
        if absolute and absolute >= base.astimezone(IST) - timedelta(days=1):
            return DeadlineHit(
                absolute,
                "medium",
                _quote_at(text, anchor.start(), anchor.end() + 120),
                anchor.group(0),
            )
    return None


def _first_absolute(text: str, base: datetime) -> datetime | None:
    # Try each regex in order of reliability; return the earliest sensible date.
    candidates: list[datetime] = []
    for regex in (DATE_ISO, DATE_DMY, DATE_MDY, DATE_NUMERIC, DATE_DAY_MONTH, DATE_MONTH_DAY):
        for match in regex.finditer(text):
            dt = parse_absolute_date(match.group(0), base=base)
            if dt and dt >= base.astimezone(IST) - timedelta(days=365):
                candidates.append(dt)
        if candidates:
            break
    return min(candidates) if candidates else None


def _quote_at(text: str, start: int, end: int) -> str:
    """A verbatim, whitespace-normalised quote beginning at a keyword."""
    snippet = text[max(0, start) : min(len(text), end)]
    clean = re.sub(r"\s+", " ", snippet).strip()
    return clean[:300]


def extract_published(text: str | None, *, fallback: datetime | None = None) -> datetime | None:
    if not text:
        return fallback or now_utc()
    parsed = parse_dt(text)
    if parsed:
        return parsed
    return parse_absolute_date(text) or fallback or now_utc()


def extract_shoot_window(text: str, *, base: datetime | None = None) -> WindowHit | None:
    if not text:
        return None
    base = base or now_ist()
    pattern = re.compile(
        r"(?:shoot(?:ing)?|filming|production)\s*(?:dates?|window|schedule|from)?\s*[:\-]?\s*"
        r"([^\n]{0,140})",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        window = match.group(1)
        dates: list[datetime] = []
        for regex in (DATE_DMY, DATE_MDY, DATE_NUMERIC, DATE_ISO, DATE_DAY_MONTH, DATE_MONTH_DAY):
            for found in regex.finditer(window):
                dt = parse_absolute_date(found.group(0), base=base)
                if dt and dt not in dates:
                    dates.append(dt)
            if len(dates) >= 2:
                break
        if len(dates) >= 2:
            first, second = sorted(dates)[:2]
            return WindowHit(first, second, match.group(0).strip()[:240])
    # Compact ranges like "20-23 Nov 2026".
    compact = re.compile(
        rf"\b(\d{{1,2}})\s*[-–to]+\s*(\d{{1,2}})[\s\-/\.]+({MONTH_PATTERN})[\s\-/\.]+(\d{{2,4}})\b",
        re.IGNORECASE,
    )
    match = compact.search(text)
    if match:
        d1, d2, month_name, year = match.groups()
        month = _month_number(month_name)
        if month:
            a = _with_year(int(d1), month, int(year), base)
            b = _with_year(int(d2), month, int(year), base)
            if a and b:
                return WindowHit(a, b, match.group(0))
    return None


def deadline_confidence_from_hit(hit: DeadlineHit | None) -> str:
    return hit.confidence if hit else "unknown"
