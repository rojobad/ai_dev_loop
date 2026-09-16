"""Phase 20.6 correction-turn tests for bindings, fencing, CAS reconcile, and sequence replay."""

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
    checkpoint_git_rev_parse,
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
)
from ai_dev_loop.scheduler.application.recovery_integration_fencing import (
    RecoveryIntegrationTickContext,
)
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    FreshReviewRecoveryDefinition,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    RecoverySequenceBinding,
    SequenceRecoveryResolution,
)
from ai_dev_loop.scheduler.domain.state import (
    CodexWorkflowCheckpoint,
    CompletedState,
    CursorWorkflowCheckpoint,
    RepositoryBinding,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
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


def test_resume_integration_uses_recovery_run_accepted_patch(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    source_patch, source_tree, source_patch_sha = _stage_change(repo, "source\n")
    accepted_patch, accepted_tree, accepted_patch_sha = _stage_change(repo, "accepted\n")
    assert source_patch_sha != accepted_patch_sha
    assert source_tree != accepted_tree

    recovery_id = "rcv-" + ("f" * 32)
    recovery_run_id = "run-recovery-accepted"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=source_patch_sha,
        tree_sha=source_tree,
    )
    source_run_id = definition.source_run_id
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(
        recovery_run_id,
        patch_rel,
        accepted_patch,
        max_bytes=len(accepted_patch) + 1,
    )
    review_sha = "5" * 64

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
                        staged_patch_sha256=accepted_patch_sha,
                    ),
                    codex=CodexWorkflowCheckpoint.model_construct(
                        latest_review_result_path="codex/reviews/01.json",
                        latest_review_result_sha256=review_sha,
                        reviewer_session_id="00000000-0000-4000-8000-000000000055",
                    ),
                ),
                1,
                None,
            )
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, f"missing run {run_id}")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load_snapshot)

    artifacts.write_bytes(
        recovery_id,
        definition.source_staged_patch_path,
        source_patch,
        max_bytes=len(source_patch) + 1,
    )
    recovery_git_apply_staged_patch(repo, patch_bytes=source_patch)
    worktree = tmp_path / "managed-wt"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=accepted_patch)

    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=source_patch_sha,
        staged_tree_sha256=source_tree,
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
        "recovery/seed-evidence.json",
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id=recovery_run_id,
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=source_tree,
        accepted_tree_sha256=accepted_tree,
        reviewed_patch_sha256=accepted_patch_sha,
        review_result_sha256=review_sha,
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
    intent_bytes = _integration_intent_bytes(intent)
    intent_sha = _integration_intent_sha256_from_bytes(intent_bytes)
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256=accepted_tree,
        reviewed_patch_sha256=accepted_patch_sha,
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
        recovery_run_id=recovery_run_id,
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        seed_evidence_artifact_sha256=stored_seed.sha256,
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

    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cleanup = service._resume_integration_from_persisted_intent(
        recovery_id,
        pending,
        definition,
        skip_frozen_source_checks=False,
    )
    assert cleanup.integrated_commit_sha256
    assert checkpoint_git_write_tree(repo) == accepted_tree
    assert checkpoint_git_rev_parse(repo, "HEAD") != head


