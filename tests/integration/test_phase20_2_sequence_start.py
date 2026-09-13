"""Integration tests for Phase 20.2 scheduler sequence start."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_RUN_IDS,
    FIXED_SEQUENCE_ID,
    _prepare_service,
    _two_phase_manifest,
    _write_manifest,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.contracts import SafeNextActionKind, SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import ACTIVE_SEQUENCE_STATE_KIND
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
    backend: FakeAgentProcessBackend,
    admission: FakeGitAdmissionPort | None = None,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        admission or FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-20-2-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 80,
) -> None:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


def _prepare(git_repo: Path, scheduler_paths: dict[str, Path]) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )


def test_sequence_start_then_tick_admits_materialized_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    assert start.run_id == FIXED_RUN_IDS[0]
    assert start.run_state_kind == "authorized"

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        tick_owner_factory=lambda: "tick-integration",
    )
    receipt = tick.run_once()
    assert receipt.lease_acquired is True
    assert any(
        item.run_id == start.run_id and item.action == "admitted" for item in receipt.run_receipts
    )

    status = scheduler_status(start.run_id, db_path=scheduler_paths["db_path"])
    assert status.summary.state_kind == "admitted"
    assert status.summary.sequence_ordinal == 1
    assert status.summary.sequence_total_phases == 2


def test_active_sequence_status_shows_materialized_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    service = SequenceStatusService(SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]))
    status = service.get_status(FIXED_SEQUENCE_ID)
    assert status.state_kind == ACTIVE_SEQUENCE_STATE_KIND
    assert status.current_run_id == start.run_id
    assert status.current_ordinal == 1
    assert status.current_phase_name == "phase-one"


def test_sequence_start_does_not_create_second_run_or_phase_two(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    _prepare(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        second_planned = conn.execute(
            """
            SELECT planned_run_id FROM scheduler_sequence_entries
            WHERE sequence_id = ? AND ordinal = 2
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchone()
        second_run = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (str(second_planned[0]),),
        ).fetchone()
    assert int(run_count[0]) == 1
    assert second_run is None


def test_sequence_start_has_no_git_side_effects(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prepare(git_repo, scheduler_paths)
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("sequence start must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after


def test_materialized_run_is_addressable_by_scheduler_status(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    status = scheduler_status(start.run_id, db_path=scheduler_paths["db_path"])
    assert status.summary.run_id == start.run_id


def test_sequence_start_service_is_real() -> None:
    from ai_dev_loop.scheduler.application.sequence_start import SequenceStartService

    assert SequenceStartService is not None


def test_sequence_phase_one_completes_without_checkpoint_or_phase_two(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 12, 12, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="completed")
    sequence_status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(FIXED_SEQUENCE_ID)
    assert sequence_status.safe_next_action.kind == SafeNextActionKind.INSPECT_BLOCKED
    assert "Phase 20.3" in (sequence_status.safe_next_action.command or "")
    with tick.store.begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        second_run = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (FIXED_RUN_IDS[1],),
        ).fetchone()
    assert int(run_count[0]) == 1
    assert second_run is None


def test_sequence_waiting_codex_capacity_retains_reservation_and_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 12, 12, 30, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, start.run_id, target_kind="waiting_codex_capacity")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, start.run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID
        reservation = tick.store.get_reservation_for_run(conn, start.run_id)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert reservation is not None
    assert int(run_count[0]) == 1
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, start.run_id, target_kind="completed")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, start.run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert int(run_count[0]) == 1


def test_sequence_admission_failure_does_not_materialize_phase_two(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    _prepare(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 12, 13, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        admission=FakeGitAdmissionPort(
            resolved_root=str(git_repo.resolve()),
            status_porcelain=" M dirty.txt",
        ),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == start.run_id and item.action == "blocked" for item in receipt.run_receipts
    )
    sequence_status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(FIXED_SEQUENCE_ID)
    assert sequence_status.current_run_state_kind == "blocked"
    assert "Phase 20.3" not in (sequence_status.safe_next_action.command or "")
    with tick.store.begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        second_run = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (FIXED_RUN_IDS[1],),
        ).fetchone()
    assert int(run_count[0]) == 1
    assert second_run is None


def test_preassigned_run_exists_only_after_sequence_start(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    _prepare(git_repo, scheduler_paths)
    with pytest.raises(SchedulerEngineError, match="not found"):
        scheduler_status(FIXED_RUN_IDS[0], db_path=scheduler_paths["db_path"])
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    status = scheduler_status(start.run_id, db_path=scheduler_paths["db_path"])
    assert status.summary.run_id == FIXED_RUN_IDS[0]
