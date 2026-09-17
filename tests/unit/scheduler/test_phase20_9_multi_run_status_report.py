"""Phase 20.9 lineage-enriched sequence status and completion reports."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)

from ai_dev_loop.commands.scheduler import render_sequence_status_output
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import SequencePhaseReportEntry
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_status_json_includes_lineage_attempt_counts_after_review_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    ReviewRetryService(store, artifacts).retry(source_run_id)
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifacts=artifacts,
    ).get_status(sequence_id)
    current = next(entry for entry in status.entries if entry.ordinal == 1)
    assert current.attempt_count == 2
    assert current.accepted_attempt_kind is None
    assert "same_reviewer_retry" in current.attempt_kind_labels
    rendered = render_sequence_status_output(status, output="json")
    assert "attempt_count" in rendered
    assert "prompt" not in rendered.lower()


def test_completion_report_model_includes_lineage_fields() -> None:
    entry = SequencePhaseReportEntry(
        ordinal=1,
        phase_name="phase-one",
        run_id="run-00000000000000000000000000000001",
        run_id_prefix="run-0000",
        accepted_run_id_prefix="run-0000",
        accepted_attempt_kind="planned_run",
        attempt_count=2,
        attempt_kind_labels=("planned_run", "same_reviewer_retry"),
        accepted_outcome="completed",
    )
    payload = entry.model_dump(mode="json")
    assert payload["attempt_count"] == 2
    assert payload["accepted_run_id_prefix"] == "run-0000"
