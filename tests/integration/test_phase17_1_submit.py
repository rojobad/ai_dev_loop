"""Integration tests for Phase 17.1 scheduler submit boundary."""

from __future__ import annotations

import json
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import (
    FIXTURE_REPO,
    write_session_rollout,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, REVIEWER_SESSION

from ai_dev_loop.paths import runs_dir
from ai_dev_loop.runners.git import GitRepositoryInfo
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.submission import (
    SubmissionService,
    SubmitOptions,
    submit_run,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _fake_repo_info(repo: Path) -> GitRepositoryInfo:
    return GitRepositoryInfo(
        root=repo.resolve(),
        git_common_dir=(repo / ".git").resolve(),
        git_dir=(repo / ".git").resolve(),
        branch="main",
        head="abc1234567890123456789012345678901234567890",
        status_porcelain="",
        staged_paths=(),
    )


def _submit_options(
    repo: Path,
    *,
    db_path: Path,
    artifact_root: Path,
    controller_session_id: str = CONTROLLER_SESSION,
    codex_session_id: str = REVIEWER_SESSION,
) -> SubmitOptions:
    return SubmitOptions(
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        codex_session_id=codex_session_id,
        controller_session_id=controller_session_id,
        db_path=db_path,
        artifact_root=artifact_root,
    )


def _submission_service(
    *,
    db_path: Path,
    artifact_root: Path,
    repo: Path,
) -> SubmissionService:
    return SubmissionService(
        SqliteSchedulerStore(db_path),
        ProtectedArtifactStore(artifact_root),
        repository_discoverer=lambda _path: _fake_repo_info(repo),
    )


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


@pytest.fixture
def codex_env(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_home = isolated_home / ".codex"
    write_session_rollout(codex_home / "sessions", session_id=REVIEWER_SESSION)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))


def test_submit_is_side_effect_free(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))

    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after
    assert not runs_dir().exists()
    assert scheduler_paths["db_path"].exists()
    assert (scheduler_paths["artifact_root"] / "runs").exists()
    assert result.state_kind == "queued"
    assert result.safe_next_action.command == f"ai_dev_loop scheduler start {result.run_id}"


def test_identical_submit_reuses_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        first = service.submit(_submit_options(git_repo, **scheduler_paths))
    with patch("sys.stdin", StringIO(prompt)):
        second = service.submit(_submit_options(git_repo, **scheduler_paths))
    assert first.run_id == second.run_id
    assert second.reused_existing is True
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 1


def test_conflicting_worktree_rejected(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        service.submit(_submit_options(git_repo, **scheduler_paths))
    with (
        patch("sys.stdin", StringIO(prompt + "\n")),
        pytest.raises(SchedulerEngineError, match="active scheduler reservation"),
    ):
        service.submit(_submit_options(git_repo, **scheduler_paths))


def test_status_and_list_are_redacted(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    from ai_dev_loop.commands.scheduler import render_list_output, render_status_output
    from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(_submit_options(git_repo, **scheduler_paths))

    status = scheduler_status(submitted.run_id, db_path=scheduler_paths["db_path"])
    listings = scheduler_list(db_path=scheduler_paths["db_path"])
    status_text = render_status_output(status, output="text")
    list_text = render_list_output(listings, output="text")
    # full session IDs must not appear in default text output
    assert REVIEWER_SESSION not in status_text
    assert CONTROLLER_SESSION not in status_text
    assert REVIEWER_SESSION not in list_text
    assert CONTROLLER_SESSION not in list_text
    assert REVIEWER_SESSION[:8] in status_text
    status_json = json.loads(render_status_output(status, output="json"))
    assert REVIEWER_SESSION not in json.dumps(status_json)
    assert status_json["summary"]["state_kind"] == "queued"


def test_cli_help_lists_scheduler_submit() -> None:
    result = subprocess.run(
        ["uv", "run", "ai_dev_loop", "scheduler", "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0
    assert "submit" in result.stdout


def test_submit_freezes_dirty_baseline_when_clean_worktree_not_required(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = git_repo / "ai_dev_loop.yaml"
    config_text = config_path.read_text(encoding="utf-8").replace(
        "require_clean_worktree: true",
        "require_clean_worktree: false",
    )
    config_path.write_text(config_text, encoding="utf-8")
    (git_repo / "dirty-untracked.txt").write_text("dirty\n", encoding="utf-8")

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))

    from ai_dev_loop.runners.git import discover_repository

    expected_status = discover_repository(git_repo).status_porcelain + "\n"
    baseline_path = (
        run_artifact_root(scheduler_paths["artifact_root"], result.run_id)
        / "git"
        / "baseline-status.txt"
    )
    assert baseline_path.read_text(encoding="utf-8") == expected_status
    assert "? dirty-untracked.txt" in expected_status
    assert result.state_kind == "queued"
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn,
            result.run_id,
        )
    assert state.context.workflow.require_clean_worktree is False
    assert state.context.plan_prompt.plan_repository_path == "docs/plans/sample-plan.md"
    assert (
        state.context.plan_prompt.prompt_source_repository_path
        == "docs/plans/prompt_sample-plan.txt"
    )
