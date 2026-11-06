"""URL, contact and text normalisation rules."""

from __future__ import annotations

import pytest

from radar.normalize import (
    canonicalize_url,
    detect_languages,
    extract_emails,
    extract_phones,
    extract_urls,
    is_freemail,
    normalize_email,
    normalize_phone,
    registrable_domain,
    span_in_text,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://www.example.com/jobs/1/", "https://example.com/jobs/1"),
        ("https://m.example.com/a?utm_source=x&b=2#frag", "https://example.com/a?b=2"),
        ("https://example.com/amp/job/", "https://example.com/job"),
        ("https://EXAMPLE.com:443/Path/", "https://example.com/Path"),
        ("https://example.com//double//slash", "https://example.com/double/slash"),
        ("https://example.com/a?fbclid=1&gclid=2", "https://example.com/a"),
    ],
)
def test_canonicalize_url(raw: str, expected: str) -> None:
    assert canonicalize_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "mailto:someone@example.com",
        "javascript:void(0)",
        "/relative/path",
        "not a url",
        "",
        "ftp://example.com/file",
    ],
)
def test_canonicalize_url_rejects_non_web(raw: str) -> None:
    assert canonicalize_url(raw) is None


def test_canonicalize_relative_with_base() -> None:
    assert canonicalize_url("/jobs/7", "https://example.com/list/") == "https://example.com/jobs/7"


def test_registrable_domain() -> None:
    assert registrable_domain("www.casting.example.com") == "example.com"
    assert registrable_domain("jobs.example.co.uk") == "example.co.uk"
    assert registrable_domain("localhost") == "localhost"


def test_email_extraction_handles_sentence_period_and_multilabel_tld() -> None:
    assert extract_emails("Send to casting@bluelantern.example.") == ["casting@bluelantern.example"]
    assert extract_emails("write to a.b@site.co.uk now") == ["a.b@site.co.uk"]
    assert extract_emails("poster@site.example.png") == []


def test_normalize_email() -> None:
    assert normalize_email(" Casting@Example.COM ") == "casting@example.com"
    assert normalize_email("not-an-email") is None


def test_freemail() -> None:
    assert is_freemail("someone@gmail.com")
    assert not is_freemail("casting@production.example")


def test_phone_normalisation() -> None:
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("0091-9876543210") == "+919876543210"
    assert normalize_phone("123") is None
    assert extract_phones("Call 9876543210 or +1 415 555 0100") == ["+919876543210", "+14155550100"]


def test_url_extraction_strips_trailing_punctuation() -> None:
    urls = extract_urls("Apply at https://example.com/apply?id=3. Thanks!")
    assert urls == ["https://example.com/apply?id=3"]


def test_language_detection() -> None:
    assert {"Hindi", "English"} <= set(detect_languages("Hindi and English speaking role"))
    assert "Hinglish" in detect_languages("Hinglish web series casting")
    assert "Hindi" in detect_languages("कास्टिंग: हिंदी वेब सीरीज़")


def test_span_in_text_ignores_whitespace() -> None:
    assert span_in_text("Pay: Rs 15,000 per day", "Description\nPay:   Rs 15,000 per day\n")
    assert not span_in_text("Pay: Rs 99,000", "Pay: Rs 15,000 per day")
