"""Typed configuration: paths, environment settings, profile, sources, vetoes."""

from __future__ import annotations

import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .cities import CITY_COORDS
from .normalize import normalize_text
from .util import (
    IST,
    env_bool,
    env_float,
    env_int,
    get_logger,
    json_dumps,
)
from .yamlutil import load_yaml

log = get_logger(__name__)

VALID_SOURCE_TYPES = {
    "email",
    "rss",
    "atom",
    "web",
    "sitemap",
    "telegram",
    "searxng",
    "wayback",
    "discord",
    "google_alerts",
    "demo",
}
VALID_EXTRACTORS = {"auto", "css", "jsonld", "heuristic", "text"}
VALID_FETCH_STRATEGIES = {"http", "curl_cffi", "browser", "scrapling", "auto"}

DEFAULT_INTERVALS: dict[str, int] = {
    "email": 5,
    "rss": 30,
    "atom": 30,
    "web": 60,
    "sitemap": 360,
    "telegram": 15,
    "searxng": 360,
    "wayback": 1440,
    "discord": 5,
    "google_alerts": 30,
    "demo": 1440,
}

DEFAULT_WEIGHTS = {
    "embed": 0.35,
    "bm25": 0.25,
    "trust": 0.20,
    "urgency": 0.10,
    "completeness": 0.10,
}

DEFAULT_THRESHOLDS = {
    "instant": 80.0,
    "digest": 55.0,
    "min_completeness": 0.55,
    "exploration_min": 40.0,
    "exploration_max": 75.0,
}

NUDITY_ORDER = {"none": 0, "implied": 1, "topless": 2, "full": 3, "unknown": -1}


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Paths:
    root: Path
    config_dir: Path
    data_dir: Path
    db_path: Path
    backup_dir: Path
    log_dir: Path
    tmp_dir: Path
    exports_dir: Path
    migrations_dir: Path

    @classmethod
    def discover(cls) -> "Paths":
        env_root = os.getenv("RADAR_HOME", "").strip()
        candidates: list[Path] = []
        if env_root:
            candidates.append(Path(env_root).expanduser())
        candidates.extend([Path.cwd(), *Path.cwd().parents])
        # Package lives at <root>/radar; its parent is the project root.
        candidates.append(Path(__file__).resolve().parent.parent)

        root: Path | None = None
        for candidate in candidates:
            try:
                if (candidate / "config" / "profile.yaml").exists():
                    root = candidate
                    break
                if (candidate / "radar" / "config.py").exists() and (candidate / "config").is_dir():
                    root = candidate
                    break
            except OSError:
                continue
        if root is None:
            root = Path.cwd()

        config_dir = Path(os.getenv("RADAR_CONFIG_DIR", "config"))
        data_dir = Path(os.getenv("RADAR_DATA_DIR", "data"))
        if not config_dir.is_absolute():
            config_dir = root / config_dir
        if not data_dir.is_absolute():
            data_dir = root / data_dir

        db_path = Path(os.getenv("RADAR_DB", str(data_dir / "radar.db")))
        if not db_path.is_absolute():
            db_path = root / db_path

        return cls(
            root=root,
            config_dir=config_dir,
            data_dir=data_dir,
            db_path=db_path,
            backup_dir=data_dir / "backups",
            log_dir=data_dir / "logs",
            tmp_dir=data_dir / "tmp",
            exports_dir=data_dir / "exports",
            migrations_dir=Path(__file__).resolve().parent / "migrations",
        )

    def ensure(self) -> None:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.backup_dir,
            self.log_dir,
            self.tmp_dir,
            self.exports_dir,
            self.db_path.parent,
        ):
            directory.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def _load_dotenv(root: Path) -> None:
    env_path = root / ".env"
    try:
        from dotenv import load_dotenv

        if env_path.exists():
            load_dotenv(env_path, override=False)
    except ImportError:
        # Minimal .env parser as a fallback.
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def _read_secret(root: Path) -> str:
    value = os.getenv("RADAR_REPLY_SECRET", "").strip()
    if value:
        return value
    secret_path = root / "data" / "reply_secret"
    try:
        if secret_path.exists():
            return secret_path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


