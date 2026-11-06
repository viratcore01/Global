"""Config-driven, source-specific CSS extraction with selectolax.

Example extractor_config:

    fields:
      title: {selector: "h1"}
      deadline: {selector: ".deadline", regex: "([0-9]{1,2} [A-Za-z]+ [0-9]{4})"}
      apply_url: {selector: "a.apply", attr: "href"}
    item: "article.casting-card"     # optional listing mode
    link: {selector: "a", attr: "href"}  # optional per-item detail link
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..util import get_logger, normalize_ws

log = get_logger(__name__)


@dataclass
class CssExtractResult:
    items: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    method: str = "css"


def extract_css(html: str, config: dict[str, Any], *, base_url: str | None = None) -> CssExtractResult:
    result = CssExtractResult()
    if not html or not config:
        return result
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
    except Exception as exc:
        log.debug("selectolax init failed: %s", exc)
        return result

    fields_config = config.get("fields") or {}
    item_selector = config.get("item")
    item_nodes = []
    if item_selector:
        try:
            item_nodes = tree.css(item_selector)
        except Exception as exc:
            log.warning("Invalid item selector %r: %s", item_selector, exc)
            return result

    link_config = config.get("link")
    if item_nodes:
        for node in item_nodes[: int(config.get("max_items", 100) or 100)]:
            item: dict[str, Any] = {}
            evidence: dict[str, str] = {}
            for name, spec in fields_config.items():
                value = _extract_field(node, spec)
                if value not in (None, "", []):
                    item[name] = value
                    if isinstance(value, str):
                        evidence[name] = value[:500]
            if link_config:
                link = _extract_field(node, link_config)
                if link:
                    item["_url"] = link
            if item:
                result.items.append(item)
            for key, value in evidence.items():
                result.evidence.setdefault(key, value)
    else:
        item: dict[str, Any] = {}
        for name, spec in fields_config.items():
            value = _extract_field(tree.root, spec)
            if value not in (None, "", []):
                item[name] = value
                if isinstance(value, str):
                    result.evidence[name] = value[:500]
        if config.get("canonical"):
            value = _extract_field(tree.root, config["canonical"])
            if value:
                item["_url"] = value
        if item:
            result.items.append(item)

    # Normalise common field names from user-friendly aliases.
    for item in result.items:
        if "contact" in item and "contact_value" not in item:
            item["contact_value"] = item.pop("contact")
        if "location" in item and "city" not in item:
            item["city"] = item.pop("location")
        if "pay" in item and "pay_amount" not in item:
            item["pay_amount"] = item.pop("pay")
    return result


def _extract_field(root: Any, spec: Any) -> Any:
    if isinstance(spec, str):
        spec = {"selector": spec}
    if not isinstance(spec, dict):
        return None
    selector = spec.get("selector")
    if not selector:
        return None
    try:
        nodes = root.css(selector)
    except Exception as exc:
        log.debug("Bad selector %r: %s", selector, exc)
        return None
    if not nodes:
        return None
    want_all = bool(spec.get("all"))
    values: list[str] = []
    for node in nodes if want_all else nodes[:1]:
        attr = spec.get("attr") or "text"
        if attr == "text":
            value = node.text(separator=" ", strip=True)
        elif attr == "html":
            value = node.html or ""
        else:
            value = node.attributes.get(attr) or ""
        value = normalize_ws(str(value))
        regex = spec.get("regex")
        if regex:
            match = re.search(regex, value, re.IGNORECASE)
            if not match:
                continue
            try:
                value = match.group(1)
            except IndexError:
                value = match.group(0)
        value = normalize_ws(value)
        if value:
            values.append(value)
    if not values:
        return None
    if want_all:
        return values
    value = values[0]
    if spec.get("int"):
        try:
            return int(re.sub(r"[^\d-]", "", value))
        except ValueError:
            return None
    if spec.get("float"):
        try:
            return float(re.sub(r"[^\d.]", "", value))
        except ValueError:
            return None
    return value
