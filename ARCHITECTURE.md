# Architecture

One Python package, one SQLite file, one worker process. Everything else is a
client of those three things.

```
                        ┌──────────────────────────────┐
                        │  config/  profile.yaml        │
                        │  sources.yaml  veto_patterns  │
                        └──────────────┬───────────────┘
                                       │
        ┌──────────────────────────────▼───────────────────────────────┐
        │                        radar.jobs / cli                      │
        │  discover → ingest → score → alert → digest → health → backup│
        └───────┬───────────────┬───────────────┬──────────────┬───────┘
                │               │               │              │
      ┌─────────▼──────┐ ┌──────▼───────┐ ┌─────▼──────┐ ┌─────▼──────┐
      │ discovery/*    │ │ pipeline.py  │ │ score/*    │ │ alert/*    │
      │ adapters       │ │ fetch+extract│ │ gate+rank  │ │ ntfy+reply │
      └─────────┬──────┘ └──────┬───────┘ └─────┬──────┘ └─────┬──────┘
                │               │               │              │
                └───────────────▼───────────────▼──────────────┘
                              radar.repo (all SQL)
                                       │
                              ┌────────▼────────┐
                              │ data/radar.db   │  WAL, FTS5, migrations
                              └─────────────────┘
```

## Module map

| Module | Responsibility |
| --- | --- |
| `radar/config.py` | `Paths.discover()`, `Settings.from_env()`, `Profile`, `SourceConfig`, `VetoConfig`, `AppConfig.load()` |
| `radar/db.py` | Thread-safe SQLite wrapper, migrations from `radar/migrations/*.sql`, WAL, backup primitives |
| `radar/repo.py` | Every SQL statement in the project. Callers get decoded dicts. |
| `radar/pipeline.py` | `Pipeline.ingest(url, source, kind, payload)`: fetch → raw document → extraction → dedup → store → score |
| `radar/extract/` | Structured data (`jsonld.py`), source CSS (`css.py`), main text (`maintext.py`), dates, heuristics, OCR hook, LLM gap filler, orchestrator |
| `radar/extract/evidence.py` | Verifies every quoted value exists in the source text before it is kept |
| `radar/fetch/` | `HttpFetcher`, robots cache, token buckets, optional browser/curl_cffi escalation |
| `radar/discovery/` | One adapter per source type plus the registry in `adapters.py` and the queue contract in `base.py` |
| `radar/dedup/` | URL canonicalisation live in `normalize.py`; MinHash, structured keys and cluster assignment here |
| `radar/score/` | Eligibility gate → vetoes → risk penalties → features → ranker; `learning.py` is the logistic model |
| `radar/alert/` | Message formatting, instant/digest routing, ntfy client, HMAC actions, reply listener |
| `radar/tracking.py` | Status state machine and label capture (feature snapshot per label) |
| `radar/jobs.py` | Side-effect-free job functions used by CLI, scheduler and worker |
| `radar/scheduler.py` | `JobSpec` list with persisted `job_runs` bookkeeping |
| `radar/worker.py` | Long-running loop: due jobs, reply polling, optional IMAP IDLE, graceful shutdown |
| `radar/health.py` | Doctor checks, source warnings, snapshot, heartbeat ping |
| `radar/learning.py` | Weekly metrics, threshold recommendation, recall audit |
| `radar/backup.py` | `sqlite3 .backup` snapshots, retention, integrity verification, restore |
| `radar/cli.py` | Argparse surface; all commands accept `--json` |

## Data flow

1. **Discover.** An adapter turns a source into queue entries
   (`discovery_queue`: `source_id`, `kind`, `url`, `payload_json`, `run_after`).
   Dedupe happens on canonical URL + source, so a repeated RSS item is ignored.
2. **Fetch.** The pipeline fetches the URL with the polite HTTP client, stores the
   raw body (compressed) in `raw_documents`, and records status/headers. Failures
   mark the queue item with a retry delay; robots denials mark the source blocked.
