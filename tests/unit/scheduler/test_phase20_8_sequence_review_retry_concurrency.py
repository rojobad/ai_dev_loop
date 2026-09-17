"""Concurrency tests for Phase 20.8 sequence execution replacement intents."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)

import ai_dev_loop.scheduler.application.sequence_review_recovery as sequence_review_recovery
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_concurrent_sequence_review_retry_converges_to_one_successor(
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
    store.busy_timeout_ms = 120_000
    service = ReviewRetryService(
        store,
        artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 13, 0, tzinfo=UTC),
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
    successor_id = results[0]
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
    assert sequence.current_run_id == successor_id
    assert len(rows) == 1
    assert str(rows[0]["successor_run_id"]) == successor_id


def _run_concurrent_retry_with_materialize_gate(
    *,
    source_run_id: str,
    service: ReviewRetryService,
    tick,
    store,
    advance_successor: str,
) -> tuple[list[str], list[bool], list[BaseException]]:
    original_materialize = sequence_review_recovery._materialize_pending_successor
    slow_thread_marker = threading.Event()
    slow_at_materialize = threading.Event()
    release_slow_materialize = threading.Event()
    results: list[str] = []
    changed_flags: list[bool] = []
    errors: list[BaseException] = []
    timeout_s = 120.0

    def gated_materialize(*args, **kwargs):  # type: ignore[no-untyped-def]
        if slow_thread_marker.is_set() and threading.current_thread().name == "slow-retry":
            slow_at_materialize.set()
            release_slow_materialize.wait(timeout=timeout_s)
        return original_materialize(*args, **kwargs)

    def _slow_worker() -> None:
        slow_thread_marker.set()
        try:
            outcome = service.retry(source_run_id)
            results.append(outcome.run_id)
            changed_flags.append(outcome.changed)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _fast_worker() -> None:
        try:
            outcome = service.retry(source_run_id)
            results.append(outcome.run_id)
            changed_flags.append(outcome.changed)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    with patch.object(
        sequence_review_recovery,
        "_materialize_pending_successor",
        gated_materialize,
    ):
        slow = threading.Thread(target=_slow_worker, name="slow-retry")
        fast = threading.Thread(target=_fast_worker, name="fast-retry")
        slow.start()
        assert slow_at_materialize.wait(timeout=timeout_s), (
            "timed out waiting for slow thread to reach materialization"
        )
        fast.start()
        deadline = time.monotonic() + timeout_s
        successor_id: str | None = None
        while time.monotonic() < deadline:
            if errors:
                break
            if len(results) >= 1:
                successor_id = results[0]
                with store.begin_read() as conn:
                    run_row = conn.execute(
                        "SELECT state_kind FROM scheduler_runs WHERE run_id = ?",
                        (successor_id,),
                    ).fetchone()
                if run_row is None:
                    successor_id = None
                else:
                    state_kind = str(run_row["state_kind"])
                    if advance_successor == "waiting" and state_kind != "awaiting_codex_review":
                        break
                    if advance_successor == "completed" and state_kind == "completed":
                        break
            tick.run_once()
        assert not errors, errors
        assert successor_id is not None, "fast retry did not publish a successor in time"
        if advance_successor == "waiting":
            with store.begin_read() as conn:
                state, _, _ = store.load_validated_snapshot(conn, successor_id)
            assert state.kind != "awaiting_codex_review"
        elif advance_successor == "completed":
            with store.begin_read() as conn:
                state, _, _ = store.load_validated_snapshot(conn, successor_id)
            assert state.kind == "completed"
        release_slow_materialize.set()
        slow.join(timeout=timeout_s + 60)
        fast.join(timeout=timeout_s + 60)
        assert not slow.is_alive()
        assert not fast.is_alive()
    assert sequence_review_recovery._materialize_pending_successor is original_materialize
    return results, changed_flags, errors


def test_concurrent_retry_converges_when_successor_advances_before_materialize(
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
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    service = ReviewRetryService(
        store,
        artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
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
    assert changed_flags.count(True) == 1
    assert changed_flags.count(False) == 1
    successor_id = results[0]
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
    assert sequence.current_run_id == successor_id
    assert len(rows) == 1


def test_concurrent_retry_converges_when_successor_reaches_terminal_state(
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
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    service = ReviewRetryService(
        store,
        artifacts,
        now_factory=lambda: datetime(2026, 9, 17, 14, 30, tzinfo=UTC),
    )
    results, changed_flags, errors = _run_concurrent_retry_with_materialize_gate(
        source_run_id=source_run_id,
        service=service,
        tick=tick,
        store=store,
        advance_successor="completed",
    )
    assert not errors, errors
    assert len(set(results)) == 1
    assert changed_flags.count(True) == 1
    assert changed_flags.count(False) == 1
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, results[0])
    assert state.kind == "completed"


def test_publish_or_verify_patch_does_not_leak_after_concurrency(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run after concurrency test module exercises real publish_or_verify_bytes in-process."""
    assert ProtectedArtifactStore.publish_or_verify_bytes is not None
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = ReviewRetryService(store, artifacts)
    outcome = service.retry(source_run_id)
    assert outcome.changed is True
    replay = service.retry(source_run_id)
    assert replay.idempotent_replay is True
    assert replay.run_id == outcome.run_id
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
