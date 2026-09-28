"""Unit tests for Integration API sequence projection helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.integration_api.sequence_projection import _load_lineage_by_ordinal, paginate_runs
from ai_dev_loop.scheduler.domain.sequence_run_lineage import SequenceRunAttempt
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _chain_attempts(count: int) -> tuple[SequenceRunAttempt, ...]:
    attempts: list[SequenceRunAttempt] = []
    for generation in range(1, count + 1):
        source = attempts[-1].run_id if attempts else None
        terminal = None if generation == count else "blocked"
        resolved = None if generation == count else "2026-01-01T00:01:00.000Z"
        attempts.append(
            SequenceRunAttempt(
                schema_version=1,
                generation=generation,
                run_id=f"run-{generation:03d}",
                source_run_id=source,
                attempt_kind="planned_run" if generation == 1 else "same_reviewer_retry",
                materialized_at="2026-01-01T00:00:00.000Z",
                terminal_outcome=terminal,
                resolved_at=resolved,
            )
        )
    return tuple(attempts)


def test_paginate_runs_supports_101_elements_with_stable_boundaries() -> None:
    attempts = _chain_attempts(101)
    page0, more0, next0 = paginate_runs(attempts, offset=0, limit=100)
    assert len(page0) == 100
    assert more0 is True
    assert next0 == 100
    page1, more1, next1 = paginate_runs(attempts, offset=100, limit=100)
    assert len(page1) == 1
    assert more1 is False
    assert next1 is None
    assert page0[0].run_id == "run-001"
    assert page1[0].run_id == "run-101"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


@dataclass(frozen=True)
class _SequenceStatusHandle:
    sequence_id: str


def test_load_lineage_uses_historical_projection_when_rows_missing(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    run_id = _start_service(scheduler_paths).start(sequence_id).run_id
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        if not store.schema_supports_sequence_run_lineage(conn):
            pytest.skip("lineage schema not present")
        conn.execute(
            "DELETE FROM scheduler_sequence_run_attempts WHERE sequence_id = ?",
            (sequence_id,),
        )
    handle = _SequenceStatusHandle(sequence_id=sequence_id)
    with store.begin_read() as conn:
        lineage = _load_lineage_by_ordinal(store, conn, handle)  # type: ignore[arg-type]
    assert lineage[1].attempts[0].run_id == run_id
