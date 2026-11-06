"""Near-duplicate detection and clustering."""

from __future__ import annotations

from radar import repo
from radar.db import Database
from radar.dedup import assign_cluster
from radar.dedup.minhash import estimate_jaccard, signature_for_text, text_similarity
from radar.dedup.structured import fuzzy_match, make_structured_key


def insert(db: Database, **fields):
    data = {
        "canonical_url": fields.pop("url"),
        "title": fields.pop("title", "Casting call"),
        "description": fields.pop("description", ""),
        **fields,
    }
    opportunity_id, is_new = repo.upsert_opportunity(db, data)
    assert is_new
    return opportunity_id, data


def test_minhash_near_duplicate_detection() -> None:
    left = "Casting a supporting actress for a Hindi web series shooting in Mumbai in January."
    right = "Casting a supporting actress for a Hindi web series shooting in Mumbai in January 2027."
    unrelated = "Pune short film festival call for entries with a submission fee."
    assert estimate_jaccard(signature_for_text(left), signature_for_text(right)) > 0.7
    assert estimate_jaccard(signature_for_text(left), signature_for_text(unrelated)) < 0.2
    assert text_similarity(left, right) > 0.7


def test_structured_key_ignores_boilerplate() -> None:
    first = make_structured_key(
        title="Casting call: Supporting actress required",
        city="Mumbai",
        deadline="2026-12-15T00:00:00+05:30",
        contact="casting@example.com",
    )
    second = make_structured_key(
        title="Supporting actress - casting",
        city="Mumbai",
        deadline="2026-12-15",
        contact="Casting@Example.com",
    )
    assert first == second


def test_fuzzy_match_requires_shared_facts() -> None:
    base = {"title": "Lead actress for Delhi feature", "city": "Delhi", "deadline": "2026-12-01", "contact_value": ""}
    similar = {"title": "Lead actress Delhi feature film", "city": "Delhi", "deadline": "2026-12-01", "contact_value": ""}
    different = {"title": "Voiceover artist required", "city": "Mumbai", "deadline": "2027-01-20", "contact_value": ""}
    assert fuzzy_match(base, similar)
    assert not fuzzy_match(base, different)


def test_identical_calls_share_one_cluster(db: Database) -> None:
    first_id, first = insert(
        db,
        url="https://a.example/jobs/1",
        title="Casting: Supporting Actress for Mumbai Web Series",
        description="Casting a supporting actress in Mumbai. Deadline 15 December 2026.",
        city="Mumbai",
        deadline="2026-12-15T00:00:00+05:30",
        contact_value="casting@example.com",
    )
    first_cluster = assign_cluster(db, {**first, "id": first_id}, opportunity_id=first_id)
    assert first_cluster["reason"] == "new"

    second_id, second = insert(
        db,
        url="https://b.example/repost/2",
        title="Casting: Supporting Actress for Mumbai Web Series",
        description="Repost: casting a supporting actress in Mumbai. Deadline 15 December 2026.",
        city="Mumbai",
        deadline="2026-12-15T00:00:00+05:30",
        contact_value="casting@example.com",
    )
    second_cluster = assign_cluster(db, {**second, "id": second_id}, opportunity_id=second_id)
    assert second_cluster["cluster_id"] == first_cluster["cluster_id"]
    assert second_cluster["duplicate_of"] == first_id
    cluster = repo.get_cluster(db, first_cluster["cluster_id"])
    assert cluster is not None
    assert int(cluster["source_count"]) >= 1
    members = repo.cluster_members(db, first_cluster["cluster_id"])
    assert {int(member["id"]) for member in members} == {first_id, second_id}


def test_distinct_calls_get_distinct_clusters(db: Database) -> None:
    first_id, first = insert(
        db,
        url="https://a.example/jobs/10",
        title="Lead actor for Delhi indie feature",
        description="Lead actor, Delhi, February 2027.",
        city="Delhi",
        deadline="2026-11-30T00:00:00+05:30",
        contact_value="delhi@example.com",
    )
    second_id, second = insert(
        db,
        url="https://a.example/jobs/11",
        title="Voiceover artist for animated short",
        description="Voiceover artist, remote, animated short.",
        city="Mumbai",
        deadline="2026-12-20T00:00:00+05:30",
        contact_value="voice@example.com",
    )
    first_cluster = assign_cluster(db, {**first, "id": first_id}, opportunity_id=first_id)
    second_cluster = assign_cluster(db, {**second, "id": second_id}, opportunity_id=second_id)
    assert first_cluster["cluster_id"] != second_cluster["cluster_id"]