@dataclass
class Settings:
    root: Path
    timezone: str = "Asia/Kolkata"
    log_level: str = "INFO"
    user_agent: str = "GlobalCinemaRadar/1.0 (personal research)"
    contact_url: str = ""
    max_bytes: int = 5 * 1024 * 1024
    global_concurrency: int = 4
    domain_rate_seconds: float = 2.0
    respect_robots: bool = True

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_reply_topic: str = ""
    ntfy_token: str = ""
    ntfy_reply_token: str = ""
    reply_secret: str = ""

    # None means "use config/profile.yaml"; a value here is an explicit override.
    instant_threshold: float | None = None
    digest_threshold: float | None = None
    daily_alert_cap: int = 15
    quiet_start: int = 23
    quiet_end: int = 7
    digest_max_items: int = 20
    exploration_slots: int = 2
    digest_individual: bool = False

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_folder: str = "INBOX"
    imap_label_folder: str = ""
    imap_fetch_limit: int = 50
    imap_poll_seconds: int = 60
    imap_enabled: bool = False

    searxng_url: str = "http://127.0.0.1:8888"
    searxng_enabled: bool = False
    searxng_time_range: str = "day"

    llm_provider: str = "none"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "llama-3.1-8b-instant"
    llm_timeout_seconds: int = 45
    llm_max_chars: int = 12000

    embedding_model: str = "auto"
    rdap_lookup: bool = False
    rdap_timeout_seconds: float = 7.0

    healthchecks_url: str = ""
    heartbeat_interval_minutes: int = 15

    enable_browser: bool = False
    enable_ocr: bool = False
    ocr_langs: str = "eng+hin"
    discord_bot_token: str = ""
    discord_guild_ids: list[str] = field(default_factory=list)
    discord_channel_ids: list[str] = field(default_factory=list)

    litestream_replica_url: str = ""
    backup_retention_days: int = 30

    dashboard_port: int = 8501
    dashboard_url: str = "http://127.0.0.1:8501"

    @classmethod
    def from_env(cls, paths: Paths) -> "Settings":
        _load_dotenv(paths.root)
        root = paths.root
        llm_provider = os.getenv("LLM_PROVIDER", "none").strip().lower()
        llm_key = os.getenv("LLM_API_KEY", "").strip()
        if llm_provider in {"", "none"} and llm_key:
            llm_provider = "groq"
        if llm_provider not in {"none", "groq", "openai", "ollama", "custom"}:
            log.warning("Unknown LLM_PROVIDER %r; disabling LLM", llm_provider)
            llm_provider = "none"

        settings = cls(
            root=root,
            timezone=os.getenv("RADAR_TIMEZONE", "Asia/Kolkata"),
            log_level=os.getenv("RADAR_LOG_LEVEL", "INFO"),
            user_agent=os.getenv(
                "RADAR_USER_AGENT",
                "GlobalCinemaRadar/1.0 (personal research; +mailto:you@example.com)",
            ),
            contact_url=os.getenv("RADAR_CONTACT_URL", ""),
            max_bytes=env_int("RADAR_MAX_BYTES", 5 * 1024 * 1024),
            global_concurrency=max(1, env_int("RADAR_GLOBAL_CONCURRENCY", 4)),
            domain_rate_seconds=max(0.0, env_float("RADAR_DOMAIN_RATE_SECONDS", 2.0)),
            respect_robots=env_bool("RADAR_RESPECT_ROBOTS", True),
            ntfy_server=os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            ntfy_topic=os.getenv("NTFY_TOPIC", "").strip(),
            ntfy_reply_topic=os.getenv("NTFY_REPLY_TOPIC", "").strip(),
            ntfy_token=os.getenv("NTFY_TOKEN", "").strip(),
            ntfy_reply_token=os.getenv("NTFY_REPLY_TOKEN", "").strip() or os.getenv("NTFY_TOKEN", "").strip(),
            reply_secret=_read_secret(root),
            instant_threshold=_env_float_override("RADAR_INSTANT_THRESHOLD"),
            digest_threshold=_env_float_override("RADAR_DIGEST_THRESHOLD"),
            daily_alert_cap=max(1, env_int("RADAR_DAILY_ALERT_CAP", 15)),
            quiet_start=env_int("RADAR_QUIET_START", 23),
            quiet_end=env_int("RADAR_QUIET_END", 7),
            digest_max_items=max(1, env_int("RADAR_DIGEST_MAX_ITEMS", 20)),
            exploration_slots=max(0, env_int("RADAR_EXPLORATION_SLOTS", 2)),
            digest_individual=env_bool("RADAR_DIGEST_INDIVIDUAL", False),
            imap_host=os.getenv("IMAP_HOST", "").strip(),
            imap_port=env_int("IMAP_PORT", 993),
            imap_user=os.getenv("IMAP_USER", "").strip(),
            imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
            imap_folder=os.getenv("IMAP_FOLDER", "INBOX").strip() or "INBOX",
            imap_label_folder=os.getenv("IMAP_LABEL_FOLDER", "").strip(),
            imap_fetch_limit=max(1, env_int("IMAP_FETCH_LIMIT", 50)),
            imap_poll_seconds=max(10, env_int("IMAP_POLL_SECONDS", 60)),
            imap_enabled=env_bool("IMAP_ENABLED", False),
            searxng_url=os.getenv("SEARXNG_URL", "http://127.0.0.1:8888").rstrip("/"),
            searxng_enabled=env_bool("SEARXNG_ENABLED", False),
            searxng_time_range=os.getenv("SEARXNG_TIME_RANGE", "day").strip() or "day",
            llm_provider=llm_provider,
            llm_api_key=llm_key,
            llm_base_url=os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", "llama-3.1-8b-instant").strip(),
            llm_timeout_seconds=max(5, env_int("LLM_TIMEOUT_SECONDS", 45)),
            llm_max_chars=max(2000, env_int("LLM_MAX_CHARS", 12000)),
            embedding_model=os.getenv("RADAR_EMBEDDING_MODEL", "auto").strip() or "auto",
            rdap_lookup=env_bool("RADAR_RDAP_LOOKUP", False),
            rdap_timeout_seconds=max(2.0, env_float("RADAR_RDAP_TIMEOUT_SECONDS", 7.0)),
            healthchecks_url=os.getenv("HEALTHCHECKS_URL", "").strip(),
            heartbeat_interval_minutes=max(1, env_int("HEARTBEAT_INTERVAL_MINUTES", 15)),
            enable_browser=env_bool("RADAR_ENABLE_BROWSER", False),
            enable_ocr=env_bool("RADAR_ENABLE_OCR", False),
            ocr_langs=os.getenv("RADAR_OCR_LANGS", "eng+hin").strip() or "eng+hin",
            discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", "").strip(),
            discord_guild_ids=_split_ids(os.getenv("DISCORD_GUILD_IDS", "")),
            discord_channel_ids=_split_ids(os.getenv("DISCORD_CHANNEL_IDS", "")),
            litestream_replica_url=os.getenv("LITESTREAM_REPLICA_URL", "").strip(),
            backup_retention_days=max(1, env_int("BACKUP_RETENTION_DAYS", 30)),
            dashboard_port=env_int("RADAR_DASHBOARD_PORT", 8501),
            dashboard_url=os.getenv("RADAR_DASHBOARD_URL", "http://127.0.0.1:8501").rstrip("/"),
        )
        return settings

    # -- derived -----------------------------------------------------------
    @property
    def effective_user_agent(self) -> str:
        """User-Agent plus a contact URL, so site owners can reach a human."""
        agent = self.user_agent.strip() or "GlobalCinemaRadar/1.0"
        contact = self.contact_url.strip()
        if contact and contact not in agent:
            return f"{agent} (+{contact})"
        return agent

    @property
    def db_path(self) -> Path:
        return Path(os.getenv("RADAR_DB", str(Paths.discover().db_path)))

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "none":
            return False
        if self.llm_provider in {"groq", "openai", "custom"} and not self.llm_api_key:
            return False
        return True

    @property
    def ntfy_configured(self) -> bool:
        return bool(self.ntfy_server and self.ntfy_topic)

    @property
    def reply_configured(self) -> bool:
        return bool(self.ntfy_server and self.ntfy_reply_topic)

    @property
    def reply_url(self) -> str:
        if not self.reply_configured:
            return ""
        return f"{self.ntfy_server}/{quote(self.ntfy_reply_topic, safe='')}"

    @property
    def alert_url(self) -> str:
        if not self.ntfy_configured:
            return ""
        return f"{self.ntfy_server}/{quote(self.ntfy_topic, safe='')}"

    @property
    def imap_folder_effective(self) -> str:
        return self.imap_label_folder or self.imap_folder

    def generate_reply_secret(self) -> str:
        secret = secrets.token_urlsafe(48)
        path = self.root / "data" / "reply_secret"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
        self.reply_secret = secret
        return secret

    def summary(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "db": str(self.db_path),
            "timezone": self.timezone,
            "instant_threshold": self.instant_threshold,
            "digest_threshold": self.digest_threshold,
            "ntfy": self.ntfy_server,
            "ntfy_configured": self.ntfy_configured,
            "reply_configured": self.reply_configured,
            "reply_secret": bool(self.reply_secret),
            "imap_enabled": self.imap_enabled,
            "llm_provider": self.llm_provider,
            "llm_enabled": self.llm_enabled,
            "embedding_model": self.embedding_model,
            "heartbeat": bool(self.healthchecks_url),
        }


