"""End-to-end: fixture -> raw store -> extract -> dedup -> score -> alert -> label."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from radar import repo
from radar.alert.actions import verify_action
from radar.alert.ntfy import PublishResult
from radar.alert.routing import AlertRouter, format_alert
from radar.config import SourceConfig
from radar.tracking import apply_action
from radar.util import IST

FIXTURES = Path(__file__).resolve().parent / "fixtures"

NOON_IST = datetime(2026, 11, 5, 12, 0, tzinfo=IST)
NIGHT_IST = datetime(2026, 11, 5, 3, 0, tzinfo=IST)


def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeNtfy:
    def __init__(self) -> None:
        self.published: list[dict] = []

    def publish(self, **kwargs) -> PublishResult:
        self.published.append(kwargs)
        return PublishResult(ok=True, message_id=f"msg-{len(self.published)}", status=200)


def add_source(db) -> dict:
    source_id = repo.add_source(
        db,
        SourceConfig(name="Fixture source", type="web", url="https://fixtures.example", tier=2, enabled=False),
    )
    source = repo.get_source(db, source_id)
    assert source is not None
    return source


def test_fixture_pipeline_clusters_scores_and_dedups(config, db, pipeline) -> None:
    source = add_source(db)
    first = pipeline.ingest(
        "fixture://demo/mumbai-1",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html"), "title": "Casting: Supporting Actress for Mumbai Web Series"},
    )
    assert first.records, first.error
    first_id = first.records[0].opportunity_id

    second = pipeline.ingest(
        "fixture://demo/mumbai-repost",
        source,
        kind="page",
        payload={
            "title": "Casting: Supporting Actress for Mumbai Web Series",
            "text": (
                "Casting: Supporting Actress for Mumbai Web Series. Blue Lantern Pictures is casting "
                "a supporting actress, age 22-30, Hindi and English. Location: Mumbai. "
                "Pay: Rs 15,000 per day. Deadline: 15 December 2026. "
                "Send your self tape to casting@bluelantern.example. No nudity."
            ),
        },
    )
    assert second.records, second.error
    second_id = second.records[0].opportunity_id
    assert second_id != first_id

    stored_first = repo.get_opportunity(db, first_id)
    stored_second = repo.get_opportunity(db, second_id)
    assert stored_first["cluster_id"] == stored_second["cluster_id"]
    cluster_id = int(stored_first["cluster_id"])
    cluster = repo.get_cluster(db, cluster_id)
    assert int(cluster["source_count"]) >= 1
    assert {int(member["id"]) for member in repo.cluster_members(db, cluster_id)} == {first_id, second_id}

    assert stored_first["score"] > 0
    assert stored_first["canonical_url"].startswith("fixture://demo/mumbai-1")
    assert stored_first["raw_document_id"] is not None

    raw = repo.get_raw_document(db, int(stored_first["raw_document_id"]))
    replayed = pipeline.replay_raw(int(raw["id"]))
    assert replayed.records
    assert repo.count_opportunities(db)["total"] == 2


def test_digest_sends_once_and_records_alerts(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/digest",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    assert result.records
    config.settings.ntfy_topic = "test-topic"
    config.settings.ntfy_server = "https://ntfy.example"
    config.settings.digest_max_items = 10
    client = FakeNtfy()
    router = AlertRouter(config, db, client=client)
    first = router.send_digest()
    assert first["sent"] is True
    assert first["count"] >= 1
    assert client.published
    assert repo.alert_history(db, limit=10)
    second = router.send_digest()
    assert second["sent"] is False
    assert second["reason"] == "no digest candidates"


def test_digest_ends_with_dashboard_link(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/digest-link",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    assert result.records
    config.settings.ntfy_topic = "test-topic"
    config.settings.ntfy_server = "https://ntfy.example"
    config.settings.dashboard_url = "https://radar.example"
    client = FakeNtfy()
    router = AlertRouter(config, db, client=client)
    sent = router.send_digest()
    assert sent["sent"] is True
    message = client.published[0]["message"]
    assert "Dashboard: https://radar.example" in message


def test_instant_threshold_env_override_reaches_routing(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/override",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    opportunity_id = result.records[0].opportunity_id
    config.settings.ntfy_topic = "test-topic"
    config.settings.ntfy_server = "https://ntfy.example"
    repo.update_score(
        db,
        opportunity_id,
        92.0,
        {"features": {"completeness": 0.9, "embed": 0.8, "bm25": 0.7, "trust": 0.8, "urgency": 0.6}},
    )
    opportunity = repo.get_opportunity(db, opportunity_id)
    config.settings.instant_threshold = 95.0
    router = AlertRouter(config, db, client=FakeNtfy())
    assert router.consider(opportunity, now=NOON_IST).action == "digest"
    config.settings.instant_threshold = 90.0
    decision = router.consider(opportunity, now=NOON_IST)
    assert decision.action == "sent", decision.reason


def test_instant_alert_requires_threshold_completeness_and_clean_risk(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/instant",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    opportunity_id = result.records[0].opportunity_id
    config.settings.ntfy_topic = "test-topic"
    config.settings.ntfy_server = "https://ntfy.example"
    config.settings.reply_secret = "secret"
    config.settings.ntfy_reply_topic = "reply-topic"
    repo.update_score(
        db,
        opportunity_id,
        92.0,
        {"features": {"completeness": 0.9, "embed": 0.8, "bm25": 0.7, "trust": 0.8, "urgency": 0.6}},
    )
    opportunity = repo.get_opportunity(db, opportunity_id)
    assert opportunity["risk_flags"] == []
    client = FakeNtfy()
    router = AlertRouter(config, db, client=client)
    decision = router.consider(opportunity, now=NOON_IST)
    assert decision.action == "sent", decision.reason
    assert repo.alert_exists(db, f"instant:{opportunity_id}")
    actions = client.published[0]["actions"]
    assert [action["label"] for action in actions] == ["Apply", "Ignore", "Scam"]
    import json

    payload = json.loads(actions[0]["body"])
    ok, reason = verify_action("secret", payload)
    assert ok, reason

    title, body = format_alert(opportunity, db=db)
    assert "Safety" in body
    assert "Mumbai" in body


def test_instant_alert_is_deferred_during_quiet_hours(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/quiet",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    opportunity_id = result.records[0].opportunity_id
    config.settings.ntfy_topic = "test-topic"
    config.settings.ntfy_server = "https://ntfy.example"
    config.settings.quiet_start = 23
    config.settings.quiet_end = 7
    repo.update_score(
        db,
        opportunity_id,
        92.0,
        {"features": {"completeness": 0.9, "embed": 0.8, "bm25": 0.7, "trust": 0.8, "urgency": 0.6}},
    )
    opportunity = repo.get_opportunity(db, opportunity_id)
    assert opportunity["risk_flags"] == []
    client = FakeNtfy()
    router = AlertRouter(config, db, client=client)
    decision = router.consider(opportunity, now=NIGHT_IST)
    assert decision.action == "digest"
    assert decision.reason == "quiet hours"
    assert client.published == []
    assert not repo.alert_exists(db, f"instant:{opportunity_id}")


def test_status_state_machine_and_labels(config, db, pipeline) -> None:
    source = add_source(db)
    result = pipeline.ingest(
        "fixture://demo/label",
        source,
        kind="page",
        payload={"html": read_fixture("jsonld_casting.html")},
    )
    opportunity_id = result.records[0].opportunity_id
    applied = apply_action(db, {"id": opportunity_id, "action": "apply", "note": "sent self tape"})
    assert applied.ok, applied.message
    assert applied.new_status == "applied"
    replied = apply_action(db, {"id": opportunity_id, "action": "replied"})
    assert replied.ok and replied.new_status == "replied"
    invalid = apply_action(db, {"id": opportunity_id, "action": "undo"})
    assert invalid.ok
    booked = apply_action(db, {"id": opportunity_id, "action": "booked"})
    assert booked.ok and booked.new_status == "booked"
    assert apply_action(db, {"id": opportunity_id, "action": "apply"}).ok is False
    labels = repo.list_labels(db, limit=10)
    assert len(labels) >= 2


def test_jobs_cycle_offline(config, db, pipeline) -> None:
    from radar import jobs

    seeded = jobs.seed_demo(config, db, pipeline, file=str(FIXTURES / "demo_items.json"))
    assert seeded["ok"], seeded
    assert seeded["ingested"] >= 6
    cycle = jobs.run_cycle(config, db, pipeline, force_discovery=False)
    assert cycle["discovery"]["ok"] is True
    assert cycle["ingest"]["processed"] >= 0
    # New records are already scored during ingest; the score job reports how
    # many needed rescor­ing, which may legitimately be zero.
    assert "scored" in cycle["score"]
    health = jobs.job_health(config, db)
    assert health["integrity"] == "ok"
    assert health["opportunities"]["total"] >= 6
