"""Stage 3/4 orchestration: eligibility, vetoes, risk, features and score."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Sequence

from .. import repo
from ..config import AppConfig, Profile
from ..db import Database
from ..util import clamp, get_logger, now_utc
from .eligibility import EligibilityResult, evaluate_eligibility
from .embeddings import Embedder, embedding_fit, profile_query_text
from .features import bm25_relevance, completeness, language_fit, role_fit, source_trust, urgency
from .learning import load_model, model_score
from .risk import RiskResult, evaluate_risk
from .vetoes import VetoResult, evaluate_vetoes

log = get_logger(__name__)


@dataclass
class ScoreResult:
    opportunity_id: int
    score: float = 0.0
    eligible: bool = True
    vetoed: bool = False
    veto: VetoResult | None = None
    eligibility: EligibilityResult | None = None
    risk: RiskResult | None = None
    features: dict[str, float] = field(default_factory=dict)
    breakdown: dict[str, Any] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)
    status: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "score": self.score,
            "eligible": self.eligible,
            "vetoed": self.vetoed,
            "veto": self.veto.to_dict() if self.veto else None,
            "eligibility": self.eligibility.to_dict() if self.eligibility else None,
            "risk": self.risk.to_dict() if self.risk else None,
            "features": self.features,
            "breakdown": self.breakdown,
            "risk_flags": self.risk_flags,
            "status": self.status,
        }


def _as_dict(opportunity: dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(opportunity, dict):
        out = dict(opportunity)
    else:
        out = opportunity.to_repo_dict() if hasattr(opportunity, "to_repo_dict") else dict(opportunity)
    # Defensive decoding for rows passed directly from SQLite.
    for key, list_key in (
        ("languages", "language_json"),
        ("role_types", "role_type_json"),
        ("shoot_window", "shoot_window_json"),
        ("risk_flags", "risk_flags_json"),
    ):
        value = out.get(key)
        if isinstance(value, str):
            from ..util import json_loads

            out[key] = json_loads(value, []) or []
        elif value is None:
            from ..util import json_loads

            out[key] = json_loads(out.get(list_key), []) or []
    return out


class Ranker:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        embedder: Embedder | None = None,
    ):
        self.config = config
        self.db = db
        self.profile = config.profile
        self.embedder = embedder or Embedder(config.settings)
        self._profile_vector: list[float] | None = None
        self._applied: list[str] | None = None
        self._cluster_sources: dict[int, int] = {}
        self.model = load_model(db)

    def _applied_texts(self) -> list[str]:
        if self._applied is None:
            rows = self.db.query(
                "SELECT o.title, o.description FROM labels l JOIN opportunities o ON o.id=l.opportunity_id "
                "WHERE l.action IN ('apply','applied','booked') ORDER BY l.created_at DESC LIMIT 12"
            )
            self._applied = [f"{row.get('title','')} {row.get('description','')}" for row in rows]
        return self._applied

    def _profile_vec(self) -> list[float]:
        if self._profile_vector is None:
            text = profile_query_text(self.profile, self._applied_texts())
            self._profile_vector = self.embedder.encode(text, query=True)
        return self._profile_vector

    def _cluster_source_count(self, opportunity: dict[str, Any]) -> int:
        cluster_id = opportunity.get("cluster_id")
        if not cluster_id:
            return 1
        cid = int(cluster_id)
        if cid not in self._cluster_sources:
            row = self.db.query_one("SELECT source_count FROM clusters WHERE id=?", (cid,))
            self._cluster_sources[cid] = max(1, int(row.get("source_count") or 1)) if row else 1
        return self._cluster_sources[cid]

    def score(self, opportunity: dict[str, Any] | Any, *, persist: bool = True) -> ScoreResult:
        opp = _as_dict(opportunity)
        opp_id = int(opp.get("id") or 0)
        if not opp_id:
            raise ValueError("ranker requires an inserted opportunity with an id")

        eligibility = evaluate_eligibility(opp, self.profile)
        if not eligibility.eligible:
            result = ScoreResult(
                opportunity_id=opp_id,
                score=0.0,
                eligible=False,
                eligibility=eligibility,
                breakdown={"eligible": False, "reasons": eligibility.reasons, "checks": eligibility.checks},
            )
            if persist:
                self._persist(opp, result, vetoed=False)
                repo.log_veto(
                    self.db,
                    opportunity_id=opp_id,
                    cluster_id=opp.get("cluster_id"),
                    stage="eligibility",
                    rule_id="eligibility_gate",
                    reason="; ".join(eligibility.reasons),
                    detail=str(eligibility.checks)[:2000],
                )
            return result

        veto = evaluate_vetoes(opp, self.profile, self.config.vetoes, db=self.db)
        if veto.vetoed:
            status = "expired" if veto.rule_id in {"expired", "stale_unknown_deadline"} else (
                "scam" if veto.rule_id == "reputation" else None
            )
            result = ScoreResult(
                opportunity_id=opp_id,
                score=0.0,
                eligible=True,
                vetoed=True,
                veto=veto,
                eligibility=eligibility,
                breakdown={"eligible": True, "vetoed": True, "veto": veto.to_dict()},
                status=status,
            )
            if persist:
                self._persist(opp, result, vetoed=True)
                repo.log_veto(
                    self.db,
                    opportunity_id=opp_id,
                    cluster_id=opp.get("cluster_id"),
                    stage=veto.stage,
                    rule_id=veto.rule_id,
                    reason=veto.reason,
                    detail=veto.detail[:2000] if veto.detail else None,
                )
            return result

        risk = evaluate_risk(opp, self.config.vetoes, db=self.db, settings=self.config.settings)
        embed_value, embed_method = embedding_fit(
            opp,
            self.profile,
            settings=self.config.settings,
            applied_texts=self._applied_texts(),
            embedder=self.embedder,
            profile_vector=self._profile_vec(),
        )
        role_value = role_fit(opp, self.profile)
        language_value = language_fit(opp, self.profile)
        # Blend a little explicit profile fit into the semantic component.  This
        # keeps the deterministic fallback useful and is fully explainable.
        embed_value = clamp(0.78 * embed_value + 0.14 * role_value + 0.08 * language_value)
        bm25_value, bm25_detail = bm25_relevance(self.db, opp, self.profile)
        trust_value, trust_detail = source_trust(
            self.db, opp, cluster_sources=self._cluster_source_count(opp)
        )
        urgency_value, urgency_detail = urgency(opp)
        completeness_value, completeness_detail = completeness(opp)

        features = {
            "embed": embed_value,
            "bm25": bm25_value,
            "trust": trust_value,
            "urgency": urgency_value,
            "completeness": completeness_value,
        }
        weights = dict(self.profile.weights)
        total_weight = sum(max(0.0, v) for v in weights.values()) or 1.0
        weighted = sum(features[name] * max(0.0, weights.get(name, 0.0)) for name in features) / total_weight
        base_score = weighted * 100.0

        used_model = None
        if self.model:
            model_value = model_score(self.model, features)
            # The learned model is still bounded by the same safety rules.  A
            # small anchor to the transparent weights prevents wild swings.
            base_score = 0.75 * model_value + 0.25 * base_score
            used_model = {
                "trained_at": self.model.get("trained_at"),
                "n": self.model.get("n"),
            }

        final_score = clamp(base_score - risk.penalty_points, 0.0, 100.0)
        existing_flags = [str(flag) for flag in (opp.get("risk_flags") or [])]
        risk_flags = list(dict.fromkeys([*existing_flags, *risk.flags]))
        breakdown = {
            "eligible": True,
            "vetoed": False,
            "score_before_penalties": round(base_score, 2),
            "risk_penalty": round(risk.penalty_points, 2),
            "features": {key: round(value, 4) for key, value in features.items()},
            "weights": {key: round(value, 4) for key, value in weights.items()},
            "details": {
                "embedding": {"value": round(embed_value, 4), "method": embed_method},
                "bm25": bm25_detail,
                "trust": trust_detail,
                "urgency": urgency_detail,
                "completeness": completeness_detail,
                "role_fit": round(role_value, 4),
                "language_fit": round(language_value, 4),
                "risk": risk.to_dict(),
            },
            "model": used_model,
        }
        result = ScoreResult(
            opportunity_id=opp_id,
            score=round(final_score, 2),
            eligible=True,
            vetoed=False,
            eligibility=eligibility,
            risk=risk,
            features=features,
            breakdown=breakdown,
            risk_flags=risk_flags,
        )
        if persist:
            self._persist(opp, result, vetoed=False)
        return result

    def _persist(self, opp: dict[str, Any], result: ScoreResult, *, vetoed: bool) -> None:
        repo.update_score(self.db, result.opportunity_id, result.score, result.breakdown)
        if result.risk_flags:
            repo.mark_risk_flags(self.db, result.opportunity_id, result.risk_flags)
        if result.status:
            repo.set_status(self.db, result.opportunity_id, result.status)

    def score_pending(self, *, limit: int = 200, rescore: bool = False) -> list[ScoreResult]:
        rows = repo.due_for_scoring(self.db, limit=limit)
        if rescore and not rows:
            rows = repo.list_opportunities(self.db, limit=limit, order="newest")
        results: list[ScoreResult] = []
        for row in rows:
            try:
                results.append(self.score(row))
            except Exception as exc:  # keep one bad record from stopping the batch
                log.exception("Scoring failed for opportunity %s: %s", row.get("id"), exc)
        return results


def score_opportunity(
    config: AppConfig,
    db: Database,
    opportunity: dict[str, Any] | Any,
    *,
    embedder: Embedder | None = None,
) -> ScoreResult:
    return Ranker(config, db, embedder=embedder).score(opportunity)
