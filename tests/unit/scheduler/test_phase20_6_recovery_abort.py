"""Abort tests for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.runners.git import checkpoint_git_rev_parse
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.recovery_abort import (
    RecoveryAbortService,
    abort_recovery,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import load_recovery_definition
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_start import RecoveryStartService
from ai_dev_loop.scheduler.application.recovery_worktree import FakeRecoveryWorktreePort
from ai_dev_loop.scheduler.domain.recovery import (
    ABORTED_RECOVERY_STATE_KIND,
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    INTEGRATED_RECOVERY_STATE_KIND,
    PREPARED_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    CleanupPendingRecoveryState,
    PreparedRecoveryState,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_abort_prepared_recovery(
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
        commit_message="abort test commit",
    )
    result = RecoveryAbortService(store).abort(prepared.recovery_id)
    assert result.state_kind == ABORTED_RECOVERY_STATE_KIND
    assert result.changed is True


def test_abort_cleanup_pending_preserves_integrated_commit(
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
        commit_message="integrated abort commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert row is not None
    assert str(row["state_kind"]) == PREPARED_RECOVERY_STATE_KIND
    prepared_state = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
    integrated_sha = checkpoint_git_rev_parse(git_repo, "HEAD")
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
    from ai_dev_loop.scheduler.domain.recovery import RECOVERY_SEED_EVIDENCE_ARTIFACT

    with store.begin_read() as conn:
        definition = load_recovery_definition(
            artifacts,
            prepared.recovery_id,
            definition_sha256=prepared_state.definition_sha256,
            definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        )
    seed = RecoverySeedEvidence(
        parent_head=definition.parent_head,
        staged_patch_sha256=definition.source_staged_patch_sha256,
        staged_tree_sha256=definition.source_staged_tree_sha256,
        worktree_path=str(git_repo / "missing-managed-wt"),
        private_ref=definition.private_ref,
        target_repository_root=definition.repository.root,
        managed_git_common_dir=definition.repository.git_common_dir,
        managed_git_dir=definition.repository.git_dir,
        managed_branch="main",
        pre_seed_admission_status="",
    )
    stored_seed = artifacts.write_bytes(
        prepared.recovery_id,
        RECOVERY_SEED_EVIDENCE_ARTIFACT,
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    cleanup = CleanupPendingRecoveryState(
        recovery_id=prepared.recovery_id,
        version=prepared_state.version + 1,
        updated_at="2026-09-15T12:00:00.000000Z",
        definition_sha256=prepared_state.definition_sha256,
        definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=prepared_state.source_run_id_prefix,
        recovery_run_id="run-recovery-abort",
        sequence=prepared_state.sequence,
        started_at="2026-09-15T12:00:00.000000Z",
        accepted_outcome="completed",
        residual_risk=False,
        integrated_commit_sha256=integrated_sha,
        integrated_commit_sha256_prefix=integrated_sha[:8],
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    from datetime import UTC, datetime

    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.update_fresh_review_recovery(
            conn,
            recovery_id=prepared.recovery_id,
            state=cleanup,
            expected_version=prepared_state.version,
            now=now,
        )
    result = RecoveryAbortService(store, artifacts).abort(prepared.recovery_id)
    assert result.state_kind == INTEGRATED_RECOVERY_STATE_KIND
    assert result.changed is True
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert str(row["state_kind"]) == INTEGRATED_RECOVERY_STATE_KIND


def test_abort_cleanup_failure_preserves_cleanup_pending_for_retry(
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
        commit_message="cleanup retry commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    prepared_state = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
    integrated_sha = checkpoint_git_rev_parse(git_repo, "HEAD")
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
    from ai_dev_loop.scheduler.domain.recovery import RECOVERY_SEED_EVIDENCE_ARTIFACT

    with store.begin_read() as conn:
        definition = load_recovery_definition(
            artifacts,
            prepared.recovery_id,
            definition_sha256=prepared_state.definition_sha256,
            definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        )
    seed = RecoverySeedEvidence(
        parent_head=definition.parent_head,
        staged_patch_sha256=definition.source_staged_patch_sha256,
        staged_tree_sha256=definition.source_staged_tree_sha256,
        worktree_path=str(git_repo / "missing-managed-wt"),
        private_ref=definition.private_ref,
        target_repository_root=definition.repository.root,
        managed_git_common_dir=definition.repository.git_common_dir,
        managed_git_dir=definition.repository.git_dir,
        managed_branch="main",
        pre_seed_admission_status="",
    )
    stored_seed = artifacts.write_bytes(
        prepared.recovery_id,
        RECOVERY_SEED_EVIDENCE_ARTIFACT,
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    cleanup = CleanupPendingRecoveryState(
        recovery_id=prepared.recovery_id,
        version=prepared_state.version + 1,
        updated_at="2026-09-15T12:00:00.000000Z",
        definition_sha256=prepared_state.definition_sha256,
        definition_artifact_sha256=prepared_state.definition_artifact_sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=prepared_state.source_run_id_prefix,
        recovery_run_id="run-recovery-abort",
        sequence=prepared_state.sequence,
        started_at="2026-09-15T12:00:00.000000Z",
        accepted_outcome="completed",
        residual_risk=False,
        integrated_commit_sha256=integrated_sha,
        integrated_commit_sha256_prefix=integrated_sha[:8],
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    from datetime import UTC, datetime

    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.update_fresh_review_recovery(
            conn,
            recovery_id=prepared.recovery_id,
            state=cleanup,
            expected_version=prepared_state.version,
            now=now,
        )

    def fail_cleanup(self: RecoveryReconcileService, recovery_id: str) -> TickRunReceipt:
        return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_failed")

    monkeypatch.setattr(RecoveryReconcileService, "cleanup_deferred", fail_cleanup)
    result = RecoveryAbortService(store, artifacts).abort(prepared.recovery_id)
    assert result.state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND
    assert result.changed is False
    assert checkpoint_git_rev_parse(git_repo, "HEAD") == integrated_sha
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert str(row["state_kind"]) == CLEANUP_PENDING_RECOVERY_STATE_KIND

    monkeypatch.undo()
    RecoveryReconcileService(store, artifacts).cleanup_deferred(prepared.recovery_id)
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    assert str(row["state_kind"]) == INTEGRATED_RECOVERY_STATE_KIND


def test_abort_recovery_public_entry_writes_abort_intent(
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
        commit_message="public abort commit",
    )
    RecoveryStartService(store, artifacts, worktree_port=FakeRecoveryWorktreePort()).start(
        prepared.recovery_id
    )
    abort_recovery(
        prepared.recovery_id,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    abort_path = artifacts.run_root(prepared.recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
    assert abort_path.is_file()
