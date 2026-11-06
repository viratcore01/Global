-- Global Cinema Opportunity Radar - initial schema
-- All timestamps are ISO-8601 strings. Deadlines carry an explicit +05:30
-- offset; audit timestamps are UTC.

CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  name        TEXT NOT NULL,
  applied_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Sources and health
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS sources (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  name                   TEXT NOT NULL UNIQUE,
  type                   TEXT NOT NULL,
  url                    TEXT,
  tier                   INTEGER NOT NULL DEFAULT 3,
  enabled                INTEGER NOT NULL DEFAULT 0,
  poll_interval_minutes  INTEGER NOT NULL DEFAULT 60,
  fetch_strategy         TEXT NOT NULL DEFAULT 'http',
  extractor              TEXT NOT NULL DEFAULT 'auto',
  extractor_config_json  TEXT,
  cadence_hint           TEXT,
  contact                TEXT,
  notes                  TEXT,
  config_json            TEXT,
  tags_json              TEXT,
  assume_opportunity     INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sources_enabled ON sources(enabled, type);
CREATE INDEX IF NOT EXISTS idx_sources_tier ON sources(tier);

CREATE TABLE IF NOT EXISTS source_health (
  source_id              INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  last_run_at            TEXT,
  last_success_at        TEXT,
  last_error             TEXT,
  last_http_status       INTEGER,
  consecutive_empty      INTEGER NOT NULL DEFAULT 0,
  consecutive_failures   INTEGER NOT NULL DEFAULT 0,
  consecutive_ok         INTEGER NOT NULL DEFAULT 0,
  avg_gap_hours          REAL,
  next_run_at            TEXT,
  status                 TEXT NOT NULL DEFAULT 'new',
  selector_drift         INTEGER NOT NULL DEFAULT 0,
  last_records           INTEGER NOT NULL DEFAULT 0,
  updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_next ON source_health(next_run_at, status);

CREATE TABLE IF NOT EXISTS source_stats (
  source_id              INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
  applies                INTEGER NOT NULL DEFAULT 0,
  ignores                INTEGER NOT NULL DEFAULT 0,
  scams                  INTEGER NOT NULL DEFAULT 0,
  replies                INTEGER NOT NULL DEFAULT 0,
  bookings               INTEGER NOT NULL DEFAULT 0,
  labels_total           INTEGER NOT NULL DEFAULT 0,
  last_label_at          TEXT,
  trust_score            REAL NOT NULL DEFAULT 0.5,
  updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS domain_intel (
  domain                 TEXT PRIMARY KEY,
  created_at             TEXT,
  expires_at             TEXT,
  age_days               REAL,
  registrar              TEXT,
  status                 TEXT NOT NULL DEFAULT 'unknown',
  raw_json               TEXT,
  checked_at             TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Raw store
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS raw_documents (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id              INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  url                    TEXT NOT NULL,
  canonical_url          TEXT NOT NULL,
  content_type           TEXT,
  http_status            INTEGER,
  headers_json           TEXT,
  body                   BLOB,
  body_text              TEXT,
  fetched_at             TEXT NOT NULL,
  content_hash           TEXT,
  kind                   TEXT NOT NULL DEFAULT 'page',
  meta_json              TEXT,
  compressed             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_raw_canonical ON raw_documents(canonical_url, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_source ON raw_documents(source_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_hash ON raw_documents(content_hash);

-- ---------------------------------------------------------------------------
-- Clusters and opportunities
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS clusters (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_key            TEXT NOT NULL UNIQUE,
  title                  TEXT,
  city                   TEXT,
  deadline               TEXT,
  contact_value          TEXT,
  source_count           INTEGER NOT NULL DEFAULT 0,
  opportunity_count      INTEGER NOT NULL DEFAULT 1,
  representative_id      INTEGER,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  canonical_url          TEXT NOT NULL,
  canonical_hash         TEXT NOT NULL UNIQUE,
  cluster_id             INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
  source_id              INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  source_type            TEXT,
  opportunity_type       TEXT NOT NULL DEFAULT 'other',
  title                  TEXT NOT NULL,
  description            TEXT,
  published_at           TEXT,
  first_seen_at          TEXT NOT NULL,
  last_seen_at           TEXT NOT NULL,
  deadline               TEXT,
  deadline_confidence    TEXT NOT NULL DEFAULT 'unknown',
  city                   TEXT,
  country                TEXT,
  remote_ok              INTEGER,
  language_json          TEXT,
  role_type_json         TEXT,
  comp_type              TEXT,
  pay_amount             REAL,
  currency               TEXT,
  fee_required           INTEGER,
  usage_rights           TEXT,
  nudity                 TEXT,
  travel_covered         INTEGER,
  audition_format        TEXT,
  shoot_window_json      TEXT,
  age_min                INTEGER,
  age_max                INTEGER,
  gender_presentation    TEXT,
  contact_channel        TEXT,
  contact_value          TEXT,
  fields_evidence_json   TEXT,
  risk_flags_json        TEXT,
  trust_tier             INTEGER NOT NULL DEFAULT 3,
  raw_document_id        INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
  raw_text_hash          TEXT,
  minhash_signature      TEXT,
  structured_key         TEXT,
  score                  REAL NOT NULL DEFAULT 0,
  score_breakdown_json   TEXT,
  status                 TEXT NOT NULL DEFAULT 'new',
  extraction_method      TEXT,
  llm_model              TEXT,
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_status ON opportunities(status, score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_deadline ON opportunities(deadline);
CREATE INDEX IF NOT EXISTS idx_opp_cluster ON opportunities(cluster_id);
CREATE INDEX IF NOT EXISTS idx_opp_source ON opportunities(source_id);
CREATE INDEX IF NOT EXISTS idx_opp_structured ON opportunities(structured_key);
CREATE INDEX IF NOT EXISTS idx_opp_score ON opportunities(score DESC);
CREATE INDEX IF NOT EXISTS idx_opp_first_seen ON opportunities(first_seen_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS opportunities_fts USING fts5(
  title,
  description,
  city,
  contact_value,
  role_type,
  tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS opportunities_ai AFTER INSERT ON opportunities BEGIN
  INSERT INTO opportunities_fts(rowid, title, description, city, contact_value, role_type)
  VALUES (new.id, new.title, COALESCE(new.description, ''), COALESCE(new.city, ''),
          COALESCE(new.contact_value, ''), COALESCE(new.role_type_json, ''));
END;

CREATE TRIGGER IF NOT EXISTS opportunities_ad AFTER DELETE ON opportunities BEGIN
  DELETE FROM opportunities_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS opportunities_au AFTER UPDATE ON opportunities BEGIN
  DELETE FROM opportunities_fts WHERE rowid = old.id;
  INSERT INTO opportunities_fts(rowid, title, description, city, contact_value, role_type)
  VALUES (new.id, new.title, COALESCE(new.description, ''), COALESCE(new.city, ''),
          COALESCE(new.contact_value, ''), COALESCE(new.role_type_json, ''));
END;

CREATE TABLE IF NOT EXISTS embeddings (
  opportunity_id         INTEGER PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
  model                  TEXT NOT NULL,
  dim                    INTEGER NOT NULL,
  vector                 BLOB NOT NULL,
  updated_at             TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Discovery queue / sitemaps / hubs / Wayback
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS discovery_queue (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id              INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  kind                   TEXT NOT NULL,
  url                    TEXT NOT NULL,
  canonical_url          TEXT NOT NULL,
  payload_json           TEXT,
  discovered_at          TEXT NOT NULL,
  run_after              TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'pending',
  attempts               INTEGER NOT NULL DEFAULT 0,
  last_error             TEXT,
  processed_at           TEXT,
  UNIQUE(source_id, canonical_url)
);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON discovery_queue(status, run_after);
CREATE INDEX IF NOT EXISTS idx_queue_source ON discovery_queue(source_id, status);

CREATE TABLE IF NOT EXISTS sitemap_urls (
  domain                 TEXT NOT NULL,
  url                    TEXT NOT NULL,
  lastmod                TEXT,
  content_hash           TEXT,
  first_seen_at          TEXT NOT NULL,
  last_seen_at           TEXT NOT NULL,
  PRIMARY KEY(domain, url)
);
CREATE INDEX IF NOT EXISTS idx_sitemap_lastmod ON sitemap_urls(domain, lastmod);

CREATE TABLE IF NOT EXISTS hub_candidates (
  domain                 TEXT PRIMARY KEY,
  link_count             INTEGER NOT NULL DEFAULT 0,
  example_url            TEXT,
  source_id              INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  status                 TEXT NOT NULL DEFAULT 'pending',
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cdx_runs (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  domain                 TEXT NOT NULL,
  url_pattern            TEXT NOT NULL,
  resume_key             TEXT,
  pages_done             INTEGER NOT NULL DEFAULT 0,
  urls_seen              INTEGER NOT NULL DEFAULT 0,
  last_run_at            TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'running',
  error                  TEXT
);
CREATE INDEX IF NOT EXISTS idx_cdx_domain ON cdx_runs(domain, last_run_at DESC);

CREATE TABLE IF NOT EXISTS email_messages (
  message_id             TEXT PRIMARY KEY,
  source_id              INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  uid                    TEXT,
  subject                TEXT,
  sender                 TEXT,
  received_at            TEXT,
  processed_at           TEXT,
  status                 TEXT NOT NULL DEFAULT 'new'
);

-- ---------------------------------------------------------------------------
-- Labels, reputation, vetoes, quarantine, alerts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS labels (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id         INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  action                 TEXT NOT NULL,
  previous_status        TEXT,
  new_status             TEXT,
  score_at_decision      REAL,
  features_json          TEXT,
  note                   TEXT,
  created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_labels_opp ON labels(opportunity_id);
CREATE INDEX IF NOT EXISTS idx_labels_action ON labels(action, created_at DESC);

CREATE TABLE IF NOT EXISTS reputation (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  kind                   TEXT NOT NULL,
  value                  TEXT NOT NULL,
  reason                 TEXT,
  source_opportunity_id  INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
  hits                   INTEGER NOT NULL DEFAULT 0,
  created_at             TEXT NOT NULL,
  last_seen_at           TEXT,
  UNIQUE(kind, value)
);
CREATE INDEX IF NOT EXISTS idx_rep_value ON reputation(kind, value);

CREATE TABLE IF NOT EXISTS veto_log (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id         INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
  cluster_id             INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
  stage                  TEXT NOT NULL,
  rule_id                TEXT,
  reason                 TEXT NOT NULL,
  detail                 TEXT,
  created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_veto_created ON veto_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_veto_rule ON veto_log(rule_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quarantine (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  raw_document_id        INTEGER REFERENCES raw_documents(id) ON DELETE SET NULL,
  source_id              INTEGER REFERENCES sources(id) ON DELETE SET NULL,
  url                    TEXT,
  errors_json            TEXT,
  payload_json           TEXT,
  created_at             TEXT NOT NULL,
  resolved_at            TEXT,
  resolution             TEXT
);
CREATE INDEX IF NOT EXISTS idx_quarantine_open ON quarantine(resolved_at, created_at DESC);

CREATE TABLE IF NOT EXISTS alerts (
  id                     INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id         INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  kind                   TEXT NOT NULL DEFAULT 'instant',
  channel                TEXT NOT NULL DEFAULT 'ntfy',
  topic                  TEXT,
  message_id             TEXT,
  title                  TEXT,
  body                   TEXT,
  score                  REAL,
  dedup_key              TEXT NOT NULL UNIQUE,
  sent_at                TEXT NOT NULL,
  response               TEXT,
  responded_at           TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_opp ON alerts(opportunity_id);

CREATE TABLE IF NOT EXISTS reply_messages (
  message_id             TEXT PRIMARY KEY,
  topic                  TEXT,
  received_at            TEXT NOT NULL,
  payload_json           TEXT,
  processed_at           TEXT,
  result                 TEXT
);

-- ---------------------------------------------------------------------------
-- Operations
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS job_runs (
  name                   TEXT PRIMARY KEY,
  last_run_at            TEXT,
  last_status            TEXT,
  last_error             TEXT,
  run_count              INTEGER NOT NULL DEFAULT 0,
  updated_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_reports (
  week_start             TEXT PRIMARY KEY,
  metrics_json           TEXT NOT NULL,
  created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
  key                    TEXT PRIMARY KEY,
  value                  TEXT,
  updated_at             TEXT NOT NULL
);
