## Global Cinema Opportunity Radar

Final design report: what it is, how it works, every component, how everything connects, and how to build it

Version: Final (audited) | Date: 5 November 2026

Scope: personal, self-hosted system for discovering, filtering, ranking and tracking acting, casting, festival and crew opportunities.

Core rule: Discovery first, deterministic extraction second, AI last. The AI may rank and fill gaps; it may never decide whether something is real, paid, expired or safe.


## Contents

| # | Section | What you will learn |
| --- | --- | --- |
| 1 | What it is and why it exists | The problem, the promise, what it will and will not do |
| 2 | Architecture and data flow | The full diagram and how each layer hands data to the next |
| 3 | A day in the life | One opportunity followed from discovery to your phone |
| 4 | Discovery layer | Every source: what, why, how, limits |
| 5 | Fetch layer | Fetch order, politeness, ethics |
| 6 | Extraction layer | How raw text becomes validated structured data |
| 7 | Deduplication and clustering | Why the same casting call is not shown five times |
| 8 | Profile, eligibility, vetoes, scoring | The decision engine and the final formula |
| 9 | Alerts, buttons and tracking | How you are notified and how your taps become learning |
| 10 | Learning loop and quality metrics | How the system improves and how you know it works |
| 11 | Source health and reliability | Selector drift, quiet sources, dead sources |
| 12 | Storage, backup and recovery | SQLite, Litestream, snapshots, git |
| 13 | Where it runs | Local first, then always-on host |
| 14 | Security, safety and legal | Scam defence, prompt injection, personal safety, ToS |
| 15 | Failure modes | What breaks and what the system does about it |
| 16 | Build roadmap | Day 0 to week 4+ |
| 17 | Kill criteria and limits | When to stop, and the honest ceiling |
| A | Appendix | Complete tool stack, config examples, glossary |


## 1. What it is and why it exists

The Global Cinema Opportunity Radar is a personal automation system. It watches many places where cinema opportunities appear (auditions, open casting calls, film-festival submission windows, crew jobs, modelling and voiceover calls), pulls them into one database, removes duplicates, extracts the facts (deadline, city, pay, role, contact), filters out likely scams and mismatches, ranks what is left against your own profile, and sends only the high-value items to your phone with one-tap Apply / Ignore / Scam buttons.

## 1.1 The problem it solves

- Opportunities are scattered across newsletters, Discord, Telegram, agency sites, festival directories and social media. Checking them by hand is slow and you still miss things.

- Many calls have short deadlines (days). Missing the window means missing the chance.

- The same casting text is reposted on many sites with different titles, so manual browsing wastes time on repeats.

- A large share of casting posts in this space are scams (pay-to-apply, fake guarantees, off-platform payment). Newcomers are targeted most.

- Volume overwhelms. At 30 to 50 items a day, an unranked feed becomes noise within days.

## 1.2 The core principle

Discovery first, deterministic extraction second, AI last. Sources decide what you can find. Rules and parsers decide what a post actually says. A language model is allowed only to (a) fill gaps the parsers cannot, with quoted evidence that code verifies, and (b) reorder the top candidates. It can never override an eligibility rule, a veto, or a risk flag.

## 1.3 What it will do

- Monitor public and authorised sources continuously and near real time where possible (email IDLE, Telegram, RSS).

- Extract structured fields with evidence for each field.

- Reject expired, ineligible, unpaid-when-you-want-paid, and scam-pattern items before any AI sees them.

- Rank by fit, trust, urgency and completeness; alert instantly only on the best, capped at 15 per day.

- Track every item through a status machine (new to booked) and learn from your Apply / Ignore / Scam taps.

- Tell you when sources die, when parsers break, and when the whole system stops (external heartbeat).

## 1.4 What it will not do

- Read private Instagram Stories, closed Facebook groups, login-only casting portals or paid databases.

- Defeat CAPTCHAs or ignore sites that explicitly forbid automated access.

- Apply on your behalf. A human always reviews before applying.

- Guarantee that an item is real. It lowers risk; it does not remove it.


## 2. Architecture and data flow

The system is a one-directional pipeline with one feedback loop. Each layer reads from the layer above, writes to SQLite, and passes a well-defined record downward. Nothing skips a layer, and nothing downstream can silently alter upstream facts.

## 2.1 Layer-to-layer hand-offs


| From | To | What is passed | Where it is stored |
| --- | --- | --- | --- |
| Discovery | Fetch | A URL, message ID or feed entry plus source_id | queue table |
| Fetch | Raw store | Bytes or text, HTTP status, headers, fetch time | raw_documents (compressed) |
| Raw store | Extract | A raw document id | raw_documents |
| Extract | Dedup | A validated record with per-field evidence | opportunities (status=new), |
|   |   |   | quarantine for failures |
| Dedup | Gate/Score | A cluster id and a list of corroborating sources | opportunities.cluster_id |
| Gate/Score | Alert | A final score, risk flags, veto reason (if any) | scores, veto_log |
| Alert | Track | A pushed notification with signed action buttons | alerts |
| Track | Gate/Score (loop) Labels and reputation entries |   | labels, reputation |

