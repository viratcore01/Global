"""Extraction orchestration: structured data -> CSS -> main text -> OCR -> LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping
from urllib.parse import urljoin, urlsplit

from pydantic import ValidationError

from ..config import Settings
from ..models import STATUSES, Evidence, OpportunityRecord
from ..normalize import canonicalize_url, extract_emails, normalize_text, normalize_ws, sha256_text
from ..util import get_logger, now_utc, parse_dt, truncate
from .board import BoardItem, detect_board_items
from .css import extract_css
from .dates import parse_absolute_date
from .evidence import verify_evidence
from .heuristics import HeuristicResult, extract_heuristics
from .jsonld import extract_structured
from .llm import LLMClient
from .maintext import extract_main_text, html_to_text, title_from_html
from .ocr import ocr_images

log = get_logger(__name__)

FIELD_LIST = [
    "deadline",
    "city",
    "country",
    "comp_type",
    "pay_amount",
    "currency",
    "fee_required",
    "age_min",
    "age_max",
    "gender_presentation",
    "nudity",
    "audition_format",
    "contact_channel",
    "contact_value",
    "language",
    "role_type",
    "opportunity_type",
    "remote_ok",
    "travel_covered",
    "usage_rights",
]


@dataclass
class ExtractionResult:
    records: list[OpportunityRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    method: str = "heuristic"
    skipped: bool = False
    reason: str = ""
    text: str = ""
    html: str = ""
    jsonld_blobs: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    notes: dict[str, Any] = field(default_factory=dict)


class ExtractionEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        image_fetcher: Callable[[str], bytes | None] | None = None,
        llm: LLMClient | None = None,
    ):
        self.settings = settings
        self.image_fetcher = image_fetcher
        self.llm = llm or LLMClient(settings)

    def extract(
        self,
        raw: Mapping[str, Any],
        source: Mapping[str, Any] | Any,
        *,
        allow_llm: bool = False,
    ) -> ExtractionResult:
        url = str(raw.get("canonical_url") or raw.get("url") or "")
        body_text = str(raw.get("body_text") or "")
        content_type = str(raw.get("content_type") or "")
        meta = raw.get("meta") or {}
        kind = str(raw.get("kind") or "page")
        is_html = "html" in content_type.lower() or _looks_like_html(body_text)

        result = ExtractionResult(text=body_text, html=body_text if is_html else "")
        title_hint = _title_hint(meta, body_text, is_html)
        published_at = parse_dt(meta.get("published_at")) or parse_dt(meta.get("date")) or now_utc()
        result.published_at = published_at

        structured = extract_structured(body_text, url=url) if is_html else None
        if structured:
            result.jsonld_blobs = structured.raw_blobs
            result.method = "jsonld" if structured.fields else "heuristic"

        css_result = None
        detail_page = bool(meta.get("detail"))
        extractor = str(_get(source, "extractor", "auto") or "auto")
        extractor_config = _get(source, "extractor_config", {}) or {}
        listing_mode = (
            bool(extractor_config.get("item")) if isinstance(extractor_config, dict) else False
        ) and not detail_page
        if extractor == "css" and extractor_config and is_html and not detail_page:
            css_result = extract_css(body_text, extractor_config, base_url=url)
            if css_result.items:
                result.method = "css"

        main_text = ""
        if is_html:
            main = extract_main_text(body_text, url=url)
            main_text = main.text
            if not title_hint and main.title:
                title_hint = main.title
        text = main_text or body_text
        if kind in {"email", "telegram", "rss", "discord"} and body_text:
            # Email/Telegram bodies are already the message; append extracted
            # main text only when it adds something.
            if main_text and main_text not in body_text:
                text = body_text + "\n\n" + main_text
            else:
                text = body_text
        result.text = normalize_text(text)

        # A page with several repeated casting cards is a board, not one
        # opportunity.  Detect that structure before the single-page gate so a
        # board can still yield records when its main text is mostly chrome.
        if is_html and not detail_page and not (css_result and css_result.items):
            board_items = detect_board_items(body_text, base_url=url)
            if board_items:
                board_records = self._extract_board(
                    board_items,
                    raw=raw,
                    source=source,
                    published_at=published_at,
                    page_text=result.text,
                )
                if board_records:
                    result.records = board_records
                    result.method = "board"
                    return result

        heuristics = extract_heuristics(
            result.text,
            title_hint=title_hint,
            url=url,
            source_type=str(_get(source, "type", "web")),
            base=published_at,
            assume_opportunity=bool(_get(source, "assume_opportunity", False)),
        )
        if not heuristics.is_opportunity:
            result.skipped = True
            result.reason = "no opportunity signals found"
            return result

        merged_fields = dict(heuristics.fields)
        merged_evidence = dict(heuristics.evidence)

        # Structured data is authoritative for facts it explicitly states.
        if structured:
            _merge_structured(merged_fields, merged_evidence, structured)
        # CSS selectors are the most specific source; they win where present.
        if css_result and css_result.items:
            _merge_css(merged_fields, merged_evidence, css_result, index=0)

        # OCR only when the page is image-heavy or the text is thin.
        ocr_text = ""
        if is_html and self.settings.enable_ocr and (len(result.text) < 500 or _poster_likely(heuristics)):
            ocr = ocr_images(
                body_text,
                base_url=url,
                settings=self.settings,
                fetch_bytes=self.image_fetcher,
            )
            if ocr.text:
                ocr_text = ocr.text
                ocr_heur = extract_heuristics(
                    ocr_text,
                    title_hint=title_hint,
                    url=url,
                    source_type=str(_get(source, "type", "web")),
                    base=published_at,
                    assume_opportunity=True,
                )
                _fill_missing(merged_fields, merged_evidence, ocr_heur)
                result.notes["ocr"] = {"method": ocr.method, "images": ocr.images_used}

        # LLM is last, gaps only, with a strict budget at the call site.
        llm_model = None
        if allow_llm and self.llm.enabled:
            known = {key: value for key, value in merged_fields.items() if value not in (None, "", [], {}, "unknown")}
            missing = [field for field in FIELD_LIST if field not in known or known.get(field) in (None, "", "unknown")]
            # Do not waste an LLM call on a page with almost no text.
            if len(result.text) >= 120 and missing:
                llm_result = self.llm.fill_gaps(result.text, known=known, missing=missing)
                if llm_result.fields:
                    for key, value in llm_result.fields.items():
                        if key in {"language", "role_type"}:
                            target = "languages" if key == "language" else "role_types"
                            merged_fields[target] = value
                        else:
                            merged_fields[key] = value
                        if key in llm_result.evidence:
                            merged_evidence[key] = llm_result.evidence[key]
                    llm_model = llm_result.model
                    result.notes["llm"] = {"model": llm_result.model, "used": True}
                elif llm_result.error:
                    result.notes["llm"] = {"error": llm_result.error}

        # Build one record per CSS item when the extractor is in listing mode.
        items = css_result.items if css_result and css_result.items else [None]
        for index, css_item in enumerate(items):
            fields = dict(merged_fields)
            evidence = dict(merged_evidence)
            item_url = url
            item_text = ""
            if css_item:
                # Re-run the deterministic extractors on the card's own text.
                # A board page holds many posts; using the page-wide deadline
                # or pay for every card is how junk facts get attached to the
                # wrong opportunity.  Single-item CSS pages keep the merged
                # page-level facts (JSON-LD etc.).
                item_text = _item_text(css_item) if listing_mode else ""
                if item_text:
                    item_heur = extract_heuristics(
                        item_text,
                        title_hint=str(css_item.get("title") or "") or None,
                        url=url,
                        source_type=str(_get(source, "type", "web")),
                        base=published_at,
                        assume_opportunity=True,
                    )
                    fields = dict(item_heur.fields)
                    evidence = dict(item_heur.evidence)
                fields = _apply_css_item_to_fields(fields, css_item)
                for key, value in css_item.items():
                    if key.startswith("_") or not isinstance(value, str):
                        continue
                    target = {"contact": "contact_value", "location": "city", "pay": "pay_amount"}.get(key, key)
                    evidence[target] = value[:500]
                if css_item.get("_closed"):
                    # The board itself marks this card as ended/expired; keep the
                    # record for history but never present it as a live lead.
                    fields["status"] = "expired"
                if css_item.get("_url"):
                    item_url = canonicalize_url(str(css_item["_url"]), url) or url
            if index > 0 and item_url == url:
                # Avoid a UNIQUE canonical_hash collision for multi-item pages.
                item_url = f"{url}?item={index + 1}"
            try:
                record = self._build_record(
                    fields=fields,
                    evidence=evidence,
                    url=item_url,
                    raw=raw,
                    source=source,
                    published_at=published_at,
                    text=result.text,
                    title_hint=title_hint,
                    verification_texts=[
                        result.text,
                        body_text,
                        title_hint or "",
                        item_text,
                        *result.jsonld_blobs,
                        ocr_text,
                    ],
                    llm_model=llm_model,
                    method="css" if css_item else result.method,
                )
            except ValidationError as exc:
                result.errors.append(f"validation failed: {exc.errors()[:3]}")
                continue
            result.records.append(record)

        if not result.records and not result.errors:
            result.skipped = True
            result.reason = "record failed validation"
        return result

    def _extract_board(
        self,
        items: list[BoardItem],
        *,
        raw: Mapping[str, Any],
        source: Mapping[str, Any] | Any,
        published_at: datetime,
        page_text: str,
    ) -> list[OpportunityRecord]:
        """Build one record per detected card, using the card's own copy."""
        records: list[OpportunityRecord] = []
        for item in items:
            heuristics = extract_heuristics(
                item.text,
                title_hint=item.title,
                url=item.url,
                source_type=str(_get(source, "type", "web")),
                base=published_at,
            )
            if not heuristics.is_opportunity:
                continue
            fields = dict(heuristics.fields)
            if item.title:
                fields["title"] = item.title
            if item.city:
                fields.setdefault("city", item.city)
            if item.closed:
                fields["status"] = "expired"
            try:
                record = self._build_record(
                    fields=fields,
                    evidence=dict(heuristics.evidence),
                    url=item.url,
                    raw=raw,
                    source=source,
                    published_at=published_at,
                    text=item.text,
                    title_hint=item.title,
                    verification_texts=[item.text, page_text],
                    llm_model=None,
                    method="board",
                )
            except ValidationError as exc:
                log.debug("Board card rejected for %s: %s", item.url, exc.errors()[:2])
                continue
            records.append(record)
        return records

    def _build_record(
        self,
        *,
        fields: dict[str, Any],
        evidence: dict[str, str],
        url: str,
        raw: Mapping[str, Any],
        source: Mapping[str, Any] | Any,
        published_at: datetime,
        text: str,
        title_hint: str | None,
        verification_texts: list[str],
        llm_model: str | None,
        method: str,
    ) -> OpportunityRecord:
        title = normalize_ws(str(fields.get("title") or title_hint or _fallback_title(text) or ""))
        if not title:
            title = "Untitled opportunity"
        description = str(fields.get("description") or text or "")
        deadline = fields.get("deadline")
        if isinstance(deadline, datetime):
            deadline = deadline.isoformat(timespec="seconds")
        if deadline and not isinstance(deadline, str):
            parsed = parse_absolute_date(str(deadline))
            deadline = parsed.isoformat(timespec="seconds") if parsed else None
        if deadline:
            parsed_deadline = parse_dt(deadline)
            deadline = parsed_deadline.isoformat(timespec="seconds") if parsed_deadline else None

        evidence_models = {
            key: Evidence(text=value[:2000], method=method) for key, value in evidence.items() if value
        }
        structured_key = _structured_key(
            title=title,
            city=fields.get("city"),
            deadline=deadline,
            contact=fields.get("contact_value"),
        )
        record_data: dict[str, Any] = {
            "canonical_url": url,
            "source_id": raw.get("source_id"),
            "source_type": str(_get(source, "type", "web")),
            "opportunity_type": fields.get("opportunity_type") or "other",
            "title": title,
            "description": truncate(description, 12000),
            "published_at": published_at.isoformat(timespec="seconds"),
            "first_seen_at": now_utc().isoformat(timespec="seconds"),
            "last_seen_at": now_utc().isoformat(timespec="seconds"),
            "deadline": deadline,
            "deadline_confidence": fields.get("deadline_confidence") or ("high" if deadline else "unknown"),
            "city": _clean_value(fields.get("city")),
            "country": _clean_value(fields.get("country")),
            "remote_ok": _bool_or_none(fields.get("remote_ok")),
            "languages": fields.get("languages") or [],
            "role_types": fields.get("role_types") or [],
            "comp_type": fields.get("comp_type") or "unknown",
            "pay_amount": _float_or_none(fields.get("pay_amount")),
            "currency": _clean_value(fields.get("currency")),
            "fee_required": _bool_or_none(fields.get("fee_required")),
            "usage_rights": fields.get("usage_rights") or "unknown",
            "nudity": fields.get("nudity") or "unknown",
            "travel_covered": _bool_or_none(fields.get("travel_covered")),
            "audition_format": fields.get("audition_format") or "unknown",
            "shoot_window": fields.get("shoot_window") or [],
            "age_min": _int_or_none(fields.get("age_min")),
            "age_max": _int_or_none(fields.get("age_max")),
            "gender_presentation": fields.get("gender_presentation") or "any",
            "contact_channel": fields.get("contact_channel") or "unknown",
            "contact_value": _clean_value(fields.get("contact_value")),
            "evidence": evidence_models,
            "trust_tier": int(_get(source, "tier", 3) or 3),
            "raw_document_id": raw.get("id"),
            "raw_text_hash": sha256_text(normalize_ws(text)),
            "structured_key": structured_key,
            "status": fields.get("status") if fields.get("status") in STATUSES else "new",
            "extraction_method": method,
            "llm_model": llm_model,
        }
        record = OpportunityRecord.model_validate(record_data)
        verified, errors = verify_evidence(record, verification_texts)
        if errors:
            log.debug("Evidence verification notes for %s: %s", url, errors)
        if not verified.description:
            verified.description = truncate(text, 12000)
        return verified


