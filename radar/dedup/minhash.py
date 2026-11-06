"""MinHash signatures and Jaccard estimates for near-duplicate posts.

The report calls for 5-word shingles on long text and 3-word shingles on short
texts.  We use datasketch's MinHash implementation when installed; a compact
pure-Python fallback keeps the core pipeline usable without optional packages.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable, Sequence

from ..normalize import normalize_ws

NUM_PERM = 96
NEAR_DUPLICATE_THRESHOLD = 0.70
SHORT_TEXT_WORDS = 120


def _tokens(text: str) -> list[str]:
    text = normalize_ws(text).lower()
    # Keep Devanagari and Latin letters plus digits.  Punctuation is noise for
    # shingling but rupee amounts and ages are useful, so digits stay.
    text = re.sub(r"[^\w\u0900-\u097F]+", " ", text, flags=re.UNICODE)
    return [token for token in text.split() if token]


def shingles(text: str, *, size: int | None = None) -> list[str]:
    tokens = _tokens(text)
    if not tokens:
        return []
    if size is None:
        size = 3 if len(tokens) < SHORT_TEXT_WORDS else 5
    size = max(1, int(size))
    if len(tokens) < size:
        return [" ".join(tokens)]
    return [" ".join(tokens[i : i + size]) for i in range(0, len(tokens) - size + 1)]


def _stable_hash(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _fallback_signature(values: Iterable[str], num_perm: int = NUM_PERM) -> list[int]:
    shingle_hashes = {_stable_hash(value) for value in values}
    if not shingle_hashes:
        return [0] * num_perm
    signature: list[int] = []
    for seed in range(num_perm):
        prefix = f"{seed}:".encode("ascii")
        signature.append(
            min(int.from_bytes(hashlib.blake2b(prefix + str(h).encode("ascii"), digest_size=8).digest(), "big")
                for h in shingle_hashes)
        )
    return signature


def signature_for_text(text: str, *, num_perm: int = NUM_PERM) -> str:
    """Return a JSON-serialisable MinHash signature for storage."""
    values = shingles(text)
    try:
        from datasketch import MinHash  # type: ignore

        mh = MinHash(num_perm=num_perm)
        for value in values:
            mh.update(value.encode("utf-8"))
        signature = [int(v) for v in mh.hashvalues]
    except Exception:
        signature = _fallback_signature(values, num_perm=num_perm)
    return json.dumps(signature, separators=(",", ":"))


def parse_signature(value: str | Sequence[int] | None) -> list[int]:
    if not value:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [int(v) for v in parsed]
        return []
    return [int(v) for v in value]


def estimate_jaccard(left: str | Sequence[int] | None, right: str | Sequence[int] | None) -> float:
    a = parse_signature(left)
    b = parse_signature(right)
    if not a or not b or len(a) != len(b):
        return 0.0
    matches = sum(1 for x, y in zip(a, b) if x == y)
    return matches / len(a)


def text_similarity(left: str, right: str) -> float:
    """Exact Jaccard similarity on the selected shingle size.

    This is the expensive recheck after the cheap signature comparison.
    """
    a = set(shingles(left))
    b = set(shingles(right))
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
