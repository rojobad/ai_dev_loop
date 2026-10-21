from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from tests.unit.scheduler.helpers import sample_submitted_state
from tests.unit.scheduler.test_phase27_6_sequence_implementer_recovery import (
    _blocked_codex_leaf,
    _drive_sequence_final,
    _drive_sequence_leaf_to,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters import sqlite_read
from ai_dev_loop.dashboard.adapters.sqlite_read import SqliteDashboardReadRepository
from ai_dev_loop.dashboard.application.ports import DashboardReadError, DashboardReadErrorKind
from ai_dev_loop.integration_api.dashboard_service import (
    IntegrationDashboardReadService,
    default_dashboard_read_service,
)
from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import IntegrationCapabilities
from ai_dev_loop.integration_api.schemas import (
    DASHBOARD_ACTIVITY_DATA_SCHEMA,
    DASHBOARD_PROJECTS_DATA_SCHEMA,
    DASHBOARD_RUNS_DATA_SCHEMA,
    DASHBOARD_SUMMARY_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import ImplementerRetryService
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_IMPLEMENTER,
    ImplementerBinding,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()
FIXED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
HISTORICAL = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "phase21_4_pre_21_4_historical"
    / "engine.sqlite3"
)


@pytest.fixture
def recovery_scheduler_paths(
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path]:
    monkeypatch.delenv("AI_DEV_LOOP_REVIEW_PROMPT_WITNESS_ROOT", raising=False)
    monkeypatch.delenv("AI_DEV_LOOP_REVIEW_PROMPT_REL", raising=False)
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
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
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _database_path(xdg_state: Path) -> Path:
    return xdg_state / "ai_dev_loop" / "engine.sqlite3"


def _insert_run(
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    project_name: str = "project-a",
    submitted_at: datetime = FIXED,
    provider: str = "cursor",
) -> str:
    state = sample_submitted_state(run_id=run_id, repo_root=f"/tmp/{run_id}")
    context_updates: dict[str, object] = {"project_name": project_name}
    if provider == "codex":
        context_updates.update(
            {
                "schema_version": SUBMITTED_CONTEXT_SCHEMA_VERSION_IMPLEMENTER,
                "implementer": ImplementerBinding(
                    provider="codex",
                    command="codex",
                    model="gpt-5.6-sol",
                    reasoning_effort="xhigh",
                    timeout_minutes=90,
                    sandbox="workspace-write",
                    approval_policy="never",
                ),
            }
        )
    context = state.context.model_copy(update=context_updates)
    digest = __import__("hashlib").sha256(run_id.encode()).hexdigest()
    state = state.model_copy(
        update={
            "context": context,
            "idempotency_key": digest,
            "submitted_at": encode_utc_instant(submitted_at),
            "updated_at": encode_utc_instant(submitted_at),
        }
    )
    event_id = f"event-{run_id}"
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=run_id,
            state=state,
            event_id=event_id,
            event=RunSubmittedEvent(
                run_id=run_id,
                idempotency_key=state.idempotency_key,
                worktree_key=state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=submitted_at,
        )
    return event_id


def _insert_attempt(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    event_id: str,
    suffix: str,
    component: str,
    effect_kind: str,
    iteration: int,
    launch: datetime | None,
    completed: datetime | None,
    status: str = "completed",
    ingested: bool = True,
) -> None:
    dispatch_id = f"dispatch-{run_id}-{suffix}"
    attempt_id = f"attempt-{run_id}-{suffix}"
    created = launch or FIXED
    with store.begin_immediate() as conn:
        ordinal = int(
            conn.execute(
                "SELECT COUNT(*) FROM scheduler_effects WHERE source_event_id = ?",
                (event_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, 'succeeded', ?, ?, ?)
            """,
            (
                dispatch_id,
                event_id,
                ordinal,
                run_id,
                f"effect-{suffix}",
                f"{run_id}:{suffix}",
                effect_kind,
                "d" * 64,
                encode_utc_instant(created),
                encode_utc_instant(created),
                encode_utc_instant(created),
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at, launch_requested_at, completed_at, ingested
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                run_id,
                dispatch_id,
                component,
                iteration,
                status,
                encode_utc_instant(created),
                encode_utc_instant(completed or created),
                encode_utc_instant(launch) if launch is not None else None,
                encode_utc_instant(completed) if completed is not None else None,
                int(ingested),
            ),
        )


def _fixture_ledger(xdg_state: Path) -> Path:
    db = _database_path(xdg_state)
    store = SqliteSchedulerStore(db)
    cursor_event = _insert_run(store, "cursor-run", submitted_at=FIXED)
    codex_event = _insert_run(
        store,
        "codex-run",
        submitted_at=FIXED + timedelta(minutes=5),
        provider="codex",
    )
    _insert_attempt(
        store,
        run_id="cursor-run",
        event_id=cursor_event,
        suffix="chat",
        component="cursor",
        effect_kind="cursor.create_chat",
        iteration=1,
        launch=FIXED - timedelta(hours=1),
        completed=FIXED - timedelta(minutes=59),
    )
    _insert_attempt(
        store,
        run_id="cursor-run",
        event_id=cursor_event,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED + timedelta(hours=1),
        completed=FIXED + timedelta(hours=2),
    )
    _insert_attempt(
        store,
        run_id="cursor-run",
        event_id=cursor_event,
        suffix="review-a",
        component="codex",
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        iteration=1,
        launch=FIXED + timedelta(hours=2),
        completed=FIXED + timedelta(hours=3),
    )
    _insert_attempt(
        store,
        run_id="cursor-run",
        event_id=cursor_event,
        suffix="review-retry",
        component="codex",
        effect_kind=RESUME_CODEX_REVIEW_EFFECT_KIND,
        iteration=1,
        launch=FIXED + timedelta(hours=3),
        completed=FIXED + timedelta(hours=4),
    )
    _insert_attempt(
        store,
        run_id="codex-run",
        event_id=codex_event,
        suffix="implementer",
        component="codex_implementer",
        effect_kind=BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
        iteration=1,
        launch=FIXED + timedelta(hours=1),
        completed=FIXED + timedelta(hours=2),
    )
    return db


def _invoke(args: list[str]) -> dict[str, object]:
    result = runner.invoke(app, ["integration", *args])
    assert result.exit_code == 0, result.stdout
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    return json.loads(lines[0])


def _call_dashboard_operation(
    service: IntegrationDashboardReadService,
    operation: str,
) -> object:
    kwargs = {
        "from_value": "2026-01-01T00:00:00Z",
        "to_value": "2026-01-03T00:00:00Z",
        "time_zone": "UTC",
    }
    if operation == "projects":
        return service.projects()
    if operation == "summary":
        return service.summary(**kwargs)
    if operation == "runs":
        return service.runs(**kwargs)
    if operation == "activity":
        return service.activity(metric="submissions", **kwargs)
    raise AssertionError(f"unexpected dashboard operation {operation}")


def test_c261_3_four_real_cli_commands_schemas_help_and_compatibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "state"
    _fixture_ledger(xdg_state)
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    start = "2026-01-01T00:00:00Z"
    end = "2026-01-03T00:00:00Z"
    operations = (
        (["dashboard", "projects"], DASHBOARD_PROJECTS_DATA_SCHEMA),
        (["dashboard", "summary", "--from", start, "--to", end], DASHBOARD_SUMMARY_DATA_SCHEMA),
        (["dashboard", "runs", "--from", start, "--to", end], DASHBOARD_RUNS_DATA_SCHEMA),
        (
            [
                "dashboard",
                "activity",
                "--from",
                start,
                "--to",
                end,
                "--metric",
                "review-rounds",
            ],
            DASHBOARD_ACTIVITY_DATA_SCHEMA,
        ),
    )
    payloads = []
    for args, schema in operations:
        payload = _invoke(args)
        assert payload["apiVersion"] == {"major": 1, "minor": 13}
        validate_integration_instance(payload["data"], schema)
        payloads.append(payload)
    assert payloads[1]["data"]["totals"]["runsStarted"] == 2
    assert {item["implementerProvider"] for item in payloads[2]["data"]["items"]} == {
        "cursor",
        "codex",
    }
    assert payloads[3]["data"]["total"] == 1

    info = _invoke(["info"])
    assert info["data"]["capabilities"]["dashboard"] is True
    historical = IntegrationCapabilities.model_validate({"runs": True})
    assert historical.dashboard is False
    with pytest.raises(ValidationError):
        IntegrationCapabilities.model_validate({"dashboard": None})
    service = default_dashboard_read_service(db_path=_database_path(xdg_state))
    with pytest.raises(IntegrationApiError) as explicit_null:
        service.summary(from_value=start, to_value=end, project_name=None)
    assert explicit_null.value.code == "INVALID_ARGUMENT"

    help_result = runner.invoke(app, ["integration", "dashboard", "--help"])
    assert help_result.exit_code == 0 and "projects" in help_result.stdout
    invalid = runner.invoke(app, ["integration", "dashboard", "projects", "--limit", "101"])
    assert invalid.exit_code == 2
    invalid_lines = [line for line in invalid.stdout.splitlines() if line.strip()]
    assert len(invalid_lines) == 1
    assert json.loads(invalid_lines[0])["error"]["code"] == "INVALID_ARGUMENT"


def test_c261_1_real_cli_started_cohort_and_totals_outlive_first_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "state"
    db = _fixture_ledger(xdg_state)
    writer = SqliteSchedulerStore(db)
    for index in range(24):
        run_id = f"additional-{index:02d}"
        event_id = _insert_run(writer, run_id)
        _insert_attempt(
            writer,
            run_id=run_id,
            event_id=event_id,
            suffix="turn",
            component="cursor",
            effect_kind="cursor.run_turn",
            iteration=1,
            launch=FIXED + timedelta(hours=1, minutes=index + 1),
            completed=FIXED + timedelta(hours=2, minutes=index + 1),
        )
    pre_range_id = "pre-range-submission"
    pre_range_event = _insert_run(
        writer,
        pre_range_id,
        submitted_at=FIXED - timedelta(days=2),
    )
    _insert_attempt(
        writer,
        run_id=pre_range_id,
        event_id=pre_range_event,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED + timedelta(hours=1, minutes=30),
        completed=FIXED + timedelta(hours=2, minutes=30),
    )
    _insert_run(writer, "queued-only")
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    start = "2026-01-01T12:00:00Z"
    end = "2026-01-03T00:00:00Z"
    summary = _invoke(["dashboard", "summary", "--from", start, "--to", end, "--time-zone", "UTC"])[
        "data"
    ]
    runs = _invoke(["dashboard", "runs", "--from", start, "--to", end, "--time-zone", "UTC"])[
        "data"
    ]
    assert isinstance(summary, dict) and isinstance(runs, dict)
    assert summary["totals"]["runsStarted"] == 27
    assert summary["projects"][0]["stateCounts"]["waiting"] == 27
    assert summary["coverage"]["unstartedRuns"] == 1
    assert runs["totalCount"] == 27
    assert len(runs["items"]) == 25 and runs["page"]["hasMore"] is True
    assert pre_range_id in {
        item["runId"]
        for item in _invoke(
            [
                "dashboard",
                "runs",
                "--from",
                start,
                "--to",
                end,
                "--time-zone",
                "UTC",
                "--limit",
                "100",
            ]
        )["data"]["items"]
    }
    cursor_item = next(item for item in runs["items"] if item["runId"] == "cursor-run")
    assert cursor_item["firstExecutionLaunchAt"] == encode_utc_instant(FIXED + timedelta(hours=1))


def test_c261_1_authoritative_sequence_lineage_keeps_two_replacements(
    git_repo: Path,
    recovery_scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler_paths = recovery_scheduler_paths
    prepared, tick, first_source = _blocked_codex_leaf(
        git_repo,
        scheduler_paths,
        monkeypatch,
        implementer_sequence="fail,fail,success",
    )
    retry = ImplementerRetryService(
        tick.store,
        tick.artifacts,
        now_factory=lambda: datetime(2026, 10, 14, 20, 0, tzinfo=UTC),
    )
    first = retry.retry(first_source.run_id, force=True)
    second_source = _drive_sequence_leaf_to(
        tick,
        scheduler_paths,
        prepared.sequence_id,
        "blocked",
    )
    second = retry.retry(second_source.run_id, force=True)
    _drive_sequence_final(
        tick,
        scheduler_paths,
        prepared.sequence_id,
        monkeypatch,
    )

    service = default_dashboard_read_service(db_path=scheduler_paths["db_path"])
    result = service.runs(
        from_value="2026-10-14T19:00:00Z",
        to_value="2026-10-14T21:00:00Z",
        project_name="fixture-project",
        time_zone="UTC",
        offset=0,
        limit=100,
    )
    lineage_items = {
        item.run_id: item.sequence_binding
        for item in result.items
        if item.sequence_binding is not None
        and item.sequence_binding.sequence_id == prepared.sequence_id
        and item.sequence_binding.ordinal == 1
    }
    assert set(lineage_items) == {first_source.run_id, first.run_id, second.run_id}
    assert [
        lineage_items[run_id].generation  # type: ignore[union-attr]
        for run_id in (first_source.run_id, first.run_id, second.run_id)
    ] == [1, 2, 3]
    assert [
        lineage_items[run_id].attempt_kind  # type: ignore[union-attr]
        for run_id in (first_source.run_id, first.run_id, second.run_id)
    ] == ["planned_run", "implementer_retry", "implementer_retry"]
    assert lineage_items[first.run_id].source_run_id == first_source.run_id  # type: ignore[union-attr]
    assert lineage_items[second.run_id].source_run_id == first.run_id  # type: ignore[union-attr]


def test_c261_2_real_activity_counts_parallel_seconds_and_reviewer_round_only(
    tmp_path: Path,
) -> None:
    db = _fixture_ledger(tmp_path / "state")
    service = default_dashboard_read_service(db_path=db)
    kwargs = {
        "from_value": "2026-01-01T00:00:00Z",
        "to_value": "2026-01-03T00:00:00Z",
        "time_zone": "UTC",
    }
    reviews = service.activity(metric="review-rounds", **kwargs)
    process = service.activity(metric="process-hours", **kwargs)
    assert reviews.total == 1
    # Cursor and Codex implementer overlap for one hour; two reviewer attempts also contribute.
    assert process.total == 4 * 60 * 60


def test_c261_4_range_and_effect_admission_excludes_old_and_chat_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    old_event = _insert_run(
        writer,
        "old-run",
        submitted_at=FIXED - timedelta(days=10),
    )
    for index in range(3):
        _insert_attempt(
            writer,
            run_id="old-run",
            event_id=old_event,
            suffix=f"old-turn-{index}",
            component="cursor",
            effect_kind="cursor.run_turn",
            iteration=index + 1,
            launch=FIXED - timedelta(days=9, hours=index),
            completed=FIXED - timedelta(days=9, hours=index) + timedelta(minutes=1),
        )

    current_event = _insert_run(writer, "current-run")
    for index in range(3):
        _insert_attempt(
            writer,
            run_id="current-run",
            event_id=current_event,
            suffix=f"chat-{index}",
            component="cursor",
            effect_kind="cursor.create_chat",
            iteration=1,
            launch=FIXED + timedelta(minutes=index),
            completed=FIXED + timedelta(minutes=index + 1),
        )
    _insert_attempt(
        writer,
        run_id="current-run",
        event_id=current_event,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED + timedelta(hours=1),
        completed=FIXED + timedelta(hours=1, minutes=1),
    )

    pre_range_event = _insert_run(
        writer,
        "pre-range-run",
        submitted_at=FIXED - timedelta(days=10),
    )
    _insert_attempt(
        writer,
        run_id="pre-range-run",
        event_id=pre_range_event,
        suffix="old-turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED - timedelta(days=9),
        completed=FIXED - timedelta(days=9) + timedelta(minutes=1),
    )
    _insert_attempt(
        writer,
        run_id="pre-range-run",
        event_id=pre_range_event,
        suffix="in-range-turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=2,
        launch=FIXED + timedelta(hours=2),
        completed=FIXED + timedelta(hours=2, minutes=1),
    )

    missing_event = _insert_run(writer, "missing-launch")
    _insert_attempt(
        writer,
        run_id="missing-launch",
        event_id=missing_event,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=None,
        completed=None,
    )
    _insert_run(writer, "queued-only")

    monkeypatch.setattr(sqlite_read, "MAX_RELEVANT_RUNS", 2)
    monkeypatch.setattr(sqlite_read, "MAX_CONTRIBUTING_ATTEMPTS", 2)
    service = default_dashboard_read_service(db_path=db)
    kwargs = {
        "from_value": "2026-01-01T12:00:00Z",
        "to_value": "2026-01-02T12:00:00Z",
        "time_zone": "UTC",
    }
    summary = service.summary(**kwargs)
    assert summary.totals.runs_started == 1
    assert summary.coverage.unstarted_runs == 2
    assert summary.coverage.missing_launch_times == 1
    process = service.activity(metric="process-hours", **kwargs)
    assert process.total == 120


@pytest.mark.parametrize(
    "operation",
    ("projects", "summary", "runs", "activity"),
)
@pytest.mark.parametrize("corruption", ("malformed", "missing_table"))
def test_c261_4_all_operations_classify_storage_corruption_without_disclosure(
    tmp_path: Path,
    operation: str,
    corruption: str,
) -> None:
    db = tmp_path / f"{corruption}.sqlite3"
    if corruption == "malformed":
        db.write_bytes(b"not a SQLite database")
    else:
        writer = SqliteSchedulerStore(db)
        with writer.begin_immediate() as conn:
            conn.execute("DROP TABLE scheduler_timers")
    before = db.read_bytes()
    with pytest.raises(IntegrationApiError) as exc:
        _call_dashboard_operation(default_dashboard_read_service(db_path=db), operation)
    assert exc.value.code == "DATA_INTEGRITY"
    assert exc.value.message == "Scheduler dashboard data failed integrity validation."
    assert str(db) not in exc.value.message
    assert "scheduler_timers" not in exc.value.message
    assert db.read_bytes() == before


def test_c261_4_exclusive_writer_contention_is_transient_and_recovers(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    _insert_run(writer, "healthy-run")
    service = default_dashboard_read_service(db_path=db)

    lock = sqlite3.connect(db, isolation_level=None)
    assert lock.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    lock.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(IntegrationApiError) as exc:
            service.projects()
        assert exc.value.code == "INTERNAL_ERROR"
        assert exc.value.message == "An internal error occurred."
        assert str(db) not in exc.value.message
        assert "locked" not in exc.value.message.lower()
        assert "operationalerror" not in exc.value.message.lower()
    finally:
        lock.rollback()
        lock.close()

    projects = service.projects()
    assert projects.total_count == 1
    assert projects.items[0].run_count == 1


def test_c261_4_open_operational_failure_is_safe_io_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "engine.sqlite3"
    db.touch()

    def fail_open(_: Path) -> SqliteSchedulerStore:
        raise sqlite3.OperationalError("sensitive storage driver detail")

    monkeypatch.setattr(SqliteSchedulerStore, "open_readonly", staticmethod(fail_open))
    with pytest.raises(IntegrationApiError) as exc:
        default_dashboard_read_service(db_path=db).projects()
    assert exc.value.code == "IO_ERROR"
    assert exc.value.message == "Unable to read scheduler dashboard data."
    assert "sensitive" not in exc.value.message


def test_c261_4_malformed_catalog_cli_emits_safe_data_integrity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "state"
    db = _database_path(xdg_state)
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a SQLite database")
    before = db.read_bytes()
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))

    result = runner.invoke(app, ["integration", "dashboard", "projects"])
    assert result.exit_code != 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["error"] == {
        "code": "DATA_INTEGRITY",
        "message": "Scheduler dashboard data failed integrity validation.",
    }
    assert str(db) not in result.stdout
    assert "DatabaseError" not in result.stdout
    assert db.read_bytes() == before


def test_c261_4_foreign_run_effect_binding_is_data_integrity(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    source_event = _insert_run(writer, "source-run", project_name="source-project")
    _insert_run(writer, "target-run", project_name="target-project")
    _insert_attempt(
        writer,
        run_id="source-run",
        event_id=source_event,
        suffix="review",
        component="codex",
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        iteration=1,
        launch=FIXED + timedelta(hours=1),
        completed=FIXED + timedelta(hours=2),
    )
    with writer.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at, launch_requested_at, completed_at, ingested
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "attempt-target-foreign-review",
                "target-run",
                "dispatch-source-run-review",
                "codex",
                1,
                "completed",
                encode_utc_instant(FIXED + timedelta(hours=1)),
                encode_utc_instant(FIXED + timedelta(hours=2)),
                encode_utc_instant(FIXED + timedelta(hours=1)),
                encode_utc_instant(FIXED + timedelta(hours=2)),
                1,
            ),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

    service = default_dashboard_read_service(db_path=db)
    with pytest.raises(IntegrationApiError) as exc:
        service.activity(
            from_value="2026-01-01T00:00:00Z",
            to_value="2026-01-03T00:00:00Z",
            project_name="target-project",
            time_zone="UTC",
            metric="review-rounds",
        )
    assert exc.value.code == "DATA_INTEGRITY"
    assert exc.value.message == "Scheduler dashboard attempt data failed integrity validation."


def test_c261_4_missing_old_unsupported_and_corrupt_ledgers_are_read_only(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing" / "engine.sqlite3"
    service = default_dashboard_read_service(db_path=missing)
    assert service.projects().total_count == 0
    assert not missing.exists() and not missing.parent.exists()

    historical = tmp_path / "historical.sqlite3"
    shutil.copyfile(HISTORICAL, historical)
    conn = sqlite3.connect(historical)
    conn.execute(
        """
        UPDATE scheduler_attempts
        SET launch_requested_at = NULL
        WHERE dispatch_id IN (
            SELECT dispatch_id FROM scheduler_effects WHERE effect_kind = 'cursor.run_turn'
        )
        """
    )
    conn.commit()
    conn.close()
    before = historical.read_bytes()
    old_projects = default_dashboard_read_service(db_path=historical).projects()
    assert old_projects.total_count == 1
    old_summary = default_dashboard_read_service(db_path=historical).summary(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-12-31T00:00:00Z",
        time_zone="UTC",
    )
    assert old_summary.filters.time_zone == "UTC"
    assert old_summary.coverage.data_state == "partial"
    assert old_summary.coverage.missing_launch_times == 1
    assert "missing_launch_times" in old_summary.coverage.warnings
    assert historical.read_bytes() == before

    unsupported = tmp_path / "unsupported.sqlite3"
    conn = sqlite3.connect(unsupported)
    conn.execute("PRAGMA user_version = 3")
    conn.close()
    with pytest.raises(IntegrationApiError) as unsupported_error:
        default_dashboard_read_service(db_path=unsupported).projects()
    assert unsupported_error.value.code == "UNSUPPORTED"

    corrupt = tmp_path / "corrupt.sqlite3"
    writer = SqliteSchedulerStore(corrupt)
    _insert_run(writer, "corrupt-run")
    with writer.begin_immediate() as conn:
        conn.execute("UPDATE scheduler_runs SET state_payload = '{}' WHERE run_id = 'corrupt-run'")
    with pytest.raises(IntegrationApiError) as corrupt_error:
        default_dashboard_read_service(db_path=corrupt).projects()
    assert corrupt_error.value.code == "DATA_INTEGRITY"


def test_c261_4_scope_envelope_and_progress_bounds_return_no_partial_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    event_a = _insert_run(writer, "run-a")
    event_b = _insert_run(writer, "run-b")
    _insert_attempt(
        writer,
        run_id="run-a",
        event_id=event_a,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED,
        completed=FIXED + timedelta(minutes=1),
    )
    _insert_attempt(
        writer,
        run_id="run-b",
        event_id=event_b,
        suffix="turn",
        component="cursor",
        effect_kind="cursor.run_turn",
        iteration=1,
        launch=FIXED,
        completed=FIXED + timedelta(minutes=1),
    )
    bounded = default_dashboard_read_service(db_path=db)
    monkeypatch.setattr(sqlite_read, "MAX_RELEVANT_RUNS", 1)
    with pytest.raises(IntegrationApiError) as scope_error:
        bounded.summary(
            from_value="2026-01-01T00:00:00Z",
            to_value="2026-01-02T00:00:00Z",
        )
    assert scope_error.value.code == "INVALID_ARGUMENT"

    monkeypatch.setattr(sqlite_read, "MAX_RELEVANT_RUNS", 10_000)
    monkeypatch.setattr(sqlite_read, "MAX_CONTRIBUTING_ATTEMPTS", 1)
    with pytest.raises(IntegrationApiError) as attempts_error:
        bounded.summary(
            from_value="2026-01-01T00:00:00Z",
            to_value="2026-01-02T00:00:00Z",
        )
    assert attempts_error.value.code == "INVALID_ARGUMENT"

    repository = SqliteDashboardReadRepository(db_path=db)
    tiny = IntegrationDashboardReadService(
        default_dashboard_read_service(db_path=db).queries,
        envelope_max_bytes=32,
    )
    with pytest.raises(IntegrationApiError) as envelope_error:
        tiny.projects()
    assert envelope_error.value.code == "INVALID_ARGUMENT"
    assert envelope_error.value.message == (
        "Dashboard project catalog is too large; retry or reduce the page size."
    )

    cancelled = SqliteDashboardReadRepository(db_path=db, cancelled=lambda: True)
    with pytest.raises(DashboardReadError) as progress_error:
        cancelled.read_project_catalog(offset=0, limit=25)
    assert progress_error.value.kind is DashboardReadErrorKind.INVALID_ARGUMENT
    clock_values = iter((0.0, 6.0))
    expired = SqliteDashboardReadRepository(
        db_path=db,
        monotonic=lambda: next(clock_values, 6.0),
        query_budget_seconds=5,
        progress_steps=1,
    )
    with pytest.raises(DashboardReadError) as expiry_error:
        expired.read_project_catalog(offset=0, limit=25)
    assert expiry_error.value.kind is DashboardReadErrorKind.INVALID_ARGUMENT
    # Both failures retired their read transactions; the next writer still succeeds.
    _insert_run(writer, "run-c")
    catalog = repository.read_project_catalog(offset=0, limit=25)
    assert catalog.total_count == 1 and catalog.items[0].run_count == 3


def test_c261_4_snapshot_allows_separate_wal_writer_and_observes_it_next_call(
    tmp_path: Path,
) -> None:
    db = tmp_path / "engine.sqlite3"
    writer = SqliteSchedulerStore(db)
    for index in range(40):
        _insert_run(writer, f"run-{index:02d}")
    committed = threading.Event()
    launched = False
    clock_calls = 0

    def monotonic() -> float:
        nonlocal clock_calls, launched
        clock_calls += 1
        if clock_calls > 1 and not launched:
            launched = True

            def write() -> None:
                _insert_run(writer, "writer-run")
                committed.set()

            thread = threading.Thread(target=write)
            thread.start()
            thread.join(timeout=5)
        return 0.0

    repository = SqliteDashboardReadRepository(
        db_path=db,
        monotonic=monotonic,
        query_budget_seconds=5,
        progress_steps=1,
    )
    first = repository.read_project_catalog(offset=0, limit=25)
    assert committed.is_set()
    assert first.items[0].run_count in {40, 41}
    second = repository.read_project_catalog(offset=0, limit=25)
    assert second.items[0].run_count == 41
