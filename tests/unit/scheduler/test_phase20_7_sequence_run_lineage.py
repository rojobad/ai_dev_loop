"""Unit tests for Phase 20.7 sequence phase run lineage."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from referencing import Registry, Resource
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_NOW,
    FIXED_RUN_IDS,
    FIXED_SEQUENCE_ID,
    SequencePrepareOptions,
    _fake_repo_target,
    _manifest_yaml,
    _write_manifest,
)
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareService
from ai_dev_loop.scheduler.application.sequence_reconcile import SequenceReconcileService
from ai_dev_loop.scheduler.application.sequence_start import SequenceStartService
from ai_dev_loop.scheduler.domain.sequence import (
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    future_entry_ordinals,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_PLANNED,
    SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
    SequencePhaseExecution,
    SequenceRunAttempt,
    SequenceRunLineage,
    SequenceRunLineageValidationError,
    _lineage_aggregate_format_checker,
    _phase_execution_format_checker,
    project_historical_lineage_from_state,
    validate_lineage_json_schema,
    validate_lineage_terminal_outcomes_for_state,
)
from ai_dev_loop.scheduler.domain.state import AuthorizedState, BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    SequenceExecutionCASExpectation,
    build_lineage_from_rows,
    load_sequence_run_lineage,
    persist_authoritative_lineage_from_state,
    resolve_sequence_attempt_terminal,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _lineage_table_exists(db: Path) -> bool:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("scheduler_sequence_run_attempts",),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _lineage_schema_validators() -> tuple[
    jsonschema.Draft202012Validator,
    jsonschema.Draft202012Validator,
    jsonschema.Draft202012Validator,
]:
    attempt_schema = json.loads(
        schema_path("scheduler-sequence-run-attempt-v1.json").read_text(encoding="utf-8")
    )
    phase_schema = json.loads(
        schema_path("scheduler-sequence-phase-execution-v1.json").read_text(encoding="utf-8")
    )
    lineage_schema = json.loads(
        schema_path("scheduler-sequence-run-lineage-v1.json").read_text(encoding="utf-8")
    )
    registry = Registry().with_resources(
        [
            (attempt_schema["$id"], Resource.from_contents(attempt_schema)),
            (phase_schema["$id"], Resource.from_contents(phase_schema)),
            (lineage_schema["$id"], Resource.from_contents(lineage_schema)),
        ]
    )
    format_checker = jsonschema.FormatChecker()
    format_checker.checks("scheduler-sequence-phase-execution-v1")(_phase_execution_format_checker)
    format_checker.checks("scheduler-sequence-run-lineage-v1")(_lineage_aggregate_format_checker)
    return (
        jsonschema.Draft202012Validator(attempt_schema, registry=registry),
        jsonschema.Draft202012Validator(
            phase_schema,
            registry=registry,
            format_checker=format_checker,
        ),
        jsonschema.Draft202012Validator(
            lineage_schema,
            registry=registry,
            format_checker=format_checker,
        ),
    )


def _prepare_sequence_without_migration(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    manifest_name: str = "fixture-sequence",
    manifest_path: Path | None = None,
    sequence_id: str = FIXED_SEQUENCE_ID,
    run_ids: tuple[str, str] = FIXED_RUN_IDS,
) -> str:
    manifest_file = manifest_path or git_repo / "sequence.yaml"
    manifest = _write_manifest(
        manifest_file,
        _manifest_yaml(
            name=manifest_name,
            phases=[
                {
                    "name": "phase-one",
                    "plan_path": "docs/plans/sample-plan.md",
                    "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                    "commit_message": "checkpoint after phase one",
                    "codex": {
                        "review_model": "gpt-5.6-sol",
                        "review_reasoning_effort": "high",
                    },
                },
                {
                    "name": "phase-two",
                    "plan_path": "docs/plans/sample-plan.md",
                    "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                    "codex": {
                        "review_model": "gpt-5.6-sol",
                        "review_reasoning_effort": "high",
                    },
                },
            ],
        ),
    )
    run_counter = {"value": 0}

    def run_id_factory(_slug: str, _now: object) -> str:
        run_id = run_ids[run_counter["value"]]
        run_counter["value"] += 1
        return run_id

    service = SequencePrepareService(
        SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: _fake_repo_target(git_repo),
        now_factory=lambda: FIXED_NOW,
        sequence_id_factory=lambda _slug, _now: sequence_id,
        run_id_factory=run_id_factory,
    )
    result = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    return result.sequence_id


def _start_sequence_without_migration(
    scheduler_paths: dict[str, Path],
    sequence_id: str,
) -> object:
    service = SequenceStartService(
        SqliteSchedulerStore(scheduler_paths["db_path"], bootstrap=False),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: FIXED_NOW,
    )
    return service.start(sequence_id)


def _pause_v8_database(tmp_path: Path) -> Path:
    db = tmp_path / "v8.sqlite3"
    paused = False

    def pause_v9(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_sequence_run_attempts" in statement:
            paused = True
            raise RuntimeError("pause-v9")

    with pytest.raises(RuntimeError, match="pause-v9"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v9)
    assert _user_version(db) == 8
    return db


def _sample_attempt(**overrides: object) -> SequenceRunAttempt:
    payload = {
        "schema_version": 1,
        "generation": 1,
        "run_id": "run-000000000001",
        "source_run_id": None,
        "attempt_kind": SEQUENCE_ATTEMPT_KIND_PLANNED,
        "materialized_at": "2026-09-16T12:00:00.000000Z",
        "terminal_outcome": None,
        "resolved_at": None,
    }
    payload.update(overrides)
    return SequenceRunAttempt(**payload)


def _assert_schema_and_model_reject(
    payload: dict[str, object],
    *,
    schema_validator: jsonschema.Draft202012Validator,
    model_type: type[SequenceRunAttempt] | type[SequencePhaseExecution] | type[SequenceRunLineage],
) -> None:
    with pytest.raises(jsonschema.ValidationError):
        schema_validator.validate(payload)
    with pytest.raises(ValidationError):
        model_type.model_validate(payload)


def _assert_schema_and_model_accept(
    payload: dict[str, object],
    *,
    schema_validator: jsonschema.Draft202012Validator,
    model_type: type[SequenceRunAttempt] | type[SequencePhaseExecution] | type[SequenceRunLineage],
) -> None:
    schema_validator.validate(payload)
    model_type.model_validate(payload)


def _complete_phase_execution() -> SequencePhaseExecution:
    attempt = _sample_attempt(
        terminal_outcome="completed",
        resolved_at="2026-09-16T12:02:00.000000Z",
    )
    return SequencePhaseExecution(
        schema_version=1,
        ordinal=1,
        planned_run_id="run-000000000001",
        attempts=(attempt,),
        current_run_id="run-000000000001",
        accepted_run_id="run-000000000001",
    )


def test_generation_one_equals_planned_run_id() -> None:
    attempt = _sample_attempt(run_id="planned-run")
    phase = SequencePhaseExecution(
        schema_version=1,
        ordinal=1,
        planned_run_id="planned-run",
        attempts=(attempt,),
        current_run_id="planned-run",
        accepted_run_id=None,
    )
    assert phase.attempts[0].run_id == phase.planned_run_id


def test_superseded_attempt_requires_terminal_resolution() -> None:
    first = _sample_attempt(generation=1, run_id="run-a")
    second = SequenceRunAttempt(
        schema_version=1,
        generation=2,
        run_id="run-b",
        source_run_id="run-a",
        attempt_kind=SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        materialized_at="2026-09-16T12:01:00.000000Z",
        terminal_outcome=None,
        resolved_at=None,
    )
    with pytest.raises(ValidationError, match="superseded attempt must be terminal"):
        SequencePhaseExecution(
            schema_version=1,
            ordinal=1,
            planned_run_id="run-a",
            attempts=(first, second),
            current_run_id="run-b",
            accepted_run_id=None,
        )


def test_accepted_run_requires_authenticated_current_leaf() -> None:
    attempt = _sample_attempt(
        terminal_outcome="completed",
        resolved_at="2026-09-16T12:02:00.000000Z",
    )
    with pytest.raises(ValidationError, match="authenticated current leaf"):
        SequencePhaseExecution(
            schema_version=1,
            ordinal=1,
            planned_run_id="run-000000000001",
            attempts=(attempt,),
            current_run_id="run-000000000001",
            accepted_run_id="other-run",
        )


def test_schema_model_parity_for_all_lineage_shapes() -> None:
    attempt = _sample_attempt(
        terminal_outcome="completed",
        resolved_at="2026-09-16T12:02:00.000000Z",
    )
    phase = _complete_phase_execution()
    lineage = SequenceRunLineage(schema_version=1, sequence_id="seq-1", phase_executions=(phase,))
    attempt_validator, phase_validator, lineage_validator = _lineage_schema_validators()
    attempt_validator.validate(json.loads(attempt.model_dump_json()))
    phase_validator.validate(json.loads(phase.model_dump_json()))
    lineage_validator.validate(json.loads(lineage.model_dump_json()))
    unresolved_attempt = _sample_attempt()
    unresolved_payload = json.loads(unresolved_attempt.model_dump_json())
    attempt_validator.validate(unresolved_payload)
    SequenceRunAttempt.model_validate(unresolved_payload)
    for missing_field in ("schema_version", "source_run_id", "terminal_outcome", "resolved_at"):
        missing_payload = dict(unresolved_payload)
        del missing_payload[missing_field]
        _assert_schema_and_model_reject(
            missing_payload,
            schema_validator=attempt_validator,
            model_type=SequenceRunAttempt,
        )
    invalid_attempt = json.loads(attempt.model_dump_json())
    invalid_attempt["resolved_at"] = "2026-09-16T12:02:00.000000Z"
    invalid_attempt["terminal_outcome"] = None
    _assert_schema_and_model_reject(
        invalid_attempt,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    outcome_without_timestamp = dict(unresolved_payload)
    outcome_without_timestamp["terminal_outcome"] = "blocked"
    outcome_without_timestamp["resolved_at"] = None
    _assert_schema_and_model_reject(
        outcome_without_timestamp,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    timestamp_without_outcome = dict(unresolved_payload)
    timestamp_without_outcome["terminal_outcome"] = None
    timestamp_without_outcome["resolved_at"] = "2026-09-16T12:02:00.000000Z"
    _assert_schema_and_model_reject(
        timestamp_without_outcome,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    generation_one_with_source = dict(unresolved_payload)
    generation_one_with_source["source_run_id"] = "run-parent"
    _assert_schema_and_model_reject(
        generation_one_with_source,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    phase_payload = json.loads(phase.model_dump_json())
    phase_validator.validate(phase_payload)
    for missing_field in ("schema_version", "accepted_run_id"):
        missing_phase = dict(phase_payload)
        del missing_phase[missing_field]
        _assert_schema_and_model_reject(
            missing_phase,
            schema_validator=phase_validator,
            model_type=SequencePhaseExecution,
        )
    invalid_phase = dict(phase_payload)
    invalid_phase["current_run_id"] = "other-run"
    _assert_schema_and_model_reject(
        invalid_phase,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    invalid_phase = dict(phase_payload)
    invalid_phase["attempts"] = [
        dict(phase_payload["attempts"][0]),
        {
            **phase_payload["attempts"][0],
            "generation": 2,
            "run_id": "retry-run",
            "source_run_id": phase_payload["attempts"][0]["run_id"],
            "attempt_kind": SEQUENCE_ATTEMPT_KIND_PLANNED,
        },
    ]
    _assert_schema_and_model_reject(
        invalid_phase,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    invalid_phase = dict(phase_payload)
    invalid_phase["attempts"] = [
        dict(phase_payload["attempts"][0]),
        {
            **phase_payload["attempts"][0],
            "generation": 2,
            "run_id": "retry-run",
            "source_run_id": "wrong-source",
            "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        },
    ]
    _assert_schema_and_model_reject(
        invalid_phase,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    lineage_payload = json.loads(lineage.model_dump_json())
    lineage_validator.validate(lineage_payload)
    for missing_field in ("schema_version", "phase_executions"):
        missing_lineage = dict(lineage_payload)
        del missing_lineage[missing_field]
        _assert_schema_and_model_reject(
            missing_lineage,
            schema_validator=lineage_validator,
            model_type=SequenceRunLineage,
        )
    duplicate_phase = dict(lineage_payload)
    duplicate_phase["phase_executions"] = [
        dict(lineage_payload["phase_executions"][0]),
        dict(lineage_payload["phase_executions"][0]),
    ]
    _assert_schema_and_model_reject(
        duplicate_phase,
        schema_validator=lineage_validator,
        model_type=SequenceRunLineage,
    )


def test_generation_order_parity_rejects_gaps_duplicates_and_out_of_order_chains() -> None:
    phase = _complete_phase_execution()
    phase_payload = json.loads(phase.model_dump_json())
    _, phase_validator, _ = _lineage_schema_validators()
    first = dict(phase_payload["attempts"][0])
    gap_payload = dict(phase_payload)
    gap_payload["attempts"] = [
        first,
        {
            **first,
            "generation": 3,
            "run_id": "retry-run-gap",
            "source_run_id": first["run_id"],
            "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            "terminal_outcome": None,
            "resolved_at": None,
        },
    ]
    gap_payload["current_run_id"] = "retry-run-gap"
    gap_payload["accepted_run_id"] = None
    _assert_schema_and_model_reject(
        gap_payload,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    duplicate_payload = dict(phase_payload)
    duplicate_payload["attempts"] = [
        first,
        {
            **first,
            "generation": 1,
            "run_id": "retry-run-dup",
            "source_run_id": first["run_id"],
            "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            "terminal_outcome": None,
            "resolved_at": None,
        },
    ]
    duplicate_payload["current_run_id"] = "retry-run-dup"
    duplicate_payload["accepted_run_id"] = None
    _assert_schema_and_model_reject(
        duplicate_payload,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    second = {
        **first,
        "generation": 3,
        "run_id": "retry-run-mid",
        "source_run_id": first["run_id"],
        "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        "terminal_outcome": "blocked",
        "resolved_at": "2026-09-16T12:01:00.000000Z",
    }
    third = {
        **first,
        "generation": 2,
        "run_id": "retry-run-last",
        "source_run_id": "retry-run-mid",
        "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        "terminal_outcome": None,
        "resolved_at": None,
    }
    out_of_order_payload = dict(phase_payload)
    out_of_order_payload["attempts"] = [first, second, third]
    out_of_order_payload["current_run_id"] = "retry-run-last"
    out_of_order_payload["accepted_run_id"] = None
    _assert_schema_and_model_reject(
        out_of_order_payload,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )


def test_primitive_value_model_schema_parity() -> None:
    attempt = _sample_attempt(
        terminal_outcome="completed",
        resolved_at="2026-09-16T12:02:00.000000Z",
    )
    phase = _complete_phase_execution()
    lineage = SequenceRunLineage(schema_version=1, sequence_id="seq-1", phase_executions=(phase,))
    attempt_payload = json.loads(attempt.model_dump_json())
    phase_payload = json.loads(phase.model_dump_json())
    lineage_payload = json.loads(lineage.model_dump_json())
    attempt_validator, phase_validator, lineage_validator = _lineage_schema_validators()
    for bad_generation in (True, "1"):
        payload = dict(attempt_payload)
        payload["generation"] = bad_generation
        _assert_schema_and_model_reject(
            payload,
            schema_validator=attempt_validator,
            model_type=SequenceRunAttempt,
        )
    for bad_schema_version in (True, "1"):
        payload = dict(attempt_payload)
        payload["schema_version"] = bad_schema_version
        _assert_schema_and_model_reject(
            payload,
            schema_validator=attempt_validator,
            model_type=SequenceRunAttempt,
        )
    for bad_ordinal in (True, "1"):
        payload = dict(phase_payload)
        payload["ordinal"] = bad_ordinal
        _assert_schema_and_model_reject(
            payload,
            schema_validator=phase_validator,
            model_type=SequencePhaseExecution,
        )
    for field in ("run_id", "materialized_at"):
        payload = dict(attempt_payload)
        payload[field] = "   "
        _assert_schema_and_model_reject(
            payload,
            schema_validator=attempt_validator,
            model_type=SequenceRunAttempt,
        )
    resolved_payload = dict(attempt_payload)
    resolved_payload["resolved_at"] = "   "
    _assert_schema_and_model_reject(
        resolved_payload,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    retry_payload = dict(attempt_payload)
    retry_payload.update(
        {
            "generation": 2,
            "run_id": "retry-run",
            "source_run_id": attempt_payload["run_id"],
            "attempt_kind": SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            "terminal_outcome": None,
            "resolved_at": None,
        }
    )
    retry_payload["source_run_id"] = "   "
    _assert_schema_and_model_reject(
        retry_payload,
        schema_validator=attempt_validator,
        model_type=SequenceRunAttempt,
    )
    for field in ("planned_run_id", "current_run_id"):
        payload = dict(phase_payload)
        payload[field] = "   "
        _assert_schema_and_model_reject(
            payload,
            schema_validator=phase_validator,
            model_type=SequencePhaseExecution,
        )
    accepted_payload = dict(phase_payload)
    accepted_payload["accepted_run_id"] = "   "
    _assert_schema_and_model_reject(
        accepted_payload,
        schema_validator=phase_validator,
        model_type=SequencePhaseExecution,
    )
    sequence_payload = dict(lineage_payload)
    sequence_payload["sequence_id"] = "   "
    _assert_schema_and_model_reject(
        sequence_payload,
        schema_validator=lineage_validator,
        model_type=SequenceRunLineage,
    )


def test_json_native_integer_model_schema_parity() -> None:
    attempt = _sample_attempt(
        terminal_outcome="completed",
        resolved_at="2026-09-16T12:02:00.000000Z",
    )
    phase = _complete_phase_execution()
    attempt_payload = json.loads(attempt.model_dump_json())
    phase_payload = json.loads(phase.model_dump_json())
    attempt_validator, phase_validator, _ = _lineage_schema_validators()
    cases = (
        ("generation", attempt_payload, attempt_validator, SequenceRunAttempt),
        ("ordinal", phase_payload, phase_validator, SequencePhaseExecution),
    )
    for field_name, base_payload, schema_validator, model_type in cases:
        integral_payload = dict(base_payload)
        integral_payload[field_name] = 1.0
        _assert_schema_and_model_accept(
            integral_payload,
            schema_validator=schema_validator,
            model_type=model_type,
        )
        non_integral_payload = dict(base_payload)
        non_integral_payload[field_name] = 1.5
        _assert_schema_and_model_reject(
            non_integral_payload,
            schema_validator=schema_validator,
            model_type=model_type,
        )
        for bad_value in (True, "1"):
            bad_payload = dict(base_payload)
            bad_payload[field_name] = bad_value
            _assert_schema_and_model_reject(
                bad_payload,
                schema_validator=schema_validator,
                model_type=model_type,
            )


def test_validate_lineage_json_schema_rejects_malformed_payloads_safely() -> None:
    phase = _complete_phase_execution()
    lineage_payload = json.loads(
        SequenceRunLineage(
            schema_version=1,
            sequence_id="seq-1",
            phase_executions=(phase,),
        ).model_dump_json()
    )
    malformed_attempts = dict(lineage_payload)
    malformed_attempts["phase_executions"] = {"ordinal": 1}
    with pytest.raises(
        SequenceRunLineageValidationError, match="lineage aggregate payload is invalid"
    ):
        validate_lineage_json_schema(malformed_attempts)
    malformed_terminal = json.loads(json.dumps(lineage_payload))
    malformed_terminal["phase_executions"][0]["attempts"][0]["terminal_outcome"] = []
    with pytest.raises(
        SequenceRunLineageValidationError, match="lineage aggregate payload is invalid"
    ):
        validate_lineage_json_schema(malformed_terminal)
    malformed_phase_attempts = json.loads(json.dumps(lineage_payload))
    malformed_phase_attempts["phase_executions"][0]["attempts"] = {"generation": 1}
    with pytest.raises(
        SequenceRunLineageValidationError, match="lineage aggregate payload is invalid"
    ):
        validate_lineage_json_schema(malformed_phase_attempts)


def test_v9_migration_backfills_blocked_and_abort_pending_sequences(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    db = _pause_v8_database(tmp_path)
    store = SqliteSchedulerStore(db, bootstrap=False)
    scheduler_paths = {
        "db_path": db,
        "artifact_root": tmp_path / "artifacts",
    }
    sequence_id = _prepare_sequence_without_migration(git_repo, scheduler_paths)
    start = _start_sequence_without_migration(scheduler_paths, sequence_id)
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=start.run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            blocked_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            block_reason_kind="preflight_blocked",
            block_reason_summary="simulated",
            context=state.context,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        reconcile.reconcile_run(conn, start.run_id)
    pending_sequence_id = _prepare_sequence_without_migration(
        git_repo,
        scheduler_paths,
        manifest_name="fixture-sequence-pending",
        manifest_path=git_repo / "sequence-pending.yaml",
        sequence_id="fixture-project-seq-20260912T120000Z-pending",
        run_ids=(
            "fixture-project-20260912T120000Z-run100",
            "fixture-project-20260912T120000Z-run101",
        ),
    )
    pending_start = _start_sequence_without_migration(scheduler_paths, pending_sequence_id)
    with store.begin_immediate() as conn:
        active_pending = store.load_validated_sequence_state(conn, pending_sequence_id)
        assert isinstance(active_pending, ActiveSequenceState)
        now_text = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        abort_pending = AbortPendingSequenceState(
            schema_version=active_pending.schema_version,
            sequence_id=pending_sequence_id,
            version=active_pending.version + 1,
            prepared_at=active_pending.prepared_at,
            updated_at=now_text,
            started_at=active_pending.started_at,
            abort_requested_at=now_text,
            abort_reason="user_requested_abort",
            idempotency_key=active_pending.idempotency_key,
            definition=active_pending.definition,
            current_ordinal=active_pending.current_ordinal,
            current_run_id=pending_start.run_id,
            materialized_entries=active_pending.materialized_entries,
            residual_risk_ordinals=active_pending.residual_risk_ordinals,
            cancelled_ordinals=future_entry_ordinals(
                active_pending.definition,
                active_pending.current_ordinal,
            ),
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=pending_sequence_id,
            expected_version=active_pending.version,
            new_state=abort_pending,
            now=FIXED_NOW,
        )
    assert _user_version(db) == 8
    assert not _lineage_table_exists(db)
    with store.begin_read() as conn:
        blocked_before = store.load_validated_sequence_state(conn, sequence_id)
        pending_before = store.load_validated_sequence_state(conn, pending_sequence_id)
    assert isinstance(blocked_before, BlockedSequenceState)
    assert isinstance(pending_before, AbortPendingSequenceState)
    migrated = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    with migrated.begin_read() as conn:
        blocked_lineage = load_sequence_run_lineage(conn, sequence_id)
        blocked_sequence = migrated.load_validated_sequence_state(conn, sequence_id)
        pending_sequence = migrated.load_validated_sequence_state(conn, pending_sequence_id)
        pending_lineage = load_sequence_run_lineage(conn, pending_sequence_id)
    assert isinstance(blocked_sequence, BlockedSequenceState)
    blocked_phase = blocked_lineage.phase_executions[0]
    assert blocked_phase.attempts[0].terminal_outcome == "blocked"
    assert blocked_phase.attempts[0].resolved_at == blocked_sequence.blocked_at
    assert blocked_phase.accepted_run_id is None
    assert isinstance(pending_sequence, AbortPendingSequenceState)
    pending_phase = pending_lineage.phase_executions[0]
    assert pending_phase.attempts[0].terminal_outcome is None
    assert pending_phase.accepted_run_id is None


def test_v9_migration_backfills_finalized_multi_phase_sequence_with_distinct_timestamps(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    db = _pause_v8_database(tmp_path)
    store = SqliteSchedulerStore(db, bootstrap=False)
    scheduler_paths = {
        "db_path": db,
        "artifact_root": tmp_path / "artifacts",
    }
    sequence_id = _prepare_sequence_without_migration(git_repo, scheduler_paths)
    start = _start_sequence_without_migration(scheduler_paths, sequence_id)
    start_at = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    handoff_at = (FIXED_NOW + timedelta(hours=2)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    finalized_at = (
        (FIXED_NOW + timedelta(hours=4)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    final_entry = active.definition.entries[1]
    phase_one = MaterializedSequenceEntry(
        ordinal=1,
        run_id=start.run_id,
        entry_hash=active.materialized_entries[0].entry_hash,
        materialized_at=start_at,
    )
    phase_two = MaterializedSequenceEntry(
        ordinal=2,
        run_id=final_entry.planned_run_id,
        entry_hash=frozen_entry_hash(final_entry),
        materialized_at=handoff_at,
    )
    awaiting = AwaitingFinalizationSequenceState(
        schema_version=active.schema_version,
        sequence_id=sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=finalized_at,
        started_at=active.started_at,
        finalized_at=finalized_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=final_entry.planned_run_id,
        final_outcome="completed",
        materialized_entries=(phase_one, phase_two),
    )
    with store.begin_immediate() as conn:
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=awaiting,
            now=FIXED_NOW,
        )
    assert _user_version(db) == 8
    assert not _lineage_table_exists(db)
    with store.begin_read() as conn:
        pre_migration = store.load_validated_sequence_state(
            conn, sequence_id, validate_lineage=False
        )
    assert isinstance(pre_migration, AwaitingFinalizationSequenceState)
    migrated = SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    with migrated.begin_read() as conn:
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=awaiting.definition)
        sequence = migrated.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AwaitingFinalizationSequenceState)
    assert len(lineage.phase_executions) == 2
    phase_one_lineage = lineage.phase_executions[0]
    phase_two_lineage = lineage.phase_executions[1]
    assert phase_one_lineage.attempts[0].materialized_at == start_at
    assert phase_one_lineage.attempts[0].resolved_at == handoff_at
    assert phase_two_lineage.attempts[0].materialized_at == handoff_at
    assert phase_two_lineage.attempts[0].resolved_at == finalized_at
    assert start_at != handoff_at != finalized_at
    projected = project_historical_lineage_from_state(sequence)
    assert projected.phase_executions[0].attempts[0].resolved_at == handoff_at
    assert projected.phase_executions[1].attempts[0].resolved_at == finalized_at


def test_sequence_start_writes_generation_one_lineage(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        lineage = load_sequence_run_lineage(conn, sequence_id)
        state = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(state, ActiveSequenceState)
    assert len(lineage.phase_executions) == 1
    phase = lineage.phase_executions[0]
    assert phase.current_run_id == start.run_id
    assert phase.attempts[0].generation == 1
    assert phase.attempts[0].terminal_outcome is None


def test_partial_lineage_rows_are_rejected(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash
    from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        final_entry = active.definition.entries[1]
        awaiting = AwaitingFinalizationSequenceState(
            schema_version=1,
            sequence_id=sequence_id,
            version=active.version + 1,
            prepared_at=active.prepared_at,
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            started_at=active.started_at,
            finalized_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            idempotency_key=active.idempotency_key,
            definition=active.definition,
            final_run_id=final_entry.planned_run_id,
            final_outcome="completed",
            materialized_entries=(
                active.materialized_entries[0],
                MaterializedSequenceEntry(
                    ordinal=2,
                    run_id=final_entry.planned_run_id,
                    entry_hash=frozen_entry_hash(final_entry),
                    materialized_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            ),
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=awaiting,
            now=FIXED_NOW,
        )
        with pytest.raises(SchedulerEngineError, match="missing materialized phase rows"):
            store.load_validated_sequence_state(conn, sequence_id)


def test_block_reconcile_cas_loser_leaves_lineage_unresolved(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=start.run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            blocked_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            block_reason_kind="preflight_blocked",
            block_reason_summary="simulated",
            context=state.context,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        stale = active.model_copy(update={"version": active.version - 1})
        receipt = reconcile._block_sequence(
            conn,
            sequence_state=stale,
            run_id=start.run_id,
            block_reason_kind="preflight_blocked",
            now=FIXED_NOW,
        )
        assert receipt.action == "sequence_block_cas_lost"
        lineage = load_sequence_run_lineage(conn, sequence_id)
    assert lineage.phase_executions[0].attempts[0].terminal_outcome is None


def test_compare_and_swap_execution_leaf_reloads_and_rejects_stale_definition(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        replacement = SequenceRunAttempt(
            schema_version=1,
            generation=2,
            run_id="retry-run-00000001",
            source_run_id=start.run_id,
            attempt_kind=SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            materialized_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            terminal_outcome=None,
            resolved_at=None,
        )
        updated_materialized = MaterializedSequenceEntry(
            ordinal=1,
            run_id=replacement.run_id,
            entry_hash=active.materialized_entries[0].entry_hash,
            materialized_at=replacement.materialized_at,
        )
        updated = active.model_copy(
            update={
                "version": active.version + 1,
                "current_run_id": replacement.run_id,
                "materialized_entries": (updated_materialized,),
            }
        )
        expectation = SequenceExecutionCASExpectation(
            expected_version=active.version,
            definition=active.definition,
            materialized_entries=active.materialized_entries,
            current_ordinal=active.current_ordinal,
            current_run_id=active.current_run_id,
        )
        assert store.compare_and_swap_sequence_execution_leaf(
            conn,
            sequence_id=sequence_id,
            ordinal=1,
            expectation=expectation,
            expected_current_run_id=start.run_id,
            replacement_attempt=replacement,
            updated_sequence_state=updated,
            now=FIXED_NOW,
        )
        reloaded = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=active.definition)
        assert isinstance(reloaded, ActiveSequenceState)
        assert reloaded.current_run_id == replacement.run_id
        assert len(lineage.phase_executions[0].attempts) == 2
        assert lineage.phase_executions[0].attempts[0].terminal_outcome == "blocked"
        assert lineage.phase_executions[0].attempts[1].terminal_outcome is None
        from dataclasses import replace

        current_expectation = SequenceExecutionCASExpectation(
            expected_version=reloaded.version,
            definition=reloaded.definition,
            materialized_entries=reloaded.materialized_entries,
            current_ordinal=reloaded.current_ordinal,
            current_run_id=reloaded.current_run_id,
        )
        with pytest.raises(SchedulerEngineError, match="contiguous"):
            store.compare_and_swap_sequence_execution_leaf(
                conn,
                sequence_id=sequence_id,
                ordinal=1,
                expectation=current_expectation,
                expected_current_run_id=replacement.run_id,
                replacement_attempt=replacement.model_copy(
                    update={"generation": 4, "run_id": "retry-run-00000002"}
                ),
                updated_sequence_state=reloaded.model_copy(
                    update={
                        "version": reloaded.version + 1,
                        "current_run_id": "retry-run-00000002",
                    }
                ),
                now=FIXED_NOW,
            )
        stale_definition = replace(
            current_expectation,
            definition=current_expectation.definition.model_copy(
                update={"name": "stale-definition"}
            ),
        )
        assert not store.compare_and_swap_sequence_execution_leaf(
            conn,
            sequence_id=sequence_id,
            ordinal=1,
            expectation=stale_definition,
            expected_current_run_id=replacement.run_id,
            replacement_attempt=replacement.model_copy(
                update={
                    "generation": 3,
                    "run_id": "retry-run-00000003",
                    "source_run_id": replacement.run_id,
                }
            ),
            updated_sequence_state=reloaded.model_copy(
                update={
                    "version": reloaded.version + 1,
                    "current_run_id": "retry-run-00000003",
                    "materialized_entries": (
                        MaterializedSequenceEntry(
                            ordinal=1,
                            run_id="retry-run-00000003",
                            entry_hash=reloaded.materialized_entries[0].entry_hash,
                            materialized_at=replacement.materialized_at,
                        ),
                    ),
                }
            ),
            now=FIXED_NOW,
        )


def test_open_readonly_v8_database_restores_planned_run_binding_check(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    db = _pause_v8_database(tmp_path)
    scheduler_paths = {"db_path": db, "artifact_root": tmp_path / "artifacts"}
    sequence_id = _prepare_sequence_without_migration(git_repo, scheduler_paths)
    _start_sequence_without_migration(scheduler_paths, sequence_id)
    store = SqliteSchedulerStore(db, bootstrap=False)
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        drifted = active.model_copy(
            update={
                "version": active.version + 1,
                "current_run_id": "drifted-run-id",
                "materialized_entries": (
                    MaterializedSequenceEntry(
                        ordinal=1,
                        run_id="drifted-run-id",
                        entry_hash=active.materialized_entries[0].entry_hash,
                        materialized_at=active.materialized_entries[0].materialized_at,
                    ),
                ),
            }
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=drifted,
            now=FIXED_NOW,
        )
    readonly = SqliteSchedulerStore.open_readonly(db)
    with (
        readonly.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="planned run binding"),
    ):
        readonly.load_validated_sequence_state(conn, sequence_id)


def test_historical_projection_for_blocked_sequence_uses_blocked_at(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=start.run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            blocked_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            block_reason_kind="preflight_blocked",
            block_reason_summary="simulated",
            context=state.context,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        reconcile.reconcile_run(conn, start.run_id)
        blocked_sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(blocked_sequence, BlockedSequenceState)
    projected = project_historical_lineage_from_state(blocked_sequence)
    assert projected.phase_executions[0].attempts[0].resolved_at == blocked_sequence.blocked_at


def test_lineage_rejects_duplicate_run_ids_across_ordinals() -> None:
    shared_run = "run-shared-across-phases"
    phase_one = SequencePhaseExecution(
        schema_version=1,
        ordinal=1,
        planned_run_id=shared_run,
        attempts=(
            _sample_attempt(
                run_id=shared_run,
                terminal_outcome="completed",
                resolved_at="2026-09-16T10:00:00.000000Z",
            ),
        ),
        current_run_id=shared_run,
        accepted_run_id=shared_run,
    )
    phase_two = SequencePhaseExecution(
        schema_version=1,
        ordinal=2,
        planned_run_id=shared_run,
        attempts=(_sample_attempt(run_id=shared_run),),
        current_run_id=shared_run,
        accepted_run_id=None,
    )
    with pytest.raises(ValidationError, match="unique across sequence lineage"):
        SequenceRunLineage(
            schema_version=1,
            sequence_id="seq-1",
            phase_executions=(phase_one, phase_two),
        )


def test_earlier_phase_retains_distinct_resolution_timestamp(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from datetime import timedelta

    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    start_at = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    handoff_at = (FIXED_NOW + timedelta(hours=2)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    finalized_at = (
        (FIXED_NOW + timedelta(hours=4)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    )
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    final_entry = active.definition.entries[1]
    phase_one = MaterializedSequenceEntry(
        ordinal=1,
        run_id=start.run_id,
        entry_hash=active.materialized_entries[0].entry_hash,
        materialized_at=start_at,
    )
    phase_two_materialized = MaterializedSequenceEntry(
        ordinal=2,
        run_id=final_entry.planned_run_id,
        entry_hash=frozen_entry_hash(final_entry),
        materialized_at=handoff_at,
    )
    awaiting = AwaitingFinalizationSequenceState(
        schema_version=1,
        sequence_id=sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=finalized_at,
        started_at=active.started_at,
        finalized_at=finalized_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=final_entry.planned_run_id,
        final_outcome="completed",
        materialized_entries=(phase_one, phase_two_materialized),
    )
    stored_lineage = project_historical_lineage_from_state(awaiting)
    validate_lineage_terminal_outcomes_for_state(awaiting, stored_lineage)
    phase_one_attempt = stored_lineage.phase_executions[0].attempts[0]
    assert phase_one_attempt.materialized_at == start_at
    assert phase_one_attempt.resolved_at == handoff_at
    assert stored_lineage.phase_executions[1].attempts[0].resolved_at == finalized_at
    assert start_at != handoff_at != finalized_at
    with store.begin_immediate() as conn:
        from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
            resolve_accepted_phase_handoff,
        )

        resolve_accepted_phase_handoff(
            store,
            conn,
            sequence_id=sequence_id,
            predecessor_ordinal=1,
            predecessor_run_id=start.run_id,
            accepted_outcome="completed",
            successor_ordinal=2,
            successor_run_id=final_entry.planned_run_id,
            successor_planned_run_id=final_entry.planned_run_id,
            successor_materialized_at=handoff_at,
            resolved_at=FIXED_NOW + timedelta(hours=2),
        )
        loaded = load_sequence_run_lineage(conn, sequence_id, definition=active.definition)
    assert loaded.phase_executions[0].attempts[0].resolved_at == handoff_at
    mismatched = stored_lineage.model_copy(
        update={
            "phase_executions": (
                stored_lineage.phase_executions[0].model_copy(
                    update={
                        "attempts": (
                            stored_lineage.phase_executions[0]
                            .attempts[0]
                            .model_copy(update={"resolved_at": start_at}),
                        )
                    }
                ),
                stored_lineage.phase_executions[1],
            )
        }
    )
    with pytest.raises(
        SequenceRunLineageValidationError,
        match="lineage resolved_at disagrees with sequence state",
    ):
        validate_lineage_terminal_outcomes_for_state(awaiting, mismatched)


def test_execution_leaf_cas_loser_leaves_predecessor_unresolved(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from unittest.mock import patch

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        replacement = SequenceRunAttempt(
            schema_version=1,
            generation=2,
            run_id="retry-run-00000001",
            source_run_id=start.run_id,
            attempt_kind=SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            materialized_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            terminal_outcome=None,
            resolved_at=None,
        )
        updated = active.model_copy(
            update={
                "version": active.version + 1,
                "current_run_id": replacement.run_id,
                "materialized_entries": (
                    MaterializedSequenceEntry(
                        ordinal=1,
                        run_id=replacement.run_id,
                        entry_hash=active.materialized_entries[0].entry_hash,
                        materialized_at=replacement.materialized_at,
                    ),
                ),
            }
        )
        expectation = SequenceExecutionCASExpectation(
            expected_version=active.version,
            definition=active.definition,
            materialized_entries=active.materialized_entries,
            current_ordinal=active.current_ordinal,
            current_run_id=active.current_run_id,
        )
        with patch.object(store, "compare_and_swap_sequence_state", return_value=False):
            assert not store.compare_and_swap_sequence_execution_leaf(
                conn,
                sequence_id=sequence_id,
                ordinal=1,
                expectation=expectation,
                expected_current_run_id=start.run_id,
                replacement_attempt=replacement,
                updated_sequence_state=updated,
                now=FIXED_NOW,
            )
        lineage = load_sequence_run_lineage(conn, sequence_id)
    assert lineage.phase_executions[0].attempts[0].terminal_outcome is None


def test_malformed_lineage_row_fails_validated_read(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET attempt_kind = 'invalid_kind'
            WHERE sequence_id = ? AND ordinal = 1 AND generation = 1
            """,
            (sequence_id,),
        )
        with pytest.raises(SchedulerEngineError, match="lineage row payload is invalid"):
            store.load_validated_sequence_state(conn, sequence_id)


