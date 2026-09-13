"""Integration tests for Phase 20.1.1 blocked-run review recovery."""

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

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
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
        max_review_iterations=3,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def test_blocked_run_recovery_successor_completes_without_cursor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])

    def _legacy_block_instead_of_retry(workflow, monkeypatch: pytest.MonkeyPatch) -> None:
        original = workflow._enter_review_retry_wait

        def _blocked(
            run_id: str,
            *,
            attempt_id: str,
            review_iteration: int,
            failure_kind: str,
        ):
            return workflow._block_review(
                run_id,
                attempt_id=attempt_id,
                reason_kind=failure_kind,
                summary="legacy blocked review failure",
            )

        monkeypatch.setattr(workflow, "_enter_review_retry_wait", _blocked)
        return original

    tick = TickService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 12, 18, 15, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-int-20-1-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    assert tick._codex_workflow is not None
    _legacy_block_instead_of_retry(tick._codex_workflow, monkeypatch)
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                break
    assert isinstance(state, BlockedState)
    assert state.block_reason_kind == "codex_review_outcome_invalid"

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = ReviewRetryService(store, artifacts)
    recovery = service.retry(run_id)
    assert recovery.recovery_successor is True
    successor_id = recovery.run_id
    assert successor_id != run_id

    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        assert source.kind == "blocked"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            successor, _, _ = tick.store.load_validated_snapshot(conn, successor_id)
            if successor.kind == "completed":
                break
    assert successor.kind == "completed"
    assert successor.codex.reviewer_session_id == BOOTSTRAP_ID
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert "cursor_attempt_runner" not in codex_log or "'resume'" in codex_log
    source_final = next(
        artifacts.run_root(run_id).glob("cursor/iterations/01/*/final.txt")
    ).read_text(encoding="utf-8")
    successor_final = next(
        artifacts.run_root(successor_id).glob("cursor/iterations/01/*/final.txt")
    ).read_text(encoding="utf-8")
    assert successor_final == source_final
    replay = service.retry(run_id)
    assert replay.idempotent_replay is True
    assert replay.run_id == successor_id
