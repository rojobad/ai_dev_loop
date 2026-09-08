"""Integration tests for Phase 17.3 systemd attempt executor tick boundary."""

from __future__ import annotations

from pathlib import Path

from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, _bootstrap_run, _tick_service

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)


def test_tick_exits_while_attempt_active_then_reconciles_once(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    first = tick.run_once()
    assert any(item.action == "admitted" for item in first.run_receipts)
    assert any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in first.run_receipts
    )
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) in {"launching", "active"}
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id

    second = tick.run_once()
    assert any(item.action == "attempt_completed" for item in second.run_receipts)
    with store.begin_read() as conn:
        attempt = store.get_attempt_by_id(conn, "att-" + "a" * 32)
        assert attempt is not None
        assert str(attempt["status"]) == "completed"
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1


def test_concurrent_launch_requests_do_not_start_second_unit(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, exit_code=0)
    )
    tick = _tick_service(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    first = tick.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in first.run_receipts
    )
    assert len(backend.launch_calls) == 1

    second = tick.run_once()
    assert len(backend.launch_calls) == 1
    assert any(item.action in {"attempt_active", "attempt_busy"} for item in second.run_receipts)
