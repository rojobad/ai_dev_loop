"""Partial-start reconciliation tests for Phase 20.6."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    persist_recovery_start_intent,
    recovery_run_id_for,
)
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.application.recovery_start import RecoveryStartService
from ai_dev_loop.scheduler.application.recovery_worktree import FakeRecoveryWorktreePort
from ai_dev_loop.scheduler.domain.recovery import (
    ACTIVE_RECOVERY_STATE_KIND,
    PREPARED_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    PreparedRecoveryState,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_start_resumes_after_persisted_intent_only(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="partial start commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert row is not None
    assert str(row["state_kind"]) == PREPARED_RECOVERY_STATE_KIND
    prepared_state = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    persist_recovery_start_intent(
        artifacts,
        prepared.recovery_id,
        definition_sha256=prepared_state.definition_sha256,
        definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        recovery_run_id=recovery_run_id_for(prepared.recovery_id),
        requested_at=now_text,
    )
    fake_worktree = FakeRecoveryWorktreePort()
    result = RecoveryStartService(store, artifacts, worktree_port=fake_worktree).start(
        prepared.recovery_id
    )
    assert result.changed is True
    assert result.state_kind == ACTIVE_RECOVERY_STATE_KIND
    assert len(fake_worktree.created) == 1


def test_start_rejects_continuation_when_abort_intent_exists(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="abort fence commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    prepared_state = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    persist_recovery_start_intent(
        artifacts,
        prepared.recovery_id,
        definition_sha256=prepared_state.definition_sha256,
        definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        recovery_run_id=recovery_run_id_for(prepared.recovery_id),
        requested_at=now_text,
    )
    artifacts.write_bytes(
        prepared.recovery_id,
        RECOVERY_ABORT_INTENT_ARTIFACT,
        b"recovery_id=abort\n",
        max_bytes=64,
    )
    with pytest.raises(SchedulerEngineError, match="abort intent blocks"):
        RecoveryStartService(store, artifacts, worktree_port=FakeRecoveryWorktreePort()).start(
            prepared.recovery_id
        )
