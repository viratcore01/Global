# Adding sources

The source list is the real product. The code is replaceable; a small list of
hand-vetted sources that actually publish casting calls in your market is not.

Everything ships disabled. Enable a source only after you have visited it and
confirmed it is legitimate and worth your time.

```yaml
# config/sources.yaml
sources:
  - name: "Mumbai casting board"
    type: web
    url: "https://example.com/casting"
    enabled: true
    tier: 1                 # 1 = trusted, 2 = normal, 3 = noisy/experimental
    poll_interval_minutes: 60
    extractor: auto         # auto | jsonld | css | heuristic | text
    fetch: http             # http | curl_cffi | browser | scrapling | auto
    config: {}
```

After editing, run `python -m radar sources sync` (or `python -m radar doctor`) so
the database picks up the change. `python -m radar sources disable "Name"` is the
fastest way to quiet a noisy source without deleting it.

## Tiers and trust

`tier` feeds the trust feature: tier 1 sources score higher, tier 3 lower.
Corroboration (multiple sources in one cluster) raises trust further. A source
labelled Scam once is marked dead; `python -m radar sources list` shows status.

---

## Email (best source for agency calls)

```yaml
- name: "Email inbox"
  type: email
  enabled: true
  tier: 1
  poll_interval_minutes: 5
  contact: "you@example.com"
```

`.env`:

```
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=you@gmail.com
IMAP_PASSWORD=your-16-char-app-password
IMAP_FOLDER=INBOX
IMAP_ENABLED=true
```

- Create a dedicated mailbox, subscribe it to agency newsletters, casting
  newsletters and festival announcements.
- Gmail: enable 2FA, create an App Password. Never use the account password.
- `IMAP_LABEL_FOLDER=Radar` restricts ingestion to one Gmail label (filters can
  auto-apply it).
- Fetching uses `BODY.PEEK`, so mail is never marked read.
- `python -m radar imap` polls once or runs IMAP IDLE; the worker does it for you.

---

## RSS / Atom / Google Alerts

```yaml
- name: "Casting newsletter"
  type: rss
  url: "https://example.com/feed.xml"
  tier: 2
  poll_interval_minutes: 30
  assume_opportunity: true
```

`assume_opportunity: true` skips the "does this look like an opportunity?"
classifier when the feed is entirely casting calls.

Google Alerts: create alerts (e.g. `"casting call" Mumbai audition`,
`"open call" actor India`, `"short film" submission deadline India`), deliver to
**RSS**, paste the feed URL with `type: google_alerts`.

---

## Sitemap watcher

```yaml
- name: "Casting site sitemap"
  type: sitemap
  url: "https://example.com"
  poll_interval_minutes: 360
  config:
    include: ["/casting/", "/audition", "/open-call", "/jobs/", "/festival"]
    exclude: ["/tag/", "/author/", "/privacy", "/login"]
    max_urls_per_run: 200
```

Reads `robots.txt`, expands sitemap indexes and `.gz` files, and only queues new
or changed URLs (`lastmod` + URL diff). Best for large boards where scraping the
listing page is wasteful.

---

## HTML listing page

```yaml
- name: "Agency listing"
  type: web
  url: "https://example.com/casting"
  poll_interval_minutes: 60
  config:
    link_selector: "a[href]"
    link_include: ["/casting/", "/audition"]
    max_links: 50
```

If `link_selector` is omitted, the page itself is treated as one opportunity.
Outbound links that look like hubs can be queued as `hub_candidates` for manual
approval; approve with `python -m radar sources ...`/SQL review, never blindly.

---

## Source-specific CSS extractor

Use this once you know a site's markup; it is the highest-precision path.

```yaml
- name: "Casting detail pages"
  type: web
  url: "https://example.com/casting/123"
  extractor: css
  extractor_config:
    fields:
      title: { selector: "h1" }
      description: { selector: "article" }
      deadline: { selector: ".deadline, time" }
      city: { selector: ".location" }
      contact: { selector: ".contact" }
    # If the page lists several opportunities:
    # item: "article.casting-card"
```

Missing fields fall back to heuristics over the main text, so partial configs are
fine. Evidence spans are still recorded and verified.

---

## Telegram public channels

```yaml
- name: "Casting channel"
  type: telegram
  url: "https://t.me/s/CHANNEL_NAME"
  poll_interval_minutes: 15
  config:
    max_pages: 3
    enqueue_external_links: true
```

Only channels with public preview enabled. The adapter paginates `t.me/s/` pages
and expands link previews (`telegram_preview.html` in tests). Private channels
need Telethon and your own account; that is out of scope.

---

## SearXNG sweeps

```yaml
- name: "SearXNG"
  type: searxng
  url: "http://127.0.0.1:8888"
  poll_interval_minutes: 360
  config:
    queries:
      - 'site:.in "casting call"'
      - 'site:.in "audition" actor'
      - 'site:.in "short film" submission deadline'
      - 'site:filmfreeway.com "short film"'
    max_results_per_query: 20
    language: "en"
```

Self-host SearXNG, enable the JSON format, set `SEARXNG_URL` and
`SEARXNG_ENABLED=true` (the source is skipped with an explicit error otherwise,
so a half-configured sweeper is never silent). Date filtering is done in code
because engine support varies. Start broad, then delete queries that produce
noise — every query is a permanent source of work.

---

## Wayback CDX (low yield, late build)

```yaml
- name: "Wayback"
  type: wayback
  url: "example.com"
  poll_interval_minutes: 1440
  config:
    patterns: ["casting", "open-call", "audition"]
    max_pages: 3
```

Finds pages a site published and removed. One request per two seconds, honours
`Retry-After`, resumes with CDX resume keys. Useful for discovering a site's URL
patterns, not for fresh calls.

---

## Discord (read-only)

```yaml
- name: "Discord casting server"
  type: discord
  poll_interval_minutes: 5
  config:
    guild_ids: ["123456789012345678"]
    channel_ids: ["123456789012345678"]
```

Bot token in `.env` (`DISCORD_BOT_TOKEN`), Message Content intent enabled, only
servers where an admin invited the bot. No user tokens, no DMs.

---

## Demo fixtures

```yaml
- name: "Demo fixtures"
  type: demo
  enabled: true
```

Local fixtures only. Used by `init --demo` and tests; no network.

---

## What not to add

- Login-only boards, paywalled posts, CAPTCHA-protected pages.
- Sites whose robots.txt forbids the path you want. The fetcher will mark them
  `blocked` and stop; don't disable robots to get around it.
- Instagram / Facebook / X / LinkedIn / TikTok. Their terms and anti-bot measures
  make them fragile; they are deliberately out of scope.
- Aggregator spam farms that repost the same call with no original contact.
  If you cannot find who is actually casting, it is not actionable.

## Source hygiene

- Review `python -m radar sources list` and `python -m radar health` weekly.
- Disable a source after ~2 weeks of consistently low relevance; keep the note
  explaining why.
- When a site changes markup, prefer updating the CSS extractor over adding
  another heuristic.
- Add a fixture page under `tests/fixtures/` for any source that needs a custom
  extractor, so a future change cannot silently break it.