def _env_float_override(name: str) -> float | None:
    """Read an optional numeric override; unset/blank means "not configured"."""
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("Ignoring %s=%r (not a number)", name, raw)
        return None


def _split_ids(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\s]+", value or "") if part.strip()]


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    name: str = "Your Name"
    age_range: tuple[int, int] = (18, 30)
    gender_presentation: str = "any"
    travel_radius_km: float | None = 300.0
    paid_only: bool = False
    nudity_max: str = "none"
    role_types: list[str] = field(default_factory=lambda: ["lead", "supporting", "voiceover", "web_series", "short_film"])
    showreel_ready: bool = True
    availability: str = "weekends+evenings"
    languages: list[str] = field(default_factory=lambda: ["Hindi", "English", "Hinglish"])
    base_city: str = "Delhi"
    base_country: str = "India"
    preferred_cities: list[str] = field(default_factory=lambda: ["Delhi", "Mumbai", "Noida", "Gurugram"])
    remote_selftape_ok: bool = True
    fee_tolerance: str = "festivals_only"
    usage_rights: str = "no_perpetual_buyout"
    selftape_ready: bool = True
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    thresholds: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    city_coords: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Profile":
        data = data or {}
        age = data.get("age_range") or [18, 30]
        try:
            age_min, age_max = int(age[0]), int(age[1])
        except (TypeError, ValueError, IndexError):
            age_min, age_max = 18, 30
        if age_min > age_max:
            age_min, age_max = age_max, age_min

        weights = dict(DEFAULT_WEIGHTS)
        weights.update({k: float(v) for k, v in (data.get("weights") or {}).items() if k in DEFAULT_WEIGHTS})
        total = sum(max(0.0, v) for v in weights.values()) or 1.0
        weights = {k: max(0.0, v) / total for k, v in weights.items()}

        thresholds = dict(DEFAULT_THRESHOLDS)
        thresholds.update(
            {k: float(v) for k, v in (data.get("thresholds") or {}).items() if k in DEFAULT_THRESHOLDS}
        )

        coords: dict[str, tuple[float, float, str]] = {}
        for city, value in (data.get("city_coords") or {}).items():
            try:
                coords[str(city).lower()] = (float(value[0]), float(value[1]), str(value[2]) if len(value) > 2 else "")
            except (TypeError, ValueError, IndexError):
                continue

        languages = data.get("languages") or ["Hindi", "English"]
        if isinstance(languages, str):
            languages = [part.strip() for part in languages.split(",") if part.strip()]
        roles = data.get("role_types") or []
        if isinstance(roles, str):
            roles = [part.strip() for part in roles.split(",") if part.strip()]
        preferred = data.get("preferred_cities") or [data.get("base_city", "Delhi")]
        if isinstance(preferred, str):
            preferred = [part.strip() for part in preferred.split(",") if part.strip()]

        return cls(
            name=str(data.get("name", "Your Name")),
            age_range=(age_min, age_max),
            gender_presentation=str(data.get("gender_presentation", "any")).lower(),
            travel_radius_km=_optional_float(data.get("travel_radius_km"), 300.0),
            paid_only=bool(data.get("paid_only", False)),
            nudity_max=str(data.get("nudity_max", "none")).lower(),
            role_types=[str(r).lower() for r in roles],
            showreel_ready=bool(data.get("showreel_ready", True)),
            availability=str(data.get("availability", "weekends+evenings")),
            languages=[str(lang) for lang in languages],
            base_city=str(data.get("base_city", "Delhi")),
            base_country=str(data.get("base_country", "India")),
            preferred_cities=[str(c) for c in preferred],
            remote_selftape_ok=bool(data.get("remote_selftape_ok", True)),
            fee_tolerance=str(data.get("fee_tolerance", "festivals_only")),
            usage_rights=str(data.get("usage_rights", "no_perpetual_buyout")),
            selftape_ready=bool(data.get("selftape_ready", True)),
            weights=weights,
            thresholds=thresholds,
            city_coords=coords,
            raw=data,
        )

    @classmethod
    def load(cls, path: Path) -> "Profile":
        data = load_yaml(path, default={}, required=True) or {}
        if not isinstance(data, dict):
            raise ValueError(f"profile.yaml must be a mapping, got {type(data).__name__}")
        return cls.from_dict(data)

    def to_text(self) -> str:
        parts = [
            f"Acting opportunities for a {self.age_range[0]}-{self.age_range[1]} year old performer",
            f"based in {self.base_city}, {self.base_country}",
            f"languages: {', '.join(self.languages)}",
            f"role types: {', '.join(self.role_types)}",
            f"available: {self.availability}",
            f"preferred cities: {', '.join(self.preferred_cities)}",
            "self tape" if self.selftape_ready else "",
            "remote work" if self.remote_selftape_ok else "",
            "showreel ready" if self.showreel_ready else "",
            f"nudity limit: {self.nudity_max}",
            "paid work only" if self.paid_only else "paid or unpaid",
            f"festival fees: {self.fee_tolerance}",
            f"usage rights: {self.usage_rights}",
        ]
        return normalize_text("; ".join(p for p in parts if p))

    def search_terms(self) -> list[str]:
        terms: list[str] = []
        for value in (
            self.base_city,
            self.base_country,
            *self.preferred_cities,
            *self.languages,
            *self.role_types,
            "actor",
            "actress",
            "casting",
            "audition",
            "short film",
            "web series",
            "voiceover",
        ):
            value = (value or "").strip().lower()
            if value and value not in terms:
                terms.append(value)
        return terms

    def nudity_limit(self) -> int:
        return NUDITY_ORDER.get(self.nudity_max, 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "age_range": list(self.age_range),
            "gender_presentation": self.gender_presentation,
            "travel_radius_km": self.travel_radius_km,
            "paid_only": self.paid_only,
            "nudity_max": self.nudity_max,
            "role_types": self.role_types,
            "showreel_ready": self.showreel_ready,
            "availability": self.availability,
            "languages": self.languages,
            "base_city": self.base_city,
            "base_country": self.base_country,
            "preferred_cities": self.preferred_cities,
            "remote_selftape_ok": self.remote_selftape_ok,
            "fee_tolerance": self.fee_tolerance,
            "usage_rights": self.usage_rights,
            "selftape_ready": self.selftape_ready,
            "weights": self.weights,
            "thresholds": self.thresholds,
        }