## 2.2 Why this shape

- Raw store before parsing: a parser bug never costs you data. Fix the parser, replay the raw documents.

- Gate and vetoes before AI: cheap deterministic checks remove most noise and all hard-no items, so AI cost and hallucination risk apply to a tiny slice.

- One writer: a single worker process writes to SQLite. Everything else reads or sends it a message. That removes locking bugs and race conditions.

- Feedback into scoring only: your labels adjust ranking and the reputation table. They never rewrite stored facts.


## 3. A day in the life: one opportunity, end to end

To make the architecture concrete, here is one realistic item moving through every layer.

| Step Event |   | Component |
| --- | --- | --- |
| 1 | A casting agency emails its newsletter at 10:42. The message lands in the dedicated Gmail | imap-tools IDLE |
|   | label. |   |
| 2 | The worker is woken by IDLE, fetches the message with BODY.PEEK (stays unread), and | Discovery |
|   | extracts links and body text. |   |
| 3 | The full email and the linked casting page are saved compressed as raw documents. | Raw store |
| 4 | extruct finds no JSON-LD; the agency has a known CSS template, so selectolax pulls title, | Extraction |
|   | city, deadline, contact. Missing pay is read by trafilatura text rules; poster image goes |   |
|   | through OCR. |   |
| 5 | Pydantic validates. Deadline '12 Nov' is parsed as DMY in Asia/Kolkata, end of day. Every | Validation |
|   | field carries a quoted source span. |   |
| 6 | The same text appears on a Telegram channel 20 minutes later with a different title. | Dedup |
|   | MinHash links them into one cluster; corroboration raises the trust component. |   |
| 7 | Eligibility gate: age range, language (Hindi), Delhi/Mumbai or self-tape allowed: pass. | Gate |
|   | Vetoes: no fee, no guaranteed-role text, contact not on the blocklist: pass. Risk penalty: |   |
|   | WhatsApp contact (small). |   |
| 8 | Score is computed (fit, BM25, trust, urgency, completeness). It lands at 91, above the | Scoring |
|   | instant threshold, within today's cap. |   |
| 9 | ntfy pushes: title, match, deadline, location, pay, source, safety line, plus Apply / Ignore / | Alert |
|   | Scam buttons. |   |
| 10 | You tap Apply. The phone POSTs to the private reply topic; the worker (subscribed | Track |
|   | outbound) sets status=applied and stores a positive label. |   |
| 11 | At week's end the label is included in the weekly refit; the agency source gains trust. | Learning |


## 4. Discovery layer

Discovery is the most important layer. The best scraper is useless if it is pointed at nothing. The list of sources (your source list) is the real moat: code can be rewritten in a weekend, but knowing which 200 agencies, festivals and channels matter takes months.

## 4.1 Source tiers

| Tier | Definition | Effect |
| --- | --- | --- |
| Tier 1 | Agencies, festivals, commissions you vetted by hand | Highest trust weight; may qualify for instant alert |
|   |   | on their own |
| Tier 2 | Sources that have produced Apply labels from you | Trust grows with labels |
| Tier 3 | Open web results and graph-discovered sites | Lowest trust; require corroboration or manual |
|   |   | approval |

## 4.2 Each source in detail

| Source | What it is | Why it is used | How it works | Limits / notes |
| --- | --- | --- | --- | --- |
| Email inbox Dedicated Gmail |   | Highest value: many | imap-tools, App Password (needs | IDLE connections drop |
|   | account subscribed | calls never appear | 2FA), IMAP IDLE for near real | about every 29 minutes: |
|   | to agencies, | prominently on the | time, BODY.PEEK so nothing is | reconnect loop is |
|   | festivals, | web | marked read | mandatory |
|   | commissions, |   |   |   |
|   | boards |   |   |   |
| Telegram | Public channel | Real-time industry | Fetch the preview page, parse | Only channels with preview |
| public | preview pages at | chatter without an | posts, paginate backwards with | enabled; not groups. Use |
| channels | t.me/s/name | API session | the page's before parameter | Telethon only for private |
|   |   |   |   | channels you belong to |
| Discord | Servers for acting, | Structured gateway | Read-only bot receives | A bot can only join servers |
|   | film, voice, | events, more stable | messages; stores server, | where an admin invites it. |
|   | regional | than scraping | channel, message ID, author, | For public servers use |
|   | communities |   | time, text, attachments, links | announcement Follow into |
|   |   |   |   | your own server. Never use |
|   |   |   |   | a user token (ToS violation). |
|   |   |   |   | Needs the Message Content |
|   |   |   |   | intent |
| Google | Saved searches | Cheap broad | Queries such as casting call | Coverage is partial; treat as |
| Alerts | delivered as RSS or | coverage of new | Mumbai, audition Delhi Hindi | a lead generator, not a feed |
|   | email | pages | film, open call actor India, short | of record |
|   |   |   | film submission deadline |   |
| SearXNG | Self-hosted | Scheduled sweeps | Rotate upstream engines; use | Wildcard TLD syntax and |
|   | metasearch | beyond your known | site:.in style operators and plain | after: are not supported |
|   |   | sources | keywords; filter dates in your | consistently; engines may |
|   |   |   | own code | rate-limit |
| Sitemap | Compare each | Finds new casting | Read robots.txt, expand sitemap | Not every site publishes a |
| diffing | known domain's | pages without | indexes and .gz, store URL and | sitemap or accurate |
|   | sitemap with | crawling unchanged | lastmod, queue only new or | lastmod |
|   | yesterday's | ones | changed URLs |   |
| Outbound-lin | Count which | Finds aggregators | Targets linked by 3 or more | Scam directories also link to |
| k graph | external sites your | and directories you | seeds become hub candidates; | each other: every new hub |
|   | seeds link to | would never search | recurse one level | needs your manual |
|   |   | for |   | approval |
| RSSHub | Converts sites and | Uniform ingestion for | Self-host; point your fetcher at | Social routes break often; |
|   | some social pages | awkward sources | generated feeds | treat as fragile |
|   | to RSS |   |   |   |


