"""schema.org JSON-LD, Microdata and OpenGraph extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..normalize import normalize_text
from ..util import get_logger, parse_dt
from .dates import parse_absolute_date

log = get_logger(__name__)


@dataclass
class JsonLdResult:
    fields: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    raw_blobs: list[str] = field(default_factory=list)
    method: str = "jsonld"


def extract_structured(html: str, *, url: str | None = None) -> JsonLdResult:
    result = JsonLdResult()
    if not html:
        return result

    objects: list[dict[str, Any]] = []
    try:
        import extruct

        data = extruct.extract(html, base_url=url or "", syntaxes=["json-ld"], errors="ignore")
        for item in data.get("json-ld", []) or []:
            result.raw_blobs.append(json.dumps(item, ensure_ascii=False, default=str)[:20000])
            objects.extend(_iter_objects(item))
    except Exception as exc:
        log.debug("extruct failed: %s", exc)

    # OpenGraph / meta fallback is cheap and often present.
    meta = _meta(html)
    for key, value in meta.items():
        if value and key not in result.fields:
            result.fields[key] = value
            result.evidence[key] = value

    for obj in objects:
        types = _types(obj)
        if not types:
            continue
        if types & {"jobposting", "job"}:
            _apply_jobposting(obj, result)
        elif types & {"event", "festival"}:
            _apply_event(obj, result)
        elif types & {"movie", "tvseries", "creativework", "videoobject", "shortfilm"}:
            _apply_creativework(obj, result)
        elif types & {"article", "blogposting", "newsarticle"}:
            _apply_article(obj, result)

    # HTML canonical link.
    canonical = _canonical(html)
    if canonical:
        result.fields.setdefault("canonical_url", canonical)
    return result


def _iter_objects(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        if "@graph" in value:
            for item in value.get("@graph") or []:
                yield from _iter_objects(item)
        if "@type" in value or "name" in value:
            yield value
        for child in value.values():
            if isinstance(child, (dict, list)) and child is not value.get("@graph"):
                yield from _iter_objects(child)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_objects(item)


def _types(obj: dict[str, Any]) -> set[str]:
    raw = obj.get("@type") or obj.get("type") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(item).lower().replace(" ", "") for item in raw}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = re.sub(r"<[^>]+>", " ", value)
        value = normalize_text(value, keep_newlines=False)
        return value or None
    return None


def _apply_jobposting(obj: dict[str, Any], result: JsonLdResult) -> None:
    _set(result, "title", _text(obj.get("title")) or _text(obj.get("name")))
    _set(result, "description", _text(obj.get("description")))
    _set(result, "published_at", _date(obj.get("datePosted")))
    _set(result, "deadline", _date(obj.get("validThrough")))
    _set(result, "opportunity_type", "casting" if _is_performer_job(obj) else "crew")
    # Keep the source's original date string as evidence; the normalised ISO
    # value may not appear verbatim in the document.
    if obj.get("validThrough"):
        result.evidence["deadline"] = str(obj.get("validThrough"))[:500]
    if obj.get("datePosted"):
        result.evidence["published_at"] = str(obj.get("datePosted"))[:500]

    location = obj.get("jobLocation") or obj.get("jobLocationType")
    city, country = _location(location)
    if city:
        _set(result, "city", city)
    if country:
        _set(result, "country", country)
    if str(obj.get("jobLocationType", "")).upper() == "TELECOMMUTE":
        _set(result, "remote_ok", True)

    org = obj.get("hiringOrganization")
    if isinstance(org, dict):
        _set(result, "contact_value", _text(org.get("email")) or _text(org.get("url")))
        if _text(org.get("name")):
            result.fields.setdefault("company", _text(org.get("name")))

    salary = obj.get("baseSalary") or obj.get("estimatedSalary")
    _apply_salary(salary, result)


def _apply_event(obj: dict[str, Any], result: JsonLdResult) -> None:
    _set(result, "title", _text(obj.get("name")))
    _set(result, "description", _text(obj.get("description")))
    _set(result, "opportunity_type", "festival")
    _set(result, "published_at", _date(obj.get("datePublished")))
    raw_deadline = obj.get("endDate") or obj.get("startDate")
    _set(result, "deadline", _date(raw_deadline))
    if raw_deadline:
        result.evidence["deadline"] = str(raw_deadline)[:500]
    if obj.get("datePublished"):
        result.evidence["published_at"] = str(obj.get("datePublished"))[:500]
    city, country = _location(obj.get("location"))
    if city:
        _set(result, "city", city)
    if country:
        _set(result, "country", country)
    offers = obj.get("offers")
    if isinstance(offers, dict):
        price = offers.get("price")
        if price is not None:
            try:
                result.fields.setdefault("fee_required", True)
                result.fields.setdefault("fee_amount", float(price))
                result.evidence.setdefault("fee_required", str(price))
            except (TypeError, ValueError):
                pass
    _set(result, "contact_value", _text(obj.get("url")))


def _apply_creativework(obj: dict[str, Any], result: JsonLdResult) -> None:
    _set(result, "title", _text(obj.get("name")) or _text(obj.get("headline")))
    _set(result, "description", _text(obj.get("description")))
    _set(result, "published_at", _date(obj.get("datePublished")))
    genre = obj.get("genre")
    if isinstance(genre, list):
        genre = ", ".join(str(g) for g in genre)
    if genre:
        result.fields.setdefault("genre", _text(genre))


def _apply_article(obj: dict[str, Any], result: JsonLdResult) -> None:
    _set(result, "title", _text(obj.get("headline")) or _text(obj.get("name")))
    _set(result, "description", _text(obj.get("description")))
    _set(result, "published_at", _date(obj.get("datePublished")))


def _apply_salary(salary: Any, result: JsonLdResult) -> None:
    if not isinstance(salary, dict):
        return
    value = salary.get("value")
    amount: float | None = None
    unit = str(salary.get("unitText") or "").lower()
    if isinstance(value, dict):
        # schema.org puts unitText inside the QuantitativeValue, not on salary.
        unit = str(value.get("unitText") or unit).lower()
        for key in ("value", "minValue", "maxValue"):
            try:
                if value.get(key) is not None:
                    amount = float(value[key])
                    break
            except (TypeError, ValueError):
                continue
    elif value is not None:
        try:
            amount = float(value)
        except (TypeError, ValueError):
            amount = None
    # Template-generated JobPosting markup often carries a placeholder amount
    # ("value": 1) with a default USD currency.  Real performer pay is never
    # under 100 in any currency these boards use, so small figures are dropped.
    if amount is not None and amount < 100:
        amount = None
    if amount is not None:
        result.fields.setdefault("pay_amount", amount)
        # Keep the quote compact ("15000", not "15000.0") so it appears
        # verbatim in the JSON-LD blob during span verification.
        result.evidence.setdefault("pay_amount", str(int(amount)) if float(amount).is_integer() else str(amount))
    currency = salary.get("currency")
    if currency:
        result.fields.setdefault("currency", str(currency))
    # Platforms often publish an empty MonetaryAmount with unitText "DAY".
    # Without a figure that says nothing about pay, so it must not create a
    # compensation claim that overrides the page's own "Payment terms".
    if amount is not None:
        if "day" in unit:
            result.fields.setdefault("comp_type", "day_rate")
        else:
            result.fields.setdefault("comp_type", "flat")


def _location(location: Any) -> tuple[str | None, str | None]:
    if isinstance(location, list):
        for item in location:
            city, country = _location(item)
            if city or country:
                return city, country
        return None, None
    if isinstance(location, str):
        return (location.strip(), None) if location.strip() else (None, None)
    if not isinstance(location, dict):
        return None, None
    address = location.get("address") or location
    if isinstance(address, str):
        return address.strip() or None, None
    if isinstance(address, dict):
        city = address.get("addressLocality") or address.get("addressRegion") or address.get("name")
        country = address.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        return (_text(city), _text(country))
    name = location.get("name")
    return _text(name), None


def _is_performer_job(obj: dict[str, Any]) -> bool:
    text = " ".join(str(obj.get(k, "")) for k in ("title", "description", "occupationalCategory", "industry"))
    return bool(re.search(r"\b(?:actor|actress|casting|audition|performer|artist|extra|voice)\b", text, re.I))


def _date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, str):
        parsed = parse_dt(value)
        if parsed:
            return parsed.isoformat(timespec="seconds")
        parsed = parse_absolute_date(value)
        if parsed:
            return parsed.isoformat(timespec="seconds")
    return None


def _set(result: JsonLdResult, field: str, value: Any) -> None:
    if value is None or value == "":
        return
    if field in result.fields and result.fields[field]:
        return
    result.fields[field] = value
    if isinstance(value, (str, int, float, bool)):
        result.evidence[field] = str(value)[:500]


def _meta(html: str) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        def meta(prop: str) -> str | None:
            node = tree.css_first(f'meta[property="{prop}"], meta[name="{prop}"]')
            if node:
                return _text(node.attributes.get("content"))
            return None

        fields["title"] = meta("og:title") or meta("twitter:title")
        fields["description"] = meta("og:description") or meta("description") or meta("twitter:description")
        published = meta("article:published_time") or meta("datePublished")
        if published:
            fields["published_at"] = _date(published)
        if meta("og:url"):
            fields["canonical_url"] = meta("og:url")
        # <title> as a last resort.
        if not fields.get("title"):
            title_node = tree.css_first("title")
            if title_node:
                fields["title"] = _text(title_node.text())
    except Exception:
        pass
    return {key: value for key, value in fields.items() if value}


def _canonical(html: str) -> str | None:
    try:
        from ..htmlutil import parse_html

        tree = parse_html(html)
        node = tree.css_first('link[rel="canonical"]')
        if node:
            return node.attributes.get("href")
    except Exception:
        pass
    return None
