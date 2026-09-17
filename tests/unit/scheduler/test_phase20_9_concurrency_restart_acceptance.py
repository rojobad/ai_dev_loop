"""Phase 20.9 explicit-barrier concurrency and restart fault acceptance."""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import MethodType
from unittest.mock import patch

import pytest
from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (
    CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
    _persist_active_sequence_for_fixture,
    _prepare_handoff_reconcile,
    _reconcile_checkpoint,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry_concurrency import (
    _run_concurrent_retry_with_materialize_gate,
)
from tests.unit.scheduler.test_phase20_9_terminal_replay_bindings import (
    FIXED_NOW,
    _terminal_replay_control_fixture,
    _tick_service_for_terminal_fixture,
)

from ai_dev_loop.scheduler.application import sequence_review_recovery
from ai_dev_loop.scheduler.application.abort import default_abort_service
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.application.sequence_materializer import (
    build_sequence_run_context,
    frozen_entry_hash,
)
from ai_dev_loop.scheduler.domain.checkpoint import SequenceCheckpointIntent
from ai_dev_loop.scheduler.domain.sequence import (
    ABORTED_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import BlockedState, CompletedState
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _race_abort_and_retry_at_materialize_gate(
    *,
    sequence_id: str,
    source_run_id: str,
    retry_service: ReviewRetryService,
    abort_service: SequenceAbortService,
    tick,
    store: SqliteSchedulerStore,
    abort_wins: bool,
) -> tuple[object, list[SchedulerEngineError], list[str]]:
    original_materialize = sequence_review_recovery._materialize_pending_successor
    slow_at_materialize = threading.Event()
    release_materialize = threading.Event()
    worker_errors: list[BaseException] = []
    retry_errors: list[SchedulerEngineError] = []
    successor_ids: list[str] = []
    abort_result: object | None = None
    timeout_s = 120.0

    def gated_materialize(*args, **kwargs):  # type: ignore[no-untyped-def]
        if threading.current_thread().name == "slow-retry":
            slow_at_materialize.set()
            release_materialize.wait(timeout=timeout_s)
        return original_materialize(*args, **kwargs)

    def _retry_worker() -> None:
        try:
            outcome = retry_service.retry(source_run_id)
            successor_ids.append(outcome.run_id)
        except SchedulerEngineError as exc:
            retry_errors.append(exc)
        except BaseException as exc:  # noqa: BLE001
            worker_errors.append(exc)

    def _abort_worker() -> None:
        nonlocal abort_result
        try:
            abort_result = abort_service.abort_sequence(sequence_id)
        except BaseException as exc:  # noqa: BLE001
            worker_errors.append(exc)

    with patch.object(
        sequence_review_recovery,
        "_materialize_pending_successor",
        gated_materialize,
    ):
        slow = threading.Thread(target=_retry_worker, name="slow-retry")
        abort_thread = threading.Thread(target=_abort_worker, name="abort-worker")
        slow.start()
        assert slow_at_materialize.wait(timeout=timeout_s), (
            "timed out waiting for retry to reach materialization boundary"
        )
        if abort_wins:
            abort_thread.start()
            abort_thread.join(timeout=timeout_s)
            release_materialize.set()
            slow.join(timeout=timeout_s)
        else:
            release_materialize.set()
            slow.join(timeout=timeout_s)
            abort_thread.start()
            abort_thread.join(timeout=timeout_s)
        assert not worker_errors, worker_errors
        assert not slow.is_alive()
        assert not abort_thread.is_alive()
    assert abort_result is not None
    return abort_result, retry_errors, successor_ids


def _assert_aborted_sequence_terminal(
    store: SqliteSchedulerStore,
    tick,
    sequence_id: str,
    *,
    source_run_id: str,
) -> AbortedSequenceState:
    for _ in range(40):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, sequence_id)
            if isinstance(sequence, AbortedSequenceState):
                lineage = load_sequence_run_lineage(conn, sequence_id)
                replacement_rows = conn.execute(
                    """
                    SELECT successor_run_id, cancelled_at
                    FROM scheduler_sequence_execution_replacements
                    WHERE source_run_id = ?
                    """,
                    (source_run_id,),
                ).fetchall()
                assert not isinstance(sequence, (ActiveSequenceState, BlockedSequenceState))
                assert len(replacement_rows) <= 1
                assert len(lineage.phase_executions) >= 1
                return sequence
    raise AssertionError(f"sequence {sequence_id} did not reach aborted terminal state")


@pytest.mark.parametrize("abort_wins", [True, False])
def test_abort_race_with_review_retry_preserves_single_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    abort_wins: bool,
) -> None:
    from ai_dev_loop.scheduler.application.contracts import SequenceAbortResult

    sequence_id, source_run_id, tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    store.busy_timeout_ms = 120_000
    retry_service = ReviewRetryService(store, artifacts, now_factory=lambda: FIXED_NOW)
    abort_service = SequenceAbortService(store, now_factory=lambda: FIXED_NOW)
    abort_result, retry_errors, successor_ids = _race_abort_and_retry_at_materialize_gate(
        sequence_id=sequence_id,
        source_run_id=source_run_id,
        retry_service=retry_service,
        abort_service=abort_service,
        tick=tick,
        store=store,
        abort_wins=abort_wins,
    )
    assert isinstance(abort_result, SequenceAbortResult)
    assert abort_result.abort_persisted
    assert not abort_result.idempotent_replay
    assert abort_result.state_kind == ABORTED_SEQUENCE_STATE_KIND
    assert len(successor_ids) <= 1
    if successor_ids:
        assert len(set(successor_ids)) == 1
    if abort_wins:
        assert retry_errors, "abort-winning race should surface retry conflict at adoption"
        assert all(exc.kind == SchedulerEngineErrorKind.CONFLICT for exc in retry_errors)
        assert any(
            "abort" in str(exc).lower() or "cancelled" in str(exc).lower() for exc in retry_errors
        )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        rows = conn.execute(
            """
            SELECT successor_run_id
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchall()
    assert len(rows) <= 1
    assert not isinstance(sequence, (ActiveSequenceState, BlockedSequenceState))
    assert isinstance(sequence, (AbortPendingSequenceState, AbortedSequenceState))
    final_sequence = _assert_aborted_sequence_terminal(
        store, tick, sequence_id, source_run_id=source_run_id
    )
    assert final_sequence.abort_reason == "user_requested_abort"
    if abort_wins:
        with store.begin_read() as conn:
            source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
            assert isinstance(source_state, BlockedState)
            assert final_sequence.preserved_current_leaf_terminal == "blocked"
    else:
        assert final_sequence.current_run_id != source_run_id
        with store.begin_read() as conn:
            rows = conn.execute(
                """
                SELECT successor_run_id
                FROM scheduler_sequence_execution_replacements
                WHERE source_run_id = ?
                """,
                (source_run_id,),
            ).fetchall()
        assert len(rows) == 1


def test_abort_after_checkpoint_commit_before_handoff_preserves_hold(
    tmp_path: Path,
) -> None:
    fixture = _prepare_handoff_reconcile(tmp_path)
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    parent_head = str(fixture["parent_head"])
    _reconcile_checkpoint(fixture, stop_before_handoff=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert head != parent_head
    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert str(hold["hold_reason"]) in {
            CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
            "checkpoint_ref_advanced",
        }
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
    tick = _tick_service_for_terminal_fixture(fixture)
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    assert not result.idempotent_replay
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head
    )
    with store.begin_read() as conn:
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
    for _ in range(20):
        tick.run_once()
        with store.begin_read() as conn:
            run_state, _, _ = store.load_validated_snapshot(conn, run_id)
            if run_state.kind == "aborted":
                break
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head
    )
    with store.begin_read() as conn:
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert run_state.kind == "aborted"
        intent = fixture["intent"]
        assert isinstance(intent, SequenceCheckpointIntent)
        successor_exists = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchone()
    assert successor_exists is None


def test_competing_terminal_replay_threads_single_handoff_outcome(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    handoff = fixture["service"]
    assert isinstance(handoff, SequenceHandoffService)
    active_sequence = fixture["active_sequence"]
    assert isinstance(active_sequence, ActiveSequenceState)
    _persist_active_sequence_for_fixture(store, active_sequence, now=FIXED_NOW)
    original_load = fixture["load_sequence_state_original"]
    store.load_validated_sequence_state = original_load  # type: ignore[method-assign]
    with store.begin_read() as conn:
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
    assert isinstance(run_state, CompletedState)

    def _stub_materialize_next_entry(
        self: object,
        *,
        sequence_id: str,
        definition: object,
        entry: object,
    ) -> tuple[object, str]:
        from ai_dev_loop.scheduler.domain.sequence import (
            FrozenSequenceEntry,
            PreparedSequenceDefinition,
        )

        assert isinstance(definition, PreparedSequenceDefinition)
        assert isinstance(entry, FrozenSequenceEntry)
        entry_hash = frozen_entry_hash(entry)
        context = build_sequence_run_context(
            definition=definition,
            entry=entry,
            entry_hash=entry_hash,
        )
        return context, entry_hash

    start_barrier = threading.Barrier(2)
    receipts: list[str] = []
    errors: list[BaseException] = []

    def _worker() -> None:
        try:
            start_barrier.wait(timeout=30)
            with store.begin_immediate() as conn:
                sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
                if not isinstance(sequence_state, ActiveSequenceState):
                    receipts.append("sequence_not_active")
                    return
                if sequence_state.current_run_id != run_id:
                    receipts.append("sequence_handoff_complete")
                    return
                leaf_state, _, _ = store.load_validated_snapshot(conn, run_id)
                if not isinstance(leaf_state, CompletedState):
                    receipts.append("sequence_leaf_not_terminal")
                    return
                receipt = handoff.reconcile_interrupted_sequence_advancement(
                    conn,
                    sequence_state=sequence_state,
                    run_state=leaf_state,
                    now=FIXED_NOW,
                )
                receipts.append(receipt.action)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    materializer = handoff._materializer
    original_materialize = materializer.materialize_next_entry
    materializer.materialize_next_entry = MethodType(_stub_materialize_next_entry, materializer)
    try:
        threads = [threading.Thread(target=_worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        materializer.materialize_next_entry = original_materialize
    assert not errors, errors
    assert len(receipts) == 2
    reconciled = receipts.count("sequence_handoff_reconciled")
    idempotent = receipts.count("sequence_handoff_complete")
    assert reconciled == 1
    assert reconciled + idempotent >= 1
    assert len(receipts) == 2
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, intent.sequence_id)
        successor_rows = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchall()
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == intent.successor_run_id
    assert len(successor_rows) == 1


def test_barrier_retry_adoption_with_tick_advances_without_duplicate_successors(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    store.busy_timeout_ms = 120_000
    service = ReviewRetryService(
        store,
        artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 15, 0, tzinfo=UTC),
    )
    results, _changed_flags, errors = _run_concurrent_retry_with_materialize_gate(
        source_run_id=source_run_id,
        service=service,
        tick=tick,
        store=store,
        advance_successor="waiting",
    )
    assert not errors, errors
    assert len(set(results)) == 1
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT successor_run_id
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchall()
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert len(rows) == 1
    assert isinstance(sequence, ActiveSequenceState)


def test_sequence_abort_terminalizes_without_duplicate_git_effects(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    head_before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    result = SequenceAbortService(store, now_factory=lambda: FIXED_NOW).abort_sequence(sequence_id)
    assert result.state_kind == ABORTED_SEQUENCE_STATE_KIND
    head_after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=git_repo, text=True
    ).strip()
    assert head_before == head_after