| Source | What it is | Why it is used | How it works | Limits / notes |
| --- | --- | --- | --- | --- |
| Wayback | Archive index of | Reveals URL patterns | Query with resumeKey | Lower return than other |
| CDX | historical URLs on | such as /casting/ and | pagination, throttle to about one | sources; build it late |
| (optional) | a domain | /open-call/ you can | request per 2 seconds, honour |   |
|   |   | then probe | Retry-After, track runs in |   |
|   |   |   | cdx_runs |   |

## 4.3 Google Alerts and SearXNG query sets

```
Google Alerts:
"casting call" Mumbai "audition" Delhi Hindi film "extras wanted" Mumbai
"open call" actor India
"short film" submission deadline "casting director" India
"film festival" submission India
SearXNG (examples, adapt per engine):
site:.in "casting call" site:.ae "audition"
site:.uk "open casting call" site:filmfreeway.com "short film"
"casting call" Mumbai "film commission" site:.gov.in
```

## 5. Fetch layer

The fetch layer turns a URL into bytes safely and politely. It tries cheap methods first and escalates only when needed.

| Order Tool |   | Used when | Why |
| --- | --- | --- | --- |
| 1 | Honest HTTP client (with bot | Source offers RSS, sitemap, or robots | Cheapest, most respectful, most |
|   | User-Agent and contact URL) | allows | stable |
| 2 | curl_cffi | Site serves normal visitors but rejects | Browser-like TLS/HTTP fingerprint |
|   |   | non-browser clients | without a full browser |
| 3 | Camoufox | Page needs JavaScript rendering | Real Firefox with anti-fingerprint |
|   |   |   | tweaks; at most one instance at a |
|   |   |   | time |
| 4 | Scrapling | A source keeps failing or its markup | Adaptive selector relocation after |
|   |   | keeps changing | markup changes |

## 5.1 Politeness and reliability rules

- Per-domain token bucket, default about one request every 2 seconds.

- robots.txt check and Crawl-delay respected. If a site explicitly blocks bots or forbids scraping, drop it.

- Exponential backoff with jitter; Retry-After honoured; per-domain timeouts and failure counters.

- Global concurrency cap of 4; one browser instance maximum.

- Never hammer Instagram, Facebook, X, LinkedIn or TikTok. They restrict automation and change often; connectors are built last and budgeted at roughly 2 hours per month each.

Ethics note. Fingerprint impersonation is for sites that serve ordinary visitors but choke on non-browser clients. It is not a tool for evading an explicit prohibition. Where a site says no, the answer is no.


## 6. Extraction layer

Extraction converts a raw document into a validated record. The order is deliberate: most reliable and most structured first, least reliable last.

| Step Tool |   | What it does | Why in this position |
| --- | --- | --- | --- |
| 1 | extruct | Reads schema.org JSON-LD such as | Structured data maintained by the site itself |
|   |   | JobPosting, Event, CreativeWork |   |
| 2 | selectolax | Source-specific CSS/XPath for known sites | Fast and precise where you wrote a template |
| 3 | trafilatura | Main-text extraction for unknown pages, | Good generic fallback |
|   |   | drops menus and ads |   |
| 4 | OCR | Text from posters and screenshots | Many calls are only images. Verify Hindi |
|   |   |   | accuracy on real posters; if weak use |
|   |   |   | Tesseract with hin+eng or a PaddleOCR |
|   |   |   | Devanagari model |
| 5 | yt-dlp (optional) | Captions or transcripts for public videos | Heavy and ToS-sensitive; optional |
| 6 | Pydantic | Validates types and required fields | Bad records go to quarantine, not into the |
|   |   |   | database |
| 7 | LLM (gaps only) | Fills fields parsers could not, with quoted | Last resort; every span is verified by code |
|   |   | spans |   |

