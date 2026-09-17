"""Phase 20.9 sequence lineage projection error handling."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.sequence_lineage_projection import (
    load_phase_lineage_projections,
)
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_lineage_projection_uses_historical_adapter_when_lineage_table_empty(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        projections = load_phase_lineage_projections(
            store,
            conn,
            sequence_id=sequence_id,
            definition=sequence.definition,
            sequence_state=sequence,
        )
    assert projections[1].attempt_count == 1
    assert projections[1].attempt_kind_labels == ("planned_run",)


def test_lineage_projection_fails_closed_on_corrupt_lineage_row(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        if not store.schema_supports_sequence_run_lineage(conn):
            pytest.skip("lineage schema not present")
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET attempt_kind = 'not-a-real-kind'
            WHERE sequence_id = ? AND run_id = ?
            """,
            (sequence_id, start.run_id),
        )
        sequence = store.load_validated_sequence_state(conn, sequence_id, validate_lineage=False)
        assert isinstance(sequence, ActiveSequenceState)
        with pytest.raises(SchedulerEngineError):
            load_phase_lineage_projections(
                store,
                conn,
                sequence_id=sequence_id,
                definition=sequence.definition,
                sequence_state=sequence,
            )
