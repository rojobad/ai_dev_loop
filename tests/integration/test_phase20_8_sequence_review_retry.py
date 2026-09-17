"""CLI integration tests for Phase 20.8 sequence-aware review retry."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _legacy_block_instead_of_retry,
    _run_until_blocked_sequence,
    _tick_service,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_cli_sequence_review_retry_returns_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "019def00-0000-0000-0000-0000000000bb")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 12, 30, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    source_run_id, _ = _run_until_blocked_sequence(tick, sequence_id)
    assert source_run_id == start.run_id

    runner = CliRunner()
    result = runner.invoke(app, ["scheduler", "review", "retry", source_run_id])
    assert result.exit_code == 0, result.output
    assert "Recovery successor: yes" in result.output

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id in result.output
