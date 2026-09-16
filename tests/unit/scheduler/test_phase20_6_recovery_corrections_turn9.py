"""Phase 20.6 correction-turn tests for abort proof, delta restart, and sequence holds."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_6_recovery_corrections_turn8 import (
    _CrashingGitCheckpointPort,
    _sequence_managed_integration_setup,
)
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _definition,
    _init_repo,
    _reacquire_target_reservation,
    _stage_change,
)

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    GitIdentity,
    _checkpoint_git_success,
    checkpoint_git_commit_tree,
    checkpoint_git_rev_parse,
    checkpoint_git_update_ref_cas,
    checkpoint_git_write_tree,
    recovery_git_apply_staged_patch,
    recovery_git_create_private_ref,
    recovery_git_update_private_ref_cas,
    recovery_git_worktree_add,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_abort import (
    RecoveryAbortService,
    abort_recovery,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    definition_digest_binding,
    persist_recovery_definition,
)
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _integration_intent_bytes,
    _integration_intent_sha256_from_bytes,
    _tree_delta_patch,
)
from ai_dev_loop.scheduler.application.recovery_integration_fencing import (
    RecoveryIntegrationTickContext,
)
from ai_dev_loop.scheduler.application.recovery_reconcile import RecoveryReconcileService
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    ABORT_PENDING_RECOVERY_STATE_KIND,
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    INTEGRATION_PENDING_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
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


def test_public_abort_after_target_cas_reconciles_before_finalization(
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    recovery_id = "rcv-" + ("a" * 32)
    source_run_id = "run-source-abort-cas"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha="1" * 64,
        tree_sha=tree,
    )
    definition = definition.model_copy(
        update={
            "source_run_id": source_run_id,
            "source_run_id_prefix": source_run_id[:8],
        }
    )
    definition = definition.model_copy(
        update={"definition_sha256": definition_digest_binding(definition)}
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    now = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
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
    _checkpoint_git_success(["reset", "--hard", commit_sha], cwd=repo, context="reset after cas")
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=source_run_id,
        recovery_run_id="run-recovery-abort-cas",
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
        recovery_run_id="run-recovery-abort-cas",
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
    result = abort_recovery(
        recovery_id,
        db_path=isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3",
        artifact_root=isolated_xdg / "state" / "ai_dev_loop" / "artifacts",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, recovery_id)
    assert str(row["state_kind"]) == CLEANUP_PENDING_RECOVERY_STATE_KIND
    assert result.state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND


def _delta_integration_setup(
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
    bytes,
    bytes,
]:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    source_patch, source_tree, source_patch_sha = _stage_change(repo, "source\n")
    accepted_patch, accepted_tree, accepted_patch_sha = _stage_change(repo, "accepted\n")
    recovery_id = "rcv-" + ("d" * 32)
    recovery_run_id = "run-recovery-delta"
    source_run_id = "run-source-delta"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=source_patch_sha,
        tree_sha=source_tree,
    )
    definition = definition.model_copy(
        update={
            "source_run_id": source_run_id,
            "source_run_id_prefix": source_run_id[:8],
        }
    )
    definition = definition.model_copy(
        update={"definition_sha256": definition_digest_binding(definition)}
    )
    stored = persist_recovery_definition(artifacts, recovery_id, definition)
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=source_run_id,
        repo_root=repo,
    )
    now = datetime(2026, 9, 15, 16, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(
        recovery_run_id, patch_rel, accepted_patch, max_bytes=len(accepted_patch) + 1
    )
    review_sha = "4" * 64

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
                        reviewer_session_id="00000000-0000-4000-8000-000000000099",
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
        tree_sha=accepted_tree,
        parent_sha=head,
        message=definition.integration.commit_message,
        identity=git_identity,
    )
    recovery_git_update_private_ref_cas(
        repo,
        ref=definition.private_ref,
        new_sha=managed_commit,
        old_sha=head,
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
        target_branch_ref="refs/heads/main",
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
    service = RecoveryIntegrationService(store, artifacts, now_factory=lambda: now)
    return (
        store,
        artifacts,
        service,
        recovery_id,
        definition,
        accepted_tree,
        head,
        accepted_patch,
        source_patch,
    )


def test_delta_restart_finishes_worktree_when_index_already_accepted(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        store,
        artifacts,
        service,
        recovery_id,
        definition,
        accepted_tree,
        head,
        accepted_patch,
        source_patch,
    ) = _delta_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    repo = Path(definition.repository.root)
    recovery_git_apply_staged_patch(repo, patch_bytes=source_patch)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "source\n"
    delta = _tree_delta_patch(
        definition.source_staged_tree_sha256,
        accepted_tree,
        repo,
    )
    assert delta
    delta_path = repo / ".delta-only.patch"
    delta_path.write_bytes(delta)
    _checkpoint_git_success(
        ["apply", "--whitespace=nowarn", "--cached", str(delta_path)],
        cwd=repo,
        context="apply delta to index only",
    )
    delta_path.unlink(missing_ok=True)
    assert checkpoint_git_write_tree(repo) == accepted_tree
    assert (repo / "a.txt").read_text(encoding="utf-8") == "source\n"
    evidence_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence.pop("delta_applied", None)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256
    assert checkpoint_git_write_tree(repo) == accepted_tree
    assert (repo / "a.txt").read_text(encoding="utf-8") == "accepted\n"
    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert saved.get("delta_applied") == "true"


def test_delta_restart_after_crash_before_evidence_publication(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, recovery_id, definition, accepted_tree, head, _, source_patch = (
        _delta_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    )
    repo = Path(definition.repository.root)
    recovery_git_apply_staged_patch(repo, patch_bytes=source_patch)
    delta = _tree_delta_patch(
        definition.source_staged_tree_sha256,
        accepted_tree,
        repo,
    )
    assert delta
    delta_path = repo / ".delta-only.patch"
    delta_path.write_bytes(delta)
    _checkpoint_git_success(
        ["apply", "--whitespace=nowarn", "--cached", str(delta_path)],
        cwd=repo,
        context="apply delta to index only",
    )
    delta_path.unlink(missing_ok=True)
    assert (repo / "a.txt").read_text(encoding="utf-8") == "source\n"
    evidence_path = artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence.pop("delta_applied", None)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    original_persist = service._persist_integration_evidence
    crashed = {"value": False}

    def crash_on_delta_persist(
        recovery_id_arg: str,
        evidence_state: dict[str, str | None],
    ) -> None:
        if evidence_state.get("delta_applied") == "true" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("crash before delta evidence publication")
        original_persist(recovery_id_arg, evidence_state)

    monkeypatch.setattr(service, "_persist_integration_evidence", crash_on_delta_persist)
    with pytest.raises(RuntimeError, match="crash before delta evidence"):
        service.integrate_recovery_pending(recovery_id)
    assert checkpoint_git_write_tree(repo) == accepted_tree
    assert (repo / "a.txt").read_text(encoding="utf-8") == "accepted\n"
    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert "delta_applied" not in saved
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256
    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert saved.get("delta_applied") == "true"


def test_non_final_sequence_cas_failure_preserves_checkpoint_hold(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, recovery_id, definition, _, _, head = (
        _sequence_managed_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    )
    source_run_id = definition.source_run_id
    cas_attempts = 0

    def fail_cas_once(*args: object, **kwargs: object) -> None:
        nonlocal cas_attempts
        cas_attempts += 1
        if cas_attempts == 1:
            raise ValidationError("injected sequence CAS failure")
        checkpoint_git_update_ref_cas(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.git_checkpoint.checkpoint_git_update_ref_cas",
        fail_cas_once,
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, recovery_id)
    pending = IntegrationPendingRecoveryState.model_validate_json(str(row["state_payload"]))
    with pytest.raises(ValidationError, match="injected sequence CAS failure"):
        service._resume_integration_from_persisted_intent(
            recovery_id,
            pending,
            definition,
            skip_frozen_source_checks=True,
        )
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    monkeypatch.setattr(
        service, "_sequence_recovery_effects_complete", lambda *args, **kwargs: True
    )
    cleanup = service.integrate_recovery_pending(recovery_id)
    assert cleanup.integrated_commit_sha256
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    assert checkpoint_git_rev_parse(Path(definition.repository.root), "refs/heads/main") != head


def test_non_final_sequence_abort_after_cas_preserves_hold_until_reconciled(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, artifacts, service, recovery_id, definition, _, _, head = (
        _sequence_managed_integration_setup(tmp_path, isolated_xdg, monkeypatch)
    )
    repo = Path(definition.repository.root)
    source_run_id = definition.source_run_id
    crashing = RecoveryIntegrationService(
        store,
        artifacts,
        git_checkpoint=_CrashingGitCheckpointPort("commit"),
        now_factory=service._now_factory,
    )
    with pytest.raises(RuntimeError, match="crash after commit evidence"):
        crashing.integrate_recovery_pending(recovery_id)
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    result = RecoveryAbortService(store, artifacts).abort(recovery_id)
    assert result.state_kind == INTEGRATION_PENDING_RECOVERY_STATE_KIND
    assert result.changed is True
    abort_path = artifacts.run_root(recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
    assert abort_path.is_file()
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    with pytest.raises(SchedulerEngineError, match="recovery abort intent blocks integration"):
        service.integrate_recovery_pending(recovery_id)
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == head
    now = service._now_factory()
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner",
            now=now,
            ttl_seconds=300,
        )
    assert lease is not None
    generation, _ = lease
    reconcile = RecoveryReconcileService(
        store,
        artifacts,
        now_factory=service._now_factory,
        tick_context=RecoveryIntegrationTickContext(
            tick_owner_id="tick-owner",
            tick_lease_generation=generation,
        ),
    )
    with store.begin_read() as conn:
        receipt = reconcile.reconcile_recovery(conn, recovery_id)
    assert receipt is not None
    assert receipt.action == "recovery_pre_cas_abort_hold_reconciled"
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    abort_result = RecoveryAbortService(store, artifacts).abort(recovery_id)
    assert abort_result.state_kind == ABORT_PENDING_RECOVERY_STATE_KIND
    assert checkpoint_git_rev_parse(repo, "refs/heads/main") == head
