"""Integration tests for Phase 17.2 tick control and controller status."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort

from ai_dev_loop.commands.controller import controller_status, render_controller_status
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


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


def test_submit_start_tick_controller_status_flow(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start = start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    assert start.state_kind == "authorized"

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    fake = FakeGitAdmissionPort(resolved_root=str(git_repo.resolve()))
    tick = TickService(
        store,
        artifacts,
        fake,
        tick_owner_factory=lambda: "tick-integration",
    )
    receipt = tick.run_once()
    assert receipt.lease_acquired is True
    assert any(item.action == "admitted" for item in receipt.run_receipts)

    status = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
    )
    assert status.match_count == 1
    assert status.run_id == run_id
    assert status.scheduler_state_kind == "admitted"
    assert status.run_source == "scheduler"
    assert status.reviewer_session_id is None

    rendered = render_controller_status(status, output="json")
    assert '"scheduler_state_kind": "admitted"' in rendered
    assert '"reviewer_session_id_prefix": null' in rendered


def test_controller_status_wrong_controller_read_only_failure(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    status = controller_status(
        controller_session_id="22222222-2222-2222-2222-222222222222",
        repo_path=git_repo,
    )
    assert status.match_count == 0
