"""Disposable-repository integration lifecycle tests for Phase 20.6."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_dev_loop.runners.git import (
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_rev_parse,
    checkpoint_git_write_tree,
    recovery_git_apply_staged_patch,
    recovery_git_create_private_ref,
    recovery_git_private_ref_peek,
    recovery_git_worktree_add,
)
from ai_dev_loop.scheduler.application.contracts import ReservationStatus, SchedulerEngineError
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    definition_digest_binding,
    load_recovery_seed_evidence,
    persist_recovery_definition,
)
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _tree_delta_patch,
)
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    FreshReviewRecoveryDefinition,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    RecoveryIntegrationPolicy,
)
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    WorkflowLimits,
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


def _reacquire_target_reservation(
    store: SqliteSchedulerStore,
    *,
    worktree_key: str,
    run_id: str,
    repo_root: Path,
) -> None:
    now = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "blocked",
                "{}",
                "0" * 64,
                1,
                "0" * 64,
                worktree_key,
                now_text,
                now_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_repository_reservations(
                worktree_key, run_id, repository_root, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                worktree_key,
                run_id,
                str(repo_root.resolve()),
                ReservationStatus.RELEASED.value,
                now_text,
                now_text,
            ),
        )
        store.ensure_recovery_target_reservation(
            conn,
            source_run_id=run_id,
            worktree_key=worktree_key,
            repository_root=str(repo_root.resolve()),
            now=now,
        )


def _stage_change(repo: Path, text: str) -> tuple[bytes, str, str]:
    (repo / "a.txt").write_text(text, encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    patch = checkpoint_git_diff_cached_patch_bytes(repo)
    tree = checkpoint_git_write_tree(repo)
    patch_sha = hashlib.sha256(patch).hexdigest()
    subprocess.run(
        ["git", "restore", "--staged", "a.txt"], cwd=repo, check=True, capture_output=True
    )
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    return patch, tree, patch_sha


def _cursor_binding() -> CursorBinding:
    return CursorBinding(
        command="agent",
        model="composer-2.5-fast",
        output_format="stream-json",
        force=True,
        trust_workspace=True,
        sandbox="disabled",
    )


def _workflow_limits() -> WorkflowLimits:
    return WorkflowLimits(
        max_review_iterations=3,
        stage_mode="all",
        cursor_timeout_minutes=30,
        codex_timeout_minutes=30,
        require_clean_worktree=True,
    )


def _definition(
    *,
    recovery_id: str,
    repo: Path,
    head: str,
    patch_sha: str,
    tree_sha: str,
) -> FreshReviewRecoveryDefinition:
    worktree_key = hashlib.sha256(str(repo.resolve()).encode()).hexdigest()
    private_ref = "refs/ai-dev-loop/recovery/" + hashlib.sha256(recovery_id.encode()).hexdigest()
    base_kwargs = {
        "recovery_id": recovery_id,
        "definition_sha256": "0" * 64,
        "source_run_id": "run-source",
        "source_run_id_prefix": "run-sour",
        "repository": RepositoryBinding(
            root=str(repo.resolve()),
            git_common_dir=str((repo / ".git").resolve()),
            git_dir=str((repo / ".git").resolve()),
            branch="main",
            initial_head=head,
            worktree_key=worktree_key,
        ),
        "target_branch_ref": "refs/heads/main",
        "parent_head": head,
        "source_staged_patch_path": "recovery/source-staged.patch",
        "source_staged_patch_sha256": patch_sha,
        "source_staged_tree_sha256": tree_sha,
        "plan_prompt": PlanPromptBinding(
            plan_repository_path="docs/plan.md",
            prompt_source_repository_path="docs/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256="b" * 64,
            prompt_artifact_path="plan/prompt.txt",
            prompt_sha256="c" * 64,
        ),
        "effective_config": EffectiveConfigBinding(
            effective_config_artifact_path="config/effective.yaml",
            effective_config_sha256="d" * 64,
            source_config_artifact_path="config/source.yaml",
            source_config_sha256="e" * 64,
        ),
        "codex": FreshCodexReviewerBinding(
            review_model="gpt-test",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-changes",
            sandbox="workspace-write",
            binding_artifact_path="codex/binding.json",
            binding_sha256="f" * 64,
        ),
        "cursor": _cursor_binding(),
        "workflow": _workflow_limits(),
        "controller": ControllerBinding(
            controller_session_id="00000000-0000-4000-8000-000000000001"
        ),
        "integration": RecoveryIntegrationPolicy(commit_message="recovery integration commit"),
        "managed_worktree_path_token": hashlib.sha256(recovery_id.encode()).hexdigest(),
        "private_ref": private_ref,
        "recovery_worktree_key": hashlib.sha256(f"{recovery_id}:managed".encode()).hexdigest(),
        "prepared_at": "2026-09-15T12:00:00.000000Z",
    }
    digest = definition_digest_binding(FreshReviewRecoveryDefinition(**base_kwargs))
    base_kwargs["definition_sha256"] = digest
    return FreshReviewRecoveryDefinition(**base_kwargs)


def test_tree_delta_is_none_for_unchanged_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    tree = checkpoint_git_write_tree(repo)
    assert _tree_delta_patch(tree, tree, repo) is None


def test_tree_delta_applies_for_changed_tree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source_tree = checkpoint_git_write_tree(repo)
    patch, accepted_tree, _ = _stage_change(repo, "accepted\n")
    assert source_tree != accepted_tree
    delta = _tree_delta_patch(source_tree, accepted_tree, repo)
    assert delta
    recovery_git_apply_staged_patch(repo, patch_bytes=delta)
    assert checkpoint_git_write_tree(repo) == accepted_tree


def test_seed_tampering_is_refused(tmp_path: Path, isolated_xdg: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "changed\n")
    recovery_id = "rcv-" + ("a" * 32)
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    persist_recovery_definition(artifacts, recovery_id, definition)
    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(tmp_path / "wt"),
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=definition.repository.git_common_dir,
        managed_git_dir=definition.repository.git_dir,
        managed_branch="HEAD",
        pre_seed_admission_status="branch=HEAD\n",
    )
    artifacts.write_bytes(
        recovery_id,
        "recovery/seed-evidence.json",
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )
    tampered = definition.model_copy(
        update={"source_staged_patch_sha256": "0" * 64},
    )
    with pytest.raises(SchedulerEngineError, match="patch digest"):
        load_recovery_seed_evidence(artifacts, recovery_id, definition=tampered)


def test_integrate_target_standalone_advances_branch(tmp_path: Path, isolated_xdg: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    source_patch, source_tree, source_patch_sha = _stage_change(repo, "source\n")
    accepted_patch, accepted_tree, accepted_patch_sha = _stage_change(repo, "accepted\n")
    recovery_id = "rcv-" + ("b" * 32)
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=source_patch_sha,
        tree_sha=source_tree,
    )
    definition = definition.model_copy(
        update={
            "source_staged_patch_sha256": source_patch_sha,
            "source_staged_tree_sha256": source_tree,
        }
    )
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    persist_recovery_definition(artifacts, recovery_id, definition)
    artifacts.write_bytes(
        recovery_id,
        definition.source_staged_patch_path,
        source_patch,
        max_bytes=len(source_patch) + 1,
    )
    recovery_git_apply_staged_patch(repo, patch_bytes=source_patch)

    worktree = tmp_path / "managed"
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
        managed_git_common_dir=definition.repository.git_common_dir,
        managed_git_dir=str(worktree / ".git"),
        managed_branch="HEAD",
        pre_seed_admission_status="branch=HEAD\n",
    )
    artifacts.write_bytes(
        recovery_id,
        "recovery/seed-evidence.json",
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )

    run_id = "run-recovery"
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(
        run_id,
        patch_rel,
        accepted_patch,
        max_bytes=len(accepted_patch) + 1,
    )
    review_rel = "codex/reviews/01.json"
    review_bytes = b'{"ok": true}\n'
    artifacts.write_bytes(
        run_id,
        review_rel,
        review_bytes,
        max_bytes=len(review_bytes) + 1,
    )
    run_root = artifacts.run_root(run_id)
    review_sha = hashlib.sha256(review_bytes).hexdigest()

    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=definition.source_run_id,
        recovery_run_id=run_id,
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
        target_git_common_dir=definition.repository.git_common_dir,
        target_git_dir=definition.repository.git_dir,
        managed_repository_root=str(worktree),
        managed_git_common_dir=seed.managed_git_common_dir,
        managed_git_dir=seed.managed_git_dir,
        git_identity=identity,
        recorded_at=now_text,
    )
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=definition.source_run_id,
        repo_root=repo,
    )
    service = RecoveryIntegrationService(store, artifacts)
    trusted_tree = RecoveryCheckpointTrustedTree(
        intent_sha256="a" * 64,
        reviewed_tree_sha256=accepted_tree,
        reviewed_patch_sha256=accepted_patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    commit_sha = service._integrate_target(
        intent=intent,
        intent_sha256="a" * 64,
        trusted=trusted_tree,
        patch_path=run_root / patch_rel,
        source_patch_path=artifacts.run_root(recovery_id) / definition.source_staged_patch_path,
        source_staged_patch_sha256=source_patch_sha,
        managed_root=worktree,
        definition_parent_head=head,
        source_run_id=definition.source_run_id,
        target_worktree_key=definition.repository.worktree_key,
        sequence_is_final=True,
        recovery_version=1,
    )
    assert checkpoint_git_rev_parse(repo, "HEAD") == commit_sha
    assert checkpoint_git_rev_parse(worktree, "HEAD") == commit_sha
    assert recovery_git_private_ref_peek(repo, ref=definition.private_ref) == commit_sha


def test_integrate_target_unchanged_accepted_tree(tmp_path: Path, isolated_xdg: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    patch, tree, patch_sha = _stage_change(repo, "same\n")
    recovery_id = "rcv-" + ("c" * 32)
    definition = _definition(
        recovery_id=recovery_id,
        repo=repo,
        head=head,
        patch_sha=patch_sha,
        tree_sha=tree,
    )
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    persist_recovery_definition(artifacts, recovery_id, definition)
    artifacts.write_bytes(
        recovery_id,
        definition.source_staged_patch_path,
        patch,
        max_bytes=len(patch) + 1,
    )
    recovery_git_apply_staged_patch(repo, patch_bytes=patch)

    worktree = tmp_path / "managed"
    recovery_git_create_private_ref(repo, ref=definition.private_ref, parent_head=head)
    recovery_git_worktree_add(repo, worktree_path=worktree, parent_head=head)
    recovery_git_apply_staged_patch(worktree, patch_bytes=patch)

    seed = RecoverySeedEvidence(
        parent_head=head,
        staged_patch_sha256=patch_sha,
        staged_tree_sha256=tree,
        worktree_path=str(worktree),
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        managed_git_common_dir=definition.repository.git_common_dir,
        managed_git_dir=str(worktree / ".git"),
        managed_branch="HEAD",
        pre_seed_admission_status="branch=HEAD\n",
    )
    artifacts.write_bytes(
        recovery_id,
        "recovery/seed-evidence.json",
        json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8"),
        max_bytes=4096,
    )

    run_id = "run-recovery-unchanged"
    patch_rel = "git/diffs/01.patch"
    artifacts.write_bytes(run_id, patch_rel, patch, max_bytes=len(patch) + 1)
    review_bytes = b'{"ok": true}\n'
    artifacts.write_bytes(
        run_id,
        "codex/reviews/01.json",
        review_bytes,
        max_bytes=len(review_bytes) + 1,
    )
    review_sha = hashlib.sha256(review_bytes).hexdigest()
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id=recovery_id,
        source_run_id=definition.source_run_id,
        recovery_run_id=run_id,
        accepted_outcome="completed",
        parent_head=head,
        source_tree_sha256=tree,
        accepted_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        review_result_sha256=review_sha,
        commit_message=definition.integration.commit_message,
        target_branch_ref=definition.target_branch_ref,
        private_ref=definition.private_ref,
        target_repository_root=str(repo),
        target_git_common_dir=definition.repository.git_common_dir,
        target_git_dir=definition.repository.git_dir,
        managed_repository_root=str(worktree),
        managed_git_common_dir=seed.managed_git_common_dir,
        managed_git_dir=seed.managed_git_dir,
        git_identity=identity,
        recorded_at=now_text,
    )
    trusted_tree = RecoveryCheckpointTrustedTree(
        intent_sha256="a" * 64,
        reviewed_tree_sha256=tree,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at=now_text,
    )
    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    _reacquire_target_reservation(
        store,
        worktree_key=definition.repository.worktree_key,
        run_id=definition.source_run_id,
        repo_root=repo,
    )
    service = RecoveryIntegrationService(store, artifacts)
    commit_sha = service._integrate_target(
        intent=intent,
        intent_sha256="a" * 64,
        trusted=trusted_tree,
        patch_path=artifacts.run_root(run_id) / patch_rel,
        source_patch_path=artifacts.run_root(recovery_id) / definition.source_staged_patch_path,
        source_staged_patch_sha256=patch_sha,
        managed_root=worktree,
        definition_parent_head=head,
        source_run_id=definition.source_run_id,
        target_worktree_key=definition.repository.worktree_key,
        sequence_is_final=True,
        recovery_version=1,
    )
    assert _tree_delta_patch(tree, tree, repo) is None
    assert checkpoint_git_rev_parse(repo, "HEAD") == commit_sha
    assert checkpoint_git_rev_parse(worktree, "HEAD") == commit_sha


def test_tree_delta_supports_binary_file_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    source_tree = checkpoint_git_write_tree(repo)
    binary = repo / "bin.dat"
    binary.write_bytes(b"\x00\x01binary-content\xff")
    subprocess.run(["git", "add", "bin.dat"], cwd=repo, check=True, capture_output=True)
    accepted_tree = checkpoint_git_write_tree(repo)
    delta = _tree_delta_patch(source_tree, accepted_tree, repo)
    assert delta
    assert b"GIT binary patch" in delta or b"bin.dat" in delta
