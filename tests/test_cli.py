"""CLI smoke tests: init, doctor, seed-demo, list, label, digest, backup, health."""

from __future__ import annotations

import json

import pytest

from radar import cli


def run(capsys, *args: str) -> tuple[int, str, str]:
    code = cli.main(list(args))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_version(capsys) -> None:
    code, out, _ = run(capsys, "version")
    assert code == 0
    assert "1.0.0" in out


def test_help_without_command(capsys) -> None:
    code, out, _ = run(capsys)
    assert code == 0
    assert "init" in out and "doctor" in out


def test_init_and_doctor(demo_home, capsys) -> None:
    code, out, _ = run(capsys, "init")
    assert code == 0, out
    assert (demo_home / "data" / "radar.db").exists()
    assert (demo_home / "data" / "reply_secret").exists()
    code, out, _ = run(capsys, "doctor", "--json")
    payload = json.loads(out)
    assert payload["ok"] is True, payload
    assert payload["failed"] == 0


def test_init_demo_and_list(demo_home, capsys) -> None:
    assert run(capsys, "init", "--demo", "--json")[0] == 0
    code, out, _ = run(capsys, "opportunities", "--json", "--limit", "50")
    assert code == 0
    rows = json.loads(out)
    assert len(rows) >= 6
    titles = " ".join(row["title"] for row in rows)
    assert "Mumbai Web Series" in titles
    assert any(row["city"] == "Delhi" for row in rows)


def test_seed_demo_and_label_flow(demo_home, capsys) -> None:
    assert run(capsys, "init")[0] == 0
    code, out, _ = run(capsys, "seed-demo")
    assert code == 0, out
    code, out, _ = run(capsys, "opportunities", "--json", "--limit", "5")
    rows = json.loads(out)
    opportunity_id = int(rows[0]["id"])
    code, out, _ = run(capsys, "label", str(opportunity_id), "apply", "--note", "self tape sent", "--json")
    assert code == 0, out
    result = json.loads(out)
    assert result["ok"] is True
    assert result["new_status"] == "applied"
    code, out, _ = run(capsys, "show", str(opportunity_id), "--json")
    assert code == 0
    shown = json.loads(out)
    assert shown["opportunity"]["status"] == "applied"


def test_opportunities_full_text_search(demo_home, capsys) -> None:
    assert run(capsys, "init", "--demo", "--json")[0] == 0
    code, out, _ = run(capsys, "opportunities", "--json", "--q", "hinglish")
    assert code == 0, out
    rows = json.loads(out)
    assert rows, "FTS search returned no rows"
    assert any("Hinglish" in (row.get("title") or "") for row in rows)
    # Filters must compose with the FTS join.
    code, out, _ = run(capsys, "opportunities", "--json", "--q", "hinglish", "--city", "Delhi")
    assert code == 0, out
    assert json.loads(out)


def test_label_accepts_past_tense_alias(demo_home, capsys) -> None:
    run(capsys, "init", "--demo")
    code, out, _ = run(capsys, "opportunities", "--json", "--limit", "1")
    assert code == 0
    opportunity_id = int(json.loads(out)[0]["id"])
    code, out, _ = run(capsys, "label", str(opportunity_id), "applied", "--json")
    assert code == 0, out
    assert json.loads(out)["new_status"] == "applied"


def test_digest_dry_run(demo_home, capsys) -> None:
    run(capsys, "init", "--demo")
    code, out, _ = run(capsys, "digest", "--dry-run", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["reason"] == "dry_run"
    assert payload["items"], payload


def test_run_cycle_offline(demo_home, capsys) -> None:
    run(capsys, "init", "--demo")
    code, out, err = run(capsys, "run")
    assert code == 0, err
    assert "Discovery:" in out
    assert "Ingest:" in out


def test_backup_commands(demo_home, capsys) -> None:
    run(capsys, "init")
    code, out, _ = run(capsys, "backup", "snapshot", "--json")
    assert code == 0
    snapshot = json.loads(out)
    assert snapshot["bytes"] > 0
    code, out, _ = run(capsys, "backup", "verify", snapshot["path"], "--json")
    assert code == 0
    assert json.loads(out)["ok"] is True
    code, out, _ = run(capsys, "backup", "list", "--json")
    assert code == 0
    assert json.loads(out)


def test_backup_restore_roundtrip(demo_home, capsys) -> None:
    run(capsys, "init", "--demo")
    code, out, _ = run(capsys, "backup", "snapshot", "--json")
    assert code == 0, out
    path = json.loads(out)["path"]
    # The restore must not need an open database handle: on Windows the file
    # cannot be moved while the CLI holds it.
    code, out, _ = run(capsys, "backup", "restore", path, "--force", "--json")
    assert code == 0, out
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["verify"]["ok"] is True
    code, out, _ = run(capsys, "health", "--json")
    assert code == 0
    assert json.loads(out)["opportunities"]["total"] >= 6


def test_quarantine_resolve_unknown_id(demo_home, capsys) -> None:
    run(capsys, "init")
    code, out, _ = run(capsys, "quarantine", "resolve", "999", "--json")
    assert code == 1
    assert json.loads(out)["ok"] is False


def test_health_and_metrics(demo_home, capsys) -> None:
    run(capsys, "init", "--demo")
    code, out, _ = run(capsys, "health", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload["integrity"] == "ok"
    assert payload["opportunities"]["total"] >= 6
    code, out, _ = run(capsys, "metrics", "--json")
    assert code == 0
    metrics = json.loads(out)
    assert "precision_at_15" in metrics


def test_quarantine_command(demo_home, capsys) -> None:
    run(capsys, "init")
    code, out, _ = run(capsys, "quarantine", "--json")
    assert code == 0
    assert json.loads(out) == []


def test_invalid_arguments(demo_home, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["label", "1", "not-an-action"])
    assert exc.value.code == 2
