"""Phase 23.3 production-path correction recovery."""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.integration.test_phase17_4_cursor_workflow import _submit
from tests.integration.test_phase23_1_cursor_recovery_check import _blocked_run
from tests.integration.test_phase23_2_initial_recovery import (
    _drive_until,
    _rewrite_ready_record,
)
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase23_1_cursor_recovery_evidence import (
    NOW,
    _blocked_correction_failure,
    _normalize_run_artifact_permissions,
    _run_until,
    _tick_service,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
    CursorInitialRecoveryFault,
    CursorInitialRecoveryService,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    analyze_cursor_recovery_evidence,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_budget import effective_review_ceiling_for_run
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import RUN_CURSOR_TURN_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    EFFECTIVE_PROMPT_REL,
    RECOVERY_NOTE,
    effective_prompt_bytes,
)
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    CORRECTION_BUDGET_CARRY_REL,
    CORRECTION_RECORD_REL,
    BoundReviewerEvidenceV2,
    CursorRecoveryRecordV2,
)
from ai_dev_loop.scheduler.domain.state import BlockedState, MaxIterationsReachedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

AFTER_FORCE = datetime(2027, 1, 1, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


def _force(run_id: str) -> dict[str, object]:
    result = CliRunner().invoke(
        app,
        ["scheduler", "cursor-retry", run_id, "--force", "--output", "json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, dict)
    return payload


def _record(artifacts: ProtectedArtifactStore, successor: str) -> CursorRecoveryRecordV2:
    payload = (artifacts.run_root(successor) / CORRECTION_RECORD_REL).read_bytes()
    return CursorRecoveryRecordV2.model_validate_json(payload)


def _resume_session_ids(log_path: Path) -> list[str]:
    if not log_path.is_file():
        return []
    found: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("ARGS:"):
            continue
        args = [str(item) for item in ast.literal_eval(line.removeprefix("ARGS:"))]
        if "resume" in args:
            found.extend(item for item in args if item == BOOTSTRAP_ID)
    return found


def test_failed_correction_force_resumes_exact_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    assert analysis.receipt.recovery_supported is True
    source_root = artifacts.run_root(run_id)
    before = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    published = _force(run_id)
    assert published["turn_kind"] == "correction"
    successor = str(published["successor_run_id"])
    record = _record(artifacts, successor)
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    assert record.reviewer.session_id == BOOTSTRAP_ID
    assert record.chat_id == analysis.evidence.chat_id
    assert record.iteration == analysis.evidence.iteration
    assert record.reviews_completed == 1
    base = artifacts.read_verified_bytes(
        record.base_prompt.owner_run_id,
        record.base_prompt.relative_path,
        expected_sha256=record.base_prompt.sha256,
    )
    effective = (artifacts.run_root(successor) / EFFECTIVE_PROMPT_REL).read_bytes()
    assert effective == effective_prompt_bytes(base)
    assert (artifacts.run_root(successor) / record.raw_fix.relative_path).read_bytes() == (
        artifacts.run_root(record.raw_fix.owner_run_id) / record.raw_fix.relative_path
    ).read_bytes()
    after = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    assert after == before
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        successor_state, _, _ = store.load_validated_snapshot(conn, successor)
    assert isinstance(source, BlockedState)
    assert successor_state.context.workflow.max_review_iterations == (
        source.context.workflow.max_review_iterations
    )
    assert successor_state.codex.reviewer_session_id == BOOTSTRAP_ID

    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["codex_log"])
    log_path.write_bytes(b"")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="completed", max_ticks=30)
    with store.begin_read() as conn:
        finished, _, _ = store.load_validated_snapshot(conn, successor)
    assert finished.codex.reviewer_session_id == BOOTSTRAP_ID
    assert finished.codex.reviews_completed == 2
    assert BOOTSTRAP_ID in _resume_session_ids(log_path)


def test_second_correction_failure_keeps_one_note(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    first = str(_force(run_id)["successor_run_id"])
    first_record = _record(artifacts, first)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, first, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], first)
    second = str(_force(first)["successor_run_id"])
    second_record = _record(artifacts, second)
    note = RECOVERY_NOTE.encode("utf-8")
    effective = (artifacts.run_root(second) / EFFECTIVE_PROMPT_REL).read_bytes()
    assert effective.count(note) == 1
    assert second_record.base_prompt == first_record.base_prompt
    assert effective == effective_prompt_bytes(
        artifacts.read_verified_bytes(
            second_record.base_prompt.owner_run_id,
            second_record.base_prompt.relative_path,
            expected_sha256=second_record.base_prompt.sha256,
        )
    )
    assert second_record.reviews_completed == first_record.reviews_completed


def test_extended_budget_survives_correction_recovery(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", ",".join(["findings"] * 8))
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=7)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    extender = ReviewBudgetExtendService(store, artifacts, now_factory=lambda: NOW)
    extended = False
    failed = False
    for _ in range(300):
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if failed and state.kind == "blocked":
            break
        if isinstance(state, MaxIterationsReachedState) and not extended:
            extender.extend(run_id, target_total=9)
            extended = True
            continue
        if (
            extended
            and state.kind == "waiting_for_cursor_fix"
            and state.codex.reviews_completed == 8
            and not failed
        ):
            monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
            failed = True
        tick.run_once()
    else:
        completed = getattr(getattr(state, "codex", None), "reviews_completed", None)
        raise AssertionError(
            f"budget setup did not block; last={state.kind} completed={completed} "
            f"extended={extended} failed={failed}"
        )
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.evidence is not None
    assert analysis.evidence.reviews_completed == 8
    assert analysis.evidence.effective_review_ceiling == 9
    assert analysis.evidence.submitted_max_review_iterations == 7
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert record.submitted_max_review_iterations == 7
    assert record.effective_review_ceiling == 9
    assert record.reviews_completed == 8
    with store.begin_read() as conn:
        successor_state, _, _ = store.load_validated_snapshot(conn, successor)
        ceiling = effective_review_ceiling_for_run(
            store,
            conn,
            successor_state,
            artifacts=artifacts,
        )
    assert successor_state.context.workflow.max_review_iterations == 7
    assert successor_state.codex.reviews_completed == 8
    assert successor_state.codex.reviewer_session_id == BOOTSTRAP_ID
    assert ceiling == 9

    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    later = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(later, successor, target_kind="max_iterations_reached", max_ticks=40)
    with store.begin_read() as conn:
        exhausted, _, _ = store.load_validated_snapshot(conn, successor)
    assert exhausted.codex.reviews_completed == 9
    later_extender = ReviewBudgetExtendService(
        store,
        artifacts,
        now_factory=lambda: AFTER_FORCE,
    )
    extended_again = later_extender.extend(successor, target_total=10)
    assert extended_again.previous_effective_total == 9
    assert extended_again.new_effective_total == 10
    with store.begin_read() as conn:
        after, _, _ = store.load_validated_snapshot(conn, successor)
        ceiling_after = effective_review_ceiling_for_run(
            store,
            conn,
            after,
            artifacts=artifacts,
        )
    assert after.context.workflow.max_review_iterations == 7
    assert ceiling_after == 10