def test_validated_read_rejects_malformed_lineage_ordinals(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET ordinal = 0
            WHERE sequence_id = ? AND generation = 1
            """,
            (sequence_id,),
        )
        with pytest.raises(
            SchedulerEngineError, match="lineage ordinal exceeds sequence definition"
        ):
            store.load_validated_sequence_state(conn, sequence_id)


def test_validated_read_rejects_non_contiguous_generations(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_sequence_run_attempts(
                sequence_id, ordinal, generation, run_id, source_run_id,
                attempt_kind, materialized_at, terminal_outcome, resolved_at
            ) VALUES (?, 1, 3, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                sequence_id,
                "retry-run-gap",
                start.run_id,
                SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
                FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        with pytest.raises(SchedulerEngineError, match="lineage phase payload is invalid"):
            store.load_validated_sequence_state(conn, sequence_id)


def test_validated_read_rejects_broken_attempt_source_chain(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_sequence_run_attempts(
                sequence_id, ordinal, generation, run_id, source_run_id,
                attempt_kind, materialized_at, terminal_outcome, resolved_at
            ) VALUES (?, 1, 2, ?, ?, ?, ?, 'blocked', ?)
            """,
            (
                sequence_id,
                "retry-run-broken",
                "wrong-source-run",
                SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
                FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET terminal_outcome = 'blocked', resolved_at = ?
            WHERE sequence_id = ? AND ordinal = 1 AND generation = 1
            """,
            (
                FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                sequence_id,
            ),
        )
        with pytest.raises(SchedulerEngineError, match="lineage phase payload is invalid"):
            store.load_validated_sequence_state(conn, sequence_id)


def test_build_lineage_from_rows_rejects_cross_sequence_rows() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT 'other-sequence' AS sequence_id, 1 AS ordinal, 1 AS generation,
               'run-a' AS run_id, NULL AS source_run_id, 'planned_run' AS attempt_kind,
               '2026-09-16T12:00:00.000000Z' AS materialized_at,
               NULL AS terminal_outcome, NULL AS resolved_at
        """
    ).fetchone()
    with pytest.raises(SequenceRunLineageValidationError, match="another sequence"):
        build_lineage_from_rows("seq-a", [row])  # type: ignore[list-item]


def _terminal_resolution_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE scheduler_sequence_run_attempts (
            sequence_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            run_id TEXT NOT NULL,
            source_run_id TEXT,
            attempt_kind TEXT NOT NULL,
            materialized_at TEXT NOT NULL,
            terminal_outcome TEXT,
            resolved_at TEXT
        )
        """
    )
    return conn


def _insert_unresolved_attempt(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO scheduler_sequence_run_attempts(
            sequence_id, ordinal, generation, run_id, source_run_id,
            attempt_kind, materialized_at, terminal_outcome, resolved_at
        ) VALUES (?, 1, 1, ?, NULL, ?, ?, NULL, NULL)
        """,
        (
            "seq-1",
            "run-000000000001",
            SEQUENCE_ATTEMPT_KIND_PLANNED,
            "2026-09-16T12:00:00.000000Z",
        ),
    )


