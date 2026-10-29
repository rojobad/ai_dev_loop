"""Phase 30.2 usage-backfill request, check, and old-schema boundaries."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.usage_backfill import (
    parse_usage_backfill_request,
    run_usage_backfill,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()


def _request(**overrides: object) -> object:
    values: dict[str, object] = {
        "all_runs": False,
        "run_id": None,
        "project_name": None,
        "check": False,
        "apply": False,
        "batch_size": 32,
        "max_attempts": 100,
        "resume_job_id": None,
    }
    values.update(overrides)
    return parse_usage_backfill_request(**values)  # type: ignore[arg-type]


def test_cli_rejects_conflicting_scopes_and_modes_without_creating_a_ledger(
    isolated_xdg: Path,
) -> None:
    db = isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3"
    cases = (
        ["scheduler", "usage-backfill", "--all", "--run-id", "run-1", "--check"],
        ["scheduler", "usage-backfill", "--check", "--apply", "--all"],
        ["scheduler", "usage-backfill", "--all"],
        ["scheduler", "usage-backfill", "--all", "--check", "--batch-size", "0"],
        ["scheduler", "usage-backfill", "--all", "--check", "--max-attempts", "100001"],
        ["scheduler", "usage-backfill", "--resume", "not-a-job", "--apply"],
    )
    for args in cases:
        result = runner.invoke(app, args)
        assert result.exit_code != 0, args
        assert not db.exists()
        assert not db.parent.exists()


def test_check_missing_ledger_does_not_create_parents(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "engine.sqlite3"
    request = _request(all_runs=True, check=True)
    with pytest.raises(SchedulerEngineError, match="scheduler database not found"):
        run_usage_backfill(
            request,  # type: ignore[arg-type]
            db_path=database,
            artifact_root=tmp_path / "artifacts",
        )
    assert not database.exists()
    assert not database.parent.exists()
    assert not (tmp_path / "artifacts").exists()


def test_empty_ledger_check_is_ready_and_apply_resume_does_not_invent_facts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(database)
    artifacts = tmp_path / "artifacts"
    checked = run_usage_backfill(
        _request(all_runs=True, check=True),  # type: ignore[arg-type]
        db_path=database,
        artifact_root=artifacts,
    )
    assert checked.statistically_ready is True
    assert checked.provider_coverage == "not_applicable"
    assert checked.eligible_terminal == 0
    assert checked.materialization == "complete"
    assert checked.unmaterialized == 0
    assert not artifacts.exists()

    applied = run_usage_backfill(
        _request(all_runs=True, apply=True, batch_size=1, max_attempts=1),  # type: ignore[arg-type]
        db_path=database,
        artifact_root=artifacts,
    )
    assert applied.job_status == "completed"
    assert applied.job_id is not None
    assert len(applied.job_id) == 32
    with sqlite3.connect(database) as conn:
        jobs = conn.execute("SELECT COUNT(*) FROM scheduler_usage_backfill_jobs").fetchone()[0]
        facts = conn.execute("SELECT COUNT(*) FROM scheduler_usage_attempt_facts").fetchone()[0]
        updated = conn.execute(
            "SELECT updated_at FROM scheduler_usage_backfill_jobs WHERE job_id = ?",
            (applied.job_id,),
        ).fetchone()[0]
    assert jobs == 1
    assert facts == 0

    resumed = run_usage_backfill(
        _request(apply=True, resume_job_id=applied.job_id, batch_size=1, max_attempts=1),  # type: ignore[arg-type]
        db_path=database,
        artifact_root=artifacts,
    )
    assert resumed.job_id == applied.job_id
    assert resumed.job_status == "completed"
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_usage_backfill_jobs").fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT updated_at FROM scheduler_usage_backfill_jobs WHERE job_id = ?",
                (applied.job_id,),
            ).fetchone()[0]
            == updated
        )
    with pytest.raises(SchedulerEngineError, match="scope does not match"):
        run_usage_backfill(
            _request(  # type: ignore[arg-type]
                apply=True,
                run_id="run-1",
                resume_job_id=applied.job_id,
                batch_size=1,
                max_attempts=1,
            ),
            db_path=database,
            artifact_root=artifacts,
        )


def test_check_on_schema_21_does_not_migrate_or_create_job_tables(tmp_path: Path) -> None:
    database = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_items")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_jobs")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 22")
        conn.execute("PRAGMA user_version = 21")
        conn.commit()
    before = database.read_bytes()
    report = run_usage_backfill(
        _request(all_runs=True, check=True),  # type: ignore[arg-type]
        db_path=database,
        artifact_root=tmp_path / "unused-artifacts",
    )
    assert report.statistically_ready is True
    assert report.provider_coverage == "not_applicable"
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_backfill_jobs" not in names
    assert "scheduler_usage_attempt_facts" in names
    rendered = runner.invoke(app, ["scheduler", "usage-backfill", "--help"])
    assert rendered.exit_code == 0
    assert "--check" in rendered.stdout
    assert "--apply" in rendered.stdout
    assert "--resume" in rendered.stdout


def test_cli_check_does_not_migrate_schema_21(isolated_xdg: Path) -> None:
    database = isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3"
    database.parent.mkdir(parents=True)
    SqliteSchedulerStore(database)
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_items")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_jobs")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 22")
        conn.execute("PRAGMA user_version = 21")
        conn.commit()
    before = database.read_bytes()
    result = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--all", "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["statistically_ready"] is True
    assert payload["provider_coverage"] == "not_applicable"
    assert payload["eligible_terminal"] == 0
    assert "job_id" in payload
    assert database.read_bytes() == before
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 21
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_backfill_jobs" not in names


def test_f03_schema11_unmaterialized_history_is_unknown_and_check_does_not_migrate(
    isolated_xdg: Path,
) -> None:
    del isolated_xdg
    fixture = Path("tests/fixtures/phase21_4_pre_21_4_historical")
    engine = fixture / "engine.sqlite3"
    digest = hashlib.sha256(engine.read_bytes()).hexdigest()
    assert digest == "43da316de14f2e9cd8fb9cdcbf27c78a36c1e7e20bb8913040377c160edec8cb"
    database = default_engine_db_path()
    database.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(engine, database)
    artifacts = default_artifact_root()
    shutil.copytree(fixture / "artifacts", artifacts)
    before_db = database.read_bytes()
    before_artifacts = _tree_digest(artifacts)

    result = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--all", "--check", "--output", "json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["eligible_terminal"] == 2
    assert payload["unmaterialized"] == 2
    assert payload["materialized"] == 0
    assert payload["provider_coverage"] == "unknown"
    assert payload["materialization"] == "incomplete"
    assert payload["statistically_ready"] is False
    assert payload["remaining"] == 2
    assert "backfill_required" in payload["diagnostics"]
    assert database.read_bytes() == before_db
    assert _tree_digest(artifacts) == before_artifacts
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 11
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_attempt_facts" not in names
    assert "scheduler_usage_backfill_jobs" not in names


def _tree_digest(root: Path) -> str:
    lines = [
        f"{path.relative_to(root).as_posix()} {hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def test_bounds_and_unknown_scope_use_typed_errors(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="integers from 1 to 100000"):
        _request(all_runs=True, check=True, batch_size=True)  # type: ignore[arg-type]
    database = tmp_path / "engine.sqlite3"
    SqliteSchedulerStore(database)
    with pytest.raises(SchedulerEngineError, match="run not found"):
        run_usage_backfill(
            _request(check=True, run_id="missing-run"),  # type: ignore[arg-type]
            db_path=database,
            artifact_root=tmp_path / "artifacts",
        )
    with pytest.raises(SchedulerEngineError, match="project not found"):
        run_usage_backfill(
            _request(check=True, project_name="missing-project"),  # type: ignore[arg-type]
            db_path=database,
            artifact_root=tmp_path / "artifacts",
        )
