"""Phase 25.4 production runner, ledger authentication, CLI, and aggregation."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase25_2_sdk_workflow import (
    _configure_v2,
    _credential,
    _install_fake_sdk,
)
from tests.integration.test_phase25_3_sdk_recovery import (
    _bind_fake_sdk_run,
    _submit_sdk_run,
    _tick_until_kind,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    _tick as _sequence_tick,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    drive as _drive_sequence,
)
from tests.integration.test_phase27_3_implementer_sequences import (
    prepare as _prepare_sequence,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    _run_until,
    _submit,
    _tick,
)
from tests.unit.scheduler.test_phase27_6_sequence_implementer_recovery import (
    _blocked_codex_leaf,
    _drive_sequence_final,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    default_http_service_factories,
)
from ai_dev_loop.integration_api.run_service import default_run_read_service
from ai_dev_loop.integration_api.sequence_service import default_sequence_read_service
from ai_dev_loop.scheduler.application.attempt_service import AttemptService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
    CodexImplementerFailureRecoveryService,
)
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.token_usage_capture import capture_cursor_usage_safely
from ai_dev_loop.scheduler.application.token_usage_query import TokenUsageReadService
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()
_ATTEMPTS = itertools.count()


@pytest.mark.parametrize(
    "command",
    [
        (
            "scheduler",
            "usage",
            "fixture-project-20260921T013201Z-50f2bd",
            "--output",
            "json",
        ),
        ("scheduler", "sequence", "usage", "missing-sequence", "--output", "json"),
    ],
)
def test_f01_usage_cli_never_migrates_historical_database(
    isolated_xdg: Path,
    command: tuple[str, ...],
) -> None:
    del isolated_xdg
    fixture = Path("tests/fixtures/phase21_4_pre_21_4_historical/engine.sqlite3")
    db = default_engine_db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture, db)
    before = db.read_bytes()
    with sqlite3.connect(db) as conn:
        historical_version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert historical_version == 11

    runner.invoke(app, list(command))

    assert db.read_bytes() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == historical_version


@pytest.mark.parametrize(
    "command",
    [
        ("scheduler", "usage", "missing-run", "--output", "json"),
        ("scheduler", "sequence", "usage", "missing-sequence", "--output", "json"),
    ],
)
def test_f01_usage_cli_missing_database_creates_no_state(
    isolated_xdg: Path,
    command: tuple[str, ...],
) -> None:
    del isolated_xdg
    db = default_engine_db_path()
    assert not db.exists()
    before = tuple(db.parent.iterdir()) if db.parent.is_dir() else ()

    result = runner.invoke(app, list(command))

    assert result.exit_code != 0
    assert not db.exists()
    assert (tuple(db.parent.iterdir()) if db.parent.is_dir() else ()) == before


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {
                        "id": "gpt-5.6-sol",
                        "supported_reasoning_efforts": ["high", "xhigh"],
                    },
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    state = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": state / "engine.sqlite3", "artifact_root": state / "artifacts"}


def test_u01_u03_real_codex_roles_publish_and_read_without_provider_runtime(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "usage-implemented.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {
                    "input_tokens": 100,
                    "cached_input_tokens": 60,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 4,
                }
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {
                    "input_tokens": 25,
                    "cached_input_tokens": 5,
                    "output_tokens": 5,
                }
            ]
        ),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")

    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    report = TokenUsageReadService(
        store, artifact_root=scheduler_paths["artifact_root"]
    ).run_report(run_id)
    assert report.direct.known_total_tokens == 140
    assert report.direct.coverage == "complete"
    assert {
        (item.provider, item.role, item.stage, item.known_total_tokens)
        for item in report.direct.breakdowns
    } == {
        ("codex", "implementer", "initial", 110),
        ("codex", "reviewer", "review", 30),
    }

    database_before = scheduler_paths["db_path"].read_bytes()
    artifacts_before = {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): path.read_bytes()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    }
    monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")
    result = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["direct"]["known_total_tokens"] == 140
    assert scheduler_paths["db_path"].read_bytes() == database_before
    assert {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): path.read_bytes()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    } == artifacts_before

    # Simulate a pre-observation Codex capture: authenticated JSONL stays in
    # place while the additive usage artifacts are absent. Projection is
    # in-memory and leaves every historical authority byte untouched.
    for path in scheduler_paths["artifact_root"].rglob("usage/observations/*/*.json"):
        path.unlink()
    historical_before = {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): path.read_bytes()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    }
    historical = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).run_report(run_id)
    assert historical.direct.known_total_tokens == 140
    assert {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): path.read_bytes()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    } == historical_before

    missing_artifacts = scheduler_paths["artifact_root"].parent / "missing-artifacts"
    public = default_run_read_service(
        db_path=scheduler_paths["db_path"], artifact_root=missing_artifacts
    ).inspect_run(run_id)
    assert public.token_usage.direct.known_total_tokens is None
    assert public.token_usage.direct.coverage == "unknown"
    assert public.token_usage.direct.inference_attempts == 2
    assert "usage_evidence_not_recorded" in public.token_usage.direct.diagnostics

    unsafe_artifacts = scheduler_paths["artifact_root"].parent / "unsafe-artifacts"
    unsafe_artifacts.write_text("not a directory", encoding="utf-8")
    unsafe = default_run_read_service(
        db_path=scheduler_paths["db_path"], artifact_root=unsafe_artifacts
    ).inspect_run(run_id)
    assert unsafe.token_usage.direct.coverage == "unknown"
    assert "usage_evidence_integrity_error" in unsafe.token_usage.direct.diagnostics


def test_u01_real_cursor_sdk_terminal_usage_is_published_once(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([{"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}]),
    )
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_USAGE_JSON",
        json.dumps(
            {
                "input_tokens": 4820,
                "output_tokens": 139,
                "cache_read_tokens": 2560,
                "cache_write_tokens": 0,
                "total_tokens": 7519,
                "reasoning_tokens": 118,
            }
        ),
    )
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "tracked baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / "fake-sdk"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(
            SubmitOptions(
                short_name="SDK token usage",
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-6.1-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts_root,
            )
        ).run_id
    run_root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    monkeypatch.setenv("FAKE_CURSOR_SDK_RUN_ROOT", str(run_root))
    monkeypatch.setenv("FAKE_CURSOR_SDK_IDENTITY_PATH", str(run_root / "cursor/sdk-identity.json"))
    start_run(run_id, db_path=db)
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 17, 12, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase254-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(50):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "completed":
            break
        assert state.kind != "blocked"
    assert state.kind == "completed"
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.stage == "initial"
    assert cursor.known_total_tokens == 7519
    assert cursor.categories["reasoning_output"].known_total == 118
    assert cursor.categories["output_total"].known_total == 139


@pytest.mark.parametrize(
    ("case", "usage", "known"),
    [
        (
            "boolean",
            {
                "input_tokens": True,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            False,
        ),
        (
            "string",
            {
                "input_tokens": "0",
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            False,
        ),
        (
            "fractional",
            {
                "input_tokens": 0.5,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            False,
        ),
        (
            "negative",
            {
                "input_tokens": -1,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            False,
        ),
        (
            "null",
            {
                "input_tokens": None,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            False,
        ),
        (
            "missing",
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            },
            False,
        ),
        (
            "zero",
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 0,
            },
            True,
        ),
    ],
)
def test_f03_fake_sdk_raw_usage_is_strict_and_never_changes_execution_decision(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    case: str,
    usage: dict[str, object],
    known: bool,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name=f"SDK strict usage {case}",
    )

    state = _tick_until_kind(tick, store, run_id, "completed")
    assert state.kind == "completed"  # type: ignore[attr-defined]
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    if known:
        assert cursor.known_total_tokens == 0
        assert cursor.coverage == "complete"
        assert "invalid_cursor_usage" not in report.direct.diagnostics
    else:
        assert cursor.known_total_tokens is None
        assert cursor.coverage == "unknown"
        assert "invalid_cursor_usage" in report.direct.diagnostics
        metadata_paths = list(
            (artifacts_root / "runs").rglob("cursor/iterations/*/*/sdk-metadata.json")
        )
        assert len(metadata_paths) == 1
        stored_usage = json.loads(metadata_paths[0].read_text(encoding="utf-8"))["usage"]
        if case == "string":
            assert stored_usage["input_tokens"] is None


@pytest.mark.parametrize(
    ("turn_totals", "terminal_total"),
    [([110, 20], 130), ([20, 20], 40)],
)
def test_f05_cursor_turn_events_reconcile_with_cumulative_result(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    turn_totals: list[int],
    terminal_total: int,
) -> None:
    del fake_clis

    def usage(total: int) -> dict[str, int]:
        return {
            "input_tokens": total,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": total,
        }

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_STREAM_USAGE_SEQUENCE_JSON",
        json.dumps([usage(total) for total in turn_totals]),
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage(terminal_total)))
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK per-turn usage",
    )

    state = _tick_until_kind(tick, store, run_id, "completed")
    assert state.kind == "completed"  # type: ignore[attr-defined]
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.known_total_tokens == terminal_total
    assert cursor.coverage == "complete"
    assert "conflicting_cursor_usage_evidence" not in report.direct.diagnostics
    observations = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (artifacts_root / "runs").rglob("usage/observations/*/*.json")
    ]
    cursor_observations = [item for item in observations if item["provider"] == "cursor"]
    assert len(cursor_observations) == 1
    observation = cursor_observations[0]
    assert observation["native_key"] == "run-019abc00-1111-4222-8333-555555555555"
    assert observation["normalized"]["total_tokens"] == terminal_total
    assert observation["source_event_offsets"] == ["2", "3"]


def test_f05_stream_usage_survives_interruption_and_reconciliation_once(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 4820,
        "output_tokens": 139,
        "cache_read_tokens": 2560,
        "cache_write_tokens": 0,
        "total_tokens": 7519,
        "reasoning_tokens": 118,
    }
    marker = tmp_path / "stream-interrupted"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK usage replay once",
    )

    state = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert state.kind == "completed"  # type: ignore[attr-defined]
    assert marker.read_text(encoding="utf-8") == "failed"
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.known_total_tokens == 7519
    assert cursor.inference_attempts == 1
    assert cursor.partial_attempts == 1
    observation_paths = list((artifacts_root / "runs").rglob("usage/observations/*/*.json"))
    assert len(observation_paths) == 1
    observation = json.loads(observation_paths[0].read_text(encoding="utf-8"))
    with store.begin_read() as conn:
        logical = conn.execute(
            """
            SELECT attempt.attempt_id, attempt.dispatch_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.run_turn'
            """,
            (run_id,),
        ).fetchone()
    assert logical is not None
    assert observation["scheduler_attempt_id"] == logical["attempt_id"]
    assert observation["source_dispatch_id"] == logical["dispatch_id"]

    authority_before = db.read_bytes()
    artifact_hashes_before = {
        path.relative_to(artifacts_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts_root.rglob("*")
        if path.is_file()
    }
    http = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts_root)
    ).dispatch("run.inspect", {"runId": run_id})
    assert http["tokenUsage"]["direct"]["knownTotalTokens"] == 7519
    assert http["tokenUsage"]["direct"]["coverage"] == "partial"
    assert db.read_bytes() == authority_before
    assert {
        path.relative_to(artifacts_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts_root.rglob("*")
        if path.is_file()
    } == artifact_hashes_before

    # A protected file alone is not publication authority. Without an
    # authenticated outcome binding, this otherwise valid observation is fenced
    # out of every read and cannot inflate the recovered run.
    orphan = capture_cursor_usage_safely(
        run_root=ProtectedArtifactStore(artifacts_root).run_root(run_id),
        run_id=run_id,
        stage="initial",
        scheduler_attempt_id=str(logical["attempt_id"]),
        publisher_attempt_id=str(logical["attempt_id"]),
        source_dispatch_id=str(logical["dispatch_id"]),
        native_run_id="unbound-native-run",
        source_artifact_path=observation["source_artifact_path"],
        source_artifact_sha256=observation["source_artifact_sha256"],
        usage=usage,
        capture_complete=True,
    )
    assert len(orphan.bindings) == 1
    repeated = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    assert repeated.direct.known_total_tokens == 7519
    repeated_cursor = next(item for item in repeated.direct.breakdowns if item.provider == "cursor")
    assert repeated_cursor.inference_attempts == 1


def test_f05_deadline_usage_stays_bound_and_counted_once_after_reconciliation(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 110,
    }
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK deadline usage",
    )

    reconciliation_requested = False
    for _ in range(80):
        receipt = tick.run_once()
        if any(
            item.run_id == run_id and item.action == "cursor_sdk_reconciliation_requested"
            for item in receipt.run_receipts
        ):
            reconciliation_requested = True
            break
    assert reconciliation_requested
    partial_report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in partial_report.direct.breakdowns if item.provider == "cursor")
    assert cursor.inference_attempts == 1
    assert cursor.known_total_tokens == 110
    assert cursor.known_attempts == 0
    assert cursor.partial_attempts == 1
    assert cursor.unknown_attempts == 0
    assert cursor.coverage == "partial"
    assert cursor.categories["total_tokens"].known_total == 110
    assert cursor.categories["total_tokens"].coverage == "partial"

    cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert cli.exit_code == 0, cli.stdout
    assert json.loads(cli.stdout)["direct"]["known_total_tokens"] == 110
    inspected = default_run_read_service(db_path=db, artifact_root=artifacts_root).inspect_run(
        run_id
    )
    assert inspected.token_usage.direct.known_total_tokens == 110
    dashboard = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts_root)
    ).dispatch("run.inspect", {"runId": run_id})
    assert dashboard["tokenUsage"]["direct"]["knownTotalTokens"] == 110
    assert dashboard["tokenUsage"]["direct"]["coverage"] == "partial"

    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    final = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    for scope in (final.direct, final.recovery_lineage):
        assert scope.known_total_tokens == 110
        final_cursor = next(item for item in scope.breakdowns if item.provider == "cursor")
        assert final_cursor.inference_attempts == 1
        assert final_cursor.partial_attempts == 1
        assert final_cursor.coverage == "partial"
    observations = list((artifacts_root / "runs").rglob("usage/observations/*/*.json"))
    assert len(observations) == 1
    with store.begin_read() as conn:
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.reconcile_sdk_run'
            """,
            (run_id,),
        ).fetchone()[0]
    assert reconciliation_attempts == 1


