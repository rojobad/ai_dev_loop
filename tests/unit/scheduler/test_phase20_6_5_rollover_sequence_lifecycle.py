"""Disposable-repository sequence rollover lifecycle tests for Phase 20.6.5."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import (
    maxed_sequence_source_fixture,
)
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_review_budget_extend import _run_until
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.runners.git import checkpoint_git_rev_parse
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
from ai_dev_loop.scheduler.application.rollover_worktree import ProductionRolloverWorktreePort
from ai_dev_loop.scheduler.application.sequence_report import (
    SEQUENCE_COMPLETION_REPORT_ARTIFACT,
    set_completion_report_publication_step_hook,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.rollover import INTEGRATED_ROLLOVER_STATE_KIND
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    RecoveryIntegratedFinalizationSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    AwaitingCodexReviewState,
    CompletedState,
    WaitingForCursorFixState,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _tick_until_rollover_integrated(
    tick: TickService,
    *,
    rollover_id: str,
    rollover_run_id: str,
    max_ticks: int = 240,
) -> str:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            row = tick.store.get_authenticated_rollover(conn, rollover_id)
            if row is not None and str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND:
                break
            state, _, _ = tick.store.load_validated_snapshot(conn, rollover_run_id)
            if state.kind == "blocked":
                raise AssertionError(
                    f"rollover run blocked: {state.block_reason_kind}: {state.block_reason_summary}"
                )
    else:
        with tick.store.begin_read() as conn:
            row = tick.store.get_authenticated_rollover(conn, rollover_id)
            state, _, _ = tick.store.load_validated_snapshot(conn, rollover_run_id)
        raise AssertionError(
            f"rollover did not integrate (aggregate={row and row['state_kind']}, run={state.kind})"
        )
    with tick.store.begin_read() as conn:
        row = tick.store.get_authenticated_rollover(conn, rollover_id)
    assert row is not None
    payload = row["state_payload"]
    import json

    integrated = json.loads(str(payload))
    return str(integrated["integrated_commit_sha256"])


def _harden_sequence_artifact_permissions(artifacts, sequence_id: str) -> None:
    root = artifacts.sequence_root(sequence_id)
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if path.is_file():
            with contextlib.suppress(OSError):
                path.chmod(0o600)


def _harden_run_artifact_permissions(artifacts, run_id: str) -> None:
    run_root = artifacts.run_root(run_id)
    if not run_root.is_dir():
        return
    for path in run_root.rglob("*"):
        if path.is_file():
            with contextlib.suppress(OSError):
                path.chmod(0o600)


def _run_rollover_successor_to_completed(
    tick: TickService,
    rollover_run_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, rollover_run_id)
            if state.kind == "waiting_for_cursor_fix":
                break
    assert isinstance(state, WaitingForCursorFixState)
    assert state.fresh_rollover is not None
    for _ in range(30):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, rollover_run_id)
            if state.cursor.chat_id:
                break
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    for _ in range(120):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, rollover_run_id)
            if state.kind == "completed":
                break
    assert isinstance(state, CompletedState)
    _harden_run_artifact_permissions(tick.artifacts, rollover_run_id)


def test_non_final_sequence_rollover_lifecycle_through_integration(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    sequence_id, source_run_id, store, artifacts, fixed_now = maxed_sequence_source_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    head_before = checkpoint_git_rev_parse(git_repo, "HEAD")
    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="sequence rollover phase-one commit",
    )
    started = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    rollover_run_id = started.rollover_run_id
    with store.begin_read() as conn:
        rollover_state, _, _ = store.load_validated_snapshot(conn, rollover_run_id)
    assert isinstance(rollover_state, AwaitingCodexReviewState)
    assert rollover_state.fresh_rollover is not None
    assert rollover_state.context.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED
    assert rollover_state.context.sequence is None

    counter = itertools.count(3_000)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 15, 18, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-seq-rollover-{next(counter)}",
        attempt_id_factory=lambda: f"att-{next(counter):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _run_rollover_successor_to_completed(tick, rollover_run_id, monkeypatch)
    integrated_sha = _tick_until_rollover_integrated(
        tick,
        rollover_id=prepared.rollover_id,
        rollover_run_id=rollover_run_id,
    )
    assert checkpoint_git_rev_parse(git_repo, "HEAD") == integrated_sha
    assert integrated_sha != head_before
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        row = store.get_authenticated_rollover(conn, prepared.rollover_id)
    assert row is not None
    assert str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_ordinal == 2
    assert sequence.current_run_id != source_run_id
    with store.begin_read() as conn:
        reservation = store.get_reservation_for_run(conn, sequence.current_run_id)
        effects_before = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            (rollover_run_id,),
        ).fetchone()[0]
    assert reservation is not None
    for _ in range(5):
        tick.run_once()
    with store.begin_read() as conn:
        effects_after = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            (rollover_run_id,),
        ).fetchone()[0]
        row = store.get_authenticated_rollover(conn, prepared.rollover_id)
    assert effects_after == effects_before
    assert str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND


def test_final_sequence_rollover_lifecycle_through_integration(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    sequence_id, source_run_id, store, artifacts, _ = maxed_sequence_source_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    prepared_phase_one = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="sequence rollover phase-one commit",
    )
    started_phase_one = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared_phase_one.rollover_id)
    counter = itertools.count(4_000)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 15, 19, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-seq-final-{next(counter)}",
        attempt_id_factory=lambda: f"att-{next(counter):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _run_rollover_successor_to_completed(tick, started_phase_one.rollover_run_id, monkeypatch)
    _tick_until_rollover_integrated(
        tick,
        rollover_id=prepared_phase_one.rollover_id,
        rollover_run_id=started_phase_one.rollover_run_id,
    )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    phase_two_run_id = sequence.current_run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    _run_until(tick, phase_two_run_id, target_kind="max_iterations_reached", max_ticks=120)
    head_before = checkpoint_git_rev_parse(git_repo, "HEAD")
    prepared_final = RolloverPrepareService(store, artifacts).prepare(
        phase_two_run_id,
        commit_message="final sequence rollover commit",
    )
    started_final = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared_final.rollover_id)
    _run_rollover_successor_to_completed(tick, started_final.rollover_run_id, monkeypatch)
    _harden_sequence_artifact_permissions(artifacts, sequence_id)
    _harden_run_artifact_permissions(artifacts, started_final.rollover_run_id)
    integrated_sha = _tick_until_rollover_integrated(
        tick,
        rollover_id=prepared_final.rollover_id,
        rollover_run_id=started_final.rollover_run_id,
        max_ticks=240,
    )
    assert checkpoint_git_rev_parse(git_repo, "HEAD") == integrated_sha
    assert integrated_sha != head_before
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        row = store.get_authenticated_rollover(conn, prepared_final.rollover_id)
        reservation = store.get_reservation_for_run(conn, phase_two_run_id)
    assert row is not None
    assert str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND
    assert isinstance(sequence, RecoveryIntegratedFinalizationSequenceState)
    assert sequence.integrated_commit_sha256 == integrated_sha
    assert sequence.completion_report_sha256 is not None
    assert reservation is None
    report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    assert report_path.is_file()
    report_bytes = report_path.read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == sequence.completion_report_sha256
    with store.begin_read() as conn:
        effects_before = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            (started_final.rollover_run_id,),
        ).fetchone()[0]
    for _ in range(5):
        tick.run_once()
    with store.begin_read() as conn:
        effects_after = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
            (started_final.rollover_run_id,),
        ).fetchone()[0]
        row = store.get_authenticated_rollover(conn, prepared_final.rollover_id)
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert effects_after == effects_before
    assert str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND
    assert isinstance(sequence, RecoveryIntegratedFinalizationSequenceState)
    assert report_path.read_bytes() == report_bytes


def test_final_sequence_completion_report_crash_replay_via_ticks(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    sequence_id, source_run_id, store, artifacts, _ = maxed_sequence_source_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    prepared_phase_one = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="sequence rollover phase-one commit",
    )
    started_phase_one = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared_phase_one.rollover_id)
    base_now = datetime(2026, 9, 15, 20, 0, tzinfo=UTC)
    counter = itertools.count(5_000)
    clock = {"now": base_now}

    def advancing_now() -> datetime:
        return clock["now"]

    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=advancing_now,
        tick_owner_factory=lambda: f"tick-seq-crash-{next(counter)}",
        attempt_id_factory=lambda: f"att-{next(counter):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _run_rollover_successor_to_completed(tick, started_phase_one.rollover_run_id, monkeypatch)
    _tick_until_rollover_integrated(
        tick,
        rollover_id=prepared_phase_one.rollover_id,
        rollover_run_id=started_phase_one.rollover_run_id,
    )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    phase_two_run_id = sequence.current_run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    _run_until(tick, phase_two_run_id, target_kind="max_iterations_reached", max_ticks=120)
    prepared_final = RolloverPrepareService(store, artifacts).prepare(
        phase_two_run_id,
        commit_message="final sequence rollover commit",
    )
    started_final = RolloverStartService(
        store,
        artifacts,
        worktree_port=ProductionRolloverWorktreePort(),
    ).start(prepared_final.rollover_id)
    _run_rollover_successor_to_completed(tick, started_final.rollover_run_id, monkeypatch)
    _harden_sequence_artifact_permissions(artifacts, sequence_id)
    _harden_run_artifact_permissions(artifacts, started_final.rollover_run_id)
    crashed = {"value": False}

    def crash_before_db_record(step: str) -> None:
        if step == "before_db_record" and not crashed["value"]:
            crashed["value"] = True
            raise RuntimeError("simulated crash before sequence finalization CAS")

    set_completion_report_publication_step_hook(crash_before_db_record)
    head_before = checkpoint_git_rev_parse(git_repo, "HEAD")
    try:
        for _ in range(240):
            try:
                tick.run_once()
            except RuntimeError as exc:
                if "simulated crash" not in str(exc):
                    raise
                clock["now"] = base_now + timedelta(hours=2)
                set_completion_report_publication_step_hook(None)
            with store.begin_read() as conn:
                row = store.get_authenticated_rollover(conn, prepared_final.rollover_id)
                if row is not None and str(row["state_kind"]) == INTEGRATED_ROLLOVER_STATE_KIND:
                    break
        else:
            raise AssertionError("final rollover did not integrate after publication crash replay")
    finally:
        set_completion_report_publication_step_hook(None)
    report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    assert report_path.is_file()
    report_bytes = report_path.read_bytes()
    integrated_sha = checkpoint_git_rev_parse(git_repo, "HEAD")
    assert integrated_sha != head_before
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        reservation = store.get_reservation_for_run(conn, phase_two_run_id)
    assert isinstance(sequence, RecoveryIntegratedFinalizationSequenceState)
    assert sequence.integrated_commit_sha256 == integrated_sha
    assert sequence.completion_report_sha256 == hashlib.sha256(report_bytes).hexdigest()
    assert reservation is None
