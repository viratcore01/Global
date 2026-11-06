"""Typed configuration, profile parsing, sources and veto rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from radar.config import AppConfig, Profile, SourceConfig, VetoConfig, load_sources
from radar.normalize import normalize_text


def test_alert_threshold_precedence(config) -> None:
    config.settings.instant_threshold = None
    config.settings.digest_threshold = None
    assert config.instant_cutoff == float(config.profile.thresholds.get("instant", 80))
    assert config.digest_cutoff == float(config.profile.thresholds.get("digest", 55))
    config.settings.instant_threshold = 92.0
    config.settings.digest_threshold = 61.0
    assert config.instant_cutoff == 92.0
    assert config.digest_cutoff == 61.0


def test_contact_url_is_part_of_the_user_agent(demo_home, monkeypatch) -> None:
    monkeypatch.setenv("RADAR_USER_AGENT", "RadarTest/1.0")
    monkeypatch.setenv("RADAR_CONTACT_URL", "https://example.com/about")
    config = AppConfig.load()
    assert config.settings.effective_user_agent == "RadarTest/1.0 (+https://example.com/about)"


def test_profile_defaults_and_roundtrip() -> None:
    profile = Profile.from_dict(
        {
            "name": "Test Actor",
            "age_range": [24, 31],
            "base_city": "Delhi",
            "languages": "Hindi, English",
            "role_types": ["lead", "voiceover"],
            "preferred_cities": ["Delhi", "Mumbai"],
            "weights": {"embed": 2, "bm25": 1, "trust": 1, "urgency": 1, "completeness": 1},
            "thresholds": {"instant": 82},
        }
    )
    assert profile.age_range == (24, 31)
    assert profile.languages == ["Hindi", "English"]
    assert profile.role_types == ["lead", "voiceover"]
    assert profile.thresholds["instant"] == 82.0
    assert abs(sum(profile.weights.values()) - 1.0) < 1e-9
    assert "Delhi" in profile.to_text()
    assert "casting" in profile.search_terms()
    assert profile.nudity_limit() == 0


def test_age_range_reversed_is_normalised() -> None:
    profile = Profile.from_dict({"age_range": [40, 20]})
    assert profile.age_range == (20, 40)


def test_real_config_loads(config: AppConfig) -> None:
    assert config.profile.base_city
    assert config.sources, "config/sources.yaml should define example sources"
    assert config.vetoes.vetoes, "veto patterns must exist"
    assert any(rule.id for rule in config.vetoes.vetoes)
    for rule in config.vetoes.vetoes:
        assert rule.pattern
    assert all(not source.enabled for source in config.sources), "example sources ship disabled"


def test_source_url_aliases() -> None:
    source = SourceConfig.from_dict(
        {"name": "Example", "type": "web", "url": "https://a.example", "config": {"urls": ["https://b.example"]}}
    )
    assert source.urls == ["https://a.example", "https://b.example"]


def test_unknown_source_type_rejected() -> None:
    with pytest.raises(ValueError):
        SourceConfig.from_dict({"name": "Bad", "type": "mystery"})


def test_duplicate_source_names_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(
        "sources:\n"
        "  - {name: Duplicate, type: web, url: 'https://a.example'}\n"
        "  - {name: Duplicate, type: web, url: 'https://b.example'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="duplicate source name"):
        load_sources(path)


def test_veto_config_compiles_and_matches(tmp_path: Path) -> None:
    path = tmp_path / "veto.yaml"
    path.write_text(
        r"""
vetoes:
  - id: pay_to_apply
    name: Pay to apply
    pattern: '(?:registration|audition)\s+fee'
    reason: 'never pay to apply'
    applies_to: [casting]
penalties:
  - id: whatsapp_only
    name: WhatsApp only
    pattern: 'whatsapp only'
    weight: 4
""",
        encoding="utf-8",
    )
    vetoes = VetoConfig.load(path)
    assert len(vetoes.vetoes) == 1
    matched = vetoes.veto_for("Audition fee: Rs 999", "casting")
    assert matched is not None and matched.id == "pay_to_apply"
    assert vetoes.veto_for("Audition fee: Rs 999", "festival") is None
    assert [rule.id for rule in vetoes.penalty_matches("WhatsApp only contact")] == ["whatsapp_only"]


def test_bad_regex_rejected(tmp_path: Path) -> None:
    path = tmp_path / "veto.yaml"
    path.write_text("vetoes:\n  - id: broken\n    pattern: '(['\n", encoding="utf-8")
    with pytest.raises(ValueError, match="invalid regex"):
        VetoConfig.load(path)


def test_hinglish_text_normalisation() -> None:
    text = normalize_text("Casting  call\r\nfor  a   Hinglish web-series\tDelhi")
    assert "\r" not in text
    assert "Casting call" in text
    assert "  " not in text
