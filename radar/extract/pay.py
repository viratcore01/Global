"""Compensation understanding: what a call actually pays.

The generic pay parser only fires when a currency amount sits next to a pay
keyword.  Real casting posts are far more qualitative: "TFP", "fee + royalty",
"paid", "deferred", "credit only".  Those phrases decide whether a call is worth
an actor's time, so they are classified here instead of being left as
``unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Explicit no-cash compensation.  "TFP"/"TFCD" are standard trade-for-print /
# trade-for-CD deals: photos instead of money.
CREDIT_ONLY_RE = re.compile(
    r"\b(?:tfp|tfcd|tfpcd|trade\s+for\s+(?:print|prints|portfolio|photos?|cd)"
    r"|time\s+for\s+(?:print|cd)|unpaid|no\s+pay|not\s+paid|without\s+pay"
    r"|credit[\s-]?only|credits[\s-]?only|honorary|gratis|volunteer(?:ing)?)\b",
    re.IGNORECASE,
)

# Money later rather than money now.
DEFERRED_RE = re.compile(
    r"\b(?:deferred|royalt(?:y|ies)(?![-\s]?free)|profit[\s-]?shar(?:e|ing)"
    r"|revenue[\s-]?shar(?:e|ing)|back[\s-]?end(?:\s+(?:deal|share|profit|points))?"
    r"|points\s+on\s+the\s+back[\s-]?end)\b",
    re.IGNORECASE,
)

# A promise of cash, even when no figure is stated.
PAID_PROMISE_RE = re.compile(
    r"\b(?:paid|paying|stipend|honorarium|remuneration|salary|day\s*rate"
    r"|per\s+day|daily\s+wage|flat\s+fee|project\s+fee|professional\s+fee)\b",
    re.IGNORECASE,
)
# A bare "fee" is only a pay promise when it is not an entry/submission fee.
BARE_FEE_RE = re.compile(r"\bfee\b", re.IGNORECASE)
NON_PAY_FEE_RE = re.compile(
    r"\b(?:entry|submission|registration|application|audition|participation|membership"
    r"|platform|processing|service)\s+(?:fee|charge|charges|amount)\b",
    re.IGNORECASE,
)
DAY_RATE_RE = re.compile(r"\b(?:per\s+day|day\s*rate|daily|/day|a\s+day|per\s+shoot\s+day)\b", re.IGNORECASE)


@dataclass
class Compensation:
    comp_type: str  # day_rate | flat | deferred | credit_only | unpaid | unknown
    quote: str = ""
    reason: str = ""

    @property
    def known(self) -> bool:
        return self.comp_type not in {"", "unknown"}


def _snippet(text: str, start: int, end: int, *, width: int = 60) -> str:
    return text[max(0, start - width) : min(len(text), end + width)].strip()


def _clause(text: str, start: int, end: int) -> str:
    """The sentence-ish span around a match, used to disambiguate "fee"."""
    left = max(text.rfind(".", 0, start), text.rfind("\n", 0, start), text.rfind("|", 0, start))
    right_candidates = [text.find(mark, end) for mark in (".", "\n", "|") if text.find(mark, end) != -1]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right]


def classify_compensation(text: str) -> Compensation:
    """Classify qualitative compensation language.

    Order matters: an explicit no-pay marker beats a vague "fee" mention, and a
    stated cash promise beats a revenue-share mention ("fee + royalty" is a paid
    job with a bonus, not deferred work).
    """
    body = text or ""

    match = CREDIT_ONLY_RE.search(body)
    if match:
        phrase = match.group(0).lower()
        # Posts often say both ("unpaid, credit only"); the more specific
        # credit-only promise is the useful label then.
        clause = _clause(body, match.start(), match.end()).lower()
        creditish = "credit" in phrase or phrase in {"tfp", "tfcd", "tfpcd"} or "credit" in clause
        comp = "credit_only" if creditish else "unpaid"
        return Compensation(comp, _snippet(body, match.start(), match.end()), "no-cash phrase")

    paid_match = PAID_PROMISE_RE.search(body)
    fee_match = None
    for candidate in BARE_FEE_RE.finditer(body):
        clause = _clause(body, candidate.start(), candidate.end()).lower()
        if NON_PAY_FEE_RE.search(clause):
            continue
        if re.search(r"\broyalt", clause) and not re.search(r"\b(?:paid|fee\s+plus|fee\s*\+)", clause):
            # "royalty only" is deferred, handled below.
            continue
        fee_match = candidate
        break

    deferred_match = DEFERRED_RE.search(body)
    if paid_match:
        basis = "day_rate" if DAY_RATE_RE.search(_clause(body, paid_match.start(), paid_match.end())) else "flat"
        return Compensation(basis, _snippet(body, paid_match.start(), paid_match.end()), "paid promise")
    if fee_match is not None:
        return Compensation("flat", _snippet(body, fee_match.start(), fee_match.end()), "fee without amount")
    if deferred_match:
        return Compensation("deferred", _snippet(body, deferred_match.start(), deferred_match.end()), "deferred pay")
    return Compensation("unknown")
