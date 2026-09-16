"""Shared fixtures for Phase 20.6.5 authenticated rollover tests."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_review_budget_extend import _setup_maxed_run

from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


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
