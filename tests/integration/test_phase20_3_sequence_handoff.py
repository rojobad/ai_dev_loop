"""Integration tests for Phase 20.3 sequence checkpoint handoff."""

from __future__ import annotations

import itertools
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_SEQUENCE_ID,
    _prepare_service,
    _write_manifest,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
THREE_PHASE_RUN_IDS = (
    "fixture-project-20260912T120000Z-run000",
    "fixture-project-20260912T120000Z-run001",
    "fixture-project-20260912T120000Z-run002",
)
_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _three_phase_manifest() -> str:
    phases = []
    for index, name in enumerate(("phase-one", "phase-two", "phase-final"), start=1):
        phase = {
            "name": name,
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
        }
        if index < 3:
            phase["commit_message"] = f"checkpoint after {name}"
        phases.append(phase)
    return yaml.safe_dump(
        {"schema_version": 1, "name": "three-phase", "phases": phases}, sort_keys=False
    )


def _tick_service(git_repo: Path, scheduler_paths: dict[str, Path]) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-20-3-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_COUNTER):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def _run_until(tick: TickService, run_id: str, *, target_kind: str) -> None:
    for _ in range(100):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


def _prepare_three_phase(git_repo: Path, scheduler_paths: dict[str, Path]) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _three_phase_manifest())
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


def test_three_phase_sequence_advances_through_checkpoints_and_finalizes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _prepare_three_phase(git_repo, scheduler_paths)
    start = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, THREE_PHASE_RUN_IDS[1], target_kind="completed")
    _run_until(tick, THREE_PHASE_RUN_IDS[2], target_kind="completed")
    sequence_status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    ).get_status(FIXED_SEQUENCE_ID)
    assert sequence_status.state_kind == AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
    assert sequence_status.current_run_id == THREE_PHASE_RUN_IDS[2]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo, text=True).strip()
    log = subprocess.check_output(["git", "log", "--oneline"], cwd=git_repo, text=True)
    assert "checkpoint after phase-one" in log
    assert "checkpoint after phase-two" in log
    assert head
    with tick.store.begin_read() as conn:
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
        reservation = conn.execute(
            "SELECT status FROM scheduler_repository_reservations WHERE worktree_key IS NOT NULL"
        ).fetchone()
    assert int(run_count[0]) == 3
    assert reservation is not None
    assert reservation[0] == "released"
