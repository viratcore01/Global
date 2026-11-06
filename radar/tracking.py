"""Opportunity status state machine and label capture."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import repo
from .db import Database
from .models import ActionPayload
from .util import get_logger, json_loads, now_utc

log = get_logger(__name__)

ALLOWED: dict[str, set[str]] = {
    "new": {"applied", "ignored", "scam", "archived"},
    "reviewed": {"applied", "ignored", "scam", "archived"},
    "applied": {"replied", "booked", "ignored", "scam", "archived"},
    "replied": {"booked", "ignored", "archived"},
    "booked": {"archived"},
    "ignored": {"applied", "archived"},
    "scam": {"archived"},
    "expired": {"archived"},
    "archived": set(),
}

ACTION_STATUS = {
    "apply": "applied",
    "ignore": "ignored",
    "scam": "scam",
    "replied": "replied",
    "booked": "booked",
    "archive": "archived",
}

STATUS_LABEL = {
    "applied": "apply",
    "ignored": "ignore",
    "scam": "scam",
    "replied": "replied",
    "booked": "booked",
    "archived": "archive",
}


@dataclass
class TrackingResult:
    ok: bool
    opportunity_id: int
    previous_status: str = ""
    new_status: str = ""
    action: str = ""
    message: str = ""
    reputation_added: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "opportunity_id": self.opportunity_id,
            "previous_status": self.previous_status,
            "new_status": self.new_status,
            "action": self.action,
            "message": self.message,
            "reputation_added": self.reputation_added,
        }


def apply_action(db: Database, payload: dict[str, Any] | ActionPayload, *, note: str | None = None) -> TrackingResult:
    try:
        model = payload if isinstance(payload, ActionPayload) else ActionPayload.model_validate(payload)
    except Exception as exc:
        return TrackingResult(ok=False, opportunity_id=0, message=f"invalid action: {exc}")
    opportunity = repo.get_opportunity(db, model.id)
    if not opportunity:
        return TrackingResult(ok=False, opportunity_id=model.id, message="opportunity not found")
    previous = str(opportunity.get("status") or "new")
    action = model.action

    if action == "undo":
        return _undo(db, opportunity, previous)

    target = ACTION_STATUS.get(action)
    if not target:
        return TrackingResult(ok=False, opportunity_id=model.id, message=f"unknown action {action!r}")
    if previous == target:
        return TrackingResult(
            ok=True,
            opportunity_id=model.id,
            previous_status=previous,
            new_status=target,
            action=action,
            message="already in that state",
        )
    if target not in ALLOWED.get(previous, set()):
        return TrackingResult(
            ok=False,
            opportunity_id=model.id,
            previous_status=previous,
            new_status=previous,
            action=action,
            message=f"cannot transition {previous} -> {target}",
        )

    breakdown = opportunity.get("score_breakdown") or {}
    features = dict(breakdown.get("features") or {})
    features["risk_flags"] = opportunity.get("risk_flags") or []
    features["risk_penalty"] = breakdown.get("risk_penalty", 0)
    repo.insert_label(
        db,
        opportunity_id=model.id,
        action=STATUS_LABEL.get(target, action),
        previous_status=previous,
        new_status=target,
        score=float(opportunity.get("score") or 0.0),
        features=features,
        note=model.note or note,
    )
    repo.set_status(db, model.id, target)
    if action in {"apply", "ignore", "scam", "replied", "booked"}:
        repo.apply_source_label(db, opportunity.get("source_id"), action)
    if action in {"apply", "ignore", "scam"}:
        repo.record_alert_response(db, model.id, action)

    reputation_added = 0
    if action == "scam":
        for kind, value in repo.values_from_opportunity(opportunity):
            if kind not in {"email", "phone", "domain", "contact"}:
                continue
            if repo.add_reputation(
                db,
                kind=kind,
                value=value,
                reason="user marked scam",
                opportunity_id=model.id,
            ):
                reputation_added += 1
        log.warning("Scam marked for opportunity %s; added %d reputation entries", model.id, reputation_added)

    return TrackingResult(
        ok=True,
        opportunity_id=model.id,
        previous_status=previous,
        new_status=target,
        action=action,
        message=f"{previous} -> {target}",
        reputation_added=reputation_added,
    )


def _undo(db: Database, opportunity: dict[str, Any], current: str) -> TrackingResult:
    rows = db.query(
        "SELECT * FROM labels WHERE opportunity_id=? AND action <> 'undo' ORDER BY id DESC LIMIT 1",
        (int(opportunity["id"]),),
    )
    if not rows:
        return TrackingResult(ok=False, opportunity_id=int(opportunity["id"]), message="nothing to undo")
    last = rows[0]
    target = str(last.get("previous_status") or "new")
    if target not in ALLOWED and target != "new":
        target = "new"
    repo.set_status(db, int(opportunity["id"]), target)
    repo.insert_label(
        db,
        opportunity_id=int(opportunity["id"]),
        action="undo",
        previous_status=current,
        new_status=target,
        score=float(opportunity.get("score") or 0.0),
        features={"undone_label_id": last.get("id")},
        note="undo",
    )
    return TrackingResult(
        ok=True,
        opportunity_id=int(opportunity["id"]),
        previous_status=current,
        new_status=target,
        action="undo",
        message=f"{current} -> {target}",
    )
