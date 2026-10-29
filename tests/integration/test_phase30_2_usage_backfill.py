"""Phase 30.2 historical usage backfill through the production scheduler."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase25_3_sdk_recovery import _submit_sdk_run, _tick_until_kind
from tests.unit.scheduler.test_phase27_2_codex_implementer import _run_until, _submit, _tick
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.integration_api.dashboard_service import default_dashboard_read_service
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.process_output_auth import authenticate_process_attempt
from ai_dev_loop.integration_api.run_service import default_run_read_service
from ai_dev_loop.scheduler.application.attempt_envelope import envelope_sha256
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.token_usage_query import scheduler_usage
from ai_dev_loop.scheduler.application.usage_backfill import (
    UsageBackfillBarrier,
    parse_usage_backfill_request,
    run_usage_backfill,
)
from ai_dev_loop.scheduler.application.usage_ingestion import settle_ingested_attempt_usage
from ai_dev_loop.scheduler.domain.token_usage import UsageObservationV1
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()
_IMPLEMENTER_110 = {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 10}
_IMPLEMENTER_50 = {"input_tokens": 40, "cached_input_tokens": 0, "output_tokens": 10}
_DEADLINE_USAGE = {
    "input_tokens": 100,
    "output_tokens": 10,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
    "total_tokens": 110,
}
_AUTHORITY_TABLES = (
    "scheduler_runs",
    "scheduler_events",
    "scheduler_attempts",
    "scheduler_effects",
)
_IMPLEMENTER_USAGE = {
    "input_tokens": 100,
    "cached_input_tokens": 60,
    "output_tokens": 10,
    "reasoning_output_tokens": 4,
}
_REVIEW_USAGE = {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}


def test_b01_b02_backfill_preserves_counters_across_resume_conflict_and_source_loss(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "implemented-by-codex.txt")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([_IMPLEMENTER_USAGE]),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([_REVIEW_USAGE]),
    )
    paths = _paths(isolated_xdg)
    run_id = _submit(git_repo, paths)
    start_run(run_id, db_path=paths["db_path"])
    _run_until(_tick(git_repo, paths), run_id, "completed")
    assert _direct_total(paths, run_id) == 140
    assert _observation_count(paths["db_path"]) == 2

    ready = _run(paths, all_runs=True, check=True)
    assert ready.statistically_ready is True
    assert ready.provider_coverage == "complete"
    assert ready.known == 2
    assert ready.unmaterialized == 0
    project = _project_name(paths["db_path"])
    scoped = _run(paths, project_name=project, check=True)
    assert scoped.eligible_terminal == 2
    assert scoped.statistically_ready is True
    _set_one_attempt_status(paths["db_path"], run_id, "active")
    active = _run(paths, run_id=run_id, check=True)
    assert active.active_unsettled >= 1
    assert active.statistically_ready is False
    assert _direct_total(paths, run_id) == 140
    _set_one_attempt_status(paths["db_path"], run_id, "completed")
    assert _run(paths, run_id=run_id, check=True).statistically_ready is True

    _clear_facts(paths["db_path"])
    _assert_classification_releases_sqlite(paths, monkeypatch)
    missing = _run(paths, run_id=run_id, check=True)
    assert missing.statistically_ready is False
    assert missing.unmaterialized == 2
    assert missing.provider_coverage == "unknown"
    assert "backfill_required" in missing.diagnostics
    assert _direct_total(paths, run_id) is None

    partial = _run(paths, run_id=run_id, apply=True, batch_size=1, max_attempts=1)
    assert partial.job_status == "running"
    assert partial.unmaterialized == 1
    assert partial.provider_coverage == "partial"
    assert _fact_count(paths["db_path"]) == 1
    resumed = _run(
        paths,
        apply=True,
        resume_job_id=partial.job_id,
        batch_size=1,
        max_attempts=1,
    )
    assert resumed.job_status == "completed"
    assert resumed.statistically_ready is True
    assert _direct_total(paths, run_id) == 140
    assert _observation_count(paths["db_path"]) == 2

    _clear_facts(paths["db_path"])
    with pytest.raises(UsageBackfillBarrier, match="after_fact_before_cursor"):
        _run(
            paths,
            run_id=run_id,
            apply=True,
            batch_size=32,
            max_attempts=100,
            barrier="after_fact_before_cursor",
        )
    assert _fact_count(paths["db_path"]) == 1
    with sqlite3.connect(paths["db_path"]) as conn:
        pending = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_usage_backfill_items
            WHERE status = 'pending'
            """
        ).fetchone()[0]
        water = conn.execute(
            "SELECT high_water, status FROM scheduler_usage_backfill_jobs ORDER BY created_at DESC"
        ).fetchone()
    assert pending >= 1
    assert int(water[0]) == 0
    assert str(water[1]) == "running"
    with sqlite3.connect(paths["db_path"]) as conn:
        crashed_job = str(
            conn.execute(
                "SELECT job_id FROM scheduler_usage_backfill_jobs ORDER BY created_at DESC"
            ).fetchone()[0]
        )
    recovered = _run(
        paths,
        apply=True,
        resume_job_id=crashed_job,
        batch_size=32,
        max_attempts=100,
    )
    assert recovered.job_status == "completed"
    assert _direct_total(paths, run_id) == 140
    assert _observation_count(paths["db_path"]) == 2

    _clear_facts(paths["db_path"])

    def ingest_first(attempt_id: str) -> None:
        store = SqliteSchedulerStore(paths["db_path"])
        artifacts = ProtectedArtifactStore(paths["artifact_root"])
        with store.begin_immediate() as conn:
            settle_ingested_attempt_usage(
                store,
                artifacts,
                conn,
                attempt_id=attempt_id,
                now=datetime.now(tz=UTC),
            )

    raced = _run(
        paths,
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
        before_commit=ingest_first,
    )
    assert raced.statistically_ready is True
    assert _direct_total(paths, run_id) == 140
    assert _observation_count(paths["db_path"]) == 2

    _assert_source_class_then_enrich(
        paths, run_id, replacement=b"{}", code="usage_source_malformed"
    )
    _assert_source_class_then_enrich(
        paths,
        run_id,
        replacement=None,
        code="usage_source_corrupt",
    )

    preserved = _observation_totals(paths["db_path"])
    _retarget_one_observation(paths, run_id)
    conflicted = _run(paths, run_id=run_id, apply=True, batch_size=32, max_attempts=100)
    assert conflicted.conflicting >= 1
    assert conflicted.statistically_ready is False
    assert _direct_total(paths, run_id) == 140
    assert _observation_totals(paths["db_path"]) == preserved

    held = paths["db_path"].parent / "held-ledger.sqlite3"
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(paths["db_path"], held)
    _clear_facts(held)
    shutil.rmtree(paths["artifact_root"])
    retained = _run(paths, run_id=run_id, apply=True, batch_size=32, max_attempts=100)
    assert _direct_total(paths, run_id) == 140
    assert retained.conflicting >= 1
    lost = _run(
        {"db_path": held, "artifact_root": paths["artifact_root"]},
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
    )
    assert lost.statistically_ready is True
    assert lost.provider_coverage == "unknown"
    assert lost.remaining == 0
    assert lost.unknown >= 1
    assert "usage_source_missing" in lost.diagnostics
    assert _direct_total({"db_path": held, "artifact_root": paths["artifact_root"]}, run_id) is None
    assert _direct_total(paths, run_id) == 140

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
        _refuse_source,
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts."
        "ReadOnlyProtectedArtifactStore.read_verified_bytes",
        _refuse_source,
    )
    assert _direct_total(paths, run_id) == 140
    inspected = default_run_read_service(
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    ).inspect_run(run_id)
    assert inspected.token_usage.direct.known_total_tokens == 140
    assert inspected.token_usage.direct.coverage == "complete"
    start, end = _window(paths["db_path"])
    dashboard = default_dashboard_read_service(
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    ).token_usage(from_value=start, to_value=end, time_zone="UTC", run_id=run_id)
    payload = dashboard.model_dump(mode="json", by_alias=True)
    assert payload["source"]["knownTotalTokens"] == 140
    assert payload["rows"][0]["configuredModel"] in {"gpt-5.5", "gpt-5.6-sol"}


