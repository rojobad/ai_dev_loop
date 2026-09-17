"""Phase 20.8 correction tests: replay, fencing, routing, corruption, multi-generation."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _blocked_sequence_review_fixture,
    _legacy_block_instead_of_retry,
)

from ai_dev_loop.scheduler.application.codex_workflow_service import CodexWorkflowService
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_recovery import (
    resolve_recovery_ledger_evidence_run_id,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_review_recovery import (
    _adopt_sequence_recovery_successor,
)
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState, BlockedSequenceState
from ai_dev_loop.scheduler.domain.state import BlockedState, WaitingCodexReviewRetryState
from ai_dev_loop.scheduler.infrastructure.sequence_execution_replacement_intent_store import (
    load_validated_sequence_execution_replacement_intent,
)
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


def test_interrupted_before_adoption_replays_to_single_successor(
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
    adopt_calls = 0
    real_adopt = _adopt_sequence_recovery_successor

    def _adopt_once_then_fail(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal adopt_calls
        adopt_calls += 1
        if adopt_calls == 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "simulated crash before sequence adoption",
            )
        return real_adopt(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.sequence_review_recovery._adopt_sequence_recovery_successor",
        _adopt_once_then_fail,
    )
    service = ReviewRetryService(store, artifacts)
    with pytest.raises(SchedulerEngineError):
        service.retry(source_run_id)
    with store.begin_read() as conn:
        pending_rows = conn.execute(
            """
            SELECT run_id, state_kind
            FROM scheduler_runs
            WHERE state_kind = 'pending_sequence_review_recovery'
            """
        ).fetchall()
        tick_eligible = store.list_tick_eligible_run_ids(conn)
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert len(pending_rows) == 1
    assert pending_rows[0]["run_id"] not in tick_eligible
    assert isinstance(sequence, BlockedSequenceState)

    outcome = service.retry(source_run_id)
    assert outcome.changed is True
    successor_id = outcome.run_id
    replay = service.retry(source_run_id)
    assert replay.idempotent_replay is True
    assert replay.run_id == successor_id
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        intent_rows = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_execution_replacements WHERE source_run_id = ?",
            (source_run_id,),
        ).fetchall()
        tick_eligible = store.list_tick_eligible_run_ids(conn)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_run_id == successor_id
    assert len(intent_rows) == 1
    assert successor_id in tick_eligible


def test_corrupt_intent_digest_rejects_replay(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = ReviewRetryService(store, artifacts)
    service.retry(source_run_id)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_sequence_execution_replacements
            SET intent_payload_sha256 = 'deadbeef'
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        )
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT recovery_key FROM scheduler_sequence_execution_replacements WHERE source_run_id = ?",
            (source_run_id,),
        ).fetchone()
        assert row is not None
        with pytest.raises(SchedulerEngineError, match="digest mismatch"):
            load_validated_sequence_execution_replacement_intent(
                store,
                conn,
                source_run_id=source_run_id,
                recovery_key=str(row["recovery_key"]),
            )


def test_active_sequence_rejects_fresh_recovery_without_published_intent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = ReviewRetryService(store, artifacts)
    service.retry(source_run_id)
    with store.begin_immediate() as conn:
        conn.execute(
            "DELETE FROM scheduler_sequence_execution_replacements WHERE source_run_id = ?",
            (source_run_id,),
        )
    with pytest.raises(SchedulerEngineError, match="still active"):
        service.retry(source_run_id)


def test_published_replay_after_successor_completed_is_idempotent(
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
    successor_id = recovery.run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    for _ in range(120):
        tick.run_once()
        with tick.store.begin_read() as conn:
            successor, _, _ = tick.store.load_validated_snapshot(conn, successor_id)
            if successor.kind == "completed":
                break
    assert successor.kind == "completed"
    with store.begin_read() as conn:
        events_before = conn.execute(
            "SELECT COUNT(*) AS c FROM scheduler_events WHERE run_id = ?",
            (source_run_id,),
        ).fetchone()
    replay = service.retry(source_run_id)
    assert replay.idempotent_replay is True
    assert replay.run_id == successor_id
    assert replay.state_kind == "completed"
    with store.begin_read() as conn:
        events_after = conn.execute(
            "SELECT COUNT(*) AS c FROM scheduler_events WHERE run_id = ?",
            (source_run_id,),
        ).fetchone()
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert events_after["c"] == events_before["c"]
    assert isinstance(sequence, ActiveSequenceState)


def test_two_successive_recovery_generations_and_handoff(
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
    gen1 = service.retry(source_run_id)
    gen1_id = gen1.run_id
    assert tick._codex_workflow is not None
    workflow = tick._codex_workflow
    monkeypatch.setattr(
        workflow,
        "_enter_review_retry_wait",
        CodexWorkflowService._enter_review_retry_wait.__get__(workflow, CodexWorkflowService),
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    for _ in range(120):
        tick.run_once()
        with tick.store.begin_read() as conn:
            gen1_state, _, _ = tick.store.load_validated_snapshot(conn, gen1_id)
            if isinstance(gen1_state, WaitingCodexReviewRetryState):
                break
    else:
        raise AssertionError("gen1 did not reach waiting_codex_review_retry")
    service.retry(gen1_id)
    _legacy_block_instead_of_retry(tick, monkeypatch)
    for _ in range(120):
        tick.run_once()
        with tick.store.begin_read() as conn:
            gen1_state, _, _ = tick.store.load_validated_snapshot(conn, gen1_id)
            sequence = tick.store.load_validated_sequence_state(conn, sequence_id)
            if (
                isinstance(gen1_state, BlockedState)
                and isinstance(sequence, BlockedSequenceState)
                and sequence.current_run_id == gen1_id
            ):
                break
    else:
        raise AssertionError("gen1 did not block for second recovery generation")
    gen2 = service.retry(gen1_id)
    assert gen2.changed is True
    gen2_id = gen2.run_id
    with store.begin_read() as conn:
        row = store.get_review_recovery_source_for_successor(conn, successor_run_id=gen2_id)
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        gen1_state, _, _ = store.load_validated_snapshot(conn, gen1_id)
        lineage = load_sequence_run_lineage(conn, sequence_id)
    assert row is not None
    assert str(row["source_run_id"]) == gen1_id
    assert source_state.kind == "blocked"
    assert gen1_state.kind == "blocked"
    assert len(lineage.phase_executions[0].attempts) == 3
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    for _ in range(120):
        tick.run_once()
        with tick.store.begin_read() as conn:
            gen2_state, _, _ = tick.store.load_validated_snapshot(conn, gen2_id)
            if gen2_state.kind == "completed":
                break
    assert gen2_state.kind == "completed"


def test_sequence_abort_cancels_outstanding_replacement_before_adoption(
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

    def _block_adoption(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "simulated crash before sequence adoption",
        )

    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.sequence_review_recovery._adopt_sequence_recovery_successor",
        _block_adoption,
    )
    service = ReviewRetryService(store, artifacts)
    with pytest.raises(SchedulerEngineError):
        service.retry(source_run_id)
    from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService

    abort = SequenceAbortService(store)
    result = abort.abort_sequence(sequence_id)
    assert result.state_kind == "aborted"
    with pytest.raises(SchedulerEngineError):
        service.retry(source_run_id)


def test_recovery_successor_inherits_ledger_evidence_from_chain(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sequence_id, source_run_id, _tick, store, artifacts = _blocked_sequence_review_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    service = ReviewRetryService(store, artifacts)
    gen1 = service.retry(source_run_id)
    gen1_id = gen1.run_id
    with store.begin_read() as conn:
        lineage_row = store.get_review_recovery_source_for_successor(
            conn,
            successor_run_id=gen1_id,
        )
        evidence_run_id = resolve_recovery_ledger_evidence_run_id(store, conn, gen1_id)
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    assert lineage_row is not None
    assert str(lineage_row["source_run_id"]) == source_run_id
    assert evidence_run_id == source_run_id
    assert source_state.kind == "blocked"
