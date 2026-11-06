"""Best-effort RDAP domain age lookup with a 30-day SQLite cache.

RDAP is public, free and usually does not require a key.  Many registrars redact
creation dates; failures are cached as ``unavailable`` and never block scoring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import Settings
from ..db import Database
from ..normalize import registrable_domain
from ..repo import domain_intel_get, domain_intel_put
from ..util import get_logger, iso, now_utc, parse_dt

log = get_logger(__name__)


def _extract_age_days(payload: dict[str, Any], now: datetime) -> float | None:
    events = payload.get("events") or []
    created = None
    for event in events:
        action = str(event.get("eventAction") or "").lower()
        if action in {"registration", "registered", "created"}:
            created = parse_dt(event.get("eventDate"))
            if created:
                break
    if created is None:
        return None
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created).total_seconds() / 86400.0)


def _registrar(payload: dict[str, Any]) -> str | None:
    for entity in payload.get("entities") or []:
        roles = [str(role).lower() for role in entity.get("roles") or []]
        if "registrar" not in roles:
            continue
        vcard = entity.get("vcardArray") or []
        if isinstance(vcard, list) and len(vcard) > 1:
            for item in vcard[1]:
                if isinstance(item, list) and len(item) >= 4 and item[0] == "fn":
                    return str(item[3])
        if entity.get("handle"):
            return str(entity["handle"])
    return None


def lookup_domain(domain: str, *, timeout: float = 7.0) -> dict[str, Any]:
    domain = registrable_domain(domain) or domain
    if not domain:
        return {"status": "invalid", "age_days": None, "registrar": None, "raw": {}}
    url = f"https://rdap.org/domain/{domain}"
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "GlobalCinemaRadar/1.0 (domain age check)"},
        ) as client:
            response = client.get(url)
            if response.status_code != 200:
                return {
                    "status": f"http_{response.status_code}",
                    "age_days": None,
                    "registrar": None,
                    "raw": {},
                }
            payload = response.json()
    except Exception as exc:
        log.debug("RDAP lookup failed for %s: %s", domain, exc)
        return {"status": "unavailable", "age_days": None, "registrar": None, "raw": {}}

    now = now_utc()
    age_days = _extract_age_days(payload, now)
    status = "ok" if age_days is not None else "redacted"
    return {
        "status": status,
        "age_days": age_days,
        "registrar": _registrar(payload),
        "raw": payload if isinstance(payload, dict) else {},
        "checked_at": iso(now),
    }


def get_domain_intel(db: Database, domain: str, *, settings: Settings | None = None) -> dict[str, Any] | None:
    domain = registrable_domain(domain) or domain
    if not domain:
        return None
    cached = domain_intel_get(db, domain)
    if cached:
        return cached
    timeout = getattr(settings, "rdap_timeout_seconds", 7.0) if settings else 7.0
    result = lookup_domain(domain, timeout=timeout)
    domain_intel_put(
        db,
        domain=domain,
        status=result["status"],
        age_days=result.get("age_days"),
        registrar=result.get("registrar"),
        raw=result.get("raw"),
        ttl_days=30,
    )
    return domain_intel_get(db, domain) or {
        "domain": domain,
        **result,
    }
