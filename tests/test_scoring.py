"""Eligibility, vetoes, risk and the ranker."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from radar import repo
from radar.config import Profile, SourceConfig
from radar.score.eligibility import evaluate_eligibility
from radar.score.risk import evaluate_risk
from radar.score.vetoes import evaluate_vetoes
from radar.util import IST, iso, now_utc

BASE = datetime(2026, 11, 5, 12, 0, tzinfo=IST)


def opportunity(**overrides):
    data = {
        "id": 1,
        "title": "Casting call: supporting actress",
        "description": "Casting a supporting actress for a Hindi web series in Mumbai.",
        "opportunity_type": "casting",
        "age_min": 22,
        "age_max": 28,
        "gender_presentation": "any",
        "city": "Mumbai",
        "country": "India",
        "languages": ["Hindi", "English"],
        "comp_type": "day_rate",
        "pay_amount": 15000.0,
        "fee_required": False,
        "nudity": "none",
        "audition_format": "self_tape",
        "deadline": iso(now_utc() + timedelta(days=20)),
    }
    data.update(overrides)
    return data


@pytest.fixture()
def profile() -> Profile:
    return Profile.from_dict(
        {
            "age_range": [18, 30],
            "gender_presentation": "any",
            "base_city": "Delhi",
            "base_country": "India",
            "preferred_cities": ["Delhi", "Mumbai", "Noida"],
            "travel_radius_km": 300,
            "languages": ["Hindi", "English", "Hinglish"],
            "nudity_max": "none",
            "paid_only": False,
            "fee_tolerance": "festivals_only",
            "remote_selftape_ok": True,
        }
    )


def test_eligible_role_passes(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(), profile)
    assert result.eligible, result.reasons
    assert result.checks["age"]["ok"]
    assert result.checks["language"]["ok"]


def test_age_conflict_rejected(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(age_min=50, age_max=60), profile)
    assert not result.eligible
    assert any("age" in reason for reason in result.reasons)


def test_unknown_age_passes(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(age_min=None, age_max=None), profile)
    assert result.eligible


def test_language_conflict_rejected_but_hinglish_bridges(profile: Profile) -> None:
    tamil = evaluate_eligibility(opportunity(languages=["Tamil"]), profile)
    assert not tamil.eligible
    hinglish = evaluate_eligibility(opportunity(languages=["Hinglish"]), profile)
    assert hinglish.eligible


def test_location_outside_radius_rejected(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(city="Chennai", audition_format="in_person"), profile)
    assert not result.eligible
    assert "travel radius" in " ".join(result.reasons)


def test_self_tape_bypasses_location(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(city="Chennai", audition_format="self_tape"), profile)
    assert result.eligible


def test_nudity_limit_rejected(profile: Profile) -> None:
    result = evaluate_eligibility(opportunity(nudity="full"), profile)
    assert not result.eligible


def test_paid_only_profile_rejects_unpaid(profile: Profile) -> None:
    profile.paid_only = True
    result = evaluate_eligibility(opportunity(comp_type="unpaid"), profile)
    assert not result.eligible


def test_festival_fee_allowed_but_casting_fee_rejected(profile: Profile) -> None:
    festival = evaluate_eligibility(opportunity(opportunity_type="festival", fee_required=True), profile)
    casting = evaluate_eligibility(opportunity(fee_required=True), profile)
    assert festival.eligible
    assert not casting.eligible


def test_expired_deadline_vetoed(config, profile: Profile) -> None:
    veto = evaluate_vetoes(
        opportunity(deadline=iso(now_utc() - timedelta(days=1))),
        profile,
        config.vetoes,
    )
    assert veto.vetoed
    assert veto.rule_id == "expired"


def test_pay_to_apply_vetoed(config, profile: Profile) -> None:
    veto = evaluate_vetoes(
        opportunity(description="Audition registration fee: Rs 999. Pay to apply now."),
        profile,
        config.vetoes,
    )
    assert veto.vetoed
    assert veto.rule_id in {"registration_fee", "pay_to_apply", "pay_to_apply_phrase"}


def test_festival_fee_is_not_vetoed(config, profile: Profile) -> None:
    veto = evaluate_vetoes(
        opportunity(
            opportunity_type="festival",
            title="Pune Short Film Festival",
            description="Entry fee: Rs 500 per film. Submission fee applies.",
            fee_required=True,
            comp_type="unknown",
        ),
        profile,
        config.vetoes,
    )
    assert not veto.vetoed


def test_unpaid_veto_when_profile_requires_pay(config, profile: Profile) -> None:
    profile.paid_only = True
    veto = evaluate_vetoes(opportunity(comp_type="credit_only"), profile, config.vetoes)
    assert veto.vetoed
    assert veto.rule_id == "unpaid_paid_only"


def test_reputation_veto(config, profile: Profile, db) -> None:
    opportunity_data = opportunity(contact_value="scam@example.com", contact_channel="email")
    repo.add_reputation(db, kind="email", value="scam@example.com", reason="user marked scam")
    veto = evaluate_vetoes(opportunity_data, profile, config.vetoes, db=db)
    assert veto.vetoed
    assert veto.rule_id == "reputation"


def test_risk_penalties(config, profile: Profile) -> None:
    risk = evaluate_risk(
        opportunity(
            description="Urgent casting! WhatsApp only. Contact me on WhatsApp. Pay advance payment first.",
        ),
        config.vetoes,
        settings=config.settings,
    )
    assert risk.penalty_points > 0
    assert "whatsapp_only" in risk.flags
    assert "urgency_pressure" in risk.flags


def test_ranker_scores_ingested_opportunity(config, db, pipeline) -> None:
    source_id = repo.add_source(
        db,
        SourceConfig(name="Ranker fixture", type="web", url="https://fixtures.example", tier=2, enabled=False),
    )
    source = repo.get_source(db, source_id)
    result = pipeline.ingest(
        "fixture://test/ranker",
        source,
        kind="page",
        payload={"html": (Path(__file__).parent / "fixtures" / "jsonld_casting.html").read_text(encoding="utf-8")},
    )
    assert result.records
    opportunity_id = result.records[0].opportunity_id
    stored = repo.get_opportunity(db, opportunity_id)
    assert stored is not None
    scored = pipeline.ranker.score(stored)
    assert 0.0 <= scored.score <= 100.0
    assert not scored.vetoed
    assert set(scored.breakdown["features"]) == {"embed", "bm25", "trust", "urgency", "completeness"}
    refreshed = repo.get_opportunity(db, opportunity_id)
    assert refreshed["score"] == pytest.approx(scored.score, abs=0.01)
    assert refreshed["score_breakdown"]["features"]


def test_scam_ingestion_is_vetoed_and_logged(config, db, pipeline) -> None:
    text = (Path(__file__).parent / "fixtures" / "scam_post.txt").read_text(encoding="utf-8")
    source_id = repo.add_source(
        db,
        SourceConfig(name="Scam fixture", type="web", url="https://fixtures.example", tier=3, enabled=False),
    )
    result = pipeline.ingest(
        "fixture://test/scam",
        repo.get_source(db, source_id),
        kind="page",
        payload={"text": text, "title": "Female actor required for music video"},
    )
    assert result.records
    opportunity_id = result.records[0].opportunity_id
    stored = repo.get_opportunity(db, opportunity_id)
    scored = pipeline.ranker.score(stored)
    # The pay-to-apply fee is caught by the eligibility gate before ranking;
    # either path must keep the item at zero and leave an audit trail.
    assert scored.score == 0.0
    assert scored.vetoed or not scored.eligible
    assert repo.list_vetoes(db, days=1, limit=10)
