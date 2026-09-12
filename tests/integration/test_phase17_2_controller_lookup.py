"""Integration tests for scheduler-only controller lookup."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION

from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run


def _submit(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(options)
    return result.run_id


def test_controller_status_returns_scheduler_run(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    scheduler_run_id = _submit(git_repo, paths)
    start_run(scheduler_run_id, db_path=paths["db_path"])

    status = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
    )
    assert status.match_count == 1
    assert status.run_id == scheduler_run_id
    assert status.run_source == "scheduler"

    explicit = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
        run_id=scheduler_run_id,
    )
    assert explicit.match_count == 1
    assert explicit.run_id == scheduler_run_id
    assert explicit.run_source == "scheduler"


def test_controller_status_scopes_matches_to_repository(
    git_repo: Path,
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    import shutil

    second_repo = tmp_path / "repo-b"
    shutil.copytree(git_repo, second_repo)
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    first = _submit(git_repo, paths)
    second = _submit(second_repo, paths)
    assert first != second

    status = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
    )
    assert status.match_count == 1
    assert status.run_id == first

    status_second_repo = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=second_repo,
    )
    assert status_second_repo.match_count == 1
    assert status_second_repo.run_id == second
