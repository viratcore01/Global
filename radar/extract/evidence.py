"""Span grounding: a quoted field is only kept if the quote exists in the source."""

from __future__ import annotations

from typing import Iterable

from ..models import OpportunityRecord
from ..normalize import span_in_text
from ..util import get_logger, normalize_ws

log = get_logger(__name__)

# Fields where a wrong value is worse than an unknown value.
STRICT_FIELDS = {
    "deadline",
    "city",
    "country",
    "comp_type",
    "pay_amount",
    "fee_required",
    "age_min",
    "age_max",
    "gender_presentation",
    "nudity",
    "audition_format",
    "shoot_window",
    "travel_covered",
    "usage_rights",
    "contact_value",
    "remote_ok",
}

DEFAULTS: dict[str, object] = {
    "deadline": None,
    "deadline_confidence": "unknown",
    "city": None,
    "country": None,
    "comp_type": "unknown",
    "pay_amount": None,
    "currency": None,
    "fee_required": None,
    "age_min": None,
    "age_max": None,
    "gender_presentation": "any",
    "nudity": "unknown",
    "audition_format": "unknown",
    "shoot_window": [],
    "travel_covered": None,
    "usage_rights": "unknown",
    "contact_value": None,
    "remote_ok": None,
}


def verify_evidence(
    record: OpportunityRecord,
    texts: Iterable[str],
) -> tuple[OpportunityRecord, list[str]]:
    """Null any field whose stored quote is not verbatim in the source.

    Inputs are normalised with whitespace collapse, so line wrapping and
    multiple spaces do not cause false negatives.
    """
    haystack = "\n".join(text or "" for text in texts)
    errors: list[str] = []
    if not haystack.strip():
        return record, ["no source text available for span verification"]

    data = record.model_dump(mode="python")
    evidence = {
        key: (value.model_dump() if hasattr(value, "model_dump") else dict(value))
        for key, value in record.evidence.items()
    }
    for field, item in list(evidence.items()):
        quote = item.get("text") if isinstance(item, dict) else str(item)
        if not quote:
            evidence.pop(field, None)
            continue
        if not span_in_text(quote, haystack):
            errors.append(f"evidence for {field!r} not found in source; field nulled")
            evidence.pop(field, None)
            if field in STRICT_FIELDS:
                data[field] = DEFAULTS.get(field)
                if field == "deadline":
                    data["deadline_confidence"] = "unknown"
            elif field == "title":
                # Keep the title only if it is a plausible short heading.
                title = normalize_ws(data.get("title") or "")
                if len(title) > 400:
                    data["title"] = "Untitled opportunity"
            elif field == "description":
                pass

    # If a strict field has no evidence at all but a value was inferred from
    # free text, that is acceptable only for derived fields. For fields that
    # are pure guesses, null them.
    for field in ("deadline", "city", "contact_value"):
        if data.get(field) and field not in evidence:
            # Deadline/city/contact must be quoted; leave them to the caller's
            # explicit heuristics only if the pipeline verified a span.
            if field == "deadline":
                data["deadline"] = None
                data["deadline_confidence"] = "unknown"
            elif field == "city":
                data["city"] = None
                data["country"] = None
            elif field == "contact_value":
                data["contact_value"] = None
                data["contact_channel"] = "unknown"

    cleaned = OpportunityRecord.model_validate({**data, "evidence": evidence})
    return cleaned, errors


def evidence_coverage(record: OpportunityRecord, *, keys: Iterable[str] | None = None) -> float:
    keys = keys or (
        "title",
        "deadline",
        "city",
        "comp_type",
        "contact_value",
        "age_min",
        "age_max",
        "audition_format",
        "shoot_window",
        "languages",
        "role_types",
    )
    present = 0
    for key in keys:
        value = getattr(record, key, None)
        if key in ("age_min", "age_max"):
            if record.age_min is not None or record.age_max is not None:
                present += 1
            continue
        if key == "comp_type":
            if record.comp_type != "unknown":
                present += 1
            continue
        if value not in (None, "", [], {}, "unknown"):
            present += 1
    return present / max(1, len(list(keys)))


def missing_evidence(record: OpportunityRecord) -> list[str]:
    return [
        field
        for field in STRICT_FIELDS
        if getattr(record, field, None) not in (None, "", [], {}, "unknown", "any")
        and field not in record.evidence
    ]
