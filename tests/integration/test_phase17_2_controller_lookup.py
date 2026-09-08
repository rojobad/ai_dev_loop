"""Integration tests for combined scheduler and legacy controller lookup."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION

from ai_dev_loop.commands.controller import ControllerStatusResult, controller_status
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.scheduler.application.contracts import (
    ControllerSchedulerCandidate,
    authorized_safe_next_action,
)
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


def _prepare_legacy(git_repo: Path):
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-5.6-sol",
                codex_review_reasoning_effort="high",
            )
        )


def test_controller_status_ambiguous_with_scheduler_and_legacy_candidates(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    scheduler_run_id = _submit(git_repo, paths)
    start_run(scheduler_run_id, CONTROLLER_SESSION, db_path=paths["db_path"])
    legacy = _prepare_legacy(git_repo)
    assert legacy.run_id != scheduler_run_id

    status = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
    )
    assert status.match_count == 2
    assert status.run_id is None
    assert scheduler_run_id in status.candidate_run_ids
    assert legacy.run_id in status.candidate_run_ids

    explicit_scheduler = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
        run_id=scheduler_run_id,
    )
    assert explicit_scheduler.match_count == 1
    assert explicit_scheduler.run_id == scheduler_run_id
    assert explicit_scheduler.run_source == "scheduler"


def test_controller_status_ambiguous_when_both_stores_match_explicit_run_id(
    git_repo: Path,
    isolated_xdg: Path,
) -> None:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    run_id = _submit(git_repo, paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=paths["db_path"])
    candidate = ControllerSchedulerCandidate(
        run_id=run_id,
        state_kind="authorized",
        project_name="fixture-project",
        repository_root=str(git_repo.resolve()),
        submitted_at="2026-09-04T12:00:00.000000Z",
        updated_at="2026-09-04T12:01:00.000000Z",
        safe_next_action=authorized_safe_next_action(),
        capacity_holder_run_id=None,
        last_event_kind="run_authorized",
    )
    legacy_state = MagicMock()
    legacy_state.run_id = run_id
    with (
        patch(
            "ai_dev_loop.commands.controller.load_scheduler_candidate",
            return_value=candidate,
        ),
        patch(
            "ai_dev_loop.commands.controller.load_run",
            return_value=(Path("/tmp/run"), legacy_state),
        ),
        patch(
            "ai_dev_loop.commands.controller._require_controller_match",
            return_value=None,
        ),
    ):
        status = controller_status(
            controller_session_id=CONTROLLER_SESSION,
            repo_path=git_repo,
            run_id=run_id,
        )
    assert isinstance(status, ControllerStatusResult)
    assert status.match_count == 2
    assert status.run_id is None
    assert status.candidate_run_ids == [run_id]