def _optional_float(value: Any, default: float | None) -> float | None:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


@dataclass
class SourceConfig:
    name: str
    type: str
    url: str = ""
    tier: int = 3
    enabled: bool = True
    poll_interval_minutes: int = 60
    fetch_strategy: str = "http"
    extractor: str = "auto"
    extractor_config: dict[str, Any] = field(default_factory=dict)
    cadence_hint: str = ""
    contact: str = ""
    notes: str = ""
    config: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    assume_opportunity: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, index: int = 0) -> "SourceConfig":
        if not isinstance(data, dict):
            raise ValueError(f"source #{index} must be a mapping")
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError(f"source #{index} has no name")
        stype = str(data.get("type") or "web").lower().strip()
        if stype not in VALID_SOURCE_TYPES:
            raise ValueError(f"source {name!r}: unknown type {stype!r}")
        fetcher = str(data.get("fetch") or data.get("fetch_strategy") or "http").lower().strip()
        if fetcher not in VALID_FETCH_STRATEGIES:
            fetcher = "auto"
        extractor = str(data.get("extractor") or "auto").lower().strip()
        if extractor not in VALID_EXTRACTORS:
            extractor = "auto"
        tier = int(data.get("tier", 3) or 3)
        tier = min(3, max(1, tier))
        interval = data.get("poll_interval_minutes")
        if interval is None:
            interval = DEFAULT_INTERVALS.get(stype, 60)
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        return cls(
            name=name,
            type=stype,
            url=str(data.get("url") or "").strip(),
            tier=tier,
            enabled=bool(data.get("enabled", True)),
            poll_interval_minutes=max(1, int(interval)),
            fetch_strategy=fetcher,
            extractor=extractor,
            extractor_config=data.get("extractor_config") or {},
            cadence_hint=str(data.get("cadence_hint") or ""),
            contact=str(data.get("contact") or ""),
            notes=str(data.get("notes") or ""),
            config=data.get("config") or {},
            tags=[str(t) for t in tags],
            assume_opportunity=bool(data.get("assume_opportunity", False)),
        )

    @property
    def urls(self) -> list[str]:
        values: list[str] = []
        if self.url:
            values.append(self.url)
        extra = self.config.get("urls") or []
        if isinstance(extra, str):
            extra = [extra]
        for item in extra:
            if item and item not in values:
                values.append(str(item))
        return values

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "url": self.url,
            "tier": self.tier,
            "enabled": self.enabled,
            "poll_interval_minutes": self.poll_interval_minutes,
            "fetch": self.fetch_strategy,
            "extractor": self.extractor,
            "extractor_config": self.extractor_config,
            "cadence_hint": self.cadence_hint,
            "contact": self.contact,
            "notes": self.notes,
            "config": self.config,
            "tags": self.tags,
            "assume_opportunity": self.assume_opportunity,
        }


