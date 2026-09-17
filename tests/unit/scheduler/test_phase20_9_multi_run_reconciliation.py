"""Phase 20.9 sequence reconciliation discovery."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.phase20_9_test_helpers import completed_state_from_authorized
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.scheduler.application.sequence_reconcile import SequenceReconcileService
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXED_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_list_sequence_terminal_current_run_ids_includes_completed_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.state import AuthorizedState

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    now_text = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        completed = completed_state_from_authorized(
            state,
            version=version + 1,
            updated_at=now_text,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=completed,
            now=FIXED_NOW,
        )
        terminal_ids = store.list_sequence_terminal_current_run_ids(conn)
    assert start.run_id in terminal_ids


def test_reconcile_pending_sequences_still_covers_non_tick_eligible_blocked_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.sequence import BlockedSequenceState
    from ai_dev_loop.scheduler.domain.state import AuthorizedState, BlockedState

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
        tick_eligible = store.list_tick_eligible_run_ids(conn)
        assert start.run_id not in tick_eligible
        receipts = reconcile.reconcile_pending_sequences(conn)
    assert any(receipt.action == "sequence_blocked" for receipt in receipts)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, BlockedSequenceState)
