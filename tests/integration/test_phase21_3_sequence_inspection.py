"""Integration tests for Phase 21.3 sequence inspection commands."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sqlite3
import subprocess
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.integration.test_phase17_4_cursor_workflow import (
    _run_until,
)
from tests.integration.test_phase17_4_cursor_workflow import (
    _tick_service as _run_tick_service,
)
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
)
from tests.integration.test_phase20_3_sequence_handoff import (
    _tick_service as _handoff_tick_service,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)
from tests.unit.scheduler.test_phase20_7_sequence_run_lineage import (
    _pause_v8_database,
    _prepare_sequence_without_migration,
    _start_sequence_without_migration,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
    _run_until_blocked_sequence,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from tests.unit.scheduler.test_v5_migration import _pause_v4_database
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.schemas import (
    SEQUENCE_INSPECT_DATA_SCHEMA,
    SEQUENCE_LIST_DATA_SCHEMA,
    SEQUENCE_PHASE_RUN_LIST_DATA_SCHEMA,
    SEQUENCE_REPORT_CHUNK_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.paths import set_sensitive_file_mode
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_report import (
    SEQUENCE_COMPLETION_REPORT_ARTIFACT,
    reconcile_completion_report_publication,
    set_completion_report_publication_step_hook,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

runner = CliRunner()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _invoke(args: list[str]) -> tuple[int, dict[str, object], str]:
    result = runner.invoke(app, args)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result.stdout


def _logical_ledger_snapshot(db_path: Path) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        runs = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT run_id, state_kind, version, idempotency_key, created_at
                FROM scheduler_runs
                ORDER BY run_id
                """
            )
        )
        events = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT event_id, run_id, sequence, event_kind
                FROM scheduler_events
                ORDER BY run_id, sequence
                """
            )
        )
        reservations = tuple(
            dict(row)
            for row in conn.execute(
                """
                SELECT worktree_key, run_id, status
                FROM scheduler_repository_reservations
                ORDER BY worktree_key
                """
            )
        )
        sequences = tuple(
            {
                "sequence_id": row["sequence_id"],
                "state_kind": row["state_kind"],
                "version": row["version"],
                "payload_sha256": row["payload_sha256"],
                "completion_report_sha256": json.loads(row["payload"]).get(
                    "completion_report_sha256"
                ),
            }
            for row in conn.execute(
                """
                SELECT sequence_id, state_kind, version, payload_sha256, payload
                FROM scheduler_sequences
                ORDER BY sequence_id
                """
            )
        )
    finally:
        conn.close()
    return {
        "user_version": version,
        "runs": runs,
        "events": events,
        "reservations": reservations,
        "sequences": sequences,
    }


@contextmanager
def _report_publication_read_spies():
    with (
        patch(
            "ai_dev_loop.scheduler.application.sequence_report.build_completion_report",
            MagicMock(name="build_completion_report"),
        ) as build_mock,
        patch(
            "ai_dev_loop.scheduler.application.sequence_report.ensure_completion_report",
            MagicMock(name="ensure_completion_report"),
        ) as ensure_mock,
        patch(
            "ai_dev_loop.scheduler.application.sequence_report.reconcile_completion_report_publication",
            MagicMock(name="reconcile_completion_report_publication"),
        ) as reconcile_mock,
    ):
        yield build_mock, ensure_mock, reconcile_mock


def _invoke_report_read_without_publishers(sequence_id: str) -> dict[str, object]:
    with _report_publication_read_spies() as (build_mock, ensure_mock, reconcile_mock):
        code, payload, _ = _invoke(["integration", "sequence", "report", sequence_id])
        assert code == 0
        build_mock.assert_not_called()
        ensure_mock.assert_not_called()
        reconcile_mock.assert_not_called()
        return payload["data"]


def _invoke_inspect_without_publishers(sequence_id: str) -> dict[str, object]:
    with _report_publication_read_spies() as (build_mock, ensure_mock, reconcile_mock):
        code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
        assert code == 0
        build_mock.assert_not_called()
        ensure_mock.assert_not_called()
        reconcile_mock.assert_not_called()
        return payload["data"]


def _git_snapshot(git_repo: Path) -> tuple[str, str]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo).decode().strip()
    index = subprocess.check_output(["git", "write-tree"], cwd=git_repo).decode().strip()
    return head, index


def _awaiting_finalization_after_retry_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    interrupt_publication_at: str | None = None,
) -> tuple[str, AwaitingFinalizationSequenceState, SqliteSchedulerStore, ProtectedArtifactStore]:
    sequence_id, source_run_id, tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    successor_id = ReviewRetryService(store, artifacts).retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_id, target_kind="completed")
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    second_run_id = active.current_run_id
    interrupted = False

    def publication_hook(step: str) -> None:
        nonlocal interrupted
        if (
            interrupt_publication_at is not None
            and step == interrupt_publication_at
            and not interrupted
        ):
            interrupted = True
            raise RuntimeError(f"simulated interrupt at {step}")

    if interrupt_publication_at is not None:
        set_completion_report_publication_step_hook(publication_hook)
    try:
        if interrupt_publication_at is not None:
            with pytest.raises(RuntimeError, match="simulated interrupt"):
                _run_until(tick, second_run_id, target_kind="completed")
        else:
            _run_until(tick, second_run_id, target_kind="completed")
    finally:
        if interrupt_publication_at is not None:
            set_completion_report_publication_step_hook(None)
    with store.begin_read() as conn:
        awaiting = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(awaiting, AwaitingFinalizationSequenceState)
    return sequence_id, awaiting, store, artifacts


def test_c01_prepared_sequence_list_and_inspect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    code, payload, _ = _invoke(["integration", "sequences", "list"])
    assert code == 0
    assert payload["ok"] is True
    validate_integration_instance(payload["data"], SEQUENCE_LIST_DATA_SCHEMA)
    items = payload["data"]["items"]
    assert any(item["sequenceId"] == sequence_id for item in items)
    code, inspect_payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    validate_integration_instance(inspect_payload["data"], SEQUENCE_INSPECT_DATA_SCHEMA)
    data = inspect_payload["data"]
    assert data["state"] == "prepared"
    assert data["currentRunId"] is None
    assert data["safeNextAction"]["sequenceId"] == sequence_id
    assert data["safeNextAction"]["runId"] is None
    assert data["report"]["available"] is False
    assert data["report"]["reason"] == "not_yet_produced"


def test_c02_retry_successor_lineage_via_integration_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, source_run_id, _tick, store, _artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    successor_id = ReviewRetryService(store, _artifacts).retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    blocked_successor, _ = _run_until_blocked_sequence(_tick, sequence_id)
    assert blocked_successor == successor_id
    successor_two = ReviewRetryService(store, _artifacts).retry(successor_id).run_id
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=sequence.definition)
    attempts = lineage.phase_executions[0].attempts
    assert len(attempts) == 3
    assert [attempt.run_id for attempt in attempts] == [
        source_run_id,
        successor_id,
        successor_two,
    ]
    code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    phase0 = payload["data"]["phases"][0]
    assert phase0["runCount"] == 3
    assert phase0["runs"][2]["runId"] == successor_two
    assert phase0["currentRunId"] == successor_two
    assert phase0["runs"][1]["sourceRunId"] == source_run_id
    code, runs_payload, _ = _invoke(
        [
            "integration",
            "sequence",
            "phase-runs",
            sequence_id,
            "--ordinal",
            "1",
            "--offset",
            "0",
            "--limit",
            "1",
        ]
    )
    assert code == 0
    validate_integration_instance(runs_payload["data"], SEQUENCE_PHASE_RUN_LIST_DATA_SCHEMA)
    assert runs_payload["data"]["runCount"] == 3
    assert runs_payload["data"]["page"]["hasMore"] is True
    assert runs_payload["data"]["items"][0]["runId"] == source_run_id
    code, tail_payload, _ = _invoke(
        [
            "integration",
            "sequence",
            "phase-runs",
            sequence_id,
            "--ordinal",
            "1",
            "--offset",
            "2",
            "--limit",
            "1",
        ]
    )
    assert code == 0
    assert tail_payload["data"]["items"][0]["runId"] == successor_two
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(_tick, successor_two, target_kind="completed")
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=active.definition)
    assert isinstance(active, ActiveSequenceState)
    assert [attempt.run_id for attempt in lineage.phase_executions[0].attempts] == [
        source_run_id,
        successor_id,
        successor_two,
    ]
    second_phase_run_id = active.current_run_id
    _run_until(_tick, second_phase_run_id, target_kind="completed")
    with store.begin_read() as conn:
        awaiting = store.load_validated_sequence_state(conn, sequence_id)
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=awaiting.definition)
    assert isinstance(awaiting, AwaitingFinalizationSequenceState)
    assert lineage.phase_executions[0].accepted_run_id == successor_two
    code, accepted_payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    phase0 = accepted_payload["data"]["phases"][0]
    assert phase0["acceptedRunId"] == successor_two
    assert phase0["currentRunId"] == successor_two
    assert [run["runId"] for run in phase0["runs"]] == [
        source_run_id,
        successor_id,
        successor_two,
    ]
    assert phase0["runs"][1]["sourceRunId"] == source_run_id
    phase1 = accepted_payload["data"]["phases"][1]
    assert phase1["currentRunId"] == second_phase_run_id
    assert phase1["acceptedRunId"] == second_phase_run_id


def test_c03_frozen_phase_plan_survives_repository_mutation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    plan_path = git_repo / "docs/plans/sample-plan.md"
    original = plan_path.read_text(encoding="utf-8")
    plan_path.write_text("mutated plan content", encoding="utf-8")
    code, payload, _ = _invoke(
        [
            "integration",
            "sequence",
            "phase-plan",
            sequence_id,
            "--ordinal",
            "1",
        ]
    )
    assert code == 0
    chunk = payload["data"]
    assert chunk["available"] is True
    plan_digest = chunk["sha256"]
    assert plan_digest == hashlib.sha256(original.encode("utf-8")).hexdigest()
    decoded = base64.b64decode(chunk["contentBase64"]).decode("utf-8")
    assert decoded == original
    plan_path.unlink()
    code, after_delete, _ = _invoke(
        [
            "integration",
            "sequence",
            "phase-plan",
            sequence_id,
            "--ordinal",
            "1",
        ]
    )
    assert code == 0
    assert base64.b64decode(after_delete["data"]["contentBase64"]).decode("utf-8") == original
    assert after_delete["data"]["sha256"] == plan_digest
    plan_path.write_text(original, encoding="utf-8")
    prompt_path = git_repo / "docs/plans/prompt_sample-plan.txt"
    prompt_original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text("mutated prompt", encoding="utf-8")
    code, prompt_payload, _ = _invoke(
        [
            "integration",
            "sequence",
            "phase-prompt",
            sequence_id,
            "--ordinal",
            "1",
        ]
    )
    assert code == 0
    prompt_decoded = base64.b64decode(prompt_payload["data"]["contentBase64"]).decode("utf-8")
    assert prompt_decoded == prompt_original
    assert (
        prompt_payload["data"]["sha256"]
        == hashlib.sha256(prompt_original.encode("utf-8")).hexdigest()
    )
    prompt_path.write_text(prompt_original, encoding="utf-8")


def test_c04_report_read_does_not_invoke_publishers(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, _, _, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    code, pending, _ = _invoke(["integration", "sequence", "report", sequence_id])
    assert code == 0
    validate_integration_instance(pending["data"], SEQUENCE_REPORT_CHUNK_SCHEMA)
    assert pending["data"]["available"] is False
    pending2 = _invoke_report_read_without_publishers(sequence_id)
    validate_integration_instance(pending2, SEQUENCE_REPORT_CHUNK_SCHEMA)
    assert pending2["reason"] in {"not_yet_produced", "publication_pending"}


def test_c05_sequence_reads_do_not_mutate_scheduler_db(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, _, _, _ = _awaiting_finalization_after_retry_successor(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    db = scheduler_paths["db_path"]
    ledger_before = _logical_ledger_snapshot(db)
    git_before = _git_snapshot(git_repo)
    code, inspect_payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    counts = inspect_payload["data"]["aggregateCounts"]
    assert counts["checkpointed"] >= 1
    phase0 = inspect_payload["data"]["phases"][0]
    assert phase0["checkpoint"]["commitSha256Prefix"] is not None
    _invoke(["integration", "sequences", "list"])
    _invoke(["integration", "sequence", "inspect", sequence_id])
    _invoke(["integration", "sequence", "phase-plan", sequence_id, "--ordinal", "1"])
    _invoke(["integration", "sequence", "phase-runs", sequence_id, "--ordinal", "1"])
    _invoke(["integration", "sequence", "report", sequence_id])
    ledger_after = _logical_ledger_snapshot(db)
    git_after = _git_snapshot(git_repo)
    assert ledger_before == ledger_after
    assert git_before == git_after


def test_c06_v4_database_sequence_list_returns_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _pause_v4_database(tmp_path)
    state_root = tmp_path / "state" / "ai_dev_loop"
    state_root.mkdir(parents=True)
    import shutil

    shutil.copy(db, state_root / "engine.sqlite3")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    code, payload, _ = _invoke(["integration", "sequences", "list"])
    assert code == 4
    assert payload["error"]["code"] == "UNSUPPORTED"


def test_c01_blocked_and_aborted_sequence_states_project(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, _, _, _, _ = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    assert payload["data"]["state"] == "blocked"
    assert payload["data"]["currentRunId"] is not None


def test_c01_aborted_sequence_cancelled_phase_and_frozen_inputs(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    SequenceAbortService(store, now_factory=lambda: FIXED_NOW).abort_sequence(sequence_id)
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(30):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, sequence_id)
            if isinstance(sequence, AbortedSequenceState):
                break
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, AbortedSequenceState)
    code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    assert payload["data"]["state"] == "aborted"
    phase_two = payload["data"]["phases"][1]
    assert phase_two["cancelled"] is True
    plan_path = git_repo / "docs/plans/sample-plan.md"
    original = plan_path.read_text(encoding="utf-8")
    plan_path.write_text("aborted-phase mutation", encoding="utf-8")
    code, plan_payload, _ = _invoke(
        ["integration", "sequence", "phase-plan", sequence_id, "--ordinal", "2"]
    )
    assert code == 0
    assert base64.b64decode(plan_payload["data"]["contentBase64"]).decode("utf-8") == original
    assert plan_payload["data"]["sha256"] == hashlib.sha256(original.encode("utf-8")).hexdigest()
    plan_path.unlink()
    code, plan_after_delete, _ = _invoke(
        ["integration", "sequence", "phase-plan", sequence_id, "--ordinal", "2"]
    )
    assert code == 0
    assert base64.b64decode(plan_after_delete["data"]["contentBase64"]).decode("utf-8") == original
    assert plan_after_delete["data"]["sha256"] == plan_payload["data"]["sha256"]
    plan_path.write_text(original, encoding="utf-8")
    prompt_path = git_repo / "docs/plans/prompt_sample-plan.txt"
    prompt_original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text("aborted-phase prompt mutation", encoding="utf-8")
    code, prompt_payload, _ = _invoke(
        ["integration", "sequence", "phase-prompt", sequence_id, "--ordinal", "2"]
    )
    assert code == 0
    assert (
        base64.b64decode(prompt_payload["data"]["contentBase64"]).decode("utf-8") == prompt_original
    )
    assert (
        prompt_payload["data"]["sha256"]
        == hashlib.sha256(prompt_original.encode("utf-8")).hexdigest()
    )
    prompt_path.unlink()
    code, prompt_after_delete, _ = _invoke(
        ["integration", "sequence", "phase-prompt", sequence_id, "--ordinal", "2"]
    )
    assert code == 0
    assert (
        base64.b64decode(prompt_after_delete["data"]["contentBase64"]).decode("utf-8")
        == prompt_original
    )
    assert prompt_after_delete["data"]["sha256"] == prompt_payload["data"]["sha256"]
    prompt_path.write_text(prompt_original, encoding="utf-8")
    plan_path.write_text(original, encoding="utf-8")
    del start


def test_c05_residual_risk_intermediate_and_final_phases_integration_cli(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    del fake_clis
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "blocked_environment")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    tick = _handoff_tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed_with_residual_risk")
    code, intermediate, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    validate_integration_instance(intermediate["data"], SEQUENCE_INSPECT_DATA_SCHEMA)
    assert intermediate["data"]["state"] == "active"
    phase_one = intermediate["data"]["phases"][0]
    phase_two = intermediate["data"]["phases"][1]
    assert phase_one["residualRisk"] is True
    assert phase_one["acceptedOutcome"] == "completed_with_residual_risk"
    assert phase_one["runs"][0]["terminalOutcome"] == "completed_with_residual_risk"
    assert phase_two["residualRisk"] is False
    assert phase_two["acceptedOutcome"] is None
    assert intermediate["data"]["aggregateCounts"]["residualRisk"] == 1
    assert intermediate["data"]["residualRiskOrdinals"] == [1]
    with tick.store.begin_read() as conn:
        active = tick.store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    second_run_id = active.current_run_id
    _run_until(tick, second_run_id, target_kind="completed_with_residual_risk")
    code, finalized, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    assert finalized["data"]["state"] == "awaiting_finalization"
    assert finalized["data"]["residualRisk"] is True
    assert finalized["data"]["aggregateCounts"]["residualRisk"] == 2
    assert finalized["data"]["residualRiskOrdinals"] == [1, 2]
    final_phase = finalized["data"]["phases"][1]
    assert final_phase["residualRisk"] is True
    assert final_phase["acceptedOutcome"] == "completed_with_residual_risk"
    assert final_phase["runs"][0]["terminalOutcome"] == "completed_with_residual_risk"
    assert finalized["data"]["phases"][0]["residualRisk"] is True


def test_c04_published_and_corrupt_report_cli_paths(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id, awaiting, store, artifacts = _awaiting_finalization_after_retry_successor(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
        interrupt_publication_at="after_report_write",
    )
    report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    assert report_path.is_file()
    assert awaiting.completion_report_sha256 is None
    db = scheduler_paths["db_path"]
    ledger_before_unbound = _logical_ledger_snapshot(db)
    pending = _invoke_report_read_without_publishers(sequence_id)
    assert pending["available"] is True
    assert pending["integrity"] == "schema_validated"
    report_bytes = report_path.read_bytes()
    unbound_sha = hashlib.sha256(report_bytes).hexdigest()
    assert pending["sha256"] == unbound_sha
    inspect_unbound = _invoke_inspect_without_publishers(sequence_id)
    assert inspect_unbound["report"]["available"] is True
    assert inspect_unbound["report"]["sha256Prefix"] == unbound_sha[:16]
    ledger_after_unbound = _logical_ledger_snapshot(db)
    assert ledger_before_unbound == ledger_after_unbound
    published = reconcile_completion_report_publication(
        store,
        artifacts,
        awaiting,
        now=FIXED_NOW + timedelta(hours=1),
    )
    bound_sha = published.completion_report_sha256
    assert bound_sha == unbound_sha
    bound_chunk = _invoke_report_read_without_publishers(sequence_id)
    assert bound_chunk["integrity"] == "ledger_sha256_bound"
    inspect_bound = _invoke_inspect_without_publishers(sequence_id)
    assert inspect_bound["report"]["available"] is True
    assert inspect_bound["report"]["sha256Prefix"] == bound_sha[:16]
    report_path.write_bytes(b"{not valid json")
    set_sensitive_file_mode(report_path)
    code, corrupt, _ = _invoke(["integration", "sequence", "report", sequence_id])
    assert code == 5
    assert corrupt["error"]["code"] == "DATA_INTEGRITY"
    tampered_bytes = report_bytes[:-1] + (b"x" if report_bytes[-1:] != b"x" else b"y")
    report_path.write_bytes(tampered_bytes)
    set_sensitive_file_mode(report_path)
    code, mismatch, _ = _invoke(["integration", "sequence", "report", sequence_id])
    assert code == 5
    assert mismatch["error"]["code"] == "DATA_INTEGRITY"
    report_path.unlink()
    ledger_before_missing = _logical_ledger_snapshot(db)
    missing = _invoke_report_read_without_publishers(sequence_id)
    assert missing["available"] is False
    assert missing["reason"] == "publication_pending"
    ledger_after_missing = _logical_ledger_snapshot(db)
    assert ledger_before_missing == ledger_after_missing
    assert ledger_after_missing["sequences"][0]["completion_report_sha256"] == bound_sha


def test_f04_safe_next_action_wait_until_from_run_state(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "120")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "seq-usage-limit-counter.txt"),
    )
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    run_id = _start_service(scheduler_paths).start(sequence_id).run_id
    now = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _run_tick_service(git_repo, scheduler_paths, now=now, backend=backend)
    _run_until(tick, run_id, target_kind="waiting_usage_limit")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
    wait_until = state.cursor.wait_until
    assert wait_until is not None
    code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    action = payload["data"]["safeNextAction"]
    assert action["kind"] == "wait_until"
    assert action["runId"] == run_id
    assert action["sequenceId"] == sequence_id
    assert action["waitUntil"] == wait_until
    assert wait_until not in (action.get("command") or "")


def test_f05_phase_input_errors_use_integration_envelopes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    isolated_xdg: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    code, missing_db, _ = _invoke(
        ["integration", "sequence", "phase-plan", "missing-sequence", "--ordinal", "1"]
    )
    assert code == 3
    assert missing_db["error"]["code"] == "NOT_FOUND"
    code, bad_ordinal, _ = _invoke(
        ["integration", "sequence", "phase-plan", sequence_id, "--ordinal", "99"]
    )
    assert code == 2
    assert bad_ordinal["error"]["code"] == "INVALID_ARGUMENT"
    db = _pause_v4_database(tmp_path)
    state_root = tmp_path / "state" / "ai_dev_loop"
    state_root.mkdir(parents=True)
    import shutil

    shutil.copy(db, state_root / "engine.sqlite3")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    code, unsupported, _ = _invoke(
        ["integration", "sequence", "phase-prompt", sequence_id, "--ordinal", "1"]
    )
    assert code == 4
    assert unsupported["error"]["code"] == "UNSUPPORTED"


def test_f02_v8_pre_lineage_database_projects_via_integration_cli(
    git_repo: Path,
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(isolated_xdg / "state"))
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    state_root.mkdir(parents=True)
    scheduler_paths = {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
    v8_src = _pause_v8_database(isolated_xdg)
    shutil.copy(v8_src, scheduler_paths["db_path"])
    assert (
        not sqlite3.connect(scheduler_paths["db_path"])
        .execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_sequence_run_attempts'"
        )
        .fetchone()
    )
    sequence_id = _prepare_sequence_without_migration(git_repo, scheduler_paths)
    start = _start_sequence_without_migration(scheduler_paths, sequence_id)
    run_id = start.run_id
    ledger_before = _logical_ledger_snapshot(scheduler_paths["db_path"])
    code, payload, _ = _invoke(["integration", "sequence", "inspect", sequence_id])
    assert code == 0
    assert payload["data"]["phases"][0]["runs"][0]["runId"] == run_id
    code, runs_payload, _ = _invoke(
        ["integration", "sequence", "phase-runs", sequence_id, "--ordinal", "1"]
    )
    assert code == 0
    assert runs_payload["data"]["items"][0]["runId"] == run_id
    ledger_after = _logical_ledger_snapshot(scheduler_paths["db_path"])
    assert ledger_before == ledger_after
