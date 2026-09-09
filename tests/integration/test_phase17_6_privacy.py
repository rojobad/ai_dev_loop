"""Privacy regression tests for scheduler status/history/controller output."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, REVIEWER_SESSION

from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.commands.scheduler import render_scheduler_history_output, render_status_output
from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.domain.events import CursorChatCreatedEvent
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
        return submit_run(options).run_id


def test_status_history_and_controller_remain_redacted(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    unit_identity = "ai-dev-loop-attempt-" + ("d" * 32)
    with store.begin_immediate() as conn:
        sequence = store.next_event_sequence(conn, run_id)
        store.append_event(
            conn,
            event_id="evt-sensitive",
            run_id=run_id,
            sequence=sequence,
            event=CursorChatCreatedEvent(
                run_id=run_id,
                chat_id=REVIEWER_SESSION,
                chat_artifact_path="prompts/top-secret-fix.txt",
                chat_artifact_sha256="a" * 64,
            ),
            now=now,
        )
    status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    status_text = render_status_output(status, output="json")
    history = scheduler_history(
        run_id, db_path=scheduler_paths["db_path"], order="newest", limit=20
    )
    history_text = render_scheduler_history_output(history, output="json")
    controller = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
        run_id=run_id,
    )
    controller_text = json.dumps(
        {
            "next_safe_action": controller.next_safe_action,
            "abort_control": controller.abort_control,
            "reviewer_session_id_prefix": controller.reviewer_session_id,
        }
    )
    blob = "\n".join([status_text, history_text, controller_text, unit_identity])
    assert REVIEWER_SESSION not in blob
    assert "prompts/top-secret-fix.txt" not in blob
    assert "top-secret-fix" not in blob