def load_sources(path: Path) -> list[SourceConfig]:
    data = load_yaml(path, default={}, required=True) or {}
    if isinstance(data, dict):
        items = data.get("sources") or []
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("sources.yaml must contain a `sources:` list")
    sources: list[SourceConfig] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        source = SourceConfig.from_dict(item, index=index)
        if source.name in names:
            raise ValueError(f"duplicate source name: {source.name}")
        names.add(source.name)
        sources.append(source)
    return sources


# ---------------------------------------------------------------------------
# Veto / risk patterns
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    id: str
    name: str
    pattern: str
    reason: str = ""
    applies_to: list[str] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    weight: float = 0.0
    enabled: bool = True
    flags: int = re.IGNORECASE
    _compiled: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def compile(self) -> None:
        try:
            self._compiled = re.compile(self.pattern, self.flags)
        except re.error as exc:
            raise ValueError(f"invalid regex for rule {self.id}: {exc}") from exc

    def matches(self, text: str) -> re.Match[str] | None:
        if not self.enabled or not text:
            return None
        if self._compiled is None:
            self.compile()
        return self._compiled.search(text)  # type: ignore[union-attr]

    def applies(self, opportunity_type: str) -> bool:
        if not self.applies_to:
            return True
        return opportunity_type.lower() in {x.lower() for x in self.applies_to}

    def exempt(self, opportunity_type: str) -> bool:
        return opportunity_type.lower() in {x.lower() for x in self.exceptions}


