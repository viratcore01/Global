"""Understanding the call: fact tables, pay wording, ended cards, apply briefs."""

from __future__ import annotations

from datetime import datetime

from radar.brief import build_brief
from radar.config import Profile
from radar.extract.badges import closed_from_text, is_closed_badge
from radar.extract.board import detect_board_items
from radar.extract.facts import extract_facts
from radar.extract.heuristics import extract_heuristics
from radar.extract.pay import classify_compensation
from radar.extract.pipeline import ExtractionEngine
from radar.util import IST

BASE = datetime(2026, 11, 5, 12, 0, tzinfo=IST)

CARD_PAGE = """<html><body>
  <h1>Role: Lead Hero - Badshah movie</h1>
  <div class="row"><div class="col-md-4">Type of the project</div><div class="col-md-8">Castings for movies</div></div>
  <div class="row"><div class="col-md-4">Casting dates</div><div class="col-md-8">to 18 November 2026</div></div>
  <div class="row"><div class="col-md-4">Country, city</div><div class="col-md-8">India, Hailakandi</div></div>
  <div class="row"><div class="col-md-4">Payment terms</div><div class="col-md-8">Fee + royalty</div></div>
  <div class="row"><div class="col-md-4">Wanted</div><div class="col-md-8">15+ years old, M</div></div>
  <div class="row"><div class="col-md-4">Status</div><div class="col-md-8">Open</div></div>
  <p>Badshah is a Hindi action film. We are looking for actors. Send a self tape to badshah@example.com.</p>
  <h2>Other current auditions</h2>
  <div class="row"><div class="col-md-4">Wanted</div><div class="col-md-8">13-16 years old, M</div></div>
  <div class="row"><div class="col-md-4">Payment terms</div><div class="col-md-8">TFP</div></div>
  <div class="row"><div class="col-md-4">Casting dates</div><div class="col-md-8">to 26 November 2026</div></div>
</body></html>"""


def test_fact_table_beats_prose_and_related_cards() -> None:
    facts = extract_facts(
        "Type of the project\nCastings for movies\n"
        "Casting dates\nto 18 November 2026\n"
        "Country, city\nIndia,\nHailakandi\n"
        "Payment terms\n$\nFee + royalty\n"
        "Wanted\n15+ years old, M\n"
        "Status\nOpen\n"
        "Project description\nWe are looking for actors.\n"
        "Other current auditions\nWanted\n13-16 years old, M\n",
        base=BASE,
    )
    assert facts.fields["deadline"] == "2026-11-18"
    assert facts.fields["comp_type"] == "flat"
    assert facts.fields["city"] == "Hailakandi"
    assert facts.fields["age_min"] == 15
    assert facts.fields.get("age_max") is None
    assert facts.fields["gender_presentation"] == "male"
    assert facts.fields.get("status") == "new"


def test_related_cards_do_not_leak_into_the_record(config) -> None:
    raw = {
        "id": 1,
        "canonical_url": "https://board.example/auditions/c/1",
        "body_text": CARD_PAGE,
        "content_type": "text/html",
        "meta": {"title": "Role: Lead Hero - Badshah movie", "detail": True},
        "kind": "page",
        "source_id": 1,
    }
    result = ExtractionEngine(config.settings).extract(raw, {"type": "web", "tier": 2})
    assert len(result.records) == 1
    record = result.records[0]
    assert record.deadline and record.deadline.startswith("2026-11-18")
    assert record.age_min == 15
    assert record.age_max is None
    assert record.comp_type == "flat"
    assert record.city == "Hailakandi"
    assert record.contact_value == "badshah@example.com"


