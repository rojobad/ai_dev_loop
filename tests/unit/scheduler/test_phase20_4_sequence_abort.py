"""Unit tests for Phase 20.4 sequence abort coordinator."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import (
    ABORT_PENDING_SEQUENCE_STATE_KIND,
    ABORTED_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepared_abort_is_idempotent(git_repo: Path, scheduler_paths: dict[str, Path]) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    service = SequenceAbortService(store, now_factory=lambda: FIXED_NOW)
    first = service.abort_sequence(sequence_id)
    second = service.abort_sequence(sequence_id)
    assert first.state_kind == ABORTED_SEQUENCE_STATE_KIND
    assert second.idempotent_replay is True


def test_active_abort_persists_abort_pending_then_aborted_on_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    abort = SequenceAbortService(store, now_factory=lambda: FIXED_NOW)
    result = abort.abort_sequence(sequence_id)
    assert result.state_kind in {ABORT_PENDING_SEQUENCE_STATE_KIND, ABORTED_SEQUENCE_STATE_KIND}
    with store.begin_read() as conn:
        state = store.load_validated_sequence_state(conn, sequence_id)
    if isinstance(state, AbortedSequenceState):
        assert len(state.cancelled_ordinals) == 1
        return
    assert isinstance(state, AbortPendingSequenceState)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(20):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, sequence_id)
            run_state, _, _ = store.load_validated_snapshot(conn, start.run_id)
            if isinstance(sequence, AbortedSequenceState):
                break
            if run_state.kind == "aborted":
                with store.begin_immediate() as inner:
                    from ai_dev_loop.scheduler.application.sequence_reconcile import (
                        SequenceReconcileService,
                    )

                    SequenceReconcileService(store).reconcile_run(inner, start.run_id)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        reservation = conn.execute(
            "SELECT status FROM scheduler_repository_reservations"
        ).fetchone()
    assert isinstance(sequence, AbortedSequenceState)
    assert len(sequence.cancelled_ordinals) == 1
    assert reservation is not None
    assert reservation[0] == "released"


def test_abort_pending_replays_run_abort_after_intent_commit_without_duplicate_event(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
    from ai_dev_loop.scheduler.domain.events import SequenceAbortRequestedEvent
    from ai_dev_loop.scheduler.domain.sequence import future_entry_ordinals

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState

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
        current_run_id=start.run_id,
        materialized_entries=active.materialized_entries,
        residual_risk_ordinals=active.residual_risk_ordinals,
        cancelled_ordinals=future_entry_ordinals(active.definition, active.current_ordinal),
    )
    abort_request = SequenceAbortRequestedEvent(
        sequence_id=sequence_id,
        reason="user_requested_abort",
        current_run_id=start.run_id,
    )
    with store.begin_immediate() as conn:
        event_id = "evt-abort-intent"
        store.append_event(
            conn,
            event_id=event_id,
            run_id=start.run_id,
            sequence=store.next_event_sequence(conn, start.run_id),
            event=abort_request,
            now=FIXED_NOW,
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=pending,
            now=FIXED_NOW,
        )
    run_abort = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=artifacts,
        now_factory=lambda: FIXED_NOW,
    )
    service = SequenceAbortService(store, run_abort=run_abort, now_factory=lambda: FIXED_NOW)
    service.continue_pending_abort_for_run(start.run_id)
    service.continue_pending_abort_for_run(start.run_id)
    with store.begin_read() as conn:
        run_state, _, _ = store.load_validated_snapshot(conn, start.run_id)
        event_count = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id = ? AND event_kind = 'sequence_abort_requested'
            """,
            (start.run_id,),
        ).fetchone()
    assert run_state.kind == "aborted"
    assert int(event_count[0]) == 1


def test_abort_refuses_awaiting_finalization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.sequence import (
        AwaitingFinalizationSequenceState,
        MaterializedSequenceEntry,
    )

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    start = _start_service(scheduler_paths).start(sequence_id)
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState

    assert isinstance(active, ActiveSequenceState)
    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    entry_hash = frozen_entry_hash(active.definition.entries[0])
    final_entry_hash = frozen_entry_hash(active.definition.entries[1])
    materialized_entries = (
        MaterializedSequenceEntry(
            ordinal=1,
            run_id=start.run_id,
            entry_hash=entry_hash,
            materialized_at=active.materialized_entries[0].materialized_at,
        ),
        MaterializedSequenceEntry(
            ordinal=2,
            run_id=active.definition.entries[1].planned_run_id,
            entry_hash=final_entry_hash,
            materialized_at="2026-09-13T12:00:00.000000Z",
        ),
    )
    final_active = ActiveSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at="2026-09-13T12:00:00.000000Z",
        started_at=active.started_at,
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        current_ordinal=2,
        current_run_id=active.definition.entries[1].planned_run_id,
        materialized_entries=materialized_entries,
        residual_risk_ordinals=active.residual_risk_ordinals,
    )
    finalized = AwaitingFinalizationSequenceState(
        schema_version=1,
        sequence_id=sequence_id,
        version=final_active.version + 1,
        prepared_at=active.prepared_at,
        updated_at="2026-09-13T12:00:00.000000Z",
        started_at=active.started_at,
        finalized_at="2026-09-13T12:00:00.000000Z",
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        final_run_id=active.definition.entries[1].planned_run_id,
        final_outcome="completed",
        materialized_entries=materialized_entries,
    )
    from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
        sync_authoritative_lineage_from_state,
    )

    with store.begin_immediate() as conn:
        assert store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=final_active,
            now=FIXED_NOW,
        )
        sync_authoritative_lineage_from_state(store, conn, final_active)
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=final_active.version,
            new_state=finalized,
            now=FIXED_NOW,
        )
        sync_authoritative_lineage_from_state(store, conn, finalized)
    service = SequenceAbortService(store, now_factory=lambda: FIXED_NOW)
    with pytest.raises(SchedulerEngineError, match="awaiting finalization"):
        service.abort_sequence(sequence_id)
