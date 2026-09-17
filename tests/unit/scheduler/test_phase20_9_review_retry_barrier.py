"""Explicit-barrier retry races for Phase 20.9 acceptance."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry_concurrency import (
    _run_concurrent_retry_with_materialize_gate,
)

from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_barrier_competing_review_retries_converge_to_one_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    store.busy_timeout_ms = 120_000
    service = ReviewRetryService(
        store,
        artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 14, 30, tzinfo=UTC),
    )
    barrier_timeout_s = 120.0
    start_barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def _wait_start_barrier() -> None:
        try:
            index = start_barrier.wait(timeout=barrier_timeout_s)
        except threading.BrokenBarrierError as exc:
            raise AssertionError("start barrier broken") from exc
        if index < 0:
            raise AssertionError(f"start barrier timed out after {barrier_timeout_s}s")

    def _worker() -> None:
        try:
            _wait_start_barrier()
            outcome = service.retry(source_run_id)
            results.append(outcome.run_id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=barrier_timeout_s + 60)
        assert not thread.is_alive(), f"worker still alive; errors={errors!r} results={results!r}"
    assert not errors, errors
    assert len(results) == 2
    assert len(set(results)) == 1
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        rows = conn.execute(
            """
            SELECT successor_run_id
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchall()
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == results[0]
    assert len(rows) == 1


def test_barrier_retry_while_tick_advances_successor_stays_single(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, tick, store, _artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    store.busy_timeout_ms = 120_000
    service = ReviewRetryService(
        store,
        artifacts=_artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 14, 45, tzinfo=UTC),
    )
    results, changed_flags, errors = _run_concurrent_retry_with_materialize_gate(
        source_run_id=source_run_id,
        service=service,
        tick=tick,
        store=store,
        advance_successor="waiting",
    )
    assert not errors, errors
    assert len(results) == 2
    assert len(set(results)) == 1
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == results[0]
