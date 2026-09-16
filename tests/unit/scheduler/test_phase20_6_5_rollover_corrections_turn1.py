"""Crash and reconciliation tests for Phase 20.6.5 rollover integration."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _init_repo,
    _reacquire_target_reservation,
)

from ai_dev_loop.runners.git import (
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_write_tree,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
)
from ai_dev_loop.scheduler.application.rollover_integration import (
    RolloverIntegrationService,
    set_rollover_integration_publication_step_hook,
)
from ai_dev_loop.scheduler.application.rollover_integration_fencing import (
    RolloverIntegrationTickContext,
)
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.rollover import (
    RolloverCheckpointTrustedTree,
    RolloverIntegrationIntent,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _identity(now_text: str) -> GitIdentitySnapshot:
    return GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )


def test_standalone_cas_reconcile_releases_hold_after_crash(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    source_run_id = "run-source-rollover-cas"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    _reacquire_target_reservation(
        store,
        worktree_key="wt-rollover-cas",
        run_id=source_run_id,
        repo_root=repo,
    )
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity_snapshot = _identity(now_text)
    identity = GitIdentity(
        author_name=identity_snapshot.author_name,
        author_email=identity_snapshot.author_email,
        author_date=identity_snapshot.author_date,
        committer_name=identity_snapshot.committer_name,
        committer_email=identity_snapshot.committer_email,
        committer_date=identity_snapshot.committer_date,
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message="rollover cas reconcile commit",
        identity=identity,
    )
    intent = RolloverIntegrationIntent(
        rollover_id="rol-" + ("i" * 32),
        source_run_id=source_run_id,
        rollover_run_id="run-rollover",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        review_result_artifact_path="codex/reviews/01.json",
        review_result_sha256="1" * 64,
        commit_message="rollover cas reconcile commit",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/rollover/" + ("a" * 64),
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        git_identity=identity_snapshot,
        recorded_at=now_text,
    )
    trusted = RolloverCheckpointTrustedTree(
        intent_sha256="b" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        parent_head=head,
        recorded_at=now_text,
    )
    service = RolloverIntegrationService(store, artifacts, now_factory=lambda: now)
    release_calls = 0
    original_release = store.release_checkpoint_reconciliation_hold

    def release_once_then_real(
        conn: object,
        *,
        run_id: str,
        intent_sha256: str,
    ) -> None:
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            return
        original_release(conn, run_id=run_id, intent_sha256=intent_sha256)

    monkeypatch.setattr(store, "release_checkpoint_reconciliation_hold", release_once_then_real)
    evidence = {"tree_sha256": tree, "commit_sha256": commit_sha}
    first = service._integrate_target_standalone(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        patch_path=repo / "missing.patch",
        target_root=repo,
        identity=identity,
        source_run_id=source_run_id,
        target_worktree_key="wt-rollover-cas",
        evidence=evidence,
        rollover_version=1,
    )
    assert first == commit_sha
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    reconciled = service._reconcile_standalone_target_cas_if_proven(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        target_root=repo,
        source_run_id=source_run_id,
        evidence=evidence,
    )
    assert reconciled == commit_sha
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)


def test_publication_crash_after_intent_write_before_binding_retries_safely(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    rollover_id = "rol-" + ("p" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    from ai_dev_loop.scheduler.domain.rollover import IntegrationPendingRolloverState

    pending = IntegrationPendingRolloverState(
        rollover_id=rollover_id,
        version=1,
        updated_at=now_text,
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        rollover_run_id="run-rollover",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
    )
    with store.begin_immediate() as conn:
        store.insert_authenticated_rollover(
            conn,
            rollover_id=rollover_id,
            state=pending,
            now=now,
        )
    intent = RolloverIntegrationIntent(
        rollover_id=rollover_id,
        source_run_id="run-source",
        rollover_run_id="run-rollover",
        accepted_outcome="completed",
        parent_head="a" * 40,
        source_tree_sha256="b" * 40,
        accepted_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        review_result_artifact_path="codex/reviews/02.json",
        review_result_sha256="2" * 64,
        commit_message="msg",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/rollover/" + ("c" * 64),
        target_repository_root=str(tmp_path),
        target_git_common_dir=str(tmp_path / ".git"),
        target_git_dir=str(tmp_path / ".git"),
        managed_repository_root=str(tmp_path),
        managed_git_common_dir=str(tmp_path / ".git"),
        managed_git_dir=str(tmp_path / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    service = RolloverIntegrationService(store, artifacts)
    crashed = {"value": False}

    def crash_after_intent_write(step: str) -> None:
        if step == "after_intent_write" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after intent write")

    set_rollover_integration_publication_step_hook(crash_after_intent_write)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            service._publish_integration_intent(rollover_id, pending, intent)
        _, intent_sha, pending_after = service._publish_integration_intent(
            rollover_id,
            pending,
            intent,
        )
    finally:
        set_rollover_integration_publication_step_hook(None)
    assert pending_after.integration_intent_artifact_sha256 == intent_sha


def test_tick_lease_expired_rejects_integration_mutation(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    source_run_id = "run-source-fence"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    _reacquire_target_reservation(
        store,
        worktree_key="wt-rollover-fence",
        run_id=source_run_id,
        repo_root=repo,
    )
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    intent = RolloverIntegrationIntent(
        rollover_id="rol-" + ("f" * 32),
        source_run_id=source_run_id,
        rollover_run_id="run-rollover-fence",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        review_result_artifact_path="codex/reviews/01.json",
        review_result_sha256="1" * 64,
        commit_message="fence commit",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/rollover/" + ("c" * 64),
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    trusted = RolloverCheckpointTrustedTree(
        intent_sha256="1" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        parent_head=head,
        recorded_at=now_text,
    )
    service = RolloverIntegrationService(
        store,
        artifacts,
        now_factory=lambda: now,
        tick_context=RolloverIntegrationTickContext(
            tick_owner_id="stale-owner",
            tick_lease_generation=99,
        ),
    )
    with pytest.raises(SchedulerEngineError, match="tick lease expired"):
        service._integrate_target(
            intent=intent,
            intent_sha256="1" * 64,
            trusted=trusted,
            patch_path=artifacts.run_root("run-rollover-fence") / "git/diffs/01.patch",
            source_patch_path=artifacts.run_root("rol-fence") / "rollover/source-staged.patch",
            source_staged_patch_sha256="0" * 64,
            managed_root=repo,
            definition_parent_head=head,
            source_run_id=source_run_id,
            target_worktree_key="wt-rollover-fence",
            sequence_is_final=True,
            rollover_version=1,
        )
