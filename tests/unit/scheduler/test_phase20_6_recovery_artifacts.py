"""Artifact binding tests for Phase 20.6 recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.runners.git import (
    checkpoint_git_diff_cached_patch_bytes,
    compute_ephemeral_staged_tree_sha,
    recovery_git_create_private_ref,
    recovery_git_private_ref_exists,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    definition_digest_binding,
    load_recovery_definition,
)
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.domain.recovery import PreparedRecoveryState


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


def test_ephemeral_tree_sha_does_not_mutate_source_index(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    target = repo / "a.txt"
    target.write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    patch = checkpoint_git_diff_cached_patch_bytes(repo)
    subprocess.run(
        ["git", "restore", "--staged", "a.txt"], cwd=repo, check=True, capture_output=True
    )
    target.write_text("base\n", encoding="utf-8")
    before = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    tree_sha = compute_ephemeral_staged_tree_sha(repo, parent_head=head, patch_bytes=patch)
    after = subprocess.check_output(["git", "status", "--porcelain"], cwd=repo, text=True)
    assert before == after
    assert tree_sha


def test_private_ref_collision_is_refused(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)
    ref = "refs/ai-dev-loop/recovery/" + ("b" * 64)
    recovery_git_create_private_ref(repo, ref=ref, parent_head=head)
    assert recovery_git_private_ref_exists(repo, ref=ref)
    recovery_git_create_private_ref(repo, ref=ref, parent_head=head)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "second"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    other_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    with pytest.raises(Exception, match="collision"):
        recovery_git_create_private_ref(repo, ref=ref, parent_head=other_head)


def test_prepare_persists_authenticated_definition_binding(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduler_paths = {
        "db_path": isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3",
        "artifact_root": isolated_xdg / "state" / "ai_dev_loop" / "artifacts",
    }
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="artifact binding commit",
    )
    with store.begin_read() as conn:
        row = store.get_fresh_review_recovery(conn, prepared.recovery_id)
    state_payload = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))  # type: ignore[index]
    definition = load_recovery_definition(
        artifacts,
        prepared.recovery_id,
        definition_sha256=state_payload.definition_sha256,
        definition_artifact_sha256=state_payload.definition_artifact_sha256,
    )
    assert definition_digest_binding(definition) == state_payload.definition_sha256
