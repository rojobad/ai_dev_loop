"""Integration tests for Phase 17.1.5 fresh Codex reviewer bootstrap via scheduler."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO

from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.domain.state import FreshCodexReviewerBinding
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

CONTROLLER_ID = "019abc00-aaaa-0000-0000-0000000000aa"
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING = "high"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_scheduler_submit_freezes_reviewer_without_session(
    git_repo: Path,
    isolated_xdg,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(
            SubmitOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_ID,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
                db_path=scheduler_paths["db_path"],
                artifact_root=scheduler_paths["artifact_root"],
            )
        )
    assert result.state_kind == "queued"
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn, result.run_id
        )
    assert state.context.schema_version == 3
    assert isinstance(state.context.codex, FreshCodexReviewerBinding)
    assert state.context.codex.review_model == REVIEW_MODEL
