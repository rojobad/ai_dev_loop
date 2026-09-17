"""Blocked-sequence abort with outstanding review-replacement intents."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.domain.sequence import ABORTED_SEQUENCE_STATE_KIND, AbortedSequenceState
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_abort_blocked_sequence_before_replacement_intent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = SequenceAbortService(store)
    first = service.abort_sequence(sequence_id)
    second = service.abort_sequence(sequence_id)
    assert first.state_kind == ABORTED_SEQUENCE_STATE_KIND
    assert second.idempotent_replay is True
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id)
        source, _, _ = store.load_validated_snapshot(conn, source_run_id)
    assert isinstance(sequence, AbortedSequenceState)
    assert sequence.preserved_current_leaf_terminal == "blocked"
    assert lineage.phase_executions[0].attempts[-1].terminal_outcome == "blocked"
    assert source.kind == "blocked"
    retry = ReviewRetryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="cancelled|aborted|not blocked"):
        retry.retry(source_run_id)


def test_abort_blocked_sequence_after_pending_materialization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.sequence_review_recovery._adopt_sequence_recovery_successor",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "simulated crash before sequence adoption",
            )
        ),
    )
    retry = ReviewRetryService(store, artifacts)
    with pytest.raises(SchedulerEngineError):
        retry.retry(source_run_id)
    abort = SequenceAbortService(store)
    result = abort.abort_sequence(sequence_id)
    assert result.state_kind == ABORTED_SEQUENCE_STATE_KIND
    with store.begin_read() as conn:
        pending = conn.execute(
            """
            SELECT run_id FROM scheduler_runs
            WHERE state_kind = 'pending_sequence_review_recovery'
            """
        ).fetchall()
        cancelled = conn.execute(
            """
            SELECT cancelled_at FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchone()
    assert len(pending) == 1
    assert cancelled is not None
    assert cancelled["cancelled_at"] is not None
    with pytest.raises(SchedulerEngineError):
        retry.retry(source_run_id)


def test_cli_abort_blocked_sequence_with_outstanding_replacement(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    sequence_id, _source_run_id, _tick, store, _artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    runner = CliRunner()
    result = runner.invoke(app, ["scheduler", "sequence", "abort", sequence_id])
    assert result.exit_code == 0, result.output
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AbortedSequenceState)
