"""Unit tests for correction Git staging safety."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    validate_correction_pre_cursor,
    validate_no_preexisting_staged_paths,
    validate_staged_patch_matches_artifact,
)
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    atomic_write_text,
    utc_now,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sample_state(repo_root: Path) -> RunState:
    now = utc_now()
    return RunState(
        run_id="fixture-project-20260704T134512Z-abc123",
        project=ProjectRef(name="fixture-project"),
        status=RunStatus.STAGING,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root=str(repo_root),
            git_common_dir=str(repo_root / ".git"),
            git_dir=str(repo_root / ".git"),
            branch="main",
            initial_head="abc123",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256="a" * 64,
        ),
        prompt=PromptState(
            source_repository_path="docs/plans/prompt_sample-plan.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            session_model=None,
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        iterations=[
            {
                "number": 1,
                "kind": "initial_implementation",
                "git": {"staged_diff_path": "git/diffs/01.patch"},
            }
        ],
    )


@pytest.fixture
def tiny_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    plan = repo / "docs/plans/sample-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# plan\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def test_first_iteration_rejects_preexisting_staged_paths(tiny_repo: Path) -> None:
    staged_file = tiny_repo / "staged.txt"
    staged_file.write_text("staged\n", encoding="utf-8")
    _git(tiny_repo, "add", "staged.txt")
    with pytest.raises(ValidationError, match="pre-existing staged"):
        validate_no_preexisting_staged_paths(("staged.txt",))


def test_validate_staged_patch_matches_artifact_detects_drift(
    tiny_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_artifact = tmp_path / "01.patch"
    patch_artifact.write_text("recorded-patch\n", encoding="utf-8")
    monkeypatch.setattr(
        "ai_dev_loop.runners.git.git_diff_cached_patch",
        lambda _repo: "current-patch\n",
    )
    with pytest.raises(ValidationError, match="no longer matches"):
        validate_staged_patch_matches_artifact(tiny_repo, patch_artifact)


def test_correction_staging_rejects_emptied_index(
    tiny_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.runners.staging import validate_pre_staging

    target = tiny_repo / "feature.txt"
    target.write_text("feature\n", encoding="utf-8")
    _git(tiny_repo, "add", "feature.txt")
    patch_artifact = tmp_path / "run" / "git" / "diffs" / "01.patch"
    patch_artifact.parent.mkdir(parents=True)
    patch_artifact.write_text(
        subprocess.run(
            ["git", "diff", "--cached"],
            cwd=tiny_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
        encoding="utf-8",
    )
    _git(tiny_repo, "restore", "--staged", "feature.txt")
    state = _sample_state(tiny_repo)
    monkeypatch.setattr(
        "ai_dev_loop.runners.staging.validate_plan_hash_unchanged",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "ai_dev_loop.runners.staging.validate_prompt_source_unchanged",
        lambda *args, **kwargs: None,
    )
    with pytest.raises(
        ValidationError, match="expected the previous orchestrator-recorded staged patch"
    ):
        validate_pre_staging(
            state,
            tiny_repo,
            iteration_number=2,
            run_directory=tmp_path / "run",
        )


def test_correction_pre_cursor_rejects_unstaged_changes(tiny_repo: Path, tmp_path: Path) -> None:
    target = tiny_repo / "feature.txt"
    target.write_text("feature\n", encoding="utf-8")
    _git(tiny_repo, "add", "feature.txt")
    patch_artifact = tmp_path / "01.patch"
    atomic_write_text(
        patch_artifact,
        subprocess.run(
            ["git", "diff", "--cached"],
            cwd=tiny_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
    )
    target.write_text("unstaged change\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="unstaged tracked changes"):
        validate_correction_pre_cursor(tiny_repo, patch_artifact=patch_artifact)


def test_correction_pre_cursor_rejects_untracked_files(tiny_repo: Path, tmp_path: Path) -> None:
    target = tiny_repo / "feature.txt"
    target.write_text("feature\n", encoding="utf-8")
    _git(tiny_repo, "add", "feature.txt")
    patch_artifact = tmp_path / "01.patch"
    atomic_write_text(
        patch_artifact,
        subprocess.run(
            ["git", "diff", "--cached"],
            cwd=tiny_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout,
    )
    (tiny_repo / "new.txt").write_text("new\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="untracked files"):
        validate_correction_pre_cursor(tiny_repo, patch_artifact=patch_artifact)
