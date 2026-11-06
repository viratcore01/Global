"""Explainable score features and SQLite FTS5 BM25 relevance."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..config import Profile
from ..db import Database
from ..normalize import normalize_ws
from ..util import clamp, days_until, parse_dt

KEY_FIELDS = (
    "deadline",
    "city",
    "comp_type",
    "contact_value",
    "languages",
    "role_types",
    "age",
    "audition_format",
    "shoot_window",
    "description",
)

GENERIC_QUERY_TERMS = {
    "actor",
    "actress",
    "casting",
    "audition",
    "open call",
    "short film",
    "web series",
    "voiceover",
}


def completeness(opportunity: dict[str, Any]) -> tuple[float, dict[str, float]]:
    present: dict[str, float] = {}
    present["deadline"] = 1.0 if opportunity.get("deadline") else 0.0
    present["city"] = 1.0 if opportunity.get("city") else 0.0
    present["comp_type"] = 1.0 if str(opportunity.get("comp_type") or "unknown") != "unknown" else 0.0
    present["contact_value"] = 1.0 if opportunity.get("contact_value") else 0.0
    present["languages"] = 1.0 if opportunity.get("languages") else 0.0
    present["role_types"] = 1.0 if opportunity.get("role_types") else 0.0
    present["age"] = 1.0 if opportunity.get("age_min") is not None or opportunity.get("age_max") is not None else 0.0
    present["audition_format"] = 1.0 if str(opportunity.get("audition_format") or "unknown") != "unknown" else 0.0
    present["shoot_window"] = 1.0 if opportunity.get("shoot_window") else 0.0
    description = normalize_ws(str(opportunity.get("description") or ""))
    present["description"] = 1.0 if len(description) >= 120 else (0.5 if len(description) >= 40 else 0.0)
    score = sum(present.values()) / len(KEY_FIELDS)
    return clamp(score), present


def urgency(opportunity: dict[str, Any], *, now: datetime | None = None) -> tuple[float, dict[str, Any]]:
    deadline = parse_dt(opportunity.get("deadline"))
    confidence = str(opportunity.get("deadline_confidence") or "unknown").lower()
    if deadline is None:
        return 0.25, {"days": None, "confidence": confidence, "reason": "unknown deadline"}
    remaining = days_until(deadline, now=now)
    if remaining is None:
        return 0.25, {"days": None, "confidence": confidence}
    if remaining < 0:
        return 0.0, {"days": round(remaining, 2), "confidence": confidence, "reason": "expired"}
    if remaining <= 3:
        value = 1.0
    elif remaining <= 7:
        value = 0.90
    elif remaining <= 14:
        value = 0.75
    elif remaining <= 30:
        value = 0.55
    elif remaining <= 60:
        value = 0.35
    else:
        value = 0.20
    multiplier = {"high": 1.0, "medium": 0.9, "low": 0.75, "unknown": 0.7}.get(confidence, 0.7)
    return clamp(value * multiplier), {
        "days": round(remaining, 2),
        "confidence": confidence,
        "multiplier": multiplier,
    }


def _query_terms(profile: Profile) -> list[str]:
    terms: list[str] = []
    for term in profile.search_terms():
        term = normalize_ws(term).lower()
        if term and term not in terms:
            terms.append(term)
    return terms[:24]


def _fts_quote(term: str) -> str:
    return '"' + term.replace('"', " ") + '"'


def bm25_relevance(
    db: Database | None,
    opportunity: dict[str, Any],
    profile: Profile,
) -> tuple[float, dict[str, Any]]:
    text = " ".join(
        [
            str(opportunity.get("title") or ""),
            str(opportunity.get("description") or ""),
            str(opportunity.get("city") or ""),
            " ".join(opportunity.get("role_types") or []),
            " ".join(opportunity.get("languages") or []),
        ]
    ).lower()
    terms = _query_terms(profile)
    matched = [term for term in terms if term and term in text]
    specific = [term for term in terms if term not in GENERIC_QUERY_TERMS]
    specific_matches = [term for term in matched if term in specific]
    coverage = (len(specific_matches) / max(1, len(specific))) if specific else 0.0
    generic_coverage = (len(matched) / max(1, len(terms))) if terms else 0.0
    keyword = clamp(0.7 * coverage + 0.3 * generic_coverage)

    bm25_value = 0.0
    fts_norm = 0.0
    if db is not None and opportunity.get("id") and terms:
        query = " OR ".join(_fts_quote(term) for term in terms)
        try:
            row = db.query_one(
                "SELECT bm25(opportunities_fts) AS rank FROM opportunities_fts "
                "WHERE opportunities_fts MATCH ? AND rowid = ?",
                (query, int(opportunity["id"])),
            )
            if row and row.get("rank") is not None:
                bm25_value = float(row["rank"])
                fts_norm = clamp(-bm25_value / 15.0)
        except Exception:
            bm25_value = 0.0
            fts_norm = 0.0
    score = clamp(0.55 * fts_norm + 0.45 * keyword)
    return score, {
        "matched": matched,
        "specific_matched": specific_matches,
        "coverage": round(coverage, 4),
        "fts_bm25": round(bm25_value, 4),
        "fts_norm": round(fts_norm, 4),
    }


def source_trust(
    db: Database | None,
    opportunity: dict[str, Any],
    *,
    cluster_sources: int = 1,
) -> tuple[float, dict[str, Any]]:
    tier = int(opportunity.get("trust_tier") or opportunity.get("source_tier") or 3)
    source_id = opportunity.get("source_id")
    value = {1: 0.90, 2: 0.70, 3: 0.45}.get(tier, 0.45)
    if db is not None and source_id:
        from ..repo import source_trust as repo_source_trust

        value = repo_source_trust(db, int(source_id), tier, cluster_sources)
    corroboration = min(0.2, max(0, int(cluster_sources) - 1) * 0.06)
    return clamp(value), {
        "tier": tier,
        "learned_or_base": round(value, 4),
        "cluster_sources": int(cluster_sources),
        "corroboration_bonus": round(corroboration, 4),
    }


def role_fit(opportunity: dict[str, Any], profile: Profile) -> float:
    roles = {str(role).lower() for role in (opportunity.get("role_types") or [])}
    if not roles:
        return 0.55
    wanted = {str(role).lower() for role in profile.role_types}
    if roles & wanted:
        return 1.0
    return 0.25


def language_fit(opportunity: dict[str, Any], profile: Profile) -> float:
    languages = {str(lang).lower() for lang in (opportunity.get("languages") or [])}
    wanted = {str(lang).lower() for lang in profile.languages}
    if not languages:
        return 0.55
    if languages & wanted:
        return 1.0
    if "hinglish" in languages and ({"hindi", "english"} & wanted):
        return 0.9
    return 0.2
