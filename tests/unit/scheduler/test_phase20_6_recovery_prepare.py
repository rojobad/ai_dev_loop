"""Prepare tests for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepare_is_idempotent_for_same_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = RecoveryPrepareService(store, artifacts)
    first = service.prepare(source_run_id, commit_message="recovery integration commit")
    second = service.prepare(source_run_id, commit_message="recovery integration commit")
    assert first.recovery_id == second.recovery_id
    assert second.idempotent_replay is True
    assert second.changed is False


def test_prepare_requires_commit_message_for_standalone(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = RecoveryPrepareService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="commit-message"):
        service.prepare(source_run_id)
