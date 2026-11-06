"""Stage 1: deterministic eligibility gate.

Eligibility is a filter, never a score.  Unknown values pass: the record is
allowed to reach ranking so a human can inspect it.  Only a positive conflict
(known age range that cannot overlap, a known location beyond the travel radius,
a nudity level above the profile limit, etc.) rejects a role.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..cities import distance_between
from ..config import Profile
from ..normalize import normalize_ws


@dataclass
class EligibilityResult:
    eligible: bool
    reasons: list[str] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"eligible": self.eligible, "reasons": self.reasons, "checks": self.checks}


def _norm_lang(value: str) -> str:
    value = normalize_ws(value).lower()
    aliases = {
        "hindi": "hindi",
        "hi": "hindi",
        "english": "english",
        "en": "english",
        "hinglish": "hinglish",
        "urdu": "urdu",
        "marathi": "marathi",
        "tamil": "tamil",
        "telugu": "telugu",
        "kannada": "kannada",
        "malayalam": "malayalam",
        "bengali": "bengali",
        "bangla": "bengali",
        "gujarati": "gujarati",
        "punjabi": "punjabi",
        "odia": "odia",
        "assamese": "assamese",
    }
    return aliases.get(value, value)


def _language_overlap(required: list[str], known: list[str]) -> bool:
    required_set = {_norm_lang(x) for x in required if x}
    known_set = {_norm_lang(x) for x in known if x}
    if not required_set or not known_set:
        return True
    if required_set & known_set:
        return True
    # Hinglish performances are usually accessible to Hindi or English
    # speakers, and vice versa.
    if "hinglish" in required_set and ({"hindi", "english"} & known_set):
        return True
    if "hinglish" in known_set and ({"hindi", "english"} & required_set):
        return True
    return False


def _profile_coords(profile: Profile, city: str) -> tuple[float, float] | None:
    custom = profile.city_coords.get(city.strip().lower())
    if custom:
        return (custom[0], custom[1])
    from ..cities import coords_for

    return coords_for(city)


def evaluate_eligibility(opportunity: dict[str, Any] | Any, profile: Profile) -> EligibilityResult:
    def get(key: str, default: Any = None) -> Any:
        if isinstance(opportunity, dict):
            return opportunity.get(key, default)
        return getattr(opportunity, key, default)

    reasons: list[str] = []
    checks: dict[str, Any] = {}
    op_type = str(get("opportunity_type", "other") or "other").lower()

    # Age overlap ----------------------------------------------------------
    age_min = get("age_min")
    age_max = get("age_max")
    p_min, p_max = profile.age_range
    age_ok = True
    if age_min is not None and age_max is not None:
        age_ok = int(age_min) <= p_max and int(age_max) >= p_min
    elif age_min is not None:
        age_ok = int(age_min) <= p_max
    elif age_max is not None:
        age_ok = int(age_max) >= p_min
    checks["age"] = {"ok": age_ok, "role": [age_min, age_max], "profile": [p_min, p_max]}
    if not age_ok:
        reasons.append(f"age {age_min or '?'}-{age_max or '?'} does not overlap profile {p_min}-{p_max}")

    # Gender ---------------------------------------------------------------
    role_gender = str(get("gender_presentation", "any") or "any").lower()
    gender_ok = role_gender in {"any", "unknown", ""} or profile.gender_presentation in {"any", "unknown", ""}
    if not gender_ok:
        gender_ok = role_gender == profile.gender_presentation
    checks["gender"] = {"ok": gender_ok, "role": role_gender, "profile": profile.gender_presentation}
    if not gender_ok:
        reasons.append(f"role requires {role_gender}; profile is {profile.gender_presentation}")

    # Language -------------------------------------------------------------
    languages = get("languages") or []
    if isinstance(languages, str):
        languages = [languages]
    language_ok = _language_overlap([str(x) for x in languages], profile.languages)
    checks["language"] = {"ok": language_ok, "role": languages, "profile": profile.languages}
    if not language_ok:
        reasons.append(f"language requirement {languages} not covered by profile {profile.languages}")

    # Location / remote / self-tape ---------------------------------------
    city = get("city")
    country = get("country")
    remote = bool(get("remote_ok")) or (
        profile.remote_selftape_ok
        and str(get("audition_format", "unknown")).lower() in {"self_tape", "zoom"}
    )
    location_ok = True
    distance = None
    location_reason = ""
    if remote:
        location_ok = True
        location_reason = "remote/self-tape bypass"
    elif city:
        base_candidates = [profile.base_city, *profile.preferred_cities]
        known_city = normalize_ws(str(city)).lower()
        # A preferred city is explicitly acceptable regardless of radius.
        if any(normalize_ws(c).lower() == known_city for c in profile.preferred_cities):
            location_ok = True
            location_reason = "preferred city"
        elif profile.travel_radius_km is None:
            location_ok = True
            location_reason = "unlimited travel radius"
        else:
            point = _profile_coords(profile, known_city)
            for base in base_candidates:
                base_point = _profile_coords(profile, base)
                if not point or not base_point:
                    continue
                d = distance_km(point, base_point)
                if distance is None or d < distance:
                    distance = d
            if distance is None:
                # Unknown city coordinates: do not reject.  It is a scoping
                # uncertainty, not a factual ineligibility.
                location_ok = True
                location_reason = "unknown city coordinates; allowed for manual review"
            else:
                location_ok = distance <= float(profile.travel_radius_km)
                location_reason = f"{distance:.0f} km from nearest base city"
    elif country:
        same_country = normalize_ws(str(country)).lower() == profile.base_country.lower()
        location_ok = same_country or bool(get("remote_ok"))
        location_reason = "country match" if same_country else "outside base country and not remote"
    checks["location"] = {
        "ok": location_ok,
        "city": city,
        "country": country,
        "remote": remote,
        "distance_km": round(distance, 1) if distance is not None else None,
        "reason": location_reason,
    }
    if not location_ok:
        reasons.append(
            f"location {city or country or 'unknown'} is outside travel radius"
            + (f" ({distance:.0f} km)" if distance is not None else "")
        )

    # Nudity ---------------------------------------------------------------
    nudity_order = {"none": 0, "implied": 1, "topless": 2, "full": 3}
    role_nudity = str(get("nudity", "unknown") or "unknown").lower()
    nudity_ok = role_nudity == "unknown" or nudity_order.get(role_nudity, 0) <= profile.nudity_limit()
    checks["nudity"] = {"ok": nudity_ok, "role": role_nudity, "profile_max": profile.nudity_max}
    if not nudity_ok:
        reasons.append(f"nudity '{role_nudity}' exceeds profile limit '{profile.nudity_max}'")

    # Paid-only and festival fees -----------------------------------------
    comp = str(get("comp_type", "unknown") or "unknown").lower()
    paid_ok = not profile.paid_only or comp not in {"unpaid", "credit_only", "deferred"}
    checks["paid"] = {"ok": paid_ok, "comp_type": comp, "paid_only": profile.paid_only}
    if not paid_ok:
        reasons.append(f"unpaid work ({comp}) excluded by paid_only profile")

    fee_required = bool(get("fee_required"))
    fee_ok = True
    if fee_required:
        if profile.fee_tolerance == "never":
            fee_ok = False
            reasons.append("entry/application fee required but profile fee_tolerance is 'never'")
        elif profile.fee_tolerance == "festivals_only" and op_type != "festival":
            fee_ok = False
            reasons.append("fee required for a non-festival opportunity")
    checks["fee"] = {"ok": fee_ok, "required": fee_required, "tolerance": profile.fee_tolerance}

    return EligibilityResult(eligible=not reasons, reasons=reasons, checks=checks)


def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    from ..cities import haversine_km

    return haversine_km(a, b)