## 6.1 The record schema

```
{
"canonical_url": "https://example.com/casting/123",
"source_id": 42, "source_type": "email|discord|telegram|web|rss|social",
"opportunity_type": "casting|festival|crew|voiceover|other",
"title": "...",
"published_at": "2026-11-05T14:30:00+05:30",
"first_seen_at": "...", "last_seen_at": "...",
"deadline": "2026-11-12T23:59:00+05:30",
"deadline_confidence": "high|medium|low|unknown",
"city": "Mumbai", "country": "India", "remote_ok": false,
"language": ["Hindi", "English"],
"role_type": ["supporting", "voiceover"],
"comp_type": "day_rate|flat|deferred|credit_only|unpaid|unknown",
"pay_amount": null,
"fee_required": false,
"usage_rights": "perpetual_buyout|limited|unknown",
"nudity": "none|implied|topless|full|unknown",
"travel_covered": true,
"audition_format": "self_tape|in_person|zoom|unknown",
"shoot_window": ["2026-11-20", "2026-11-23"],
"age_range": [18, 30], "gender_presentation": "any",
"contact_channel": "form|email|dm|whatsapp|unknown",
"contact_value": "casting@example.com",
"fields_evidence": { "deadline": "Last date to apply: 12 November", "...": "..." },
"risk_flags": ["whatsapp_only", "freemail"],
"trust_tier": 1, "cluster_id": 981,
"raw_text_hash": "...", "minhash_signature": "...",
"score": 91, "status": "new"
}
```

## 6.2 Extraction rules that protect you

- Unknown is a valid value. If a field has no evidence, store unknown or null. Never guess.

- Per-field evidence. Each field stores the exact quoted text that supports it.

- Span grounding check. Code verifies that every quoted span appears verbatim in the raw text. If not, the field is nulled.

- Dates. dateparser with Asia/Kolkata and day-month-year order. Date-only deadlines mean end of that day, local time. Relative phrases ('by Friday') resolve against the post date. Unknown deadlines auto-expire at published date plus 14 days.


- Prompt-injection defence. Scraped text is untrusted data. The LLM has no tools, returns strict JSON only, and the page text is delimited as data.


## 7. Deduplication and clustering

The same casting call is commonly reposted with a new title on a new site. Without deduplication you see it five times. With naive deduplication you lose the useful fact that it appeared on five sources.

## 7.1 Three levels of matching

| Level | Method | Catches |
| --- | --- | --- |
| 1. Exact | Canonical URL, normalised then SHA-256 | Same page reached through tracking |
|   |   | links, www, http, trailing slash, AMP |
| 2. Near-text | MinHash LSH with exact Jaccard recheck. 5-word shingles for | Same text, different title, site, or URL |
|   | long text; 3-word shingles for texts under 120 words; |   |
|   | threshold about 0.7 |   |
| 3. Structured | Fuzzy key: title + city + deadline + contact | Rewritten posts with the same |
|   |   | underlying facts |

## 7.2 URL normalisation rules

- Force HTTPS, lowercase host, remove www., m. and AMP variants, remove fragments and trailing slash.

- Strip tracking parameters (utm_*, fbclid, gclid). Keep meaningful ids (id, p, jobid, post).

http://www.example.com/casting/123/?utm_source=telegram

-> https://example.com/casting/123

canonical_hash = sha256(canonical_url.encode()).hexdigest()

## 7.3 Cluster, do not drop

Duplicates are merged into a cluster, never deleted. The cluster keeps every source. Appearing on several independent sources raises the trust component of the score. A post that exists only on a brand-new unknown source does not get that boost.

## 8. Profile, eligibility, vetoes and scoring

This is the decision engine. It answers three questions in strict order: can I do this, is it safe, and is it worth my time.

## 8.1 The profile (profile.yaml)

Everything is ranked against your profile, so it must be explicit. It lives in version control.

age_range: [18, 30]

gender_presentation: any

travel_radius_km: 300

paid_only: false

nudity_max: none

role_types: [lead, supporting, voiceover, web_series, short_film]

showreel_ready: true

availability: weekends+evenings

languages: [Hindi, English, Hinglish]

base_city: Delhi

remote_selftape_ok: true

fee_tolerance: festivals_only

usage_rights: no_perpetual_buyout

selftape_ready: true

## 8.2 Stage 1: eligibility gate (deterministic)

Age range, gender presentation, language, location or remote/self-tape, nudity limit and paid-only are filters, not scores. A role you cannot play never reaches ranking. Remote and self-tape roles bypass the location

filter.

## 8.3 Stage 2: hard vetoes

| Veto | Detail |
| --- | --- |
| Expired | Deadline passed (end of day IST), or unknown deadline older than 14 days |
| Pay-to-apply | Patterns such as 'pay Rs/$ N to apply' for casting, acting or modelling items. Festival entry |
|   | fees are NOT vetoed; they are stored as fee_required and shown |
| Guaranteed outcome | 'Guaranteed role/work/break' |