def _effect_count(store: SqliteSchedulerStore, run_id: str, kind: str) -> int:
    with store.begin_read() as conn:
        rows = conn.execute(
            "SELECT effect_kind FROM scheduler_effects WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    return sum(1 for row in rows if str(row[0]) == kind)


def test_review_retry_after_correction_recovery_keeps_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    successor = str(_force(run_id)["successor_run_id"])
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="waiting_codex_review_retry", max_ticks=40)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    authorized = ReviewRetryService(store, artifacts).retry(successor)
    assert authorized.changed is True
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor, target_kind="completed", max_ticks=20)
    with store.begin_read() as conn:
        finished, _, _ = store.load_validated_snapshot(conn, successor)
    assert finished.codex.reviewer_session_id == BOOTSTRAP_ID
    assert finished.codex.reviews_completed == 2


def test_correction_publication_crash_replays_one_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    crashing = CursorInitialRecoveryService(store, artifacts, fault_point="after_candidate")
    with pytest.raises(CursorInitialRecoveryFault):
        crashing.force(run_id)
    service = CursorInitialRecoveryService(store, artifacts)
    published = service.force(run_id)
    replay = service.force(run_id)
    assert published.publication_status == "ready"
    assert replay.idempotent_replay is True
    assert replay.successor_run_id == published.successor_run_id
    assert _effect_count(store, published.successor_run_id, RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_tampered_budget_carry_does_not_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    successor = str(_force(run_id)["successor_run_id"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    carry = artifacts.run_root(successor) / CORRECTION_BUDGET_CARRY_REL
    original = carry.read_bytes()
    carry.write_bytes(original[:-1] + (b"0" if original[-1:] != b"0" else b"1"))
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, successor)
        attempts = store.get_nonterminal_attempt_for_run(conn, successor)
    assert state.kind == "cursor_ready"
    assert attempts is None


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


def test_new_correction_after_recovery_uses_the_new_envelope(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    first = str(_force(run_id)["successor_run_id"])
    first_record = _record(artifacts, first)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, first, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, first, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], first)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, first)
    assert analysis.evidence is not None
    failed_envelope = artifacts.read_verified_bytes(
        first,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    assert failed_envelope != artifacts.read_verified_bytes(
        first_record.base_prompt.owner_run_id,
        first_record.base_prompt.relative_path,
        expected_sha256=first_record.base_prompt.sha256,
    )
    second = str(_force(first)["successor_run_id"])
    second_record = _record(artifacts, second)
    assert isinstance(second_record.reviewer, BoundReviewerEvidenceV2)
    assert second_record.reviewer.session_id == BOOTSTRAP_ID
    assert second_record.base_prompt.sha256 != first_record.base_prompt.sha256
    effective = (artifacts.run_root(second) / EFFECTIVE_PROMPT_REL).read_bytes()
    assert effective == effective_prompt_bytes(failed_envelope)
    assert effective.count(RECOVERY_NOTE.encode("utf-8")) == 1
    assert second_record.reviews_completed == first_record.reviews_completed + 1
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        finished, _, _ = store.load_validated_snapshot(conn, second)
    assert finished.context.workflow.max_review_iterations == (
        source.context.workflow.max_review_iterations
    )
    assert finished.codex.reviewer_session_id == BOOTSTRAP_ID
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    _run_until(tick, second, target_kind="completed", max_ticks=40)
    prompts = _prompt_args(log_path)
    assert prompts.count(effective) == 1
    with store.begin_read() as conn:
        accepted, _, _ = store.load_validated_snapshot(conn, second)
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.codex.reviews_completed == second_record.reviews_completed + 1
    assert accepted.context.workflow.max_review_iterations == (
        source.context.workflow.max_review_iterations
    )
    assert accepted.cursor.chat_id == second_record.chat_id
    assert accepted.context.cursor.model == source.context.cursor.model


def test_correction_replay_rejects_missing_or_unrelated_reviewer_proof(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    events = (
        artifacts.run_root(record.reviewer.bootstrap_run_id)
        / record.reviewer.bootstrap_events_path
    )
    original_events = events.read_bytes()
    events.write_bytes(b"")
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="bootstrap proof"):
        service.force(run_id)
    events.write_bytes(original_events)
    mismatched_session = "019def00-1111-4111-8111-111111111111"
    mismatched = record.model_copy(
        update={
            "reviewer": record.reviewer.model_copy(update={"session_id": mismatched_session})
        }
    )
    _publish_rewritten_record(
        store,
        artifacts,
        successor,
        mismatched,
        intent_updates={"reviewer_session_id": mismatched_session},
    )
    with pytest.raises(SchedulerEngineError, match="disagrees with the failed source"):
        service.force(run_id)
    unrelated_owner = successor + "-other"
    unrelated = record.model_copy(
        update={
            "reviewer": record.reviewer.model_copy(update={"binding_run_id": unrelated_owner})
        }
    )
    _publish_rewritten_record(
        store,
        artifacts,
        successor,
        unrelated,
        intent_updates={
            "binding_run_id": unrelated_owner,
            "reviewer_session_id": record.reviewer.session_id,
        },
    )
    with pytest.raises(SchedulerEngineError, match="does not authenticate the artifact"):
        service.force(run_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert attempts == []


def _publish_rewritten_record(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    successor: str,
    record: CursorRecoveryRecordV2,
    *,
    intent_updates: dict[str, object],
) -> None:
    import hashlib

    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
        CursorRecoveryPublicationIntentV2,
    )

    payload = record.canonical_bytes()
    path = artifacts.run_root(successor) / CORRECTION_RECORD_REL
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    with store.begin_immediate() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
        assert row is not None
        intent = CursorRecoveryPublicationIntentV2.model_validate_json(str(row["intent_payload"]))
        updated = intent.model_copy(update={**intent_updates, "record_sha256": digest})
        intent_bytes = updated.canonical_bytes()
        conn.execute(
            """
            UPDATE scheduler_cursor_initial_recoveries
            SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
            WHERE successor_run_id = ?
            """,
            (
                digest,
                intent_bytes.decode("utf-8"),
                hashlib.sha256(intent_bytes).hexdigest(),
                successor,
            ),
        )


def test_usage_limit_continuation_keeps_the_recovered_correction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase23_2_initial_recovery import _drive_until

    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert record.raw_fix is not None
    effective = (artifacts.run_root(successor) / EFFECTIVE_PROMPT_REL).read_bytes()
    raw_fix = (
        artifacts.run_root(record.raw_fix.owner_run_id) / record.raw_fix.relative_path
    ).read_bytes()
    assert raw_fix in effective
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,success")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    state, continuation, _waiting = _drive_until(
        git_repo,
        scheduler_paths,
        successor,
        target_kind="completed",
    )
    assert continuation is not None
    assert continuation != effective
    assert effective in continuation
    assert raw_fix in continuation
    prompts = _prompt_args(log_path)
    assert effective in prompts
    assert continuation in prompts
    assert state.codex.reviewer_session_id == BOOTSTRAP_ID
    assert state.codex.reviews_completed == record.reviews_completed + 1
    assert state.context.workflow.max_review_iterations == record.submitted_max_review_iterations
    assert (
        artifacts.run_root(record.raw_fix.owner_run_id) / record.raw_fix.relative_path
    ).read_bytes() == raw_fix
    with store.begin_read() as conn:
        ceiling = effective_review_ceiling_for_run(store, conn, state, artifacts=artifacts)
    assert ceiling == record.effective_review_ceiling


