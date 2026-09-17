"""Phase 20.8 sequence-aware manual review retry tests."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState, BlockedSequenceState
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore

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


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-20-8-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def _legacy_block_instead_of_retry(tick: TickService, monkeypatch: pytest.MonkeyPatch) -> None:
    assert tick._codex_workflow is not None
    workflow = tick._codex_workflow

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


def _run_until_blocked_sequence(
    tick: TickService,
    sequence_id: str,
    *,
    max_ticks: int = 120,
) -> tuple[str, BlockedState]:
    run_id: str | None = None
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            sequence = tick.store.load_validated_sequence_state(conn, sequence_id)
            if isinstance(sequence, BlockedSequenceState):
                run_id = sequence.current_run_id
                state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
                assert isinstance(state, BlockedState)
                return run_id, state
    raise AssertionError(f"sequence {sequence_id} did not reach blocked")


def _blocked_sequence_review_fixture(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, TickService, SqliteSchedulerStore, ProtectedArtifactStore]:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 12, 0, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    run_id, blocked = _run_until_blocked_sequence(tick, sequence_id)
    assert blocked.block_reason_kind == "codex_usage_limit"
    assert run_id == start.run_id
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return sequence_id, run_id, tick, store, artifacts


class TestPhase208Schema:
    def test_v10_migration_adds_sequence_execution_replacement_table(self, tmp_path: Path) -> None:
        db_path = tmp_path / "engine.sqlite3"
        store = SqliteSchedulerStore(db_path)
        store.bootstrap()
        assert SCHEMA_VERSION == 11
        with store.begin_read() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        assert "scheduler_sequence_execution_replacements" in tables
        with store.begin_read() as conn:
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(scheduler_sequence_execution_replacements)"
                ).fetchall()
            }
        assert "cancelled_at" in columns


class TestSequenceReviewRetryHappyPath:
    def test_blocked_sequence_review_retry_reactivates_sequence_leaf(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sequence_id, source_run_id, tick, store, artifacts = _blocked_sequence_review_fixture(
            git_repo,
            scheduler_paths,
            fake_clis,
            monkeypatch,
        )
        service = ReviewRetryService(store, artifacts)
        recovery = service.retry(source_run_id)
        assert recovery.recovery_successor is True
        assert recovery.changed is True
        successor_id = recovery.run_id
        assert successor_id != source_run_id
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, sequence_id)
            source, _, _ = store.load_validated_snapshot(conn, source_run_id)
            lineage = load_sequence_run_lineage(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
        assert sequence.current_run_id == successor_id
        assert source.kind == "blocked"
        assert len(lineage.phase_executions[0].attempts) == 2
        assert lineage.phase_executions[0].attempts[0].terminal_outcome == "blocked"
        assert lineage.phase_executions[0].attempts[1].run_id == successor_id

        replay = service.retry(source_run_id)
        assert replay.idempotent_replay is True
        assert replay.run_id == successor_id

        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
        for _ in range(120):
            tick.run_once()
            with tick.store.begin_read() as conn:
                successor, _, _ = tick.store.load_validated_snapshot(conn, successor_id)
                if successor.kind == "completed":
                    break
        assert successor.kind == "completed"
