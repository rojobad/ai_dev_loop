"""Integration tests for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.domain.recovery import PREPARED_RECOVERY_STATE_KIND


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepare_from_blocked_source_is_process_free(
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
    result = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="integration recovery commit",
    )
    assert result.state_kind == PREPARED_RECOVERY_STATE_KIND
    assert result.changed is True
