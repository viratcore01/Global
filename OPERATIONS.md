# Operations

How to run the radar for years without it silently dying.

- **One writer.** Only the worker (or a one-off CLI command) may write to
  `data/radar.db`. The dashboard opens it read-only. Don't run `run` while the
  worker is up.
- **The radar cannot report its own death.** Use `HEALTHCHECKS_URL`, or at least
  check `python -m radar health` and the dashboard weekly.
- **An untested backup is not a backup.** Verify monthly; restore quarterly.

---

## Layout on disk

```
data/
  radar.db                 SQLite (WAL: radar.db-wal, radar.db-shm)
  reply_secret             HMAC key for ntfy buttons (chmod 600)
  backups/                 radar-YYYYmmdd-HHMMSS.db snapshots
  logs/                    rotating logs
  tmp/  exports/
```

`RADAR_HOME` overrides the project root; `RADAR_DB`, `RADAR_DATA_DIR` and
`RADAR_CONFIG_DIR` override individual paths.

---

## Install

### Bare Python

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python -m radar init
python -m radar doctor
```

`init` creates the DB, syncs sources, and generates `data/reply_secret` if
`RADAR_REPLY_SECRET` is empty. Keep that file out of git and out of images.

### Docker

```bash
docker compose build
docker compose run --rm radar python -m radar init
docker compose up -d worker
docker compose run --rm radar python -m radar doctor
```

The compose file mounts `./config` and `./data`, so the database, backups and
secrets live on the host. The container runs as a non-root user and drops
capabilities. The dashboard is a separate profile because it needs its own port
and is optional.

### systemd

```bash
sudo cp deploy/systemd/radar-worker.service /etc/systemd/system/
sudo cp deploy/systemd/radar-digest.* deploy/systemd/radar-backup.* /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radar-worker.service radar-digest.timer radar-backup.timer
journalctl -u radar-worker -f
```

Edit `User=`, `WorkingDirectory=` and `ExecStart=` in the units first. The timers
run in the host timezone; set `Environment=TZ=Asia/Kolkata` or use
`OnCalendar=*-*-* 08:00:00 Asia/Kolkata` (systemd ≥ 240).

### Windows

```powershell
.\scripts\run-radar.ps1 doctor
.\scripts\run-radar.ps1 run --force
.\scripts\install-task.ps1            # registers worker + digest + backup tasks
```

The scheduled tasks run under your account, log to `data/logs`, and use
`-NoProfile`. Check them with `Get-ScheduledTask -TaskName 'Radar*'`.

---

## Daily / weekly checks

```bash
python -m radar health              # queue, sources, jobs, disk
python -m radar metrics --days 7    # precision@15, outcomes, scams
python -m radar alerts --limit 20   # what was actually sent
python -m radar audit               # sample vetoed/low-ranked items
```

Healthy looks like: queue `pending` near zero, every enabled source ran within
3× its poll interval, `unresolved_48h` small, no `dead`/`blocked` sources.

---

## Backups

### Automatic

The worker runs a snapshot daily at 03:00 IST and prunes snapshots older than
`BACKUP_RETENTION_DAYS` (default 30). Snapshots use SQLite's online `.backup`
API, so they are consistent while the worker writes.

```bash
python -m radar backup list
python -m radar backup verify            # newest snapshot
python -m radar backup verify data/backups/radar-20260101-030000.db
```

A snapshot is only good if `verify` reports `"ok": true`, `integrity: "ok"` and
non-zero counts.

### Off-machine

Local snapshots do not survive disk loss. Use **one** of:

- `deploy/litestream.yml` with `LITESTREAM_REPLICA_URL` (S3/B2/SSH). Litestream
  streams WAL continuously and restores to any point in time.
- A nightly `rclone`/`restic` copy of `data/backups/` to cloud storage.

### Restore

1. Stop the worker (`systemctl stop radar-worker`, `docker compose stop worker`,
   or stop the Windows task).
2. Copy the current DB aside yourself if you want a second safety net.
3. Run:

```bash
python -m radar backup verify /path/to/radar-YYYYmmdd-HHMMSS.db
python -m radar backup restore /path/to/radar-YYYYmmdd-HHMMSS.db --force
python -m radar doctor
```

`restore` re-verifies the snapshot, moves the existing database to
`radar.db.pre-restore-<timestamp>`, copies the snapshot in, and removes stale
`-wal`/`-shm` files. It refuses to overwrite without `--force`.

4. Start the worker and run `python -m radar run --force` to catch up.

### Restore drill (quarterly)

Restore last night's snapshot into a throwaway directory, run `doctor` and
`opportunities`, then delete it. A backup you have never restored is a guess.

---

## Rotating secrets

| Secret | Where | How to rotate |
| --- | --- | --- |
| `RADAR_REPLY_SECRET` | `.env` or `data/reply_secret` | Generate a new random value, update both, restart worker. Old buttons stop working — that's fine. |
| `NTFY_TOKEN` / `NTFY_REPLY_TOKEN` | `.env` | Create a new token in ntfy, update `.env`, restart, revoke the old one. |
| `IMAP_PASSWORD` | `.env` | Gmail App Password: revoke and reissue, update `.env`, restart. |
| `LLM_API_KEY` | `.env` | Rotate at the provider, update `.env`, restart. |

After rotating the reply secret, previously delivered notification buttons will
fail signature verification. That is intentional.

---

## Upgrades

1. `python -m radar backup snapshot`
2. Pull the new code, `pip install -r requirements.txt` (deps are pinned with
   upper bounds).
3. Stop the worker, `python -m radar doctor`, start the worker.
4. Check `python -m radar health` and the log for `Applied migration` lines.

Migrations are additive and idempotent per version. **Never** edit an applied
migration file; write `003_*.sql` instead.

---

## Incident response

### "I stopped getting alerts"

1. `python -m radar health` — job last-run times and source warnings.
2. `python -m radar alerts --limit 5` — was anything sent? If nothing is being
   produced, the problem is upstream.
3. `python -m radar digest --dry-run` — are there candidates? If yes, check ntfy:
   `NTFY_TOPIC`, token, and the ntfy app subscription.
4. Check quiet hours (`RADAR_QUIET_START`/`END`), `RADAR_DAILY_ALERT_CAP`, and
   whether everything is scoring below `thresholds.instant`.
5. If the worker is alive but no job runs, `job_runs` is stale — restart it. If it
   restarts in a crash loop, run the failing job directly
   (`python -m radar run --force`) to see the traceback.

### "A source went silent"

```bash
python -m radar sources list
python -m radar health
```

If status is `selector_drift` or `empty`, the site's markup changed: update the
CSS extractor in `config/sources.yaml` or disable the source. If `blocked`, read
the robots.txt change — do not work around it. If `failing`, fetch the URL by hand
and check the HTTP status.

### "The database is locked"

Only one write path is allowed. Stop any stray `run`/`ingest`/`serve` writer, and
confirm the worker is the only process with `radar.db` open. WAL readers never
block the writer; a persistent lock usually means a second worker.

### "I restored the wrong database"

`restore` left the old one at `data/radar.db.pre-restore-<timestamp>`. Stop the
worker, move it back, delete `-wal`/`-shm`, restart.

### Suspected scam got through

1. `python -m radar show ID` and read the evidence.
2. `python -m radar label ID scam --note "..."` — this writes a reputation entry
   that vetoes the contact/domain everywhere.
3. Add the missing pattern to `config/veto_patterns.yaml` and add a regression
   case to `tests/fixtures/` if it is a new class of scam.

### Disk full

Snapshots and raw documents are the usual growth. Check
`python -m radar health`, lower `BACKUP_RETENTION_DAYS`, prune `data/backups/`,
and archive old raw documents (they are immutable, so copying and deleting is
safe if you keep the archived copy).

---

## Metrics that matter

| Metric | Target | Meaning |
| --- | --- | --- |
| `precision_at_15` | > 0.35 | share of instant alerts you acted on |
| `outcome_rate` | > 0.15 | replies/bookings per application |
| `scam_labels` | > 0 | you are catching the bad ones |
| `unresolved_48h` | small | alerts you never answered |
| `quarantine_new` | small, non-zero | extraction is not silently failing |
| source warnings | 0 | nothing silently dead |

`weekly` prints a threshold recommendation when there are enough alerts; apply it
to `config/profile.yaml` deliberately, not automatically.
