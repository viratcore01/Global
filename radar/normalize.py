"""URL canonicalisation, contact extraction, language detection, text helpers."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .util import get_logger, normalize_unicode, normalize_ws, sha256_text

log = get_logger(__name__)

TRACKING_PREFIXES = ("utm_",)
TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_",
    "referrer",
    "source",
    "campaign",
    "affiliate",
    "aff",
    "srsltid",
    "yclid",
    "_ga",
    "_gl",
    "vero_conv",
    "vero_id",
    "oly_anon_id",
    "oly_enc_id",
    "trk",
    "trkCampaign",
}

# Meaningful query parameters that should survive canonicalisation.
MEANINGFUL_KEYS = {"id", "p", "post", "jobid", "job", "opportunity", "casting", "audition", "event", "slug", "page"}

AMP_SUFFIXES = ("/amp", "/amp/", ".amp", ".amp.html", "?amp", "&amp")

FREEMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.in",
    "yahoo.in",
    "hotmail.com",
    "outlook.com",
    "live.com",
    "msn.com",
    "rediffmail.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "mail.com",
    "gmx.com",
    "zoho.com",
    "yandex.com",
    "icloud.com",
    "me.com",
    "163.com",
    "qq.com",
}

# Multi-label public suffixes we care about. Not a full PSL, but enough for
# contact-domain grouping and reputation checks.
MULTI_SUFFIXES = {
    "co.in",
    "com.in",
    "net.in",
    "org.in",
    "gen.in",
    "firm.in",
    "ind.in",
    "co.uk",
    "org.uk",
    "me.uk",
    "ltd.uk",
    "com.au",
    "net.au",
    "org.au",
    "co.nz",
    "co.za",
    "com.br",
    "com.mx",
    "com.ar",
    "co.jp",
    "co.kr",
    "com.sg",
    "com.my",
    "co.id",
    "com.ph",
    "com.vn",
    "co.th",
    "com.tr",
    "com.ae",
    "co.ae",
    "com.sa",
    "com.qa",
    "com.pk",
    "com.bd",
    "com.np",
    "com.lk",
    "co.il",
    "com.eg",
    "com.ng",
    "co.ke",
    "com.gh",
}

EMAIL_RE = re.compile(
    # The trailing lookahead allows a sentence-ending dot ("...@site.example.")
    # while still refusing to stop early inside a multi-label TLD
    # ("...@site.co.uk" must not become "...@site.co").
    r"(?<![\w.+-])([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,24})(?![\w\-]|\.[A-Z0-9]{2,})",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"https?://[^\s<>\"'()\[\]{}]+",
    re.IGNORECASE,
)

# Indian mobile numbers, optional country code / trunk zero. Boundaries are
# deliberately strict to avoid matching years, amounts and postcodes.
IN_PHONE_RE = re.compile(
    r"(?<![\d])(?:(?:\+|00)?91[\s\-.]?)?(?:0)?([6-9]\d{9})(?![\d])"
)
INTL_PHONE_RE = re.compile(
    r"(?<![\d])\+(?!91\b)(\d{1,3})[\s\-.]?(\d{2,5})[\s\-.]?(\d{3,5})[\s\-.]?(\d{3,5})(?![\d])"
)

WHATSAPP_RE = re.compile(r"\b(whats\s?app|wa\.me|wa\.link|whatsapp\.com)\b", re.IGNORECASE)
TELEGRAM_RE = re.compile(r"\b(telegram|t\.me/)\b", re.IGNORECASE)
INSTAGRAM_RE = re.compile(r"\b(instagram|insta\b)|(?<![\w@.])@[A-Za-z0-9._]{2,30}", re.IGNORECASE)


# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------


def is_http_url(value: str | None) -> bool:
    if not value:
        return False
    try:
        scheme = urlsplit(value.strip()).scheme.lower()
    except ValueError:
        return False
    return scheme in {"http", "https"}


def _clean_host(host: str) -> str:
    host = host.strip().lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    if host.startswith("mobile."):
        host = host[7:]
    if host.endswith(".amp"):
        host = host[: -len(".amp")]
    return host


def canonicalize_url(url: str, base: str | None = None) -> str | None:
    """Return a stable canonical http(s) URL, or None for non-http schemes."""
    if not url:
        return None
    raw = url.strip()
    if not raw or raw.startswith(("javascript:", "mailto:", "tel:", "data:")):
        return None
    if base:
        try:
            raw = urljoin(base, raw)
        except ValueError:
            return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"}:
        return None
    if not parts.netloc:
        return None

    host = _clean_host(parts.hostname or "")
    if not host:
        return None
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host
    if port and not ((parts.scheme == "https" and port == 443) or (parts.scheme == "http" and port == 80)):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    # Remove duplicate slashes, AMP suffix, and tracking path fragments.
    path = re.sub(r"/{2,}", "/", path)
    for suffix in ("/amp/", "/amp"):
        if path.lower().endswith(suffix) and path != "/":
            path = path[: -len(suffix)] or "/"
            break
    # Google AMP URLs are served as /amp/<path>; treat them as the same page.
    if path.lower() == "/amp":
        path = "/"
    elif path.lower().startswith("/amp/"):
        path = path[len("/amp") :]
    if path.lower().endswith(".amp.html"):
        path = path[: -len(".amp.html")]
    elif path.lower().endswith(".amp"):
        path = path[: -len(".amp")]
    if ";" in path:
        path = path.split(";", 1)[0]
    if len(path) > 1:
        path = path.rstrip("/")

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        lk = key.lower()
        if lk in TRACKING_KEYS or lk.startswith(TRACKING_PREFIXES):
            continue
        if lk in {"amp", "output"} and value.lower() in {"1", "amp"}:
            continue
        kept.append((key, value))
    # Stable order; repeated parameters kept.
    kept.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(kept, doseq=True)

    return urlunsplit(("https", netloc, path or "/", query, ""))


def canonical_hash(url: str) -> str:
    canonical = canonicalize_url(url) or url.strip()
    return sha256_text(canonical)


def domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlsplit(url if "://" in url else f"https://{url}").hostname
    except ValueError:
        return None
    if not host:
        return None
    return _clean_host(host)


def registrable_domain(host: str | None) -> str | None:
    if not host:
        return None
    host = _clean_host(host)
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", host):
        return host
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    suffix2 = ".".join(labels[-2:])
    if suffix2 in MULTI_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix2


def is_freemail(email: str | None) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.rsplit("@", 1)[1].lower().strip(".")
    return domain in FREEMAIL_DOMAINS


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    email = value.strip().strip(".,;:()[]<>\"'").lower()
    if not EMAIL_RE.fullmatch(email):
        return None
    return email


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if not digits:
        return None
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("91") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if 8 <= len(digits) <= 15:
        return "+" + digits
    return None


def phone_key(value: str | None) -> str | None:
    phone = normalize_phone(value)
    return phone


def extract_emails(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in EMAIL_RE.finditer(text or ""):
        email = match.group(1).lower().strip(".")
        if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
            continue
        if email not in seen:
            seen.add(email)
            found.append(email)
    return found


def extract_phones(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in IN_PHONE_RE.finditer(text or ""):
        phone = normalize_phone(match.group(1))
        if phone and phone not in seen:
            seen.add(phone)
            found.append(phone)
    for match in INTL_PHONE_RE.finditer(text or ""):
        phone = normalize_phone(match.group(0))
        if phone and phone not in seen:
            seen.add(phone)
            found.append(phone)
    return found


def extract_urls(text: str, *, base: str | None = None, canonical: bool = True) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip(".,;:!?)]}\"'")
        url = canonicalize_url(raw, base) if canonical else raw
        if url and url not in seen:
            seen.add(url)
            found.append(url)
    return found


def extract_domains(text: str) -> list[str]:
    domains: list[str] = []
    seen: set[str] = set()
    for email in extract_emails(text):
        domain = registrable_domain(email.rsplit("@", 1)[1])
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    for url in extract_urls(text):
        domain = registrable_domain(domain_from_url(url))
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains


def contact_domain(value: str | None) -> str | None:
    if not value:
        return None
    if "@" in value:
        return registrable_domain(value.rsplit("@", 1)[1])
    if value.startswith("http"):
        return registrable_domain(domain_from_url(value))
    if value.startswith("+") or value.replace(" ", "").isdigit():
        return None
    return registrable_domain(value)


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def normalize_text(text: str, *, keep_newlines: bool = True) -> str:
    text = normalize_unicode(text)
    # Remove control characters except newlines/tabs.
    text = "".join(ch if (ch in "\n\t" or ord(ch) >= 32) else " " for ch in text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    if keep_newlines:
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = "\n".join(line.strip() for line in text.splitlines())
    else:
        text = normalize_ws(text)
    return text.strip()


def evidence_text(text: str) -> str:
    return normalize_ws(normalize_unicode(text))


def span_in_text(span: str, text: str) -> bool:
    """Check that a quoted span appears verbatim after whitespace normalisation."""
    if not span or not text:
        return False
    needle = normalize_ws(normalize_unicode(span))
    haystack = normalize_ws(normalize_unicode(text))
    if not needle:
        return False
    return needle.lower() in haystack.lower()


def find_span(text: str, span: str) -> tuple[int, int] | None:
    needle = normalize_ws(normalize_unicode(span)).lower()
    haystack = normalize_ws(normalize_unicode(text)).lower()
    idx = haystack.find(needle)
    if idx < 0:
        return None
    return idx, idx + len(needle)


def text_tokens(text: str) -> list[str]:
    """Unicode-aware-ish tokenisation that also keeps Devanagari words."""
    text = normalize_unicode(text).lower()
    # Keep letters/digits from any script, split on everything else.
    return re.findall(r"[^\W_]+", text, flags=re.UNICODE)


def word_shingles(text: str, size: int = 5) -> list[str]:
    tokens = text_tokens(text)
    if size <= 0:
        return tokens
    if len(tokens) <= size:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def detect_languages(text: str) -> list[str]:
    if not text:
        return []
    devanagari = len(re.findall(r"[\u0900-\u097F]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    tokens = set(text_tokens(text))
    # Romanised Hindi function words only.  English casting vocabulary ("role",
    # "audition", "casting", "contact") appears on almost every Indian post and
    # must not be enough to label a page Hinglish.
    hinglish_markers = {
        "hai",
        "hain",
        "ka",
        "ki",
        "ke",
        "mein",
        "nahi",
        "chahiye",
        "karo",
        "karna",
        "aap",
        "hum",
        "bhejo",
        "bhejna",
        "paisa",
        "paise",
        "kya",
        "raha",
        "rahe",
        "hoga",
        "bhi",
        "kyun",
        "milna",
        "milega",
    }
    has_hinglish = len(tokens & hinglish_markers) >= 2 and latin > 0
    langs: list[str] = []
    if devanagari >= 8 or (devanagari > 0 and devanagari > latin * 0.15):
        langs.append("Hindi")
    if has_hinglish:
        langs.append("Hinglish")
    if latin > 20 or (latin > 0 and not langs):
        langs.append("English")

    known = {
        "Tamil": ("tamil", "தமிழ்"),
        "Telugu": ("telugu", "తెలుగు"),
        "Malayalam": ("malayalam", "മലയാളം"),
        "Kannada": ("kannada", "ಕನ್ನಡ"),
        "Marathi": ("marathi", "मराठी"),
        "Bengali": ("bengali", "বাংলা"),
        "Punjabi": ("punjabi", "ਪੰਜਾਬੀ"),
        "Gujarati": ("gujarati", "ગુજરાતી"),
        "Urdu": ("urdu", "اردو"),
        "French": ("french", "français"),
        "Spanish": ("spanish", "español"),
        "Arabic": ("arabic", "العربية"),
    }
    lower = text.lower()
    for name, markers in known.items():
        if any(marker.lower() in lower for marker in markers):
            if name not in langs:
                langs.append(name)
    # Script ranges that are unambiguous.
    if re.search(r"[\u0B80-\u0BFF]", text) and "Tamil" not in langs:
        langs.append("Tamil")
    if re.search(r"[\u0C00-\u0C7F]", text) and "Telugu" not in langs:
        langs.append("Telugu")
    if re.search(r"[\u0D00-\u0D7F]", text) and "Malayalam" not in langs:
        langs.append("Malayalam")
    if re.search(r"[\u0C80-\u0CFF]", text) and "Kannada" not in langs:
        langs.append("Kannada")
    if re.search(r"[\u0980-\u09FF]", text) and "Bengali" not in langs:
        langs.append("Bengali")
    if re.search(r"[\u0A00-\u0A7F]", text) and "Punjabi" not in langs:
        langs.append("Punjabi")
    # Explicitly named languages ("Hindi and English speaking role") should be
    # honoured even when the surrounding text is short.
    for name in ("Hindi", "English", "Hinglish"):
        if re.search(rf"\b{name.lower()}\b", lower) and name not in langs:
            langs.append(name)
    return langs


def looks_like_casting(text: str) -> bool:
    lower = (text or "").lower()
    strong = (
        "casting",
        "audition",
        "open call",
        "cast call",
        "looking for actor",
        "looking for actress",
        "actors required",
        "artists required",
        "role:",
        "self tape",
        "self-tape",
        "extras wanted",
        "walk-in",
        "walk in audition",
        "casting call",
        "casting director",
        "शूटिंग",
        "ऑडिशन",
        "कास्टिंग",
        "कलाकार",
    )
    return any(token in lower for token in strong)


def looks_like_festival(text: str) -> bool:
    lower = (text or "").lower()
    tokens = (
        "film festival",
        "festival submission",
        "submission deadline",
        "call for entries",
        "call for submissions",
        "filmfreeway",
        "festhome",
        "short film festival",
        "documentary festival",
        "submit your film",
        "entry fee",
    )
    return any(token in lower for token in tokens)


def looks_like_crew(text: str) -> bool:
    lower = (text or "").lower()
    tokens = (
        "crew required",
        "crew call",
        "hiring crew",
        "director of photography",
        "dop required",
        "sound recordist",
        "gaffer",
        "grip",
        "production assistant",
        "line producer",
        "editor required",
        "colorist",
        "art director",
        "makeup artist",
        "hair stylist",
        "stunt coordinator",
        "casting assistant",
    )
    return any(token in lower for token in tokens)


def is_probably_not_opportunity(text: str) -> bool:
    """Cheap negative filter for nav pages, login walls and generic blogs."""
    lower = normalize_ws(text).lower()
    if len(lower) < 40:
        return True
    negative = (
        "page not found",
        "404 not found",
        "sign in to continue",
        "log in to continue",
        "enable javascript",
        "access denied",
        "captcha",
        "subscribe to read",
        "cookie policy",
        "privacy policy",
        "terms of service",
    )
    positive = looks_like_casting(text) or looks_like_festival(text) or looks_like_crew(text)
    if any(token in lower for token in negative) and not positive:
        return True
    return False


# ---------------------------------------------------------------------------
# Opportunity vs news classification
# ---------------------------------------------------------------------------

# An actual call an applicant can act on.  News articles can contain the word
# "casting" or "audition" without ever asking anyone to apply.
DIRECT_CALL_RE = re.compile(
    r"\b(?:"
    r"casting\s+call|cast\s+call|open\s+call|audition\s+call|call\s+for\s+(?:entries|submissions|applicants|actors|actresses|artists|crew)"
    r"|now\s+casting|we\s+are\s+casting|currently\s+casting|casting\s+(?:for|an?\s+actor|actress|artists?)"
    r"|looking\s+for|seeking\s+(?:an?\s+)?(?:actor|actress|artist|performer|model|dancer|singer|crew|dop|voice)"
    r"|(?:actor|actress|artist|performer|crew|dop|dancer|model|singer|voice\s+artist|extra)s?\s+(?:required|needed|wanted|urgently\s+required)"
    r"|(?:required|needed|wanted)\s*[:\-]"
    r"|urgent(?:ly)?\s+(?:required|needed)"
    r"|apply\s+(?:now|here|at|by|before|through|via|on|with)|send\s+(?:a|an|the|your|us)"
    r"|self[\s-]?tape|walk[\s-]?in|audition\s+(?:on|at|in|for|by|date)"
    r"|dm\s+(?:us|me)|inbox\s+(?:us|me)|whatsapp\s+(?:us|me|on)|contact\s+(?:us|me|at|on)"
    r"|submit\s+(?:your|by|before|the)|register\s+(?:now|at|here|by)"
    r"|ki\s+audition|casting\s+ke\s+liye|रोल\s+के\s+लिए|ऑडिशन|कास्टिंग"
    r")\b",
    re.IGNORECASE,
)

# Casting-specific asks.  Generic page chrome ("Contact Us", "Submit an ad")
# must not make a news article look actionable, so news filtering only trusts
# this narrower pattern.
CASTING_CALL_RE = re.compile(
    r"\b(?:"
    r"casting\s+call|cast\s+call|open\s+call|audition\s+call|call\s+for\s+(?:entries|submissions|applicants|actors|actresses|artists|crew)"
    r"|now\s+casting|we\s+are\s+casting|currently\s+casting|casting\s+(?:for|an?\s+actor|actress|artists?)"
    r"|looking\s+for\s+(?:an?\s+)?(?:actor|actress|artist|performer|model|dancer|singer|crew|dop|voice|talent|extra)(?:e?s)?"
    r"|looking\s+for\s+(?:\w+[,\s&]{1,12}){0,3}(?:actor|actress|model|dancer|artist|performer|talent)(?:e?s)?"
    r"|(?:actor|actress|artist|performer|crew|dop|dancer|model|singer|voice\s+artist|extra)(?:e?s)?\s+(?:required|needed|wanted)"
    r"|self[\s-]?tape|audition\s+(?:on|at|in|for|by|date)|casting\s+director"
    r"|रोल\s+के\s+लिए|ऑडिशन|कास्टिंग"
    r")\b",
    re.IGNORECASE,
)

# Hard news / press-release language.  When one of these is present and there
# is no direct call, the document is an article, not an opportunity.
NEWS_HARD_PHRASES = (
    "announces",
    "announced",
    "reveals",
    "revealed",
    "reportedly",
    "rumour",
    "rumor",
    "rumors",
    "rumours",
    "trailer",
    "teaser",
    "box office",
    "first look",
    "review",
    "recap",
    "obituary",
    "dead at",
    "dies at",
    "died at",
    "tribute",
    "election",
    "senate",
    "gunman",
    "killing",
    "tax credit",
    "tax credits",
    "jury",
    "finalists",
    "selects",
    "selected",
    "winners",
    "nominations",
    "awards ceremony",
)

# Softer editorial language that only matters when it piles up.
NEWS_WEAK_PHRASES = (
    "according to",
    "sources say",
    "exclusive",
    "interview",
    "opens up",
    "slams",
    "criticizes",
    "denies",
    "confirms",
    "report",
    "breaking",
    "opinion",
    "editorial",
    "explained",
    "why ",
    "how to",
    "tips",
    "guide",
    "top 10",
    "recap",
    "photos",
    "video",
)

# News/trade-press hosts.  A casting call published on a newspaper site still
# passes when it contains a direct call, so this only blocks article pages.
NEWS_DOMAIN_SUFFIXES = (
    "washingtonpost.com",
    "nytimes.com",
    "cnn.com",
    "bbc.com",
    "bbc.co.uk",
    "reuters.com",
    "apnews.com",
    "theguardian.com",
    "variety.com",
    "hollywoodreporter.com",
    "deadline.com",
    "indiewire.com",
    "collider.com",
    "screenrant.com",
    "ew.com",
    "people.com",
    "pagesix.com",
    "aol.com",
    "yahoo.com",
    "msn.com",
    "cbsnews.com",
    "nbcnews.com",
    "abcnews.go.com",
    "foxnews.com",
    "usatoday.com",
    "latimes.com",
    "nypost.com",
    "dailymail.co.uk",
    "thesun.co.uk",
    "mirror.co.uk",
    "indiatimes.com",
    "hindustantimes.com",
    "ndtv.com",
    "thehindu.com",
    "indianexpress.com",
    "firstpost.com",
    "news18.com",
    "zeenews.india.com",
    "abplive.com",
    "filmfare.com",
    "bollywoodhungama.com",
    "koimoi.com",
    "pinkvilla.com",
    "mid-day.com",
    "tellychakkar.com",
    "ottplay.com",
    "deccanherald.com",
    "telegraphindia.com",
    "scmp.com",
    "forbes.com",
    "bloomberg.com",
    "cnbc.com",
    "explorersweb.com",
    "cartoonbrew.com",
    "animenewsnetwork.com",
    "heraldpubs.com",
    "salisburypost.com",
    "mississippifreepress.org",
    "pajiba.com",
    "finalweapon.net",
    "wfmd.com",
    "justjared.com",
    "realtor.com",
    "charlotteobserver.com",
    "isthmus.com",
    "theindependentcritic.com",
    "aljazeera.com",
    "dawn.com",
    "tribune.com.pk",
    "gulfnews.com",
    "khaleejtimes.com",
)

DEADLINE_PHRASE_RE = re.compile(
    r"\b(?:deadline|last\s+date(?:\s+to\s+apply)?|apply\s+(?:by|before)|submit\s+(?:by|before)|"
    r"closing\s+date|applications?\s+close|auditions?\s+on)\b",
    re.IGNORECASE,
)


def is_news_url(url: str | None) -> bool:
    """True when the URL host is a newspaper/trade-press site."""
    if not url:
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if host.startswith("www."):
        host = host[4:]
    return any(host == suffix or host.endswith("." + suffix) for suffix in NEWS_DOMAIN_SUFFIXES)


def looks_like_news_article(text: str, url: str | None = None) -> bool:
    """Reject news/press coverage that merely mentions casting.

    The radar exists to surface calls an actor can act on.  A headline such as
    "James Bond casting rumours heat up" contains the word *casting* but has no
    audition, deadline or way to apply, so it must never become an opportunity.
    """
    clean = normalize_ws(text or "")
    lower = clean.lower()
    if not lower:
        return False

    casting_call = bool(CASTING_CALL_RE.search(clean))
    has_contact = bool(EMAIL_RE.search(clean) or IN_PHONE_RE.search(clean))
    has_deadline = bool(DEADLINE_PHRASE_RE.search(clean))

    # Newspaper / trade-press pages are not casting notices.  Even an "open
    # casting call" mentioned in an article is past-tense coverage unless the
    # page also carries a real deadline to apply by.  A byline e-mail is not a
    # way to apply, so contact alone is not enough here.
    if is_news_url(url) and not (casting_call and has_deadline):
        return True

    # "Open call finalists", "jury announced", "showreel tips" are coverage of
    # a finished process or general advice, not a call to apply.
    if re.search(r"\b(?:finalists?|winners?|shortlist(?:ed)?)\b", lower) and re.search(
        r"\b(?:open\s+call|audition|casting|call\s+for\s+entries)\b", lower
    ):
        return True

    hard = sum(1 for phrase in NEWS_HARD_PHRASES if phrase in lower)
    weak = sum(1 for phrase in NEWS_WEAK_PHRASES if phrase in lower)

    if casting_call:
        # A genuine casting call keeps passing unless it reads like a write-up
        # with several news markers and no deadline/contact of its own.
        return hard >= 3 and not (has_contact or has_deadline)
    if hard >= 1:
        return True
    if weak >= 3:
        return True
    return False