def test_review_recovery_after_cursor_successor_preserves_budget(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
        _legacy_block_instead_of_retry,
    )

    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=2)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    cursor_successor = str(_force(run_id)["successor_run_id"])
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
    fresh_path = source.context.codex.binding_artifact_path
    assert (artifacts.run_root(cursor_successor) / fresh_path).read_bytes() == (
        artifacts.run_root(run_id) / fresh_path
    ).read_bytes()
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    review_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _legacy_block_instead_of_retry(review_tick, monkeypatch)
    _run_until(review_tick, cursor_successor, target_kind="blocked", max_ticks=40)
    recovered = ReviewRetryService(store, artifacts).retry(cursor_successor)
    review_successor = recovered.run_id
    assert review_successor != cursor_successor
    assert (artifacts.run_root(review_successor) / fresh_path).is_file()
    with store.begin_read() as conn:
        review_state, _, _ = store.load_validated_snapshot(conn, review_successor)
    assert review_state.codex.reviews_completed == 1
    assert review_state.codex.reviewer_session_id == BOOTSTRAP_ID
    assert review_state.context.workflow.max_review_iterations == 2
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    exhaust = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(exhaust, review_successor, target_kind="max_iterations_reached", max_ticks=40)
    with store.begin_read() as conn:
        exhausted, _, _ = store.load_validated_snapshot(conn, review_successor)
    assert exhausted.codex.reviews_completed == 2
    assert exhausted.context.workflow.max_review_iterations == 2
    extended = ReviewBudgetExtendService(
        store,
        artifacts,
        now_factory=lambda: AFTER_FORCE,
    ).extend(review_successor, target_total=3)
    assert extended.previous_effective_total == 2
    assert extended.new_effective_total == 3
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(exhaust, review_successor, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], review_successor)
    service = CursorInitialRecoveryService(store, artifacts)
    published = service.force(review_successor)
    replay = service.force(review_successor)
    assert replay.idempotent_replay is True
    assert replay.successor_run_id == published.successor_run_id
    later = _record(artifacts, published.successor_run_id)
    assert later.submitted_max_review_iterations == 2
    assert later.reviews_completed == 2
    assert later.effective_review_ceiling == 3
    assert any(
        edge.relation == "review_recovery" and ":" in edge.recovery_key
        for edge in later.ancestors
    )
    assert _effect_count(store, published.successor_run_id, RUN_CURSOR_TURN_EFFECT_KIND) == 1
    with store.begin_read() as conn:
        ready_state, _, _ = store.load_validated_snapshot(conn, published.successor_run_id)
        ready_reservation = store.get_active_reservation(
            conn,
            ready_state.context.repository.worktree_key,
        )
        assert store.has_checkpoint_reconciliation_hold(conn, published.successor_run_id) is False
        assert store.has_unresolved_abort_hold(conn, published.successor_run_id) is False
    assert ready_reservation is not None
    assert str(ready_reservation["run_id"]) == published.successor_run_id
    source_root = artifacts.run_root(review_successor)
    source_before = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _run_until(exhaust, published.successor_run_id, target_kind="completed", max_ticks=40)
    with store.begin_read() as conn:
        accepted, _, _ = store.load_validated_snapshot(conn, published.successor_run_id)
        reservation = store.get_active_reservation(
            conn,
            accepted.context.repository.worktree_key,
        )
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.codex.reviews_completed == 3
    assert accepted.context.workflow.max_review_iterations == 2
    assert accepted.cursor.chat_id == later.chat_id
    assert accepted.cursor.iteration == later.iteration
    assert reservation is None
    assert {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    } == source_before
    assert _effect_count(store, published.successor_run_id, RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_correction_publishers_abort_and_restart_dispatch_one_envelope(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    barrier = threading.Barrier(2)
    service = CursorInitialRecoveryService(store, artifacts, before_create=barrier.wait)
    results: list[str] = []
    errors: list[BaseException] = []

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
    assert _effect_count(store, results[0], RUN_CURSOR_TURN_EFFECT_KIND) == 1


def test_pending_correction_abort_cancels_without_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    pending_service = CursorInitialRecoveryService(
        store,
        artifacts,
        fault_point="after_candidate",
    )
    with pytest.raises(CursorInitialRecoveryFault):
        pending_service.force(other)
    with store.begin_read() as conn:
        pending_rows = [
            row
            for row in store.list_pending_cursor_initial_recoveries(conn)
            if str(row["source_run_id"]) == other
        ]
        source, _, _ = store.load_validated_snapshot(conn, other)
        reservation = store.get_active_reservation(
            conn,
            source.context.repository.worktree_key,
        )
    assert len(pending_rows) == 1
    pending_successor = str(pending_rows[0]["successor_run_id"])
    assert reservation is not None
    assert str(reservation["run_id"]) == pending_successor
    scheduler_abort_run(
        pending_successor,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    abort_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    abort_tick.run_once()
    with store.begin_read() as conn:
        cancelled = store.get_cursor_initial_recovery_by_successor(
            conn,
            successor_run_id=pending_successor,
        )
    assert cancelled is not None
    assert str(cancelled["status"]) == "cancelled"
    assert _effect_count(store, pending_successor, RUN_CURSOR_TURN_EFFECT_KIND) == 0


def test_correction_restart_dispatches_the_failed_envelope_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    restart_source = _blocked_correction_failure(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    restart_store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    analysis = analyze_cursor_recovery_evidence(restart_store, artifacts, restart_source)
    assert analysis.evidence is not None
    assert analysis.evidence.fix_prompt_path is not None
    assert analysis.evidence.fix_owner_run_id is not None
    failed_envelope = artifacts.read_verified_bytes(
        restart_source,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    raw_fix = artifacts.read_verified_bytes(
        analysis.evidence.fix_owner_run_id,
        analysis.evidence.fix_prompt_path,
        expected_sha256=analysis.evidence.fix_prompt_sha256 or "",
    )
    with pytest.raises(CursorInitialRecoveryFault):
        CursorInitialRecoveryService(
            restart_store,
            artifacts,
            fault_point="after_candidate",
        ).force(restart_source)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    restart = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    with restart_store.begin_read() as conn:
        restarted = [
            row
            for row in restart_store.list_pending_cursor_initial_recoveries(conn)
            if str(row["source_run_id"]) == restart_source
        ]
    assert len(restarted) == 1
    restarted_successor = str(restarted[0]["successor_run_id"])
    _run_until(restart, restarted_successor, target_kind="completed", max_ticks=40)
    prompts = _prompt_args(log_path)
    expected = effective_prompt_bytes(failed_envelope)
    assert expected in prompts
    assert prompts.count(expected) == 1
    assert raw_fix in expected
    published = _record(artifacts, restarted_successor)
    assert published.raw_fix is not None
    assert (
        artifacts.run_root(published.raw_fix.owner_run_id) / published.raw_fix.relative_path
    ).read_bytes() == raw_fix
    assert _effect_count(restart_store, restarted_successor, RUN_CURSOR_TURN_EFFECT_KIND) == 1


OTHER_REVIEWER = "019def11-2222-4222-8222-222222222222"


def test_correction_replay_rejects_valid_reviewer_from_unrelated_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
        _authenticate_reviewer_b_binding,
    )
    from ai_dev_loop.scheduler.domain.events import CodexReviewerBoundEvent

    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    other_repo = git_repo.parent / "unrelated-reviewer-repo"
    shutil.copytree(git_repo, other_repo)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", OTHER_REVIEWER)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    other_run = _submit(other_repo, scheduler_paths)
    start_run(other_run, db_path=scheduler_paths["db_path"])
    other_tick = _tick_service(
        other_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(other_tick, other_run, target_kind="completed", max_ticks=40)
    with store.begin_read() as conn:
        other_state, _, _ = store.load_validated_snapshot(conn, other_run)
        bound_row = None
        for row in store.list_events_for_run(conn, other_run, limit=100, newest_first=False):
            if str(row["event_kind"]) == "codex_reviewer_bound":
                bound_row = row
                break
    assert bound_row is not None
    assert other_state.codex.reviewer_session_id == OTHER_REVIEWER
    assert other_state.codex.binding_artifact_path
    assert other_state.codex.binding_artifact_sha256
    proof = _authenticate_reviewer_b_binding(
        store,
        artifacts,
        b_owner_run_id=other_run,
        reviewer_bound=CodexReviewerBoundEvent(
            run_id=other_run,
            reviewer_session_id_prefix=OTHER_REVIEWER[:8],
            binding_artifact_path=other_state.codex.binding_artifact_path,
            binding_artifact_sha256=other_state.codex.binding_artifact_sha256,
        ),
    )
    assert proof.session_id == OTHER_REVIEWER
    assert proof.session_id != BOOTSTRAP_ID
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    substituted = record.model_copy(
        update={
            "reviewer": record.reviewer.model_copy(
                update={
                    "session_id": proof.session_id,
                    "bootstrap_run_id": proof.bootstrap_run_id,
                    "bootstrap_attempt_id": proof.bootstrap_attempt_id,
                    "bootstrap_events_path": proof.bootstrap_events_path,
                    "bootstrap_events_sha256": proof.bootstrap_events_sha256,
                    "binding_run_id": proof.binding_run_id,
                    "binding_artifact_path": proof.binding_artifact_path,
                    "binding_artifact_sha256": proof.binding_artifact_sha256,
                }
            )
        }
    )
    _publish_rewritten_record(
        store,
        artifacts,
        successor,
        substituted,
        intent_updates={
            "reviewer_session_id": proof.session_id,
            "bootstrap_run_id": proof.bootstrap_run_id,
            "bootstrap_attempt_id": proof.bootstrap_attempt_id,
            "bootstrap_events_path": proof.bootstrap_events_path,
            "bootstrap_events_sha256": proof.bootstrap_events_sha256,
            "binding_run_id": proof.binding_run_id,
            "binding_artifact_path": proof.binding_artifact_path,
            "binding_artifact_sha256": proof.binding_artifact_sha256,
        },
    )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="disagrees with the failed source"):
        service.force(run_id)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = tick.run_once()
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert attempts == []


def test_usage_limit_failure_after_recovery_uses_the_terminal_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert record.raw_fix is not None
    effective = (artifacts.run_root(successor) / EFFECTIVE_PROMPT_REL).read_bytes()
    raw_fix = (
        artifacts.run_root(record.raw_fix.owner_run_id) / record.raw_fix.relative_path
    ).read_bytes()
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    waiting = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(waiting, successor, target_kind="waiting_usage_limit", max_ticks=20)
    failed = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(failed, successor, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], successor)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, successor)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.safe_summary
    assert analysis.receipt.turn_kind == "correction"
    assert analysis.evidence is not None
    failed_prompt = artifacts.read_verified_bytes(
        successor,
        analysis.evidence.prompt_path,
        expected_sha256=analysis.evidence.prompt_sha256,
    )
    assert effective in failed_prompt
    assert raw_fix in failed_prompt
    assert analysis.evidence.failed_attempt_id != record.failed_attempt_id
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    second = str(_force(successor)["successor_run_id"])
    second_record = _record(artifacts, second)
    assert isinstance(second_record.reviewer, BoundReviewerEvidenceV2)
    assert second_record.reviewer.session_id == BOOTSTRAP_ID
    assert second_record.reviews_completed == record.reviews_completed
    assert second_record.submitted_max_review_iterations == record.submitted_max_review_iterations
    assert second_record.effective_review_ceiling == record.effective_review_ceiling
    assert second_record.chat_id == record.chat_id
    assert second_record.iteration == record.iteration
    recovered = (artifacts.run_root(second) / EFFECTIVE_PROMPT_REL).read_bytes()
    parent_base = artifacts.read_verified_bytes(
        record.base_prompt.owner_run_id,
        record.base_prompt.relative_path,
        expected_sha256=record.base_prompt.sha256,
    )
    assert recovered == effective_prompt_bytes(parent_base)
    assert recovered.count(RECOVERY_NOTE.encode("utf-8")) == 1
    assert second_record.raw_fix is not None
    assert (
        artifacts.run_root(second_record.raw_fix.owner_run_id) / second_record.raw_fix.relative_path
    ).read_bytes() == raw_fix
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    complete = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(complete, second, target_kind="completed", max_ticks=40)
    prompts = _prompt_args(log_path)
    assert prompts.count(recovered) == 1
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        accepted, _, _ = store.load_validated_snapshot(conn, second)
        ceiling = effective_review_ceiling_for_run(store, conn, accepted, artifacts=artifacts)
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.codex.reviews_completed == record.reviews_completed + 1
    assert accepted.context.workflow.max_review_iterations == (
        source.context.workflow.max_review_iterations
    )
    assert accepted.context.cursor.model == source.context.cursor.model
    assert accepted.cursor.chat_id == record.chat_id
    assert ceiling == record.effective_review_ceiling


def test_independent_publishers_race_at_the_ready_boundary(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    source_root = artifacts.run_root(run_id)
    before = {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    }
    with pytest.raises(CursorInitialRecoveryFault):
        CursorInitialRecoveryService(
            store,
            artifacts,
            fault_point="after_artifacts",
        ).force(run_id)
    barrier = threading.Barrier(2)
    publishers = [
        CursorInitialRecoveryService(store, artifacts, before_ready_commit=barrier.wait)
        for _ in range(2)
    ]
    results: list[str] = []
    errors: list[BaseException] = []

    def invoke(service: CursorInitialRecoveryService) -> None:
        try:
            results.append(service.force(run_id).successor_run_id)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(service,)) for service in publishers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(set(results)) == 1
    successor = results[0]
    record = _record(artifacts, successor)
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    assert record.reviewer.session_id == BOOTSTRAP_ID
    assert record.reviews_completed == 1
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        reservation = store.get_active_reservation(
            conn,
            source.context.repository.worktree_key,
        )
    assert record.submitted_max_review_iterations == source.context.workflow.max_review_iterations
    assert reservation is not None
    assert str(reservation["run_id"]) == successor
    assert _effect_count(store, successor, RUN_CURSOR_TURN_EFFECT_KIND) == 1
    assert {
        path.relative_to(source_root).as_posix(): path.read_bytes()
        for path in source_root.rglob("*")
        if path.is_file()
    } == before


def test_abort_at_ready_boundary_cancels_without_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    started = threading.Event()
    release = threading.Event()

    def hold() -> None:
        started.set()
        release.wait()

    service = CursorInitialRecoveryService(store, artifacts, before_ready_commit=hold)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            service.force(run_id)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke)
    thread.start()
    assert started.wait(30)
    with store.begin_read() as conn:
        pending = [
            row
            for row in store.list_pending_cursor_initial_recoveries(conn)
            if str(row["source_run_id"]) == run_id
        ]
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        reservation = store.get_active_reservation(
            conn,
            source.context.repository.worktree_key,
        )
    assert len(pending) == 1
    successor = str(pending[0]["successor_run_id"])
    assert reservation is not None
    assert str(reservation["run_id"]) == successor
    scheduler_abort_run(
        successor,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    release.set()
    thread.join()
    assert errors
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    tick.run_once()
    with store.begin_read() as conn:
        cancelled = store.get_cursor_initial_recovery_by_successor(
            conn,
            successor_run_id=successor,
        )
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert cancelled is not None
    assert str(cancelled["status"]) == "cancelled"
    assert attempts == []
    assert _effect_count(store, successor, RUN_CURSOR_TURN_EFFECT_KIND) == 0


def test_replay_rejects_replacement_envelope_and_unrelated_owner(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    first = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, first)
    raw_fix = record.raw_fix
    assert raw_fix is not None
    replacement = b"replacement correction envelope\n"
    relative = "cursor/recovery/replacement-envelope.txt"
    digest = hashlib.sha256(replacement).hexdigest()
    for owner in (record.base_prompt.owner_run_id, first):
        artifacts.publish_or_verify_bytes(owner, relative, replacement, max_bytes=1_000_000)
    original_effective = artifacts.read_verified_bytes(
        first,
        record.effective_prompt_path,
        expected_sha256=record.effective_prompt_sha256,
    )
    effective = effective_prompt_bytes(replacement)
    (artifacts.run_root(first) / EFFECTIVE_PROMPT_REL).write_bytes(effective)
    replaced = record.model_copy(
        update={
            "base_prompt": record.base_prompt.model_copy(
                update={"relative_path": relative, "sha256": digest}
            ),
            "effective_prompt_sha256": hashlib.sha256(effective).hexdigest(),
        }
    )
    _publish_rewritten_record(
        store,
        artifacts,
        first,
        replaced,
        intent_updates={
            "base_prompt_path": relative,
            "base_prompt_sha256": digest,
        },
    )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="failed invocation"):
        service.force(run_id)
    _assert_dispatch_rejected(git_repo, scheduler_paths, first)

    unrelated = "019def99-9999-4999-8999-999999999999"
    raw = artifacts.read_verified_bytes(
        raw_fix.owner_run_id,
        raw_fix.relative_path,
        expected_sha256=raw_fix.sha256,
    )
    artifacts.publish_or_verify_bytes(
        unrelated,
        raw_fix.relative_path,
        raw,
        max_bytes=1_000_000,
    )
    (artifacts.run_root(first) / EFFECTIVE_PROMPT_REL).write_bytes(original_effective)
    copied = record.model_copy(
        update={"raw_fix": raw_fix.model_copy(update={"owner_run_id": unrelated})}
    )
    _publish_rewritten_record(
        store,
        artifacts,
        first,
        copied,
        intent_updates={
            "base_prompt_path": record.base_prompt.relative_path,
            "base_prompt_sha256": record.base_prompt.sha256,
        },
    )
    with pytest.raises(SchedulerEngineError, match="causal checkpoint"):
        service.force(run_id)
    _assert_dispatch_rejected(git_repo, scheduler_paths, first)


def test_ordinary_correction_usage_limit_recovers_the_same_reviewer(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=3)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    _run_until(tick, run_id, target_kind="waiting_usage_limit", max_ticks=20)
    later = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(later, run_id, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated"
    assert analysis.receipt.turn_kind == "correction"
    evidence = analysis.evidence
    assert evidence is not None
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert record.failed_attempt_id == evidence.failed_attempt_id
    assert record.reviewer.session_id == BOOTSTRAP_ID
    assert record.reviews_completed == 1
    assert record.submitted_max_review_iterations == 3
    assert record.effective_review_ceiling == 3
    assert record.iteration == evidence.iteration
    effective = artifacts.read_verified_bytes(
        successor,
        record.effective_prompt_path,
        expected_sha256=record.effective_prompt_sha256,
    )
    assert effective.count(RECOVERY_NOTE.encode("utf-8")) == 1
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    complete = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(complete, successor, target_kind="completed", max_ticks=30)
    with store.begin_read() as conn:
        accepted, _, _ = store.load_validated_snapshot(conn, successor)
        source, _, _ = store.load_validated_snapshot(conn, run_id)
    assert accepted.kind == "completed"
    assert accepted.codex is not None
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.cursor is not None
    assert accepted.cursor.chat_id == record.chat_id
    assert accepted.context.cursor.model == source.context.cursor.model
    assert accepted.cursor.iteration == record.iteration
    assert accepted.codex.reviews_completed == 2
    assert _prompt_args(log_path).count(effective) == 1


def _accept_usage_limited_correction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    historical_effective_sha: str,
) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    waiting = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(waiting, run_id, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    _run_until(waiting, run_id, target_kind="waiting_usage_limit", max_ticks=20)
    failed = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(failed, run_id, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated", analysis.receipt.safe_summary
    assert analysis.receipt.turn_kind == "correction"
    evidence = analysis.evidence
    assert evidence is not None
    assert evidence.fix_prompt_path is not None
    assert evidence.fix_prompt_sha256 is not None
    assert evidence.prompt_sha256 != historical_effective_sha
    envelope = artifacts.read_verified_bytes(
        run_id,
        evidence.prompt_path,
        expected_sha256=evidence.prompt_sha256,
    )
    raw_fix = artifacts.read_verified_bytes(
        evidence.fix_owner_run_id or run_id,
        evidence.fix_prompt_path,
        expected_sha256=evidence.fix_prompt_sha256,
    )
    assert raw_fix in envelope
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        inherited_ceiling = effective_review_ceiling_for_run(
            store,
            conn,
            source,
            artifacts=artifacts,
        )
        checkpoint = None
        for row in store.list_events_for_run(conn, run_id, limit=2000, newest_first=False):
            if str(row["event_kind"]) != "waiting_for_cursor_fix_entered":
                continue
            parsed = json.loads(str(row["event_payload"]))
            if parsed.get("run_id") == run_id:
                checkpoint = parsed
    assert checkpoint is not None
    assert evidence.prompt_path == checkpoint["correction_envelope_path"]
    assert evidence.prompt_sha256 == checkpoint["correction_envelope_sha256"]
    assert evidence.fix_prompt_path == checkpoint["fix_prompt_path"]
    assert evidence.fix_prompt_sha256 == checkpoint["fix_prompt_sha256"]
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    successor = str(_force(run_id)["successor_run_id"])
    record = _record(artifacts, successor)
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    assert record.reviewer.session_id == BOOTSTRAP_ID
    assert record.reviews_completed == evidence.reviews_completed
    assert record.submitted_max_review_iterations == (
        source.context.workflow.max_review_iterations
    )
    assert record.effective_review_ceiling == inherited_ceiling
    assert record.chat_id == evidence.chat_id
    assert record.iteration == evidence.iteration
    assert record.raw_fix is not None
    assert record.raw_fix.sha256 == evidence.fix_prompt_sha256
    assert record.base_prompt.sha256 == evidence.prompt_sha256
    effective = artifacts.read_verified_bytes(
        successor,
        record.effective_prompt_path,
        expected_sha256=record.effective_prompt_sha256,
    )
    assert effective == effective_prompt_bytes(envelope)
    assert effective.count(RECOVERY_NOTE.encode("utf-8")) == 1
    assert raw_fix in effective
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["agent_log"])
    log_path.write_bytes(b"")
    complete = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(complete, successor, target_kind="completed", max_ticks=40)
    assert _prompt_args(log_path).count(effective) == 1
    with store.begin_read() as conn:
        accepted, _, _ = store.load_validated_snapshot(conn, successor)
        ceiling = effective_review_ceiling_for_run(store, conn, accepted, artifacts=artifacts)
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.codex.reviews_completed == record.reviews_completed + 1
    assert accepted.context.workflow.max_review_iterations == (
        record.submitted_max_review_iterations
    )
    assert accepted.context.cursor.model == source.context.cursor.model
    assert accepted.cursor.chat_id == record.chat_id
    assert accepted.cursor.iteration == record.iteration
    assert ceiling == record.effective_review_ceiling


def test_newer_correction_after_recovered_correction_survives_usage_limit(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=5)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    _run_until(tick, run_id, target_kind="blocked", max_ticks=20)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    successor = str(_force(run_id)["successor_run_id"])
    historical = _record(artifacts, successor)
    _accept_usage_limited_correction(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
        run_id=successor,
        historical_effective_sha=historical.effective_prompt_sha256,
    )


def test_first_correction_after_recovered_initial_survives_usage_limit(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECORD_REL

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=5)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    successor = str(_force(run_id)["successor_run_id"])
    historical = json.loads(
        (artifacts.run_root(successor) / RECORD_REL).read_text(encoding="utf-8")
    )
    assert historical["turn_kind"] == "initial"
    _accept_usage_limited_correction(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
        run_id=successor,
        historical_effective_sha=str(historical["effective_prompt_sha256"]),
    )


def test_advanced_initial_successor_rejects_corrupt_historical_budget(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
        RECORD_REL,
        CursorInitialRecoveryPublicationIntentV1,
    )
    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import CursorRecoveryRecordV2

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_review_iterations=5)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=NOW,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], run_id)
    successor = str(_force(run_id)["successor_run_id"])
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    advanced = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(advanced, successor, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    _run_until(advanced, successor, target_kind="waiting_usage_limit", max_ticks=20)
    failed = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(failed, successor, target_kind="blocked", max_ticks=25)
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], successor)
    valid = analyze_cursor_recovery_evidence(store, artifacts, successor)
    assert valid.receipt.evidence_status == "authenticated", valid.receipt.safe_summary
    record_path = artifacts.run_root(successor) / RECORD_REL
    original_record = record_path.read_bytes()
    record = CursorRecoveryRecordV2.model_validate_json(original_record)
    carry_path = artifacts.run_root(successor) / record.budget_carry_path
    original_carry = carry_path.read_bytes()
    assert record.turn_kind == "initial"
    assert record.schema_version == 2

    def publish(updated: CursorRecoveryRecordV2) -> None:
        payload = updated.canonical_bytes()
        record_path.write_bytes(payload)
        record_path.chmod(0o600)
        digest = hashlib.sha256(payload).hexdigest()
        with store.begin_immediate() as conn:
            row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor)
            assert row is not None
            intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(
                str(row["intent_payload"])
            )
            assert intent.schema_version == 1
            updated_intent = intent.model_copy(update={"record_sha256": digest})
            intent_bytes = updated_intent.canonical_bytes()
            conn.execute(
                """
                UPDATE scheduler_cursor_initial_recoveries
                SET record_sha256 = ?, intent_payload = ?, intent_payload_sha256 = ?
                WHERE successor_run_id = ?
                """,
                (
                    digest,
                    intent_bytes.decode("utf-8"),
                    hashlib.sha256(intent_bytes).hexdigest(),
                    successor,
                ),
            )

    def restore() -> None:
        record_path.write_bytes(original_record)
        record_path.chmod(0o600)
        carry_path.write_bytes(original_carry)
        carry_path.chmod(0o600)
        publish(record)

    def reject(message: str) -> None:
        with store.begin_read() as conn:
            before = conn.execute(
                "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
                (successor,),
            ).fetchall()
        with pytest.raises(SchedulerEngineError, match=message):
            analyze_cursor_recovery_evidence(store, artifacts, successor)
        checked = CliRunner().invoke(app, ["scheduler", "cursor-retry", successor, "--check"])
        assert checked.exit_code != 0
        assert message in checked.output
        forced = CliRunner().invoke(app, ["scheduler", "cursor-retry", successor, "--force"])
        assert forced.exit_code != 0
        assert message in forced.output
        dispatch = _tick_service(
            git_repo,
            scheduler_paths,
            now=AFTER_FORCE + timedelta(seconds=2),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        dispatch.run_once()
        with store.begin_read() as conn:
            relations = store.list_cursor_initial_recoveries_for_source(
                conn,
                source_run_id=successor,
            )
            attempts = conn.execute(
                "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
                (successor,),
            ).fetchall()
        assert [row for row in relations if str(row["status"]) in {"pending", "ready"}] == []
        assert attempts == before

    for updated in (
        record.model_copy(
            update={
                "submitted_max_review_iterations": record.submitted_max_review_iterations - 1,
            }
        ),
        record.model_copy(
            update={"effective_review_ceiling": record.effective_review_ceiling + 2}
        ),
    ):
        assert updated.budget_carry_sha256 == record.budget_carry_sha256
        publish(updated)
        assert carry_path.read_bytes() == original_carry
        reject("budget disagrees with submitted configuration")
        restore()
        restored = analyze_cursor_recovery_evidence(store, artifacts, successor)
        assert restored.receipt.evidence_status == "authenticated"

    carry_path.write_bytes(b"corrupted-historical-carry\n")
    carry_path.chmod(0o600)
    reject("budget carry failed authentication")
    restore()
    restored = analyze_cursor_recovery_evidence(store, artifacts, successor)
    assert restored.receipt.evidence_status == "authenticated"


def _ordinary_initial_successor_blocked(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, SqliteSchedulerStore, ProtectedArtifactStore]:
    original = _blocked_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    source = str(_force(original)["successor_run_id"])
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, source, target_kind="waiting_for_cursor_fix", max_ticks=40)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "fail")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "1")
    _drive_until(git_repo, scheduler_paths, source, target_kind="blocked")
    _normalize_run_artifact_permissions(scheduler_paths["artifact_root"], source)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return original, source, store, artifacts