def test_f05_repeated_deadline_during_close_preserves_authenticated_usage(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 110,
    }
    deadline_marker = tmp_path / "repeated-deadline"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_WAIT_FOR_DEADLINE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_DEADLINE_MARKER", str(deadline_marker))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    original_binding = AttemptService._cursor_binding

    def one_second_sdk_turn(
        self: AttemptService,
        state: object,
        *,
        effect_kind: str,
        run_id: str,
    ) -> dict[str, object]:
        binding = original_binding(self, state, effect_kind=effect_kind, run_id=run_id)  # type: ignore[arg-type]
        if binding.get("runtime") == "sdk-local" and effect_kind == "cursor.run_turn":
            binding["timeout_seconds"] = 1
        return binding

    monkeypatch.setattr(AttemptService, "_cursor_binding", one_second_sdk_turn)
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK repeated deadline usage",
    )

    reconciliation_requested = False
    for _ in range(100):
        receipt = tick.run_once()
        if any(
            item.run_id == run_id and item.action == "cursor_sdk_reconciliation_requested"
            for item in receipt.run_receipts
        ):
            reconciliation_requested = True
            break
    assert reconciliation_requested
    assert deadline_marker.with_name(deadline_marker.name + ".repeated").is_file()

    before_reconciliation = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    for scope in (before_reconciliation.direct, before_reconciliation.recovery_lineage):
        assert scope.known_total_tokens == 110
        cursor = next(item for item in scope.breakdowns if item.provider == "cursor")
        assert cursor.inference_attempts == 1
        assert cursor.known_attempts == 0
        assert cursor.partial_attempts == 1
        assert cursor.unknown_attempts == 0
        assert cursor.coverage == "partial"
        assert cursor.categories["total_tokens"].known_total == 110
        assert cursor.categories["total_tokens"].coverage == "partial"

    cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert cli.exit_code == 0, cli.stdout
    cli_usage = json.loads(cli.stdout)
    for scope_name in ("direct", "recovery_lineage"):
        assert cli_usage[scope_name]["known_total_tokens"] == 110
        assert cli_usage[scope_name]["coverage"] == "partial"
    inspected = default_run_read_service(db_path=db, artifact_root=artifacts_root).inspect_run(
        run_id
    )
    for scope in (
        inspected.token_usage.direct,
        inspected.token_usage.recovery_lineage,
    ):
        assert scope.known_total_tokens == 110
        assert scope.coverage == "partial"
    dashboard = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts_root)
    ).dispatch("run.inspect", {"runId": run_id})
    for scope_name in ("direct", "recoveryLineage"):
        assert dashboard["tokenUsage"][scope_name]["knownTotalTokens"] == 110
        assert dashboard["tokenUsage"][scope_name]["coverage"] == "partial"

    completed = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    final = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    for scope in (final.direct, final.recovery_lineage):
        assert scope.known_total_tokens == 110
        cursor = next(item for item in scope.breakdowns if item.provider == "cursor")
        assert cursor.inference_attempts == 1
        assert cursor.partial_attempts == 1
        assert cursor.coverage == "partial"
    observations = list((artifacts_root / "runs").rglob("usage/observations/*/*.json"))
    assert len(observations) == 1
    with store.begin_read() as conn:
        reconciliation_attempts = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'cursor.reconcile_sdk_run'
            """,
            (run_id,),
        ).fetchone()[0]
    assert reconciliation_attempts == 1


@pytest.mark.parametrize(
    ("native_status", "usage", "known_total", "expected_coverage", "terminal_kind"),
    [
        (
            "finished",
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 110,
            },
            110,
            "complete",
            "completed",
        ),
        (
            "cancelled",
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 110,
            },
            110,
            "partial",
            "blocked",
        ),
        (
            "interrupted",
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "total_tokens": 110,
            },
            110,
            "partial",
            "blocked",
        ),
        ("cancelled", None, None, "unknown", "blocked"),
    ],
)
def test_f08_nonfinished_native_usage_is_partial_or_unknown_across_public_reads(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    native_status: str,
    usage: dict[str, int] | None,
    known_total: int | None,
    expected_coverage: str,
    terminal_kind: str,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", native_status)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    if usage is not None:
        monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
        monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name=f"SDK {native_status} usage {known_total}",
    )

    report = None
    for _ in range(100):
        tick.run_once()
        candidate = TokenUsageReadService(
            SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
        ).run_report(run_id)
        if candidate.direct.inference_attempts == 1:
            report = candidate
            if candidate.direct.coverage == expected_coverage:
                break
    assert report is not None
    assert report.direct.known_total_tokens == known_total
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.inference_attempts == 1
    assert cursor.known_total_tokens == known_total
    assert cursor.coverage == expected_coverage
    if known_total is None:
        assert cursor.known_attempts == 0
        assert cursor.unknown_attempts == 1
        assert cursor.partial_attempts == 0
        assert cursor.categories["total_tokens"].known_total is None
        assert cursor.categories["total_tokens"].coverage == "unknown"
    elif expected_coverage == "complete":
        assert cursor.known_attempts == 1
        assert cursor.partial_attempts == 0
        assert cursor.unknown_attempts == 0
        assert cursor.categories["total_tokens"].known_total == 110
        assert cursor.categories["total_tokens"].coverage == "complete"
    else:
        assert cursor.known_attempts == 0
        assert cursor.partial_attempts == 1
        assert cursor.unknown_attempts == 0
        assert cursor.categories["total_tokens"].known_total == 110
        assert cursor.categories["total_tokens"].coverage == "partial"

    cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert cli.exit_code == 0, cli.stdout
    assert json.loads(cli.stdout)["direct"]["known_total_tokens"] == known_total
    inspected = default_run_read_service(db_path=db, artifact_root=artifacts_root).inspect_run(
        run_id
    )
    assert inspected.token_usage.direct.known_total_tokens == known_total
    dashboard = DashboardHttpDispatcher(
        default_http_service_factories(db_path=db, artifact_root=artifacts_root)
    ).dispatch("run.inspect", {"runId": run_id})
    assert dashboard["tokenUsage"]["direct"]["knownTotalTokens"] == known_total
    assert dashboard["tokenUsage"]["direct"]["coverage"] == expected_coverage

    terminal = _tick_until_kind(tick, store, run_id, terminal_kind, limit=160)
    assert terminal.kind == terminal_kind  # type: ignore[attr-defined]


def test_f05_sparse_replay_cumulative_confirms_one_native_run_across_public_reads(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 110,
    }
    marker = tmp_path / "sparse-stream-interrupted"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_USAGE_SEQUENCE_JSON", "[]")
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK sparse usage replay",
    )

    state = _tick_until_kind(tick, store, run_id, "completed", limit=160)
    assert state.kind == "completed"  # type: ignore[attr-defined]
    assert marker.read_text(encoding="utf-8") == "failed"
    observations = list((artifacts_root / "runs").rglob("usage/observations/*/*.json"))
    assert len(observations) == 1
    observation = json.loads(observations[0].read_text(encoding="utf-8"))
    assert observation["native_key"] == "run-019abc00-1111-4222-8333-555555555555"
    assert observation["source_event_offsets"] == ["2"]
    assert observation["normalized"]["total_tokens"] == 110
    observation_bytes = observations[0].read_bytes()
    database_before = db.read_bytes()
    artifacts_before = {
        path.relative_to(artifacts_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts_root.rglob("*")
        if path.is_file()
    }

    service = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    )
    report = service.run_report(run_id)
    for scope in (report.direct, report.recovery_lineage):
        assert scope.known_total_tokens == 110
        cursor = next(item for item in scope.breakdowns if item.provider == "cursor")
        assert cursor.inference_attempts == 1
        assert cursor.known_total_tokens == 110
        assert cursor.categories["total_tokens"].known_total == 110
        assert "conflicting_cursor_usage_evidence" not in scope.diagnostics

    inspected = default_run_read_service(db_path=db, artifact_root=artifacts_root).inspect_run(
        run_id
    )
    assert inspected.token_usage.direct.known_total_tokens == 110
    monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")
    cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert cli.exit_code == 0, cli.stdout
    assert json.loads(cli.stdout)["direct"]["known_total_tokens"] == 110

    assert observations[0].read_bytes() == observation_bytes
    assert db.read_bytes() == database_before
    assert {
        path.relative_to(artifacts_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in artifacts_root.rglob("*")
        if path.is_file()
    } == artifacts_before


def test_f05_sparse_replay_counts_once_in_sequence_cli_and_inspection(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    usage = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 110,
    }
    _configure_v2(git_repo)
    fake_sdk = tmp_path / "sequence-fake-sdk"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    prior = os.environ.get("PYTHONPATH", "")
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk) + (":" + prior if prior else ""))
    marker = tmp_path / "sequence-stream-interrupted"
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_USAGE_SEQUENCE_JSON", "[]")
    monkeypatch.setenv("FAKE_CURSOR_SDK_USAGE_JSON", json.dumps(usage))
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_FAILURE_MARKER", str(marker))
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {
            "context": "256k",
            "reasoning_effort": "high",
            "fast": False,
        },
    }
    phases: list[dict[str, object]] = []
    for ordinal, provider in enumerate(("cursor", "codex"), start=1):
        phases.append(
            {
                "name": f"phase-{ordinal}",
                "short_name": f"Phase {ordinal}",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "commit_message": "usage checkpoint" if ordinal == 1 else None,
                "cursor": cursor,
                "codex": {
                    "review_model": "gpt-6.1-sol",
                    "review_reasoning_effort": "high",
                },
                "implementer": (
                    {"provider": "cursor"}
                    if provider == "cursor"
                    else {
                        "provider": "codex",
                        "command": "codex",
                        "model": "gpt-6.1-sol",
                        "reasoning_effort": "high",
                        "timeout_minutes": 90,
                    }
                ),
            }
        )
    manifest = git_repo.parent / "usage-sequence-v4.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "usage sequence",
                "short_name": "Usage sequence",
                "phases": phases,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    prepared = prepare_sequence(
        SequencePrepareOptions(
            repo_path=git_repo,
            manifest_path=manifest,
            **scheduler_paths,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    _bind_fake_sdk_run(monkeypatch, scheduler_paths["artifact_root"], started.run_id)
    tick = TickService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2027, 1, 1, tzinfo=UTC),
        tick_owner_factory=lambda: f"phase254-sequence-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    final = _drive_sequence(
        tick,
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    assert len(final.materialized_entries) == 2
    assert marker.read_text(encoding="utf-8") == "failed"
    database_before = scheduler_paths["db_path"].read_bytes()
    artifacts_before = {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    }

    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).sequence_report(prepared.sequence_id)
    assert report.sequence.known_total_tokens == 110
    cursor_breakdown = next(
        item for item in report.sequence.breakdowns if item.provider == "cursor"
    )
    assert cursor_breakdown.inference_attempts == 1
    assert cursor_breakdown.known_total_tokens == 110
    inspected = default_sequence_read_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    ).inspect_sequence(prepared.sequence_id)
    assert inspected.token_usage.sequence.known_total_tokens == 110
    inspected_cursor = next(
        item for item in inspected.token_usage.sequence.breakdowns if item.provider == "cursor"
    )
    assert inspected_cursor.known_total_tokens == 110
    monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")
    cli = runner.invoke(
        app,
        ["scheduler", "sequence", "usage", prepared.sequence_id, "--output", "json"],
    )
    assert cli.exit_code == 0, cli.stdout
    assert json.loads(cli.stdout)["sequence"]["known_total_tokens"] == 110
    cli_cursor = next(
        item
        for item in json.loads(cli.stdout)["sequence"]["breakdowns"]
        if item["provider"] == "cursor"
    )
    assert cli_cursor["known_total_tokens"] == 110
    assert scheduler_paths["db_path"].read_bytes() == database_before
    assert {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    } == artifacts_before


def test_f07_terminal_optional_reasoning_enriches_complete_cursor_usage(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    del fake_clis
    required = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 110,
    }
    monkeypatch.setenv("FAKE_CURSOR_SDK_STREAM_USAGE_JSON", json.dumps(required))
    monkeypatch.setenv(
        "FAKE_CURSOR_SDK_USAGE_JSON",
        json.dumps({**required, "reasoning_tokens": 2}),
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id, db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK optional reasoning",
    )

    state = _tick_until_kind(tick, store, run_id, "completed")
    assert state.kind == "completed"  # type: ignore[attr-defined]
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(db), artifact_root=artifacts_root
    ).run_report(run_id)
    cursor = next(item for item in report.direct.breakdowns if item.provider == "cursor")
    assert cursor.known_total_tokens == 110
    assert cursor.coverage == "complete"
    assert cursor.categories["total_tokens"].coverage == "complete"
    assert cursor.categories["reasoning_output"].known_total == 2
    assert cursor.categories["reasoning_output"].coverage == "complete"
    assert cursor.native_categories["reasoning_tokens"].known_total == 2
    assert cursor.native_categories["reasoning_tokens"].coverage == "complete"
    assert "conflicting_cursor_usage_evidence" not in report.direct.diagnostics


def test_u02_zero_attempt_scope_is_not_applicable(tmp_path: Path) -> None:
    # The direct aggregator's explicit empty-scope behavior is part of the public
    # report contract; it must never turn absence of inference into a zero bill.
    from ai_dev_loop.scheduler.application.token_usage_query import _aggregate_scope

    report = _aggregate_scope(
        scope="direct",
        run_ids=("run-empty",),
        attempts={},
        observations={},
        diagnostics=[],
    )
    assert report.coverage == "not_applicable"
    assert report.known_total_tokens is None


def test_u01_malformed_review_usage_never_changes_an_accepted_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "malformed-metrics.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([{"input_tokens": True, "output_tokens": 1}]),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    state = _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")
    assert state.kind == "completed"
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).run_report(run_id)
    reviewer = next(item for item in report.direct.breakdowns if item.role == "reviewer")
    assert reviewer.coverage == "unknown"
    assert "invalid_codex_usage_turn_1" in report.direct.diagnostics


def test_f04_live_and_historical_codex_omissions_keep_lower_bound_partial(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid = {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "partial-usage.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([[valid, None]]),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    state = _run_until(_tick(git_repo, scheduler_paths), run_id, "completed")
    assert state.kind == "completed"

    service = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    live = service.run_report(run_id)
    implementer = next(
        item
        for item in live.direct.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert implementer.known_total_tokens == 110
    assert implementer.coverage == "partial"
    assert implementer.partial_attempts == 1
    assert implementer.categories["total_tokens"].known_total == 110
    assert implementer.categories["total_tokens"].coverage == "partial"
    assert "missing_codex_usage_turn_2" in live.direct.diagnostics

    # Exercise the authenticated historical projection path independently of
    # the newly published observation. This is an F-04 fallback regression,
    # not the genuine pre-25 provenance required separately by F-06.
    for path in scheduler_paths["artifact_root"].rglob("usage/observations/*/*.json"):
        path.unlink()
    historical = service.run_report(run_id)
    historical_implementer = next(
        item
        for item in historical.direct.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert historical_implementer.known_total_tokens == 110
    assert historical_implementer.coverage == "partial"
    assert historical_implementer.categories["total_tokens"].coverage == "partial"
    assert "missing_historical_codex_usage_turn_2" in historical.direct.diagnostics


def test_f04_missing_published_codex_turn_is_recovered_across_public_reads(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                [
                    {"input_tokens": 100, "output_tokens": 10},
                    {"input_tokens": 15, "output_tokens": 5},
                ]
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([{"input_tokens": 25, "output_tokens": 5}]),
    )
    prepared = _prepare_sequence(git_repo, scheduler_paths, ("codex", "cursor"))
    start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    final = _drive_sequence(
        _sequence_tick(git_repo, scheduler_paths),
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    run_id = final.materialized_entries[0].run_id
    observation_paths = list(scheduler_paths["artifact_root"].rglob("usage/observations/*/*.json"))
    missing = next(
        path
        for path in observation_paths
        if (payload := json.loads(path.read_text(encoding="utf-8")))["provider"] == "codex"
        and payload["role"] == "implementer"
        and payload["native_key"] == "2"
    )
    missing.unlink()

    database_before = scheduler_paths["db_path"].read_bytes()
    artifacts_before = {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    }
    service = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    run_report = service.run_report(run_id)
    lineage_implementer = next(
        item
        for item in run_report.recovery_lineage.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert lineage_implementer.known_total_tokens == 130
    assert lineage_implementer.coverage == "partial"
    assert lineage_implementer.categories["total_tokens"].coverage == "partial"
    assert "usage_observation_integrity_error" in run_report.direct.diagnostics

    sequence_report = service.sequence_report(prepared.sequence_id)
    sequence_implementer = next(
        item
        for item in sequence_report.sequence.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert sequence_implementer.known_total_tokens == 130
    assert sequence_implementer.coverage == "partial"
    assert sequence_implementer.categories["total_tokens"].coverage == "partial"

    run_inspect = default_run_read_service(
        db_path=scheduler_paths["db_path"], artifact_root=scheduler_paths["artifact_root"]
    ).inspect_run(run_id)
    inspected_run_implementer = next(
        item
        for item in run_inspect.token_usage.direct.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert inspected_run_implementer.known_total_tokens == 130
    assert inspected_run_implementer.coverage == "partial"
    sequence_inspect = default_sequence_read_service(
        db_path=scheduler_paths["db_path"], artifact_root=scheduler_paths["artifact_root"]
    ).inspect_sequence(prepared.sequence_id)
    inspected_sequence_implementer = next(
        item
        for item in sequence_inspect.token_usage.sequence.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert inspected_sequence_implementer.known_total_tokens == 130
    assert inspected_sequence_implementer.coverage == "partial"

    monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")
    run_cli = runner.invoke(app, ["scheduler", "usage", run_id, "--output", "json"])
    assert run_cli.exit_code == 0, run_cli.stdout
    run_cli_implementer = next(
        item
        for item in json.loads(run_cli.stdout)["direct"]["breakdowns"]
        if item["provider"] == "codex" and item["role"] == "implementer"
    )
    assert run_cli_implementer["known_total_tokens"] == 130
    assert run_cli_implementer["coverage"] == "partial"
    sequence_cli = runner.invoke(
        app,
        ["scheduler", "sequence", "usage", prepared.sequence_id, "--output", "json"],
    )
    assert sequence_cli.exit_code == 0, sequence_cli.stdout
    sequence_cli_implementer = next(
        item
        for item in json.loads(sequence_cli.stdout)["sequence"]["breakdowns"]
        if item["provider"] == "codex" and item["role"] == "implementer"
    )
    assert sequence_cli_implementer["known_total_tokens"] == 130
    assert sequence_cli_implementer["coverage"] == "partial"
    assert scheduler_paths["db_path"].read_bytes() == database_before
    assert {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    } == artifacts_before


def test_u02_both_codex_corrections_reviews_and_interrupted_attempt_are_attributed(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "usage-corrections.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,success,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
                {"input_tokens": 15, "cached_input_tokens": 5, "output_tokens": 5},
                {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5},
                {"input_tokens": 35, "cached_input_tokens": 5, "output_tokens": 5},
            ]
        ),
    )
    run_id = _submit(git_repo, scheduler_paths, max_reviews=3)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")

    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).run_report(run_id)
    assert report.direct.known_total_tokens == 200
    assert report.direct.coverage == "partial"
    implementer = [
        item
        for item in report.direct.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    ]
    reviewer = next(item for item in report.direct.breakdowns if item.role == "reviewer")
    assert sum(item.known_total_tokens or 0 for item in implementer) == 130
    assert reviewer.known_total_tokens == 70
    assert reviewer.inference_attempts == 3
    assert reviewer.unknown_attempts == 1


def test_u02_codex_recovery_successor_includes_authenticated_ancestor_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "lineage-usage.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings,no_findings")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
                {"input_tokens": 15, "cached_input_tokens": 5, "output_tokens": 5},
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5},
                {"input_tokens": 35, "cached_input_tokens": 5, "output_tokens": 5},
            ]
        ),
    )
    source_id = _submit(git_repo, scheduler_paths)
    start_run(source_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), source_id, "completed")
    subprocess.run(["git", "commit", "-qm", "accepted source"], cwd=git_repo, check=True)
    successor_id = _submit(
        git_repo,
        scheduler_paths,
        resubmission_id="019def00-0000-0000-0000-000000002504",
    )
    start_run(successor_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), successor_id, "completed")

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        source_attempt = conn.execute(
            """
            SELECT attempt.attempt_id, attempt.dispatch_id
            FROM scheduler_attempts AS attempt
            JOIN scheduler_effects AS effect ON effect.dispatch_id = attempt.dispatch_id
            WHERE attempt.run_id = ? AND effect.effect_kind = 'codex.implementer_bootstrap'
            """,
            (source_id,),
        ).fetchone()
        assert source_attempt is not None
        intent = "{}"
        conn.execute(
            """
            INSERT INTO scheduler_codex_implementer_recoveries(
                recovery_key, source_run_id, failed_attempt_id, successor_run_id,
                failed_dispatch_id, status, intent_payload, intent_payload_sha256,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?)
            """,
            (
                "a" * 64,
                source_id,
                str(source_attempt["attempt_id"]),
                successor_id,
                str(source_attempt["dispatch_id"]),
                intent,
                hashlib.sha256(intent.encode()).hexdigest(),
                "2026-10-17T12:00:00.000000Z",
                "2026-10-17T12:00:00.000000Z",
            ),
        )
    report = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    ).run_report(successor_id)
    assert report.direct.known_total_tokens == 60
    assert report.recovery_lineage.known_total_tokens == 200
    assert set(report.recovery_lineage.run_ids) == {source_id, successor_id}


def test_u02_u03_sequence_usage_follows_only_authoritative_phase_run_lineage(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "sequence-usage.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps([{"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}]),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5},
                {"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5},
            ]
        ),
    )
    prepared = _prepare_sequence(git_repo, scheduler_paths, ("codex", "cursor"))
    from ai_dev_loop.scheduler.application.sequence_start import start_sequence

    start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    _drive_sequence(
        _sequence_tick(git_repo, scheduler_paths),
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    service = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    database_before = scheduler_paths["db_path"].read_bytes()
    artifacts_before = {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    }
    report = service.sequence_report(prepared.sequence_id)
    repeated_report = service.sequence_report(prepared.sequence_id)
    assert repeated_report == report
    with SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]).begin_read() as conn:
        authoritative = {
            str(row[0])
            for row in conn.execute(
                "SELECT run_id FROM scheduler_sequence_run_attempts WHERE sequence_id = ?",
                (prepared.sequence_id,),
            )
        }
    assert set(report.sequence.run_ids) == authoritative
    assert report.sequence.known_total_tokens == 170
    assert report.sequence.coverage == "partial"
    monkeypatch.setenv("PATH", "/provider-runtime-must-not-be-used")
    cli = runner.invoke(
        app,
        ["scheduler", "sequence", "usage", prepared.sequence_id, "--output", "json"],
    )
    assert cli.exit_code == 0, cli.stdout
    assert json.loads(cli.stdout)["sequence"]["known_total_tokens"] == 170
    assert scheduler_paths["db_path"].read_bytes() == database_before
    assert {
        path.relative_to(scheduler_paths["artifact_root"]).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in scheduler_paths["artifact_root"].rglob("*")
        if path.is_file()
    } == artifacts_before

    missing_artifacts = scheduler_paths["artifact_root"].parent / "missing-sequence-artifacts"
    inspected = default_sequence_read_service(
        db_path=scheduler_paths["db_path"], artifact_root=missing_artifacts
    ).inspect_sequence(prepared.sequence_id)
    assert inspected.token_usage.sequence.known_total_tokens is None
    assert inspected.token_usage.sequence.coverage == "unknown"
    assert "usage_evidence_not_recorded" in inspected.token_usage.sequence.diagnostics


def test_f06_adopted_sequence_replacement_and_shared_ancestry_count_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unrelated_repo = tmp_path / "unrelated-repo"
    shutil.copytree(git_repo, unrelated_repo)
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_USAGE_SEQUENCE_JSON",
        json.dumps(
            [
                {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1},
                {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10},
            ]
        ),
    )
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_USAGE_SEQUENCE_JSON",
        json.dumps([{"input_tokens": 25, "cached_input_tokens": 5, "output_tokens": 5}]),
    )
    prepared, tick, blocked = _blocked_codex_leaf(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    successor_run_id, changed = CodexImplementerFailureRecoveryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: datetime(2026, 10, 14, 20, tzinfo=UTC),
    ).force(blocked.run_id)
    assert changed is True
    final = _drive_sequence_final(
        _sequence_tick(git_repo, scheduler_paths),
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )
    assert final.materialized_entries[0].run_id == successor_run_id

    service = TokenUsageReadService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifact_root=scheduler_paths["artifact_root"],
    )
    first = service.sequence_report(prepared.sequence_id)
    second = service.sequence_report(prepared.sequence_id)
    assert second == first
    with SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]).begin_read() as conn:
        lineage = conn.execute(
            """
            SELECT run_id, source_run_id
            FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? ORDER BY ordinal, generation
            """,
            (prepared.sequence_id,),
        ).fetchall()
    assert [str(row["run_id"]) for row in lineage[:2]] == [
        blocked.run_id,
        successor_run_id,
    ]
    assert str(lineage[1]["source_run_id"]) == blocked.run_id
    assert set(first.sequence.run_ids) == {str(row["run_id"]) for row in lineage}
    assert first.sequence.known_total_tokens == 170
    implementer = next(
        item
        for item in first.sequence.breakdowns
        if item.provider == "codex" and item.role == "implementer"
    )
    assert implementer.known_total_tokens == 110
    assert implementer.inference_attempts == 2
    assert implementer.unknown_attempts == 1

    # Materialize an authenticated, token-bearing run in another repository and
    # relate it as a review successor without adding it to sequence lineage. The
    # sequence-attempt rows remain the adoption authority, so both this unadopted
    # successor and an otherwise unrelated run stay excluded.
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "unrelated-usage.txt")
    unrelated_id = _submit(unrelated_repo, scheduler_paths)
    start_run(unrelated_id, db_path=scheduler_paths["db_path"])
    unrelated_state = _run_until(_tick(unrelated_repo, scheduler_paths), unrelated_id, "completed")
    assert unrelated_state.kind == "completed"
    unrelated_report = service.run_report(unrelated_id)
    assert unrelated_report.direct.known_total_tokens == 140
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_review_recovery_successors(
                source_run_id, recovery_key, successor_run_id, created_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                str(lineage[-1]["run_id"]),
                "phase25-4-unadopted-review-successor",
                unrelated_id,
                "2026-10-17T20:00:00.000000Z",
            ),
        )
    after_unadopted = service.sequence_report(prepared.sequence_id)
    assert after_unadopted.sequence.known_total_tokens == 170
    assert unrelated_id not in after_unadopted.sequence.run_ids