| Veto | Detail |
| --- | --- |
| Paid portfolio | 'Portfolio shoot' combined with pay or fee |
| Join-group funnel | 'Click link to join group' style funnels |
| Reputation | Contact phone, email or domain previously marked Scam |
| Unpaid when paid_only | Respects your profile |

## 8.4 Stage 3: risk penalties (soft, not vetoes)

- WhatsApp-only contact (many legitimate Indian calls use it, so this is a penalty)

- Freemail contact address; very new website domain (WHOIS is often redacted, so soft)

- No verifiable contact; unrealistic claims; urgency pressure language

- Each penalty adds a named entry to risk_flags that is shown on the alert.

## 8.5 Stage 4: ranking and the final score

| Component | Weight | How it is computed |
| --- | --- | --- |
| Embedding fit | 35% | Cosine similarity between the opportunity and your profile plus past Apply |
|   |   | items, using multilingual-e5-small (CPU friendly, handles Hindi and English) |
| BM25 relevance | 25% | SQLite FTS5 ranking, unicode61 tokenizer with diacritic removal, plus a small |
|   |   | Hindi/Roman synonym list |
| Source trust and | 20% | Tier, label history, number of independent sources in the cluster |
| corroboration |   |   |
| Urgency | 10% | Closer deadline boosts, within reason |
| Completeness | 10% | How many key fields have evidence |

Risk penalties subtract from the total. After about 100 labels, the fixed weights are replaced by a small logistic regression fitted on your labels using the same features. Embedding thresholds are calibrated on your data, not copied from examples.

## 8.6 Stage 5: the LLM (strictly limited)

- Runs on at most the top 20 to 40 candidates per day.

- Does two things only: fill extraction gaps with verified spans, and reorder within the top slice.

- Cannot override eligibility, vetoes or risk flags.

- Hosted behind a provider interface (Groq first, another free API or Ollama as fallback); the model version is stored with every record. Send only role-relevant fields, never personal profile details.

- Ollama is used for offline re-scoring only; on a 2-core ARM host it is too slow for the live path.

- 1 eligibility gate -> 2 hard vetoes -> 3 risk penalties

- 4 score = 0.35*embed + 0.25*bm25 + 0.20*trust + 0.10*urgency + 0.10*completeness - penalties

- 5 LLM only for gaps / top-slice reorder (never overrides 1-3)

6 instant alert iff score >= threshold AND record complete AND no risk flag AND under daily cap

## decision order:


## 9. Alerts, buttons and tracking

## 9.1 Alert routing

| Condition | Outcome |
| --- | --- |
| Eligible, no veto, score above instant threshold, complete, no risk flag, | Instant ntfy push |
| under cap |   |
| Eligible and clean but below threshold | Daily digest (08:00) |
| 1 to 2 random mid-ranked items | Exploration slots in the digest so your labels |
|   | are not biased by what you were already |
|   | shown |
| Extraction failures | Quarantine count in the digest |
| Dead or drifting sources | Weekly source-health report |
| Time 23:00 to 07:00 IST | Quiet hours: hold unless the deadline is under |
|   | 6 hours |

The hard cap of 15 instant alerts per day means one bad rule can never flood your phone. The instant threshold starts at 80 and is tuned so it rarely hits the cap.

## 9.2 The alert message

Title:

Hindi short film casting - Mumbai

Match:

91/100

Deadline: 12 Nov 2026 (IST)

Location: Mumbai

Pay: Paid (day rate)

Source: example.com/casting/123 (also on 2 other sources)

Risk:

whatsapp_only

Safety: Never pay to apply. Verify the contact independently.

Meet in public, bring someone, do not share ID documents. [ Apply ] [ Ignore ] [ Scam ]

## 9.3 Button feedback flow (no public endpoint)

The buttons are ntfy http actions that POST to a private reply topic. The worker subscribes to that topic over an outbound connection. There is no inbound port, no tunnel and no public endpoint to attack. Use a self-hosted ntfy with authentication, or a long random topic name (on the public ntfy.sh server the topic name acts as the password).

## 9.4 Status state machine

## 9.5 What each tap does


| Button | Status | Learning effect |
| --- | --- | --- |
| Apply | applied | Strong positive label; source trust rises |
| Ignore | ignored | Negative / not relevant enough label |
| Scam | scam | Strong negative label; the contact phone, email and domain go into the |
|   |   | reputation table and are auto-vetoed everywhere afterward |

## 10. Learning loop and quality metrics

## 10.1 Cold start

In week 1 you hand-label at least 50 real items as Apply, Meh or Scam. These seed the scoring and the veto tuning. Until about 100 labels exist, fixed weights are used.

## 10.2 Weekly update

- Refit the scoring model on labels (after about 100 labels).

- Tune veto patterns from Scam examples; check that no good item was caught (see recall audit).