def _assert_no_correction_publication(store: SqliteSchedulerStore, source: str) -> None:
    with store.begin_read() as conn:
        relations = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=source)
    assert [row for row in relations if str(row["status"]) in {"pending", "ready"}] == []


def test_ordinary_correction_rejects_corrupt_historical_initial_budget(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original, source, store, artifacts = _ordinary_initial_successor_blocked(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    _rewrite_ready_record(
        store,
        artifacts,
        source,
        record_updates={
            "submitted_max_review_iterations": 999,
            "effective_review_ceiling": 999,
        },
        intent_updates={},
    )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="budget disagrees"):
        analyze_cursor_recovery_evidence(store, artifacts, source)
    checked = CliRunner().invoke(app, ["scheduler", "cursor-retry", source, "--check"])
    assert checked.exit_code != 0
    assert "budget disagrees" in checked.output
    with pytest.raises(SchedulerEngineError, match="budget disagrees"):
        service.force(original)
    with pytest.raises(SchedulerEngineError, match="budget disagrees"):
        service.force(source)
    _assert_no_correction_publication(store, source)


def test_ordinary_correction_rejects_corrupt_historical_initial_carry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECORD_REL

    original, source, store, artifacts = _ordinary_initial_successor_blocked(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    record = json.loads((artifacts.run_root(source) / RECORD_REL).read_bytes())
    carry_path = artifacts.run_root(source) / str(record["budget_carry_path"])
    carry_path.write_bytes(b"corrupted budget carry")
    carry_path.chmod(0o600)
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        analyze_cursor_recovery_evidence(store, artifacts, source)
    checked = CliRunner().invoke(app, ["scheduler", "cursor-retry", source, "--check"])
    assert checked.exit_code != 0
    assert "budget carry failed authentication" in checked.output
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        service.force(original)
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        service.force(source)
    _assert_no_correction_publication(store, source)


def test_published_correction_replay_rejects_corrupt_initial_ancestor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECORD_REL

    original, source, store, artifacts = _ordinary_initial_successor_blocked(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = CursorInitialRecoveryService(store, artifacts)
    recovered = service.force(source)
    assert recovered.publication_status == "ready"
    record = json.loads((artifacts.run_root(source) / RECORD_REL).read_bytes())
    carry_path = artifacts.run_root(source) / str(record["budget_carry_path"])
    carry_path.write_bytes(b"corrupted budget carry")
    carry_path.chmod(0o600)
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        service.force(original)
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        service.force(source)


def test_published_correction_dispatch_rejects_corrupt_initial_ancestor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECORD_REL

    original, source, store, artifacts = _ordinary_initial_successor_blocked(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = CursorInitialRecoveryService(store, artifacts)
    recovered = service.force(source)
    assert recovered.publication_status == "ready"
    record = json.loads((artifacts.run_root(source) / RECORD_REL).read_bytes())
    carry_path = artifacts.run_root(source) / str(record["budget_carry_path"])
    carry_path.write_bytes(b"corrupted budget carry")
    carry_path.chmod(0o600)
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="budget carry failed authentication"):
        service.force(original)
    with store.begin_read() as conn:
        before = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (recovered.successor_run_id,),
        ).fetchall()
    launch = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    launch.run_once()
    with store.begin_read() as conn:
        after = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (recovered.successor_run_id,),
        ).fetchall()
    assert after == before


def test_ordinary_correction_of_initial_successor_accepts_same_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _original, source, store, artifacts = _ordinary_initial_successor_blocked(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    analysis = analyze_cursor_recovery_evidence(store, artifacts, source)
    assert analysis.receipt.evidence_status == "authenticated"
    assert analysis.evidence is not None
    assert analysis.evidence.turn_kind == "correction"
    service = CursorInitialRecoveryService(store, artifacts)
    recovered = service.force(source)
    assert recovered.publication_status == "ready"
    record = _record(artifacts, recovered.successor_run_id)
    assert isinstance(record.reviewer, BoundReviewerEvidenceV2)
    assert record.reviewer.session_id == BOOTSTRAP_ID
    assert record.reviews_completed == analysis.evidence.reviews_completed
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    log_path = Path(fake_clis["codex_log"])
    log_path.write_bytes(b"")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, recovered.successor_run_id, target_kind="completed", max_ticks=30)
    with store.begin_read() as conn:
        accepted, _, _ = store.load_validated_snapshot(conn, recovered.successor_run_id)
    assert accepted.codex.reviewer_session_id == BOOTSTRAP_ID
    assert accepted.codex.reviews_completed == record.reviews_completed + 1


def test_correction_failure_beyond_first_event_page_agrees_with_force(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.cursor_recovery_evidence import EVENT_PAGE_SIZE
    from ai_dev_loop.scheduler.domain.common import payload_sha256
    from ai_dev_loop.scheduler.domain.events import TickStaleRejectedEvent

    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    payload = TickStaleRejectedEvent(
        run_id=run_id,
        rejection_kind="pagination_padding",
        safe_summary="pagination padding",
    ).model_dump_json()
    digest = payload_sha256(payload)
    with store.begin_immediate() as conn:
        created = conn.execute(
            "SELECT created_at FROM scheduler_events WHERE run_id = ? ORDER BY sequence LIMIT 1",
            (run_id,),
        ).fetchone()
        assert created is not None
        conn.execute(
            "UPDATE scheduler_events SET sequence = sequence + 1000000 WHERE run_id = ?",
            (run_id,),
        )
        conn.execute(
            """
            UPDATE scheduler_events
            SET sequence = sequence - 1000000 + ?
            WHERE run_id = ?
            """,
            (EVENT_PAGE_SIZE, run_id),
        )
        conn.executemany(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"pad-{index:04d}",
                    run_id,
                    index,
                    "tick_stale_rejected",
                    payload,
                    digest,
                    str(created["created_at"]),
                )
                for index in range(1, EVENT_PAGE_SIZE + 1)
            ],
        )
    analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
    assert analysis.receipt.evidence_status == "authenticated"
    evidence = analysis.evidence
    assert evidence is not None
    successor = str(_force(run_id)["successor_run_id"])
    assert _record(artifacts, successor).failed_attempt_id == evidence.failed_attempt_id
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    Path(fake_clis["agent_log"]).write_text("", encoding="utf-8")
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, successor, target_kind="completed", max_ticks=30)


