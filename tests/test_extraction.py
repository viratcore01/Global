"""Deterministic extraction: heuristics, structured data, CSS and fee handling."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from radar.extract.heuristics import extract_heuristics
from radar.extract.jsonld import extract_structured
from radar.extract.pipeline import ExtractionEngine
from radar.models import OpportunityRecord
from radar.util import IST

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


BASE = datetime(2026, 11, 5, 12, 0, tzinfo=IST)


def engine(config) -> ExtractionEngine:
    return ExtractionEngine(config.settings)


def extract_html(config, html: str, *, url: str = "https://example.com/job/1", tier: int = 2):
    raw = {
        "id": 1,
        "canonical_url": url,
        "body_text": html,
        "content_type": "text/html",
        "meta": {},
        "kind": "page",
        "source_id": 1,
    }
    return engine(config).extract(raw, {"type": "web", "tier": tier})


def extract_text(config, text: str, *, url: str = "https://example.com/job/2"):
    raw = {
        "id": 1,
        "canonical_url": url,
        "body_text": text,
        "content_type": "text/plain",
        "meta": {},
        "kind": "email",
        "source_id": 1,
    }
    return engine(config).extract(raw, {"type": "email", "tier": 2})


def test_heuristics_extract_english_fields() -> None:
    text = (
        "Casting call: supporting actress for a Hindi and English web series.\n"
        "Role: female, age 22-30. Location: Mumbai.\n"
        "Shoot: 12 January 2027 to 20 January 2027.\n"
        "Pay: Rs 15,000 per day. Self tape audition.\n"
        "Deadline: 15 December 2026. Send to casting@example.com. No nudity."
    )
    result = extract_heuristics(text, title_hint="Casting call", base=BASE, assume_opportunity=True)
    fields = result.fields
    assert result.is_opportunity
    assert fields["city"] == "Mumbai"
    assert fields["country"] == "India"
    assert fields["opportunity_type"] == "casting"
    assert fields["pay_amount"] == 15000.0
    assert fields["comp_type"] == "day_rate"
    assert fields["currency"] == "INR"
    assert fields["age_min"] == 22 and fields["age_max"] == 30
    assert fields["gender_presentation"] == "female"
    assert fields["nudity"] == "none"
    assert fields["audition_format"] == "self_tape"
    assert fields["contact_value"] == "casting@example.com"
    assert fields["contact_channel"] == "email"
    assert fields["deadline"].startswith("2026-12-15")
    assert fields["shoot_window"] == ["2027-01-12", "2027-01-20"]


def test_scam_fee_is_stored_not_treated_as_pay() -> None:
    text = read_fixture("scam_post.txt")
    fields = extract_heuristics(text, title_hint="Casting call", base=BASE).fields
    assert fields["fee_required"] is True
    assert fields["fee_amount"] == 999.0
    assert fields["pay_amount"] == 100000.0
    assert fields["contact_channel"] == "whatsapp"


def test_festival_entry_fee_is_stored() -> None:
    text = (
        "Pune Short Film Festival - call for entries. Submit by 10 January 2027. "
        "Entry fee: Rs 500 per film. The festival runs in Pune, India."
    )
    fields = extract_heuristics(text, title_hint="Pune Short Film Festival", base=BASE).fields
    assert fields["opportunity_type"] == "festival"
    assert fields["fee_required"] is True
    assert fields["fee_amount"] == 500.0
    assert fields.get("pay_amount") in (None, 0.0)


def test_hindi_pay_is_day_rate() -> None:
    text = "कास्टिंग: हिंदी वेब सीरीज़ के लिए अभिनेत्री। मानदेय: ₹12,000 प्रति दिन।"
    fields = extract_heuristics(text, title_hint="कास्टिंग", base=BASE, assume_opportunity=True).fields
    assert fields["pay_amount"] == 12000.0
    assert fields["comp_type"] == "day_rate"
    assert "Hindi" in fields["languages"]


def test_non_opportunity_is_skipped(config) -> None:
    result = extract_text(
        config,
        "Five ways to improve your acting showreel. A showreel is a short compilation "
        "of your best work. General advice for performers, not a job posting.",
    )
    assert result.skipped
    assert result.records == []


def test_jsonld_fixture(config) -> None:
    result = extract_html(config, read_fixture("jsonld_casting.html"))
    assert result.method == "jsonld"
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record, OpportunityRecord)
    assert record.title == "Casting: Supporting Actress for Mumbai Web Series"
    assert record.city == "Mumbai"
    assert record.country == "India"
    assert record.pay_amount == 15000.0
    assert record.comp_type == "day_rate"
    assert record.currency == "INR"
    assert record.contact_value == "casting@bluelantern.example"
    assert record.deadline.startswith("2026-12-15")
    assert record.age_min == 22 and record.age_max == 30
    assert record.nudity == "none", (record.evidence.get("nudity"), result.text[-220:])
    assert record.audition_format == "self_tape"
    assert record.trust_tier == 2
    assert record.evidence.get("deadline") is not None


def test_jsonld_salary_unit_inside_quantitative_value() -> None:
    html = """
    <html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Actor needed",
     "description":"Casting an actor. Apply by 20 December 2026.",
     "validThrough":"2026-12-20",
     "baseSalary":{"@type":"MonetaryAmount","currency":"INR",
       "value":{"@type":"QuantitativeValue","value":9000,"unitText":"DAY"}}}
    </script></head><body><h1>Actor needed</h1><p>Casting an actor. Apply by 20 December 2026.</p></body></html>
    """
    structured = extract_structured(html)
    assert structured.fields["pay_amount"] == 9000.0
    assert structured.fields["comp_type"] == "day_rate"


def test_css_listing_extraction(config) -> None:
    source = {
        "type": "web",
        "tier": 1,
        "extractor": "css",
        "extractor_config": {
            "item": "article.casting-card",
            "fields": {
                "title": {"selector": "h2.title"},
                "deadline": {"selector": "span.deadline"},
                "location": {"selector": "span.location"},
                "pay": {"selector": "span.pay"},
            },
            "link": {"selector": "a.apply", "attr": "href"},
        },
    }
    html = read_fixture("css_listing.html")
    raw = {
        "id": 5,
        "canonical_url": "https://casting.example/board",
        "body_text": html,
        "content_type": "text/html",
        "meta": {},
        "kind": "page",
        "source_id": 3,
    }
    result = engine(config).extract(raw, source)
    assert result.method == "css"
    assert len(result.records) >= 2
    titles = " ".join(record.title for record in result.records)
    assert "Lead actor" in titles
    assert "Voiceover" in titles


def test_text_email_fixture(config) -> None:
    result = extract_text(config, read_fixture("email_casting.txt"))
    assert not result.skipped
    record = result.records[0]
    assert record.city == "Delhi"
    assert record.pay_amount == 8000.0
    assert record.deadline.startswith("2026-11-30")
    assert record.opportunity_type == "casting"


@pytest.mark.parametrize("fixture", ["festival.html", "jsonld_casting.html", "css_listing.html"])
def test_fixture_pages_never_raise(config, fixture: str) -> None:
    result = extract_html(config, read_fixture(fixture), url=f"https://example.com/{fixture}")
    assert result.errors == [] or result.records
    assert (FIXTURES / fixture).exists()