# ---------------------------------------------------------------------------
# Merge helpers
# ---------------------------------------------------------------------------


def _item_text(item: dict[str, Any]) -> str:
    """Flatten a CSS listing item into plain text for per-card heuristics."""
    parts: list[str] = []
    for key, value in item.items():
        if key.startswith("_") or not isinstance(value, str) or not value.strip():
            continue
        if value not in parts:
            parts.append(value)
    return "\n".join(parts)


def _merge_structured(fields: dict[str, Any], evidence: dict[str, str], structured: Any) -> None:
    # Salary markup is the least reliable part of these templates, so it only
    # fills gaps; the page's own "Payment terms" / "Remuneration paid" wins.
    fill_only = {"pay_amount", "currency", "comp_type"}
    for key, value in structured.fields.items():
        if value in (None, "", [], {}):
            continue
        if key in {"genre", "company"}:
            continue
        current = fields.get(key)
        if key in fill_only and current not in (None, "", [], {}, "unknown"):
            continue
        if current in (None, "", [], {}, "unknown", "other") or key in {
            "title",
            "deadline",
            "city",
            "country",
            "fee_required",
            "remote_ok",
            "usage_rights",
        }:
            fields[key] = value
            if key in structured.evidence:
                evidence[key] = structured.evidence[key]
    # Structured descriptions frequently embed the contact the page hides.
    if not fields.get("contact_value"):
        description = str(structured.fields.get("description") or fields.get("description") or "")
        if description:
            emails = extract_emails(description)
            if emails:
                fields["contact_value"] = emails[0]
                fields["contact_channel"] = "email"
                evidence["contact_value"] = emails[0]
    if str(fields.get("comp_type") or "") in {"unpaid", "credit_only", "deferred"}:
        fields.pop("pay_amount", None)
    if fields.get("pay_amount") in (None, "", 0):
        # A currency with no figure (often a template default) is noise.
        fields.pop("currency", None)


