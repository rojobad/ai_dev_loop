"""Phase 20.6 correction-turn tests for sequence reports, frozen publication, CAS, and cleanup."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
    _checkpoint_git_success,
    checkpoint_git_commit_tree,
    checkpoint_git_update_ref_cas,
    checkpoint_git_write_tree,
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
from ai_dev_loop.scheduler.application.git_admission import discover_repository_bounded
from ai_dev_loop.scheduler.application.recovery_artifacts import persist_recovery_definition
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _integration_intent_bytes,
    _integration_intent_sha256_from_bytes,
    set_recovery_integration_publication_step_hook,
)
from ai_dev_loop.scheduler.application.recovery_reconcile import (
    RecoveryReconcileService,
    set_recovery_cleanup_publication_step_hook,
)
from ai_dev_loop.scheduler.application.sequence_report import (
    build_recovery_integrated_completion_report,
)
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    RECOVERY_SEED_EVIDENCE_ARTIFACT,
    CleanupPendingRecoveryState,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    RecoveryIntegrationPublicationInputs,
    RecoverySequenceBinding,
    SequenceRecoveryResolution,
)
from ai_dev_loop.scheduler.domain.sequence import (
    BlockedSequenceState,
    FrozenSequenceEntry,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
    RecoveryIntegratedFinalizationSequenceState,
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


def _blocked_final_sequence(
    *,
    sequence_id: str,
    source_run_id: str,
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
            name="final-seq",
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
                    phase_name="final-phase",
                    planned_run_id=source_run_id,
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


def test_final_sequence_integrate_recovery_pending_builds_report(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "accepted\n")
    recovery_id = "rcv-" + ("q" * 32)
    source_run_id = "run-source-final-seq"
    sequence_id = "seq-final-report"
    worktree_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
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
                total_phases=1,
                is_final_phase=True,
            ),
        }
    )
    from ai_dev_loop.scheduler.application.recovery_artifacts import definition_digest_binding

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
    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=commit_sha, old_sha=head)
    from ai_dev_loop.runners.git import _checkpoint_git_success

    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=repo, context="reset after cas")
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-final-seq",
        sequence_id=sequence_id,
        sequence_ordinal=1,
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
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
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    trusted_bytes = json.dumps(trusted.model_dump(mode="json"), indent=2, sort_keys=True).encode(
        "utf-8"
    )
    publication_inputs = RecoveryIntegrationPublicationInputs(
        recorded_at=now_text,
        git_identity=_identity(now_text),
    )
    inputs_bytes = json.dumps(
        publication_inputs.model_dump(mode="json"), indent=2, sort_keys=True
    ).encode("utf-8")
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT,
        inputs_bytes,
        max_bytes=len(inputs_bytes) + 1,
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
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id="run-recovery-final-seq",
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
        resolution = SequenceRecoveryResolution(
            sequence_id=sequence_id,
            ordinal=1,
            source_run_id=source_run_id,
            recovery_id=recovery_id,
            recovery_run_id="run-recovery-final-seq",
            accepted_outcome="completed",
            residual_risk=False,
            reviewed_patch_sha256=patch_sha,
            reviewed_tree_sha256=tree,
            commit_sha256=commit_sha,
            recorded_at=now_text,
        )
        store.insert_sequence_recovery_resolution(conn, resolution, now=now)
    blocked = _blocked_final_sequence(
        sequence_id=sequence_id,
        source_run_id=source_run_id,
        repo=repo,
        worktree_key=worktree_key,
        now_text=now_text,
    )
    monkeypatch.setattr(
        store,
        "load_validated_sequence_state",
        lambda conn, sid: blocked if sid == sequence_id else (_ for _ in ()).throw(
            SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")
        ),
    )

    def fake_load_snapshot(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id in {source_run_id, "run-recovery-final-seq"}:
            return (
                CompletedState.model_construct(
                    run_id=run_id,
                    kind="completed",
                    codex=CodexWorkflowCheckpoint.model_construct(
                        latest_review_result_sha256="2" * 64
                    ),
                ),
                1,
                None,
            )
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load_snapshot)
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", lambda *args, **kwargs: True)
    monkeypatch.setattr(store, "release_reservation", lambda *args, **kwargs: None)
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256 == commit_sha
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, recovery_id)
        assert str(row["state_kind"]) == CLEANUP_PENDING_RECOVERY_STATE_KIND
        cleanup_state = CleanupPendingRecoveryState.model_validate_json(str(row["state_payload"]))
    assert cleanup_state.integration_intent_artifact_sha256 == intent_sha
    finalized = RecoveryIntegratedFinalizationSequenceState.model_construct(
        sequence_id=sequence_id,
        source_run_id=source_run_id,
        recovery_id=recovery_id,
        recovery_run_id="run-recovery-final-seq",
        integrated_commit_sha256=commit_sha,
        integrated_commit_sha256_prefix=commit_sha[:8],
        final_outcome="completed",
        finalized_at=now_text,
        definition=blocked.definition,
        materialized_entries=blocked.materialized_entries,
        residual_risk_ordinals=(),
    )
    with store.begin_read() as conn:
        report = build_recovery_integrated_completion_report(
            store,
            artifacts,
            finalized,
            conn=conn,
        )
    assert report.recovery_id == recovery_id
    assert report.phases[-1].checkpoint_commit_sha256 == commit_sha


def test_public_integration_preserves_frozen_publication_inputs_across_clock_advance(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "accepted\n")
    recovery_id = "rcv-" + ("r" * 32)
    recovery_run_id = "run-recovery-frozen-pub"
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
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    artifacts.write_bytes(
        recovery_id,
        definition.source_staged_patch_path,
        patch,
        max_bytes=len(patch) + 1,
    )
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    worktree = tmp_path / "managed"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence

    managed_admission = discover_repository_bounded(worktree)
    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(worktree),
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=managed_admission.git_common_dir,
        managed_git_dir=managed_admission.git_dir,
        managed_branch="HEAD",
        pre_seed_admission_status="",
    )
    stored_seed = artifacts.write_bytes(
        recovery_id,
        RECOVERY_SEED_EVIDENCE_ARTIFACT,
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(recovery_run_id, patch_rel, patch, max_bytes=len(patch) + 1)
    review_bytes = b'{"ok": true}\n'
    review_sha = hashlib.sha256(review_bytes).hexdigest()
    artifacts.write_bytes(
        recovery_run_id,
        "codex/reviews/01.json",
        review_bytes,
        max_bytes=len(review_bytes) + 1,
    )
    base_now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    base_text = base_now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=base_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id=recovery_run_id,
        started_at=base_text,
        accepted_outcome="completed",
        residual_risk=False,
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            now=base_now,
        )

    def fake_load_snapshot(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id == recovery_run_id:
            return (
                CompletedState.model_construct(
                    run_id=run_id,
                    kind="completed",
                    cursor=CursorWorkflowCheckpoint.model_construct(
                        staged_patch_path=patch_rel,
                        staged_patch_sha256=patch_sha,
                    ),
                    codex=CodexWorkflowCheckpoint.model_construct(
                        latest_review_result_path="codex/reviews/01.json",
                        latest_review_result_sha256=review_sha,
                    ),
                ),
                1,
                None,
            )
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load_snapshot)
    clock = {"now": base_now}

    def advancing_now() -> datetime:
        return clock["now"]

    service = RecoveryIntegrationService(store, artifacts, now_factory=advancing_now)
    crashed = {"value": False}

    def crash_after_intent_binding(step: str) -> None:
        if step == "after_intent_binding" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after intent binding")

    set_recovery_integration_publication_step_hook(crash_after_intent_binding)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            service.integrate_recovery_pending(recovery_id)
        clock["now"] = base_now + timedelta(hours=1)
        service.integrate_recovery_pending(recovery_id)
    finally:
        set_recovery_integration_publication_step_hook(None)
    intent = RecoveryIntegrationIntent.model_validate_json(
        (
            artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
        ).read_text(encoding="utf-8")
    )
    assert intent.recorded_at == base_text
    assert intent.git_identity.author_date == base_text
    inputs = RecoveryIntegrationPublicationInputs.model_validate_json(
        (
            artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT
        ).read_text(encoding="utf-8")
    )
    assert inputs.recorded_at == base_text


def test_proven_cas_rejects_substituted_commit_evidence(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    _, tree, _ = _stage_change(repo, "accepted\n")
    wrong_tree = checkpoint_git_write_tree(repo)
    now_text = datetime(2026, 9, 15, 15, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    wrong_commit = checkpoint_git_commit_tree(
        repo,
        tree_sha=wrong_tree,
        parent_sha=head,
        message="wrong commit",
        identity=identity,
    )
    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=wrong_commit, old_sha=head)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    service = RecoveryIntegrationService(store, artifacts)
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("s" * 32),
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="1" * 64,
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
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="b" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256="1" * 64,
        parent_head=head,
        recorded_at=now_text,
    )
    reconciled = service._reconcile_standalone_target_cas_if_proven(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        target_root=repo,
        source_run_id="run-source",
        evidence={"commit_sha256": wrong_commit, "tree_sha256": tree},
    )
    assert reconciled is None


def test_proven_cas_rejects_unadvanced_parent_with_staged_accepted_changes(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "accepted\n")
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    now_text = datetime(2026, 9, 15, 15, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    service = RecoveryIntegrationService(store, artifacts)
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("t" * 32),
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
        git_identity=_identity(now_text),
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="b" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    fake_commit = head
    reconciled = service._reconcile_standalone_target_cas_if_proven(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        target_root=repo,
        source_run_id="run-source",
        evidence={"commit_sha256": fake_commit, "tree_sha256": tree},
    )
    assert reconciled is None


def test_cleanup_adopts_evidence_after_write_before_binding(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("u" * 32)
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
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence

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
    stored_seed = artifacts.write_bytes(
        recovery_id,
        RECOVERY_SEED_EVIDENCE_ARTIFACT,
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date="2026-09-15T15:00:00.000000Z",
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date="2026-09-15T15:00:00.000000Z",
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=identity,
    )
    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=commit_sha, old_sha=head)
    recovery_git_update_private_ref_cas(
        repo,
        ref=definition.private_ref,
        new_sha=commit_sha,
        old_sha=head,
    )
    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=repo, context="reset after cas")
    recovery_git_checkout_detach(worktree, commit_sha=commit_sha)
    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=worktree, context="clean worktree")
    recovery_git_worktree_remove(repo, worktree_path=worktree)
    recovery_git_delete_ref(repo, ref=definition.private_ref, expected_sha=commit_sha)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cleanup_pending = CleanupPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
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
    cleanup_bytes = json.dumps(
        {
            "integrated_commit_sha256": commit_sha,
            "private_ref": definition.private_ref,
            "worktree_path": str(worktree),
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    artifacts.write_bytes(
        recovery_id,
        "recovery/cleanup-evidence.json",
        cleanup_bytes,
        max_bytes=len(cleanup_bytes) + 1,
    )
    reconcile = RecoveryReconcileService(store, artifacts, now_factory=lambda: now)
    receipt = reconcile.cleanup_deferred(recovery_id)
    assert receipt.action == "recovery_cleanup_complete"
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, recovery_id)
    assert str(row["state_kind"]) == "integrated"


def test_cleanup_crash_after_evidence_write_before_binding_retries(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("v" * 32)
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
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence

    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(tmp_path / "removed-managed-wt"),
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        managed_branch="HEAD",
        pre_seed_admission_status="",
    )
    stored_seed = artifacts.write_bytes(
        recovery_id,
        RECOVERY_SEED_EVIDENCE_ARTIFACT,
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date="2026-09-15T15:00:00.000000Z",
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date="2026-09-15T15:00:00.000000Z",
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=identity,
    )
    checkpoint_git_update_ref_cas(repo, ref="refs/heads/main", new_sha=commit_sha, old_sha=head)
    recovery_git_update_private_ref_cas(
        repo,
        ref=definition.private_ref,
        new_sha=commit_sha,
        old_sha=head,
    )
    from ai_dev_loop.runners.git import _checkpoint_git_success

    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=repo, context="reset after cas")
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    cleanup_pending = CleanupPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=definition.source_run_id,
        source_run_id_prefix=definition.source_run_id_prefix,
        recovery_run_id="run-recovery-cleanup-crash",
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
    reconcile = RecoveryReconcileService(store, artifacts, now_factory=lambda: now)
    crashed = {"value": False}

    def crash_after_cleanup_write(step: str) -> None:
        if step == "after_cleanup_write" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash after cleanup evidence write")

    set_recovery_cleanup_publication_step_hook(crash_after_cleanup_write)
    try:
        with pytest.raises(RuntimeError, match="simulated crash"):
            reconcile.cleanup_deferred(recovery_id)
        receipt = reconcile.cleanup_deferred(recovery_id)
    finally:
        set_recovery_cleanup_publication_step_hook(None)
    assert receipt.action == "recovery_cleanup_complete"
