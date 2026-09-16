"""Pre-CAS abort hold convergence tests for Phase 20.6.5 rollover."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _init_repo,
    _reacquire_target_reservation,
    _stage_change,
)

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_rev_parse,
    checkpoint_git_update_ref_cas,
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
from ai_dev_loop.scheduler.application.git_admission import discover_repository_bounded
from ai_dev_loop.scheduler.application.rollover_abort import RolloverAbortService
from ai_dev_loop.scheduler.application.rollover_artifacts import (
    load_rollover_integration_intent,
    load_rollover_integration_trusted_tree,
    persist_rollover_definition,
    rollover_definition_digest_binding,
)
from ai_dev_loop.scheduler.application.rollover_integration import (
    RolloverIntegrationService,
    _integration_intent_bytes,
    _integration_intent_sha256_from_bytes,
)
from ai_dev_loop.scheduler.application.rollover_integration_fencing import (
    RolloverIntegrationTickContext,
)
from ai_dev_loop.scheduler.application.rollover_reconcile import RolloverReconcileService
from ai_dev_loop.scheduler.application.rollover_worktree import RolloverSeedEvidence
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.rollover import (
    ABORT_PENDING_ROLLOVER_STATE_KIND,
    INTEGRATION_PENDING_ROLLOVER_STATE_KIND,
    ROLLOVER_INTEGRATION_INTENT_ARTIFACT,
    ROLLOVER_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    AuthenticatedRolloverDefinition,
    IntegrationPendingRolloverState,
    RolloverCheckpointTrustedTree,
    RolloverIntegrationIntent,
    RolloverIntegrationPolicy,
)
from ai_dev_loop.scheduler.domain.state import (
    CodexWorkflowCheckpoint,
    CompletedState,
    ControllerBinding,
    CursorBinding,
    CursorWorkflowCheckpoint,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    WorkflowLimits,
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


def _definition(
    *,
    rollover_id: str,
    repo: Path,
    head: str,
    patch_sha: str,
    tree_sha: str,
) -> AuthenticatedRolloverDefinition:
    digest = "b" * 64
    repo_root = str(repo.resolve())
    worktree_key = hashlib.sha256(repo_root.encode()).hexdigest()
    return AuthenticatedRolloverDefinition(
        rollover_id=rollover_id,
        definition_sha256=digest,
        source_run_id="run-source-rollover-abort",
        source_run_id_prefix="run-sour",
        repository=RepositoryBinding(
            root=repo_root,
            git_common_dir=str(repo / ".git"),
            git_dir=str(repo / ".git"),
            branch="main",
            initial_head=head,
            worktree_key=worktree_key,
        ),
        target_branch_ref="refs/heads/main",
        parent_head=head,
        source_staged_patch_path="rollover/source-staged.patch",
        source_staged_patch_sha256=patch_sha,
        source_staged_tree_sha256=tree_sha,
        source_final_review_result_path="rollover/source-final-review-result.json",
        source_final_review_result_sha256="4" * 64,
        plan_prompt=PlanPromptBinding(
            plan_repository_path="docs/plan.md",
            prompt_source_repository_path="docs/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256=digest,
            prompt_artifact_path="plan/prompt.txt",
            prompt_sha256=digest,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path="config/effective.yaml",
            effective_config_sha256=digest,
            source_config_artifact_path="config/source.yaml",
            source_config_sha256=digest,
        ),
        codex=FreshCodexReviewerBinding(
            review_model="gpt-test",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-changes",
            sandbox="workspace-write",
            binding_artifact_path="codex/binding.json",
            binding_sha256=digest,
        ),
        cursor=CursorBinding(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowLimits(
            max_review_iterations=3,
            stage_mode="all",
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
            require_clean_worktree=True,
        ),
        controller=ControllerBinding(controller_session_id="00000000-0000-4000-8000-000000000001"),
        integration=RolloverIntegrationPolicy(commit_message="rollover abort cas commit"),
        managed_worktree_path_token=hashlib.sha256(b"token").hexdigest(),
        private_ref="refs/ai-dev-loop/rollover/" + ("c" * 64),
        rollover_worktree_key=hashlib.sha256(b"managed").hexdigest(),
        prepared_at="2026-09-15T15:00:00.000000Z",
    )


def _rollover_integration_pending_setup(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    SqliteSchedulerStore,
    ProtectedArtifactStore,
    RolloverIntegrationService,
    str,
    AuthenticatedRolloverDefinition,
    str,
]:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    rollover_id = "rol-" + ("a" * 32)
    source_run_id = "run-source-rollover-abort"
    rollover_run_id = "run-rollover-abort-cas"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        rollover_id=rollover_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    definition = definition.model_copy(
        update={"definition_sha256": rollover_definition_digest_binding(definition)}
    )
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    stored = persist_rollover_definition(artifacts, rollover_id, definition)
    now = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    patch_rel = "git/diffs/01.patch"
    review_rel = "codex/reviews/01.json"
    review_payload = json.dumps(
        {
            "has_actionable_findings": False,
            "findings_count": 0,
            "highest_severity": None,
            "cursor_fix_prompt": None,
            "tests_status": "passed",
        },
        sort_keys=True,
    ).encode("utf-8")
    artifacts.write_bytes(rollover_run_id, patch_rel, patch, max_bytes=len(patch) + 1)
    artifacts.write_bytes(
        rollover_run_id,
        review_rel,
        review_payload,
        max_bytes=len(review_payload) + 1,
    )
    review_sha = hashlib.sha256(review_payload).hexdigest()

    def fake_load_snapshot(
        self: SqliteSchedulerStore,
        conn: object,
        run_id: str,
    ) -> tuple[object, int, object]:
        if run_id == rollover_run_id:
            state = CompletedState.model_construct(
                run_id=run_id,
                kind="completed",
                cursor=CursorWorkflowCheckpoint.model_construct(
                    staged_patch_path=patch_rel,
                    staged_patch_sha256=patch_sha,
                ),
                codex=CodexWorkflowCheckpoint.model_construct(
                    latest_review_result_path=review_rel,
                    latest_review_result_sha256=review_sha,
                    reviewer_session_id="00000000-0000-4000-8000-000000000099",
                ),
            )
            return state, 1, None
        if run_id == source_run_id:
            return CompletedState.model_construct(run_id=run_id, kind="completed"), 1, None
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            f"missing run {run_id}",
        )

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
    intent = RolloverIntegrationIntent(
        rollover_id=rollover_id,
        source_run_id=source_run_id,
        rollover_run_id=rollover_run_id,
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
    trusted = RolloverCheckpointTrustedTree(
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
        rollover_id,
        definition.source_staged_patch_path,
        patch,
        max_bytes=len(patch) + 1,
    )
    artifacts.write_bytes(
        rollover_id,
        ROLLOVER_INTEGRATION_INTENT_ARTIFACT,
        intent_bytes,
        max_bytes=len(intent_bytes) + 1,
    )
    artifacts.write_bytes(
        rollover_id,
        ROLLOVER_INTEGRATION_TRUSTED_TREE_ARTIFACT,
        trusted_bytes,
        max_bytes=len(trusted_bytes) + 1,
    )
    recovery_git_checkout_detach(worktree, commit_sha=managed_commit)
    managed_admission = discover_repository_bounded(worktree)
    seed = RolloverSeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(worktree),
        private_ref=private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=managed_admission.git_common_dir,
        managed_git_dir=managed_admission.git_dir,
        managed_branch="HEAD",
        pre_seed_admission_status="",
    )
    seed_bytes = json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8")
    stored_seed = artifacts.write_bytes(
        rollover_id,
        "rollover/seed-evidence.json",
        seed_bytes,
        max_bytes=4096,
    )
    pending = IntegrationPendingRolloverState(
        rollover_id=rollover_id,
        version=1,
        updated_at=now_text,
        definition_sha256=definition.definition_sha256,
        definition_artifact_sha256=stored.sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id[:8],
        rollover_run_id=rollover_run_id,
        started_at=now_text,
        accepted_outcome="completed",
        residual_risk=False,
        integration_intent_artifact_sha256=intent_sha,
        integration_trusted_tree_artifact_sha256=hashlib.sha256(trusted_bytes).hexdigest(),
        seed_evidence_artifact_sha256=stored_seed.sha256,
    )
    with store.begin_immediate() as conn:
        store.insert_authenticated_rollover(
            conn,
            rollover_id=rollover_id,
            state=pending,
            now=now,
        )
    service = RolloverIntegrationService(
        store,
        artifacts,
        now_factory=lambda: now,
    )
    return store, artifacts, service, rollover_id, definition, head


def test_abort_after_pre_cas_hold_converges_via_tick_reconcile(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, rollover_id, definition, head = _rollover_integration_pending_setup(
        tmp_path,
        isolated_xdg,
        monkeypatch,
    )
    repo = Path(definition.repository.root)
    source_run_id = definition.source_run_id
    with store.begin_read() as conn:
        row = store.get_authenticated_rollover(conn, rollover_id)
    pending = IntegrationPendingRolloverState.model_validate_json(str(row["state_payload"]))
    intent = load_rollover_integration_intent(
        artifacts,
        rollover_id,
        expected_sha256=pending.integration_intent_artifact_sha256 or "",
    )
    trusted, trusted_sha = load_rollover_integration_trusted_tree(
        artifacts,
        rollover_id,
        expected_sha256=pending.integration_trusted_tree_artifact_sha256 or "",
    )
    identity = GitIdentity(
        author_name=intent.git_identity.author_name,
        author_email=intent.git_identity.author_email,
        author_date=intent.git_identity.author_date,
        committer_name=intent.git_identity.committer_name,
        committer_email=intent.git_identity.committer_email,
        committer_date=intent.git_identity.committer_date,
    )
    commit_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=trusted.reviewed_tree_sha256,
        parent_sha=intent.parent_head,
        message=intent.commit_message,
        identity=identity,
    )
    cas_attempts = 0

    def fail_cas_once(*args: object, **kwargs: object) -> None:
        nonlocal cas_attempts
        cas_attempts += 1
        if cas_attempts == 1:
            raise ValidationError("injected CAS failure")
        checkpoint_git_update_ref_cas(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.rollover_integration.checkpoint_git_update_ref_cas",
        fail_cas_once,
    )
    with pytest.raises(ValidationError, match="injected CAS failure"):
        service._integrate_target_standalone(
            intent=intent,
            intent_sha256=pending.integration_intent_artifact_sha256 or "",
            trusted=trusted,
            patch_path=repo / "missing.patch",
            target_root=repo,
            identity=identity,
            source_run_id=source_run_id,
            target_worktree_key=definition.repository.worktree_key,
            evidence={"tree_sha256": trusted.reviewed_tree_sha256, "commit_sha256": commit_sha},
            rollover_version=pending.version,
        )
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    abort_result = RolloverAbortService(store, artifacts).abort(rollover_id)
    assert abort_result.state_kind == INTEGRATION_PENDING_ROLLOVER_STATE_KIND
    assert abort_result.changed is True
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == head
    now = service._now_factory()
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner-rollover",
            now=now,
            ttl_seconds=300,
        )
    assert lease is not None
    generation, _ = lease
    reconcile = RolloverReconcileService(
        store,
        artifacts,
        now_factory=service._now_factory,
        tick_context=RolloverIntegrationTickContext(
            tick_owner_id="tick-owner-rollover",
            tick_lease_generation=generation,
        ),
    )
    with store.begin_read() as conn:
        receipt = reconcile.reconcile_rollover(conn, rollover_id)
    assert receipt is not None
    assert receipt.action == "rollover_pre_cas_abort_hold_reconciled"
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    follow_up = RolloverAbortService(store, artifacts).abort(rollover_id)
    assert follow_up.state_kind == ABORT_PENDING_ROLLOVER_STATE_KIND
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == head
