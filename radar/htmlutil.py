"""HTML parsing backend selection.

``selectolax`` 1.x refuses to import the legacy ``selectolax.parser`` (modest)
backend.  The lexbor backend has the same API for the handful of methods this
project uses, so prefer it and fall back only for older selectolax releases.
"""

from __future__ import annotations

from typing import Any


def parse_html(html: str) -> Any:
    """Return a selectolax tree for *html* using whichever backend is usable."""
    try:
        from selectolax.lexbor import LexborHTMLParser

        return LexborHTMLParser(html or "")
    except Exception:  # pragma: no cover - depends on the installed version
        from selectolax.parser import HTMLParser

        return HTMLParser(html or "")
