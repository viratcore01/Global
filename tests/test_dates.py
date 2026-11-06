"""Deadline parsing: English, Hinglish, Hindi and relative dates."""

from __future__ import annotations

from datetime import datetime

import pytest

from radar.extract.dates import extract_deadline, extract_published, extract_shoot_window, parse_absolute_date
from radar.util import IST

BASE = datetime(2026, 11, 5, 12, 0, tzinfo=IST)


def test_absolute_date_english() -> None:
    parsed = parse_absolute_date("15 December 2026")
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-12-15"


def test_deadline_keyword_end_of_day() -> None:
    hit = extract_deadline("Apply by 30 November 2026. Send your self tape.", base=BASE)
    assert hit is not None
    assert hit.dt.date().isoformat() == "2026-11-30"
    assert hit.confidence in {"high", "medium"}
    assert "30 November 2026" in hit.quote


def test_deadline_hindi() -> None:
    hit = extract_deadline("आवेदन की अंतिम तिथि 20 दिसंबर 2026।", base=BASE)
    assert hit is not None
    assert hit.dt.date().isoformat() == "2026-12-20"


def test_deadline_relative() -> None:
    hit = extract_deadline("Deadline: tomorrow", base=BASE)
    assert hit is not None
    assert hit.dt.date().isoformat() == "2026-11-06"


def test_infer_year_for_day_month_without_year() -> None:
    hit = extract_deadline("Last date: 2 January", base=BASE)
    assert hit is not None
    assert hit.dt.year == 2027


def test_no_deadline_for_plain_text() -> None:
    assert extract_deadline("A showreel should be under two minutes.", base=BASE) is None


def test_published_date() -> None:
    parsed = extract_published("Posted on 2026-10-28")
    assert parsed is not None
    assert parsed.date().isoformat() == "2026-10-28"


def test_shoot_window() -> None:
    window = extract_shoot_window("Shoot: 12 January 2027 to 20 January 2027 in Mumbai.", base=BASE)
    assert window is not None
    assert window.start.date().isoformat() == "2027-01-12"
    assert window.end.date().isoformat() == "2027-01-20"


@pytest.mark.parametrize("value", ["", "no dates here", "pay Rs 5,000 per day"])
def test_published_falls_back_to_now(value: str) -> None:
    parsed = extract_published(value)
    assert parsed is not None
    assert parsed.tzinfo is not None
