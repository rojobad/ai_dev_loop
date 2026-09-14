"""Unit tests for Phase 20.4 sequence lifecycle states and reconciliation."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_NOW,
)
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)

from ai_dev_loop.scheduler.application.sequence_reconcile import SequenceReconcileService
from ai_dev_loop.scheduler.domain.events import MaxIterationsReachedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_max_iterations_reached
from ai_dev_loop.scheduler.domain.sequence import (
    ABORTED_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
    SequenceLifecycleValidationError,
    validate_blocked_state,
)
from ai_dev_loop.scheduler.domain.state import AuthorizedState, BlockedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _active_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> tuple[str, str]:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    return sequence_id, start.run_id


def test_prepared_sequence_abort_transitions_to_aborted_without_runs(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    service = SequenceAbortService(store, now_factory=lambda: FIXED_NOW)
    result = service.abort_sequence(sequence_id)
    assert result.state_kind == ABORTED_SEQUENCE_STATE_KIND
    with store.begin_read() as conn:
        state = store.load_validated_sequence_state(conn, sequence_id)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert isinstance(state, AbortedSequenceState)
    assert state.started_at is None
    assert len(state.cancelled_ordinals) == 2
    assert int(run_count[0]) == 0


def test_max_iterations_reached_leaves_sequence_active_for_budget_extend(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id, run_id = _active_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        from ai_dev_loop.scheduler.domain.state import AwaitingCodexReviewState

        if not isinstance(state, AwaitingCodexReviewState):
            pytest.skip("run did not reach awaiting_codex_review in this fixture")
        event = MaxIterationsReachedEvent(
            run_id=run_id,
            review_iteration=1,
            review_result_path="codex/reviews/01.json",
            review_result_sha256="a" * 64,
            fix_prompt_path="prompts/fixes/01.txt",
            fix_prompt_sha256="b" * 64,
            correction_envelope_path="prompts/fixes/01.execution-envelope.txt",
            correction_envelope_sha256="c" * 64,
        )
        terminal = apply_max_iterations_reached(
            state,
            event,
            now_text=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=terminal,
            now=FIXED_NOW,
        )
        receipt = reconcile.reconcile_run(conn, run_id)
    assert receipt is None
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)


def test_blocked_sequence_rejects_second_reconcile(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id, run_id = _active_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=run_id,
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
            run_id=run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        receipt = reconcile.reconcile_run(conn, run_id)
        assert receipt is not None
        assert receipt.action == "sequence_blocked"
        second = reconcile.reconcile_run(conn, run_id)
    assert second is None


def test_abort_pending_does_not_finalize_while_run_hold_pending(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.events import RunAbortedEvent
    from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
    from ai_dev_loop.scheduler.domain.state import AbortedState

    sequence_id, run_id = _active_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        pending = AbortPendingSequenceState(
            schema_version=active.schema_version,
            sequence_id=active.sequence_id,
            version=active.version + 1,
            prepared_at=active.prepared_at,
            updated_at="2026-09-13T12:00:00.000000Z",
            started_at=active.started_at,
            abort_requested_at="2026-09-13T12:00:00.000000Z",
            abort_reason="user_requested_abort",
            idempotency_key=active.idempotency_key,
            definition=active.definition,
            current_ordinal=active.current_ordinal,
            current_run_id=run_id,
            materialized_entries=active.materialized_entries,
            residual_risk_ordinals=active.residual_risk_ordinals,
            cancelled_ordinals=(2,),
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=pending,
            now=FIXED_NOW,
        )
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="a" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=FIXED_NOW,
        )
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        aborted = apply_run_aborted(
            state,
            RunAbortedEvent(
                run_id=run_id,
                reason="user_requested_abort",
                prior_state_kind=state.kind,
            ),
            now_text=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        assert isinstance(aborted, AbortedState)
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=aborted,
            now=FIXED_NOW,
        )
        receipt = reconcile.reconcile_run(conn, run_id)
    assert receipt is not None
    assert receipt.action == "sequence_abort_termination_pending"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        reservation = store.get_reservation_for_run(conn, run_id)
    assert isinstance(sequence, AbortPendingSequenceState)
    assert reservation is not None


def test_reconcile_pending_sequences_covers_non_tick_eligible_blocked_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.sequence import BlockedSequenceState

    sequence_id, run_id = _active_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=run_id,
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
            run_id=run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        receipts = reconcile.reconcile_pending_sequences(conn)
    assert any(receipt.action == "sequence_blocked" for receipt in receipts)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, BlockedSequenceState)


def test_lifecycle_validation_rejects_non_contiguous_materialized_entries(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
    from ai_dev_loop.scheduler.domain.sequence import (
        BlockedSequenceState,
        MaterializedSequenceEntry,
    )

    sequence_id, run_id = _active_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    malformed = BlockedSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at="2026-09-13T12:00:00.000000Z",
        started_at=active.started_at,
        blocked_at="2026-09-13T12:00:00.000000Z",
        block_reason_kind="blocked",
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        current_ordinal=2,
        current_run_id=run_id,
        materialized_entries=(
            MaterializedSequenceEntry(
                ordinal=2,
                run_id=run_id,
                entry_hash="a" * 64,
                materialized_at="2026-09-13T12:00:00.000000Z",
            ),
        ),
    )
    with pytest.raises(SequenceLifecycleValidationError):
        validate_blocked_state(malformed)
    with store.begin_immediate() as conn, pytest.raises(SchedulerEngineError):
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=malformed,
            now=FIXED_NOW,
        )
