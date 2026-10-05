"""Phase 23.2 forced initial standalone Cursor recovery."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.test_phase17_4_cursor_workflow import _run_until, _submit, _tick_service
from tests.integration.test_phase23_1_cursor_recovery_check import _blocked_run
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
    CursorInitialRecoveryFault,
    CursorInitialRecoveryService,
    authenticated_initial_recovery_launch,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    analyze_cursor_recovery_evidence,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.cursor_contract import RUN_CURSOR_TURN_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    RECORD_REL,
    CursorInitialRecoveryPublicationIntentV1,
    CursorInitialRecoveryRecordV1,
    effective_prompt_bytes,
)
from ai_dev_loop.scheduler.domain.events import AbortRequestedEvent
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _snapshot(repo: Path) -> tuple[str, str]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-uall"],
        cwd=repo,
        text=True,
    )
    tree = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
    return status, tree


def _effect_kinds(store: SqliteSchedulerStore, run_id: str) -> list[str]:
    with store.begin_read() as conn:
        rows = conn.execute(
            "SELECT effect_kind FROM scheduler_effects WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    return [str(row["effect_kind"]) for row in rows]


def _state_kind(store: SqliteSchedulerStore, run_id: str) -> str:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    return state.kind


def test_force_publishes_one_successor_and_preserves_worktree(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    base = artifacts.read_verified_bytes(
        run_id,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    expected_prompt = effective_prompt_bytes(base)
    before = _snapshot(git_repo)
    agent_log = Path(fake_clis["agent_log"])
    log_before = agent_log.read_bytes() if agent_log.is_file() else b""
    runner = CliRunner()
    result = runner.invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["agent_execution_ready"] is True
    assert payload["publication_status"] == "ready"
    assert "Recovery note" not in result.output
    successor = str(payload["successor_run_id"])
    assert successor != run_id
    assert _state_kind(store, run_id) == "blocked"
    assert _snapshot(git_repo) == before
    log_after = agent_log.read_bytes() if agent_log.is_file() else b""
    assert log_after == log_before
    kinds = _effect_kinds(store, successor)
    assert kinds.count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    assert not any(kind.startswith("codex.") for kind in kinds)
    record_path = artifacts.run_root(successor) / RECORD_REL
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["chat_id"] == analysis.evidence.chat_id
    assert record["reviews_completed"] == 0
    assert record["reviewer_created"] is False
    effective = (artifacts.run_root(successor) / record["effective_prompt_path"]).read_bytes()
    assert effective == expected_prompt
    assert effective.count(b"Recovery note:") == 1
    replay = runner.invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert replay.exit_code == 0, replay.output
    replay_payload = json.loads(replay.output)
    assert replay_payload["successor_run_id"] == successor
    assert replay_payload["idempotent_replay"] is True
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "success")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="completed", max_ticks=40)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, successor)
    assert state.codex.reviews_completed == 1
    assert state.codex.reviewer_session_id
    launched = agent_log.read_text(encoding="utf-8")
    prompt_args = [
        ast.literal_eval(line.removeprefix("ARGS:"))
        for line in launched.splitlines()
        if line.startswith("ARGS:") and "Recovery note:" in line
    ]
    assert prompt_args
    assert prompt_args[-1][-1].encode("utf-8") == expected_prompt
    assert _state_kind(store, run_id) == "blocked"
    completed = runner.invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert completed.exit_code == 0, completed.output
    completed_payload = json.loads(completed.output)
    assert completed_payload["successor_run_id"] == successor
    assert completed_payload["idempotent_replay"] is True
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    assert _state_kind(store, successor) == "completed"


def test_failed_successor_appends_the_note_once_and_keeps_the_chat(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    base = artifacts.read_verified_bytes(
        run_id,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    runner = CliRunner()
    first = runner.invoke(app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"])
    assert first.exit_code == 0, first.output
    successor = str(json.loads(first.output)["successor_run_id"])
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="blocked", max_ticks=20)
    second = runner.invoke(
        app,
        ["scheduler", "cursor-retry", successor, "--force", "--output", "json"],
    )
    assert second.exit_code == 0, second.output
    grandchild = str(json.loads(second.output)["successor_run_id"])
    record = json.loads((artifacts.run_root(grandchild) / RECORD_REL).read_text(encoding="utf-8"))
    effective = (artifacts.run_root(grandchild) / record["effective_prompt_path"]).read_bytes()
    assert effective == effective_prompt_bytes(base)
    assert effective.count(b"Recovery note:") == 1
    assert record["chat_id"] == analysis.evidence.chat_id
    assert record["parent_source_run_id"] == run_id
    assert record["parent_record_sha256"]
    assert _state_kind(store, run_id) == "blocked"
    assert _state_kind(store, successor) == "blocked"


def test_pending_publication_does_not_dispatch_until_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = CursorInitialRecoveryService(store, artifacts, fault_point="after_candidate")
    with pytest.raises(CursorInitialRecoveryFault):
        service.force(run_id)
    with store.begin_read() as conn:
        pending = store.list_pending_cursor_initial_recoveries(conn)
    assert len(pending) == 1
    row = pending[0]
    successor = str(row["successor_run_id"])
    assert str(row["status"]) == "pending"
    assert _effect_kinds(store, successor) == []
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "success")
    tick.run_once()
    with store.begin_read() as conn:
        ready = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert ready is not None
    assert str(ready["status"]) == "ready"
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_crash_after_artifacts_converges_to_one_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = CursorInitialRecoveryService(store, artifacts, fault_point="after_artifacts")
    with pytest.raises(CursorInitialRecoveryFault):
        service.force(run_id)
    with store.begin_read() as conn:
        pending = store.list_pending_cursor_initial_recoveries(conn)
    assert len(pending) == 1
    successor = str(pending[0]["successor_run_id"])
    assert (artifacts.run_root(successor) / RECORD_REL).is_file()
    assert _effect_kinds(store, successor) == []
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    tick.run_once()
    with store.begin_read() as conn:
        ready = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert ready is not None
    assert str(ready["status"]) == "ready"
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_concurrent_force_publishes_one_relation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    barrier = threading.Barrier(2)
    service = CursorInitialRecoveryService(store, artifacts, before_create=barrier.wait)
    errors: list[BaseException] = []
    results: list[str] = []

    def invoke() -> None:
        try:
            results.append(service.force(run_id).successor_run_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(results)) == 1
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=run_id)
    assert len(rows) == 1
    assert _effect_kinds(store, results[0]).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_abort_pending_successor_cancels_without_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = CursorInitialRecoveryService(store, artifacts, fault_point="after_candidate")
    with pytest.raises(CursorInitialRecoveryFault):
        service.force(run_id)
    with store.begin_read() as conn:
        pending = store.list_pending_cursor_initial_recoveries(conn)
    assert len(pending) == 1
    successor = str(pending[0]["successor_run_id"])
    with store.begin_read() as conn:
        source_state, _, _ = store.load_validated_snapshot(conn, run_id)
        active = store.get_active_reservation(
            conn,
            source_state.context.repository.worktree_key,
        )
    assert active is not None
    assert str(active["run_id"]) == successor
    scheduler_abort_run(
        successor,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    tick.run_once()
    with store.begin_read() as conn:
        cancelled = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert cancelled is not None
    assert str(cancelled["status"]) == "cancelled"
    assert RUN_CURSOR_TURN_EFFECT_KIND not in _effect_kinds(store, successor)
    assert _state_kind(store, run_id) == "blocked"


def test_force_rejects_correction_and_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase17_4_cursor_workflow import _tick_service as tick_factory
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
    from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
        _blocked_correction_failure,
        _normalize_run_artifact_permissions,
    )

    correction = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    runner = CliRunner()
    rejected = runner.invoke(app, ["scheduler", "cursor-retry", correction, "--force"])
    assert rejected.exit_code == 4, rejected.output
    assert "correction" in rejected.output.lower()

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    started = _start_service(scheduler_paths).start(sequence_id)
    tick = tick_factory(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, started.run_id, target_kind="blocked", max_ticks=40)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], started.run_id)
    sequence_rejected = runner.invoke(
        app,
        ["scheduler", "cursor-retry", started.run_id, "--force"],
    )
    assert sequence_rejected.exit_code == 4, sequence_rejected.output
    assert "sequence" in sequence_rejected.output.lower()


def _check_reason(run_id: str) -> str:
    result = CliRunner().invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--check", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["reason_code"])


def _assert_force_rejects(run_id: str, reason: str) -> None:
    result = CliRunner().invoke(app, ["scheduler", "cursor-retry", run_id, "--force"])
    assert result.exit_code == 4, result.output
    assert reason in result.output


def test_force_rejects_wrong_block_reason(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
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
    assert _check_reason(run_id) == "ineligible_block_reason"
    _assert_force_rejects(run_id, "ineligible_block_reason")


def test_force_rejects_uncertain_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        updated = conn.execute(
            """
            UPDATE scheduler_attempts
            SET status = 'uncertain'
            WHERE run_id = ? AND status = 'failed'
            """,
            (run_id,),
        )
        assert updated.rowcount == 1
    assert _check_reason(run_id) == "ineligible_active_process"
    _assert_force_rejects(run_id, "ineligible_active_process")


def test_force_rejects_durable_abort_request(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        store.append_event(
            conn,
            event_id=str(uuid.uuid4()),
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=AbortRequestedEvent(run_id=run_id, reason="user_requested_abort"),
            now=NOW,
        )
    assert _check_reason(run_id) == "ineligible_abort_pending"
    _assert_force_rejects(run_id, "ineligible_abort_pending")


def test_force_rejects_checkpoint_hold(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="a" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=False,
            now=NOW,
        )
    assert _check_reason(run_id) == "ineligible_checkpoint_hold"
    _assert_force_rejects(run_id, "ineligible_checkpoint_hold")


def test_force_rejects_missing_prompt_evidence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    (artifacts.run_root(run_id) / analysis.evidence.prompt_path).unlink()
    reason = _check_reason(run_id)
    assert reason.startswith("corrupt_")
    _assert_force_rejects(run_id, reason)


def test_force_rejects_conflicting_reservation_owner(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO
    from unittest.mock import patch

    from tests.integration.test_phase17_4_cursor_workflow import FIXTURE_REPO
    from tests.unit.scheduler.helpers import CONTROLLER_SESSION

    from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run

    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        resubmission_id="22222222-2222-2222-2222-222222222222",
    )
    with patch("sys.stdin", StringIO(prompt)):
        other = submit_run(options).run_id
    assert other != run_id
    start_run(other, db_path=scheduler_paths["db_path"])
    assert _check_reason(run_id) == "ineligible_reservation_conflict"
    _assert_force_rejects(run_id, "ineligible_reservation_conflict")


def test_tampered_record_and_missing_parent_reject_replay_and_creation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    runner = CliRunner()
    published = runner.invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"],
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    record_path = artifacts.run_root(successor) / RECORD_REL
    original = record_path.read_bytes()
    record_path.write_bytes(original + b" ")
    os.chmod(record_path, 0o600)
    tampered = runner.invoke(app, ["scheduler", "cursor-retry", run_id, "--force"])
    assert tampered.exit_code == 1, tampered.output
    assert "failed authentication" in tampered.output
    record_path.write_bytes(original)
    os.chmod(record_path, 0o600)

    record = CursorInitialRecoveryRecordV1.model_validate_json(original)
    rewritten = record.model_copy(update={"successor_run_id": f"{successor}-other"})
    rewritten_bytes = rewritten.canonical_bytes()
    record_path.write_bytes(rewritten_bytes)
    os.chmod(record_path, 0o600)
    digest = hashlib.sha256(rewritten_bytes).hexdigest()
    with store.begin_immediate() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        assert row is not None
        intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(
            str(row["intent_payload"])
        )
        updated_intent = intent.model_copy(update={"record_sha256": digest})
        intent_bytes = updated_intent.canonical_bytes()
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
            WHERE recovery_key = ?
            """,
            (
                digest,
                intent_bytes.decode("utf-8"),
                hashlib.sha256(intent_bytes).hexdigest(),
                str(row["recovery_key"]),
            ),
        )
    owner = runner.invoke(app, ["scheduler", "cursor-retry", run_id, "--force"])
    assert owner.exit_code == 1, owner.output
    assert "disagrees with publication intent" in owner.output
    record_path.write_bytes(original)
    os.chmod(record_path, 0o600)
    original_digest = hashlib.sha256(original).hexdigest()
    with store.begin_read() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert row is not None
    restored_intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(
        str(row["intent_payload"])
    ).model_copy(update={"record_sha256": original_digest})
    restored_bytes = restored_intent.canonical_bytes()
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (
                original_digest,
                restored_bytes.decode("utf-8"),
                hashlib.sha256(restored_bytes).hexdigest(),
                successor,
            ),
        )

    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="blocked", max_ticks=20)
    record_path.unlink()
    missing_parent = runner.invoke(app, ["scheduler", "cursor-retry", successor, "--force"])
    assert missing_parent.exit_code == 1, missing_parent.output
    assert "failed authentication" in missing_parent.output
    with store.begin_read() as conn:
        created = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=successor)
    assert created == []