@dataclass
class VetoConfig:
    vetoes: list[Rule] = field(default_factory=list)
    penalties: list[Rule] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def load(cls, path: Path) -> "VetoConfig":
        data = load_yaml(path, default={}, required=True) or {}
        vetoes = [_rule_from_dict(item, "veto") for item in (data.get("vetoes") or [])]
        penalties = [_rule_from_dict(item, "penalty") for item in (data.get("penalties") or [])]
        cfg = cls(vetoes=vetoes, penalties=penalties, notes=str(data.get("notes") or ""))
        for rule in [*vetoes, *penalties]:
            rule.compile()
        return cfg

    def veto_for(self, text: str, opportunity_type: str) -> Rule | None:
        for rule in self.vetoes:
            if not rule.applies(opportunity_type) or rule.exempt(opportunity_type):
                continue
            if rule.matches(text):
                return rule
        return None

    def penalty_matches(self, text: str) -> list[Rule]:
        found: list[Rule] = []
        for rule in self.penalties:
            if rule.matches(text):
                found.append(rule)
        return found


def _rule_from_dict(data: dict[str, Any], kind: str) -> Rule:
    if not isinstance(data, dict):
        raise ValueError(f"{kind} rule must be a mapping")
    rule = Rule(
        id=str(data.get("id") or data.get("name") or "unnamed"),
        name=str(data.get("name") or data.get("id") or "unnamed"),
        pattern=str(data.get("pattern") or ""),
        reason=str(data.get("reason") or ""),
        applies_to=[str(x) for x in (data.get("applies_to") or [])],
        exceptions=[str(x) for x in (data.get("exceptions") or [])],
        weight=float(data.get("weight", 0) or 0),
        enabled=bool(data.get("enabled", True)),
        flags=re.IGNORECASE | (re.MULTILINE if data.get("multiline", False) else 0),
    )
    if not rule.pattern:
        raise ValueError(f"{kind} rule {rule.id!r} has no pattern")
    return rule


