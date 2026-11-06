"""Near-duplicate detection and clustering."""

from .cluster import assign_cluster
from .minhash import estimate_jaccard, signature_for_text, text_similarity
from .structured import fuzzy_match, make_structured_key

__all__ = [
    "assign_cluster",
    "estimate_jaccard",
    "signature_for_text",
    "text_similarity",
    "fuzzy_match",
    "make_structured_key",
]