def test_resolve_sequence_attempt_terminal_idempotent_replay() -> None:
    conn = _terminal_resolution_conn()
    _insert_unresolved_attempt(conn)
    resolved_at = FIXED_NOW
    resolve_sequence_attempt_terminal(
        conn,
        sequence_id="seq-1",
        ordinal=1,
        run_id="run-000000000001",
        terminal_outcome="blocked",
        resolved_at=resolved_at,
    )
    resolve_sequence_attempt_terminal(
        conn,
        sequence_id="seq-1",
        ordinal=1,
        run_id="run-000000000001",
        terminal_outcome="blocked",
        resolved_at=resolved_at,
    )
    row = conn.execute(
        """
        SELECT terminal_outcome, resolved_at
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ? AND run_id = ?
        """,
        ("seq-1", 1, "run-000000000001"),
    ).fetchone()
    assert row is not None
    assert row["terminal_outcome"] == "blocked"
    assert row["resolved_at"] == resolved_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_resolve_sequence_attempt_terminal_rejects_contradictory_replay() -> None:
    conn = _terminal_resolution_conn()
    _insert_unresolved_attempt(conn)
    resolve_sequence_attempt_terminal(
        conn,
        sequence_id="seq-1",
        ordinal=1,
        run_id="run-000000000001",
        terminal_outcome="blocked",
        resolved_at=FIXED_NOW,
    )
    with pytest.raises(SchedulerEngineError, match="disagrees with stored outcome"):
        resolve_sequence_attempt_terminal(
            conn,
            sequence_id="seq-1",
            ordinal=1,
            run_id="run-000000000001",
            terminal_outcome="aborted",
            resolved_at=FIXED_NOW,
        )


