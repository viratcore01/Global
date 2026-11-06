"""Core ingestion pipeline: fetch -> raw store -> extract -> dedup -> score."""

from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping

from . import repo
from .config import AppConfig, SourceConfig
from .db import Database
from .dedup import assign_cluster
from .dedup.minhash import signature_for_text
from .extract.pipeline import ExtractionEngine, ExtractionResult
from .fetch.http import FetchResult, HttpFetcher
from .models import OpportunityRecord
from .normalize import canonicalize_url
from .score.ranker import Ranker, ScoreResult
from .util import (
    backoff_delay,
    get_logger,
    iso,
    json_dumps,
    now_utc,
    sha256_bytes,
    slugify,
)

log = get_logger(__name__)

INTERNAL_SCHEMES = ("email://", "fixture://", "discord://", "telegram://", "rss://", "imap://")
COMPRESS_THRESHOLD = 8192


@dataclass
class IngestedRecord:
    opportunity_id: int
    record: OpportunityRecord
    is_new: bool
    cluster: dict[str, Any] = field(default_factory=dict)
    score: ScoreResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "opportunity_id": self.opportunity_id,
            "title": self.record.title,
            "is_new": self.is_new,
            "cluster": self.cluster,
            "score": self.score.to_dict() if self.score else None,
        }


@dataclass
class IngestionResult:
    url: str
    raw_document_id: int | None = None
    records: list[IngestedRecord] = field(default_factory=list)
    skipped: bool = False
    error: str | None = None
    reason: str = ""
    http_status: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "raw_document_id": self.raw_document_id,
            "records": [record.to_dict() for record in self.records],
            "skipped": self.skipped,
            "error": self.error,
            "reason": self.reason,
            "http_status": self.http_status,
        }


