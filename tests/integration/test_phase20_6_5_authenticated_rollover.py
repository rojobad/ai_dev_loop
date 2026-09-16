"""Integration tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture

from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.domain.rollover import PREPARED_ROLLOVER_STATE_KIND


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_prepare_from_maxed_source_is_process_free(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    service = RolloverPrepareService(store, artifacts)
    first = service.prepare(source_run_id, commit_message="integration rollover commit")
    second = service.prepare(source_run_id, commit_message="integration rollover commit")
    assert first.state_kind == PREPARED_ROLLOVER_STATE_KIND
    assert first.changed is True
    assert second.idempotent_replay is True
    assert second.rollover_id == first.rollover_id
