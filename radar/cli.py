"""Command line interface for the Global Cinema Opportunity Radar.

Every command is a thin wrapper around :mod:`radar.jobs` / the pipeline so the
same code paths run from cron, systemd, Docker and a human shell.

Examples
--------
    python -m radar init --demo
    python -m radar doctor
    python -m radar run --force --digest
    python -m radar opportunities --min-score 60 --limit 20
    python -m radar label 42 apply
    python -m radar backup verify data/backups/radar-20260101-030000.db
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from . import __version__, jobs, repo
from .backup import list_backups, restore as restore_backup, snapshot as snapshot_backup, verify as verify_backup
from .config import AppConfig, Paths
from .db import Database, DatabaseError
from .health import doctor as doctor_check
from .learning import metrics as quality_metrics, recall_audit, run_weekly
from .pipeline import Pipeline
from .score.learning import clear_model, refit as refit_model
from .tracking import apply_action
from .util import configure_logging, get_logger, humanize_deadline, iso, json_dumps, now_utc, parse_dt

log = get_logger(__name__)

STATUSES = ("new", "reviewed", "applied", "replied", "booked", "ignored", "scam", "expired", "archived")
LABEL_ACTIONS = ("apply", "ignore", "scam", "replied", "booked", "archive", "undo")
# Past-tense spellings are a common typo; accept them and map to the canonical verb.
LABEL_ALIASES = {"applied": "apply", "ignored": "ignore", "replies": "replied", "archived": "archive"}


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


class Output:
    """Tiny output shim: JSON for machines, readable tables for humans."""

    def __init__(self, as_json: bool = False):
        self.as_json = as_json

    def raw(self, payload: Any, text: str | Callable[[], str] | None = None) -> None:
        if self.as_json:
            print(json_dumps(payload, pretty=True))
            return
        if callable(text):
            text = text()
        if text is not None:
            print(text)
        else:
            print(_humanize(payload))

    def table(self, rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]], *, empty: str = "No rows.") -> None:
        if self.as_json:
            print(json_dumps(list(rows), pretty=True))
            return
        if not rows:
            print(empty)
            return
        print_table(rows, columns)


def _humanize(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_humanize(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, (list, tuple)):
        if not value:
            return f"{pad}[]"
        return "\n".join(_humanize(item, indent) for item in value)
    return f"{pad}{_scalar(value)}"


def _scalar(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.2f}"
    if isinstance(value, (dict, list, tuple)):
        return json_dumps(value)
    text = str(value).replace("\n", " ")
    return text if len(text) <= 160 else text[:157] + "..."


def print_table(rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]], *, max_width: int = 48) -> None:
    headers = [label for label, _ in columns]
    body: list[list[str]] = []
    for row in rows:
        cells = []
        for _, key in columns:
            text = _scalar(row.get(key) if isinstance(row, dict) else None)
            cells.append(text if len(text) <= max_width else text[: max_width - 3] + "...")
        body.append(cells)
    widths = [len(header) for header in headers]
    for cells in body:
        for index, cell in enumerate(cells):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for cells in body:
        print("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(cells)))


# ---------------------------------------------------------------------------
# Runtime bootstrap
# ---------------------------------------------------------------------------


class Runtime:
    """Config + database + pipeline, created on demand and closed reliably."""

    def __init__(self, *, allow_llm: bool = False, need_pipeline: bool = False):
        self.config = AppConfig.load()
        configure_logging(self.config.settings.log_level, self.config.paths.log_dir)
        self.db = Database(self.config.paths.db_path)
        self.pipeline = Pipeline(self.config, self.db, allow_llm=allow_llm) if need_pipeline else None

    def close(self) -> None:
        if self.pipeline is not None:
            self.pipeline.close()
        self.db.close()

    def __enter__(self) -> "Runtime":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-j", "--json", action="store_true", help="machine-readable JSON output")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, out: Output) -> int:
    paths = Paths.discover()
    paths.ensure()
    config = AppConfig.load(paths)
    configure_logging(config.settings.log_level, paths.log_dir)
    db = Database(paths.db_path)
    try:
        synced = jobs.sync_sources(config, db)
        secret_created = False
        if not config.settings.reply_secret:
            config.settings.generate_reply_secret()
            secret_created = True
        db.set_setting("initialised_at", iso(now_utc()))
        payload: dict[str, Any] = {
            "ok": True,
            "root": str(paths.root),
            "db": str(paths.db_path),
            "sources": synced,
            "reply_secret_created": secret_created,
            "schema_version": db.scalar("SELECT COALESCE(MAX(version),0) FROM schema_migrations", default=0),
        }
        if args.demo:
            pipeline = Pipeline(config, db)
            try:
                payload["demo"] = jobs.seed_demo(config, db, pipeline)
            finally:
                pipeline.close()
        next_steps = [
            "1. Edit config/profile.yaml with your real age, city, languages and role types.",
            "2. Add hand-vetted sources to config/sources.yaml.",
            "3. Copy .env.example to .env and fill NTFY_TOPIC / NTFY_REPLY_TOPIC.",
            "4. Run: python -m radar doctor",
            "5. Run: python -m radar run --force",
        ]
        payload["next_steps"] = next_steps
        demo_line: list[str] = []
        demo = payload.get("demo")
        if isinstance(demo, dict):
            if demo.get("ok"):
                demo_line = [f"Demo: {demo.get('ingested', 0)} record(s) loaded, {demo.get('skipped', 0)} skipped"]
            else:
                demo_line = [f"Demo: not loaded ({demo.get('error') or '; '.join(demo.get('errors') or [])})"]
        text = "\n".join(
            [
                f"Radar initialised at {paths.root}",
                f"Database: {paths.db_path}",
                f"Sources synced: {synced.get('inserted', 0)} added, {synced.get('updated', 0)} updated, "
                f"{len(config.sources)} configured",
                "Reply secret: " + ("created at data/reply_secret" if secret_created else "already present (or set in .env)"),
                *demo_line,
                "",
                *next_steps,
            ]
        )
        out.raw(payload, text)
        return 0
    finally:
        db.close()


def cmd_doctor(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        result = doctor_check(rt.config, rt.db)
        if out.as_json:
            out.raw(result)
        else:
            lines = []
            for check in result["checks"]:
                mark = "ok  " if check["ok"] else ("WARN" if check["severity"] == "warning" else "FAIL")
                lines.append(f"[{mark}] {check['name']}: {check['detail']}")
            lines.append("")
            lines.append(f"Failed: {result['failed']}  Warnings: {result['warnings']}")
            out.raw(result, "\n".join(lines))
        return 0 if result["ok"] else 1


def cmd_config(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        summary = json.loads(rt.config.dump_summary())
        summary["profile_text"] = rt.config.profile.to_text()
        summary["search_terms"] = rt.config.profile.search_terms()
        out.raw(summary)
        return 0


def cmd_sources(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        if args.action == "enable":
            source = repo.get_source_by_name(rt.db, args.name)
            if not source:
                out.raw({"ok": False, "error": f"source not found: {args.name}"})
                return 1
            repo.set_source_enabled(rt.db, int(source["id"]), True)
            out.raw({"ok": True, "source": source["name"], "enabled": True}, f"Enabled {source['name']}")
            return 0
        if args.action == "disable":
            source = repo.get_source_by_name(rt.db, args.name)
            if not source:
                out.raw({"ok": False, "error": f"source not found: {args.name}"})
                return 1
            repo.set_source_enabled(rt.db, int(source["id"]), False)
            out.raw({"ok": True, "source": source["name"], "enabled": False}, f"Disabled {source['name']}")
            return 0
        if args.action == "sync":
            synced = jobs.sync_sources(rt.config, rt.db)
            out.raw({"ok": True, "synced": synced})
            return 0
        rows = repo.list_sources(rt.db)
        out.table(
            rows,
            [("id", "id"), ("name", "name"), ("type", "type"), ("tier", "tier"), ("enabled", "enabled"), ("interval", "poll_interval_minutes"), ("url", "url")],
            empty="No sources configured. Add them to config/sources.yaml then run `python -m radar sources sync`.",
        )
        return 0


def cmd_discover(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True) as rt:
        result = jobs.job_discover(
            rt.config,
            rt.db,
            rt.pipeline,
            force=args.force,
            source_name=args.source,
            limit=args.limit,
        )
        if out.as_json:
            out.raw(result)
        else:
            rows = result.get("sources") or []
            print(f"Queued {result.get('queued', 0)} URL(s) from {len(rows)} source(s).")
            out.table(rows, [("source", "source"), ("type", "type"), ("queued", "queued"), ("error", "error")])
        return 0 if result.get("ok") else 1


def cmd_ingest(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True, allow_llm=args.llm) as rt:
        if args.url:
            result = rt.pipeline.ingest(args.url, kind="page")
            payload = result.to_dict()
            if not out.as_json:
                for record in payload.get("records") or []:
                    score = (record.get("score") or {}).get("score")
                    print(f"[{record['opportunity_id']}] {record.get('title')}  score={_scalar(score)}")
                if payload.get("skipped"):
                    print(f"Skipped: {payload.get('reason')}")
                if payload.get("error"):
                    print(f"Error: {payload.get('error')}")
                if not payload.get("records") and not payload.get("error") and not payload.get("skipped"):
                    print("No records extracted.")
            else:
                out.raw(payload)
            return 0 if (payload.get("records") or payload.get("skipped")) and not payload.get("error") else 1
        stats = jobs.job_ingest(rt.config, rt.db, rt.pipeline, limit=args.limit)
        out.raw(stats, _dict_lines(stats))
        return 0


def cmd_run(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True, allow_llm=args.llm) as rt:
        result = jobs.run_cycle(
            rt.config,
            rt.db,
            rt.pipeline,
            force_discovery=args.force,
            digest=args.digest,
            limit_sources=args.limit_sources,
            queue_limit=args.queue_limit,
        )
        if out.as_json:
            out.raw(result)
            return 0
        discovery = result["discovery"]
        ingest = result["ingest"]
        score = result["score"]
        alerts = result["alerts"]
        print(
            f"Discovery: {discovery.get('queued', 0)} queued from {len(discovery.get('sources') or [])} source(s)"
        )
        print(
            f"Ingest: {ingest.get('processed', 0)} processed, {ingest.get('failed', 0)} failed, "
            f"{ingest.get('pending', 0)} retry, {ingest.get('records', 0)} record(s)"
        )
        print(
            f"Score: {score.get('scored', 0)} scored, {score.get('vetoed', 0)} vetoed, "
            f"{score.get('ineligible', 0)} ineligible"
        )
        print(f"Alerts: {alerts.get('sent', 0)} instant, {alerts.get('digest_held', 0)} held for digest")
        if result.get("digest"):
            digest = result["digest"]
            print(f"Digest: sent={digest.get('sent')} reason={digest.get('reason')} items={len(digest.get('items') or [])}")
        for entry in discovery.get("sources") or []:
            if entry.get("error"):
                print(f"  ! {entry.get('source')}: {entry.get('error')}")
        return 0


def cmd_worker(args: argparse.Namespace, out: Output) -> int:
    from .worker import Worker

    with Runtime(need_pipeline=True, allow_llm=args.llm) as rt:
        worker = Worker(rt.config, rt.db, pipeline=rt.pipeline, poll_seconds=args.poll_seconds)
        if args.once:
            recovery = worker.recover()
            result = worker.run_once(force=args.force or None)
            payload = {"recovery": recovery, **result}
            out.raw(payload, _humanize(payload))
            return 0
        print("Starting worker (Ctrl+C to stop). Auxiliary listeners follow configuration.")
        worker.run_forever(replies=not args.no_replies, imap=not args.no_imap)
        return 0


def cmd_score(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True, allow_llm=args.llm) as rt:
        if args.id:
            opportunity = repo.get_opportunity(rt.db, args.id)
            if not opportunity:
                out.raw({"ok": False, "error": f"opportunity {args.id} not found"})
                return 1
            result = rt.pipeline.ranker.score(opportunity)
            out.raw(result.to_dict())
            return 0
        results = rt.pipeline.ranker.score_pending(limit=args.limit, rescore=args.rescore)
        payload = [result.to_dict() for result in results]
        if out.as_json:
            out.raw(payload)
            return 0
        print(f"Scored {len(payload)} opportunity(ies).")
        out.table(
            payload,
            [("id", "opportunity_id"), ("score", "score"), ("vetoed", "vetoed"), ("eligible", "eligible"), ("reason", "veto_reason")],
        )
        return 0


def cmd_digest(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True) as rt:
        result = jobs.job_digest(rt.config, rt.db, dry_run=args.dry_run)
        if out.as_json:
            out.raw(result)
            return 0
        items = result.get("items") or []
        # A real send reports ``count`` (the items were stored as alerts, not
        # returned), while dry runs and individual sends report ``items``.
        count = result.get("count", len(items) if items else 0)
        print(f"Digest: sent={result.get('sent')} reason={result.get('reason')} items={count}")
        if result.get("quarantine"):
            print(f"Quarantine waiting: {result['quarantine']}")
        for item in items:
            if isinstance(item, dict) and "body" in item:
                print()
                print(f"[{item.get('score')}] {item.get('title')}")
                print(item.get("body"))
            elif isinstance(item, dict):
                print(f"- {_scalar(item.get('title'))} (score {_scalar(item.get('score'))})")
        if result.get("reason") == "ntfy_not_configured":
            print("Set NTFY_SERVER / NTFY_TOPIC in .env to send digests.")
        return 0


def cmd_alerts(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        rows = repo.alert_history(rt.db, limit=args.limit)
        out.table(
            rows,
            [("id", "id"), ("sent", "sent_at"), ("kind", "kind"), ("score", "score"), ("response", "response"), ("title", "title")],
            empty="No alerts sent yet.",
        )
        return 0


def cmd_opportunities(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        if args.q:
            rows = repo.list_opportunities(rt.db, query=args.q, limit=args.limit, order=args.order)
        else:
            rows = repo.list_opportunities(
                rt.db,
                status=args.status,
                min_score=args.min_score,
                city=args.city,
                risk_only=args.risk_only,
                limit=args.limit,
                offset=args.offset,
                order=args.order,
            )
        if out.as_json:
            out.raw(rows)
            return 0
        view = [
            {
                **row,
                "deadline_text": humanize_deadline(parse_dt(row.get("deadline"))),
            }
            for row in rows
        ]
        out.table(
            view,
            [
                ("id", "id"),
                ("score", "score"),
                ("status", "status"),
                ("title", "title"),
                ("city", "city"),
                ("deadline", "deadline_text"),
                ("pay", "comp_type"),
            ],
            empty="No opportunities yet. Try `python -m radar seed-demo` or add sources.",
        )
        return 0


def cmd_show(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        opportunity = repo.get_opportunity(rt.db, args.id)
        if not opportunity:
            out.raw({"ok": False, "error": f"opportunity {args.id} not found"})
            return 1
        payload: dict[str, Any] = {"opportunity": opportunity}
        if opportunity.get("cluster_id"):
            payload["cluster"] = repo.get_cluster(rt.db, int(opportunity["cluster_id"]))
            payload["cluster_members"] = repo.cluster_members(rt.db, int(opportunity["cluster_id"]))
        if out.as_json:
            out.raw(payload)
            return 0
        print(f"#{opportunity['id']}  {opportunity.get('title')}")
        print(f"Score: {_scalar(opportunity.get('score'))}/100   Status: {opportunity.get('status')}")
        print(f"Deadline: {humanize_deadline(parse_dt(opportunity.get('deadline')))}")
        print(f"Location: {_scalar(opportunity.get('city'))}, {_scalar(opportunity.get('country'))}")
        print(f"Pay: {_scalar(opportunity.get('comp_type'))} {_scalar(opportunity.get('pay_amount'))} {_scalar(opportunity.get('currency'))}")
        print(f"URL: {opportunity.get('canonical_url')}")
        if opportunity.get("contact_value"):
            print(f"Contact: {opportunity.get('contact_value')}")
        if opportunity.get("risk_flags"):
            print(f"Risk: {', '.join(str(flag) for flag in opportunity['risk_flags'])}")
        description = (opportunity.get("description") or "").strip()
        if description:
            print()
            print(description[:3000])
        breakdown = opportunity.get("score_breakdown") or {}
        if breakdown:
            print()
            print("Score breakdown:")
            print(_humanize(breakdown))
        if payload.get("cluster_members"):
            print()
            print("Also seen on:")
            for member in payload["cluster_members"]:
                if int(member.get("id") or 0) != int(opportunity["id"]):
                    print(f"  - {member.get('canonical_url')}")
        return 0


def cmd_brief(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        from .brief import brief_for_id

        brief = brief_for_id(rt.db, rt.config.profile, args.id)
        if brief is None:
            out.raw({"ok": False, "error": f"opportunity {args.id} not found"})
            return 1
        if out.as_json:
            out.raw(brief.to_dict())
            return 0
        print(brief.render())
        return 0


def cmd_label(args: argparse.Namespace, out: Output) -> int:
    action = args.action.lower()
    action = LABEL_ALIASES.get(action, action)
    if action not in LABEL_ACTIONS:
        out.raw({"ok": False, "error": f"unknown action {action!r}; choose from {', '.join(LABEL_ACTIONS)}"})
        return 2
    with Runtime() as rt:
        payload = {"id": args.id, "action": action, "note": args.note}
        result = apply_action(rt.db, payload)
        out.raw(result.to_dict(), result.message or ("ok" if result.ok else "failed"))
        return 0 if result.ok else 1


def cmd_replay(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True, allow_llm=args.llm) as rt:
        if args.queue_id:
            item = repo.get_queue_item(rt.db, args.queue_id)
            if not item:
                out.raw({"ok": False, "error": f"queue item {args.queue_id} not found"})
                return 1
            repo.replay_queue(rt.db, args.queue_id)
            stats = rt.pipeline.process_queue(limit=1)
            out.raw({"ok": True, "queue_id": args.queue_id, "stats": stats})
            return 0
        result = rt.pipeline.replay_raw(args.raw_id)
        payload = result.to_dict()
        if out.as_json:
            out.raw(payload)
        else:
            print(f"Replayed raw document {args.raw_id}: {len(payload.get('records') or [])} record(s)")
            if payload.get("error"):
                print(f"Error: {payload['error']}")
        return 0 if payload.get("records") else 1


def cmd_quarantine(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        if args.action == "resolve":
            existing = rt.db.query_one("SELECT id FROM quarantine WHERE id=?", (args.id,))
            if not existing:
                out.raw({"ok": False, "error": f"quarantine record {args.id} not found"})
                return 1
            repo.resolve_quarantine(rt.db, args.id, args.resolution or "fixed")
            out.raw({"ok": True, "id": args.id, "resolution": args.resolution})
            return 0
        rows = repo.list_quarantine(rt.db, open_only=not args.all, limit=args.limit)
        view = []
        for row in rows:
            errors = row.get("errors") if isinstance(row.get("errors"), list) else []
            view.append(
                {
                    **row,
                    "error_text": "; ".join(str(error) for error in errors[:3]),
                }
            )
        out.table(
            view,
            [("id", "id"), ("created", "created_at"), ("url", "url"), ("errors", "error_text")],
            empty="Quarantine is empty.",
        )
        return 0


def cmd_health(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:  # noqa: F841 - config paths are used inside snapshot
        from .health import snapshot

        result = snapshot(rt.config, rt.db)
        if out.as_json:
            out.raw(result)
            return 0
        counts = result["opportunities"]
        print(f"Database: {result['db_bytes'] / 1024:.0f} KiB  integrity={result['integrity']}")
        print(f"Disk free: {result['disk_free_bytes'] / (1024 ** 3):.1f} GiB")
        print("Opportunities: " + ", ".join(f"{key}={value}" for key, value in counts.items()))
        print("Queue: " + ", ".join(f"{key}={value}" for key, value in result["queue"].items()))
        print(f"Alerts: {result['alerts']}   Quarantine open: {result['quarantine_open']}")
        print(f"Labels: {result['labels']}")
        warnings = result.get("source_warnings") or []
        if warnings:
            print()
            print("Source warnings:")
            for warning in warnings:
                print(f"  [{warning['severity']}] {warning['name']}: {warning['message']}")
        jobs_rows = result.get("jobs") or []
        if jobs_rows:
            print()
            print("Last jobs:")
            out.table(jobs_rows[:12], [("job", "name"), ("status", "status"), ("last", "last_run_at"), ("error", "last_error")])
        return 0 if result["integrity"] == "ok" else 1


def cmd_metrics(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        result = quality_metrics(rt.db, days=args.days)
        if out.as_json:
            out.raw(result)
            return 0
        alerts = result["alerts"]
        responses = result["responses"]
        print(f"Window: last {args.days} day(s)")
        print(f"New opportunities: {result['new_opportunities']}")
        print(f"Alerts: instant={alerts['instant']} digest={alerts['digest']} unresolved48h={alerts['unresolved_48h']}")
        print(f"Responses: apply={responses['apply']} ignore={responses['ignore']} scam={responses['scam']}")
        print(f"Precision@15: {_scalar(result['precision_at_15'])}   Outcome rate: {_scalar(result['outcome_rate'])}")
        print(f"Time to alert (h): {_scalar(result['time_to_alert_hours'])}")
        print(f"Vetoes: {result['vetoes']}   Quarantine new: {result['quarantine_new']}")
        print(f"Labels: {result['labels']}")
        return 0


def cmd_audit(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        result = recall_audit(rt.db, sample=args.sample, days=args.days, low_cutoff=rt.config.digest_cutoff)
        if out.as_json:
            out.raw(result)
            return 0
        print(result["instructions"])
        print()
        print("Vetoed sample:")
        out.table(result["vetoed"], [("id", "opportunity_id"), ("score", "score"), ("rule", "rule_id"), ("title", "title"), ("reason", "reason")])
        print()
        print("Low-ranked, never alerted:")
        out.table(result["low_ranked"], [("id", "opportunity_id"), ("score", "score"), ("city", "city"), ("title", "title")])
        return 0


def cmd_weekly(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        result = run_weekly(rt.config, rt.db, days=args.days, force=args.force)
        out.raw(result)
        return 0


def cmd_refit(args: argparse.Namespace, out: Output) -> int:
    with Runtime() as rt:
        if args.clear:
            clear_model(rt.db)
            out.raw({"ok": True, "cleared": True}, "Cleared the learned model; deterministic weights are active.")
            return 0
        result = refit_model(rt.db)
        out.raw(result)
        return 0


def cmd_backup(args: argparse.Namespace, out: Output) -> int:
    # verify/restore must not hold an open database handle: Windows refuses to
    # move a file that is open, and a restore is only meaningful on a clean file.
    if args.action in {"verify", "restore"}:
        config = AppConfig.load()
        configure_logging(config.settings.log_level, config.paths.log_dir)
        if args.action == "verify":
            backups = list_backups(config)
            target = args.path or (backups[0]["path"] if backups else "")
            if not target:
                out.raw({"ok": False, "error": "no backup found; run `python -m radar backup snapshot`"})
                return 1
            result = verify_backup(target)
            out.raw(result)
            return 0 if result.get("ok") else 1
        if not args.path:
            out.raw({"ok": False, "error": "restore requires a snapshot path"})
            return 2
        result = restore_backup(config, args.path, force=args.force)
        out.raw(result)
        return 0 if result.get("ok") else 1
    with Runtime() as rt:
        if args.action == "list":
            rows = list_backups(rt.config)
            out.table(rows, [("name", "name"), ("bytes", "bytes"), ("modified", "modified")], empty="No backups yet.")
            return 0
        result = snapshot_backup(rt.config, rt.db)
        if out.as_json:
            out.raw(result)
        else:
            print(f"Snapshot: {result['path']} ({result['bytes'] / 1024:.0f} KiB)")
            if result.get("removed"):
                print(f"Pruned {len(result['removed'])} old snapshot(s)")
        return 0


def cmd_seed_demo(args: argparse.Namespace, out: Output) -> int:
    with Runtime(need_pipeline=True) as rt:
        result = jobs.seed_demo(rt.config, rt.db, rt.pipeline, file=args.file)
        if out.as_json:
            out.raw(result)
            return 0 if result.get("ok") else 1
        print(f"Ingested {result.get('ingested', 0)} record(s) from {result.get('file')}")
        if result.get("skipped"):
            print(f"Skipped {result['skipped']} item(s) (no opportunity signals)")
        if result.get("error"):
            print(f"  ! {result['error']}")
        for error in result.get("errors") or []:
            print(f"  ! {error}")
        if result.get("ok"):
            print("Next: `python -m radar opportunities` or `python -m radar digest --dry-run`.")
        return 0 if result.get("ok") else 1


def cmd_reply(args: argparse.Namespace, out: Output) -> int:
    from .alert.ntfy import NtfyClient
    from .alert.reply_listener import listen_forever, poll_once

    with Runtime() as rt:
        if not rt.config.settings.reply_configured:
            out.raw(
                {"ok": False, "error": "NTFY_REPLY_TOPIC is not configured"},
                "Set NTFY_REPLY_TOPIC in .env (and RADAR_REPLY_SECRET) to use the reply listener.",
            )
            return 1
        if args.once:
            client = NtfyClient(rt.config.settings)
            results = poll_once(client, rt.db, rt.config, timeout=args.timeout)
            payload = [result.to_dict() for result in results]
            out.raw(payload, f"Processed {len(payload)} reply event(s).")
            return 0
        print("Listening for button replies on the private ntfy topic (Ctrl+C to stop).")
        listen_forever(rt.config, rt.db)
        return 0


def cmd_imap(args: argparse.Namespace, out: Output) -> int:
    from .discovery.email_idle import listen_forever, poll_email_sources

    with Runtime(need_pipeline=True) as rt:
        if not rt.config.settings.imap_enabled:
            out.raw(
                {"ok": False, "error": "IMAP_ENABLED is false"},
                "Set IMAP_ENABLED=true plus IMAP_HOST/IMAP_USER/IMAP_PASSWORD in .env.",
            )
            return 1
        if args.once:
            results = poll_email_sources(rt.config, rt.db, fetcher=rt.pipeline.fetcher)
            out.raw(results, f"Processed {len(results)} mailbox(es).")
            return 0
        print("Listening with IMAP IDLE (Ctrl+C to stop).")
        listen_forever(rt.config, rt.db, fetcher=rt.pipeline.fetcher)
        return 0


def cmd_serve(args: argparse.Namespace, out: Output) -> int:
    root = Paths.discover().root
    # RADAR_HOME may point at a data-only or config-only tree, so fall back to
    # the dashboard shipped next to the installed package before giving up.
    candidates = [
        root / "dashboard" / "app.py",
        Path(__file__).resolve().parent.parent / "dashboard" / "app.py",
    ]
    app = next((path for path in candidates if path.exists()), candidates[0])
    if not app.exists():
        out.raw(
            {"ok": False, "error": f"dashboard not found: {app}"},
            "Run this from the project checkout, or set RADAR_HOME to it.",
        )
        return 1
    try:
        import streamlit  # noqa: F401
    except ImportError:
        out.raw(
            {"ok": False, "error": "streamlit is not installed"},
            "Install the dashboard extra: pip install -r requirements-optional.txt",
        )
        return 1
    port = str(args.port or AppConfig.load().settings.dashboard_port)
    env = dict(os.environ)
    env.setdefault("RADAR_HOME", str(root))
    # Headless mode stops Streamlit's first-run "Email:" prompt from blocking
    # (and killing) the process when it is launched from a script or service.
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    cmd = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(app),
        "--server.port",
        port,
        "--server.address",
        args.address,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]
    print(f"Dashboard: http://{args.address}:{port}")
    return subprocess.call(cmd, env=env)


def cmd_version(_args: argparse.Namespace, out: Output) -> int:
    out.raw({"version": __version__}, f"Global Cinema Opportunity Radar {__version__}")
    return 0


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _dict_lines(payload: dict[str, Any]) -> str:
    return "\n".join(f"{key}: {_scalar(value)}" for key, value in payload.items() if key != "decisions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="Global Cinema Opportunity Radar - discovery, filtering and tracking for acting work.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    init = sub.add_parser("init", help="create directories, database and reply secret")
    init.add_argument("--demo", action="store_true", help="also load the demo fixture corpus")
    _add_common(init)
    init.set_defaults(func=cmd_init)

    doctor = sub.add_parser("doctor", help="validate configuration, database and optional extras")
    _add_common(doctor)
    doctor.set_defaults(func=cmd_doctor)

    config = sub.add_parser("config", help="print the effective configuration")
    _add_common(config)
    config.set_defaults(func=cmd_config)

    sources = sub.add_parser("sources", help="list, enable, disable or sync sources")
    sources.add_argument("action", nargs="?", default="list", choices=["list", "enable", "disable", "sync"])
    sources.add_argument("name", nargs="?", default="", help="source name for enable/disable")
    _add_common(sources)
    sources.set_defaults(func=cmd_sources)

    discover = sub.add_parser("discover", help="run discovery adapters and queue URLs")
    discover.add_argument("--force", action="store_true", help="ignore poll intervals")
    discover.add_argument("--source", help="only this configured source name")
    discover.add_argument("--limit", type=int, help="maximum number of sources")
    _add_common(discover)
    discover.set_defaults(func=cmd_discover)

    ingest = sub.add_parser("ingest", help="process the discovery queue (or one URL)")
    ingest.add_argument("url", nargs="?", help="ingest a single URL directly")
    ingest.add_argument("--limit", type=int, default=25)
    ingest.add_argument("--llm", action="store_true", help="allow LLM gap-filling for this run")
    _add_common(ingest)
    ingest.set_defaults(func=cmd_ingest)

    run = sub.add_parser("run", help="one full cycle: discover, ingest, score, alert")
    run.add_argument("--force", action="store_true", help="discover from every enabled source")
    run.add_argument("--digest", action="store_true", help="send the digest at the end of the cycle")
    run.add_argument("--limit-sources", type=int)
    run.add_argument("--queue-limit", type=int, default=40)
    run.add_argument("--llm", action="store_true")
    _add_common(run)
    run.set_defaults(func=cmd_run)

    worker = sub.add_parser("worker", help="long-running scheduler with reply and IMAP listeners")
    worker.add_argument("--once", action="store_true", help="run due jobs a single time")
    worker.add_argument("--poll-seconds", type=int, default=30)
    worker.add_argument("--force", action="append", help="force a job name (repeatable)")
    worker.add_argument("--no-replies", action="store_true")
    worker.add_argument("--no-imap", action="store_true")
    worker.add_argument("--llm", action="store_true")
    _add_common(worker)
    worker.set_defaults(func=cmd_worker)

    score = sub.add_parser("score", help="score pending opportunities")
    score.add_argument("id", nargs="?", type=int, help="score a single opportunity id")
    score.add_argument("--limit", type=int, default=200)
    score.add_argument("--rescore", action="store_true", help="rescore already-scored items")
    score.add_argument("--llm", action="store_true")
    _add_common(score)
    score.set_defaults(func=cmd_score)

    digest = sub.add_parser("digest", help="send (or preview) the daily digest")
    digest.add_argument("--dry-run", action="store_true")
    _add_common(digest)
    digest.set_defaults(func=cmd_digest)

    alerts = sub.add_parser("alerts", help="alert history")
    alerts.add_argument("--limit", type=int, default=50)
    _add_common(alerts)
    alerts.set_defaults(func=cmd_alerts)

    opportunities = sub.add_parser("opportunities", help="list or search stored opportunities")
    opportunities.add_argument("--status", choices=STATUSES)
    opportunities.add_argument("--min-score", type=float)
    opportunities.add_argument("--city")
    opportunities.add_argument("--risk-only", action="store_true")
    opportunities.add_argument("--q", help="full-text search (FTS5)")
    opportunities.add_argument("--order", default="score", choices=["score", "deadline", "newest"])
    opportunities.add_argument("--limit", type=int, default=25)
    opportunities.add_argument("--offset", type=int, default=0)
    _add_common(opportunities)
    opportunities.set_defaults(func=cmd_opportunities)

    show = sub.add_parser("show", help="full detail for one opportunity, including cluster sources")
    show.add_argument("id", type=int)
    _add_common(show)
    show.set_defaults(func=cmd_show)

    brief = sub.add_parser("brief", help="turn an opportunity into an application checklist")
    brief.add_argument("id", type=int)
    _add_common(brief)
    brief.set_defaults(func=cmd_brief)

    label = sub.add_parser("label", help="record apply / ignore / scam / replied / booked / archive / undo")
    label.add_argument("id", type=int)
    label.add_argument("action", choices=(*LABEL_ACTIONS, *LABEL_ALIASES))
    label.add_argument("--note")
    _add_common(label)
    label.set_defaults(func=cmd_label)

    replay = sub.add_parser("replay", help="re-extract a stored raw document or queue item")
    group = replay.add_mutually_exclusive_group(required=True)
    group.add_argument("--raw-id", type=int)
    group.add_argument("--queue-id", type=int)
    replay.add_argument("--llm", action="store_true")
    _add_common(replay)
    replay.set_defaults(func=cmd_replay)

    quarantine = sub.add_parser("quarantine", help="inspect or resolve quarantined extractions")
    quarantine.add_argument("action", nargs="?", default="list", choices=["list", "resolve"])
    quarantine.add_argument("id", nargs="?", type=int)
    quarantine.add_argument("--resolution", default="fixed")
    quarantine.add_argument("--all", action="store_true", help="include resolved items")
    quarantine.add_argument("--limit", type=int, default=50)
    _add_common(quarantine)
    quarantine.set_defaults(func=cmd_quarantine)

    health = sub.add_parser("health", help="database, queue, source and job health")
    _add_common(health)
    health.set_defaults(func=cmd_health)

    metrics = sub.add_parser("metrics", help="precision, outcomes and scam metrics")
    metrics.add_argument("--days", type=int, default=7)
    _add_common(metrics)
    metrics.set_defaults(func=cmd_metrics)

    audit = sub.add_parser("audit", help="sample vetoed and low-ranked items for recall review")
    audit.add_argument("--sample", type=int, default=10)
    audit.add_argument("--days", type=int, default=7)
    _add_common(audit)
    audit.set_defaults(func=cmd_audit)

    weekly = sub.add_parser("weekly", help="generate the weekly report and refit the model")
    weekly.add_argument("--days", type=int, default=7)
    weekly.add_argument("--force", action="store_true")
    _add_common(weekly)
    weekly.set_defaults(func=cmd_weekly)

    refit = sub.add_parser("refit", help="refit the logistic model from labels")
    refit.add_argument("--clear", action="store_true", help="delete the learned model")
    _add_common(refit)
    refit.set_defaults(func=cmd_refit)

    backup = sub.add_parser("backup", help="snapshot, list, verify or restore the database")
    backup.add_argument("action", nargs="?", default="snapshot", choices=["snapshot", "list", "verify", "restore"])
    backup.add_argument("path", nargs="?", help="snapshot path for verify/restore")
    backup.add_argument("--force", action="store_true", help="restore over an existing database")
    _add_common(backup)
    backup.set_defaults(func=cmd_backup)

    seed = sub.add_parser("seed-demo", help="load local demo fixtures (no network)")
    seed.add_argument("--file", help="path to a demo_items.json fixture")
    _add_common(seed)
    seed.set_defaults(func=cmd_seed_demo)

    reply = sub.add_parser("reply", help="poll ntfy button replies (no inbound port)")
    reply.add_argument("--once", action="store_true")
    reply.add_argument("--timeout", type=float, default=10.0)
    _add_common(reply)
    reply.set_defaults(func=cmd_reply)

    imap = sub.add_parser("imap", help="poll or IDLE the configured mailbox")
    imap.add_argument("--once", action="store_true")
    _add_common(imap)
    imap.set_defaults(func=cmd_imap)

    serve = sub.add_parser("serve", help="launch the read-only Streamlit dashboard")
    serve.add_argument("--port", type=int)
    serve.add_argument("--address", default="127.0.0.1")
    _add_common(serve)
    serve.set_defaults(func=cmd_serve)

    version = sub.add_parser("version", help="print the version")
    _add_common(version)
    version.set_defaults(func=cmd_version)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    out = Output(bool(getattr(args, "json", False)))
    try:
        return int(args.func(args, out) or 0)
    except DatabaseError as exc:
        print(f"database error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, FileNotFoundError, PermissionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
