"""Cluster assignment: exact keys, MinHash proximity, structured fuzz."""

from __future__ import annotations

from typing import Any

from .. import repo
from ..db import Database
from ..models import OpportunityRecord
from ..normalize import canonical_hash, normalize_ws
from ..util import get_logger, iso, now_utc
from .minhash import NEAR_DUPLICATE_THRESHOLD, estimate_jaccard, signature_for_text, text_similarity
from .structured import fuzzy_match, make_structured_key

log = get_logger(__name__)


def _as_dict(opportunity: OpportunityRecord | dict[str, Any]) -> dict[str, Any]:
    if isinstance(opportunity, OpportunityRecord):
        return opportunity.to_repo_dict()
    return dict(opportunity)


def assign_cluster(
    db: Database,
    opportunity: OpportunityRecord | dict[str, Any],
    *,
    opportunity_id: int | None = None,
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> dict[str, Any]:
    """Assign an opportunity to a cluster, creating or merging clusters.

    Returns ``{"cluster_id", "duplicate_of", "similarity", "reason"}``.
    The caller must already have inserted the opportunity (except when passing an
    explicit ``opportunity_id`` for an existing row).
    """
    data = _as_dict(opportunity)
    opp_id = opportunity_id or data.get("id")
    if not opp_id:
        raise ValueError("assign_cluster needs an inserted opportunity id")

    structured = data.get("structured_key") or make_structured_key(
        title=str(data.get("title") or ""),
        city=data.get("city"),
        deadline=data.get("deadline"),
        contact=data.get("contact_value"),
    )
    signature = data.get("minhash_signature")
    text = f"{data.get('title', '')}\n{data.get('description', '')}"
    if not signature:
        signature = signature_for_text(text)
    db.execute(
        "UPDATE opportunities SET structured_key=?, minhash_signature=?, updated_at=? WHERE id=?",
        (structured, signature, iso(now_utc()), opp_id),
    )

    candidates = repo.recent_for_dedup(db, days=365, limit=8000)
    best: tuple[float, dict[str, Any], str] | None = None
    for candidate in candidates:
        if int(candidate["id"]) == int(opp_id):
            continue
        if candidate.get("canonical_url") and candidate["canonical_url"] == data.get("canonical_url"):
            # URL-level duplicate; upsert normally prevents this, but keep the
            # behaviour deterministic for imported/replayed records.
            score = 1.0
            reason = "canonical_url"
        elif candidate.get("structured_key") and candidate["structured_key"] == structured:
            score = 0.98
            reason = "structured_key"
        else:
            score = estimate_jaccard(signature, candidate.get("minhash_signature"))
            reason = "minhash" if score >= threshold else ""
            if score >= threshold:
                candidate_text = f"{candidate.get('title', '')}\n{candidate.get('description', '')}"
                exact = text_similarity(text, candidate_text)
                if exact < threshold:
                    # A signature collision is not proof.  Fall through to the
                    # fuzzy-key check, which is cheaper than a false merge.
                    score = 0.0
                    reason = ""
                else:
                    score = max(score, exact)
            if score < threshold:
                candidate_dict = dict(candidate)
                if fuzzy_match(data, candidate_dict):
                    score = max(score, 0.75)
                    reason = "structured_fuzzy"
        if best is None or score > best[0]:
            best = (score, candidate, reason)

    if best and best[0] >= 0.70:
        score, candidate, reason = best
        candidate_cluster = candidate.get("cluster_id")
        # recent_for_dedup intentionally omits cluster_id; fetch it.
        if candidate_cluster is None:
            row = db.query_one("SELECT cluster_id FROM opportunities WHERE id=?", (candidate["id"],))
            candidate_cluster = row.get("cluster_id") if row else None
        if candidate_cluster:
            cluster_id = int(candidate_cluster)
        else:
            cluster_id = repo.get_or_create_cluster(
                db,
                cluster_key=f"structured:{candidate.get('structured_key') or canonical_hash(candidate['canonical_url'])}",
                title=candidate.get("title"),
                city=candidate.get("city"),
                deadline=candidate.get("deadline"),
                contact_value=candidate.get("contact_value"),
            )
            repo.set_cluster(db, int(candidate["id"]), cluster_id)
        repo.set_cluster(db, int(opp_id), cluster_id)
        _merge_other_clusters(db, cluster_id, int(opp_id))
        repo.refresh_cluster_stats(db, cluster_id)
        return {
            "cluster_id": cluster_id,
            "duplicate_of": int(candidate["id"]),
            "similarity": round(float(score), 4),
            "reason": reason,
        }

    cluster_key = f"structured:{structured}" if structured else f"url:{canonical_hash(str(data.get('canonical_url') or ''))}"
    cluster_id = repo.get_or_create_cluster(
        db,
        cluster_key=cluster_key,
        title=data.get("title"),
        city=data.get("city"),
        deadline=data.get("deadline"),
        contact_value=data.get("contact_value"),
    )
    repo.set_cluster(db, int(opp_id), cluster_id)
    repo.refresh_cluster_stats(db, cluster_id)
    return {"cluster_id": cluster_id, "duplicate_of": None, "similarity": 0.0, "reason": "new"}


def _merge_other_clusters(db: Database, target_id: int, opportunity_id: int) -> None:
    """If this opportunity belonged to a different cluster, move it over.

    Full cluster merging happens when two previously separate clusters are
    joined by a new common member; the cheaper common case is handled here.
    """
    row = db.query_one("SELECT cluster_id FROM opportunities WHERE id=?", (opportunity_id,))
    previous = row.get("cluster_id") if row else None
    if previous and int(previous) != int(target_id):
        db.execute(
            "UPDATE opportunities SET cluster_id=? WHERE cluster_id=?",
            (target_id, previous),
        )
        db.execute("DELETE FROM clusters WHERE id=?", (previous,))
        log.info("Merged cluster %s into %s via opportunity %s", previous, target_id, opportunity_id)
    db.execute(
        "UPDATE clusters SET updated_at=? WHERE id=?",
        (iso(now_utc()), target_id),
    )
    repo.refresh_cluster_stats(db, target_id)