def test_corrupt_or_incomplete_event_pages_reject_publication(
    scheduler_paths: dict[str, Path],
    git_repo: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _blocked_correction_failure(git_repo, scheduler_paths, fake_clis, monkeypatch)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    successor = str(_force(run_id)["successor_run_id"])
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_events
            SET event_payload_sha256 = ?
            WHERE event_id = (
                SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence LIMIT 1
            )
            """,
            ("0" * 64, run_id),
        )
    service = CursorInitialRecoveryService(store, artifacts)
    with pytest.raises(SchedulerEngineError, match="event page failed authentication"):
        service.force(run_id)
    _assert_dispatch_rejected(git_repo, scheduler_paths, successor)
    with store.begin_immediate() as conn:
        row = conn.execute(
            """
            SELECT event_payload FROM scheduler_events
            WHERE run_id = ? ORDER BY sequence LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        conn.execute(
            """
            UPDATE scheduler_events
            SET event_payload_sha256 = ?
            WHERE event_id = (
                SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence LIMIT 1
            )
            """,
            (payload_sha256(str(row["event_payload"])), run_id),
        )
    with (
        patch(
            "ai_dev_loop.scheduler.application.cursor_recovery_evidence._load_all_verified_events",
            return_value=([], False),
        ),
        pytest.raises(SchedulerEngineError, match="pagination did not complete"),
    ):
        service.force(run_id)


def _assert_dispatch_rejected(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    successor: str,
) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        before = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=AFTER_FORCE,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = tick.run_once()
    with store.begin_read() as conn:
        successor_state, _, _ = store.load_validated_snapshot(conn, successor)
        attempts = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id = ?",
            (successor,),
        ).fetchall()
    assert successor_state.kind == "cursor_ready"
    assert attempts == before
    assert any(
        item.run_id == successor and item.action == "cursor_initial_recovery_evidence_invalid"
        for item in receipt.run_receipts
    )
