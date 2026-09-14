"""Unit tests for Phase 20.4 enriched sequence status projections."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.commands.scheduler import render_sequence_status_output
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import ABORTED_SEQUENCE_STATE_KIND
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_sequence_status_json_includes_aggregate_counts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    status = SequenceStatusService(store, artifacts=artifacts).get_status(sequence_id)
    rendered = render_sequence_status_output(status, output="json")
    payload = json.loads(rendered)
    assert payload["aggregate_counts"]["planned"] == 2
    assert payload["aggregate_counts"]["materialized"] == 1
    assert payload["aggregate_counts"]["remaining"] == 1
    assert "prompt" not in rendered.lower()
    assert "review_markdown" not in rendered


def test_aborted_sequence_status_marks_cancelled_entries(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    SequenceAbortService(store, now_factory=lambda: FIXED_NOW).abort_sequence(sequence_id)
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
    ).get_status(sequence_id)
    assert status.state_kind == ABORTED_SEQUENCE_STATE_KIND
    assert all(entry.cancelled for entry in status.entries)
    assert status.aggregate_counts is not None
    assert status.aggregate_counts.cancelled == 2
