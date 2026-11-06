# Runbook

A checklist you can follow without thinking. Times are IST.

## Every day (~5 minutes)

- [ ] Read the 08:00 digest. Label each item: **Apply**, **Ignore** or **Scam**.
- [ ] If an instant alert arrived overnight, handle it first — deadlines are short.
- [ ] `python -m radar health` — no dead/blocked sources, queue near zero.
- [ ] `python -m radar alerts --limit 10` — anything unresolved for >48h?
- [ ] If you applied to something, `python -m radar label ID apply --note "..."`.

## Every week (~20 minutes)

- [ ] `python -m radar metrics --days 7` — precision@15, outcome rate, scams.
- [ ] `python -m radar audit --sample 10` — did a veto or low score hide something
      you would have wanted? If yes, fix the rule or the profile, not the score.
- [ ] `python -m radar quarantine --limit 20` — resolve or replay failures.
- [ ] `python -m radar sources list` — disable low-yield sources, add vetted ones.
- [ ] `python -m radar weekly` — weekly report + refit (also runs Monday 09:00).
- [ ] Check the dashboard overview for a source that has gone silent.
- [ ] Glance at `data/logs` for repeated tracebacks.

## Every month (~30 minutes)

- [ ] `python -m radar backup verify` — must report `"ok": true`.
- [ ] Confirm off-machine replication (Litestream lag, or rclone/restic job).
- [ ] `python -m radar doctor` — optional extras still import, config valid.
- [ ] Review `config/veto_patterns.yaml` against scams you saw this month; add
      a pattern and a regression fixture.
- [ ] Review `config/profile.yaml` — age range, travel radius, paid-only,
      fee tolerance still accurate?
- [ ] Check disk usage: `data/backups/`, raw documents, logs.

## Every quarter (~1 hour)

- [ ] **Restore drill**: restore last night's snapshot into a temp directory,
      run `doctor` and `opportunities`, then delete it.
- [ ] Rotate `RADAR_REPLY_SECRET` and ntfy tokens if they may have been exposed.
- [ ] Re-read [SECURITY.md](../SECURITY.md) safety rules.
- [ ] Prune sources that have not produced an application in 90 days.
- [ ] Update dependencies in a branch, run `pytest`, deploy after a backup.

## When something breaks

| Symptom | First command | Then |
| --- | --- | --- |
| No alerts | `python -m radar health` | Check jobs, thresholds, quiet hours, ntfy config |
| Source silent | `python -m radar health` | Update CSS extractor or disable source |
| DB locked | `ps aux \| grep radar` | Ensure only one writer is running |
| Disk full | `python -m radar health` | Lower retention, prune backups, archive raw docs |
| Corrupt DB | `python -m radar backup verify` | Stop worker, restore snapshot |
| Scam got through | `python -m radar label ID scam` | Add veto pattern + fixture |

Full procedures: [OPERATIONS.md](../OPERATIONS.md).

## Alert hygiene

- Instant alerts are for score ≥ 80, complete, clean items outside quiet hours.
- If you are ignoring most instant alerts, raise `thresholds.instant` — do not
  stop reading them.
- If you are missing good calls, check `audit` before lowering the threshold:
  the problem is usually a missing source, not a strict score.
- Exploration slots (2 by default) intentionally surface one or two lower-scored
  items so the model keeps learning. Label them honestly.
