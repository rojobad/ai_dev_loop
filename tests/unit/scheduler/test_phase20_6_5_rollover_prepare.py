"""Prepare tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
)

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.domain.rollover import PREPARED_ROLLOVER_STATE_KIND


def test_prepare_is_idempotent_for_same_source(
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
    first = service.prepare(source_run_id, commit_message="rollover integration commit")
    second = service.prepare(source_run_id, commit_message="rollover integration commit")
    assert first.rollover_id == second.rollover_id
    assert second.idempotent_replay is True
    assert second.changed is False


def test_prepare_requires_commit_message_for_standalone(
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
    with pytest.raises(SchedulerEngineError, match="commit-message"):
        service.prepare(source_run_id)


def test_prepare_rejects_non_maxed_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, blocked_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = RolloverPrepareService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="not eligible"):
        service.prepare(blocked_run_id, commit_message="should fail")


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
    with store.begin_read() as conn:
        attempts_before = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (source_run_id,),
        ).fetchone()[0]
    result = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="process-free rollover commit",
    )
    assert result.state_kind == PREPARED_ROLLOVER_STATE_KIND
    assert result.changed is True
    with store.begin_read() as conn:
        attempts_after = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (source_run_id,),
        ).fetchone()[0]
    assert attempts_after == attempts_before
