"""Integration tests for scheduler abort across attempt lifecycle positions."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.attempt_identity import unit_identity_from_attempt_id
from ai_dev_loop.scheduler.application.contracts import AbortProcessAction
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


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


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    backend: FakeAgentProcessBackend,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-17-6-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def test_abort_before_launch_is_durable_first(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend()
    result = scheduler_abort_run(run_id, db_path=scheduler_paths["db_path"], backend=backend)
    assert result.abort_persisted is True
    assert result.process_action is AbortProcessAction.NONE
    assert backend.terminate_calls == []
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"


def test_abort_during_active_cursor_attempt_and_late_result_fenced(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(
            active_ticks=5,
            stays_active_after_terminate=True,
            exit_code=0,
        )
    )
    tick = _tick_service(git_repo, scheduler_paths, backend=backend)
    active_attempt = False
    attempt_id = None
    for _ in range(12):
        receipt = tick.run_once()
        with tick.store.begin_read() as conn:
            row = tick.store.get_nonterminal_attempt_for_run(conn, run_id)
            if row is not None:
                active_attempt = True
                attempt_id = str(row["attempt_id"])
                break
    assert active_attempt, f"expected active attempt; last receipts={receipt.run_receipts}"
    abort = scheduler_abort_run(run_id, db_path=scheduler_paths["db_path"], backend=backend)
    assert abort.process_action is AbortProcessAction.TERMINATED
    assert abort.termination_pending is True
    assert attempt_id is not None
    backend.set_scenario(attempt_id, FakeAttemptScenario(active_ticks=0, exit_code=0))
    late = tick.run_once()
    assert any(item.action == "attempt_result_stale" for item in late.run_receipts)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"


def test_controller_status_works_during_active_scheduler_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=5, exit_code=0)
    )
    tick = _tick_service(git_repo, scheduler_paths, backend=backend)
    tick.run_once()
    status = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
        run_id=run_id,
    )
    assert status.run_id == run_id
    assert status.run_source == "scheduler"
    assert status.abort_control is not None
    assert "scheduler abort" in str(status.abort_control.get("scheduler_abort_command", ""))


def test_process_identity_mismatch_refuses_terminate(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(owned=False, active_ticks=1)
    )
    attempt_id = "att-" + "c" * 32
    unit_identity = unit_identity_from_attempt_id(attempt_id)
    with pytest.raises(RuntimeError, match="refusing to terminate"):
        backend.terminate(unit_identity=unit_identity, attempt_id=attempt_id)
