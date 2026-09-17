"""Restart reconciliation for interrupted finalization and recovery adoption."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
    _run_until,
    _tick_service,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_RUN_IDS
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.application.sequence_restart_reconcile import (
    SequenceRestartReconcileService,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXED_NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_restart_reconcile_finalizes_active_sequence_with_completed_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    finalize_calls = 0
    original_finalize = SqliteSchedulerStore.finalize_sequence_state

    def flaky_finalize(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return None
        return original_finalize(self, conn, **kwargs)

    monkeypatch.setattr(SqliteSchedulerStore, "finalize_sequence_state", flaky_finalize)
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = start_sequence(sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, FIXED_RUN_IDS[1], target_kind="completed")
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    handoff = SequenceHandoffService(store, artifacts, now_factory=lambda: FIXED_NOW)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    final_run_id = sequence.current_run_id
    service = SequenceRestartReconcileService(
        store, artifacts, handoff=handoff, now_factory=lambda: FIXED_NOW
    )
    with store.begin_immediate() as conn:
        receipt = service.reconcile_terminal_current_leaf(conn, final_run_id)
    assert receipt is not None
    assert receipt.action == "sequence_finalization_reconciled"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AwaitingFinalizationSequenceState)


def test_restart_reconcile_adopts_pending_successor_after_simulated_crash(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    adopt_calls = 0
    real_adopt = __import__(
        "ai_dev_loop.scheduler.application.sequence_review_recovery",
        fromlist=["_adopt_sequence_recovery_successor"],
    )._adopt_sequence_recovery_successor

    def _adopt_once_then_fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal adopt_calls
        adopt_calls += 1
        if adopt_calls == 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "simulated crash before sequence adoption",
            )
        return real_adopt(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.sequence_review_recovery._adopt_sequence_recovery_successor",
        _adopt_once_then_fail,
    )
    service = ReviewRetryService(store, artifacts)
    with pytest.raises(SchedulerEngineError):
        service.retry(source_run_id)
    restart = SequenceRestartReconcileService(store, artifacts, now_factory=lambda: FIXED_NOW)
    with store.begin_read() as conn:
        pending = store.list_pending_sequence_review_recovery_run_ids(conn)
    assert len(pending) == 1
    with store.begin_immediate() as conn:
        receipt = restart.reconcile_pending_review_recovery_successor(conn, pending[0])
    assert receipt is not None
    assert receipt.action == "sequence_recovery_adoption_reconciled"
    with store.begin_read() as conn:
        successor_state, _, _ = store.load_validated_snapshot(conn, pending[0])
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == pending[0]
    assert successor_state.kind == "awaiting_codex_review"