class Pipeline:
    def __init__(
        self,
        config: AppConfig,
        db: Database,
        *,
        fetcher: HttpFetcher | None = None,
        allow_llm: bool = False,
    ):
        self.config = config
        self.db = db
        self.fetcher = fetcher or HttpFetcher(config.settings)
        self.engine = ExtractionEngine(
            config.settings,
            image_fetcher=self._fetch_image,
        )
        self.ranker = Ranker(config, db)
        self.allow_llm = allow_llm

    def close(self) -> None:
        self.fetcher.close()

    def _fetch_image(self, url: str) -> bytes | None:
        result = self.fetcher.fetch(url)
        return result.body if result.ok else None

    # -- single item -------------------------------------------------------
    def ingest(
        self,
        url: str,
        source: Mapping[str, Any] | SourceConfig | None = None,
        *,
        kind: str = "page",
        payload: dict[str, Any] | None = None,
        update_fields: bool = False,
    ) -> IngestionResult:
        source_dict = self._source_dict(source)
        payload = payload or {}
        if url.startswith(INTERNAL_SCHEMES):
            raw = self._store_internal(url, kind, payload, source_dict)
            return self._extract_and_store(
                raw,
                source_dict,
                update_fields=update_fields,
                fallback_text=str(payload.get("text") or ""),
            )

        canonical = canonicalize_url(url) or url
        response = self.fetcher.fetch(canonical)
        if not response.ok:
            fallback_text = str(payload.get("text") or "")
            fallback_html = str(payload.get("html") or "")
            if (fallback_text or fallback_html) and "robots disallowed" not in (response.error or ""):
                # Feeds and search snippets often contain enough to extract even
                # when the target page is blocked, dead or slow.
                raw = self._store_payload(
                    canonical,
                    kind,
                    payload,
                    source_dict,
                    http_status=response.status,
                    error=response.error,
                )
                return self._extract_and_store(
                    raw,
                    source_dict,
                    update_fields=update_fields,
                    fallback_text=fallback_text,
                )
            return IngestionResult(
                url=canonical,
                error=response.error or f"HTTP {response.status}",
                http_status=response.status,
            )

        final_url = canonicalize_url(response.final_url or canonical) or canonical
        body = response.body or b""
        body_text = response.text or body.decode("utf-8", errors="replace")
        content_hash = sha256_bytes(body) if body else None
        earlier = repo.recent_raw_for_url(self.db, final_url, limit=1)
        if earlier and content_hash and earlier[0].get("content_hash") == content_hash:
            existing = repo.get_opportunity_by_url(self.db, final_url)
            if existing:
                db_update = repo.upsert_opportunity(
                    self.db,
                    {**existing, "canonical_url": final_url, "source_id": source_dict.get("id") if source_dict else None},
                    update_fields=False,
                )
                return IngestionResult(
                    url=final_url,
                    raw_document_id=int(earlier[0]["id"]),
                    records=[
                        IngestedRecord(
                            opportunity_id=db_update[0],
                            record=OpportunityRecord.model_validate({
                                **existing,
                                "evidence": existing.get("evidence") or {},
                                "risk_flags": existing.get("risk_flags") or [],
                            }),
                            is_new=False,
                            cluster={"reason": "unchanged_raw"},
                        )
                    ],
                    reason="content unchanged",
                )

        compressed = len(body) > COMPRESS_THRESHOLD
        stored_body = zlib.compress(body, level=6) if compressed else body
        meta = {
            "title": payload.get("title"),
            "published_at": payload.get("published_at"),
            "author": payload.get("author"),
            **(payload.get("meta") or {}),
        }
        raw_id = repo.insert_raw_document(
            self.db,
            source_id=source_dict.get("id") if source_dict else None,
            url=url,
            canonical_url=final_url,
            content_type=response.content_type,
            http_status=response.status,
            headers=response.headers,
            body=stored_body,
            body_text=body_text,
            content_hash=content_hash,
            kind=kind,
            meta=meta,
            compressed=compressed,
        )
        raw = repo.get_raw_document(self.db, raw_id) or {
            "id": raw_id,
            "canonical_url": final_url,
            "body_text": body_text,
            "content_type": response.content_type,
            "meta": meta,
            "kind": kind,
            "source_id": source_dict.get("id") if source_dict else None,
        }
        return self._extract_and_store(
            raw,
            source_dict,
            update_fields=update_fields,
            fallback_text=body_text,
            http_status=response.status,
        )

    def replay_raw(self, raw_id: int) -> IngestionResult:
        raw = repo.get_raw_document(self.db, raw_id)
        if not raw:
            return IngestionResult(url="", error=f"raw document {raw_id} not found")
        source = repo.get_source(self.db, int(raw["source_id"])) if raw.get("source_id") else None
        return self._extract_and_store(
            raw,
            source or {"type": "web", "tier": 3},
            update_fields=True,
            fallback_text=str(raw.get("body_text") or ""),
        )

    # -- raw helpers -------------------------------------------------------
    def _source_dict(self, source: Mapping[str, Any] | SourceConfig | None) -> dict[str, Any]:
        if source is None:
            return {"id": None, "type": "web", "tier": 3, "name": "manual"}
        if isinstance(source, SourceConfig):
            return {**source.to_dict(), "id": None}
        return dict(source)

    def _store_internal(
        self,
        url: str,
        kind: str,
        payload: dict[str, Any],
        source: dict[str, Any],
    ) -> dict[str, Any]:
        body_text = str(payload.get("text") or "")
        html = str(payload.get("html") or "")
        body_text = body_text or html
        content_type = "text/html" if html or "<html" in body_text.lower() else "text/plain"
        body = body_text.encode("utf-8", errors="replace")
        compressed = len(body) > COMPRESS_THRESHOLD
        stored = zlib.compress(body, level=6) if compressed else body
        raw_id = repo.insert_raw_document(
            self.db,
            source_id=source.get("id"),
            url=url,
            canonical_url=url,
            content_type=content_type,
            http_status=None,
            headers={},
            body=stored,
            body_text=body_text,
            content_hash=sha256_bytes(body),
            kind=kind,
            meta={
                "title": payload.get("title"),
                "published_at": payload.get("published_at"),
                "author": payload.get("author") or payload.get("sender"),
                "channel": payload.get("channel"),
                "sender": payload.get("sender"),
                "source_url": payload.get("source_url"),
                **{k: v for k, v in payload.items() if k in {"search_query", "feed_url", "email_subject"}},
            },
            compressed=compressed,
        )
        return repo.get_raw_document(self.db, raw_id) or {
            "id": raw_id,
            "canonical_url": url,
            "body_text": body_text,
            "content_type": content_type,
            "meta": payload,
            "kind": kind,
            "source_id": source.get("id"),
        }

    def _store_payload(
        self,
        url: str,
        kind: str,
        payload: dict[str, Any],
        source: dict[str, Any],
        *,
        http_status: int | None,
        error: str | None,
    ) -> dict[str, Any]:
        body_text = str(payload.get("text") or "")
        html = str(payload.get("html") or "")
        if html and not body_text:
            body_text = html
        body = body_text.encode("utf-8", errors="replace")
        compressed = len(body) > COMPRESS_THRESHOLD
        raw_id = repo.insert_raw_document(
            self.db,
            source_id=source.get("id"),
            url=url,
            canonical_url=canonicalize_url(url) or url,
            content_type="text/html" if html else "text/plain",
            http_status=http_status,
            headers={"x-radar-fetch-error": error or ""},
            body=zlib.compress(body, 6) if compressed else body,
            body_text=body_text,
            content_hash=sha256_bytes(body),
            kind=kind,
            meta={"title": payload.get("title"), "published_at": payload.get("published_at"), "fetch_error": error},
            compressed=compressed,
        )
        return repo.get_raw_document(self.db, raw_id) or {
            "id": raw_id,
            "canonical_url": url,
            "body_text": body_text,
            "content_type": "text/html" if html else "text/plain",
            "meta": payload,
            "kind": kind,
            "source_id": source.get("id"),
        }

    # -- extraction / persistence -----------------------------------------
    def _extract_and_store(
        self,
        raw: dict[str, Any],
        source: dict[str, Any],
        *,
        update_fields: bool,
        fallback_text: str = "",
        http_status: int | None = None,
    ) -> IngestionResult:
        extraction: ExtractionResult = self.engine.extract(raw, source, allow_llm=self.allow_llm)
        url = str(raw.get("canonical_url") or raw.get("url") or "")
        if extraction.skipped:
            return IngestionResult(
                url=url,
                raw_document_id=raw.get("id"),
                skipped=True,
                reason=extraction.reason or "no opportunity signals",
                http_status=http_status,
            )
        if extraction.errors:
            repo.add_quarantine(
                self.db,
                raw_document_id=raw.get("id"),
                source_id=source.get("id"),
                url=url,
                errors=extraction.errors,
                payload={"title": extraction.text[:500], "source": source.get("name")},
            )
        if not extraction.records:
            return IngestionResult(
                url=url,
                raw_document_id=raw.get("id"),
                error="extraction produced no valid records",
                reason=extraction.reason or "validation failed",
                http_status=http_status,
            )

        records: list[IngestedRecord] = []
        for record in extraction.records:
            data = record.to_repo_dict()
            canonical_url = data.get("canonical_url") or url
            if canonical_url.startswith(("http://", "https://")):
                canonical_url = canonicalize_url(canonical_url) or canonical_url
            text_for_signature = f"{data.get('title', '')}\n{data.get('description', '')}"
            data["minhash_signature"] = signature_for_text(text_for_signature)
            data["canonical_url"] = canonical_url
            data["source_id"] = source.get("id") or data.get("source_id")
            data["source_type"] = source.get("type") or data.get("source_type") or "web"
            data["trust_tier"] = int(source.get("tier") or data.get("trust_tier") or 3)
            opp_id, is_new = repo.upsert_opportunity(self.db, data, update_fields=update_fields)
            existing = repo.get_opportunity(self.db, opp_id)
            cluster = {"reason": "existing"}
            if is_new or not existing.get("cluster_id"):
                cluster = assign_cluster(self.db, existing or data, opportunity_id=opp_id)
            score_result = None
            refreshed = repo.get_opportunity(self.db, opp_id)
            if refreshed and (is_new or not refreshed.get("score_breakdown")):
                try:
                    score_result = self.ranker.score(refreshed)
                except Exception as exc:
                    log.warning("Scoring failed for %s: %s", opp_id, exc)
            records.append(
                IngestedRecord(
                    opportunity_id=opp_id,
                    record=record,
                    is_new=is_new,
                    cluster=cluster,
                    score=score_result,
                )
            )
        return IngestionResult(
            url=url,
            raw_document_id=raw.get("id"),
            records=records,
            http_status=http_status,
        )

    # -- queue -------------------------------------------------------------
    def process_queue(self, *, limit: int = 25) -> dict[str, Any]:
        items = repo.claim_queue(self.db, limit=limit)
        stats = {"claimed": len(items), "processed": 0, "failed": 0, "records": 0, "pending": 0}
        for item in items:
            source = repo.get_source(self.db, int(item["source_id"]))
            if not source:
                repo.mark_queue(self.db, int(item["id"]), "failed", error="source missing")
                stats["failed"] += 1
                continue
            result = self.ingest(
                item["url"],
                source,
                kind=str(item.get("kind") or "page"),
                payload=item.get("payload") or {},
            )
            if result.error and not result.records:
                attempts = int(item.get("attempts") or 1)
                if attempts >= 4:
                    repo.mark_queue(self.db, int(item["id"]), "failed", error=result.error)
                    stats["failed"] += 1
                else:
                    delay = backoff_delay(attempts, base=60, cap=3600)
                    repo.mark_queue(
                        self.db,
                        int(item["id"]),
                        "pending",
                        error=result.error,
                        retry_after=now_utc() + timedelta(seconds=delay),
                    )
                    stats["pending"] += 1
            else:
                repo.mark_queue(self.db, int(item["id"]), "done", error=result.error)
                stats["processed"] += 1
                stats["records"] += len(result.records)
        return stats
