"""Phase 20.9 sequence abort coordination regressions."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence

from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.domain.sequence import ABORTED_SEQUENCE_STATE_KIND
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepared_sequence_abort_remains_non_destructive(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    result = SequenceAbortService(store, now_factory=lambda: FIXED_NOW).abort_sequence(sequence_id)
    assert result.state_kind == ABORTED_SEQUENCE_STATE_KIND
    with store.begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert run_count == 0