def test_check_and_force_are_mutually_exclusive() -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["scheduler", "cursor-retry", "run", "--check", "--force"],
    )
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output


def _backend() -> FakeAgentProcessBackend:
    return FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )


def _pending_candidate(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, SqliteSchedulerStore, ProtectedArtifactStore]:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = CursorInitialRecoveryService(store, artifacts, fault_point="after_candidate")
    with pytest.raises(CursorInitialRecoveryFault):
        service.force(run_id)
    with store.begin_read() as conn:
        pending = store.list_pending_cursor_initial_recoveries(conn)
    assert len(pending) == 1
    return run_id, str(pending[0]["successor_run_id"]), store, artifacts


def _result_artifact(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
    successor_run_id: str,
) -> Path:
    with store.begin_read() as conn:
        row = store.get_cursor_initial_recovery_by_successor(
            conn,
            successor_run_id=successor_run_id,
        )
        assert row is not None
        attempt = store.get_attempt_by_id(conn, str(row["failed_attempt_id"]))
    assert attempt is not None
    return artifacts.run_root(source_run_id) / str(attempt["result_artifact_path"])


def _replace_bytes(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    os.chmod(path, 0o600)


def test_removed_failure_evidence_does_not_publish_an_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, successor, store, artifacts = _pending_candidate(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    result_path = _result_artifact(store, artifacts, run_id, successor)
    original = result_path.read_bytes()
    result_path.unlink()
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="no longer authenticated"):
        service.force(run_id)
    tick = _tick_service(git_repo, scheduler_paths, now=NOW, backend=_backend())
    receipt = tick.run_once()
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
    assert _effect_kinds(store, successor) == []
    assert _state_kind(store, run_id) == "blocked"
    _replace_bytes(result_path, b"corrupted-result")
    with pytest.raises(SchedulerEngineError, match="no longer authenticated"):
        service.force(run_id)
    assert _effect_kinds(store, successor) == []
    _replace_bytes(result_path, original)
    ready = CursorInitialRecoveryService(store, artifacts).force(run_id)
    assert ready.publication_status == "ready"
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    assert _state_kind(store, run_id) == "blocked"


def test_abort_hold_and_foreign_reservation_block_publication(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, successor, store, artifacts = _pending_candidate(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="a" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=False,
            now=NOW,
        )
    with pytest.raises(SchedulerEngineError, match="checkpoint hold"):
        CursorInitialRecoveryService(store, artifacts).force(run_id)
    tick = _tick_service(git_repo, scheduler_paths, now=NOW, backend=_backend())
    tick.run_once()
    assert _effect_kinds(store, successor) == []
    with store.begin_immediate() as conn:
        conn.execute("DELETE FROM scheduler_checkpoint_holds WHERE run_id = ?", (run_id,))
        store.append_event(
            conn,
            event_id=f"evt-{uuid.uuid4()}",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=AbortRequestedEvent(run_id=run_id, reason="user_requested_abort"),
            now=NOW,
        )
    with pytest.raises(SchedulerEngineError, match="durable abort"):
        CursorInitialRecoveryService(store, artifacts).force(run_id)
    tick.run_once()
    assert _effect_kinds(store, successor) == []
    with store.begin_immediate() as conn:
        conn.execute(
            "DELETE FROM scheduler_events WHERE run_id = ? AND event_kind = 'abort_requested'",
            (run_id,),
        )
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        conn.execute(
            """
            UPDATE scheduler_repository_reservations
            SET run_id = ?
            WHERE worktree_key = ? AND status = 'active'
            """,
            (run_id, source.context.repository.worktree_key),
        )
    with pytest.raises(SchedulerEngineError, match="held by another run"):
        CursorInitialRecoveryService(store, artifacts).force(run_id)
    tick.run_once()
    assert _effect_kinds(store, successor) == []
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        active = store.get_active_reservation(conn, source.context.repository.worktree_key)
    assert active is not None
    assert str(active["run_id"]) == run_id
    assert _state_kind(store, run_id) == "blocked"


def test_missing_prompt_isolates_reconciliation_and_resumes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, successor, store, artifacts = _pending_candidate(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    exploding = CursorInitialRecoveryService(store, artifacts)

    def explode(row: object) -> object:
        del row
        raise RuntimeError("programming error")

    exploding._resume = explode  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="programming error"):
        exploding.reconcile_pending()
    other_repo = git_repo.parent / "unrelated-repo"
    shutil.copytree(git_repo, other_repo)
    other_id = _submit(other_repo, scheduler_paths)
    start_run(other_id, db_path=scheduler_paths["db_path"])
    with store.begin_read() as conn:
        before, _, _ = store.load_validated_snapshot(conn, other_id)
        source, _, _ = store.load_validated_snapshot(conn, run_id)
    plan = artifacts.run_root(run_id) / source.context.plan_prompt.plan_artifact_path
    original = plan.read_bytes()
    plan.unlink()
    with pytest.raises(ProtectedArtifactError):
        CursorInitialRecoveryService(store, artifacts).force(run_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=_backend(),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == successor
        and item.action == "cursor_initial_recovery_evidence_invalid"
        and item.detail == "ProtectedArtifactError"
        for item in receipt.run_receipts
    )
    assert any(item.run_id == other_id for item in receipt.run_receipts)
    with store.begin_read() as conn:
        after, _, _ = store.load_validated_snapshot(conn, other_id)
    assert after.kind != before.kind
    assert _effect_kinds(store, successor) == []
    _replace_bytes(plan, original)
    published = CursorInitialRecoveryService(store, artifacts).force(run_id)
    assert published.publication_status == "ready"
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def _prompt_args(log_path: Path) -> list[bytes]:
    if not log_path.is_file():
        return []
    prompts: list[bytes] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ARGS:"):
            continue
        args = ast.literal_eval(line.removeprefix("ARGS:"))
        prompts.append(str(args[-1]).encode("utf-8"))
    return prompts


def _drive_until(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    run_id: str,
    *,
    target_kind: str,
) -> tuple[object, bytes | None, int | None]:
    from tests.integration.test_phase17_4_cursor_workflow import _next_attempt_id
    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.tick import TickService

    clock = [datetime(2026, 10, 6, tzinfo=UTC)]
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"tick-23-2-{uuid.uuid4()}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=_backend(),
        preflight_port=OkPreflightPort(),
    )
    continuation: bytes | None = None
    waiting_iteration: int | None = None
    observed: list[tuple[str, str]] = []
    for _ in range(40):
        receipt = tick.run_once()
        observed.extend((item.action, item.detail) for item in receipt.run_receipts)
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "waiting_usage_limit":
            if continuation is None:
                path = state.cursor.continuation_envelope_path
                assert path
                continuation = (artifacts.run_root(run_id) / path).read_bytes()
                waiting_iteration = state.cursor.iteration
            clock[0] = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
            clock[0] += timedelta(seconds=1)
            continue
        if state.kind == target_kind:
            return state, continuation, waiting_iteration
        if state.kind == "blocked":
            raise AssertionError(
                f"blocked before {target_kind}: {state.block_reason_kind} "
                f"{state.block_reason_summary} actions={[item.action for item in receipt.run_receipts]}"
            )
    raise AssertionError(f"did not reach {target_kind}; last={state.kind} actions={observed[-8:]}")


def test_usage_limit_continuation_keeps_the_current_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    published = CliRunner().invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    record = json.loads((artifacts.run_root(successor) / RECORD_REL).read_text(encoding="utf-8"))
    effective = (artifacts.run_root(successor) / record["effective_prompt_path"]).read_bytes()
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,success")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    state, continuation, waiting_iteration = _drive_until(
        git_repo, scheduler_paths, successor, target_kind="completed"
    )
    assert waiting_iteration == 1
    assert continuation is not None
    assert continuation != effective
    prompts = _prompt_args(log_path)
    assert effective in prompts
    assert continuation in prompts
    assert state.cursor.chat_id == analysis.evidence.chat_id
    assert state.codex.reviewer_session_id
    assert state.codex.reviews_completed == 1