3. **Extract.** `extract/pipeline.py` tries structured data first, then a source
   CSS extractor, then heuristics over main text. OCR and LLM are opt-in and run
   only for missing fields. Pydantic validation failures go to `quarantine` with
   the raw id, so nothing is lost.
4. **Dedup.** Canonical URL → exact hash → structured fuzzy key → MinHash LSH →
   Jaccard recheck. A match joins an existing cluster; a new item creates one.
   Cluster source count feeds trust.
5. **Decide.** `Ranker.score()`:
   - eligibility gate against `profile.yaml` (unknowns pass, conflicts reject);
   - hard vetoes from `config/veto_patterns.yaml` + reputation + expiry;
   - risk penalties (WhatsApp-only, freemail, urgency, unrealistic claims…);
   - features: embedding 0.35, BM25 0.25, trust 0.20, urgency 0.10,
     completeness 0.10 (weights normalised, configurable);
   - optional logistic model blended 75/25 once ≥100 labels exist;
   - final score = weighted × 100 − risk penalty, clamped 0–100.
   Every component and veto reason is written to `score_breakdown_json` / `veto_log`.
6. **Alert.** Instant if score ≥ `thresholds.instant`, completeness ≥
   `min_completeness`, no risk flags, outside quiet hours and under the daily cap.
   Otherwise the item is a digest candidate. `RADAR_INSTANT_THRESHOLD` and
   `RADAR_DIGEST_THRESHOLD` in `.env` override the profile values; `doctor`
   prints which source is in effect. Both paths write to `alerts` with a
   dedupe key so a restarted worker cannot double-send.
7. **Track.** Apply / Ignore / Scam buttons carry an HMAC-signed payload. The
   reply listener verifies it, `tracking.apply_action()` moves the status through
   the state machine, records a `labels` row with the feature snapshot, and a
   Scam label writes a `reputation` entry that vetoes that contact everywhere.
8. **Learn.** `weekly` computes precision@15, outcome rate, scam catch rate,
   quarantine growth and time-to-alert; `refit` trains the logistic model from
   label snapshots.

## Storage invariants

- SQLite runs in WAL mode with `busy_timeout=5000`; exactly **one writer**
  (worker or CLI), any number of read-only dashboard readers.
- Migrations are numbered SQL files applied in order and recorded in
  `schema_migrations`. Never edit an applied migration; add a new one.
- Raw documents are immutable. Re-extraction is `replay --raw-id N`.
- Clusters never delete members; duplicates are links, not removals.
- FTS5 (`opportunities_fts`) is kept in sync by triggers and uses
  `unicode61 remove_diacritics 2` so Hindi/Hinglish text is searchable.
- Labels store the score features at decision time, so later model changes do not
  rewrite history.

## Failure model

| Failure | Behaviour |
| --- | --- |
| Network error / 5xx | Retry with jitter; after `max_attempts` the queue item is failed, not dropped |
| robots.txt denies | Source marked `blocked`; no further fetches until changed |
| Extraction too thin | Quarantine row with reason and raw id; visible in digest and dashboard |
| One bad record during scoring | Logged, batch continues |
| ntfy down | Alert stays unsent; next cycle retries (dedupe key prevents duplicates) |
| Worker crash | `job_runs` shows the last run; queue leases expire and are requeued |
| DB corruption | `backup verify` detects it; restore from `data/backups` or Litestream |

## Extension points

- **New source type**: subclass the adapter base, register it in
  `discovery/adapters.py`, document it in [docs/SOURCES.md](docs/SOURCES.md).
- **New scoring feature**: compute it in `score/features.py`, add a weight to
  `Profile.weights`, expose detail in the breakdown. Keep it explainable.
- **New veto rule**: add it to `config/veto_patterns.yaml` first; only add code
  when the rule needs database or profile context.
- **New alert channel**: implement `publish()` like `alert/ntfy.py` and route
  through `AlertRouter`; keep the signed-action contract.

## Deliberate non-goals

Auto-applying, login-walled scraping, CAPTCHA solving, social-platform scrapers,
a hosted multi-user service, and any design where an LLM decides whether an
opportunity is real, paid or safe.
