"""Detect "this posting has ended" badges on listing cards.

Boards keep filled or expired calls online with a small badge ("Ended",
"Expired", "Closed").  Treating those as live opportunities is exactly the kind
of noise that makes a radar untrustworthy, so the badge is read from the DOM
where possible and from the leading words of the card text otherwise.
"""

from __future__ import annotations

import re
from typing import Any

from ..util import normalize_ws

CLOSED_WORDS = (
    "ended",
    "expired",
    "closed",
    "no longer accepting",
    "applications closed",
    "audition over",
    "casting closed",
    "position filled",
    "role filled",
    "filled",
)
BADGE_CLASS_HINTS = ("badge", "status", "label", "tag", "chip", "state")

LEADING_CLOSED_RE = re.compile(
    r"^\s*(?:ended|expired|closed|no\s+longer\s+accepting)\b",
    re.IGNORECASE,
)


def is_closed_badge(text: str) -> bool:
    """True when a short element's text is a closed/expired badge."""
    value = normalize_ws(text or "").lower().strip(" [](){}·-|")
    return value in CLOSED_WORDS


def closed_from_text(text: str) -> bool:
    """Fallback for sources without DOM access: a card led by "Ended"."""
    return bool(LEADING_CLOSED_RE.match(text or ""))


def node_closed(node: Any, *, max_nodes: int = 12) -> bool:
    """Look for a small badge element inside *node* that marks it closed."""
    try:
        candidates = node.css("span, small, em, strong, label, div")[:max_nodes]
    except Exception:
        return False
    for element in candidates:
        classes = str((getattr(element, "attributes", {}) or {}).get("class") or "").lower()
        if not any(hint in classes for hint in BADGE_CLASS_HINTS):
            continue
        text = normalize_ws(element.text(separator=" ", strip=True))  # type: ignore[attr-defined]
        if len(text) <= 24 and is_closed_badge(text):
            return True
    return False
