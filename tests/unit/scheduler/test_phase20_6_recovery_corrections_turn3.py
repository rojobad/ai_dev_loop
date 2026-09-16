"""Phase 20.6 correction-turn tests for hold lifecycle, abort proof, and ref delete."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)
from tests.unit.scheduler.test_phase20_6_recovery_integration_lifecycle import (
    _reacquire_target_reservation,
)

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_rev_parse,
    checkpoint_git_update_ref_cas,
    checkpoint_git_write_tree,
    recovery_git_create_private_ref,
    recovery_git_delete_ref,
    recovery_git_private_ref_peek,
    recovery_git_update_private_ref_cas,
)
from ai_dev_loop.scheduler.application.recovery_abort import RecoveryAbortService
from ai_dev_loop.scheduler.application.recovery_integration import RecoveryIntegrationService
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    IntegrationPendingRecoveryState,
    PreparedRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "x@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    tracked = path / "a.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=path, text=True).strip()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_abort_integration_pending_rejects_unauthenticated_branch_advance(
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
        commit_message="abort false integration commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    prepared_state = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
    advanced = checkpoint_git_rev_parse(git_repo, "HEAD")
    pending = IntegrationPendingRecoveryState(
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
    )
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.update_fresh_review_recovery(
            conn,
            recovery_id=prepared.recovery_id,
            state=pending,
            expected_version=prepared_state.version,
            now=now,
        )
    result = RecoveryAbortService(store, artifacts).abort(prepared.recovery_id)
    assert result.state_kind == "abort_pending"
    assert result.changed is True
    assert checkpoint_git_rev_parse(git_repo, "HEAD") == advanced


def test_standalone_cas_failure_preserves_checkpoint_hold(
    tmp_path: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    source_run_id = "run-source-hold"
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    _reacquire_target_reservation(
        store,
        worktree_key="wt-hold",
        run_id=source_run_id,
        repo_root=repo,
    )
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity_snapshot = GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
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
        message="hold test commit",
        identity=identity,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("d" * 32),
        source_run_id=source_run_id,
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256="0" * 64,
        review_result_sha256="1" * 64,
        commit_message="hold test commit",
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

    cas_attempts = 0

    def fail_cas_once(*args: object, **kwargs: object) -> None:
        nonlocal cas_attempts
        cas_attempts += 1
        if cas_attempts == 1:
            raise ValidationError("injected CAS failure")
        checkpoint_git_update_ref_cas(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.recovery_integration.checkpoint_git_update_ref_cas",
        fail_cas_once,
    )
    with pytest.raises(ValidationError, match="injected CAS failure"):
        service._integrate_target_standalone(
            intent=intent,
            intent_sha256="b" * 64,
            trusted=trusted,
            patch_path=repo / "missing.patch",
            target_root=repo,
            identity=identity,
            source_run_id=source_run_id,
            target_worktree_key="wt-hold",
            evidence={"tree_sha256": tree, "commit_sha256": commit_sha},
            recovery_version=1,
        )
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, source_run_id)
    commit_sha2 = service._integrate_target_standalone(
        intent=intent,
        intent_sha256="b" * 64,
        trusted=trusted,
        patch_path=repo / "missing.patch",
        target_root=repo,
        identity=identity,
        source_run_id=source_run_id,
        target_worktree_key="wt-hold",
        evidence={"tree_sha256": tree, "commit_sha256": commit_sha},
        recovery_version=1,
    )
    assert commit_sha2 == commit_sha
    with store.begin_read() as conn:
        assert not store.has_checkpoint_reconciliation_hold(conn, source_run_id)


def test_private_ref_delete_requires_expected_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    ref = "refs/ai-dev-loop/recovery/" + ("c" * 64)
    recovery_git_create_private_ref(repo, ref=ref, parent_head=head)
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentity(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    tree = checkpoint_git_write_tree(repo)
    new_sha = checkpoint_git_commit_tree(
        repo,
        tree_sha=tree,
        parent_sha=head,
        message="advance",
        identity=identity,
    )
    recovery_git_update_private_ref_cas(repo, ref=ref, new_sha=new_sha, old_sha=head)
    with pytest.raises(ValidationError):
        recovery_git_delete_ref(repo, ref=ref, expected_sha=head)
    assert recovery_git_private_ref_peek(repo, ref=ref) == new_sha
    recovery_git_delete_ref(repo, ref=ref, expected_sha=new_sha)
    assert recovery_git_private_ref_peek(repo, ref=ref) is None