- Recompute source trust from outcomes; flag sources producing false positives or high-quality items.

- Review thresholds against precision.

## 10.3 Recall audit (catching what the system hides)

Vetoes and low scores silently remove items. Every week, review a random sample of 10 vetoed or low-ranked items. If you find good ones, fix the rule. Every veto is written to a veto_log with its reason so this is possible.

## 10.4 Quality metrics

| Metric | Definition | Action level |
| --- | --- | --- |
| Precision@15 | Apply taps divided by instant alerts, weekly | Below 40%: profile, threshold or source mix is |
|   |   | wrong. Below 25% after 4 weeks: stop adding |
|   |   | sources and fix scoring |
| Outcome rate | Replied or booked divided by applied | Tracks real-world value, not just interest |
| Scam catch rate | Scam-labelled items that were NOT already | Should fall over time as the reputation table |
|   | auto-flagged | grows |
| Quarantine growth | Records failing validation per week | Rising means extraction needs fixing |
| Time-to-alert | Source post time to push time | Shows whether discovery is fast enough |


## 11. Source health and reliability

Every source has a health record: type, URL, poll interval, consecutive empty runs, consecutive extraction failures, last success and status.

| Situation | Meaning | Response |
| --- | --- | --- |
| HTTP 200, zero records, twice | Possible selector drift (site changed its HTML) Flag selector-drift |   |
| HTTP 200, zero records, four times Almost certainly broken parser |   | Quarantine source + weekly report |
| Silence longer than about 3x the | Genuinely quiet or dying (cadence learned | Slow polling, then notify |
| source's normal gap | from history, so weekly posters are not |   |
|   | punished) |   |
| 404 or 410 twice | Gone | Mark dead, keep history |
| Repeated failures | Blocked or unstable | Back off; consider dropping per ethics |
|   |   | rule |

Selector drift is not the same as an empty source. A page that returns 200 with no extracted items may simply have changed its markup. The system distinguishes the two so it fixes parsers instead of abandoning good sources.

## 11.1 Watching the watcher

An external heartbeat (healthchecks.io free tier) is pinged on every successful cycle. If pings stop, the external service notifies you. The radar cannot be trusted to report its own death, so this check lives outside

it.

## 12. Storage, backup and recovery

## 12.1 What is stored

| Table | Purpose |
| --- | --- |
| raw_documents | Compressed original payloads for replay and evidence |
| opportunities | Validated records, status, score, cluster id |
| clusters | Groups of duplicates and their sources |
| sources / source_health | Source list, trust tier, health counters |
| labels | Your Apply / Ignore / Scam taps with features at decision time |
| reputation | Blocked phones, emails, domains from Scam labels |
| veto_log | Every veto and its reason (for the recall audit) |
| quarantine | Records that failed validation, awaiting fixes |
| alerts | What was pushed, when, and the response |
| cdx_runs | Wayback pagination state |

## 12.2 Database discipline

- WAL mode, busy_timeout=5000, synchronous=NORMAL.

- Exactly one process owns the write connection (the worker). Others are read-only or send messages.

- Migrations from day one.

## 12.3 Backup layers


| Layer | Tool | Protects against |
| --- | --- | --- |
| Continuous | Litestream replicating WAL to a local path or | Disk or VM loss with near-zero data loss |
|   | S3-compatible bucket (such as Cloudflare R2) |   |
| Nightly snapshot | sqlite3 .backup to a dated file | Independent, simple recovery point |
| Config and moat | git: profile.yaml, source list, veto patterns, exported | Losing the things that cannot be |
|   | labels | re-derived |
| Restore test | Monthly dry-run restore | An untested backup is not a backup |

nightly: sqlite3 /data/radar.db ".backup /backups/radar-\$(date +%F).db" recovery: new host -> restore Litestream replica (or latest snapshot)

-> start workers -> source-health detects missed polling


## 13. Where it runs

## 13.1 Phase 1: your own computer

Start entirely local to prove value for free. Use separate virtual environments (or containers) so native dependencies do not clash.

| Environment | Contains |
| --- | --- |
| radar-core | SQLite, fetchers, extraction, scoring, alerts, worker |
| browser-env | Camoufox and Scrapling (Playwright-style dependencies) |
| media-env | OCR, yt-dlp (heavy native libraries) |
| Services | SQLite + Litestream replica, ntfy topic or self-hosted ntfy, heartbeat ping |

## 13.2 Phase 2: always-on host

Move once the local version works. Options:

| Option | Pros | Cons |
| --- | --- | --- |
| Oracle Cloud Always Free ARM VM | Free within the allowance (verify the | Capacity errors in popular regions; idle |
| (Docker: api, worker, ntfy, Litestream) | current allowance in Oracle's docs); | instances can be reclaimed. Upgrading |
|   | good for this workload | to Pay-As-You-Go (still free inside the |
|   |   | allowance) reduces both risks |
| Home mini-PC or Raspberry Pi with a | Full control, no cloud account | Power and connectivity depend on your |
| cheap UPS |   | home |
| Small paid VPS | Reliable, simple | Small monthly cost |

## 13.3 Scheduling

Never rely on in-process schedulers alone. Use systemd timers or Docker Compose with restart: always so a crash or reboot does not silently kill the radar.

| Job | Frequency |
| --- | --- |
| Email IDLE listener (persistent, with reconnect) | Continuous |
| Ingest queue | Every 5 minutes |
| Fetch | Every 15 minutes (per-source intervals) |
| Discovery sweeps (SearXNG, sitemap, graph) | Every 6 hours |
| Daily digest | 08:00 IST |
| Health checks | Hourly |
| Backup snapshot | 03:00 daily |
| Weekly refit and recall audit | Weekly |

## 14. Security, safety and legal

## 14.1 Scam defence in layers

- Hard vetoes for pay-to-apply, guaranteed outcomes, funnels.

- Risk penalties and named risk flags shown on every alert.

- Reputation table: one Scam tap blocks that phone, email and domain everywhere.

- Source trust tiers and corroboration across independent sources.

- Human review before applying is mandatory.


## 14.2 Personal safety (printed on every alert)

- Never pay to apply or for a 'portfolio shoot' as a condition.

- Verify the casting director or company independently (official site, known agency, other trusted sources).

- Meet in public places, bring someone, tell a friend where you are going.

- Do not share ID documents or financial details early; be wary of requests to move to private messaging.

## 14.3 Technical security

- No public inbound endpoint (reply-topic design). Authenticated ntfy or a long random topic.

- Secrets (App Password, bot token, API keys) in environment files, never in git.

- Prompt-injection defence: untrusted text delimited as data, no tools for the LLM, strict JSON, code-verified spans.

- Send only role-relevant fields to hosted LLMs, never your personal profile details.

## 14.4 Legal and terms of service

- Personal, low-volume use of public pages that allow access is low risk; still respect robots.txt and site terms.

- Do not use user-token Discord automation, do not bypass login walls, do not defeat CAPTCHAs.

- Email addresses and phone numbers in posts are personal data of others: use them only to evaluate and contact for the opportunity, store minimally.


## 15. Failure modes and responses

| What fails | Symptom | System response |
| --- | --- | --- |
| Whole system stops | No pings | External heartbeat notifies you |
| A site redesign | 200 OK, zero records | Selector-drift flag; Scrapling fallback; quarantine after 4 |
|   |   | runs |
| IMAP IDLE drops | No new email for a while | Reconnect loop; periodic full poll as safety net |
| LLM provider outage or limit API errors |   | Fallback provider, then deterministic-only scoring; alerts |
|   |   | continue |
| Bad regex floods alerts | Many near-identical alerts | 15 per day cap; rest goes to digest |
| Parser bug stores wrong data Odd records |   | Raw store allows replay; fix parser; re-extract |
| Scam slips through | You tap Scam | Reputation table blocks future items from that contact; |
|   |   | patterns reviewed weekly |
| Good items wrongly vetoed | Recall audit finds them | Adjust the rule; veto_log shows reason |
| Disk or VM loss | Host gone | Restore Litestream replica or latest snapshot; config |
|   |   | from git |
| Phone alert fatigue | Unread alerts pile up | Cap, quiet hours, digest, exploration slots, kill criteria |

## 16. Build roadmap

| Phase | Work | Goal / exit test |
| --- | --- | --- |
| Day 0 | Write profile.yaml; list 20 seed sources; create raw store; set up | Foundations exist; nothing built that |
|   | heartbeat; create fixture corpus plan | cannot be tested |
| Week 1 | SQLite schema + migrations; Litestream + nightly snapshot; Gmail | Three real opportunities flow end to |
|   | IDLE reader; Telegram parser; RSS; curl_cffi fetcher; trafilatura + | end |
|   | extruct + selectolax; Pydantic + quarantine; eligibility + vetoes; |   |
|   | BM25; one ntfy alert |   |
| Week 2 | Reply-topic buttons; reputation table; MinHash + structured dedup; | Buttons change status; duplicates |
|   | sitemap diffing; SearXNG sweeps; source health; digest and | merge; dead sources reported |
|   | quarantine report |   |
| Week 3 | Hand-label 50+ items; multilingual embeddings; Groq top-slice with | Precision@15 measurable and |
|   | span verification; precision@15 tracking; recall audit; weekly refit | trending up |
| Week 4+ Discord (only where permitted); Camoufox; Scrapling; outbound-link |   | Resilient, always-on |
|   | graph with approval; Wayback (optional); Oracle or home server; |   |
|   | social connectors last |   |

## 16.1 Test discipline

- Keep 100 saved real pages and messages as fixtures. Every extractor change must still pass them.

- Replay mode re-runs the pipeline on raw documents, which doubles as regression testing.

- Log every veto and every score component so any decision can be explained.


## 17. Kill criteria and honest limits

## 17.1 Kill criteria (written before you start)

| Condition | Action |
| --- | --- |
| Source produces zero useful items in 30 days | Delete or archive the source |
| Precision@15 below 25% after 4 weeks | Stop adding sources; fix profile and scoring |
| A connector needs more than 4 hours per week | Drop it |
| More than 15 unresolved alerts at hour 48 for two weeks | Reduce digest volume |
| Quarantine keeps growing | Fix extraction; do not ignore it |
| Source returns 200 with zero extracted records repeatedly | Treat as selector drift, not as an empty source |

## 17.2 Honest limits

- Login-only, closed and private sources are out of scope by design.

- Instagram, Facebook, X, LinkedIn and TikTok are fragile and need ongoing maintenance.

- CAPTCHAs are a hard boundary; the core system never depends on solving them.

- Residential proxies may eventually help on heavily protected sites but cost money and raise ToS issues.

- The LLM can still err; span checks, validation and human review are mandatory.

- The source list is the real moat. Beyond this design, extra engineering has sharply diminishing returns; effort is best spent on your top 50 hand-vetted sources and your first 100 labels.

Where the ceiling is. With the fixes in this report the architecture is close to the best achievable for a personal, free-to-cheap, public-source system. What remains is outside any design: private sources, platform restrictions, and the need for your own judgment before you apply.


## Appendix A: Complete tool stack

| Layer | Tool | Purpose | Cost |
| --- | --- | --- | --- |
| Database | SQLite (WAL) | Opportunities, sources, labels, scores, statuses | Free |
| Replication | Litestream | Continuous WAL replication to local or | Free |
|   |   | S3-compatible storage |   |
| Snapshots | sqlite3 .backup | Nightly independent recovery point | Free |
| Email | imap-tools | IMAP IDLE, BODY.PEEK reading | Free |
| Discord | discord.py | Read-only bot in permitted servers | Free |
| Telegram | t.me/s parser (Telethon for private | Public channel previews | Free |
|   | channels you belong to) |   |   |
| Search | SearXNG | Self-hosted metasearch sweeps | Free |
| History | Wayback CDX API | Historical URL pattern discovery (optional) | Free |
| Feeds | RSSHub | Sites and some social pages to RSS | Free |
| Fetch | curl_cffi, Camoufox, Scrapling | Escalating fetch strategy | Free |
| Extraction | extruct, selectolax, trafilatura | JSON-LD, CSS/XPath, main text | Free |
| OCR | RapidOCR (verify Hindi), Tesseract | Poster and screenshot text | Free |
|   | hin+eng or PaddleOCR Devanagari |   |   |
|   | fallback |   |   |
| Media | yt-dlp (optional) | Captions and transcripts | Free |
| Validation | Pydantic | Schema enforcement, quarantine on failure | Free |
| Dates | dateparser | Asia/Kolkata, day-month-year, relative dates | Free |
| Dedup | datasketch | MinHash LSH | Free |
| Ranking | SQLite FTS5 + BM25 | Explainable keyword relevance | Built in |
| Embeddings | multilingual-e5-small | Hindi/English semantic fit on CPU | Free |
| LLM | Groq free tier behind a provider | Gap-filling and top-slice reorder | Free tier |
|   | interface; Ollama offline |   |   |
| Notifications | ntfy (http actions to reply topic) | Push with Apply/Ignore/Scam buttons | Free |
| Monitoring | healthchecks.io | External heartbeat | Free tier |
| Scheduling | systemd timers or Docker restart: | Reliable execution after crashes | Free |
|   | always |   |   |
| Dashboard | Streamlit or Grafana | Quarantine, source health, applications | Free |
| Version | git | Profile, source list, veto patterns, exported labels | Free |
| control |   |   |   |

## Appendix B: Source configuration example

\- id: 42

name: Mumbai Casting Agency type: web url: https://example.com/casting tier: 1 poll_interval_minutes: 30 fetch: curl_cffi extractor: css:mumbai_agency_v2 cadence_hint: weekly contact: casting@example.com notes: vetted by hand 2026-11

## Appendix C: Glossary


| Term | Meaning |
| --- | --- |
| BM25 | A classic keyword relevance score; here via SQLite FTS5 |
| Embedding | A numeric vector capturing meaning; similar texts have similar vectors |
| MinHash / LSH | Fast approximate way to find near-duplicate texts |
| Jaccard similarity | Overlap between two sets of word shingles |
| WAL | Write-ahead log; lets SQLite read while writing and enables Litestream |
| Selector drift | A site changed its HTML so old selectors extract nothing |
| Quarantine | Holding area for records that failed validation |
| Span grounding | Checking that quoted evidence really appears in the source text |
| Reputation table | Blocklist of phones, emails and domains from Scam labels |
| Exploration slot | A deliberately random mid-ranked item to avoid biased learning |
| Heartbeat | A periodic ping to an external service proving the system is alive |
| Precision@15 | Share of the top instant alerts that you actually pursued |
