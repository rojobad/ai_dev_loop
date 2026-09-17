"""Phase 20.9 sequence lifecycle matrix and validator alignment."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
    SequenceLifecycleValidationError,
    validate_sequence_lifecycle_transition,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_active_to_awaiting_finalization_accepts_authenticated_leaf(
    git_repo,
    scheduler_paths,
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    final_ordinal = len(active.definition.entries)
    final_materialized = MaterializedSequenceEntry(
        ordinal=final_ordinal,
        run_id=start.run_id,
        entry_hash=active.materialized_entries[0].entry_hash,
        materialized_at=active.updated_at,
    )
    materialized_both = (active.materialized_entries[0], final_materialized)
    active_at_final = active.model_copy(
        update={"current_ordinal": final_ordinal, "materialized_entries": materialized_both}
    )
    finalized = AwaitingFinalizationSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=active.updated_at,
        started_at=active.started_at,
        finalized_at=active.updated_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=start.run_id,
        final_outcome="completed",
        materialized_entries=materialized_both,
    )
    validate_sequence_lifecycle_transition(active_at_final, finalized)
    residual = AwaitingFinalizationSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=active.updated_at,
        started_at=active.started_at,
        finalized_at=active.updated_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=start.run_id,
        final_outcome="completed_with_residual_risk",
        materialized_entries=materialized_both,
        residual_risk_ordinals=(final_ordinal,),
    )
    validate_sequence_lifecycle_transition(active_at_final, residual)


def test_active_to_awaiting_finalization_requires_authenticated_leaf(
    git_repo,
    scheduler_paths,
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    phase_two = active.definition.entries[1]
    final_ordinal = len(active.definition.entries)
    final_materialized = MaterializedSequenceEntry(
        ordinal=final_ordinal,
        run_id=phase_two.planned_run_id,
        entry_hash=active.materialized_entries[0].entry_hash,
        materialized_at=active.updated_at,
    )
    materialized_both = (active.materialized_entries[0], final_materialized)
    wrong_leaf = AwaitingFinalizationSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=active.updated_at,
        started_at=active.started_at,
        finalized_at=active.updated_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=final_materialized.run_id,
        final_outcome="completed",
        materialized_entries=materialized_both,
    )
    active_at_final = active.model_copy(
        update={
            "current_ordinal": final_ordinal,
            "materialized_entries": materialized_both,
        }
    )
    with pytest.raises(
        SequenceLifecycleValidationError,
        match="authenticated current leaf",
    ):
        validate_sequence_lifecycle_transition(active_at_final, wrong_leaf)


def test_handoff_rejects_residual_risk_swap_without_preserving_prior_ordinals(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    at_ordinal_two = active.model_copy(
        update={
            "current_ordinal": 2,
            "residual_risk_ordinals": (1,),
            "materialized_entries": (
                *active.materialized_entries,
                MaterializedSequenceEntry(
                    ordinal=2,
                    run_id="fixture-project-20260912T120000Z-run001",
                    entry_hash=active.materialized_entries[0].entry_hash,
                    materialized_at=active.updated_at,
                ),
            ),
            "current_run_id": "fixture-project-20260912T120000Z-run001",
        }
    )
    swapped = at_ordinal_two.model_copy(
        update={
            "version": at_ordinal_two.version + 1,
            "current_ordinal": 3,
            "residual_risk_ordinals": (2,),
            "materialized_entries": (
                *at_ordinal_two.materialized_entries,
                MaterializedSequenceEntry(
                    ordinal=3,
                    run_id="fixture-project-20260912T120000Z-run002",
                    entry_hash=at_ordinal_two.materialized_entries[0].entry_hash,
                    materialized_at=at_ordinal_two.updated_at,
                ),
            ),
            "current_run_id": "fixture-project-20260912T120000Z-run002",
        }
    )
    with pytest.raises(SequenceLifecycleValidationError, match="must not remove residual_risk"):
        validate_sequence_lifecycle_transition(at_ordinal_two, swapped)


def test_handoff_rejects_residual_risk_removal(git_repo, scheduler_paths) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    with_residual = active.model_copy(update={"residual_risk_ordinals": (1,)})
    handoff = with_residual.model_copy(
        update={
            "version": with_residual.version + 1,
            "current_ordinal": 2,
            "residual_risk_ordinals": (),
            "materialized_entries": (
                *with_residual.materialized_entries,
                MaterializedSequenceEntry(
                    ordinal=2,
                    run_id="fixture-project-20260912T120000Z-run001",
                    entry_hash=with_residual.materialized_entries[0].entry_hash,
                    materialized_at=with_residual.updated_at,
                ),
            ),
            "current_run_id": "fixture-project-20260912T120000Z-run001",
        }
    )
    with pytest.raises(SequenceLifecycleValidationError, match="must not remove residual_risk"):
        validate_sequence_lifecycle_transition(with_residual, handoff)


def test_finalization_residual_risk_requires_final_ordinal_even_when_unchanged(
    git_repo,
    scheduler_paths,
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    final_ordinal = len(active.definition.entries)
    final_materialized = MaterializedSequenceEntry(
        ordinal=final_ordinal,
        run_id=start.run_id,
        entry_hash=active.materialized_entries[0].entry_hash,
        materialized_at=active.updated_at,
    )
    materialized_both = (active.materialized_entries[0], final_materialized)
    active_at_final = active.model_copy(
        update={"current_ordinal": final_ordinal, "materialized_entries": materialized_both}
    )
    unchanged_residual = AwaitingFinalizationSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=active.updated_at,
        started_at=active.started_at,
        finalized_at=active.updated_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=start.run_id,
        final_outcome="completed_with_residual_risk",
        materialized_entries=materialized_both,
        residual_risk_ordinals=active_at_final.residual_risk_ordinals,
    )
    with pytest.raises(
        SequenceLifecycleValidationError,
        match="must include final ordinal",
    ):
        validate_sequence_lifecycle_transition(active_at_final, unchanged_residual)


def test_blocked_to_active_requires_successor_run(
    git_repo,
    scheduler_paths,
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    blocked = BlockedSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at=active.updated_at,
        started_at=active.started_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        current_ordinal=active.current_ordinal,
        current_run_id=start.run_id,
        materialized_entries=active.materialized_entries,
        block_reason_kind="codex_usage_limit",
        blocked_at=active.updated_at,
    )
    unchanged = ActiveSequenceState(
        schema_version=blocked.schema_version,
        sequence_id=blocked.sequence_id,
        version=blocked.version + 1,
        prepared_at=blocked.prepared_at,
        updated_at=blocked.updated_at,
        started_at=blocked.started_at,
        idempotency_key=blocked.idempotency_key,
        definition=blocked.definition,
        current_ordinal=blocked.current_ordinal,
        current_run_id=blocked.current_run_id,
        materialized_entries=blocked.materialized_entries,
    )
    with pytest.raises(
        SequenceLifecycleValidationError, match="advance the materialized current run"
    ):
        validate_sequence_lifecycle_transition(blocked, unchanged)
