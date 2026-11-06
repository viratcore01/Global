"""Polite fetch layer: HTTP first, escalate only when a source needs it."""

from .http import FetchResult, HttpFetcher
from .robots import RobotsCache

__all__ = ["FetchResult", "HttpFetcher", "RobotsCache"]
