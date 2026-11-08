"""Automatic board detection: split a listing page into individual calls.

Most casting sites publish a board of cards that all share the same markup.
Hand-writing CSS selectors for every site does not scale, so this module finds
the repeated card container itself, keeps only groups whose cards look like
casting calls, and returns one :class:`BoardItem` per card.

The detector is deliberately conservative: it needs several distinct cards
with casting/crew/festival language, otherwise the page is treated as a normal
single page.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin

import re

from ..cities import find_city
from ..normalize import canonicalize_url, domain_from_url
from ..util import normalize_ws
from .badges import closed_from_text, node_closed

# Card links usually carry one of these words in the path.
OPPORTUNITY_URL_RE = re.compile(
    r"(?:casting|audition|open[-_]?call|talent|role|jobs?|apply|actor|actress|crew|artist|project)",
    re.IGNORECASE,
)

GENERIC_TITLES = {
    "read more",
    "read more...",
    "view",
    "view details",
    "details",
    "apply",
    "apply now",
    "more",
    "see more",
    "click here",
    "open",
    "next",
    "previous",
}

SKIP_TAGS = {"html", "body", "main", "article", "section", "nav", "header", "footer"}

# Festival and crew calls use different wording from castings, so this is the
# strict casting pattern plus those phrasings.  Bare "casting"/"audition" is
# deliberately absent: nav links say that all the time.
CALL_RE = re.compile(
    r"\b(?:"
    r"casting\s+call|cast\s+call|open\s+call|audition\s+call"
    r"|call\s+for\s+(?:entries|submissions|applicants|actors|actresses|artists|crew)"
    r"|now\s+casting|we\s+are\s+casting|currently\s+casting|casting\s+(?:for|an?\s+actor|actress|artists?)"
    r"|looking\s+for\s+(?:an?\s+)?(?:actor|actress|artist|performer|model|dancer|singer|crew|dop|voice|talent|extra)(?:e?s)?"
    r"|looking\s+for\s+(?:\w+[,\s&]{1,12}){0,3}(?:actor|actress|model|dancer|artist|performer|talent)(?:e?s)?"
    r"|(?:actor|actress|artist|performer|crew|dop|dancer|model|singer|voice\s+artist|extra)(?:e?s)?\s+(?:required|needed|wanted)"
    r"|self[\s-]?tape|audition\s+(?:on|at|in|for|by|date)|casting\s+director"
    r"|submission\s+deadline|festival\s+submission|filmfreeway|festhome|entry\s+fee"
    r"|crew\s+(?:call|required|needed)"
    r"|\u0930\u094b\u0932\s+\u0915\u0947\s+\u0932\u093f\u090f|\u0911\u0921\u093f\u0936\u0928|\u0915\u093e\u0938\u094d\u091f\u093f\u0902\u0917"
    r")\b",
    re.IGNORECASE,
)


@dataclass
class BoardItem:
    url: str
    title: str
    text: str
    city: str | None = None
    closed: bool = False


def _classes(node: object) -> str:
    attributes = getattr(node, "attributes", {}) or {}
    return str(attributes.get("class") or "").strip()


def _candidate_ancestors(anchor: object, *, max_levels: int = 7) -> list[tuple[str, object]]:
    """Every classed ancestor that could be the card container.

    A title div can be long enough to look like a card on one post and too
    short on another, so each anchor contributes all its candidates and the
    signature is chosen later by how many actionable cards it produces.
    """
    node = getattr(anchor, "parent", None)
    candidates: list[tuple[str, object]] = []
    for _ in range(max_levels):
        if node is None:
            break
        tag = str(getattr(node, "tag", "") or "").lower()
        if tag in SKIP_TAGS:
            break
        classes = _classes(node)
        if classes:
            text = normalize_ws(node.text(separator=" ", strip=True))  # type: ignore[attr-defined]
            if 40 <= len(text) <= 1600:
                candidates.append((f"{tag}.{'.'.join(sorted(classes.split()))}", node))
        node = getattr(node, "parent", None)
    return candidates


def _item_text(node: object) -> str:
    """Card text, widened by one small parent (location is often a sibling)."""
    text = normalize_ws(node.text(separator=" ", strip=True))  # type: ignore[attr-defined]
    parent = getattr(node, "parent", None)
    if parent is not None and len(text) <= 900:
        parent_text = normalize_ws(parent.text(separator=" ", strip=True))  # type: ignore[attr-defined]
        if len(text) <= len(parent_text) <= 1200:
            text = parent_text
    return text


def _clean_title(anchor: object, container: object) -> str:
    text = normalize_ws(anchor.text(separator=" ", strip=True))  # type: ignore[attr-defined]
    if text and text.lower() not in GENERIC_TITLES and len(text) >= 3:
        return text[:300]
    try:
        headings = container.css("h1, h2, h3, h4")  # type: ignore[attr-defined]
    except Exception:
        headings = []
    for heading in headings or []:
        candidate = normalize_ws(heading.text(separator=" ", strip=True))
        if candidate and len(candidate) >= 3:
            return candidate[:300]
    alt = str((getattr(anchor, "attributes", {}) or {}).get("title") or "")
    if alt:
        return normalize_ws(alt)[:300]
    return ""


def _is_actionable(text: str) -> bool:
    return bool(CALL_RE.search(text or ""))


def detect_board_items(
    html: str,
    base_url: str | None = None,
    *,
    min_items: int = 3,
    max_items: int = 60,
) -> list[BoardItem]:
    """Return one item per card when *html* is a casting board, else ``[]``."""
    if not html or len(html) < 500:
        return []
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
    except Exception:
        return []

    base_domain = domain_from_url(base_url) if base_url else ""
    try:
        page_text_len = len(normalize_ws(tree.body.text(separator=" ", strip=True)))
    except Exception:
        page_text_len = 0
    # A wrapper or nav container repeats the whole page; real cards are always
    # a small slice of it.
    wrapper_limit = max(1500, int(page_text_len * 0.5)) if page_text_len else 100_000
    groups: dict[str, dict[str, BoardItem]] = {}
    closed_cache: dict[int, bool] = {}

    def _is_closed(node: object, text: str) -> bool:
        key = id(node)
        if key not in closed_cache:
            closed_cache[key] = node_closed(node)
        return closed_cache[key] or closed_from_text(text)

    for anchor in tree.css("a[href]")[:800]:
        href = (getattr(anchor, "attributes", {}) or {}).get("href")
        if not href:
            continue
        url = canonicalize_url(urljoin(base_url or "", href), base=base_url)
        if not url:
            continue
        if base_domain:
            host = domain_from_url(url)
            if host and host != base_domain:
                continue
        for signature, node in _candidate_ancestors(anchor):
            text = _item_text(node)
            if not text or len(text) > wrapper_limit:
                continue
            title = _clean_title(anchor, node)
            if not title:
                continue
            city_hit = find_city(text)
            bucket = groups.setdefault(signature, {})
            bucket.setdefault(
                url,
                BoardItem(
                    url=url,
                    title=title,
                    text=text,
                    city=city_hit.city.title() if city_hit else None,
                    closed=_is_closed(node, text),
                ),
            )

    best: list[BoardItem] = []
    for entries in groups.values():
        if len(entries) < min_items:
            continue
        actionable = [item for item in entries.values() if _is_actionable(item.text)]
        if len(actionable) < min_items:
            continue
        # Prefer the signature that yields the most real calls; a nav list can
        # be larger but never wins when its cards carry no casting language.
        if len(actionable) > len(best):
            best = actionable

    return best[:max_items]
