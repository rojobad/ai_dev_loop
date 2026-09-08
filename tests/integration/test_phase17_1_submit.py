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
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION

from ai_dev_loop.paths import runs_dir
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.submission import (
    SubmissionService,
    SubmitOptions,
    submit_run,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _fake_repo_target(repo: Path) -> RepositoryTarget:
    return RepositoryTarget(root=repo.resolve())


def _submit_options(
    repo: Path,
    *,
    db_path: Path,
    artifact_root: Path,
    controller_session_id: str = CONTROLLER_SESSION,
) -> SubmitOptions:
    return SubmitOptions(
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=controller_session_id,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
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
        repository_discoverer=lambda _path: _fake_repo_target(repo),
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
    """Scheduler submit no longer reads Codex session rollouts at submission time."""

    return None


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
    assert "scheduler start" in result.safe_next_action.command
    assert "--controller-session-id" in result.safe_next_action.command


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
    assert CONTROLLER_SESSION not in status_text
    assert CONTROLLER_SESSION not in list_text
    assert CONTROLLER_SESSION[:8] in status_text
    status_json = json.loads(render_status_output(status, output="json"))
    assert CONTROLLER_SESSION not in json.dumps(status_json)
    assert status_json["summary"]["state_kind"] == "queued"
    assert status_json["summary"]["reviewer_session_id_prefix"] is None


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


def test_submit_ignores_worktree_git_state_and_writes_no_baseline(
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
    tracked = git_repo / "docs/plans/sample-plan.md"
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\nstaged edit\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/plans/sample-plan.md"], cwd=git_repo, check=True, capture_output=True
    )

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))

    baseline_path = (
        run_artifact_root(scheduler_paths["artifact_root"], result.run_id)
        / "git"
        / "baseline-status.txt"
    )
    assert not baseline_path.exists()
    assert result.state_kind == "queued"
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn,
            result.run_id,
        )
    assert state.context.schema_version == 3
    assert state.context.workflow.require_clean_worktree is False
    assert state.context.baseline_status_artifact_path is None
    assert state.context.baseline_status_sha256 is None