def test_correction_usage_limit_is_not_overridden_by_iteration_one(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    published = CliRunner().invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    effective = (
        artifacts.run_root(successor) / "prompts/cursor-initial-recovery/effective.txt"
    ).read_bytes()
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success,usage_limit_retry,success")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    state, continuation, waiting_iteration = _drive_until(
        git_repo, scheduler_paths, successor, target_kind="completed"
    )
    assert waiting_iteration == 2
    assert continuation is not None
    assert continuation != effective
    assert continuation in _prompt_args(log_path)
    assert state.cursor.chat_id == analysis.evidence.chat_id
    assert state.codex.reviewer_session_id
    assert state.codex.reviews_completed == 2


def _rewrite_ready_record(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    successor: str,
    *,
    record_updates: dict[str, object],
    intent_updates: dict[str, object],
    ledger_dispatch_id: str | None = None,
    effective_bytes: bytes | None = None,
) -> None:
    path = artifacts.run_root(successor) / RECORD_REL
    record = CursorInitialRecoveryRecordV1.model_validate_json(path.read_bytes())
    rewritten = record.model_copy(update=record_updates)
    payload = rewritten.canonical_bytes()
    _replace_bytes(path, payload)
    if effective_bytes is not None:
        _replace_bytes(
            artifacts.run_root(successor) / rewritten.effective_prompt_path, effective_bytes
        )
    digest = hashlib.sha256(payload).hexdigest()
    with store.begin_immediate() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        assert row is not None
        intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(
            str(row["intent_payload"])
        )
        updated = intent.model_copy(update={**intent_updates, "record_sha256": digest})
        intent_bytes = updated.canonical_bytes()
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?,
                dispatch_id = COALESCE(?, dispatch_id)
            WHERE recovery_key = ?
            """,
            (
                digest,
                intent_bytes.decode("utf-8"),
                hashlib.sha256(intent_bytes).hexdigest(),
                ledger_dispatch_id,
                str(row["recovery_key"]),
            ),
        )


def test_hash_consistent_binding_changes_fail_against_durable_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    published = CliRunner().invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    record_path = artifacts.run_root(successor) / RECORD_REL
    original_record = record_path.read_bytes()
    record = CursorInitialRecoveryRecordV1.model_validate_json(original_record)
    effective_path = artifacts.run_root(successor) / record.effective_prompt_path
    original_effective = effective_path.read_bytes()
    tampered_dispatch = f"{record.dispatch_id}-tampered"
    _rewrite_ready_record(
        store,
        artifacts,
        successor,
        record_updates={"dispatch_id": tampered_dispatch},
        intent_updates={"dispatch_id": tampered_dispatch},
        ledger_dispatch_id=tampered_dispatch,
    )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="disagrees with the failed attempt"):
        service.force(run_id)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="disagrees with the failed attempt"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, successor)
    _replace_bytes(record_path, original_record)
    with store.begin_read() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert row is not None
    restored_intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(
        str(row["intent_payload"])
    ).model_copy(
        update={
            "dispatch_id": record.dispatch_id,
            "record_sha256": hashlib.sha256(original_record).hexdigest(),
        }
    )
    restored_intent_bytes = restored_intent.canonical_bytes()
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET dispatch_id = ?, record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (
                record.dispatch_id,
                hashlib.sha256(original_record).hexdigest(),
                restored_intent_bytes.decode("utf-8"),
                hashlib.sha256(restored_intent_bytes).hexdigest(),
                successor,
            ),
        )
    wrong_effective = original_effective + b"extra"
    _rewrite_ready_record(
        store,
        artifacts,
        successor,
        record_updates={"effective_prompt_sha256": hashlib.sha256(wrong_effective).hexdigest()},
        intent_updates={},
        effective_bytes=wrong_effective,
    )
    with pytest.raises(SchedulerEngineError, match="not the authenticated base plus one note"):
        service.force(run_id)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="not the authenticated base plus one note"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, successor)
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_hash_consistent_admission_and_prompt_disagree_with_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    published = CliRunner().invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    record_path = artifacts.run_root(successor) / RECORD_REL
    original_record = record_path.read_bytes()
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert row is not None
    original_intent = str(row["intent_payload"]).encode("utf-8")
    plan_path = source.context.plan_prompt.plan_artifact_path
    plan_sha = source.context.plan_prompt.plan_sha256
    _rewrite_ready_record(
        store,
        artifacts,
        successor,
        record_updates={
            "admitted_artifact_path": plan_path,
            "admitted_artifact_sha256": plan_sha,
        },
        intent_updates={
            "admitted_artifact_path": plan_path,
            "admitted_artifact_sha256": plan_sha,
        },
    )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="durable admission"):
        service.force(run_id)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="durable admission"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, successor)
    _replace_bytes(record_path, original_record)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (
                hashlib.sha256(original_record).hexdigest(),
                original_intent.decode("utf-8"),
                hashlib.sha256(original_intent).hexdigest(),
                successor,
            ),
        )
    decoy = b"decoy-base-prompt-not-the-failed-invocation\n"
    decoy_rel = "prompts/decoy-base.txt"
    artifacts.publish_or_verify_bytes(successor, decoy_rel, decoy, max_bytes=1024 * 1024)
    effective = effective_prompt_bytes(decoy)
    _rewrite_ready_record(
        store,
        artifacts,
        successor,
        record_updates={
            "base_prompt_path": decoy_rel,
            "base_prompt_sha256": hashlib.sha256(decoy).hexdigest(),
            "effective_prompt_sha256": hashlib.sha256(effective).hexdigest(),
        },
        intent_updates={
            "base_prompt_path": decoy_rel,
            "base_prompt_sha256": hashlib.sha256(decoy).hexdigest(),
        },
        effective_bytes=effective,
    )
    with pytest.raises(SchedulerEngineError, match="failed invocation"):
        service.force(run_id)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="failed invocation"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, successor)


def test_ready_commit_and_launch_recheck_causal_evidence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, successor, store, artifacts = _pending_candidate(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    result_path = _result_artifact(store, artifacts, run_id, successor)
    original = result_path.read_bytes()

    def remove_result() -> None:
        result_path.unlink()

    with pytest.raises(SchedulerEngineError, match="no longer authenticated"):
        CursorInitialRecoveryService(store, artifacts, before_ready_commit=remove_result).force(
            run_id
        )
    with store.begin_read() as conn:
        pending = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert pending is not None
    assert str(pending["status"]) == "pending"
    assert attempts == []
    assert _effect_kinds(store, successor) == []
    _replace_bytes(result_path, original)
    ready = CursorInitialRecoveryService(store, artifacts).force(run_id)
    assert ready.publication_status == "ready"
    assert ready.agent_execution_ready is True
    result_path.unlink()
    other_repo = git_repo.parent / "dispatch-unrelated-repo"
    shutil.copytree(git_repo, other_repo)
    other_id = _submit(other_repo, scheduler_paths)
    start_run(other_id, db_path=scheduler_paths["db_path"])
    with store.begin_read() as conn:
        before, _, _ = store.load_validated_snapshot(conn, other_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    receipt = tick.run_once()
    observed = [(item.run_id, item.action, item.detail) for item in receipt.run_receipts]
    assert any(
        item.run_id == successor
        and item.action == "cursor_initial_recovery_evidence_invalid"
        and item.detail == "SchedulerEngineError"
        for item in receipt.run_receipts
    ), observed
    assert any(item.run_id == other_id for item in receipt.run_receipts)
    with store.begin_read() as conn:
        after, _, _ = store.load_validated_snapshot(conn, other_id)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert attempts == []
    assert after.kind != before.kind
    assert _state_kind(store, run_id) == "blocked"


def test_published_grandchild_rejects_missing_parent_and_replays_when_intact(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    runner = CliRunner()
    first = runner.invoke(app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"])
    assert first.exit_code == 0, first.output
    successor = str(json.loads(first.output)["successor_run_id"])
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    _run_until(tick, successor, target_kind="blocked", max_ticks=20)
    parent_path = artifacts.run_root(successor) / RECORD_REL
    original = parent_path.read_bytes()
    parent_path.unlink()
    missing_parent = runner.invoke(
        app, ["scheduler", "cursor-retry", successor, "--force", "--output", "json"]
    )
    assert missing_parent.exit_code == 1, missing_parent.output
    with store.begin_read() as conn:
        assert store.list_cursor_initial_recoveries_for_source(conn, source_run_id=successor) == []
    _replace_bytes(parent_path, original)
    second = runner.invoke(
        app, ["scheduler", "cursor-retry", successor, "--force", "--output", "json"]
    )
    assert second.exit_code == 0, second.output
    grandchild = str(json.loads(second.output)["successor_run_id"])
    intact = runner.invoke(
        app, ["scheduler", "cursor-retry", successor, "--force", "--output", "json"]
    )
    assert intact.exit_code == 0, intact.output
    assert json.loads(intact.output)["successor_run_id"] == grandchild
    assert json.loads(intact.output)["idempotent_replay"] is True
    _replace_bytes(parent_path, original + b" ")
    tampered = runner.invoke(app, ["scheduler", "cursor-retry", successor, "--force"])
    assert tampered.exit_code == 1, tampered.output
    parent_path.unlink()
    missing = runner.invoke(app, ["scheduler", "cursor-retry", successor, "--force"])
    assert missing.exit_code == 1, missing.output
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=successor)
    assert len(rows) == 1
    assert str(rows[0]["successor_run_id"]) == grandchild
    assert _effect_kinds(store, grandchild).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="failed authentication"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, grandchild)
    _replace_bytes(parent_path, original)
    replay = runner.invoke(
        app, ["scheduler", "cursor-retry", successor, "--force", "--output", "json"]
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["successor_run_id"] == grandchild
    assert json.loads(replay.output)["idempotent_replay"] is True


def test_literal_note_preserves_sentinels_and_separate_publishers(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    note = (
        "Recovery note: A previous attempt of this turn was interrupted and may have left "
        "partial work in this repository. Inspect the existing changes and continue according "
        "to the original plan and instructions. Preserve the work already present; determine "
        "what is complete and what still needs implementation or verification. Do not reset or "
        "discard existing work merely to start over."
    )
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    base = artifacts.read_verified_bytes(
        run_id,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    expected = base + b"\n\n" + note.encode("utf-8") + b"\n"
    staged = git_repo / "staged-sentinel.txt"
    staged.write_text("staged-sentinel\n", encoding="utf-8")
    subprocess.run(["git", "add", "staged-sentinel.txt"], cwd=git_repo, check=True)
    tracked = git_repo / "ai_dev_loop.yaml"
    tracked_before = tracked.read_bytes()
    tracked.write_bytes(tracked_before + b"# unstaged-sentinel\n")
    untracked = git_repo / "untracked-sentinel.txt"
    untracked.write_text("untracked-sentinel\n", encoding="utf-8")
    before = subprocess.check_output(["git", "ls-files", "--stage"], cwd=git_repo)
    before_status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-uall"], cwd=git_repo
    )
    published = CliRunner().invoke(
        app, ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"]
    )
    assert published.exit_code == 0, published.output
    successor = str(json.loads(published.output)["successor_run_id"])
    assert subprocess.check_output(["git", "ls-files", "--stage"], cwd=git_repo) == before
    assert (
        subprocess.check_output(["git", "status", "--porcelain=v1", "-uall"], cwd=git_repo)
        == before_status
    )
    assert staged.read_text(encoding="utf-8") == "staged-sentinel\n"
    assert tracked.read_bytes() == tracked_before + b"# unstaged-sentinel\n"
    assert untracked.read_text(encoding="utf-8") == "untracked-sentinel\n"
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "success")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "none")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    _run_until(tick, successor, target_kind="completed", max_ticks=40)
    assert expected in _prompt_args(log_path)


def test_separate_publishers_and_ready_replay_converge(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    barrier = threading.Barrier(2)
    first = CursorInitialRecoveryService(store, artifacts, before_create=barrier.wait)
    second = CursorInitialRecoveryService(store, artifacts, before_create=barrier.wait)
    errors: list[BaseException] = []
    results: list[str] = []

    def invoke(service: CursorInitialRecoveryService) -> None:
        try:
            results.append(service.force(run_id).successor_run_id)
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=invoke, args=(first,)),
        threading.Thread(target=invoke, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(results)) == 1
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=run_id)
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        active = store.get_active_reservation(conn, source.context.repository.worktree_key)
    assert len(rows) == 1
    assert str(rows[0]["status"]) == "ready"
    assert active is not None
    assert str(active["run_id"]) == results[0]
    assert _effect_kinds(store, results[0]).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_interruption_after_ready_publication_replays_one_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    publisher = CursorInitialRecoveryService(store, artifacts, fault_point="after_ready")
    with pytest.raises(CursorInitialRecoveryFault):
        publisher.force(run_id)
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=run_id)
    assert len(rows) == 1
    assert str(rows[0]["status"]) == "ready"
    successor = str(rows[0]["successor_run_id"])
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    replay = CursorInitialRecoveryService(store, artifacts).force(run_id)
    assert replay.successor_run_id == successor
    assert replay.idempotent_replay is True
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1


@pytest.mark.parametrize("change", ["abort", "missing_result"])
def test_candidate_insertion_reauthenticates_before_any_mutation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    run_id = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        reservation_before = [
            (str(row["worktree_key"]), str(row["run_id"]), str(row["status"]))
            for row in conn.execute(
                """
                SELECT worktree_key, run_id, status
                FROM scheduler_repository_reservations
                ORDER BY worktree_key
                """
            ).fetchall()
        ]
        runs_before = int(conn.execute("SELECT COUNT(*) AS n FROM scheduler_runs").fetchone()["n"])

    class InvalidateBeforeInsert(CursorInitialRecoveryService):
        def _insert_candidate(self, *args: object, **kwargs: object) -> sqlite3.Row:
            evidence = args[1]
            if change == "abort":
                with self.store.begin_immediate() as conn:
                    self.store.append_event(
                        conn,
                        event_id=f"evt-{uuid.uuid4()}",
                        run_id=run_id,
                        sequence=self.store.next_event_sequence(conn, run_id),
                        event=AbortRequestedEvent(run_id=run_id, reason="user_requested_abort"),
                        now=NOW,
                    )
            else:
                with self.store.begin_read() as conn:
                    attempt = self.store.get_attempt_by_id(conn, evidence.failed_attempt_id)
                assert attempt is not None
                (artifacts.run_root(run_id) / str(attempt["result_artifact_path"])).unlink()
            return super()._insert_candidate(*args, **kwargs)

    with pytest.raises(SchedulerEngineError):
        InvalidateBeforeInsert(store, artifacts).force(run_id)
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=run_id)
        run_count = conn.execute("SELECT COUNT(*) AS n FROM scheduler_runs").fetchone()
        assert rows == []
        reservation_after = [
            (str(row["worktree_key"]), str(row["run_id"]), str(row["status"]))
            for row in conn.execute(
                """
                SELECT worktree_key, run_id, status
                FROM scheduler_repository_reservations
                ORDER BY worktree_key
                """
            ).fetchall()
        ]
        assert reservation_after == reservation_before
        assert reservation_before
        assert all(owner == run_id for _key, owner, _status in reservation_before)
        assert int(run_count["n"]) == runs_before
        assert _state_kind(store, run_id) == "blocked"


def test_parent_must_be_the_immediate_source_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = CursorInitialRecoveryService(store, artifacts)
    first = service.force(source).successor_run_id
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    _run_until(tick, first, target_kind="blocked", max_ticks=30)
    second = service.force(first).successor_run_id
    _run_until(tick, second, target_kind="blocked", max_ticks=30)
    third = service.force(second).successor_run_id
    intact = service.force(second)
    assert intact.successor_run_id == third
    assert intact.idempotent_replay is True
    with store.begin_read() as conn:
        authenticated_initial_recovery_launch(store, artifacts, conn, third)
        earlier = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=first)
        current = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=third)
    assert earlier is not None
    assert current is not None
    original_record = (artifacts.run_root(third) / RECORD_REL).read_bytes()
    original_intent = str(current["intent_payload"]).encode("utf-8")
    original_parent_key = str(current["parent_recovery_key"])
    immediate_record = (artifacts.run_root(second) / RECORD_REL).read_bytes()
    _rewrite_ready_record(
        store,
        artifacts,
        third,
        record_updates={
            "parent_recovery_key": str(earlier["recovery_key"]),
            "parent_source_run_id": source,
            "parent_record_sha256": str(earlier["record_sha256"]),
        },
        intent_updates={"parent_recovery_key": str(earlier["recovery_key"])},
    )
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET parent_recovery_key = ?
            WHERE successor_run_id = ?
            """,
            (str(earlier["recovery_key"]), third),
        )
    (artifacts.run_root(second) / RECORD_REL).unlink()
    with pytest.raises(SchedulerEngineError, match="immediate source successor"):
        service.force(second)
    with (
        store.begin_read() as conn,
        pytest.raises(SchedulerEngineError, match="immediate source successor"),
    ):
        authenticated_initial_recovery_launch(store, artifacts, conn, third)
    _replace_bytes(artifacts.run_root(third) / RECORD_REL, original_record)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET parent_recovery_key = ?, record_sha256 = ?, intent_payload = ?,
                intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (
                original_parent_key,
                hashlib.sha256(original_record).hexdigest(),
                original_intent.decode("utf-8"),
                hashlib.sha256(original_intent).hexdigest(),
                third,
            ),
        )
    with pytest.raises(SchedulerEngineError):
        service.force(second)
    with store.begin_read() as conn, pytest.raises(SchedulerEngineError):
        authenticated_initial_recovery_launch(store, artifacts, conn, third)
    _replace_bytes(artifacts.run_root(second) / RECORD_REL, immediate_record)
    restored = service.force(second)
    assert restored.successor_run_id == third
    assert restored.idempotent_replay is True
    with store.begin_read() as conn:
        authenticated_initial_recovery_launch(store, artifacts, conn, third)