def test_detail_page_ignores_listing_selectors(config) -> None:
    html = """<html><body>
      <h1>Solaris Nights</h1>
      <div class="col-xl-4"><a href="/login">Login</a></div>
      <div class="col-xl-4"><a href="/join">Join</a></div>
      <div class="col-xl-4"><a href="/amdb">AMDB</a></div>
      <p>Casting call: supporting actor for a short film in Delhi.
      Casting for a short film. Deadline: 20 December 2026.
      Send a self tape to solaris@example.com.</p>
    </body></html>"""
    raw = {
        "id": 1,
        "canonical_url": "https://board.example/auditions/c/9",
        "body_text": html,
        "content_type": "text/html",
        "meta": {"title": "Solaris Nights", "detail": True},
        "kind": "page",
        "source_id": 1,
    }
    source = {
        "type": "web",
        "tier": 2,
        "extractor": "css",
        "extractor_config": {"item": "div.col-xl-4", "fields": {"title": {"selector": "a"}}},
    }
    result = ExtractionEngine(config.settings).extract(raw, source)
    assert len(result.records) == 1
    assert result.records[0].title == "Solaris Nights"


def test_closed_badge_marks_listing_expired() -> None:
    assert is_closed_badge("Ended")
    assert is_closed_badge("[Expired]")
    assert not is_closed_badge("Open")
    assert closed_from_text("Ended Bhoot Returns Casting for a movie")
    assert not closed_from_text("Bhoot Returns casting call")

    html = """<html><body>
      <div class="card">
        <div class="casting_card_title"><a href="/c/1">Lead actor for Delhi short</a></div>
        <span class="badge badge-secondary">Ended</span>
        <p>Casting for a short film. Apply before 20 November 2026.</p>
      </div>
      <div class="card">
        <div class="casting_card_title"><a href="/c/2">Supporting actress for Punjab film</a></div>
        <p>We are casting for a feature film. Apply by 25 November 2026.</p>
      </div>
      <div class="card">
        <div class="casting_card_title"><a href="/c/3">DOP required for an ad film</a></div>
        <p>Crew call: DOP required. Paid. Apply before 30 November 2026.</p>
      </div>
    </body></html>"""
    items = detect_board_items(html, "https://board.example/auditions")
    assert len(items) == 3
    flags = {item.url.rsplit("/", 1)[-1]: item.closed for item in items}
    assert flags["1"] is True
    assert flags["2"] is False


def test_compensation_wording() -> None:
    assert classify_compensation("Casting for a short film , TFP").comp_type == "credit_only"
    assert classify_compensation("Payment terms: Not paid").comp_type == "unpaid"
    assert classify_compensation("Fee + royalty").comp_type == "flat"
    assert classify_compensation("Remuneration paid: 6 lakh").comp_type == "flat"
    assert classify_compensation("Deferred payment, profit sharing").comp_type == "deferred"
    assert classify_compensation("Entry fee: Rs 300 to submit").comp_type == "unknown"
    assert classify_compensation("royalty-free music").comp_type == "unknown"


def test_brief_builds_an_actionable_plan() -> None:
    profile = Profile.from_dict({"base_city": "Delhi", "travel_radius_km": 300})
    opportunity = {
        "id": 7,
        "title": "Lead actor for Delhi short",
        "score": 72.0,
        "deadline": "2026-11-20T18:00:00+05:30",
        "city": "Delhi",
        "comp_type": "flat",
        "pay_amount": 8000,
        "currency": "INR",
        "contact_channel": "email",
        "contact_value": "casting@example.com",
        "audition_format": "self_tape",
        "languages": ["Hindi", "English"],
        "status": "new",
        "description": "Casting for a short film. Send a self tape and headshot.",
    }
    brief = build_brief(opportunity, profile, now=BASE)
    assert brief.blockers == []
    assert "casting@example.com" in brief.next_action
    assert any("self-tape" in step.lower() for step in brief.checklist)
    assert brief.days_left == 15.2


def test_brief_flags_dead_ends() -> None:
    profile = Profile.from_dict({"base_city": "Delhi", "travel_radius_km": 300, "paid_only": True})
    opportunity = {
        "id": 8,
        "title": "Unpaid Chennai short",
        "score": 10.0,
        "city": "Chennai",
        "comp_type": "unpaid",
        "status": "expired",
        "contact_channel": "unknown",
        "description": "Unpaid student short film.",
    }
    brief = build_brief(opportunity, profile, now=BASE)
    joined = " | ".join(brief.blockers)
    assert "paid-only" in joined
    assert "travel radius" in joined
    assert "ended" in joined
    assert any("hidden behind a login" in note for note in brief.notes)
    assert brief.next_action.startswith("Skip")

