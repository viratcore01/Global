"""Embedding fit with a deterministic fallback.

The report specifies multilingual-e5-small.  It is optional because it pulls a
large PyTorch dependency; when sentence-transformers is unavailable the core
pipeline still works with a signed feature-hashing embedder over words,
character trigrams and a small Hindi/English synonym map.  The method is stored
with every vector so scores remain explainable.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import threading
from collections import Counter
from typing import Any, Sequence

from ..config import Profile, Settings
from ..util import clamp, get_logger, normalize_ws

log = get_logger(__name__)

FALLBACK_DIM = 384
_SYNONYMS = {
    "casting": ("audition", "open call", "cast", "role"),
    "audition": ("casting", "open call", "self tape"),
    "actor": ("performer", "artist", "acting"),
    "actress": ("actor", "performer", "acting"),
    "short": ("shortfilm", "short film"),
    "film": ("movie", "cinema", "shortfilm"),
    "web": ("webseries", "series", "ott"),
    "voice": ("voiceover", "dubbing", "narration"),
    "paid": ("payment", "fee", "day rate", "remuneration"),
    "delhi": ("new delhi", "ncr", "noida", "gurugram"),
    "mumbai": ("bombay",),
    "bengaluru": ("bangalore",),
    "self": ("selftape", "self tape"),
    "tape": ("selftape", "self tape"),
    "hindi": ("hinglish", "bollywood"),
    "festival": ("submission", "entry", "award"),
}

_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[str, Any] = {}


def _tokens(text: str) -> list[str]:
    text = normalize_ws(text or "").lower()
    words = re.findall(r"[a-z\u0900-\u097F0-9]+", text)
    expanded = list(words)
    for word in words:
        expanded.extend(_SYNONYMS.get(word, ()))
    return expanded


def _hash_index(token: str, dim: int, salt: str = "") -> tuple[int, float]:
    digest = hashlib.blake2b((salt + token).encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    index = value % dim
    sign = 1.0 if (value >> 63) & 1 else -1.0
    return index, sign


def hashing_embedding(text: str, *, dim: int = FALLBACK_DIM) -> list[float]:
    vector = [0.0] * dim
    tokens = _tokens(text)
    if not tokens:
        return vector
    counts = Counter(tokens)
    for token, count in counts.items():
        weight = 1.0 + math.log(count)
        index, sign = _hash_index(token, dim)
        vector[index] += sign * weight
        # Character trigrams help with inflections and transliteration noise.
        padded = f"^{token}$"
        for i in range(max(0, len(padded) - 2)):
            gram = padded[i : i + 3]
            gi, gs = _hash_index(gram, dim, salt="g:")
            vector[gi] += gs * 0.35 * weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def vector_to_bytes(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *[float(v) for v in vector])


def bytes_to_vector(blob: bytes) -> list[float]:
    if not blob:
        return []
    count = len(blob) // 4
    return list(struct.unpack(f"<{count}f", blob[: count * 4]))


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0 or nb <= 0:
        return 0.0
    return clamp(dot / (na * nb), -1.0, 1.0)


def _load_sentence_model(model_name: str) -> Any | None:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore
    except Exception:
        return None
    with _MODEL_LOCK:
        if model_name in _MODEL_CACHE:
            return _MODEL_CACHE[model_name]
        try:
            model = SentenceTransformer(model_name)
            _MODEL_CACHE[model_name] = model
            return model
        except Exception as exc:  # pragma: no cover - model download
            log.warning("Could not load embedding model %s: %s", model_name, exc)
            _MODEL_CACHE[model_name] = None
            return None


class Embedder:
    def __init__(self, settings: Settings | None = None, *, dim: int = FALLBACK_DIM):
        self.settings = settings
        self.dim = dim
        configured = (settings.embedding_model if settings else "auto") or "auto"
        self.model_name = "hashing-384"
        self._sentence = None
        self._dim = dim
        if configured and configured.lower() not in {"none", "hash", "hashing", "fallback", "hashing-384"}:
            name = "intfloat/multilingual-e5-small" if configured.lower() == "auto" else configured
            model = _load_sentence_model(name)
            if model is not None:
                self._sentence = model
                self.model_name = name
                try:
                    self._dim = int(model.get_sentence_embedding_dimension() or 384)
                except Exception:
                    self._dim = 384

    @property
    def is_semantic(self) -> bool:
        return self._sentence is not None

    def encode(self, text: str, *, query: bool = False) -> list[float]:
        if self._sentence is None:
            return hashing_embedding(text, dim=self.dim)
        prefix = "query: " if query else "passage: "
        try:
            vector = self._sentence.encode(prefix + normalize_ws(text), normalize_embeddings=True)
            return [float(value) for value in vector]
        except Exception as exc:  # pragma: no cover
            log.warning("Embedding failed; falling back to hashing: %s", exc)
            return hashing_embedding(text, dim=self.dim)

    def mean(self, texts: Sequence[str], *, query: bool = False) -> list[float]:
        vectors = [self.encode(text, query=query) for text in texts if normalize_ws(text)]
        if not vectors:
            return [0.0] * self.dim
        if len(vectors) == 1:
            return vectors[0]
        length = len(vectors[0])
        out = [0.0] * length
        for vector in vectors:
            for i, value in enumerate(vector):
                out[i] += value
        return [value / len(vectors) for value in out]


def profile_query_text(profile: Profile, applied_texts: Sequence[str] | None = None) -> str:
    parts = [profile.to_text()]
    # Past Apply items are the strongest signal about taste.  Keep this to a
    # small, role-relevant list and never include personal contact details.
    for text in (applied_texts or [])[:8]:
        parts.append(normalize_ws(text)[:600])
    return "\n".join(parts)


def embedding_fit(
    opportunity: dict[str, Any],
    profile: Profile,
    *,
    settings: Settings | None = None,
    applied_texts: Sequence[str] | None = None,
    embedder: Embedder | None = None,
    profile_vector: Sequence[float] | None = None,
) -> tuple[float, str]:
    embedder = embedder or Embedder(settings)
    text = "\n".join(
        str(opportunity.get(key) or "")
        for key in ("title", "description", "city", "country", "comp_type")
    )
    text += "\n" + " ".join(opportunity.get("role_types") or [])
    text += "\n" + " ".join(opportunity.get("languages") or [])
    opp_vector = embedder.encode(text)
    if profile_vector is None:
        profile_vector = embedder.encode(profile_query_text(profile, applied_texts), query=True)
    # Map cosine [-1, 1] to [0, 1]; hashing vectors often sit near zero.
    similarity = (cosine(opp_vector, profile_vector) + 1.0) / 2.0
    return clamp(similarity), embedder.model_name