def test_f01_reconcile_publication_imports_onto_the_logical_attempt(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(_DEADLINE_USAGE))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, database, artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="Reconcile usage backfill",
    )
    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    paths = {"db_path": database, "artifact_root": artifacts}
    assert _direct_total(paths, run_id) == 110
    authority = _authority_digest(database)
    artifact_digest = _tree_digest(artifacts)
    provenance = {
        "generator": "fake SDK deadline with terminal reconciliation usage and no streamed usage",
        "independent_total": 110,
        "authority_sha256": authority,
        "artifact_sha256": artifact_digest,
    }
    (tmp_path / "cursor-provenance.json").write_text(json.dumps(provenance), encoding="utf-8")

    _clear_facts(database)
    assert _direct_total(paths, run_id) is None
    applied = _resume_until_done(paths, run_id)
    assert applied.job_status == "completed"
    assert _direct_total(paths, run_id) == 110
    _assert_reconcile_provenance(database, run_id)
    before_replay = _observation_count(database)
    replayed = _run(paths, run_id=run_id, apply=True, batch_size=32, max_attempts=100)
    assert replayed.statistically_ready is True
    assert _direct_total(paths, run_id) == 110
    assert _observation_count(database) == before_replay

    imported = tmp_path / "imported-cursor.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(database, imported)
    _rewind_to_schema_20(database)
    check = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--run-id", run_id, "--check", "--output", "json"],
    )
    assert check.exit_code == 0, check.stderr
    checked = json.loads(check.stdout)
    assert checked["provider_coverage"] == "unknown"
    assert checked["unmaterialized"] >= 1
    assert checked["statistically_ready"] is False
    with sqlite3.connect(database) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
    text = runner.invoke(
        app,
        [
            "scheduler",
            "usage-backfill",
            "--run-id",
            run_id,
            "--apply",
            "--batch-size",
            "1",
            "--max-attempts",
            "1",
        ],
    )
    assert text.exit_code == 0, text.output
    job_id, job_status = _text_job(text.stdout)
    assert job_status == "running"
    jobs_before = _job_ids(database)
    resumed = _cli_resume(job_id)
    assert resumed.exit_code == 0, resumed.output
    resumed_id, resumed_status = _text_job(resumed.stdout)
    assert resumed_id == job_id
    assert resumed_status == "completed"
    assert _job_ids(database) == jobs_before
    usage = json.loads(
        scheduler_usage(run_id, db_path=database, artifact_root=artifacts).model_dump_json()
    )
    assert usage["direct"]["known_total_tokens"] == 110
    cursor = _breakdown(usage["direct"]["breakdowns"], provider="cursor", role="implementer")
    assert cursor["categories"]["cache_write"]["known_total"] == 0
    assert _authority_digest(database) == authority
    assert _tree_digest(artifacts) == artifact_digest
    shutil.rmtree(artifacts)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
        _refuse_source,
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts."
        "ReadOnlyProtectedArtifactStore.read_verified_bytes",
        _refuse_source,
    )
    assert _direct_total(paths, run_id) == 110
    assert _direct_total({"db_path": imported, "artifact_root": artifacts}, run_id) == 110


