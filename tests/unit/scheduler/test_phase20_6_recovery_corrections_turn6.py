"""Phase 20.6 correction-turn tests for public CAS replay, publication, cleanup, and reports."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _definition,
    _init_repo,
    _reacquire_target_reservation,
    _stage_change,
)

from ai_dev_loop.runners.git import (
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_write_tree,
    recovery_git_apply_staged_patch,
    recovery_git_create_private_ref,
    recovery_git_private_ref_peek,
    recovery_git_worktree_add,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import persist_recovery_definition
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _integration_intent_bytes,
    _integration_intent_sha256_from_bytes,
    set_recovery_integration_publication_step_hook,
)
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.application.sequence_report import (
    SequenceCheckpointEvidenceError,
    _recovery_checkpoint_fields,
)
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    CleanupPendingRecoveryState,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    SequenceRecoveryResolution,
)
from ai_dev_loop.scheduler.domain.state import (
    CodexWorkflowCheckpoint,
    CompletedState,
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


def test_integrate_recovery_pending_reconciles_proven_cas_after_crash(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    source_run_id = "run-source"
    recovery_id = "rcv-" + ("j" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha="0" * 64,
        tree_sha=tree,
    )
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    now = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=identity,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-public",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="1" * 64,
        review_result_sha256="2" * 64,
        commit_message=definition.integration.commit_message,
        target_branch_ref="refs/heads/main",
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    intent_bytes = _integration_intent_bytes(intent)
    intent_sha = _integration_intent_sha256_from_bytes(intent_bytes)
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256="1" * 64,
        parent_head=head,
        recorded_at=now_text,
    )
    trusted_bytes = json.dumps(trusted.model_dump(mode="json"), indent=2, sort_keys=True).encode(
        "utf-8"
    )
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
    )
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
        trusted_bytes,
        max_bytes=len(trusted_bytes) + 1,
    )
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id="run-recovery-public",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integration_intent_artifact_sha256=intent_sha,
        integration_trusted_tree_artifact_sha256=hashlib.sha256(trusted_bytes).hexdigest(),
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=now,
        )
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=source_run_id,
            intent_sha256=intent_sha,
            hold_reason="checkpoint_cas_authorized",
            ref_may_have_advanced=True,
            now=now,
        )
    from ai_dev_loop.runners.git import checkpoint_git_update_ref_cas

    checkpoint_git_update_ref_cas(
        repo,
        ref="refs/heads/main",
        new_sha=commit_sha,
        old_sha=head,
    )
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
        json.dumps(
            {"commit_sha256": commit_sha, "tree_sha256": tree},
            indent=2,
            sort_keys=True,
        ).encode("utf-8"),
        max_bytes=1024,
    )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256 == commit_sha
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)


def test_publication_adopts_intent_after_write_before_trusted_binding(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    recovery_id = "rcv-" + ("k" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 14, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        recovery_run_id="run-recovery",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head="a" * 40,
        source_tree_sha256="b" * 40,
        accepted_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        review_result_sha256="2" * 64,
        commit_message="msg",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("c" * 64),
        target_repository_root=str(tmp_path),
        target_git_common_dir=str(tmp_path / ".git"),
        target_git_dir=str(tmp_path / ".git"),
        managed_repository_root=str(tmp_path),
        managed_git_common_dir=str(tmp_path / ".git"),
        managed_git_dir=str(tmp_path / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    service = RecoveryIntegrationService(store, artifacts)
    intent_bytes = _integration_intent_bytes(intent)
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
    )
    loaded, intent_sha, pending_after = service._publish_integration_intent(
        recovery_id,
        pending,
        intent,
    )
    assert loaded.recovery_id == recovery_id
    assert pending_after.integration_intent_artifact_sha256 == intent_sha
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        parent_head="a" * 40,
        recorded_at=now_text,
    )
    _, trusted_sha, pending_final = service._publish_integration_trusted_tree(
        recovery_id,
        pending_after,
        trusted,
        intent_sha256=intent_sha,
    )
    assert pending_final.integration_trusted_tree_artifact_sha256 == trusted_sha


def test_publication_crash_after_intent_write_before_binding_retries_safely(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    recovery_id = "rcv-" + ("o" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 14, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        recovery_run_id="run-recovery",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head="a" * 40,
        source_tree_sha256="b" * 40,
        accepted_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        review_result_sha256="2" * 64,
        commit_message="msg",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("c" * 64),
        target_repository_root=str(tmp_path),
        target_git_common_dir=str(tmp_path / ".git"),
        target_git_dir=str(tmp_path / ".git"),
        managed_repository_root=str(tmp_path),
        managed_git_common_dir=str(tmp_path / ".git"),
        managed_git_dir=str(tmp_path / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    service = RecoveryIntegrationService(store, artifacts)
    crashed = {"value": False}

    def crash_after_intent_write(step: str) -> None:
        if step == "after_intent_write" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after intent write")

    set_recovery_integration_publication_step_hook(crash_after_intent_write)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            service._publish_integration_intent(recovery_id, pending, intent)
        _, intent_sha, pending_after = service._publish_integration_intent(
            recovery_id,
            pending,
            intent,
        )
    finally:
        set_recovery_integration_publication_step_hook(None)
    assert pending_after.integration_intent_artifact_sha256 == intent_sha


def test_publication_crash_after_trusted_write_before_binding_retries_safely(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    recovery_id = "rcv-" + ("p" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 14, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        recovery_run_id="run-recovery",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head="a" * 40,
        source_tree_sha256="b" * 40,
        accepted_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        review_result_sha256="2" * 64,
        commit_message="msg",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("c" * 64),
        target_repository_root=str(tmp_path),
        target_git_common_dir=str(tmp_path / ".git"),
        target_git_dir=str(tmp_path / ".git"),
        managed_repository_root=str(tmp_path),
        managed_git_common_dir=str(tmp_path / ".git"),
        managed_git_dir=str(tmp_path / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    service = RecoveryIntegrationService(store, artifacts)
    _, intent_sha, pending_after = service._publish_integration_intent(
        recovery_id,
        pending,
        intent,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256="b" * 40,
        reviewed_patch_sha256="1" * 64,
        parent_head="a" * 40,
        recorded_at=now_text,
    )
    crashed = {"value": False}

    def crash_after_trusted_write(step: str) -> None:
        if step == "after_trusted_write" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after trusted write")

    set_recovery_integration_publication_step_hook(crash_after_trusted_write)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            service._publish_integration_trusted_tree(
                recovery_id,
                pending_after,
                trusted,
                intent_sha256=intent_sha,
            )
        _, trusted_sha, pending_final = service._publish_integration_trusted_tree(
            recovery_id,
            pending_after,
            trusted,
            intent_sha256=intent_sha,
        )
    finally:
        set_recovery_integration_publication_step_hook(None)
    assert pending_final.integration_trusted_tree_artifact_sha256 == trusted_sha


def test_cleanup_rejects_substituted_worktree_path(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("l" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    worktree = tmp_path / "managed-wt"
    alt_worktree = tmp_path / "alt-wt"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=alt_worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    recovery_git_apply_staged_patch(alt_worktree, patch_bytes=patch)
    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(worktree),
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(worktree / ".git"),
        managed_branch="HEAD",
        pre_seed_admission_status="",
    )
    seed_bytes = json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8")
    stored_seed = artifacts.write_bytes(
        recovery_id,
        "recovery/seed-evidence.json",
        seed_bytes,
        max_bytes=4096,
    )
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date="2026-09-15T14:00:00.000000Z",
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date="2026-09-15T14:00:00.000000Z",
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=identity,
    )
    from ai_dev_loop.runners.git import checkpoint_git_update_ref_cas

    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=commit_sha, old_sha=head)
    now = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cleanup_pending = CleanupPendingRecoveryState(
        recovery_id=recovery_id,
        version=2,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=definition.source_run_id,
        source_run_id_prefix=definition.source_run_id_prefix,
        recovery_run_id="run-recovery-cleanup",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integrated_commit_sha256=commit_sha,
        integrated_commit_sha256_prefix=commit_sha[:8],
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=cleanup_pending,
            now=now,
        )
    tampered = json.dumps(
        {
            "integrated_commit_sha256": commit_sha,
            "private_ref": definition.private_ref,
            "worktree_path": str(alt_worktree),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    stored_cleanup = artifacts.write_bytes(
        recovery_id,
        RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
        tampered,
        max_bytes=len(tampered) + 1,
    )
    cleanup_pending = cleanup_pending.model_copy(
        update={"cleanup_evidence_artifact_sha256": stored_cleanup.sha256},
    )
    with store.begin_immediate() as conn:
        store.update_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=cleanup_pending,
            expected_version=2,
            now=now,
        )
    reconcile = RecoveryReconcileService(store, artifacts, now_factory=lambda: now)
    receipt = reconcile.cleanup_deferred(recovery_id)
    assert receipt.action == "recovery_cleanup_failed"


def test_managed_private_ref_update_rejected_after_abort(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("m" * 32)
    source_run_id = "run-source"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    worktree = tmp_path / "managed"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    now = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id="run-recovery-abort-managed",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integration_intent_artifact_sha256="1" * 64,
        integration_trusted_tree_artifact_sha256="2" * 64,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=now,
        )
    artifacts.write_bytes(
        recovery_id,
        "recovery/abort-intent.txt",
        b"user_requested_abort\n",
        max_bytes=64,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-abort-managed",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        review_result_sha256="3" * 64,
        commit_message=definition.integration.commit_message,
        target_branch_ref=definition.target_branch_ref,
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(worktree),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(worktree / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="1" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    peek_before = recovery_git_private_ref_peek(repo, ref=definition.private_ref)
    with pytest.raises(SchedulerEngineError, match="abort intent blocks integration"):
        service._integrate_target(
            intent=intent,
            intent_sha256="1" * 64,
            trusted=trusted,
            patch_path=artifacts.run_root("run-recovery-abort-managed") / "git/diffs/01.patch",
            source_patch_path=artifacts.run_root(recovery_id) / definition.source_staged_patch_path,
            source_staged_patch_sha256=patch_sha,
            managed_root=worktree,
            definition_parent_head=head,
            source_run_id=source_run_id,
            target_worktree_key=definition.repository.worktree_key,
            sequence_is_final=True,
            recovery_version=1,
        )
    assert recovery_git_private_ref_peek(repo, ref=definition.private_ref) == peek_before


def test_recovery_checkpoint_fields_reject_altered_trusted_tree(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 14, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recovery_id = "rcv-" + ("n" * 32)
    recovery_run_id = "run-recovery-report"
    intent_bytes = json.dumps(
        {
            "schema_version": 1,
            "recovery_id": recovery_id,
            "source_run_id": "run-source-report",
            "recovery_run_id": recovery_run_id,
            "accepted_outcome": "completed",
            "parent_head": "a" * 40,
            "source_tree_sha256": "b" * 40,
            "accepted_tree_sha256": "c" * 40,
            "reviewed_patch_sha256": "3" * 64,
            "review_result_sha256": "4" * 64,
            "commit_message": "msg",
            "target_branch_ref": "refs/heads/main",
            "private_ref": "refs/ai-dev-loop/recovery/" + ("d" * 64),
            "target_repository_root": "/tmp/repo",
            "target_git_common_dir": "/tmp/repo/.git",
            "target_git_dir": "/tmp/repo/.git",
            "managed_repository_root": "/tmp/repo",
            "managed_git_common_dir": "/tmp/repo/.git",
            "managed_git_dir": "/tmp/repo/.git",
            "git_identity": {
                "author_name": "a",
                "author_email": "a@example.com",
                "author_date": now_text,
                "committer_name": "a",
                "committer_email": "a@example.com",
                "committer_date": now_text,
            },
            "recorded_at": now_text,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    intent_digest = hashlib.sha256(intent_bytes).hexdigest()
    trusted_bytes = json.dumps(
        {
            "schema_version": 1,
            "intent_sha256": intent_digest,
            "reviewed_tree_sha256": "c" * 40,
            "reviewed_patch_sha256": "3" * 64,
            "parent_head": "a" * 40,
            "recorded_at": now_text,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    trusted_digest = hashlib.sha256(trusted_bytes).hexdigest()
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
    )
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
        trusted_bytes,
        max_bytes=len(trusted_bytes) + 1,
    )
    cleanup = CleanupPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256="5" * 64,
        definition_artifact_sha256="6" * 64,
        source_run_id="run-source-report",
        source_run_id_prefix="run-sour",
        recovery_run_id=recovery_run_id,
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integrated_commit_sha256="f" * 40,
        integrated_commit_sha256_prefix="ffffffff",
        integration_intent_artifact_sha256=intent_digest,
        integration_trusted_tree_artifact_sha256=trusted_digest,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=cleanup,
            now=datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        )
    resolution = SequenceRecoveryResolution(
        sequence_id="seq-report",
        ordinal=1,
        source_run_id="run-source-report",
        recovery_id=recovery_id,
        recovery_run_id=recovery_run_id,
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="9" * 64,
        reviewed_tree_sha256="c" * 40,
        commit_sha256="f" * 40,
        recorded_at=now_text,
    )

    def fake_load(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id == recovery_run_id:
            return (
                CompletedState.model_construct(
                    run_id=run_id,
                    kind="completed",
                    codex=CodexWorkflowCheckpoint.model_construct(
                        latest_review_result_sha256="4" * 64
                    ),
                ),
                1,
                None,
            )
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load)
    with (
        store.begin_read() as conn,
        pytest.raises(
            SequenceCheckpointEvidenceError,
            match="sequence resolution patch",
        ),
    ):
        _recovery_checkpoint_fields(store, artifacts, conn, resolution)
