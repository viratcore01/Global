"""Read-only Streamlit dashboard for the Global Cinema Opportunity Radar.

Launch with ``python -m radar serve`` (or ``streamlit run dashboard/app.py``).
The database is opened read-only, so the dashboard can never block the worker or
change a decision. Labeling stays in the CLI / ntfy buttons on purpose.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Streamlit Cloud runs this file as a script; ensure the repo root (which
# contains the `radar` package) is importable before local imports.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pandas as pd
import streamlit as st

from radar import repo
from radar.config import AppConfig, Paths
from radar.db import Database, DatabaseError
from radar.health import snapshot, source_warnings
from radar.learning import metrics as quality_metrics
from radar.util import humanize_deadline, parse_dt

st.set_page_config(
    page_title="Cinema Opportunity Radar",
    page_icon="🎬",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _full_width() -> dict[str, Any]:
    """``use_container_width`` is deprecated in favour of ``width="stretch"``.

    The pin allows Streamlit >= 1.35, so pick whichever the runtime supports
    instead of printing a deprecation warning on every render.
    """
    major_minor: list[int] = []
    for part in st.__version__.split(".")[:2]:
        digits = "".join(char for char in part if char.isdigit())
        major_minor.append(int(digits or 0))
    return {"width": "stretch"} if tuple(major_minor) >= (1, 49) else {"use_container_width": True}


FULL_WIDTH = _full_width()


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def open_runtime(db_path: str) -> tuple[AppConfig, Database]:
    """Open a fresh read-only connection for this render.

    Nothing caches the handle on purpose: while the worker writes from another
    process a long-lived read-only connection cannot follow the WAL, and a stale
    handle reported false "fts5: checksum mismatch" integrity errors. Opening
    and closing per rerun costs about a millisecond and always shows the truth.
    """
    paths = Paths.discover()
    config = AppConfig.load(paths)
    db = Database(Path(db_path), readonly=True, migrate=False)
    return config, db


def get_db() -> tuple[AppConfig, Database] | tuple[None, str]:
    """Open the database read-only, or return ``(None, message)``.

    The caller renders the message so the title always appears first.
    """
    config = AppConfig.load(Paths.discover())
    path = config.paths.db_path
    if not path.exists():
        return None, (
            f"Database not found at `{path}`. Run `python -m radar init` first "
            "(add `--demo` to load fixtures)."
        )
    try:
        return open_runtime(str(path))
    except (DatabaseError, OSError) as exc:
        return None, f"Could not open the database read-only: {exc}"


def as_frame(rows: list[dict[str, Any]], columns: dict[str, str] | None = None) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=list((columns or {}).values()))
    frame = pd.DataFrame(rows)
    if columns:
        for key in columns:
            if key not in frame.columns:
                frame[key] = None
        frame = frame[list(columns)]
        frame = frame.rename(columns=columns)
    # Blank/None/list values render as the literal "None" otherwise.
    for name in frame.columns:
        if frame[name].dtype == object:
            frame[name] = frame[name].map(cell)
    return frame


def money(value: Any, currency: Any) -> str:
    if value in (None, "", 0):
        return "—"
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{currency or 'INR'} {amount:,.0f}"


def risk_badge(flags: list[str] | None) -> str:
    return ", ".join(flags) if flags else "clear"


def cell(value: Any) -> str:
    """Render a scalar or JSON-ish field value for a dataframe cell."""
    if value in (None, "", []):
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False) if value else "—"
    return str(value)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def page_overview(config: AppConfig, db: Database) -> None:
    st.header("Overview")
    snap = snapshot(config, db)
    counts = snap["opportunities"]
    cols = st.columns(5)
    cols[0].metric("Opportunities", counts.get("total", 0))
    cols[1].metric("New", counts.get("new", 0))
    cols[2].metric("Applied", counts.get("applied", 0))
    cols[3].metric("Booked", counts.get("booked", 0))
    cols[4].metric("Scam", counts.get("scam", 0))

    cols = st.columns(4)
    cols[0].metric("Queue pending", snap["queue"].get("pending", 0))
    cols[1].metric("Alerts sent", snap["alerts"])
    cols[2].metric("Quarantine open", snap["quarantine_open"])
    cols[3].metric("DB size", f"{snap['db_bytes'] / 1024:.0f} KiB")

    st.caption(
        f"Generated {snap['generated_at']} · integrity **{snap['integrity']}** · "
        f"disk free {snap['disk_free_bytes'] / 1024**3:.1f} GiB · "
        f"embeddings {snap['embedding_coverage'].get('covered', 0)}/"
        f"{snap['embedding_coverage'].get('total', 0)}"
    )

    warnings = snap.get("source_warnings") or []
    if warnings:
        st.subheader("Source warnings")
        st.dataframe(
            as_frame(warnings, {"severity": "severity", "name": "source", "status": "status", "message": "message"}),
            **FULL_WIDTH,
            hide_index=True,
        )
    else:
        st.success("No source warnings.")

    st.subheader("Recent jobs")
    jobs = snap.get("jobs") or []
    if jobs:
        st.dataframe(
            as_frame(
                jobs,
                {
                    "name": "job",
                    "last_status": "status",
                    "last_run_at": "last run",
                    "run_count": "runs",
                    "last_error": "error",
                },
            ),
            **FULL_WIDTH,
            hide_index=True,
        )
    else:
        st.info("No job runs recorded yet. Start the worker (`python -m radar worker`).")


def page_opportunities(config: AppConfig, db: Database) -> None:
    st.header("Opportunities")
    with st.container():
        c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
        query = c1.text_input("Search (FTS5)", placeholder="casting Mumbai hindi")
        status = c2.selectbox("Status", ["any", "new", "reviewed", "applied", "replied", "booked", "ignored", "scam", "expired", "archived"])
        min_score = c3.slider("Minimum score", 0, 100, 0, 5)
        order = c4.selectbox("Order", ["score", "deadline", "newest"])

    c5, c6, c7 = st.columns([1, 1, 2])
    city = c5.text_input("City contains")
    risk_only = c6.checkbox("Risk flags only")
    limit = c7.slider("Rows", 25, 500, 100, 25)

    rows = repo.list_opportunities(
        db,
        status=None if status == "any" else status,
        min_score=min_score or None,
        city=city or None,
        query=query or None,
        risk_only=risk_only,
        limit=limit,
        order=order,
    )
    if not rows:
        st.info("Nothing matches. Try clearing filters or run `python -m radar run --force`.")
        return

    table = []
    for row in rows:
        table.append(
            {
                "id": row.get("id"),
                "score": round(float(row.get("score") or 0), 1),
                "title": row.get("title") or "(untitled)",
                "type": row.get("opportunity_type"),
                "city": row.get("city") or "—",
                "pay": money(row.get("pay_amount"), row.get("currency")),
                "deadline": humanize_deadline(parse_dt(row.get("deadline"))),
                "status": row.get("status"),
                "risk": risk_badge(row.get("risk_flags")),
                "source": row.get("source_name") or "—",
                "source_link": row.get("canonical_url"),
            }
        )
    frame = pd.DataFrame(table)
    if "source_link" in frame.columns:
        frame["source_link"] = frame["source_link"].replace({None: None, "": None})
    st.dataframe(
        frame,
        **FULL_WIDTH,
        hide_index=True,
        column_config={
            "source_link": st.column_config.LinkColumn(
                "source link", display_text="Open", help="Open the source in a new tab"
            ),
        },
    )
    st.caption(f"{len(table)} row(s). Use the Detail tab for one opportunity.")


def page_detail(config: AppConfig, db: Database) -> None:
    st.header("Opportunity detail")
    rows = repo.list_opportunities(db, limit=300, order="score")
    if not rows:
        st.info("No opportunities stored yet.")
        return
    labels = {f"#{row['id']} · {round(float(row.get('score') or 0), 1)} · {row.get('title') or '(untitled)'}": row["id"] for row in rows}
    choice = st.selectbox("Opportunity", list(labels))
    opportunity = repo.get_opportunity(db, int(labels[choice]))
    if not opportunity:
        st.warning("That opportunity no longer exists.")
        return

    left, right = st.columns([2, 1])
    with left:
        st.subheader(opportunity.get("title") or "(untitled)")
        st.markdown(
            f"**{opportunity.get('opportunity_type') or 'opportunity'}** · "
            f"{opportunity.get('city') or 'location unknown'} · "
            f"score **{round(float(opportunity.get('score') or 0), 1)}** · "
            f"status **{opportunity.get('status')}**"
        )
        st.write(opportunity.get("description") or "_No description extracted._")
        if opportunity.get("contact_value"):
            st.write(f"**Contact:** {opportunity['contact_value']}")
        if opportunity.get("canonical_url"):
            url = opportunity["canonical_url"]
            st.markdown(f"**Source:** [{url}]({url})")
    with right:
        st.metric("Pay", money(opportunity.get("pay_amount"), opportunity.get("currency")))
        st.write(f"**Pay type:** {opportunity.get('comp_type') or 'unknown'}")
        st.write(f"**Deadline:** {humanize_deadline(parse_dt(opportunity.get('deadline')))}")
        st.write(f"**Age:** {opportunity.get('age_min') or '?'}–{opportunity.get('age_max') or '?'}")
        st.write(f"**Gender:** {opportunity.get('gender_presentation') or 'any'}")
        st.write(f"**Languages:** {', '.join(opportunity.get('languages') or []) or '—'}")
        st.write(f"**Fee required:** {'yes' if opportunity.get('fee_required') else 'no'}")
        st.write(f"**Nudity:** {opportunity.get('nudity') or 'unknown'}")
        st.write(f"**Risk:** {risk_badge(opportunity.get('risk_flags'))}")

    st.subheader("Evidence")
    evidence = opportunity.get("evidence") or {}
    if evidence:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "field": key,
                        "extracted": cell(opportunity.get(key)),
                        "quote": (value.get("text") if isinstance(value, dict) else value) or "",
                        "method": (value.get("method") if isinstance(value, dict) else "") or "",
                    }
                    for key, value in evidence.items()
                ]
            ),
            **FULL_WIDTH,
            hide_index=True,
        )
    else:
        st.caption("No per-field evidence recorded.")

    st.subheader("Score breakdown")
    breakdown = opportunity.get("score_breakdown") or {}
    if breakdown:
        features = breakdown.get("features") or {}
        if features:
            st.bar_chart(pd.DataFrame({"feature": list(features), "value": list(features.values())}).set_index("feature"))
        with st.expander("Full breakdown (JSON)"):
            st.json(breakdown)
    else:
        st.caption("Not scored yet.")

    cluster_id = opportunity.get("cluster_id")
    if cluster_id:
        members = repo.cluster_members(db, int(cluster_id))
        if len(members) > 1:
            st.subheader(f"Cluster #{cluster_id} — {len(members)} source(s)")
            cluster_frame = as_frame(
                [
                    {
                        "id": m.get("id"),
                        "source": m.get("source_name"),
                        "score": m.get("score"),
                        "url": m.get("canonical_url"),
                        "first_seen": m.get("first_seen_at"),
                    }
                    for m in members
                ]
            )
            if "url" in cluster_frame.columns:
                cluster_frame["url"] = cluster_frame["url"].replace("—", None)
            st.dataframe(
                cluster_frame,
                **FULL_WIDTH,
                hide_index=True,
                column_config={
                    "url": st.column_config.LinkColumn("url", display_text="Open"),
                },
            )

    st.caption(
        f"Record it: `python -m radar label {opportunity['id']} apply` "
        f"(or ignore / scam / replied / booked)."
    )


def page_sources(config: AppConfig, db: Database) -> None:
    st.header("Sources")
    warnings = source_warnings(config, db)
    if warnings:
        st.warning(f"{len(warnings)} source warning(s) — see the Overview tab.")
    report = repo.health_report(db)
    if not report:
        st.info("No sources synced. Edit `config/sources.yaml`, then run `python -m radar sources sync`.")
        return
    table = []
    for row in report:
        table.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "type": row.get("type"),
                "tier": row.get("tier"),
                "enabled": bool(row.get("enabled")),
                "status": row.get("status") or "new",
                "last run": row.get("last_run_at") or "never",
                "failures": row.get("consecutive_failures"),
                "empty": row.get("consecutive_empty"),
                "trust": round(float(row.get("trust_score") or 0), 2),
                "applies": row.get("applies"),
                "scams": row.get("scams"),
                "error": (row.get("last_error") or "")[:120],
            }
        )
    st.dataframe(pd.DataFrame(table), **FULL_WIDTH, hide_index=True)
    st.caption("Enable/disable from the CLI: `python -m radar sources disable \"Name\"`.")


def page_quarantine(config: AppConfig, db: Database) -> None:
    st.header("Quarantine")
    st.caption("Extractions that failed validation. Nothing is deleted; replay the raw document after a fix.")
    rows = repo.list_quarantine(db, open_only=True, limit=200)
    if not rows:
        st.success("Quarantine is empty.")
        return
    quarantine_frame = as_frame(
        rows,
        {
            "id": "id",
            "created_at": "created",
            "source_name": "source",
            "url": "url",
            "errors": "errors",
            "raw_document_id": "raw id",
        },
    )
    if "url" in quarantine_frame.columns:
        quarantine_frame["url"] = quarantine_frame["url"].replace("—", None)
    st.dataframe(
        quarantine_frame,
        **FULL_WIDTH,
        hide_index=True,
        column_config={
            "url": st.column_config.LinkColumn("url", display_text="Open"),
        },
    )


def page_metrics(config: AppConfig, db: Database) -> None:
    st.header("Quality metrics")
    days = st.radio("Window", [7, 14, 30, 90], horizontal=True, index=0)
    metric = quality_metrics(db, days=days)
    cols = st.columns(4)
    precision = metric.get("precision_at_15")
    outcome = metric.get("outcome_rate")
    cols[0].metric("Precision@15", "—" if precision is None else f"{precision:.2f}")
    cols[1].metric("Outcome rate", "—" if outcome is None else f"{outcome:.2f}")
    cols[2].metric("New opportunities", metric.get("new_opportunities", 0))
    cols[3].metric("Vetoes", metric.get("vetoes", 0))

    cols = st.columns(4)
    cols[0].metric("Instant alerts", metric["alerts"].get("instant", 0))
    cols[1].metric("Digest alerts", metric["alerts"].get("digest", 0))
    cols[2].metric("Unresolved 48h", metric["alerts"].get("unresolved_48h", 0))
    cols[3].metric("Time to alert (h)", metric.get("time_to_alert_hours") or "—")

    labels = metric.get("labels") or {}
    if labels:
        st.subheader("Labels in window")
        st.bar_chart(pd.DataFrame({"action": list(labels), "count": list(labels.values())}).set_index("action"))

    if metric.get("manual_rejections") is not None:
        st.caption(f"Manual rejections: {metric['manual_rejections']} · scam labels: {metric.get('scam_labels', 0)} · new quarantine: {metric.get('quarantine_new', 0)}")

    st.subheader("Vetoes in window")
    vetoes = repo.list_vetoes(db, days=days, limit=100)
    if vetoes:
        st.dataframe(pd.DataFrame(vetoes), **FULL_WIDTH, hide_index=True)
    else:
        st.caption("No vetoes recorded in this window.")

    st.subheader("Weekly reports")
    reports = repo.weekly_reports(db, limit=8)
    if reports:
        st.dataframe(pd.DataFrame(reports), **FULL_WIDTH, hide_index=True)
    else:
        st.caption("No weekly report yet. `python -m radar weekly` creates one.")


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------

PAGES = {
    "Overview": page_overview,
    "Opportunities": page_opportunities,
    "Detail": page_detail,
    "Sources": page_sources,
    "Quarantine": page_quarantine,
    "Metrics": page_metrics,
}


def main() -> None:
    config, db = get_db()
    if config is None:
        st.title("🎬 Cinema Opportunity Radar")
        st.error(db)
        st.stop()

    try:
        with st.sidebar:
            st.title("🎬 Radar")
            page_name = st.radio("View", list(PAGES), label_visibility="collapsed")
            st.divider()
            st.caption(f"Profile: **{config.profile.name}**\n\n{config.profile.base_city}, {config.profile.base_country}")
            st.caption(f"DB: `{config.paths.db_path}`")
            if st.button("Refresh data"):
                st.rerun()
            st.divider()
            st.caption("Read-only. Label from the CLI or the ntfy buttons.")

        try:
            PAGES[page_name](config, db)
        except Exception as exc:  # never crash the whole dashboard for one page
            st.error(f"{page_name} failed: {exc}")
            with st.expander("Details"):
                st.exception(exc)
    finally:
        db.close()


main()
