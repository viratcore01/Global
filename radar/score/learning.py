"""Learning loop: fit a small logistic model from Apply / Ignore / Scam labels."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, Sequence

from ..db import Database
from ..util import clamp, get_logger, iso, json_dumps, json_loads, now_utc

log = get_logger(__name__)

FEATURES = ["embed", "bm25", "trust", "urgency", "completeness"]
MIN_LABELS = 100
MIN_PER_CLASS = 10
MODEL_SETTING = "score_model"


def label_target(action: str) -> float | None:
    action = (action or "").lower()
    if action in {"apply", "applied", "booked", "replied"}:
        return 1.0
    if action in {"ignore", "ignored", "scam"}:
        return 0.0
    return None


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def fit_logistic(
    rows: Sequence[dict[str, Any]],
    *,
    min_labels: int = MIN_LABELS,
    min_per_class: int = MIN_PER_CLASS,
    iterations: int = 600,
    learning_rate: float = 0.35,
    l2: float = 0.01,
) -> dict[str, Any] | None:
    samples: list[tuple[list[float], float]] = []
    for row in rows:
        target = label_target(str(row.get("action") or ""))
        if target is None:
            continue
        features = row.get("features") or json_loads(row.get("features_json"), {}) or {}
        try:
            vector = [clamp(float(features.get(name, 0.0) or 0.0)) for name in FEATURES]
        except (TypeError, ValueError):
            continue
        samples.append((vector, target))
    positives = sum(1 for _, target in samples if target > 0.5)
    negatives = len(samples) - positives
    if len(samples) < min_labels or positives < min_per_class or negatives < min_per_class:
        return None

    # Balance the classes so a heavy Apply bias does not produce a constant
    # predictor.
    pos_weight = len(samples) / (2.0 * positives)
    neg_weight = len(samples) / (2.0 * negatives)
    weights = [0.0] * len(FEATURES)
    intercept = 0.0
    n = float(len(samples))
    for _ in range(iterations):
        grad_w = [0.0] * len(FEATURES)
        grad_b = 0.0
        for vector, target in samples:
            z = intercept + sum(w * x for w, x in zip(weights, vector))
            pred = _sigmoid(z)
            weight = pos_weight if target > 0.5 else neg_weight
            error = (pred - target) * weight
            grad_b += error
            for i, value in enumerate(vector):
                grad_w[i] += error * value
        intercept -= learning_rate * (grad_b / n)
        for i in range(len(weights)):
            weights[i] -= learning_rate * ((grad_w[i] / n) + l2 * weights[i])
        if any(math.isnan(w) or math.isinf(w) for w in weights) or math.isnan(intercept):
            return None

    model = {
        "version": 1,
        "trained_at": iso(now_utc()),
        "n": len(samples),
        "positives": positives,
        "negatives": negatives,
        "features": FEATURES,
        "weights": [round(w, 6) for w in weights],
        "intercept": round(intercept, 6),
        "method": "logistic_regression_gradient_descent",
    }
    # A modest sanity check: the model should distinguish the two classes.
    pos_scores = [_sigmoid(intercept + sum(w * x for w, x in zip(weights, v))) for v, y in samples if y > 0.5]
    neg_scores = [_sigmoid(intercept + sum(w * x for w, x in zip(weights, v))) for v, y in samples if y <= 0.5]
    model["train_mean_apply"] = round(sum(pos_scores) / max(1, len(pos_scores)), 4)
    model["train_mean_reject"] = round(sum(neg_scores) / max(1, len(neg_scores)), 4)
    if model["train_mean_apply"] <= model["train_mean_reject"]:
        log.warning("Refit did not separate classes; keeping the fixed weighted model")
        return None
    return model


def refit(db: Database) -> dict[str, Any]:
    rows = []
    for row in _training_rows(db):
        row = dict(row)
        if not row.get("features"):
            row["features"] = json_loads(row.get("features_json"), {}) or {}
        rows.append(row)
    model = fit_logistic(rows)
    if model is None:
        return {
            "fitted": False,
            "labels": len(rows),
            "reason": f"need at least {MIN_LABELS} labels and {MIN_PER_CLASS} of each class",
        }
    db.set_json_setting(MODEL_SETTING, model)
    log.info("Fitted score model on %d labels", model["n"])
    return {"fitted": True, "model": model}


def _training_rows(db: Database) -> list[dict[str, Any]]:
    from .. import repo

    return repo.training_rows(db)


def load_model(db: Database) -> dict[str, Any] | None:
    model = db.get_json_setting(MODEL_SETTING)
    if not isinstance(model, dict):
        return None
    if model.get("features") != FEATURES or not isinstance(model.get("weights"), list):
        return None
    if len(model["weights"]) != len(FEATURES):
        return None
    return model


def model_score(model: dict[str, Any], features: dict[str, float]) -> float:
    vector = [clamp(float(features.get(name, 0.0) or 0.0)) for name in FEATURES]
    z = float(model.get("intercept", 0.0)) + sum(
        float(w) * x for w, x in zip(model.get("weights") or [], vector)
    )
    return _sigmoid(z) * 100.0


def clear_model(db: Database) -> None:
    db.set_setting(MODEL_SETTING, "")