def test_f02_f04_f05_f06_bindings_debt_receipt_and_historical_import(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "implemented-by-codex.txt")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([[_IMPLEMENTER_110, _IMPLEMENTER_50]]),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([_REVIEW_USAGE]),
    )
    paths = _paths(isolated_xdg)
    run_id = _submit(git_repo, paths)
    start_run(run_id, db_path=paths["db_path"])
    _run_until(_tick(git_repo, paths), run_id, "completed")
    assert _direct_total(paths, run_id) == 190
    usage = json.loads(
        scheduler_usage(
            run_id, db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).model_dump_json()
    )
    implementer = _breakdown(usage["direct"]["breakdowns"], provider="codex", role="implementer")
    reviewer = _breakdown(usage["direct"]["breakdowns"], provider="codex", role="reviewer")
    assert implementer["known_total_tokens"] == 160
    assert implementer["coverage"] == "complete"
    assert reviewer["known_total_tokens"] == 30
    assert implementer["categories"]["cache_read"]["known_total"] == 0
    _assert_check_ready_without_sources(paths, run_id, monkeypatch)
    authority = _authority_digest(paths["db_path"])
    artifact_digest = _tree_digest(paths["artifact_root"])
    (tmp_path / "codex-provenance.json").write_text(
        json.dumps(
            {
                "generator": "fake Codex implementer and reviewer rewound to schema 20",
                "implementer_total": 160,
                "reviewer_total": 30,
                "direct_total": 190,
                "authority_sha256": authority,
                "artifact_sha256": artifact_digest,
            }
        ),
        encoding="utf-8",
    )

    totals = _observation_totals(paths["db_path"])
    survivor = next(item for item, total in totals.items() if total == 110)
    removed = next(item for item, total in totals.items() if total == 50)
    original = _observation_row(paths["db_path"], removed)
    extra = dict(original)
    extra["observation_id"] = "ab" * 32
    extra["native_key"] = str(extra["native_key"]) + ":unreferenced"
    normalized = json.loads(str(extra["normalized_payload"]))
    normalized["total_tokens"] = 99999
    extra["normalized_payload"] = json.dumps(normalized)
    _insert_observation(paths["db_path"], extra)
    assert _direct_total(paths, run_id) == 190
    _delete_observation(paths["db_path"], removed)
    _assert_binding_failure(paths, run_id)
    _assert_cli_check_rejects(paths, run_id)
    _insert_observation(paths["db_path"], original)
    survivor_publisher = str(_observation_row(paths["db_path"], survivor)["publisher_attempt_id"])
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute(
            """
            UPDATE scheduler_usage_observations
            SET publisher_attempt_id = 'mismatch'
            WHERE observation_id = ?
            """,
            (survivor,),
        )
        conn.commit()
    _assert_binding_failure(paths, run_id)
    _assert_cli_check_rejects(paths, run_id)
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute(
            """
            UPDATE scheduler_usage_observations
            SET publisher_attempt_id = ?
            WHERE observation_id = ?
            """,
            (survivor_publisher, survivor),
        )
        conn.commit()
    assert _direct_total(paths, run_id) == 190
    survivor_digest = str(_observation_row(paths["db_path"], survivor)["payload_sha256"])
    _corrupt_observation_digest(paths["db_path"], survivor)
    _assert_cli_check_rejects(paths, run_id)
    _restore_observation_digest(paths["db_path"], survivor, survivor_digest)
    assert _direct_total(paths, run_id) == 190

    relative = str(original["source_relative_path"])
    debt_db = tmp_path / "debt.sqlite3"
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copyfile(paths["db_path"], debt_db)
    _keep_one_observation_as_debt(debt_db, survivor)
    debt_paths = {"db_path": debt_db, "artifact_root": paths["artifact_root"]}
    debt = _run(debt_paths, run_id=run_id, check=True)
    assert debt.statistically_ready is False
    assert debt.remaining >= 1
    assert debt.active_unsettled >= 1
    assert debt.unknown == 0
    assert debt.provider_coverage == "complete"
    assert _direct_total(debt_paths, run_id) == 140
    with sqlite3.connect(debt_db) as conn:
        settlement = conn.execute(
            """
            SELECT settlement_status, coverage FROM scheduler_usage_attempt_facts
            WHERE role = 'implementer'
            """
        ).fetchone()
    assert settlement == ("debt", None)

    missing_db = tmp_path / "missing.sqlite3"
    corrupt_db = tmp_path / "corrupt.sqlite3"
    missing_artifacts = tmp_path / "missing-artifacts"
    corrupt_artifacts = tmp_path / "corrupt-artifacts"
    shutil.copyfile(paths["db_path"], missing_db)
    shutil.copyfile(paths["db_path"], corrupt_db)
    shutil.copytree(paths["artifact_root"], missing_artifacts)
    shutil.copytree(paths["artifact_root"], corrupt_artifacts)
    _rewind_to_schema_20(missing_db)
    _rewind_to_schema_20(corrupt_db)
    (_run_root(missing_artifacts, run_id) / relative).unlink()
    _corrupt_one_envelope(corrupt_artifacts, run_id)
    missing = _run(
        {"db_path": missing_db, "artifact_root": missing_artifacts},
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
    )
    assert _direct_total({"db_path": missing_db, "artifact_root": missing_artifacts}, run_id) != 190
    assert missing.statistically_ready is False or "usage_source_missing" in missing.diagnostics
    corrupt = _run(
        {"db_path": corrupt_db, "artifact_root": corrupt_artifacts},
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
    )
    assert _direct_total({"db_path": corrupt_db, "artifact_root": corrupt_artifacts}, run_id) != 190
    assert any(
        code in corrupt.diagnostics
        for code in ("usage_source_corrupt", "usage_source_malformed", "usage_source_missing")
    )

    _rewind_to_schema_20(paths["db_path"])
    check = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--run-id", run_id, "--check", "--output", "json"],
    )
    assert check.exit_code == 0, check.stderr
    checked = json.loads(check.stdout)
    assert checked["provider_coverage"] == "unknown"
    assert checked["unmaterialized"] == 2
    assert checked["materialized"] == 0
    assert checked["statistically_ready"] is False
    with sqlite3.connect(paths["db_path"]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
    text = runner.invoke(
        app,
        [
            "scheduler",
            "usage-backfill",
            "--run-id",
            run_id,
            "--apply",
            "--batch-size",
            "1",
            "--max-attempts",
            "1",
        ],
    )
    assert text.exit_code == 0, text.output
    job_id, job_status = _text_job(text.stdout)
    assert job_status == "running"
    jobs_before = _job_ids(paths["db_path"])
    resumed = _cli_resume(job_id)
    assert resumed.exit_code == 0, resumed.output
    resumed_id, resumed_status = _text_job(resumed.stdout)
    assert resumed_id == job_id
    assert resumed_status == "completed"
    assert _job_ids(paths["db_path"]) == jobs_before
    assert _direct_total(paths, run_id) == 190
    restored = json.loads(
        scheduler_usage(
            run_id, db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).model_dump_json()
    )
    assert (
        _breakdown(restored["direct"]["breakdowns"], provider="codex", role="implementer")[
            "known_total_tokens"
        ]
        == 160
    )
    assert (
        _breakdown(restored["direct"]["breakdowns"], provider="codex", role="reviewer")[
            "known_total_tokens"
        ]
        == 30
    )
    assert _authority_digest(paths["db_path"]) == authority
    assert _tree_digest(paths["artifact_root"]) == artifact_digest


_BASELINE_COMMIT = "1a97becaff1af8c54f892f42740f040d4cd7478c"


def test_f06_baseline_writer_fixtures_survive_current_import(
    isolated_xdg: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Import ledgers written by the accepted schema-20 tree, not a version rewind.

    Dropping usage tables and setting user_version to 20 on a ledger from the
    current writer only proves that the current attempt bytes can be reimported.
    This test exports commit 1a97bec read-only and drives the current CLI.
    """

    archive_dir = tmp_path / "baseline-archive"
    export_root = tmp_path / "baseline-export"
    archive_dir.mkdir()
    export_root.mkdir()
    repo = Path(__file__).resolve().parents[2]
    archive_process = subprocess.Popen(
        ["git", "archive", _BASELINE_COMMIT],
        cwd=repo,
        stdout=subprocess.PIPE,
    )
    assert archive_process.stdout is not None
    with tarfile.open(fileobj=archive_process.stdout, mode="r|") as archive:
        archive.extractall(archive_dir, filter="data")
    assert archive_process.wait() == 0
    exporter = (
        archive_dir / "tests" / "unit" / "scheduler" / "test_export_phase30_baseline_fixture.py"
    )
    shutil.copyfile(repo / "tests" / "support" / "phase30_2_baseline_writer_export.py", exporter)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(archive_dir / "src")
    env["BASELINE_EXPORT_ROOT"] = str(export_root)
    env["TMPDIR"] = "/tmp"
    env["TMP"] = "/tmp"
    env["TEMP"] = "/tmp"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    generated = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/unit/scheduler/test_export_phase30_baseline_fixture.py",
            "--tb=short",
            "-p",
            "no:cacheprovider",
        ],
        cwd=archive_dir,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr

    _assert_damaged_baseline_import(export_root / "codex", tmp_path / "codex-damage", expected=190)
    _assert_damaged_baseline_import(
        export_root / "cursor", tmp_path / "cursor-damage", expected=7549
    )
    _exercise_baseline_cli(
        isolated_xdg,
        export_root / "codex",
        expected_total=190,
        implementer_provider="codex",
        implementer_total=160,
        reviewer_total=30,
    )
    _exercise_baseline_cli(
        isolated_xdg,
        export_root / "cursor",
        expected_total=7549,
        implementer_provider="cursor",
        implementer_total=7519,
        reviewer_total=30,
    )
    paths = _paths(isolated_xdg)
    run_id = (export_root / "cursor" / "run-id.txt").read_text(encoding="utf-8").strip()
    usage = json.loads(
        scheduler_usage(
            run_id, db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).model_dump_json()
    )
    cursor = _breakdown(usage["direct"]["breakdowns"], provider="cursor", role="implementer")
    assert cursor["categories"]["cache_write"]["known_total"] == 0
    assert cursor["categories"]["cache_read"]["known_total"] == 2560
    held = paths["artifact_root"].parent / "baseline-artifacts-held"
    shutil.move(paths["artifact_root"], held)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.token_usage_query.authenticate_process_attempt",
        _refuse_source,
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts."
        "ReadOnlyProtectedArtifactStore.read_verified_bytes",
        _refuse_source,
    )
    try:
        assert _direct_total(paths, run_id) == 7549
        inspected = default_run_read_service(
            db_path=paths["db_path"],
            artifact_root=paths["artifact_root"],
        ).inspect_run(run_id)
        assert inspected.token_usage.direct.known_total_tokens == 7549
    finally:
        if held.exists():
            if paths["artifact_root"].exists():
                shutil.rmtree(paths["artifact_root"])
            shutil.move(held, paths["artifact_root"])


def _remove_one_usage_source(artifacts: Path) -> None:
    observations = list(artifacts.rglob("usage/observations/*/*.json"))
    assert observations, "baseline export has no published usage observation"
    chosen_path: Path | None = None
    chosen: dict[str, object] | None = None
    for path in observations:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and payload.get("role") == "implementer":
            chosen_path = path
            chosen = payload
            break
    assert chosen_path is not None and chosen is not None
    relative = chosen.get("source_artifact_path")
    assert isinstance(relative, str) and relative
    source = chosen_path.parents[3] / relative
    assert source.is_file()
    source.unlink()


def _assert_damaged_baseline_import(source: Path, work: Path, *, expected: int) -> None:
    work.mkdir()
    run_id = (source / "run-id.txt").read_text(encoding="utf-8").strip()
    missing_db = work / "missing.sqlite3"
    corrupt_db = work / "corrupt.sqlite3"
    missing_artifacts = work / "missing-artifacts"
    corrupt_artifacts = work / "corrupt-artifacts"
    shutil.copyfile(source / "engine.sqlite3", missing_db)
    shutil.copyfile(source / "engine.sqlite3", corrupt_db)
    shutil.copytree(source / "artifacts", missing_artifacts)
    shutil.copytree(source / "artifacts", corrupt_artifacts)
    _remove_one_usage_source(missing_artifacts)
    next(corrupt_artifacts.rglob("attempts/*/result.json")).write_bytes(b"{}\n")
    missing = _run(
        {"db_path": missing_db, "artifact_root": missing_artifacts},
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
    )
    assert (
        _direct_total({"db_path": missing_db, "artifact_root": missing_artifacts}, run_id)
        != expected
    )
    assert missing.statistically_ready is False
    assert any(
        code in missing.diagnostics
        for code in (
            "usage_source_missing",
            "usage_source_debt",
            "historical_codex_usage_source_invalid",
            "usage_observation_integrity_error",
        )
    )
    corrupt = _run(
        {"db_path": corrupt_db, "artifact_root": corrupt_artifacts},
        run_id=run_id,
        apply=True,
        batch_size=32,
        max_attempts=100,
    )
    assert (
        _direct_total({"db_path": corrupt_db, "artifact_root": corrupt_artifacts}, run_id)
        != expected
    )
    assert any(
        code in corrupt.diagnostics
        for code in ("usage_source_corrupt", "usage_source_malformed", "usage_source_missing")
    )


def _exercise_baseline_cli(
    isolated_xdg: Path,
    source: Path,
    *,
    expected_total: int,
    implementer_provider: str,
    implementer_total: int,
    reviewer_total: int,
) -> None:
    provenance = json.loads((source / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["writer_commit"] == _BASELINE_COMMIT
    assert provenance["schema_user_version"] == 20
    export_db = (source / "engine.sqlite3").read_bytes()
    export_artifacts = _tree_digest(source / "artifacts")
    paths = _place_ledger(isolated_xdg, source)
    run_id = (source / "run-id.txt").read_text(encoding="utf-8").strip()
    with sqlite3.connect(paths["db_path"]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_attempt_facts" not in tables
    authority = _authority_digest(paths["db_path"])
    artifacts = _tree_digest(paths["artifact_root"])
    check = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--run-id", run_id, "--check", "--output", "json"],
    )
    assert check.exit_code == 0, check.stderr
    checked = json.loads(check.stdout)
    assert checked["provider_coverage"] == "unknown"
    assert checked["unmaterialized"] == 2
    assert checked["statistically_ready"] is False
    assert checked["remaining"] == 2
    with sqlite3.connect(paths["db_path"]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 20
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "scheduler_usage_backfill_jobs" not in tables
    text = runner.invoke(
        app,
        [
            "scheduler",
            "usage-backfill",
            "--run-id",
            run_id,
            "--apply",
            "--batch-size",
            "1",
            "--max-attempts",
            "1",
        ],
    )
    assert text.exit_code == 0, text.output
    job_id, job_status = _text_job(text.stdout)
    assert job_status == "running"
    jobs_before = _job_ids(paths["db_path"])
    resumed = _cli_resume(job_id)
    assert resumed.exit_code == 0, resumed.output
    resumed_id, resumed_status = _text_job(resumed.stdout)
    assert resumed_id == job_id
    assert resumed_status == "completed"
    assert _job_ids(paths["db_path"]) == jobs_before
    assert _direct_total(paths, run_id) == expected_total
    usage = json.loads(
        scheduler_usage(
            run_id, db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).model_dump_json()
    )
    implementer = _breakdown(
        usage["direct"]["breakdowns"], provider=implementer_provider, role="implementer"
    )
    reviewer = _breakdown(usage["direct"]["breakdowns"], provider="codex", role="reviewer")
    assert implementer["known_total_tokens"] == implementer_total
    assert reviewer["known_total_tokens"] == reviewer_total
    if implementer_provider == "codex":
        assert implementer["categories"]["cache_read"]["known_total"] == 0
    inspected = default_run_read_service(
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    ).inspect_run(run_id)
    assert inspected.token_usage.direct.known_total_tokens == expected_total
    assert _authority_digest(paths["db_path"]) == authority
    assert _tree_digest(paths["artifact_root"]) == artifacts
    assert (source / "engine.sqlite3").read_bytes() == export_db
    assert _tree_digest(source / "artifacts") == export_artifacts


def _place_ledger(isolated_xdg: Path, source: Path) -> dict[str, Path]:
    paths = _paths(isolated_xdg)
    paths["db_path"].parent.mkdir(parents=True, exist_ok=True)
    if paths["db_path"].exists():
        paths["db_path"].unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{paths['db_path']}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    if paths["artifact_root"].exists():
        shutil.rmtree(paths["artifact_root"])
    shutil.copyfile(source / "engine.sqlite3", paths["db_path"])
    shutil.copytree(source / "artifacts", paths["artifact_root"])
    return paths


def _paths(isolated_xdg: Path) -> dict[str, Path]:
    state = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": state / "engine.sqlite3", "artifact_root": state / "artifacts"}


def _run(paths: dict[str, Path], **overrides: object) -> object:
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
    barrier = overrides.pop("barrier", None)
    before_commit = overrides.pop("before_commit", None)
    values.update(overrides)
    if values["run_id"] is None and values["project_name"] is None and not values["resume_job_id"]:
        values["all_runs"] = True
    request = parse_usage_backfill_request(**values)  # type: ignore[arg-type]
    return run_usage_backfill(
        request,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
        before_commit=before_commit,  # type: ignore[arg-type]
        barrier=barrier,  # type: ignore[arg-type]
    )


def _direct_total(paths: dict[str, Path], run_id: str) -> int | None:
    report = scheduler_usage(
        run_id,
        db_path=paths["db_path"],
        artifact_root=paths["artifact_root"],
    )
    return report.direct.known_total_tokens


def _set_one_attempt_status(database: Path, run_id: str, status: str) -> None:
    with sqlite3.connect(database) as conn:
        row = conn.execute(
            """
            SELECT attempt_id FROM scheduler_attempts
            WHERE run_id = ? AND status IN ('completed', 'active')
            ORDER BY created_at LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE scheduler_attempts SET status = ? WHERE attempt_id = ?",
            (status, row[0]),
        )
        conn.commit()


def _assert_source_class_then_enrich(
    paths: dict[str, Path],
    run_id: str,
    *,
    replacement: bytes | None,
    code: str,
) -> None:
    root = run_artifact_root(paths["artifact_root"], run_id)
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT result_artifact_path FROM scheduler_attempts
            WHERE run_id = ? AND result_artifact_path IS NOT NULL
            ORDER BY created_at LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    assert row is not None
    envelope = root / str(row[0])
    original = envelope.read_bytes()
    envelope.write_bytes(original + b"\n" if replacement is None else replacement)
    _clear_facts(paths["db_path"])
    classified = _run(paths, run_id=run_id, apply=True, batch_size=32, max_attempts=100)
    assert code in classified.diagnostics
    assert _direct_total(paths, run_id) != 140
    envelope.write_bytes(original)
    enriched = _run(paths, run_id=run_id, apply=True, batch_size=32, max_attempts=100)
    assert enriched.statistically_ready is True
    assert _direct_total(paths, run_id) == 140
    assert _observation_count(paths["db_path"]) == 2


def _clear_facts(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("DELETE FROM scheduler_usage_observations")
        conn.execute("DELETE FROM scheduler_usage_attempt_facts")
        conn.commit()


def _fact_count(database: Path) -> int:
    with sqlite3.connect(database) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM scheduler_usage_attempt_facts").fetchone()[0])


def _observation_count(database: Path) -> int:
    with sqlite3.connect(database) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM scheduler_usage_observations").fetchone()[0])


def _observation_totals(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as conn:
        rows = conn.execute(
            "SELECT observation_id, normalized_payload FROM scheduler_usage_observations"
        ).fetchall()
    return {str(row[0]): int(json.loads(str(row[1]))["total_tokens"]) for row in rows}


def _project_name(database: Path) -> str:
    with sqlite3.connect(database) as conn:
        name = conn.execute(
            "SELECT json_extract(state_payload, '$.context.project_name') FROM scheduler_runs"
        ).fetchone()[0]
    assert isinstance(name, str) and name
    return name


def _assert_classification_releases_sqlite(
    paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ai_dev_loop.scheduler.application.usage_backfill as backfill

    def probe(*args: object, **kwargs: object) -> object:
        connection = sqlite3.connect(paths["db_path"], timeout=0.05)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ROLLBACK")
        finally:
            connection.close()
        return authenticate_process_attempt(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(backfill, "authenticate_process_attempt", probe)
    report = _run(paths, all_runs=True, check=True)
    monkeypatch.setattr(backfill, "authenticate_process_attempt", authenticate_process_attempt)
    assert report.unmaterialized == 2


def _retarget_one_observation(paths: dict[str, Path], run_id: str) -> None:
    root = run_artifact_root(paths["artifact_root"], run_id)
    observation_path = next(root.rglob("usage/observations/*/*.json"))
    observation = UsageObservationV1.model_validate_json(
        observation_path.read_text(encoding="utf-8")
    )
    native = observation.native.model_copy(
        update={
            "output_tokens": (observation.native.output_tokens or 0) + 50,
            "total_tokens": None
            if observation.native.total_tokens is None
            else observation.native.total_tokens + 50,
        }
    )
    normalized = observation.normalized.model_copy(
        update={
            "output_total": (observation.normalized.output_total or 0) + 50,
            "total_tokens": observation.normalized.total_tokens + 50,
        }
    )
    rewritten = observation.model_copy(update={"native": native, "normalized": normalized})
    old_digest = _sha256_file(observation_path)
    observation_path.write_text(rewritten.model_dump_json(), encoding="utf-8")
    new_digest = _sha256_file(observation_path)
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            """
            SELECT stdout_artifact_path, result_artifact_path
            FROM scheduler_attempts WHERE attempt_id = ?
            """,
            (observation.publisher_attempt_id,),
        ).fetchone()
    assert row is not None
    stdout = root / str(row[0])
    envelope = root / str(row[1])
    stdout_bytes = stdout.read_bytes().replace(old_digest.encode(), new_digest.encode(), 1)
    assert old_digest.encode() not in stdout_bytes or new_digest.encode() in stdout_bytes
    stdout.write_bytes(stdout_bytes)
    from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file

    new_stdout = sha256_file(stdout)
    envelope_bytes = envelope.read_bytes()
    old_stdout = json.loads(envelope_bytes)["stdout_sha256"]
    assert isinstance(old_stdout, str)
    updated = envelope_bytes.replace(old_stdout.encode(), new_stdout.encode(), 1)
    envelope.write_bytes(updated)
    with sqlite3.connect(paths["db_path"]) as conn:
        conn.execute(
            """
            UPDATE scheduler_attempts
            SET completion_envelope_sha256 = ?
            WHERE attempt_id = ?
            """,
            (envelope_sha256(updated), observation.publisher_attempt_id),
        )
        conn.commit()


def _sha256_file(path: Path) -> str:
    from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file

    return sha256_file(path)


def _window(database: Path) -> tuple[str, str]:
    with sqlite3.connect(database) as conn:
        start = conn.execute(
            """
            SELECT MIN(launch_requested_at) FROM scheduler_attempts
            WHERE launch_requested_at IS NOT NULL
            """
        ).fetchone()[0]
    assert isinstance(start, str)
    day = datetime.fromisoformat(start.replace("Z", "+00:00")).date()
    return (
        datetime(day.year, day.month, day.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        (datetime(day.year, day.month, day.day, tzinfo=UTC)).strftime("%Y-%m-%dT23:59:59Z"),
    )


def _refuse_source(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("sqlite statistics opened a usage source")


def _resume_until_done(paths: dict[str, Path], run_id: str) -> object:
    report = _run(paths, run_id=run_id, apply=True, batch_size=1, max_attempts=1)
    for _ in range(6):
        if report.job_status != "running":
            return report
        report = _run(
            paths,
            apply=True,
            resume_job_id=report.job_id,
            batch_size=1,
            max_attempts=1,
        )
    return report


def _cli_resume(job_id: str) -> object:
    return runner.invoke(
        app,
        [
            "scheduler",
            "usage-backfill",
            "--resume",
            job_id,
            "--batch-size",
            "1",
            "--max-attempts",
            "1",
        ],
    )


def _text_job(stdout: str) -> tuple[str, str]:
    match = re.search(r"^job_id=([0-9a-f]{32}) job_status=(\w+)$", stdout, re.M)
    assert match is not None, stdout
    return match.group(1), match.group(2)


def _job_ids(database: Path) -> list[str]:
    with sqlite3.connect(database) as conn:
        return [
            str(row[0])
            for row in conn.execute(
                "SELECT job_id FROM scheduler_usage_backfill_jobs ORDER BY created_at"
            )
        ]


def _assert_reconcile_provenance(database: Path, run_id: str) -> None:
    with sqlite3.connect(database) as conn:
        fact = conn.execute(
            """
            SELECT import_origin, publisher_attempt_id, logical_attempt_id
            FROM scheduler_usage_attempt_facts
            WHERE run_id = ? AND provider = 'cursor'
            """,
            (run_id,),
        ).fetchone()
        assert fact is not None
        reconcile = {
            str(row[0])
            for row in conn.execute(
                """
                SELECT attempt.attempt_id
                FROM scheduler_attempts AS attempt
                JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
                WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.reconcile_sdk_run'
                """,
                (run_id,),
            )
        }
    assert fact[0] == "reconciliation"
    assert fact[1] in reconcile
    assert fact[2] not in reconcile


def _rewind_to_schema_20(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_items")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_backfill_jobs")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_observations")
        conn.execute("DROP TABLE IF EXISTS scheduler_usage_attempt_facts")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version >= 21")
        conn.execute("PRAGMA user_version = 20")
        conn.commit()


def _authority_digest(database: Path) -> str:
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        payload = {
            table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in _AUTHORITY_TABLES
        }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _tree_digest(root: Path) -> str:
    lines = [
        f"{path.relative_to(root).as_posix()} {hashlib.sha256(path.read_bytes()).hexdigest()}"
        for path in sorted(root.rglob("*"))
        if path.is_file()
    ]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _observation_row(database: Path, observation_id: str) -> dict[str, object]:
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM scheduler_usage_observations WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def _insert_observation(database: Path, values: dict[str, object]) -> None:
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    with sqlite3.connect(database) as conn:
        conn.execute(
            f"INSERT INTO scheduler_usage_observations({', '.join(columns)}) VALUES ({placeholders})",
            [values[column] for column in columns],
        )
        conn.commit()


def _keep_one_observation_as_debt(database: Path, observation_id: str) -> None:
    from ai_dev_loop.scheduler.domain.usage_facts import (
        UsageFactPayloadV1,
        canonical_json,
        canonical_sha256,
    )

    with sqlite3.connect(database) as conn:
        row = conn.execute(
            """
            SELECT logical_attempt_id, fact_payload
            FROM scheduler_usage_attempt_facts
            WHERE role = 'implementer'
            """
        ).fetchone()
        assert row is not None
        payload = UsageFactPayloadV1.model_validate_json(str(row[1]))
        updated = payload.model_copy(
            update={
                "coverage": None,
                "settlement_status": "debt",
                "observation_ids": (observation_id,),
                "diagnostic_codes": tuple(
                    dict.fromkeys((*payload.diagnostic_codes, "usage_source_debt"))
                ),
            }
        )
        body = updated.model_dump(mode="json")
        conn.execute(
            """
            UPDATE scheduler_usage_attempt_facts
            SET settlement_status = 'debt',
                coverage = NULL,
                fact_payload = ?,
                payload_sha256 = ?,
                diagnostic_codes = ?
            WHERE logical_attempt_id = ?
            """,
            (
                canonical_json(body),
                canonical_sha256(body),
                canonical_json(list(updated.diagnostic_codes)),
                row[0],
            ),
        )
        conn.commit()


def _delete_observation(database: Path, observation_id: str) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute(
            "DELETE FROM scheduler_usage_observations WHERE observation_id = ?",
            (observation_id,),
        )
        conn.commit()


def _assert_check_ready_without_sources(
    paths: dict[str, Path],
    run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
        ReadOnlyProtectedArtifactStore,
    )

    held = paths["artifact_root"].parent / "artifacts-held-for-check"
    original_read = ReadOnlyProtectedArtifactStore.read_verified_bytes
    shutil.move(paths["artifact_root"], held)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.usage_backfill.authenticate_process_attempt",
        _refuse_source,
    )
    monkeypatch.setattr(ReadOnlyProtectedArtifactStore, "read_verified_bytes", _refuse_source)
    try:
        result = runner.invoke(
            app,
            ["scheduler", "usage-backfill", "--run-id", run_id, "--check", "--output", "json"],
        )
        assert result.exit_code == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["statistically_ready"] is True
        assert payload["provider_coverage"] == "complete"
        assert payload["remaining"] == 0
        assert payload["diagnostics"] == []
    finally:
        monkeypatch.setattr(
            "ai_dev_loop.scheduler.application.usage_backfill.authenticate_process_attempt",
            authenticate_process_attempt,
        )
        monkeypatch.setattr(ReadOnlyProtectedArtifactStore, "read_verified_bytes", original_read)
        if held.exists():
            if paths["artifact_root"].exists():
                shutil.rmtree(paths["artifact_root"])
            shutil.move(held, paths["artifact_root"])


def _assert_cli_check_rejects(paths: dict[str, Path], run_id: str) -> None:
    with sqlite3.connect(paths["db_path"]) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = paths["db_path"].read_bytes()
    result = runner.invoke(
        app,
        ["scheduler", "usage-backfill", "--run-id", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code != 0
    assert "usage statistics payload is invalid" in result.stderr
    assert "statistically_ready" not in result.stdout
    assert paths["db_path"].read_bytes() == before


def _corrupt_observation_digest(database: Path, observation_id: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE scheduler_usage_observations
            SET payload_sha256 = ?
            WHERE observation_id = ?
            """,
            ("cd" * 32, observation_id),
        )
        connection.commit()


def _restore_observation_digest(database: Path, observation_id: str, digest: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            UPDATE scheduler_usage_observations
            SET payload_sha256 = ?
            WHERE observation_id = ?
            """,
            (digest, observation_id),
        )
        connection.commit()


def _assert_binding_failure(paths: dict[str, Path], run_id: str) -> None:
    with pytest.raises(SchedulerEngineError, match="usage statistics payload is invalid") as raised:
        scheduler_usage(run_id, db_path=paths["db_path"], artifact_root=paths["artifact_root"])
    assert raised.value.kind is SchedulerEngineErrorKind.CORRUPTION
    failed = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert failed.exit_code != 0
    assert "usage statistics payload is invalid" in failed.stderr
    with pytest.raises(IntegrationApiError, match="Scheduler run state is inconsistent.") as api:
        default_run_read_service(
            db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).inspect_run(run_id)
    assert api.value.code is IntegrationErrorCode.DATA_INTEGRITY
    start, end = _window(paths["db_path"])
    with pytest.raises(
        IntegrationApiError, match="Scheduler dashboard data failed integrity validation."
    ):
        default_dashboard_read_service(
            db_path=paths["db_path"], artifact_root=paths["artifact_root"]
        ).token_usage(from_value=start, to_value=end, time_zone="UTC", run_id=run_id)
    with pytest.raises(IntegrationApiError):
        DashboardHttpDispatcher(
            default_http_service_factories(
                db_path=paths["db_path"], artifact_root=paths["artifact_root"]
            )
        ).dispatch(
            "dashboard.token-usage",
            {"fromUtc": start, "toUtc": end, "timeZone": "UTC", "runId": run_id},
        )


def _run_root(artifact_root: Path, run_id: str) -> Path:
    return run_artifact_root(artifact_root, run_id)


def _corrupt_one_envelope(artifact_root: Path, run_id: str) -> None:
    envelope = next(_run_root(artifact_root, run_id).rglob("attempts/*/result.json"))
    envelope.write_bytes(b"{}\n")


def _breakdown(rows: object, **expected: str) -> dict[str, object]:
    assert isinstance(rows, list)
    for item in rows:
        assert isinstance(item, dict)
        if all(item.get(key) == value for key, value in expected.items()):
            return item
    raise AssertionError(expected)
