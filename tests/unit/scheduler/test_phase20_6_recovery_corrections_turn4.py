"""Phase 20.6 correction-turn tests for replay, tick fencing, seed reconcile, and reports."""

from __future__ import annotations

import hashlib
import json
import tempfile
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
    _checkpoint_git_success,
    checkpoint_git_commit_tree,
    checkpoint_git_rev_parse,
    recovery_git_apply_staged_patch,
    recovery_git_checkout_detach,
    recovery_git_create_private_ref,
    recovery_git_update_private_ref_cas,
    recovery_git_worktree_add,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _integration_intent_bytes,
    _integration_intent_sha256_from_bytes,
)
from ai_dev_loop.scheduler.application.recovery_integration_fencing import (
    RecoveryIntegrationTickContext,
)
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_worktree import ProductionRecoveryWorktreePort
from ai_dev_loop.scheduler.application.sequence_report import _recovery_checkpoint_fields
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    INTEGRATION_PENDING_RECOVERY_STATE_KIND,
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
    CursorWorkflowCheckpoint,
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


def test_integrate_recovery_pending_resumes_after_managed_side_only(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")

    recovery_id = "rcv-" + ("a" * 32)
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
    from ai_dev_loop.scheduler.application.recovery_artifacts import persist_recovery_definition

    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    recovery_run_id = "run-recovery-replay"
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(
        recovery_run_id,
        patch_rel,
        patch,
        max_bytes=len(patch) + 1,
    )
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
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, f"missing run {run_id}")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load_snapshot)

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
        definition.source_staged_patch_path,
        patch,
        max_bytes=len(patch) + 1,
    )
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
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-replay",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        review_result_sha256="4" * 64,
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
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
    )
    trusted_bytes = json.dumps(trusted.model_dump(mode="json"), indent=2, sort_keys=True).encode(
        "utf-8"
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
    from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence

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
    pending = pending.model_copy(
        update={
            "integration_intent_artifact_sha256": intent_sha,
            "integration_trusted_tree_artifact_sha256": hashlib.sha256(trusted_bytes).hexdigest(),
            "seed_evidence_artifact_sha256": stored_seed.sha256,
        }
    )
    with store.begin_immediate() as conn:
        store.update_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=pending,
            expected_version=1,
            now=now,
        )
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256
    assert checkpoint_git_rev_parse(repo, "HEAD") != head


def test_deferred_integration_rejects_expired_tick_lease(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    recovery_id = "rcv-" + ("c" * 32)
    source_run_id = "run-source-lease"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    pending = IntegrationPendingRecoveryState(
        recovery_id=recovery_id,
        version=1,
        updated_at=now_text,
        definition_sha256="d" * 64,
        definition_artifact_sha256="e" * 64,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        recovery_run_id="run-recovery-lease",
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
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
    reconcile = RecoveryReconcileService(
        store,
        artifacts,
        tick_context=RecoveryIntegrationTickContext(
            tick_owner_id="stale-owner",
            tick_lease_generation=99,
        ),
    )
    receipt = reconcile.integrate_deferred(recovery_id)
    assert receipt.action == "recovery_integration_failed"
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, recovery_id)
    assert str(row["state_kind"]) == INTEGRATION_PENDING_RECOVERY_STATE_KIND


def test_partial_worktree_service_reconciles_index_only_seed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("g" * 32)
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    worktree = tmp_path / "managed-wt"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    with tempfile.NamedTemporaryFile(
        prefix=".ai-dev-loop-recovery-patch-",
        suffix=".patch",
        dir=worktree,
        delete=False,
    ) as handle:
        patch_path = Path(handle.name)
        handle.write(patch)
    _checkpoint_git_success(
        ["apply", "--whitespace=nowarn", "--cached", str(patch_path)],
        cwd=worktree,
        context="recovery git apply",
    )
    patch_path.unlink(missing_ok=True)
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "base\n"

    staged_patch_file = tmp_path / "source.patch"
    staged_patch_file.write_bytes(patch)
    evidence = ProductionRecoveryWorktreePort().create_and_seed(
        definition,
        patch_path=staged_patch_file,
        worktree_path=worktree,
    )
    assert evidence.staged_patch_sha256 == patch_sha
    assert (worktree / "a.txt").read_text(encoding="utf-8") == "changed\n"


def test_recovery_checkpoint_fields_keep_git_tree_and_artifact_digest_distinct(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    git_tree = "a" * 40
    git_commit = "b" * 40
    recovery_id = "rcv-" + ("h" * 32)
    recovery_run_id = "run-recovery-report"
    intent_bytes = json.dumps(
        {
            "schema_version": 1,
            "recovery_id": recovery_id,
            "source_run_id": "run-source-report",
            "recovery_run_id": recovery_run_id,
            "accepted_outcome": "completed",
            "parent_head": "c" * 40,
            "source_tree_sha256": git_tree,
            "accepted_tree_sha256": git_tree,
            "reviewed_patch_sha256": "2" * 64,
            "review_result_sha256": "3" * 64,
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
            "reviewed_tree_sha256": git_tree,
            "reviewed_patch_sha256": "2" * 64,
            "parent_head": "c" * 40,
            "recorded_at": now_text,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    trusted_digest = hashlib.sha256(trusted_bytes).hexdigest()
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
        trusted_bytes,
        max_bytes=len(trusted_bytes) + 1,
    )
    artifacts.write_bytes(
        recovery_id,
        RECOVERY_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
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
        integrated_commit_sha256=git_commit,
        integrated_commit_sha256_prefix=git_commit[:8],
        integration_intent_artifact_sha256=intent_digest,
        integration_trusted_tree_artifact_sha256=trusted_digest,
    )
    with store.begin_immediate() as conn:
        store.insert_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=cleanup,
            now=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        )
    completed = CompletedState.model_construct(
        run_id=recovery_run_id,
        kind="completed",
        codex=CodexWorkflowCheckpoint.model_construct(latest_review_result_sha256="3" * 64),
    )
    resolution = SequenceRecoveryResolution(
        sequence_id="seq-report",
        ordinal=1,
        source_run_id="run-source-report",
        recovery_id=recovery_id,
        recovery_run_id=recovery_run_id,
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="2" * 64,
        reviewed_tree_sha256=git_tree,
        commit_sha256=git_commit,
        recorded_at=now_text,
    )

    def fake_load(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id == recovery_run_id:
            return completed, 1, None
        raise SchedulerEngineError(SchedulerEngineErrorKind.NOT_FOUND, "missing")

    monkeypatch.setattr(SqliteSchedulerStore, "load_validated_snapshot", fake_load)
    with store.begin_read() as conn:
        commit, tree, intent_digest, trusted_digest_loaded, review_sha = (
            _recovery_checkpoint_fields(
                store,
                artifacts,
                conn,
                resolution,
            )
        )
    assert commit == git_commit
    assert tree == git_tree
    assert trusted_digest_loaded == trusted_digest
    assert len(trusted_digest_loaded) == 64
    assert len(tree) == 40
    assert review_sha == "3" * 64