def test_resolve_sequence_attempt_terminal_rejects_contradictory_timestamp_replay() -> None:
    from datetime import timedelta

    conn = _terminal_resolution_conn()
    _insert_unresolved_attempt(conn)
    resolve_sequence_attempt_terminal(
        conn,
        sequence_id="seq-1",
        ordinal=1,
        run_id="run-000000000001",
        terminal_outcome="blocked",
        resolved_at=FIXED_NOW,
    )
    with pytest.raises(SchedulerEngineError, match="disagrees with stored resolved_at"):
        resolve_sequence_attempt_terminal(
            conn,
            sequence_id="seq-1",
            ordinal=1,
            run_id="run-000000000001",
            terminal_outcome="blocked",
            resolved_at=FIXED_NOW + timedelta(seconds=1),
        )


def test_resolve_sequence_attempt_terminal_rejects_half_populated_rows() -> None:
    conn = _terminal_resolution_conn()
    _insert_unresolved_attempt(conn)
    conn.execute(
        """
        UPDATE scheduler_sequence_run_attempts
        SET terminal_outcome = 'blocked', resolved_at = NULL
        WHERE sequence_id = ? AND run_id = ?
        """,
        ("seq-1", "run-000000000001"),
    )
    with pytest.raises(SchedulerEngineError, match="lineage row payload is invalid"):
        resolve_sequence_attempt_terminal(
            conn,
            sequence_id="seq-1",
            ordinal=1,
            run_id="run-000000000001",
            terminal_outcome="blocked",
            resolved_at=FIXED_NOW,
        )
    conn.execute(
        """
        UPDATE scheduler_sequence_run_attempts
        SET terminal_outcome = NULL, resolved_at = ?
        WHERE sequence_id = ? AND run_id = ?
        """,
        (
            FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "seq-1",
            "run-000000000001",
        ),
    )
    with pytest.raises(SchedulerEngineError, match="lineage row payload is invalid"):
        resolve_sequence_attempt_terminal(
            conn,
            sequence_id="seq-1",
            ordinal=1,
            run_id="run-000000000001",
            terminal_outcome="blocked",
            resolved_at=FIXED_NOW,
        )


