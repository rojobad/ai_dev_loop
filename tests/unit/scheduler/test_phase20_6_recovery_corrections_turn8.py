"""Phase 20.6 correction-turn tests for sequence evidence merge, intent dates, and cleanup adoption."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _definition,
    _init_repo,
    _reacquire_target_reservation,
    _stage_change,
)

from ai_dev_loop.runners.git import (
    GitIdentity,
    _checkpoint_git_success,
    checkpoint_git_commit_tree,
    checkpoint_git_rev_parse,
    recovery_git_apply_staged_patch,
    recovery_git_checkout_detach,
    recovery_git_create_private_ref,
    recovery_git_delete_ref,
    recovery_git_update_private_ref_cas,
    recovery_git_worktree_add,
    recovery_git_worktree_remove,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.git_checkpoint import (
    CheckpointGitDeadline,
    ProductionGitCheckpointPort,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    definition_digest_binding,
    persist_recovery_definition,
)
from ai_dev_loop.scheduler.application.recovery_integration import RecoveryIntegrationService
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_worktree import (
    ProductionRecoveryWorktreePort,
    RecoverySeedEvidence,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    GitIdentitySnapshot,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.recovery import (
    RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    CleanupPendingRecoveryState,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    RecoverySequenceBinding,
)
from ai_dev_loop.scheduler.domain.sequence import (
    BlockedSequenceState,
    FrozenSequenceEntry,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
)
from ai_dev_loop.scheduler.domain.state import (
    CodexWorkflowCheckpoint,
    CompletedState,
    CursorWorkflowCheckpoint,
    RepositoryBinding,
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


def _blocked_non_final_sequence(
    *,
    sequence_id: str,
    source_run_id: str,
    successor_run_id: str,
    repo: Path,
    worktree_key: str,
    now_text: str,
) -> BlockedSequenceState:
    return BlockedSequenceState.model_construct(
        schema_version=1,
        sequence_id=sequence_id,
        version=2,
        prepared_at=now_text,
        updated_at=now_text,
        started_at=now_text,
        blocked_at=now_text,
        block_reason_kind="blocked",
        idempotency_key="a" * 64,
        current_ordinal=1,
        current_run_id=source_run_id,
        definition=PreparedSequenceDefinition.model_construct(
            sequence_id=sequence_id,
            name="nonfinal-seq",
            project_name="proj",
            repository=RepositoryBinding.model_construct(
                root=str(repo),
                git_common_dir=str(repo / ".git"),
                git_dir=str(repo / ".git"),
                branch="main",
                initial_head="0" * 40,
                worktree_key=worktree_key,
            ),
            entries=(
                FrozenSequenceEntry.model_construct(
                    ordinal=1,
                    phase_name="phase-one",
                    planned_run_id=source_run_id,
                ),
                FrozenSequenceEntry.model_construct(
                    ordinal=2,
                    phase_name="phase-two",
                    planned_run_id=successor_run_id,
                ),
            ),
        ),
        materialized_entries=(
            MaterializedSequenceEntry.model_construct(
                ordinal=1,
                run_id=source_run_id,
                entry_hash="b" * 64,
                materialized_at=now_text,
            ),
        ),
        residual_risk_ordinals=(),
    )


class _CrashingGitCheckpointPort(ProductionGitCheckpointPort):
    def __init__(self, crash_after: Literal["tree", "commit", "none"]) -> None:
        super().__init__()
        self._crash_after = crash_after

    def execute_checkpoint(
        self,
        intent: SequenceCheckpointIntent,
        *,
        patch_path: Path,
        trusted_tree: SequenceCheckpointTrustedTree,
        evidence: SequenceCheckpointEvidence | None,
        persist_tree_sha: Callable[[str], None],
        persist_commit_sha: Callable[[str], None],
        mutation_fence: Callable[[str], None] | None = None,
        authorize_ref_update: Callable[[], None] | None = None,
        on_ref_advanced: Callable[[], None] | None = None,
        deadline: CheckpointGitDeadline,
        now_factory: Callable[[], datetime],
    ):
        if self._crash_after == "tree":
            persist_tree_sha(trusted_tree.reviewed_tree_sha256)
            raise RuntimeError("crash after tree evidence")

        wrapped_persist_commit = persist_commit_sha
        if self._crash_after == "commit":

            def crashing_persist_commit(commit_sha: str) -> None:
                wrapped_persist_commit(commit_sha)
                raise RuntimeError("crash after commit evidence")

            persist_commit_sha = crashing_persist_commit

        return super().execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=evidence,
            persist_tree_sha=persist_tree_sha,
            persist_commit_sha=persist_commit_sha,
            mutation_fence=mutation_fence,
            authorize_ref_update=authorize_ref_update,
            on_ref_advanced=on_ref_advanced,
            deadline=deadline,
            now_factory=now_factory,
        )


def _sequence_managed_integration_setup(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    SqliteSchedulerStore,
    ProtectedArtifactStore,
    RecoveryIntegrationService,
    str,
    object,
    str,
    str,
    str,
]:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("t" * 32)
    source_run_id = "run-source-seq"
    successor_run_id = "run-successor-seq"
    sequence_id = "seq-nonfinal-evidence"
    recovery_run_id = "run-recovery-seq-evidence"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    definition = definition.model_copy(
        update={
            "source_run_id": source_run_id,
            "source_run_id_prefix": source_run_id[:8],
            "sequence": RecoverySequenceBinding(
                sequence_id=sequence_id,
                ordinal=1,
                total_phases=2,
                is_final_phase=False,
            ),
        }
    )
    definition = definition.model_copy(
        update={"definition_sha256": definition_digest_binding(definition)}
    )
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(recovery_run_id, patch_rel, patch, max_bytes=len(patch) + 1)
    review_sha = "4" * 64

    def fake_load_snapshot(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id == recovery_run_id:
            state = CompletedState.model_construct(
                run_id=run_id,
                kind="completed",
                cursor=CursorWorkflowCheckpoint.model_construct(
                    staged_patch_path=patch_rel,
                    staged_patch_sha256=patch_sha,
                ),
                codex=CodexWorkflowCheckpoint.model_construct(
                    latest_review_result_path="codex/reviews/01.json",
                    latest_review_result_sha256=review_sha,
                    reviewer_session_id="00000000-0000-4000-8000-000000000099",
                ),
            )
            return state, 1, None
        if run_id == source_run_id:
            return CompletedState.model_construct(run_id=run_id, kind="completed"), 1, None
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, f"missing run {run_id}")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load_snapshot)
    worktree = tmp_path / "managed-wt"
    private_ref = definition.private_ref
    recovery_git_create_private_ref(repo, ref=private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    git_identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    managed_commit = checkpoint_git_commit_tree(
        worktree,
        tree_sha=tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=git_identity,
    )
    recovery_git_update_private_ref_cas(
        repo,
        ref=private_ref,
        new_sha=managed_commit,
        old_sha=head,
    )
    recovery_git_checkout_detach(worktree, commit_sha=managed_commit)
    from ai_dev_loop.scheduler.application.recovery_integration import (
        _integration_intent_bytes,
        _integration_intent_sha256_from_bytes,
    )

    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id=recovery_run_id,
        sequence_id=sequence_id,
        sequence_ordinal=1,
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        review_result_sha256=review_sha,
        commit_message=definition.integration.commit_message,
        target_branch_ref="refs/heads/main",
        private_ref=private_ref,
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(worktree),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(worktree / ".git"),
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    intent_bytes = _integration_intent_bytes(intent)
    intent_sha = _integration_intent_sha256_from_bytes(intent_bytes)
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    trusted_bytes = json.dumps(trusted.model_dump(mode="json"), indent=2, sort_keys=True).encode(
        "utf-8"
    )
    artifacts.write_bytes(
        recovery_id,
        definition.source_staged_patch_path,
        patch,
        max_bytes=len(patch) + 1,
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
    evidence = {
        "managed_commit_sha256": managed_commit,
        "private_ref_sha256": managed_commit,
        "delta_applied": "true",
    }
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
        json.dumps(evidence, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=1024,
    )
    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(worktree),
        private_ref=private_ref,
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
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id=recovery_run_id,
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integration_intent_artifact_sha256=intent_sha,
        integration_trusted_tree_artifact_sha256=hashlib.sha256(trusted_bytes).hexdigest(),
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=now,
        )
    blocked = _blocked_non_final_sequence(
        sequence_id=sequence_id,
        source_run_id=source_run_id,
        successor_run_id=successor_run_id,
        repo=repo,
        worktree_key=definition.repository.worktree_key,
        now_text=now_text,
    )
    monkeypatch.setattr(
        store,
        "load_validated_sequence_state",
        lambda conn, sid: blocked if sid == sequence_id else (_ for _ in ()).throw(
            SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")
        ),
    )
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", lambda *args, **kwargs: True)
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    return store, artifacts, service, recovery_id, definition, managed_commit, tree, head


def test_sequence_tree_evidence_restart_preserves_managed_progress(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, recovery_id, definition, managed_commit, tree, _head = (
        _sequence_managed_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    )
    crashing = RecoveryIntegrationService(
        store,
        artifacts,
        git_checkpoint=_CrashingGitCheckpointPort("tree"),
        now_factory=service._now_factory,
    )
    with pytest.raises(RuntimeError, match="crash after tree evidence"):
        crashing.integrate_recovery_pending(recovery_id)
    evidence = json.loads(
        (artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT).read_text(
            encoding="utf-8"
        )
    )
    assert evidence["managed_commit_sha256"] == managed_commit
    assert evidence["private_ref_sha256"] == managed_commit
    assert evidence["delta_applied"] == "true"
    assert evidence["tree_sha256"] == tree
    replay = service._classify_integration_replay(recovery_id, definition)
    assert replay.managed_side_complete is True
    monkeypatch.setattr(service, "_sequence_recovery_effects_complete", lambda *args, **kwargs: True)
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256


def test_sequence_commit_evidence_restart_preserves_managed_progress_before_cas(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, recovery_id, definition, managed_commit, tree, head = (
        _sequence_managed_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    )
    repo = Path(definition.repository.root)
    crashing = RecoveryIntegrationService(
        store,
        artifacts,
        git_checkpoint=_CrashingGitCheckpointPort("commit"),
        now_factory=service._now_factory,
    )
    with pytest.raises(RuntimeError, match="crash after commit evidence"):
        crashing.integrate_recovery_pending(recovery_id)
    evidence = json.loads(
        (artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT).read_text(
            encoding="utf-8"
        )
    )
    assert evidence["managed_commit_sha256"] == managed_commit
    assert evidence["private_ref_sha256"] == managed_commit
    assert evidence["delta_applied"] == "true"
    assert evidence["tree_sha256"] == tree
    assert evidence["commit_sha256"]
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == head
    replay = service._classify_integration_replay(recovery_id, definition)
    assert replay.managed_side_complete is True
    assert replay.target_commit_sha is None
    monkeypatch.setattr(service, "_sequence_recovery_effects_complete", lambda *args, **kwargs: True)
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256 == evidence["commit_sha256"]
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == evidence["commit_sha256"]


def test_proven_cas_rejects_commit_when_only_intent_dates_differ(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "accepted\n")
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    intent_time = datetime(2026, 9, 15, 15, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    commit_time = datetime(2026, 9, 15, 16, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    commit_identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=commit_time,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=commit_time,
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message="expected commit",
        identity=commit_identity,
    )
    from ai_dev_loop.runners.git import checkpoint_git_update_ref_cas

    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=commit_sha, old_sha=head)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    service = RecoveryIntegrationService(store, artifacts)
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("d" * 32),
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        review_result_sha256="2" * 64,
        commit_message="expected commit",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("a" * 64),
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        git_identity=_identity(intent_time),
        recorded_at=intent_time,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="b" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=intent_time,
    )
    reconciled = service._reconcile_standalone_target_cas_if_proven(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        target_root=repo,
        source_run_id="run-source",
        evidence={"commit_sha256": commit_sha, "tree_sha256": tree},
    )
    assert reconciled is None


def _cleanup_pending_fixture(
    tmp_path: Path,
    isolated_xdg: Path,
) -> tuple[
    SqliteSchedulerStore,
    ProtectedArtifactStore,
    RecoveryReconcileService,
    str,
    CleanupPendingRecoveryState,
    str,
    Path,
    str,
    object,
]:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("c" * 32)
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
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
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
    recovery_git_update_private_ref_cas(
        repo,
        ref=definition.private_ref,
        new_sha=commit_sha,
        old_sha=head,
    )
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
        recovery_run_id="run-recovery-cleanup-adopt",
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
    reconcile = RecoveryReconcileService(
        store,
        artifacts,
        worktree_port=ProductionRecoveryWorktreePort(),
        now_factory=lambda: now,
    )
    return (
        store,
        artifacts,
        reconcile,
        recovery_id,
        cleanup_pending,
        commit_sha,
        worktree,
        definition.private_ref,
        definition,
    )


def _remove_cleanup_resources(
    repo: Path,
    *,
    worktree: Path,
    private_ref: str,
    commit_sha: str,
) -> None:
    recovery_git_checkout_detach(worktree, commit_sha=commit_sha)
    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=worktree, context="clean worktree")
    recovery_git_worktree_remove(repo, worktree_path=worktree)
    recovery_git_delete_ref(repo, ref=private_ref, expected_sha=commit_sha)


def test_cleanup_adopts_unbound_evidence_when_resources_removed(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    _, artifacts, reconcile, recovery_id, _, commit_sha, worktree, private_ref, definition = (
        _cleanup_pending_fixture(tmp_path, isolated_xdg)
    )
    repo = Path(definition.repository.root)
    _remove_cleanup_resources(
        repo,
        worktree=worktree,
        private_ref=private_ref,
        commit_sha=commit_sha,
    )
    cleanup_bytes = json.dumps(
        {
            "integrated_commit_sha256": commit_sha,
            "private_ref": private_ref,
            "worktree_path": str(worktree),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
        cleanup_bytes,
        max_bytes=len(cleanup_bytes) + 1,
    )
    receipt = reconcile.cleanup_deferred(recovery_id)
    assert receipt.action == "recovery_cleanup_complete"


def test_cleanup_rejects_unbound_evidence_when_resources_remain(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    _, artifacts, reconcile, recovery_id, _, commit_sha, worktree, private_ref, _definition = (
        _cleanup_pending_fixture(tmp_path, isolated_xdg)
    )
    cleanup_bytes = json.dumps(
        {
            "integrated_commit_sha256": commit_sha,
            "private_ref": private_ref,
            "worktree_path": str(worktree),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
        cleanup_bytes,
        max_bytes=len(cleanup_bytes) + 1,
    )
    receipt = reconcile.cleanup_deferred(recovery_id)
    assert receipt.action == "recovery_cleanup_failed"


def test_cleanup_rejects_unbound_evidence_on_unrelated_drift(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    _, artifacts, reconcile, recovery_id, _, commit_sha, worktree, private_ref, definition = (
        _cleanup_pending_fixture(tmp_path, isolated_xdg)
    )
    repo = Path(definition.repository.root)
    _remove_cleanup_resources(
        repo,
        worktree=worktree,
        private_ref=private_ref,
        commit_sha=commit_sha,
    )
    drifted = json.dumps(
        {
            "integrated_commit_sha256": "f" * 40,
            "private_ref": private_ref,
            "worktree_path": str(worktree),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
        drifted,
        max_bytes=len(drifted) + 1,
    )
    receipt = reconcile.cleanup_deferred(recovery_id)
    assert receipt.action == "recovery_cleanup_failed"
