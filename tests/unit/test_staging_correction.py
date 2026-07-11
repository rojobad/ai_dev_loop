"""Unit tests for correction Git staging safety and post-Cursor normalization."""

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
from ai_dev_loop.runners.staging import run_git_staging, validate_pre_staging
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
    sha256_file,
    utc_now,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _sample_state(repo_root: Path, *, head: str | None = None) -> RunState:
    now = utc_now()
    if head is None:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
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
            branch=branch,
            initial_head=head,
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256=sha256_file(repo_root / "docs/plans/sample-plan.md"),
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
    prompt = repo / "docs/plans/prompt_sample-plan.txt"
    prompt.write_text("prompt\n", encoding="utf-8")
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


def test_correction_staging_accepts_emptied_index_then_restages(
    tiny_repo: Path, tmp_path: Path
) -> None:
    target = tiny_repo / "feature.txt"
    target.write_text("feature\n", encoding="utf-8")
    _git(tiny_repo, "add", "feature.txt")
    run_directory = tmp_path / "run"
    patch_artifact = run_directory / "git" / "diffs" / "01.patch"
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
    (run_directory / "git/status").mkdir(parents=True, exist_ok=True)
    (run_directory / "git/status/02-before-cursor.txt").write_text("", encoding="utf-8")
    (run_directory / "git/status/02-after-cursor.txt").write_text("", encoding="utf-8")
    (run_directory / "git/cursor-output").mkdir(parents=True, exist_ok=True)
    (run_directory / "git/cursor-output/02.json").write_text(
        '{"aggregate_sha256": "' + ("c" * 64) + '"}\n',
        encoding="utf-8",
    )
    _git(tiny_repo, "restore", "--staged", "feature.txt")
    state = _sample_state(tiny_repo)
    # Post-Cursor staging must not reject because Cursor emptied the index.
    validate_pre_staging(
        state,
        tiny_repo,
        iteration_number=2,
        run_directory=run_directory,
    )
    result = run_git_staging(
        state,
        run_directory,
        iteration="02",
        iteration_number=2,
        cursor_started_at=utc_now(),
        cursor_exit_code=0,
        prompt_path="prompts/fixes/01.txt",
    )
    assert "feature.txt" in result.staged_paths
    assert (run_directory / "git/diffs/02.patch").is_file()


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


def test_cursor_changed_index_detects_same_path_content_change(tmp_path: Path) -> None:
    from ai_dev_loop.runners.staging import _cursor_changed_index

    run_directory = tmp_path / "run"
    (run_directory / "git/status").mkdir(parents=True)
    # Same path, different index blob hash (hI field).
    before = "1 M. N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb feature.txt\n"
    after = "1 M. N... 100644 100644 100644 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa cccccccccccccccccccccccccccccccccccccccc feature.txt\n"
    (run_directory / "git/status/02-before-cursor.txt").write_text(before, encoding="utf-8")
    (run_directory / "git/status/02-after-cursor.txt").write_text(after, encoding="utf-8")
    assert _cursor_changed_index(tmp_path, run_directory, "02") is True

    (run_directory / "git/status/02-after-cursor.txt").write_text(before, encoding="utf-8")
    assert _cursor_changed_index(tmp_path, run_directory, "02") is False


def test_cursor_changed_index_ignores_worktree_only_side_change(tmp_path: Path) -> None:
    from ai_dev_loop.runners.staging import _cursor_changed_index

    run_directory = tmp_path / "run"
    (run_directory / "git/status").mkdir(parents=True)
    # Index side unchanged (X, mI, hI, path); only Y / mW differ (M. -> MM).
    before = (
        "1 M. N... 100644 100644 100644 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb feature.txt\n"
    )
    after = (
        "1 MM N... 100644 100644 100755 "
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb feature.txt\n"
    )
    (run_directory / "git/status/02-before-cursor.txt").write_text(before, encoding="utf-8")
    (run_directory / "git/status/02-after-cursor.txt").write_text(after, encoding="utf-8")
    assert _cursor_changed_index(tmp_path, run_directory, "02") is False


def test_gitignore_plus_rm_cached_keeps_path_out_of_final_patch(
    tiny_repo: Path, tmp_path: Path
) -> None:
    generated = tiny_repo / "generated.out"
    generated.write_text("generated\n", encoding="utf-8")
    _git(tiny_repo, "add", "generated.out")
    _git(tiny_repo, "commit", "-m", "add generated")
    run_directory = tmp_path / "run"
    (run_directory / "git/diffs").mkdir(parents=True)
    (run_directory / "git/status").mkdir(parents=True)
    (run_directory / "git/cursor-output").mkdir(parents=True)
    (run_directory / "git/diffs/01.patch").write_text("prev\n", encoding="utf-8")
    (run_directory / "git/status/02-before-cursor.txt").write_text("", encoding="utf-8")
    (run_directory / "git/status/02-after-cursor.txt").write_text("", encoding="utf-8")
    (run_directory / "git/cursor-output/02.json").write_text(
        '{"aggregate_sha256": "' + ("d" * 64) + '"}\n',
        encoding="utf-8",
    )

    ignore = tiny_repo / ".gitignore"
    ignore.write_text("generated.out\n", encoding="utf-8")
    _git(tiny_repo, "rm", "--cached", "generated.out")
    _git(tiny_repo, "add", ".gitignore")

    state = _sample_state(tiny_repo)
    result = run_git_staging(
        state,
        run_directory,
        iteration="02",
        iteration_number=2,
        cursor_started_at=utc_now(),
        cursor_exit_code=0,
        prompt_path="prompts/fixes/01.txt",
    )
    assert ".gitignore" in result.staged_paths
    assert "generated.out" not in result.staged_paths or "deleted" in (
        run_directory / "git/diffs/02.patch"
    ).read_text(encoding="utf-8")