def test_integration_rejects_tampered_intent_binding(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    recovery_id = "rcv-" + ("g" * 32)
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 13, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    intent_bytes = json.dumps({"recovery_id": recovery_id}, indent=2, sort_keys=True).encode(
        "utf-8"
    )
    trusted_bytes = json.dumps(
        {
            "intent_sha256": "1" * 64,
            "reviewed_tree_sha256": "a" * 40,
            "reviewed_patch_sha256": "2" * 64,
            "parent_head": "b" * 40,
            "recorded_at": now_text,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
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
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        recovery_run_id="run-recovery",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integration_intent_artifact_sha256="0" * 64,
        integration_trusted_tree_artifact_sha256=hashlib.sha256(trusted_bytes).hexdigest(),
    )
    service = RecoveryIntegrationService(store, artifacts)
    with pytest.raises(ProtectedArtifactError, match="artifact hash mismatch"):
        service._load_authenticated_integration_bindings(recovery_id, pending)


def test_integrate_target_rejects_expired_tick_before_managed_mutations(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("h" * 32)
    source_run_id = "run-source-fence"
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
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256="e" * 64,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id="run-recovery-fence",
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
        store.acquire_global_tick_lease(
            conn,
            owner_id="live-owner",
            now=now,
            ttl_seconds=300,
        )
    worktree = tmp_path / "managed"
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-fence",
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
    private_ref_before = recovery_git_private_ref_peek(repo, ref=definition.private_ref)
    service = RecoveryIntegrationService(
        store,
        artifacts,
        now_factory=lambda: now,
        tick_context=RecoveryIntegrationTickContext(
            tick_owner_id="stale-owner",
            tick_lease_generation=99,
        ),
    )
    with pytest.raises(SchedulerEngineError, match="tick lease expired"):
        service._integrate_target(
            intent=intent,
            intent_sha256="1" * 64,
            trusted=trusted,
            patch_path=artifacts.run_root("run-recovery-fence") / "git/diffs/01.patch",
            source_patch_path=artifacts.run_root(recovery_id) / definition.source_staged_patch_path,
            source_staged_patch_sha256=patch_sha,
            managed_root=worktree,
            definition_parent_head=head,
            source_run_id=source_run_id,
            target_worktree_key=definition.repository.worktree_key,
            sequence_is_final=True,
            recovery_version=1,
        )
    evidence_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
    assert not evidence_path.is_file()
    assert recovery_git_private_ref_peek(repo, ref=definition.private_ref) == private_ref_before


def test_standalone_cas_reconcile_releases_hold_after_crash(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    source_run_id = "run-source-cas-reconcile"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    _reacquire_target_reservation(
        store,
        worktree_key="wt-cas-reconcile",
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
        message="cas reconcile commit",
        identity=identity,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("i" * 32),
        source_run_id=source_run_id,
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        review_result_sha256="1" * 64,
        commit_message="cas reconcile commit",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("a" * 64),
        target_repository_root=str(repo),
        target_git_common_dir=str(repo / ".git"),
        target_git_dir=str(repo / ".git"),
        managed_repository_root=str(repo),
        managed_git_common_dir=str(repo / ".git"),
        managed_git_dir=str(repo / ".git"),
        git_identity=identity_snapshot,
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="b" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        parent_head=head,
        recorded_at=now_text,
    )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
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
        target_worktree_key="wt-cas-reconcile",
        evidence=evidence,
        recovery_version=1,
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


def _recovery_definition_for_sequence(
    *,
    repo: Path,
    source_run_id: str,
    sequence_id: str,
    ordinal: int,
    total_phases: int,
    is_final_phase: bool,
    worktree_key: str,
) -> FreshReviewRecoveryDefinition:
    return FreshReviewRecoveryDefinition.model_construct(
        source_run_id=source_run_id,
        sequence=RecoverySequenceBinding(
            sequence_id=sequence_id,
            ordinal=ordinal,
            total_phases=total_phases,
            is_final_phase=is_final_phase,
        ),
        repository=RepositoryBinding.model_construct(
            root=str(repo),
            git_common_dir=str(repo / ".git"),
            git_dir=str(repo / ".git"),
            branch="main",
            initial_head="f" * 40,
            worktree_key=worktree_key,
        ),
    )


def test_complete_sequence_recovery_replay_is_idempotent_non_final(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sequence_id = "seq-replay-nonfinal"
    source_run_id = "run-seq-source"
    worktree_key = "a" * 64
    resolution = SequenceRecoveryResolution(
        sequence_id=sequence_id,
        ordinal=1,
        source_run_id=source_run_id,
        recovery_id="rcv-seq-nonfinal",
        recovery_run_id="run-recovery-seq",
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="b" * 64,
        reviewed_tree_sha256="c" * 40,
        commit_sha256="d" * 40,
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="e" * 64,
        reviewed_tree_sha256="c" * 40,
        reviewed_patch_sha256="b" * 64,
        parent_head="f" * 40,
        recorded_at=now_text,
    )
    recovery_definition = _recovery_definition_for_sequence(
        repo=Path("/tmp/repo"),
        source_run_id=source_run_id,
        sequence_id=sequence_id,
        ordinal=1,
        total_phases=2,
        is_final_phase=False,
        worktree_key=worktree_key,
    )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cas_calls = 0

    def fake_cas(*args: object, **kwargs: object) -> bool:
        nonlocal cas_calls
        cas_calls += 1
        return True

    monkeypatch.setattr(
        store,
        "get_sequence_recovery_resolution",
        lambda conn, **kwargs: resolution,
    )
    monkeypatch.setattr(
        service,
        "_sequence_recovery_effects_complete",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", fake_cas)
    with store.begin_immediate() as conn:
        service._complete_sequence_recovery(
            conn,
            definition=recovery_definition,
            resolution=resolution,
            commit_sha="d" * 40,
            trusted=trusted,
            now=now,
        )
    assert cas_calls == 0


def test_complete_sequence_recovery_replay_is_idempotent_final(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sequence_id = "seq-replay-final"
    worktree_key = "b" * 64
    resolution = SequenceRecoveryResolution(
        sequence_id=sequence_id,
        ordinal=2,
        source_run_id="run-seq-final-source",
        recovery_id="rcv-seq-final",
        recovery_run_id="run-recovery-final",
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="1" * 64,
        reviewed_tree_sha256="2" * 40,
        commit_sha256="3" * 40,
        recorded_at=now_text,
    )
    trusted = RecoveryCheckpointTrustedTree(
        intent_sha256="4" * 64,
        reviewed_tree_sha256="2" * 40,
        reviewed_patch_sha256="1" * 64,
        parent_head="5" * 40,
        recorded_at=now_text,
    )
    recovery_definition = _recovery_definition_for_sequence(
        repo=Path("/tmp/repo"),
        source_run_id="run-seq-final-successor",
        sequence_id=sequence_id,
        ordinal=2,
        total_phases=2,
        is_final_phase=True,
        worktree_key=worktree_key,
    )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cas_calls = 0
    release_calls = 0

    def fake_cas(*args: object, **kwargs: object) -> bool:
        nonlocal cas_calls
        cas_calls += 1
        return True

    def fake_release(conn: object, *, worktree_key: str, now: datetime) -> None:
        nonlocal release_calls
        release_calls += 1

    monkeypatch.setattr(
        store,
        "get_sequence_recovery_resolution",
        lambda conn, **kwargs: resolution,
    )
    monkeypatch.setattr(
        service,
        "_sequence_recovery_effects_complete",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", fake_cas)
    monkeypatch.setattr(store, "release_reservation", fake_release)
    with store.begin_immediate() as conn:
        service._complete_sequence_recovery(
            conn,
            definition=recovery_definition,
            resolution=resolution,
            commit_sha="3" * 40,
            trusted=trusted,
            now=now,
        )
    assert cas_calls == 0
    assert release_calls == 0