def _merge_css(fields: dict[str, Any], evidence: dict[str, str], css_result: Any, index: int = 0) -> None:
    if not css_result.items:
        return
    item = css_result.items[index]
    for key, value in item.items():
        if key.startswith("_") or value in (None, "", []):
            continue
        target = {"contact": "contact_value", "location": "city", "pay": "pay_amount"}.get(key, key)
        fields[target] = _coerce_css_value(target, value)
        if isinstance(value, str):
            evidence[target] = value[:500]


def _apply_css_item_to_fields(fields: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    updated = dict(fields)
    for key, value in item.items():
        if key.startswith("_") or value in (None, "", []):
            continue
        target = {"contact": "contact_value", "location": "city", "pay": "pay_amount"}.get(key, key)
        updated[target] = _coerce_css_value(target, value)
    return updated


def _fill_missing(fields: dict[str, Any], evidence: dict[str, str], heur: HeuristicResult) -> None:
    for key, value in heur.fields.items():
        if value in (None, "", [], {}) or key in {"description", "title"}:
            continue
        if fields.get(key) in (None, "", [], {}, "unknown", "other"):
            fields[key] = value
            if key in heur.evidence:
                evidence[key] = heur.evidence[key]


def _coerce_css_value(field: str, value: Any) -> Any:
    if field in {"fee_required", "remote_ok", "travel_covered"}:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"true", "yes", "1", "paid", "required"}
    if field in {"pay_amount"}:
        return _float_or_none(value)
    if field in {"age_min", "age_max"}:
        return _int_or_none(value)
    if field in {"languages", "role_types"}:
        if isinstance(value, str):
            return [part.strip() for part in re.split(r"[,;/]", value) if part.strip()]
        return value
    return value


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "paid", "required"}


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(str(value).replace(",", "").replace("₹", "").replace("Rs", "").strip())
        return number if number >= 0 else None
    except ValueError:
        return None