# ---------------------------------------------------------------------------
# Whole application config
# ---------------------------------------------------------------------------


@dataclass
class AppConfig:
    paths: Paths
    settings: Settings
    profile: Profile
    sources: list[SourceConfig]
    vetoes: VetoConfig

    # -- effective alert cut-offs (environment overrides profile.yaml) -----
    @property
    def instant_cutoff(self) -> float:
        if self.settings.instant_threshold is not None:
            return float(self.settings.instant_threshold)
        return float(self.profile.thresholds.get("instant", 80))

    @property
    def digest_cutoff(self) -> float:
        if self.settings.digest_threshold is not None:
            return float(self.settings.digest_threshold)
        return float(self.profile.thresholds.get("digest", 55))

    @classmethod
    def load(cls, paths: Paths | None = None) -> "AppConfig":
        paths = paths or Paths.discover()
        paths.ensure()
        settings = Settings.from_env(paths)
        profile_path = paths.config_dir / "profile.yaml"
        sources_path = paths.config_dir / "sources.yaml"
        veto_path = paths.config_dir / "veto_patterns.yaml"
        profile = Profile.load(profile_path) if profile_path.exists() else Profile()
        sources = load_sources(sources_path) if sources_path.exists() else []
        vetoes = VetoConfig.load(veto_path) if veto_path.exists() else VetoConfig()
        return cls(paths=paths, settings=settings, profile=profile, sources=sources, vetoes=vetoes)

    def ensure_dirs(self) -> None:
        self.paths.ensure()

    def dump_summary(self) -> str:
        return json_dumps(
            {
                "paths": {
                    "root": str(self.paths.root),
                    "config": str(self.paths.config_dir),
                    "db": str(self.paths.db_path),
                },
                "settings": self.settings.summary(),
                "profile": self.profile.to_dict(),
                "sources": len(self.sources),
                "enabled_sources": sum(1 for s in self.sources if s.enabled),
                "vetoes": len(self.vetoes.vetoes),
                "penalties": len(self.vetoes.penalties),
            }
        )