def test_corrupt_ready_intent_isolates_tick_until_restored(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = CursorInitialRecoveryService(store, artifacts).force(source).successor_run_id
    with store.begin_read() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
    assert row is not None
    original_intent = str(row["intent_payload"])
    original_digest = str(row["intent_payload_sha256"])
    payload = json.loads(original_intent)
    payload["iteration"] = "1"
    altered = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (altered.decode("utf-8"), hashlib.sha256(altered).hexdigest(), successor),
        )
    other_repo = git_repo.parent / "intent-unrelated-repo"
    shutil.copytree(git_repo, other_repo)
    other_id = _submit(other_repo, scheduler_paths)
    start_run(other_id, db_path=scheduler_paths["db_path"])
    with store.begin_read() as conn:
        before, _, _ = store.load_validated_snapshot(conn, other_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
    assert any(item.run_id == other_id for item in receipt.run_receipts)
    with store.begin_read() as conn:
        after, _, _ = store.load_validated_snapshot(conn, other_id)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert after.kind != before.kind
    assert attempts == []
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (original_intent, original_digest, successor),
        )
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "success")
    _run_until(tick, successor, target_kind="completed", max_ticks=40)
    with store.begin_read() as conn:
        successor_state, _, _ = store.load_validated_snapshot(conn, successor)
        invalid = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert successor_state.kind == "completed"
    assert invalid