def _int_or_none(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(float(str(value)))
        return number if 0 <= number <= 120 else None
    except ValueError:
        return None


def _clean_value(value: Any) -> str | None:
    if value is None:
        return None
    value = normalize_ws(str(value))
    return value[:200] or None


def _get(source: Mapping[str, Any] | Any, key: str, default: Any) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _title_hint(meta: Mapping[str, Any], body_text: str, is_html: bool) -> str | None:
    for key in ("title", "subject", "og_title"):
        value = meta.get(key)
        if value:
            return normalize_ws(str(value))[:400]
    if is_html:
        title = title_from_html(body_text)
        if title:
            return title
    for line in body_text.splitlines()[:5]:
        line = normalize_ws(line)
        if 5 <= len(line) <= 200:
            return line
    return None


def _fallback_title(text: str) -> str | None:
    for line in (text or "").splitlines():
        line = normalize_ws(line)
        if 5 <= len(line) <= 200:
            return line
    return None


def _looks_like_html(text: str) -> bool:
    return bool(re.search(r"<(?:html|body|div|p|h1|article|head)\b", text or "", re.I))


def _poster_likely(heur: HeuristicResult) -> bool:
    return bool(heur.signals.get("looks_like_casting")) and not heur.fields.get("deadline")


def _structured_key(*, title: str, city: Any, deadline: Any, contact: Any) -> str:
    title_key = re.sub(r"[^a-z0-9\u0900-\u097F ]+", " ", (title or "").lower())
    title_key = " ".join(title_key.split()[:10])
    city_key = normalize_ws(str(city or "")).lower()
    deadline_key = ""
    if deadline:
        parsed = parse_dt(deadline)
        deadline_key = parsed.date().isoformat() if parsed else str(deadline)[:10]
    contact_key = normalize_ws(str(contact or "")).lower()
    raw = f"{title_key}|{city_key}|{deadline_key}|{contact_key}"
    return sha256_text(raw)[:32]
