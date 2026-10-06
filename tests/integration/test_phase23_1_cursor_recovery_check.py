"""Real CLI integration for scheduler cursor-retry --check (Phase 23.1)."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import jsonschema
import pytest
from tests.integration.test_phase17_4_cursor_workflow import _run_until, _submit, _tick_service
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase23_1_historical"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _blocked_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _normalize_run_artifact_permissions,
    )

    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    return run_id


def _ledger_snapshot(db_path: Path) -> bytes:
    import hashlib
    import sqlite3

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        parts: list[bytes] = []
        for table in (
            "scheduler_events",
            "scheduler_attempts",
            "scheduler_effects",
            "scheduler_repository_reservations",
        ):
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            parts.append(repr(rows).encode("utf-8"))
        return hashlib.sha256(b"".join(parts)).digest()
    finally:
        conn.close()


def test_cli_check_repeated_preserves_ledger_and_reservations(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    db_path = scheduler_paths["db_path"]
    ledger_before = _ledger_snapshot(db_path)
    artifact_inventory_before = subprocess.check_output(
        [
            "find",
            str(scheduler_paths["artifact_root"]),
            "-type",
            "f",
            "-exec",
            "stat",
            "-c",
            "%a %n",
            "{}",
            "+",
        ],
        text=True,
    )
    artifact_bytes_before = subprocess.check_output(
        [
            "find",
            str(scheduler_paths["artifact_root"]),
            "-type",
            "f",
            "-exec",
            "sha256sum",
            "{}",
            "+",
        ],
        text=True,
    )
    agent_log = Path(fake_clis["agent_log"])
    agent_log_before = agent_log.read_bytes() if agent_log.is_file() else b""
    runner = CliRunner()
    for _ in range(3):
        result = runner.invoke(
            app,
            ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
        )
        assert result.exit_code == 0, result.output
    assert _ledger_snapshot(db_path) == ledger_before
    artifact_inventory_after = subprocess.check_output(
        [
            "find",
            str(scheduler_paths["artifact_root"]),
            "-type",
            "f",
            "-exec",
            "stat",
            "-c",
            "%a %n",
            "{}",
            "+",
        ],
        text=True,
    )
    assert artifact_inventory_after == artifact_inventory_before
    artifact_bytes_after = subprocess.check_output(
        [
            "find",
            str(scheduler_paths["artifact_root"]),
            "-type",
            "f",
            "-exec",
            "sha256sum",
            "{}",
            "+",
        ],
        text=True,
    )
    assert artifact_bytes_after == artifact_bytes_before
    agent_log_after = agent_log.read_bytes() if agent_log.is_file() else b""
    assert agent_log_after == agent_log_before


def test_cli_check_authenticated_json(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    db_before = scheduler_paths["db_path"].read_bytes()
    artifact_inventory_before = subprocess.check_output(
        ["find", str(scheduler_paths["artifact_root"]), "-type", "f"],
        text=True,
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    schema = json.loads(
        schema_path("scheduler-cursor-recovery-check-receipt-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(payload)
    assert payload["evidence_status"] == "authenticated"
    assert payload["recovery_supported"] is True
    assert payload["turn_kind"] == "initial"
    assert "prompt" not in result.output.lower()
    assert scheduler_paths["db_path"].read_bytes() == db_before
    artifact_inventory_after = subprocess.check_output(
        ["find", str(scheduler_paths["artifact_root"]), "-type", "f"],
        text=True,
    )
    assert artifact_inventory_after == artifact_inventory_before


def test_cli_check_missing_result_envelope_is_bounded(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
            resolve_decisive_failed_cursor_attempt,
        )

        attempt_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        result_rel = str(attempt["result_artifact_path"])
    result_path = run_artifact_root(scheduler_paths["artifact_root"], run_id) / result_rel
    result_path.unlink()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence_status"] == "corrupt"
    assert payload["reason_code"] in {
        "corrupt_artifacts_missing",
        "corrupt_completion_envelope",
    }
    assert "prompt" not in result.output.lower()


def test_cli_check_malformed_result_envelope_is_bounded(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
            resolve_decisive_failed_cursor_attempt,
        )

        attempt_id, _, _ = resolve_decisive_failed_cursor_attempt(store, conn, run_id, events)
        attempt = store.get_attempt_by_id(conn, attempt_id)
        assert attempt is not None
        result_rel = str(attempt["result_artifact_path"])
    result_path = run_artifact_root(scheduler_paths["artifact_root"], run_id) / result_rel
    result_path.write_text("{not-json", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence_status"] == "corrupt"
    assert payload["reason_code"] == "corrupt_completion_envelope"


def test_cli_check_missing_run() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", "missing-run-id", "--check", "--output", "json"],
    )
    assert result.exit_code != 0


def test_cli_no_flag_timeout_retry_still_works(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_cursor_timeout_retry import setup_run

    _tick, run_id, _backend = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--output", "json"],
    )
    assert result.exit_code == 0, result.output


def test_cli_check_sequence_blocked_run_reports_ordinal(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase17_4_cursor_workflow import _run_until, _tick_service
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _normalize_run_artifact_permissions,
    )

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    run_id = start.run_id
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=40)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["sequence_id"] == sequence_id
    assert payload["ordinal"] == 1
    assert payload["evidence_status"] == "authenticated"
    assert payload["recovery_supported"] is True


def test_cli_check_sequence_stale_leaf_is_ineligible(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase17_4_cursor_workflow import _run_until, _tick_service
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _normalize_run_artifact_permissions,
    )

    from ai_dev_loop.scheduler.domain.sequence import BlockedSequenceState
    from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
        SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        SequenceRunAttempt,
    )
    from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
        insert_sequence_run_attempt,
    )

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    run_id = start.run_id
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=40)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        sequence_state = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence_state, BlockedSequenceState)
    assert sequence_state.current_run_id == run_id
    replacement_run_id = f"{run_id}-later-leaf"
    current_entry = next(
        entry
        for entry in sequence_state.materialized_entries
        if entry.ordinal == sequence_state.current_ordinal
    )
    moved_entries = tuple(
        entry.model_copy(update={"run_id": replacement_run_id})
        if entry.ordinal == sequence_state.current_ordinal
        else entry
        for entry in sequence_state.materialized_entries
    )
    moved = sequence_state.model_copy(
        update={
            "version": sequence_state.version + 1,
            "current_run_id": replacement_run_id,
            "materialized_entries": moved_entries,
        }
    )
    kind, payload, digest = store.dump_sequence_state(moved)
    planned_run_id = sequence_state.definition.entries[
        sequence_state.current_ordinal - 1
    ].planned_run_id
    with store.begin_immediate() as conn:
        insert_sequence_run_attempt(
            conn,
            sequence_id=sequence_id,
            ordinal=sequence_state.current_ordinal,
            planned_run_id=planned_run_id,
            attempt=SequenceRunAttempt(
                schema_version=1,
                generation=2,
                run_id=replacement_run_id,
                source_run_id=run_id,
                attempt_kind=SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
                materialized_at=current_entry.materialized_at,
                terminal_outcome="blocked",
                resolved_at=sequence_state.blocked_at,
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?, version = ?
            WHERE sequence_id = ?
            """,
            (kind, payload, digest, moved.version, sequence_id),
        )
        store.load_validated_sequence_state(conn, sequence_id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload_out = json.loads(result.output)
    assert payload_out["reason_code"] == "ineligible_sequence_stale_leaf"
    assert payload_out["recovery_supported"] is False


def test_cli_check_aborted_sequence_run_is_ineligible(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase17_4_cursor_workflow import _run_until, _tick_service
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _normalize_run_artifact_permissions,
    )

    from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    run_id = start.run_id
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=40)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    SequenceAbortService(store, now_factory=lambda: NOW).abort_sequence(sequence_id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence_status"] == "ineligible"
    assert payload["reason_code"] == "ineligible_sequence_aborted"


def test_cli_check_extended_correction_bundle(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase17_4_cursor_workflow import _run_until, _submit, _tick_service
    from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _normalize_run_artifact_permissions,
    )

    from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
    from ai_dev_loop.scheduler.application.start import start_run

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=2)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="max_iterations_reached", max_ticks=120)
    ReviewBudgetExtendService(tick.store, tick.artifacts, now_factory=lambda: NOW).extend(
        run_id,
        target_total=3,
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=120)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=30)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["evidence_status"] == "authenticated"
    from ai_dev_loop.scheduler.application.cursor_recovery_check import (
        analyze_cursor_recovery_for_inspection,
    )

    analysis = analyze_cursor_recovery_for_inspection(
        run_id,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    assert analysis.evidence is not None
    assert analysis.evidence.iteration == 3
    assert analysis.evidence.reviews_completed == 2
    assert analysis.evidence.effective_review_ceiling == 3


def test_committed_dual_failure_fixture_is_present_and_not_rewritten() -> None:
    manifest_path = FIXTURES / "dual_failure_replay_manifest.json"
    engine_path = FIXTURES / "dual_failure_engine.sqlite3"
    assert manifest_path.is_file(), "missing committed dual_failure_replay_manifest.json"
    assert engine_path.is_file(), "missing committed dual_failure_engine.sqlite3"
    before = manifest_path.read_bytes()
    engine_before = engine_path.read_bytes()
    manifest = json.loads(before.decode("utf-8"))
    assert manifest["git_head"] == "47b58b545c2f87d14a1b7d5bbf34554ff972c225"
    assert manifest_path.read_bytes() == before
    assert engine_path.read_bytes() == engine_before
