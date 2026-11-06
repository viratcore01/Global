"""Span grounding: an unverifiable quote must never keep a guessed value."""

from __future__ import annotations

from radar.extract.evidence import evidence_coverage, missing_evidence, verify_evidence
from radar.models import OpportunityRecord


def make_record(**overrides):
    data = {
        "canonical_url": "https://example.com/job/1",
        "title": "Casting call in Mumbai",
        "description": "Casting a supporting role.",
        "deadline": "2026-12-15T00:00:00+05:30",
        "city": "Mumbai",
        "pay_amount": 15000.0,
        "comp_type": "day_rate",
        "contact_value": "casting@example.com",
        "evidence": {
            "deadline": {"text": "Deadline: 15 December 2026", "method": "rule"},
            "city": {"text": "Location: Mumbai", "method": "rule"},
            "pay_amount": {"text": "Pay: Rs 15,000 per day", "method": "rule"},
            "contact_value": {"text": "casting@example.com", "method": "rule"},
        },
    }
    data.update(overrides)
    return OpportunityRecord.model_validate(data)


def test_verified_fields_survive() -> None:
    text = (
        "Casting call in Mumbai. Location: Mumbai. Pay: Rs 15,000 per day. "
        "Deadline: 15 December 2026. Send to casting@example.com."
    )
    record, errors = verify_evidence(make_record(), [text])
    assert errors == []
    assert record.city == "Mumbai"
    assert record.deadline is not None
    assert record.pay_amount == 15000.0
    assert record.contact_value == "casting@example.com"


def test_unverifiable_quote_nulls_strict_field() -> None:
    text = "Casting call. Location: Mumbai. Send to casting@example.com."
    record, errors = verify_evidence(make_record(), [text])
    assert any("pay_amount" in error for error in errors)
    assert record.pay_amount is None
    assert "pay_amount" not in record.evidence


def test_missing_evidence_for_deadline_and_city_nulls_them() -> None:
    text = "A casting call for a supporting role in a Mumbai web series."
    record, _ = verify_evidence(
        make_record(evidence={"contact_value": {"text": "casting@example.com"}}),
        [text],
    )
    assert record.deadline is None
    assert record.deadline_confidence == "unknown"
    assert record.city is None


def test_evidence_is_matched_against_jsonld_blob() -> None:
    html = '{"@type": "JobPosting", "baseSalary": {"value": 15000}}'
    record, errors = verify_evidence(
        make_record(evidence={"pay_amount": {"text": "15000", "method": "jsonld"}}),
        [html, "Casting call"],
    )
    assert errors == []
    assert record.pay_amount == 15000.0


def test_coverage_and_missing_evidence() -> None:
    record, _ = verify_evidence(make_record(), ["Location: Mumbai. Send to casting@example.com."])
    assert 0.0 <= evidence_coverage(record) <= 1.0
    # comp_type survives with a value but has no quotable evidence of its own.
    assert "comp_type" in missing_evidence(record)
    assert record.deadline is None
