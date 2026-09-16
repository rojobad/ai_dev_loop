"""Shared fixtures for Phase 20.6.5 authenticated rollover tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
from tests.unit.scheduler.test_review_budget_extend import (
    _run_until,
    _setup_maxed_run,
    _tick_service,
)

from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def maxed_sequence_source_fixture(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_reviews: int = 2,
    review_sequence: str = "findings,findings",
) -> tuple[str, str, SqliteSchedulerStore, ProtectedArtifactStore, datetime]:
    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    run_id = start.run_id
    start_run(run_id, db_path=scheduler_paths["db_path"])
    fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=fixed_now,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="max_iterations_reached")
    return sequence_id, run_id, tick.store, tick.artifacts, fixed_now


def maxed_rollover_fixture(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_reviews: int = 2,
    review_sequence: str = "findings,findings",
) -> tuple[str, SqliteSchedulerStore, ProtectedArtifactStore, datetime]:
    run_id, tick, _, fixed_now = _setup_maxed_run(
        git_repo,
        scheduler_paths,
        monkeypatch,
        max_reviews=max_reviews,
        review_sequence=review_sequence,
    )
    return run_id, tick.store, tick.artifacts, fixed_now
