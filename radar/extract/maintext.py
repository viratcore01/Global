"""Readable main-text extraction with trafilatura, plus a selectolax fallback."""

from __future__ import annotations

from dataclasses import dataclass

from ..normalize import normalize_text
from ..util import get_logger

log = get_logger(__name__)


@dataclass
class MainText:
    title: str | None
    text: str
    method: str = "trafilatura"


def extract_main_text(html: str, *, url: str | None = None) -> MainText:
    if not html:
        return MainText(None, "", "empty")
    title = title_from_html(html)
    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(
            html,
            url=url,
            include_comments=False,
            include_tables=True,
            favor_recall=True,
            output_format="txt",
            deduplicate=True,
        ) or ""
    except Exception as exc:
        log.debug("trafilatura failed: %s", exc)
    # Trafilatura sometimes drops the closing paragraph (which often holds the
    # contact and "no nudity" statement).  The selector fallback is cheap, so
    # take it when it carries materially more text; never let a short extraction
    # hide the call to action.
    fallback = html_to_text(html)
    method = "trafilatura"
    if len(text.strip()) < 80 or len(fallback.strip()) > len(text.strip()) * 1.1:
        if len(fallback.strip()) > len(text.strip()):
            text = fallback
            method = "selectolax"
    return MainText(title, normalize_text(text), method)


def title_from_html(html: str) -> str | None:
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        for selector in (
            'meta[property="og:title"]',
            'meta[name="twitter:title"]',
            "h1",
            "title",
        ):
            node = tree.css_first(selector)
            if not node:
                continue
            value = node.attributes.get("content") if selector.startswith("meta") else node.text()
            if value:
                value = normalize_text(value, keep_newlines=False)
                if value:
                    return value[:400]
    except Exception:
        pass
    return None


def html_to_text(html: str) -> str:
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        for selector in ("script", "style", "noscript", "template", "svg", "nav", "header", "footer", "form"):
            for node in tree.css(selector):
                node.decompose()
        body = tree.body or tree.root
        if body is None:
            return ""
        text = body.text(separator="\n", strip=True)
        return normalize_text(text)
    except Exception as exc:
        log.debug("selectolax fallback failed: %s", exc)
        return ""


def extract_links(html: str, *, base_url: str | None = None) -> list[str]:
    from ..normalize import canonicalize_url

    links: list[str] = []
    seen: set[str] = set()
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        for node in tree.css("a[href]"):
            href = node.attributes.get("href")
            if not href:
                continue
            url = canonicalize_url(href, base_url)
            if url and url not in seen:
                seen.add(url)
                links.append(url)
    except Exception:
        pass
    return links


def extract_images(html: str, *, base_url: str | None = None) -> list[str]:
    from urllib.parse import urljoin

    images: list[str] = []
    seen: set[str] = set()
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        for node in tree.css("img[src]"):
            src = node.attributes.get("src")
            if not src:
                continue
            url = urljoin(base_url or "", src)
            if url and url not in seen and url.startswith(("http://", "https://")):
                seen.add(url)
                images.append(url)
    except Exception:
        pass
    return images
