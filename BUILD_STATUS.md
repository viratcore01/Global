# Global Cinema Opportunity Radar — Build Status

> This file is the project's living memory. Every completed component and every remaining task is tracked here.
> Update this file after every build milestone. Last updated: **2026-11-06**.

## Legend

- [x] Built and tested
- [ ] Not built yet
- [~] Built, needs real-world credential/source configuration (cannot be verified without the user's accounts)

---

## 0. Project foundation

- [x] Repository layout (`radar/` package, `config/`, `tests/`, `deploy/`, `scripts/`)
- [x] Dependency manifest (`requirements.txt`, optional extras)
- [x] `.env.example` and secret handling
- [x] YAML loading with PyYAML/ruamel fallback
- [x] Structured logging
- [x] Cross-platform paths and timezone handling (Asia/Kolkata, fixed offset, no tzdata dependency)
- [x] Python virtual environment created at `.venv`
- [x] Living build tracker (this file)

## 1. Configuration

- [x] `config/profile.yaml` with every profile field from the design report
- [x] `config/sources.yaml` with seed structure, disabled-by-default examples, safe defaults
- [x] `config/veto_patterns.yaml` with deterministic scam/veto patterns
- [x] Typed settings loader (`.env`, defaults, validation)
- [x] Source configuration loader + validation
- [x] Veto pattern loader + regex compilation
- [x] Path discovery (`RADAR_HOME`, project root)
- [x] Config `doctor` validation

## 2. Storage layer

- [x] SQLite schema with migrations from day one
- [x] WAL mode, busy_timeout=5000, synchronous=NORMAL
- [x] Tables: `raw_documents`, `opportunities`, `clusters`, `sources`, `source_health`, `labels`, `reputation`, `veto_log`, `quarantine`, `alerts`, `cdx_runs`, `discovery_queue`, `sitemap_urls`, `hub_candidates`, `embeddings`, `source_stats`, `reply_messages`, `job_runs`, `domain_intel`, `weekly_reports`, `settings`
- [x] FTS5 index with `unicode61 remove_diacritics 2` + triggers
- [x] Repository functions for every table
- [x] Nightly `sqlite3 .backup` snapshots + retention + integrity check
- [x] Backup restore verification command
- [x] Litestream deployment config

## 3. Fetch layer

- [x] Honest HTTP client with bot User-Agent and contact URL
- [x] `curl_cffi` browser-fingerprint escalation (optional)
- [x] Playwright/Camoufox JS rendering (optional)
- [x] Scrapling adaptive fallback (optional)
- [x] Per-domain token bucket, global concurrency cap
- [x] robots.txt + Crawl-delay enforcement + cache
- [x] Exponential backoff with jitter, Retry-After
- [x] Redirect/HTTPS safety, size limits, content-type handling
- [x] Raw body compression + replay support

## 4. Extraction layer

- [x] extruct JSON-LD / Microdata / OpenGraph extraction
- [x] selectolax source-specific CSS/XPath extractor (config-driven)
- [x] trafilatura main-text extraction
- [x] Deterministic field heuristics (deadline, city, pay, age, gender, language, role, contact, nudity, rights, shoot window, remote, fee, travel, audition format)
- [x] Hindi / Hinglish date and field support
- [x] dateparser with Asia/Kolkata, DMY, relative dates, end-of-day deadlines
- [x] Pydantic schema validation
- [x] Quarantine on validation failure
- [x] Per-field evidence spans
- [x] Span grounding verification (invalid evidence nulls the field)
- [x] OCR hooks (RapidOCR / Tesseract/PaddleOCR) for poster images
- [x] LLM gap-filling provider interface (Groq/OpenAI-compatible/Ollama), strict JSON, no tools, span-verified
- [x] Prompt-injection delimiting
- [x] Extraction method + LLM model recorded per opportunity

## 5. Deduplication and clustering

- [x] Canonical URL normalisation (HTTPS, www/m/AMP, tracking params, fragments, trailing slash)
- [x] Exact URL SHA-256 match
- [x] MinHash LSH near-duplicate matching (5-word/3-word shingles, Jaccard recheck, 0.7 threshold)
- [x] Structured fuzzy key (title + city + deadline + contact)
- [x] Cluster, never delete: every source kept
- [x] Corroboration count feeds trust score
- [x] Cluster representative selection

## 6. Decision engine

- [x] `profile.yaml`-driven eligibility gate (age, gender, language, location/travel radius, remote/self-tape, nudity, paid-only)
- [x] Haversine city distance for travel radius
- [x] Hard vetoes (expired, pay-to-apply, guaranteed outcome, paid portfolio, join-group funnel, reputation, unpaid when paid-only, perpetual buyout)
- [x] Festival entry fees are stored, not vetoed
- [x] Risk penalties (WhatsApp-only, freemail, new domain via RDAP, no contact, urgency pressure, unrealistic claims)
- [x] BM25 via SQLite FTS5
- [x] Embedding fit with multilingual-e5-small when installed; deterministic hashing embedder fallback
- [x] Source trust + corroboration scoring
- [x] Urgency + completeness scoring
- [x] Weighted score 35/25/20/10/10 minus penalties
- [x] Logistic-regression refit after 100 labels
- [x] Every score component + veto reason logged for explainability

## 7. Discovery layer

- [x] Discovery queue with dedupe, retries, run-after
- [x] RSS / Atom (feedparser)
- [x] Sitemap diffing (robots.txt, sitemap index, gzip, lastmod)
- [x] Telegram public preview pages (`t.me/s/`, pagination)
- [x] Email IMAP IDLE (`imap-tools`, BODY.PEEK, reconnect, periodic full poll)
- [x] SearXNG sweeps (configurable queries, date filtering in code)
- [x] Google Alerts RSS/email ingestion
- [x] Config-driven HTML listing discovery (CSS link selectors)
- [x] Outbound-link graph with manual hub approval
- [x] Wayback CDX with pagination, throttle, `cdx_runs`
- [x] Discord read-only bot (only invited servers, Message Content intent)
- [x] Social connectors deliberately deferred (documented as fragile)

## 8. Alerts and tracking

- [x] ntfy publishing with auth
- [x] Apply / Ignore / Scam HTTP action buttons to a private reply topic
- [x] HMAC-signed action payloads
- [x] Reply listener (no inbound port), message dedupe
- [x] Status state machine (new → applied/replied/booked; ignored; scam; expired; archived)
- [x] Label capture with feature snapshot
- [x] Scam reputation table auto-vetoes phone/email/domain everywhere
- [x] Instant alert gating (score, completeness, risk flag, daily cap 15, quiet hours)
- [x] Daily 08:00 IST digest
- [x] Exploration slots to avoid biased learning
- [x] Quarantine count in digest

## 9. Health, learning, operations

- [x] Source health counters, selector-drift detection, dead-source detection, silence detection
- [x] External heartbeat (healthchecks.io)
- [x] Weekly refit
- [x] Precision@15, outcome rate, scam catch rate, quarantine growth, time-to-alert
- [x] Recall audit sampling of vetoed/low-ranked items
- [x] Scheduler with persisted job runs
- [x] Worker with graceful shutdown and crash-safe loops
- [x] Nightly snapshot, weekly report
- [x] `doctor` self-check

## 10. Interfaces

- [x] Full CLI (`init`, `discover`, `run`, `worker`, `score`, `digest`, `label`, `replay`, `health`, `backup`, `refit`, `metrics`, `audit`, `doctor`, `seed-demo`)
- [x] Streamlit dashboard (read-only: overview, opportunities, detail, sources, quarantine, metrics)
- [x] Dockerfile + Docker Compose
- [x] systemd units/timers
- [x] Windows PowerShell helper scripts

## 11. Testing

- [x] Unit tests: URL normalisation, dates, evidence grounding, extraction, vetoes, eligibility, dedup, scoring, config, DB
- [x] Integration test: fixture → raw store → extract → dedup → score → alert record
- [x] Test fixtures (JSON-LD casting page, CSS page, festival page, scam post, email, RSS, sitemap, Telegram)
- [x] `pytest` green on the build machine
- [x] CLI smoke tests (`init`, `doctor`, `seed-demo`, `digest --dry-run`)
- [x] Backup/restore verification test

## 12. Documentation

- [x] `README.md` quick start, architecture, operations
- [x] `ARCHITECTURE.md` component map and data flow
- [x] `OPERATIONS.md` runbook (deploy, backup, restore, incident response)
- [x] `SECURITY.md` safety, secrets, ToS, prompt injection
- [x] `docs/SOURCES.md` how to add each source type
- [x] `docs/RUNBOOK.md` daily/weekly/monthly checklist
- [x] This status file kept current

---

## Verification log (2026-11-06)

Everything below was executed on this machine, not assumed.

- `pytest` — 113 tests green (unit + integration + CLI smoke).
- `python -m radar init --demo` then `doctor` — database, FTS5, WAL and optional extras all report OK; the only warnings are the expected "not configured yet" ones (ntfy, IMAP, LLM, sources, profile placeholders).
- CLI exercised against a demo corpus: `opportunities` (plain, `--status`, `--q` FTS, combined filters), `show`, `label` (apply / ignore / undo), `digest --dry-run`, `health`, `metrics`, `audit`, `weekly`, `refit`, `quarantine list`, `sources list`, `backup snapshot` / `list` / `verify`.
- Dashboard (Streamlit) rendered against a demo database with all six views — Overview, Opportunities, Detail, Sources, Quarantine, Metrics — plus live FTS search on the Opportunities page.
- Bugs found and fixed during this pass:
  - `repo.list_opportunities(query=...)` crashed with `no such column: f`; the FTS join used the table alias inside `MATCH`. Search now works from the CLI and the dashboard.
  - `radar serve` died on Streamlit's first-run `Email:` prompt when started non-interactively; it now runs headless.
  - The Detail page's evidence table read `quote`/`value` keys that the extractor never writes, so every row was blank; it now shows the extracted value, the source quote and the method.
  - `label ID applied` (the spelling the dashboard and runbook suggested) was rejected by argparse; past-tense spellings are now accepted as aliases.

Regression tests were added for the FTS listing and the label aliases.

---

## Final audit (2026-11-06, second pass)

A full re-read of every module, with each CLI subcommand, the worker/scheduler,
the reply and IMAP listeners, the dashboard and the deploy artifacts exercised
against a fresh throwaway home directory. Everything below was run, not assumed.

- `pytest` — **139 tests green** (was 113). New suites: `tests/test_fetch.py`
  (robots cache, rate limiter, fetch escalation, user agent), `tests/test_scheduler.py`
  (job due-times, worker cycle, job-run recording) and `tests/test_dashboard.py`
  (headless `streamlit.testing.AppTest` over all six pages against a seeded, an
  empty and a missing database).
- Every subcommand was run for real, not only `--help`: `init` (plain and
  `--demo`), `doctor`, `config`, `sources` (list/sync/enable/disable/add),
  `discover`, `ingest`, `run`, `worker --once`, `score`, `digest` (`--dry-run`
  and send), `alerts`, `opportunities`, `show`, `label`, `replay`,
  `quarantine`, `health`, `metrics`, `audit`, `weekly`, `refit`, `backup`
  (snapshot/list/verify/restore), `seed-demo`, `reply`, `imap --once`,
  `serve`, `version`.
- Documented command lines are now machine-checked: all 58 `python -m radar ...`
  invocations in `README.md`, `OPERATIONS.md`, `SECURITY.md`, `ARCHITECTURE.md`,
  `BUILD_STATUS.md` and `docs/*.md` parse against the real argparse tree, and
  every documented subcommand exists.
- Dashboard: all six pages render with data, with an empty database (explicit
  empty-state text instead of a blank page) and with no database (a clear
  "run `python -m radar init`" error), in the browser and under `AppTest`.
- Deploy artifacts: Dockerfile / `docker-compose.yml` / `.dockerignore`,
  systemd units and both PowerShell scripts cross-checked against the CLI they
  call; `install-task.ps1` and `run-radar.ps1` parse cleanly under PowerShell.
- Applied-threshold, timezone and user-agent behaviour is now asserted in
  `tests/test_config.py`, and `doctor` reports the resolved thresholds, the
  timezone story and warns on a placeholder profile.
- Bugs found and fixed during this pass:
  - `RADAR_INSTANT_THRESHOLD` / `RADAR_DIGEST_THRESHOLD` env values were parsed
    with `float()` and crashed the process on a bad value; they are now parsed
    safely and fall back to `config/profile.yaml`.
  - Alert routing read thresholds with `["instant"]` / `["digest"]`, so a
    partial override crashed the worker; it now uses `.get()` with profile
    defaults.
  - `HttpFetcher` exposed a duplicated `effective_effective_user_agent`
    property, so robots checks and request headers could diverge; there is now
    one property used by every call site.
  - `radar serve` resolved `dashboard/app.py` only under `RADAR_HOME`, so a
    data-only home (or a service unit pointing at one) failed with "dashboard
    not found"; it now falls back to the dashboard shipped with the package.
  - The dashboard's tables rendered the literal `None` for missing job statuses
    and errors; empty values now render as `—`.
  - `st.dataframe(use_container_width=True)` is deprecated (removal date passed)
    and logged a warning on every render; the dashboard now picks
    `width="stretch"` on Streamlit >= 1.49 and keeps `use_container_width` on
    the older versions the pin still allows.
  - The Windows `RadarBackup` scheduled task claimed "snapshot + verify" but only
    snapshotted; it now runs `backup verify` after the snapshot, matching
    `deploy/systemd/radar-backup.service`.
  - `SearXNG` discovery ignored `SEARXNG_ENABLED` in some paths and `jobs.py` /
    `learning.py` / `cli.py` disagreed about the `recall_audit` argument shape;
    both are consistent now.
  - `.env.example`, `README.md`, `ARCHITECTURE.md` and `docs/SOURCES.md` were
    updated for the above (threshold variables, SearXNG switch, serve
    behaviour).

Known non-blockers: the Windows scheduled tasks need an elevated shell for the
`-AtStartup` trigger, Docker expects the host `./data` directory to be writable
by uid 10001, and `radar backup verify` should still be run monthly as the
runbook says.

---

## Real-world configuration needed from the user (placeholders)

These are intentionally blank/safe. The system runs without them; live discovery/alerting needs them filled in `.env` / `config/sources.yaml`.

- [ ] `NTFY_SERVER`, `NTFY_TOPIC`, `NTFY_REPLY_TOPIC`, `NTFY_TOKEN`, `RADAR_REPLY_SECRET`
- [ ] IMAP host/user/app-password/folder (`IMAP_*`)
- [ ] Telegram channel list in `config/sources.yaml`
- [ ] Discord bot token + invited guild/channel IDs (optional)
- [ ] SearXNG base URL (optional)
- [ ] Groq API key (optional; deterministic extraction works without it)
- [ ] Healthchecks.io ping URL (optional)
- [ ] Litestream replica URL (optional)
- [ ] Personal profile values in `config/profile.yaml` (age, city, languages, role types)

## Known limits (by design, from the report)

- No login-only, private, CAPTCHA-protected or explicitly bot-forbidden sources.
- No auto-applying; every application is human-reviewed.
- Social platforms (Instagram/Facebook/X/LinkedIn/TikTok) are out of the core build.
- An untested backup is not a backup: run `radar backup verify` monthly.
- Source list quality matters more than code volume; add hand-vetted sources first.
