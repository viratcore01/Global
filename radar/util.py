"""Shared utility helpers: time, JSON, logging, files, retries."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, TypeVar

# India has a single fixed offset and no DST, so we do not need the system tz
# database (which is often missing on Windows).
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
UTC = timezone.utc

T = TypeVar("T")

_LOG_CONFIGURED = False


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ist() -> datetime:
    return datetime.now(IST)


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def iso(dt: datetime | None) -> str | None:
    """Serialise to an unambiguous ISO-8601 string."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.isoformat(timespec="seconds")


def iso_utc(dt: datetime | None = None) -> str:
    return iso(dt or now_utc())  # type: ignore[arg-type]


def parse_dt(value: Any, *, assume_ist: bool = True) -> datetime | None:
    """Parse ISO-8601-ish values and date-only strings.

    Date-only values are interpreted as end-of-day in IST because that is how
    casting deadlines work in practice ("last date: 12 Nov" means you can still
    submit during 12 Nov).
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=IST if assume_ist else UTC)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59, tzinfo=IST)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST if assume_ist else UTC)
        return dt
    except ValueError:
        pass

    # Common display formats.
    for fmt in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %y",
        "%d %B %y",
    ):
        try:
            dt = datetime.strptime(text, fmt)
            # Date-only formats mean end of day.
            return datetime(dt.year, dt.month, dt.day, 23, 59, 59, tzinfo=IST)
        except ValueError:
            continue
    return None


def end_of_day_ist(dt: datetime) -> datetime:
    local = to_ist(dt)
    return datetime(local.year, local.month, local.day, 23, 59, 59, tzinfo=IST)


def days_until(target: datetime | None, *, now: datetime | None = None) -> float | None:
    if target is None:
        return None
    current = now or now_utc()
    if target.tzinfo is None:
        target = target.replace(tzinfo=IST)
    return (target - current).total_seconds() / 86400.0


def humanize_deadline(target: datetime | None, *, now: datetime | None = None) -> str:
    if target is None:
        return "unknown"
    local = to_ist(target)
    delta = days_until(target, now=now)
    stamp = local.strftime("%d %b %Y")
    if delta is None:
        return stamp
    if delta < 0:
        return f"{stamp} (expired)"
    if delta < 1:
        hours = max(1, int(delta * 24))
        return f"{stamp} (in {hours}h)"
    if delta < 2:
        return f"{stamp} (tomorrow)"
    return f"{stamp} (in {int(delta)}d)"


def date_only(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return to_ist(dt).date().isoformat()


def week_start(dt: datetime | None = None) -> date:
    d = (dt or now_ist()).date()
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# JSON / hashing
# ---------------------------------------------------------------------------


def json_dumps(value: Any, *, pretty: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=_json_default,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        sort_keys=pretty,
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return iso(value if isinstance(value, datetime) else datetime(value.year, value.month, value.day, tzinfo=IST))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


def json_loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def short_hash(text: str, length: int = 16) -> str:
    return sha256_text(text)[:length]


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalize_ws(text: str) -> str:
    """Collapse all whitespace to single spaces (used for evidence checks)."""
    return re.sub(r"\s+", " ", text or "").strip()


def normalize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "").replace("\ufeff", "")
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    return text


def truncate(text: str, limit: int = 500, ellipsis: str = "...") -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(ellipsis))].rstrip() + ellipsis


def slugify(text: str, max_length: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_length].strip("-") or "item"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def atomic_write(path: Path | str, data: str | bytes, *, encoding: str = "utf-8") -> None:
    """Write atomically so a crash never leaves a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if isinstance(data, bytes) else {"encoding": encoding, "newline": "\n"}
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, mode, **kwargs) as fh:  # type: ignore[arg-type]
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_text(path: Path | str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def ensure_dir(path: Path | str) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def iter_chunks(items: Iterable[T], size: int) -> Iterator[list[T]]:
    chunk: list[T] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 60.0) -> float:
    """Exponential backoff with full jitter."""
    import random

    return min(cap, base * (2 ** max(0, attempt - 1))) * random.uniform(0.5, 1.0)


def retry_call(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    logger: logging.Logger | None = None,
    description: str = "operation",
) -> T:
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except exceptions as exc:  # type: ignore[misc]
            last = exc
            if attempt >= attempts:
                break
            delay = backoff_delay(attempt, base_delay)
            if logger:
                logger.warning("%s failed (attempt %d/%d): %s; retrying in %.1fs", description, attempt, attempts, exc, delay)
            time.sleep(delay)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class _ColorFormatter(logging.Formatter):
    COLORS = {
        "DEBUG": "\033[36m",
        "INFO": "\033[32m",
        "WARNING": "\033[33m",
        "ERROR": "\033[31m",
        "CRITICAL": "\033[1;31m",
    }

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if sys.stderr.isatty() and record.levelname in self.COLORS:
            return f"{self.COLORS[record.levelname]}{text}\033[0m"
        return text


def configure_logging(level: str = "INFO", log_dir: Path | None = None, *, quiet: bool = False) -> None:
    global _LOG_CONFIGURED
    root = logging.getLogger()
    if _LOG_CONFIGURED:
        root.setLevel(getattr(logging, level.upper(), logging.INFO))
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(_ColorFormatter(fmt, datefmt))
    if not quiet:
        root.addHandler(stream)

    if log_dir is not None:
        try:
            ensure_dir(log_dir)
            from logging.handlers import RotatingFileHandler

            file_handler = RotatingFileHandler(
                Path(log_dir) / "radar.log",
                maxBytes=5_000_000,
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter(fmt, datefmt))
            root.addHandler(file_handler)
        except OSError:
            pass

    # Third-party noise.
    for noisy in ("httpx", "httpcore", "urllib3", "dateparser", "trafilatura", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _LOG_CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def mask_secret(value: str, visible: int = 4) -> str:
    if not value:
        return "(not set)"
    if len(value) <= visible * 2:
        return "*" * len(value)
    return value[:visible] + "..." + value[-visible:]


def chunks_of_text(text: str, size: int) -> Iterator[str]:
    for i in range(0, len(text), size):
        yield text[i : i + size]
