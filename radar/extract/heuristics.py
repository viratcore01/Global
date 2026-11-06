"""Deterministic field extraction from page text, email bodies and captions.

Rules and regexes decide what a post says. The LLM may only fill remaining gaps
later, and every quote it returns is verified against the source text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..cities import find_city
from ..normalize import (
    detect_languages,
    evidence_text,
    extract_emails,
    extract_phones,
    extract_urls,
    is_probably_not_opportunity,
    looks_like_casting,
    looks_like_crew,
    looks_like_festival,
    normalize_text,
)
from ..util import normalize_ws, now_ist, truncate
from .dates import DeadlineHit, extract_deadline, extract_shoot_window

CURRENCY_MAP = {
    "₹": "INR",
    "rs": "INR",
    "rs.": "INR",
    "inr": "INR",
    "rupee": "INR",
    "rupees": "INR",
    "$": "USD",
    "usd": "USD",
    "dollar": "USD",
    "dollars": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "€": "EUR",
    "eur": "EUR",
    "aed": "AED",
}

PAY_CONTEXT = re.compile(
    # \b after "pay" keeps "payable", "pays" and "payment" from being read as
    # a pay statement ("payable on WhatsApp ... only 3 slots" is not pay).
    r"(?:pay\b|salary|honorarium|stipend|remuneration|compensation|fee|budget|"
    r"day\s*rate|per\s*day|per\s*project|flat\s*fee)",
    re.IGNORECASE,
)
UNPAID_RE = re.compile(
    r"\b(?:unpaid|no\s+pay|without\s+pay|not\s+paid|credit[\s-]?only|deferred|profit[\s-]?sharing|revenue[\s-]?share|volunteer)\b",
    re.IGNORECASE,
)
FEE_RE = re.compile(
    r"\b(?:entry|submission|registration|application|audition|participation)\s+(?:fee|charge|charges|amount)\b",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"(?:(?:₹|rs\.?|inr|\$|usd|£|gbp|€|eur|aed)\s*)?"
    # Grouped Indian/Western amounts, or at most 9 plain digits.  The digit
    # boundaries stop 10-digit phone numbers being parsed as pay (and stop a
    # match starting in the middle of a phone number).
    r"(?<!\d)(\d{1,3}(?:,\d{2,3})+|\d{1,9}(?:\.\d+)?)\s*"
    r"(k|thousand|lakh|lakhs|lac|lacs|crore|crores|cr)?(?![a-z0-9])",
    re.IGNORECASE,
)

ROLE_PATTERNS: dict[str, re.Pattern[str]] = {
    "lead": re.compile(r"\b(?:lead|main|title)\s+(?:role|character|actor|actress)\b|\blead\b", re.I),
    "supporting": re.compile(r"\b(?:supporting|secondary|character)\s+(?:role|actor|actress)?\b", re.I),
    "extra": re.compile(r"\b(?:extras?|background\s+(?:artist|actor|actress)|junior\s+artist)\b", re.I),
    "voiceover": re.compile(r"\b(?:voice[\s-]?over|voice\s+artist|dubbing|narrator)\b", re.I),
    "web_series": re.compile(r"\b(?:web\s*series|ott\s+series|digital\s+series|web\s+show)\b", re.I),
    "short_film": re.compile(r"\b(?:short\s+film|shortfilm|short\s+feature)\b", re.I),
    "feature_film": re.compile(r"\b(?:feature\s+film|feature\s+length|bollywood|film)\b", re.I),
    "advertisement": re.compile(r"\b(?:advertisement|ad\s*film|commercial|tvc|ad\s+shoot)\b", re.I),
    "modelling": re.compile(r"\b(?:model(?:ling)?|model\s+required|print\s+shoot|fashion\s+shoot)\b", re.I),
    "theatre": re.compile(r"\b(?:theatre|theater|stage\s+play|nukkad)\b", re.I),
    "crew": re.compile(r"\b(?:crew|dop|director\s+of\s+photography|editor|gaffer|grip|sound\s+recordist|production\s+assistant)\b", re.I),
}

GENDER_PATTERNS = [
    (re.compile(r"\b(?:any\s+gender|male\s*/\s*female|female\s*/\s*male|both\s+male\s+and\s+female|gender\s*[:\-]?\s*any|open\s+to\s+all)\b", re.I), "any"),
    (re.compile(r"\b(?:non[\s-]?binary|trans(?:gender)?|genderqueer)\b", re.I), "nonbinary"),
    (re.compile(r"\b(?:gender|role|sex)\s*[:\-]?\s*(?:female|woman|girl)\b", re.I), "female"),
    (re.compile(r"\b(?:gender|role|sex)\s*[:\-]?\s*(?:male|man|boy)\b", re.I), "male"),
    (re.compile(r"\bfemale\b\s*[,()]?\s*(?:age|aged)\b", re.I), "female"),
    (re.compile(r"\bmale\b\s*[,()]?\s*(?:age|aged)\b", re.I), "male"),
    (re.compile(r"\b(?:male\s+only|only\s+male|male\s+actor|male\s+artist|looking\s+for\s+(?:a\s+)?(?:male|boy|man)|role\s*[:\-]?\s*male)\b", re.I), "male"),
    (re.compile(r"\b(?:female\s+only|only\s+female|female\s+actor|female\s+artist|looking\s+for\s+(?:a\s+)?(?:female|girl|woman)|role\s*[:\-]?\s*female)\b", re.I), "female"),
]

NUDITY_PATTERNS = [
    (re.compile(r"\b(?:no|without|zero)\s+(?:nudity|nude|bold)\s*(?:scenes?)?\b", re.I), "none"),
    (re.compile(r"\b(?:implied|suggestive|intimate)\s+(?:nudity|scene|scenes)?\b|\bbold\s+scenes?\b", re.I), "implied"),
    (re.compile(r"\btopless\b", re.I), "topless"),
    (re.compile(r"\bfull\s+(?:frontal\s+)?nudity\b|\bnude\s+scenes?\b", re.I), "full"),
]

AUDITION_PATTERNS = [
    (re.compile(r"\b(?:self[\s-]?tape|self[\s-]?test|video\s+(?:submission|audition)|record\s+(?:a\s+)?video)\b", re.I), "self_tape"),
    (re.compile(r"\b(?:zoom|google\s+meet|video\s+call|online\s+audition|virtual\s+audition)\b", re.I), "zoom"),
    (re.compile(r"\b(?:walk[\s-]?in|in[\s-]?person|open\s+audition|studio\s+audition|come\s+to)\b", re.I), "in_person"),
    (re.compile(r"\b(?:phone\s+audition|telephonic\s+audition)\b", re.I), "phone"),
]

REMOTE_RE = re.compile(r"\b(?:remote|work\s+from\s+home|wfh|online|from\s+anywhere|anywhere\s+in\s+india)\b", re.I)
TRAVEL_RE = re.compile(r"\b(?:travel\s+(?:covered|provided|reimbursed|allowance)|outstation\s+(?:travel|stay)|stay\s+(?:provided|covered))\b", re.I)
USAGE_RE = re.compile(r"\b(?:perpetual|in\s+perpetuity|buyout|all\s+rights|worldwide\s+rights|limited\s+rights|territory)\b", re.I)
FORM_URL_RE = re.compile(r"(?:forms?|apply|audition|casting|typeform|airtable|jotform|docs\.google\.com/forms)", re.I)


@dataclass
class HeuristicResult:
    fields: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    signals: dict[str, Any] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def is_opportunity(self) -> bool:
        return bool(self.signals.get("is_opportunity"))


def extract_heuristics(
    text: str,
    *,
    title_hint: str | None = None,
    url: str | None = None,
    source_type: str = "web",
    base: datetime | None = None,
    assume_opportunity: bool = False,
) -> HeuristicResult:
    base = base or now_ist()
    clean = normalize_text(text or "")
    result = HeuristicResult()
    fields = result.fields

    title = _clean_title(title_hint, clean)
    if title:
        fields["title"] = title
        if title in clean:
            result.evidence["title"] = title

    opportunity_type = _detect_type(clean)
    fields["opportunity_type"] = opportunity_type
    is_opp = assume_opportunity or _is_opportunity(clean, opportunity_type)
    if is_probably_not_opportunity(clean) and not assume_opportunity:
        is_opp = False
    result.signals["is_opportunity"] = is_opp
    result.signals["opportunity_type"] = opportunity_type
    result.signals["looks_like_casting"] = looks_like_casting(clean)
    result.signals["looks_like_festival"] = looks_like_festival(clean)
    result.signals["looks_like_crew"] = looks_like_crew(clean)

    if not is_opp:
        fields["description"] = truncate(clean, 4000)
        return result

    # Deadline ------------------------------------------------------------
    deadline_hit = extract_deadline(clean, base=base)
    if deadline_hit:
        fields["deadline"] = deadline_hit.iso
        fields["deadline_confidence"] = deadline_hit.confidence
        result.evidence["deadline"] = deadline_hit.quote
        result.notes["deadline_keyword"] = deadline_hit.keyword

    # Location ------------------------------------------------------------
    city_hit = find_city(clean)
    location_match = re.search(
        r"(?:location|city|shoot\s+(?:in|at)|based\s+in|place)\s*[:\-]\s*([^\n\.;]{2,60})",
        clean,
        re.IGNORECASE,
    )
    if city_hit:
        fields["city"] = city_hit.city.title()
        fields["country"] = city_hit.country or None
        result.evidence["city"] = _snippet_around(clean, city_hit.matched)
    elif location_match:
        raw_city = location_match.group(1).strip(" -")
        if not re.search(r"\b(?:remote|anywhere|online)\b", raw_city, re.I):
            fields["city"] = raw_city[:80]
            country = _detect_country(clean)
            if country:
                fields["country"] = country
            result.evidence["city"] = location_match.group(0)[:200]
    if not fields.get("country"):
        country = _detect_country(clean)
        if country:
            fields["country"] = country

    if REMOTE_RE.search(clean):
        fields["remote_ok"] = True
        result.evidence["remote_ok"] = _snippet_around(clean, REMOTE_RE.search(clean).group(0))

    # Languages -----------------------------------------------------------
    languages = detect_languages(clean)
    explicit = []
    for name in (
        "Hindi",
        "English",
        "Hinglish",
        "Tamil",
        "Telugu",
        "Malayalam",
        "Kannada",
        "Marathi",
        "Bengali",
        "Punjabi",
        "Gujarati",
        "Urdu",
    ):
        if re.search(rf"\b{name}\b", clean, re.I) and name not in explicit:
            explicit.append(name)
    for lang in explicit:
        if lang not in languages:
            languages.append(lang)
    if languages:
        fields["languages"] = languages

    # Role types ----------------------------------------------------------
    role_types = [name for name, pattern in ROLE_PATTERNS.items() if pattern.search(clean)]
    if opportunity_type == "voiceover" and "voiceover" not in role_types:
        role_types.append("voiceover")
    if opportunity_type == "crew" and "crew" not in role_types:
        role_types.append("crew")
    if role_types:
        fields["role_types"] = list(dict.fromkeys(role_types))

    # Pay / fee -----------------------------------------------------------
    comp, pay_amount, currency, pay_quote = _extract_pay(clean)
    if comp:
        fields["comp_type"] = comp
    if pay_amount is not None:
        fields["pay_amount"] = pay_amount
    if currency:
        fields["currency"] = currency
    if pay_quote:
        result.evidence["comp_type"] = pay_quote
        if pay_amount is not None:
            result.evidence["pay_amount"] = pay_quote

    fee, fee_amount, fee_quote = _extract_fee(clean)
    if fee:
        fields["fee_required"] = True
        if fee_amount is not None:
            fields["fee_amount"] = fee_amount
        if fee_quote:
            result.evidence["fee_required"] = fee_quote
    elif opportunity_type == "festival":
        fields["fee_required"] = None
    else:
        fields["fee_required"] = False

    # Age -----------------------------------------------------------------
    age_min, age_max, age_quote = _extract_age(clean)
    if age_min is not None:
        fields["age_min"] = age_min
    if age_max is not None:
        fields["age_max"] = age_max
    if age_quote:
        result.evidence["age_min" if age_min is not None else "age_max"] = age_quote

    # Gender / nudity / audition format ----------------------------------
    for pattern, value in GENDER_PATTERNS:
        match = pattern.search(clean)
        if match:
            fields["gender_presentation"] = value
            result.evidence["gender_presentation"] = _snippet_around(clean, match.group(0))
            break

    for pattern, value in NUDITY_PATTERNS:
        match = pattern.search(clean)
        if match:
            fields["nudity"] = value
            result.evidence["nudity"] = _snippet_around(clean, match.group(0))
            break

    for pattern, value in AUDITION_PATTERNS:
        match = pattern.search(clean)
        if match:
            fields["audition_format"] = value
            result.evidence["audition_format"] = _snippet_around(clean, match.group(0))
            break

    # Shoot window --------------------------------------------------------
    window = extract_shoot_window(clean, base=base)
    if window:
        fields["shoot_window"] = [window.start.date().isoformat(), window.end.date().isoformat()]
        result.evidence["shoot_window"] = window.quote

    # Travel / rights -----------------------------------------------------
    travel = TRAVEL_RE.search(clean)
    if travel:
        fields["travel_covered"] = True
        result.evidence["travel_covered"] = _snippet_around(clean, travel.group(0))

    usage = USAGE_RE.search(clean)
    if usage:
        text_value = usage.group(0).lower()
        if "perpetual" in text_value or "perpetuity" in text_value or "buyout" in text_value or "all rights" in text_value:
            fields["usage_rights"] = "perpetual_buyout"
        else:
            fields["usage_rights"] = "limited"
        result.evidence["usage_rights"] = _snippet_around(clean, usage.group(0))

    # Contact -------------------------------------------------------------
    contact_channel, contact_value, contact_quote = _extract_contact(clean, url=url)
    if contact_channel:
        fields["contact_channel"] = contact_channel
    if contact_value:
        fields["contact_value"] = contact_value
    if contact_quote:
        result.evidence["contact_value"] = contact_quote

    fields["description"] = truncate(clean, 8000)
    if fields.get("title") and "title" not in result.evidence:
        # Title came from a page heading/title tag; the pipeline verifies it
        # against the combined title hint + body evidence text.
        result.evidence["title"] = fields["title"]
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_title(hint: str | None, body: str) -> str | None:
    candidates: list[str] = []
    if hint:
        candidates.append(normalize_ws(hint))
    if body:
        for line in body.splitlines()[:20]:
            line = normalize_ws(line)
            if 3 <= len(line) <= 220 and not re.match(r"^(home|menu|login|sign in|about|contact|search)\b", line, re.I):
                candidates.append(line)
    for candidate in candidates:
        candidate = re.sub(r"\s*[|•]\s*[^|•]{1,40}$", "", candidate).strip()
        candidate = re.sub(r"\s+-\s+[^-]{1,40}$", "", candidate).strip()
        if len(candidate) >= 3 and not candidate.lower() in {"home", "casting", "auditions", "jobs", "welcome"}:
            return candidate[:400]
    return None


def _detect_type(text: str) -> str:
    lower = text.lower()
    if looks_like_festival(text) and not re.search(r"\b(?:casting|audition|actor|actress)\b", lower):
        return "festival"
    if looks_like_festival(text) and re.search(r"\b(?:submission|call for entries|deadline|entry fee)\b", lower):
        return "festival"
    if re.search(r"\b(?:voice[\s-]?over|voice\s+artist|dubbing)\b", lower):
        return "voiceover"
    if looks_like_crew(text):
        return "crew"
    if re.search(r"\b(?:model(?:ling)?|fashion\s+shoot|print\s+shoot)\b", lower):
        return "modelling"
    if looks_like_casting(text):
        return "casting"
    return "other"


def _is_opportunity(text: str, opportunity_type: str) -> bool:
    if opportunity_type != "other":
        return True
    lower = text.lower()
    return bool(
        re.search(r"\b(?:opportunit(?:y|ies)|hiring|required|wanted|call\s+for|open\s+call|apply)\b", lower)
        and re.search(r"\b(?:actor|actress|artist|performer|crew|film|role|project)\b", lower)
    )


def _detect_country(text: str) -> str | None:
    countries = {
        "India": r"\b(?:india|indian)\b",
        "United States": r"\b(?:usa|u\.s\.a|united states|america)\b",
        "United Kingdom": r"\b(?:uk|u\.k\.|united kingdom|britain)\b",
        "United Arab Emirates": r"\b(?:uae|u\.a\.e\.|united arab emirates)\b",
        "Canada": r"\bcanada\b",
        "Australia": r"\baustralia\b",
        "France": r"\bfrance\b",
        "Germany": r"\bgermany\b",
        "Singapore": r"\bsingapore\b",
    }
    for country, pattern in countries.items():
        if re.search(pattern, text, re.I):
            return country
    return None


def _snippet_around(text: str, needle: str, radius: int = 90) -> str:
    """Return an exact, sentence-ish substring around a match.

    The result is whitespace-normalised but still a verbatim slice of the
    source (after the same normalisation), so span grounding can verify it.
    """
    if not needle:
        return ""
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return normalize_ws(needle)[:240]

    left = max(0, idx - radius)
    for delimiter in ("\n", "|", ". ", "; ", "! ", "? "):
        pos = text.rfind(delimiter, max(0, idx - radius - 40), idx)
        if pos != -1:
            left = max(left, pos + len(delimiter))
    # Do not start in the middle of a word.
    if left > 0 and not text[left - 1].isspace() and text[left].isalnum():
        space = text.find(" ", left, idx)
        if space != -1:
            left = space + 1

    right = min(len(text), idx + len(needle) + radius)
    for delimiter in ("\n", "|", ". ", "; ", "! ", "? "):
        pos = text.find(delimiter, idx + len(needle), right + 40)
        if pos != -1:
            right = min(right, pos + len(delimiter))
            break
    snippet = text[left:right].strip()
    return normalize_ws(snippet)[:300]


def _clause(text: str, start: int, end: int) -> str:
    """Return the sentence-ish clause containing text[start:end].

    Used to decide whether an amount is performer pay or an entry fee, without
    letting a fee in the previous sentence poison the next pay statement.
    """
    left = max(0, start - 80)
    for delimiter in ("\n", ". ", "; ", "| ", "! ", "? "):
        pos = text.rfind(delimiter, left, start)
        if pos != -1:
            left = max(left, pos + len(delimiter))
    right = min(len(text), end + 40)
    for delimiter in ("\n", ". ", "; ", "| ", "! ", "? "):
        pos = text.find(delimiter, end, right)
        if pos != -1:
            right = min(right, pos + len(delimiter))
    return text[left:right]


def _amount(value: str, multiplier: str | None) -> float | None:
    try:
        number = float(value.replace(",", ""))
    except ValueError:
        return None
    mult = (multiplier or "").lower()
    if mult in {"k", "thousand"}:
        number *= 1000
    elif mult in {"lakh", "lakhs", "lac", "lacs"}:
        number *= 100_000
    elif mult in {"crore", "crores", "cr"}:
        number *= 10_000_000
    return number if number >= 0 else None


def _currency_from(text: str) -> str | None:
    lower = text.lower()
    for marker, currency in (
        ("₹", "INR"),
        ("rs", "INR"),
        ("inr", "INR"),
        ("$", "USD"),
        ("usd", "USD"),
        ("£", "GBP"),
        ("gbp", "GBP"),
        ("€", "EUR"),
        ("eur", "EUR"),
        ("aed", "AED"),
        ("dirham", "AED"),
    ):
        if marker in lower:
            return currency
    return None


def _extract_pay(text: str) -> tuple[str | None, float | None, str | None, str | None]:
    unpaid = UNPAID_RE.search(text)
    if unpaid:
        phrase = unpaid.group(0).lower()
        comp = "credit_only" if "credit" in phrase else ("deferred" if ("deferred" in phrase or "profit" in phrase or "revenue" in phrase) else "unpaid")
        return comp, None, None, _snippet_around(text, unpaid.group(0))

    pay_amount: float | None = None
    currency: str | None = None
    quote: str | None = None
    for match in PAY_CONTEXT.finditer(text):
        # Prefer the text after the pay keyword ("Pay: Rs 5000 per day"); only
        # look slightly before it when the amount precedes the keyword
        # ("Rs 5000 per day"). This avoids catching an age range earlier in
        # the same sentence.
        after_start = match.start()
        after = text[after_start : min(len(text), match.end() + 120)]
        before_start = max(0, match.start() - 40)
        before = text[before_start : match.end()]
        if AMOUNT_RE.search(after):
            window, window_start = after, after_start
        else:
            window, window_start = before, before_start
        amount_match = AMOUNT_RE.search(window)
        if amount_match:
            # A festival entry / submission fee is not performer pay. The
            # keyword itself may be "fee", so inspect the clause around the
            # amount instead of a fixed window around the keyword.
            context = _clause(
                text,
                window_start + amount_match.start(),
                window_start + amount_match.end(),
            )
            if FEE_RE.search(context) or re.search(
                r"\b(?:entry|submission|registration|application|audition|participation)\s+(?:fee|charge|charges|amount)\b",
                context,
                re.I,
            ):
                continue
            amount = _amount(amount_match.group(1), amount_match.group(2))
            if amount is not None and amount > 0:
                pay_amount = amount
                currency = _currency_from(window) or "INR"
                quote = _snippet_around(text, amount_match.group(0))
                break
    if pay_amount is None:
        # A bare amount near "₹"/Rs is still useful, unless the clause is a
        # fee (entry, submission, registration) or an age range.
        for match in AMOUNT_RE.finditer(text):
            if not _currency_from(match.group(0)):
                continue
            context = _clause(text, match.start(), match.end())
            if FEE_RE.search(context) or re.search(
                r"\b(?:entry|submission|registration|application|audition|participation)\s+(?:fee|charge|charges|amount)\b",
                context,
                re.I,
            ):
                continue
            amount = _amount(match.group(1), match.group(2))
            if amount and amount > 0:
                pay_amount = amount
                currency = _currency_from(match.group(0))
                quote = _snippet_around(text, match.group(0))
                break
    if pay_amount is not None:
        lower = text.lower()
        if re.search(r"\b(?:per\s+day|day\s*rate|daily)\b", lower) or re.search(
            r"(?:प्रति\s*दिन|रोज़|रोज)", lower
        ):
            comp = "day_rate"
        elif re.search(r"\b(?:flat|one[\s-]?time|lump\s*sum|project\s+fee)\b", lower):
            comp = "flat"
        else:
            comp = "flat"
        return comp, pay_amount, currency, quote
    return None, None, None, None


def _extract_fee(text: str) -> tuple[bool, float | None, str | None]:
    match = FEE_RE.search(text)
    if not match:
        # "pay Rs 500 to apply"
        match = re.search(r"\bpay\b[^\n]{0,60}\b(?:to\s+apply|for\s+(?:the\s+)?(?:audition|application|registration))\b", text, re.I)
    if not match:
        return False, None, None
    # The amount normally follows the fee keyword ("Entry fee: Rs 500").
    # Searching a fixed window would catch an unrelated date before it
    # ("Submit by 10 January ... Entry fee").
    after = text[match.end() : min(len(text), match.end() + 90)]
    amount_match = AMOUNT_RE.search(after)
    if not amount_match:
        before = text[max(0, match.start() - 40) : match.start()]
        amount_match = AMOUNT_RE.search(before)
    amount = _amount(amount_match.group(1), amount_match.group(2)) if amount_match else None
    return True, amount, _snippet_around(text, match.group(0))


def _extract_age(text: str) -> tuple[int | None, int | None, str | None]:
    patterns = [
        re.compile(r"\b(?:age|aged|age\s+group|age\s+range)\s*[:\-]?\s*(\d{1,2})\s*(?:-|–|—|to|and)\s*(\d{1,2})\b", re.I),
        re.compile(r"\b(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*(?:years?|yrs?)\b", re.I),
        re.compile(r"\b(?:looking\s+for|need|require|seeking)\b[^\n]{0,60}?\b(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\b", re.I),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            a, b = int(match.group(1)), int(match.group(2))
            if 0 < a <= 90 and 0 < b <= 90 and a <= b:
                return a, b, _snippet_around(text, match.group(0))
    below = re.search(r"\b(?:below|under|max(?:imum)?|up\s+to)\s+(\d{1,2})\s*(?:years?|yrs?)?\b", text, re.I)
    above = re.search(r"\b(?:above|over|min(?:imum)?|at\s+least)\s+(\d{1,2})\s*(?:years?|yrs?)?\b", text, re.I)
    if below:
        value = int(below.group(1))
        if 0 < value <= 90:
            return None, value, _snippet_around(text, below.group(0))
    if above:
        value = int(above.group(1))
        if 0 < value <= 90:
            return value, None, _snippet_around(text, above.group(0))
    return None, None, None


def _extract_contact(text: str, *, url: str | None) -> tuple[str | None, str | None, str | None]:
    emails = extract_emails(text)
    phones = extract_phones(text)
    urls = extract_urls(text)

    whatsapp = re.search(r"\b(?:whats\s?app|wa\.me|wa\.link)\b", text, re.I)
    telegram = re.search(r"\b(?:telegram|t\.me/)\b", text, re.I)
    instagram = re.search(r"\b(?:instagram|insta\b|@[A-Za-z0-9._]{2,30})\b", text, re.I)
    dm = re.search(r"\b(?:dm|direct\s+message)\b", text, re.I)

    if emails:
        email = emails[0]
        return "email", email, _snippet_around(text, email)
    if whatsapp and phones:
        return "whatsapp", phones[0], _snippet_around(text, whatsapp.group(0))
    form_url = next((candidate for candidate in urls if FORM_URL_RE.search(candidate)), None)
    if form_url:
        return "form", form_url, None
    if phones:
        return "phone", phones[0], _snippet_around(text, phones[0])
    if telegram:
        tg_url = next((candidate for candidate in urls if "t.me/" in candidate), None)
        return "telegram", tg_url or "telegram", _snippet_around(text, telegram.group(0))
    if instagram:
        handle = instagram.group(0)
        if handle.startswith("@"):
            return "instagram", handle, _snippet_around(text, handle)
        return "instagram", "instagram", _snippet_around(text, handle)
    if dm:
        return "dm", "DM", _snippet_around(text, dm.group(0))
    if url and FORM_URL_RE.search(url):
        return "form", url, None
    return None, None, None
