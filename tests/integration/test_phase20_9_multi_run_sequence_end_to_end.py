"""Phase 20.9 multi-run sequence lifecycle end-to-end acceptance."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
    THREE_PHASE_RUN_IDS,
    _prepare_three_phase,
    _run_until,
    _tick_service,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    FIXED_RUN_IDS,
    _prepare_service,
    _write_manifest,
)
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
    _legacy_block_instead_of_retry,
    _run_until_blocked_sequence,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _tick_service as _tick_service_phase20_8,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.application.sequence_report import SEQUENCE_COMPLETION_REPORT_ARTIFACT
from ai_dev_loop.scheduler.application.sequence_restart_reconcile import (
    SequenceRestartReconcileService,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.sequence import (
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_review_retry_successor_exposes_lineage_on_cli_status(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    ReviewRetryService(store, artifacts).retry(source_run_id)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
    assert isinstance(sequence, ActiveSequenceState)
    assert len(lineage.phase_executions[0].attempts) == 2
    runner = CliRunner()
    result = runner.invoke(
        app, ["scheduler", "sequence", "status", sequence_id, "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["entries"][0]["attempt_count"] == 2
    assert "same_reviewer_retry" in payload["entries"][0]["attempt_kind_labels"]
    assert "prompt" not in result.output.lower()


def _four_phase_manifest() -> str:
    phases = []
    for index, name in enumerate(("phase-one", "phase-two", "phase-three", "phase-four"), start=1):
        phase = {
            "name": name,
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
        }
        if index < 4:
            phase["commit_message"] = f"checkpoint after {name}"
        phases.append(phase)
    return yaml.safe_dump(
        {"schema_version": 1, "name": "four-phase", "phases": phases}, sort_keys=False
    )


def test_two_phase_sequence_reaches_awaiting_finalization_with_cli_status(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = start_sequence(sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, FIXED_RUN_IDS[1], target_kind="completed")
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
    ).get_status(sequence_id)
    assert status.state_kind == AWAITING_FINALIZATION_SEQUENCE_STATE_KIND
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "sequence", "status", sequence_id, "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["state_kind"] == "awaiting_finalization"
    assert len(payload["entries"]) == 2
    staged_diff = subprocess.check_output(["git", "diff", "--cached"], cwd=git_repo, text=True)
    assert staged_diff.strip()


def test_four_phase_sequence_with_residual_risk_finalization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "blocked_environment")
    _write_manifest(git_repo / "sequence.yaml", _four_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    manifest = _write_manifest(git_repo / "sequence.yaml", _four_phase_manifest())
    prepared = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    with tick.store.begin_read() as conn:
        sequence = tick.store.load_validated_sequence_state(conn, prepared.sequence_id)
    run_ids = [entry.planned_run_id for entry in sequence.definition.entries]
    for run_id in run_ids:
        _run_until(tick, run_id, target_kind="completed_with_residual_risk")
    with tick.store.begin_read() as conn:
        sequence = tick.store.load_validated_sequence_state(conn, prepared.sequence_id)
        lineage = load_sequence_run_lineage(
            conn, prepared.sequence_id, definition=sequence.definition
        )
    assert isinstance(sequence, AwaitingFinalizationSequenceState)
    assert len(lineage.phase_executions) == 4
    assert all(len(phase.attempts) == 1 for phase in lineage.phase_executions)


def test_review_retry_successor_completes_two_phase_sequence_to_awaiting_finalization(
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
    recovery = ReviewRetryService(store, artifacts).retry(source_run_id)
    successor_id = recovery.run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_id, target_kind="completed")
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id != source_run_id
    assert len(lineage.phase_executions[0].attempts) == 2
    second_run_id = sequence.current_run_id
    _run_until(tick, second_run_id, target_kind="completed")
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AwaitingFinalizationSequenceState)


def test_same_ordinal_double_review_retry_stays_single_successor(
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
    service = ReviewRetryService(store, artifacts)
    first = service.retry(source_run_id)
    second = service.retry(source_run_id)
    assert second.idempotent_replay is True
    assert second.run_id == first.run_id
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT successor_run_id
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchall()
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
    assert len(rows) == 1
    assert len(lineage.phase_executions[0].attempts) == 2


def test_same_ordinal_second_retry_after_blocking_first_successor_completes_sequence(
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
    successor_one = service.retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    blocked_successor, _ = _run_until_blocked_sequence(tick, sequence_id)
    assert blocked_successor == successor_one
    successor_two = service.retry(successor_one).run_id
    assert successor_two != successor_one
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_two, target_kind="completed")
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
        replacement_rows = conn.execute(
            """
            SELECT source_run_id, successor_run_id
            FROM scheduler_sequence_execution_replacements
            WHERE source_run_id IN (?, ?)
            """,
            (source_run_id, successor_one),
        ).fetchall()
    assert isinstance(sequence, ActiveSequenceState)
    assert len(lineage.phase_executions[0].attempts) == 3
    assert len(replacement_rows) == 2
    second_phase_run_id = sequence.current_run_id
    _run_until(tick, second_phase_run_id, target_kind="completed")
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
        report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    assert isinstance(sequence, AwaitingFinalizationSequenceState)
    assert lineage.phase_executions[0].accepted_run_id == successor_two
    assert report_path.is_file()


def test_three_phase_dual_review_recovery_reaches_awaiting_finalization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_SEQUENCE_ID

    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    _prepare_three_phase(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service_phase20_8(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    service = ReviewRetryService(tick.store, tick.artifacts)
    source_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_one = service.retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_one, target_kind="completed")
    with tick.store.begin_read() as conn:
        sequence = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == THREE_PHASE_RUN_IDS[1]
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    blocked_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_two = service.retry(blocked_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_two, target_kind="completed")
    _run_until(tick, THREE_PHASE_RUN_IDS[2], target_kind="completed")
    with tick.store.begin_read() as conn:
        sequence = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        lineage = load_sequence_run_lineage(conn, FIXED_SEQUENCE_ID, definition=sequence.definition)
        run_count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()
    assert isinstance(sequence, AwaitingFinalizationSequenceState)
    assert len(lineage.phase_executions) == 3
    assert len(lineage.phase_executions[0].attempts) == 2
    assert len(lineage.phase_executions[1].attempts) == 2
    assert int(run_count[0]) == 5
    report_path = (
        tick.artifacts.sequence_root(FIXED_SEQUENCE_ID) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    )
    assert report_path.is_file()


def test_restart_reconcile_fault_injection_converges_on_second_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    finalize_calls = 0
    original_finalize = SqliteSchedulerStore.finalize_sequence_state
    fixed_now = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)

    def flaky_finalize(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return None
        return original_finalize(self, conn, **kwargs)

    monkeypatch.setattr(SqliteSchedulerStore, "finalize_sequence_state", flaky_finalize)
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = start_sequence(sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, FIXED_RUN_IDS[1], target_kind="completed")
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    handoff = SequenceHandoffService(store, artifacts, now_factory=lambda: fixed_now)
    restart = SequenceRestartReconcileService(
        store, artifacts, handoff=handoff, now_factory=lambda: fixed_now
    )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    final_run_id = sequence.current_run_id
    with store.begin_immediate() as conn:
        receipt = restart.reconcile_terminal_current_leaf(conn, final_run_id)
    assert receipt is not None
    assert receipt.action == "sequence_finalization_reconciled"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AwaitingFinalizationSequenceState)