def test_validated_read_rejects_tampered_leaf_materialized_at(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET materialized_at = '2026-09-16T09:00:00.000000Z'
            WHERE sequence_id = ? AND ordinal = 1 AND generation = 1
            """,
            (sequence_id,),
        )
        with pytest.raises(
            SchedulerEngineError,
            match="lineage leaf materialized_at disagrees with materialized entry",
        ):
            store.load_validated_sequence_state(conn, sequence_id)


def test_persist_authoritative_lineage_rejects_immutable_field_mismatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=start.run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            blocked_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            block_reason_kind="preflight_blocked",
            block_reason_summary="simulated",
            context=state.context,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        reconcile.reconcile_run(conn, start.run_id)
        blocked_sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(blocked_sequence, BlockedSequenceState)
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET materialized_at = '2026-09-16T09:00:00.000000Z',
                terminal_outcome = NULL,
                resolved_at = NULL
            WHERE sequence_id = ? AND ordinal = 1 AND generation = 1
            """,
            (sequence_id,),
        )
        with pytest.raises(SchedulerEngineError, match="replay disagrees with stored row"):
            persist_authoritative_lineage_from_state(conn, blocked_sequence)


def test_validated_read_rejects_nonnumeric_lineage_ordinal_without_exposing_sentinel(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.infrastructure import sequence_run_lineage_store

    sentinel = "CORRUPTION_SENTINEL_ORDINAL_XYZ"
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    original_loader = sequence_run_lineage_store.load_sequence_run_lineage_rows

    def corrupt_loader(conn: sqlite3.Connection, loaded_sequence_id: str) -> list[sqlite3.Row]:
        rows = original_loader(conn, loaded_sequence_id)
        if not rows:
            return rows
        base = rows[0]

        class CorruptRow:
            def __getitem__(self, key: str) -> object:
                if key == "ordinal":
                    return sentinel
                return base[key]

        return [CorruptRow()]  # type: ignore[list-item]

    monkeypatch.setattr(
        sequence_run_lineage_store,
        "load_sequence_run_lineage_rows",
        corrupt_loader,
    )
    with (
        store.begin_read() as conn,
        pytest.raises(
            SchedulerEngineError,
            match="lineage row payload is invalid",
        ) as excinfo,
    ):
        store.load_validated_sequence_state(conn, sequence_id)
    assert sentinel not in str(excinfo.value)