def test_abort_wins_ready_publication_race_without_installing_an_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, successor, store, artifacts = _pending_candidate(
        git_repo, scheduler_paths, fake_clis, monkeypatch
    )
    entered = threading.Event()
    release = threading.Event()

    def pause_before_ready() -> None:
        entered.set()
        assert release.wait(timeout=30)

    errors: list[BaseException] = []

    def publish() -> None:
        try:
            CursorInitialRecoveryService(
                store,
                artifacts,
                before_ready_commit=pause_before_ready,
            ).force(run_id)
        except SchedulerEngineError as exc:
            errors.append(exc)

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert entered.wait(timeout=30)
    with store.begin_read() as conn:
        pending = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        active = store.get_active_reservation(conn, source.context.repository.worktree_key)
    assert pending is not None
    assert str(pending["status"]) == "pending"
    assert active is not None
    assert str(active["run_id"]) == successor
    scheduler_abort_run(
        successor,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    release.set()
    publisher.join(timeout=30)
    assert not publisher.is_alive()
    assert errors
    assert _effect_kinds(store, successor) == []
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 10, 6, tzinfo=UTC),
        backend=_backend(),
    )
    tick.run_once()
    with store.begin_read() as conn:
        cancelled = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert cancelled is not None
    assert str(cancelled["status"]) == "cancelled"
    assert attempts == []
    assert _state_kind(store, run_id) == "blocked"


def test_abort_before_effect_claim_prevents_late_launch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

    from ai_dev_loop.scheduler.application.tick import TickService

    source = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = CursorInitialRecoveryService(store, artifacts).force(source).successor_run_id
    assert _effect_kinds(store, successor).count(RUN_CURSOR_TURN_EFFECT_KIND) == 1
    entered = threading.Event()
    release = threading.Event()

    def pause_before_claim() -> None:
        entered.set()
        assert release.wait(timeout=30)

    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 10, 6, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-23-2-claim-{uuid.uuid4()}",
        attempt_backend=_backend(),
        preflight_port=OkPreflightPort(),
        before_effect_claim=pause_before_claim,
    )
    launcher = threading.Thread(target=tick.run_once)
    launcher.start()
    assert entered.wait(timeout=30)
    scheduler_abort_run(
        successor,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    release.set()
    launcher.join(timeout=30)
    assert not launcher.is_alive()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, successor)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
        effect = conn.execute(
            """
            SELECT status FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ?
            """,
            (successor, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
    assert state.kind == "aborted"
    assert attempts == []
    assert effect is not None
    assert str(effect["status"]) == "cancelled"
    tick.run_once()
    with store.begin_read() as conn:
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert attempts == []
