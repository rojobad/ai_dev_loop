"""Phase 23.4 sequence Cursor recovery through real fake-agent ticks."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
    _prepare_three_phase,
    _tick_service,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_SEQUENCE_ID
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.sequence_service import IntegrationSequenceReadService
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
    CursorInitialRecoveryFault,
    CursorInitialRecoveryService,
    cursor_initial_recovery_blocks_dispatch,
)
from ai_dev_loop.scheduler.application.review_budget import effective_review_ceiling_for_run
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    EFFECTIVE_PROMPT_REL,
    RECORD_REL,
    RECOVERY_NOTE,
)
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import CORRECTION_RECORD_REL
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _arm(monkeypatch: pytest.MonkeyPatch, agent_sequence: str, review_sequence: str) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", agent_sequence)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.delenv("FAKE_AGENT_RUN_MODE", raising=False)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)


def _started(git_repo: Path, scheduler_paths: dict[str, Path]) -> TickService:
    _prepare_three_phase(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    return _tick_service(git_repo, scheduler_paths)


def _until_sequence(
    tick: TickService,
    kind: type[BlockedSequenceState] | type[ActiveSequenceState],
    *,
    limit: int = 250,
) -> BlockedSequenceState | ActiveSequenceState:
    last = None
    run_kind = None
    for _ in range(limit):
        tick.run_once()
        with tick.store.begin_read() as conn:
            last = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            if isinstance(last, (ActiveSequenceState, BlockedSequenceState)):
                run_state, _, _ = tick.store.load_validated_snapshot(conn, last.current_run_id)
                run_kind = f"{last.current_ordinal}:{run_state.kind}"
        if isinstance(last, kind):
            return last
    raise AssertionError(f"sequence stayed {type(last).__name__} run={run_kind}")


def _worktree_snapshot(repo: Path) -> bytes:
    parts = [
        subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo),
        subprocess.check_output(["git", "diff"], cwd=repo),
        subprocess.check_output(["git", "diff", "--cached"], cwd=repo),
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo),
    ]
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo,
        text=True,
    )
    for relative in untracked.splitlines():
        parts.append((repo / relative).read_bytes())
    return b"\0".join(parts)


def _recovery(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    **kwargs: object,
) -> CursorInitialRecoveryService:
    return CursorInitialRecoveryService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        **kwargs,  # type: ignore[arg-type]
    )


def _attempts(store: SqliteSchedulerStore, run_id: str) -> int:
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    return int(row[0])


def test_cursor_retry_help_mentions_sequence_leaf_recovery() -> None:
    result = CliRunner().invoke(app, ["scheduler", "cursor-retry", "--help"])
    assert result.exit_code == 0
    assert "sequence" in result.output


def test_middle_phase_initial_recovery_adopts_and_finishes_original_sequence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    assert blocked.current_ordinal == 2
    source_run_id = blocked.current_run_id
    definition = blocked.definition
    before = _worktree_snapshot(git_repo)
    head_before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    recovery = _recovery(store, artifacts).force(source_run_id)
    assert _worktree_snapshot(git_repo) == before
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=git_repo) == head_before
    successor = recovery.successor_run_id
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        reservation = store.get_reservation_for_run(conn, successor)
        effects = conn.execute(
            """
            SELECT effect_kind FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.run_turn'
            """,
            (successor,),
        ).fetchall()
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.sequence_id == FIXED_SEQUENCE_ID
    assert sequence.current_ordinal == 2
    assert sequence.current_run_id == successor
    assert sequence.definition == definition
    assert isinstance(source_state, BlockedState)
    assert reservation is not None
    assert len(effects) == 1
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            current = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        if isinstance(current, AwaitingFinalizationSequenceState):
            break
    else:
        with tick.store.begin_read() as conn:
            run_state, _, _ = tick.store.load_validated_snapshot(conn, current.current_run_id)
        raise AssertionError(
            f"{type(current).__name__} ordinal={getattr(current, 'current_ordinal', None)} "
            f"run={run_state.kind}"
        )
    replay = _recovery(store, artifacts).force(source_run_id)
    assert replay.successor_run_id == successor
    assert replay.idempotent_replay is True
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT generation, attempt_kind, run_id, terminal_outcome
            FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 2
            ORDER BY generation
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
        source_after, _, _ = store.load_validated_snapshot(conn, source_run_id)
        future = conn.execute(
            """
            SELECT run_id, attempt_kind FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3 AND generation = 1
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchone()
    assert [str(row["attempt_kind"]) for row in rows] == ["planned_run", "cursor_retry"]
    assert str(rows[0]["run_id"]) == source_run_id
    assert str(rows[0]["terminal_outcome"]) == "blocked"
    assert str(rows[1]["run_id"]) == successor
    assert str(rows[1]["terminal_outcome"]) == "completed"
    assert isinstance(source_after, BlockedState)
    assert future is not None
    assert str(future["attempt_kind"]) == "planned_run"
    assert str(future["run_id"]) == definition.entries[2].planned_run_id
    log = subprocess.check_output(["git", "log", "--format=%s"], cwd=git_repo, text=True)
    assert log.count("checkpoint after phase-one") == 1
    assert log.count("checkpoint after phase-two") == 1
    assert "checkpoint after phase-final" not in log
    status = SequenceStatusService(store, artifacts=artifacts).get_status(FIXED_SEQUENCE_ID)
    rendered = status.model_dump_json()
    assert "cursor_retry" in rendered
    assert RECOVERY_NOTE not in rendered
    assert BOOTSTRAP_ID not in rendered
    inspected = IntegrationSequenceReadService(store, artifact_root=scheduler_paths["artifact_root"])
    payload = inspected.inspect_sequence(FIXED_SEQUENCE_ID).model_dump_json(by_alias=True)
    assert "cursor_retry" in payload
    assert RECOVERY_NOTE not in payload
    assert "cursor_fix_prompt" not in payload


def test_pending_successor_cannot_dispatch_and_restart_converges(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    crashing = _recovery(store, artifacts, fault_point="after_sequence_intent")
    with pytest.raises(CursorInitialRecoveryFault):
        crashing.force(blocked.current_run_id)
    with store.begin_read() as conn:
        row = conn.execute(
            "SELECT successor_run_id, adopted_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        assert row is not None
        successor = str(row["successor_run_id"])
        assert row["adopted_at"] is None
        assert cursor_initial_recovery_blocks_dispatch(store, conn, successor) is True
        event = conn.execute(
            "SELECT event_id FROM scheduler_events WHERE run_id = ? ORDER BY sequence LIMIT 1",
            (successor,),
        ).fetchone()
    assert event is not None
    from ai_dev_loop.scheduler.domain.cursor_contract import (
        RUN_CURSOR_TURN_EFFECT_ID,
        RUN_CURSOR_TURN_EFFECT_KIND,
    )

    with store.begin_immediate() as conn:
        store.insert_effect(
            conn,
            dispatch_id="dsp-queued-old",
            source_event_id=str(event["event_id"]),
            run_id=successor,
            effect_id=RUN_CURSOR_TURN_EFFECT_ID,
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
            effect_payload={"iteration": 1},
            available_at=NOW,
            claimed_run_version=1,
            now=NOW,
        )
    assert tick._cursor_workflow is not None
    assert tick._cursor_workflow.process_run("tick-direct", 1, successor) == []
    assert _attempts(store, successor) == 0
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            current = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            ready = conn.execute(
                """
                SELECT status FROM scheduler_cursor_initial_recoveries
                WHERE successor_run_id = ?
                """,
                (successor,),
            ).fetchone()
        if (
            ready is not None
            and str(ready["status"]) == "ready"
            and isinstance(current, ActiveSequenceState)
            and current.current_run_id == successor
        ):
            break
    else:
        raise AssertionError("restart did not adopt a ready successor")
    assert _attempts(store, successor) >= 1
    with store.begin_read() as conn:
        effects = conn.execute(
            """
            SELECT effect_id FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.run_turn'
            """,
            (successor,),
        ).fetchall()
        replacements = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
        reservation = store.get_reservation_for_run(conn, successor)
    assert len(effects) == 1
    assert [str(row["successor_run_id"]) for row in replacements] == [successor]
    assert reservation is not None
    assert str(reservation["run_id"]) == successor


def test_duplicate_force_race_keeps_one_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    barrier = threading.Barrier(2)
    service = CursorInitialRecoveryService(
        store,
        artifacts,
        now_factory=lambda: NOW,
        before_create=barrier.wait,
    )
    results: list[str] = []
    errors: list[BaseException] = []

    def _force() -> None:
        try:
            results.append(service.force(blocked.current_run_id).successor_run_id)
        except BaseException as exc:  # noqa: BLE001 - race result is asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_force) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert all(not thread.is_alive() for thread in threads)
    with store.begin_read() as conn:
        successors = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
        reservations = conn.execute(
            """
            SELECT run_id FROM scheduler_repository_reservations
            WHERE status = 'active'
            """
        ).fetchall()
    assert all(isinstance(exc, SchedulerEngineError) for exc in errors)
    assert len(successors) == 1
    assert {str(row["run_id"]) for row in reservations} == {str(successors[0]["successor_run_id"])}
    assert len(results) >= 1
    assert set(results) == {str(successors[0]["successor_run_id"])}
    tick.run_once()
    with store.begin_read() as conn:
        after = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
    assert [str(row["successor_run_id"]) for row in after] == [
        str(successors[0]["successor_run_id"])
    ]


def test_abort_before_adoption_cancels_replacement_and_releases_reservation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    crashing = _recovery(store, artifacts, fault_point="after_sequence_intent")
    with pytest.raises(CursorInitialRecoveryFault):
        crashing.force(blocked.current_run_id)
    with store.begin_read() as conn:
        successor = str(
            conn.execute(
                "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
            ).fetchone()["successor_run_id"]
        )
    SequenceAbortService(store).abort_sequence(FIXED_SEQUENCE_ID)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        replacement = conn.execute(
            "SELECT cancelled_at, adopted_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        active = conn.execute(
            "SELECT run_id FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchall()
        recovery = conn.execute(
            "SELECT status FROM scheduler_cursor_initial_recoveries"
        ).fetchone()
    assert isinstance(sequence, AbortedSequenceState)
    assert replacement["cancelled_at"] is not None
    assert replacement["adopted_at"] is None
    assert active == []
    assert str(recovery["status"]) == "cancelled"
    assert tick._cursor_workflow is not None
    assert tick._cursor_workflow.process_run("tick-abort", 1, successor) == []
    assert _attempts(store, successor) == 0
    with store.begin_read() as conn:
        phase_three = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
    assert phase_three == []


def test_abort_after_adoption_does_not_materialize_the_next_phase(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    crashing = _recovery(store, artifacts, fault_point="after_sequence_adoption")
    with pytest.raises(CursorInitialRecoveryFault):
        crashing.force(blocked.current_run_id)
    result = SequenceAbortService(store).abort_sequence(FIXED_SEQUENCE_ID)
    assert result.state_kind in {"abort_pending", "aborted"}
    with store.begin_read() as conn:
        phase_three = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
        successor_row = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        successor = str(successor_row["successor_run_id"])
        effects = conn.execute(
            """
            SELECT effect_id FROM scheduler_effects
            WHERE effect_kind = 'cursor.run_turn' AND run_id = ?
            """,
            (successor,),
        ).fetchall()
    assert phase_three == []
    assert effects == []
    tick.run_once()
    with store.begin_read() as conn:
        later = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
    assert later == []


def test_stale_leaf_force_rejects_without_a_new_successor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
        SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
        SequenceRunAttempt,
    )
    from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
        insert_sequence_run_attempt,
    )

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    replacement = f"{blocked.current_run_id}-later"
    current_entry = next(
        entry for entry in blocked.materialized_entries if entry.ordinal == blocked.current_ordinal
    )
    moved = blocked.model_copy(
        update={
            "version": blocked.version + 1,
            "current_run_id": replacement,
            "materialized_entries": tuple(
                entry.model_copy(update={"run_id": replacement})
                if entry.ordinal == blocked.current_ordinal
                else entry
                for entry in blocked.materialized_entries
            ),
        }
    )
    kind, payload, digest = store.dump_sequence_state(moved)
    planned_run_id = blocked.definition.entries[blocked.current_ordinal - 1].planned_run_id
    with store.begin_immediate() as conn:
        insert_sequence_run_attempt(
            conn,
            sequence_id=FIXED_SEQUENCE_ID,
            ordinal=blocked.current_ordinal,
            planned_run_id=planned_run_id,
            attempt=SequenceRunAttempt(
                schema_version=1,
                generation=2,
                run_id=replacement,
                source_run_id=blocked.current_run_id,
                attempt_kind=SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
                materialized_at=current_entry.materialized_at,
                terminal_outcome="blocked",
                resolved_at=blocked.blocked_at,
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?, version = ?
            WHERE sequence_id = ?
            """,
            (kind, payload, digest, moved.version, FIXED_SEQUENCE_ID),
        )
    service = _recovery(store, ProtectedArtifactStore(scheduler_paths["artifact_root"]))
    with pytest.raises(SchedulerEngineError, match="ineligible_sequence_stale_leaf"):
        service.force(blocked.current_run_id)
    with store.begin_read() as conn:
        rows = conn.execute("SELECT successor_run_id FROM scheduler_sequence_cursor_replacements").fetchall()
    assert rows == []


def test_correction_recovery_keeps_reviewer_extended_ceiling_and_one_note(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(
        monkeypatch,
        "success,success,success,success,fail,success,success",
        "no_findings,findings,findings,findings,no_findings,no_findings",
    )
    tick = _started(git_repo, scheduler_paths)
    exhausted = None
    observed = None
    for _ in range(250):
        tick.run_once()
        with tick.store.begin_read() as conn:
            sequence = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            observed = type(sequence).__name__
            if isinstance(sequence, (ActiveSequenceState, BlockedSequenceState)):
                state, _, _ = tick.store.load_validated_snapshot(conn, sequence.current_run_id)
                observed = f"{sequence.current_ordinal}:{state.kind}"
                if sequence.current_ordinal == 2 and state.kind == "max_iterations_reached":
                    exhausted = sequence.current_run_id
                    break
    assert exhausted is not None, observed
    ReviewBudgetExtendService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: NOW,
    ).extend(exhausted, target_total=4)
    blocked_run = None
    last_kind = None
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, exhausted)
        last_kind = state.kind
        if isinstance(state, BlockedState) and state.block_reason_kind == "cursor_failure":
            blocked_run = exhausted
            break
    assert blocked_run is not None, last_kind
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, blocked_run)
    assert source.context.workflow.max_review_iterations == 3
    for path in Path(scheduler_paths["artifact_root"]).rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
        elif path.is_dir():
            os.chmod(path, 0o700)
    recovery = _recovery(store, artifacts).force(blocked_run)
    with store.begin_read() as conn:
        successor, _, _ = store.load_validated_snapshot(conn, recovery.successor_run_id)
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_ordinal == 2
    assert sequence.current_run_id == recovery.successor_run_id
    assert successor.codex.reviewer_session_id == BOOTSTRAP_ID
    assert successor.codex.reviews_completed >= 1
    assert successor.context.workflow.max_review_iterations == 3
    with store.begin_read() as conn:
        assert (
            effective_review_ceiling_for_run(store, conn, successor, artifacts=artifacts) == 4
        )
    record = json.loads(
        artifacts.run_root(recovery.successor_run_id).joinpath(CORRECTION_RECORD_REL).read_text()
    )
    assert record["effective_review_ceiling"] == 4
    assert record["submitted_max_review_iterations"] == 3
    assert record["reviewer"]["form"] == "bound"
    assert record["reviewer"]["session_id"] == BOOTSTRAP_ID
    effective = artifacts.run_root(recovery.successor_run_id).joinpath(EFFECTIVE_PROMPT_REL).read_bytes()
    assert effective.count(RECOVERY_NOTE.encode()) == 1
    observed = None
    replayed = False
    for _ in range(250):
        tick.run_once()
        with tick.store.begin_read() as conn:
            current = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            if isinstance(current, ActiveSequenceState):
                run_state, _, _ = tick.store.load_validated_snapshot(conn, current.current_run_id)
                observed = f"{current.current_ordinal}:{run_state.kind}"
        if (
            isinstance(current, ActiveSequenceState)
            and current.current_ordinal == 3
            and not replayed
        ):
            from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
                analyze_cursor_recovery_evidence,
            )

            fresh = analyze_cursor_recovery_evidence(store, artifacts, blocked_run)
            assert fresh.receipt.recovery_supported is False
            assert fresh.receipt.reason_code == "insufficient_sequence_ordinal"
            replay = _recovery(store, artifacts).force(blocked_run)
            assert replay.successor_run_id == recovery.successor_run_id
            assert replay.idempotent_replay is True
            record_path = artifacts.run_root(recovery.successor_run_id) / CORRECTION_RECORD_REL
            record_path.write_bytes(b"{}")
            os.chmod(record_path, 0o600)
            with pytest.raises(SchedulerEngineError):
                _recovery(store, artifacts).force(blocked_run)
            with store.begin_read() as conn:
                rows = conn.execute(
                    "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
                ).fetchall()
            assert [str(row["successor_run_id"]) for row in rows] == [recovery.successor_run_id]
            replayed = True
        if isinstance(current, AwaitingFinalizationSequenceState):
            assert replayed
            return
    raise AssertionError(f"{type(current).__name__} {observed} replayed={replayed}")


def _successor_of(store: SqliteSchedulerStore, source_run_id: str) -> str:
    with store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT successor_run_id FROM scheduler_cursor_initial_recoveries
            WHERE source_run_id = ?
            """,
            (source_run_id,),
        ).fetchone()
    assert row is not None
    return str(row["successor_run_id"])


def _assert_one_leaf(store: SqliteSchedulerStore, successor: str) -> None:
    with store.begin_read() as conn:
        replacements = conn.execute(
            "SELECT successor_run_id, adopted_at FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
        effects = conn.execute(
            """
            SELECT effect_id FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.run_turn'
            """,
            (successor,),
        ).fetchall()
        active = conn.execute(
            """
            SELECT run_id FROM scheduler_repository_reservations
            WHERE status = 'active'
            """
        ).fetchall()
    assert [str(row["successor_run_id"]) for row in replacements] == [successor]
    assert replacements[0]["adopted_at"] is not None
    assert len(effects) == 1
    assert [str(row["run_id"]) for row in active] == [successor]


@pytest.mark.parametrize(
    "fault_point",
    [
        "after_candidate",
        "after_sequence_intent",
        "after_artifacts",
        "after_sequence_adoption",
        "after_ready",
    ],
)
def test_interrupted_publication_converges_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    fault_point: str,
) -> None:
    _arm(monkeypatch, "success,fail,success,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    assert blocked.current_ordinal == 2
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point=fault_point).force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    observed = None
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            ready = conn.execute(
                """
                SELECT status FROM scheduler_cursor_initial_recoveries
                WHERE successor_run_id = ?
                """,
                (successor,),
            ).fetchone()
        observed = type(sequence).__name__
        if (
            ready is not None
            and str(ready["status"]) == "ready"
            and isinstance(sequence, ActiveSequenceState)
            and sequence.current_run_id == successor
            and _attempts(store, successor) >= 1
        ):
            break
    else:
        raise AssertionError(f"{fault_point} did not converge: {observed}")
    _assert_one_leaf(store, successor)
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, blocked.current_run_id)
    assert isinstance(source, BlockedState)
    tick.run_once()
    _assert_one_leaf(store, successor)


def test_corrupt_record_is_not_adopted_by_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_artifacts").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    record_path = artifacts.run_root(successor) / RECORD_REL
    record_path.write_bytes(b"{}")
    os.chmod(record_path, 0o600)
    receipt = tick.run_once()
    assert any(
        item.action == "sequence_cursor_publication_rejected" for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        replacement = conn.execute(
            "SELECT adopted_at, cancelled_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        status = conn.execute(
            "SELECT status FROM scheduler_cursor_initial_recoveries WHERE successor_run_id = ?",
            (successor,),
        ).fetchone()
    assert isinstance(sequence, BlockedSequenceState)
    assert sequence.current_run_id == blocked.current_run_id
    assert replacement["adopted_at"] is None
    assert replacement["cancelled_at"] is None
    assert str(status["status"]) == "pending"
    with store.begin_read() as conn:
        assert cursor_initial_recovery_blocks_dispatch(store, conn, successor) is True
    assert _attempts(store, successor) == 0


def test_review_retry_cannot_replace_a_cursor_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_sequence_intent").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    with pytest.raises(SchedulerEngineError):
        ReviewRetryService(store, artifacts, now_factory=lambda: NOW).retry(blocked.current_run_id)
    with store.begin_read() as conn:
        review_rows = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_execution_replacements"
        ).fetchall()
        cursor_rows = conn.execute(
            "SELECT successor_run_id, adopted_at FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
    assert review_rows == []
    assert [str(row["successor_run_id"]) for row in cursor_rows] == [successor]
    assert cursor_rows[0]["adopted_at"] is None
    tick.run_once()
    _assert_one_leaf(store, successor)


def test_successor_abort_before_adoption_does_not_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_sequence_intent").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    assert tick._run_abort is not None
    tick._run_abort.abort_run(successor)
    tick.run_once()
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        replacement = conn.execute(
            "SELECT adopted_at, cancelled_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        phase_three = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
    assert isinstance(sequence, BlockedSequenceState)
    assert sequence.current_run_id == blocked.current_run_id
    assert replacement["adopted_at"] is None
    assert replacement["cancelled_at"] is not None
    assert phase_three == []
    assert _attempts(store, successor) == 0


def test_sequence_abort_after_artifacts_does_not_adopt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_artifacts").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    SequenceAbortService(store).abort_sequence(FIXED_SEQUENCE_ID)
    tick.run_once()
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        replacement = conn.execute(
            "SELECT adopted_at, cancelled_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
    assert isinstance(sequence, AbortedSequenceState)
    assert replacement["adopted_at"] is None
    assert replacement["cancelled_at"] is not None
    assert _attempts(store, successor) == 0


def test_abort_after_ready_does_not_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_ready").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    SequenceAbortService(
        store,
        run_abort=tick._run_abort,
        now_factory=lambda: NOW,
    ).abort_sequence(FIXED_SEQUENCE_ID)
    tick.run_once()
    with store.begin_read() as conn:
        phase_three = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 3
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
    assert phase_three == []
    assert _attempts(store, successor) == 0


def test_second_correction_generation_keeps_reviewer_ceiling_and_one_note(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(
        monkeypatch,
        "success,success,success,success,fail,fail,success,success",
        "no_findings,findings,findings,findings,no_findings,no_findings",
    )
    tick = _started(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    exhausted = None
    for _ in range(250):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            if isinstance(sequence, (ActiveSequenceState, BlockedSequenceState)):
                state, _, _ = store.load_validated_snapshot(conn, sequence.current_run_id)
                if sequence.current_ordinal == 2 and state.kind == "max_iterations_reached":
                    exhausted = sequence.current_run_id
                    break
    assert exhausted is not None
    ReviewBudgetExtendService(store, artifacts, now_factory=lambda: NOW).extend(
        exhausted, target_total=4
    )
    first_source = None
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, exhausted)
        if isinstance(state, BlockedState) and state.block_reason_kind == "cursor_failure":
            first_source = exhausted
            break
    assert first_source is not None
    for path in Path(scheduler_paths["artifact_root"]).rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
        elif path.is_dir():
            os.chmod(path, 0o700)
    first = _recovery(store, artifacts).force(first_source)
    second_source = None
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            if not isinstance(sequence, (ActiveSequenceState, BlockedSequenceState)):
                continue
            state, _, _ = store.load_validated_snapshot(conn, sequence.current_run_id)
        if (
            sequence.current_run_id == first.successor_run_id
            and isinstance(state, BlockedState)
            and state.block_reason_kind == "cursor_failure"
        ):
            second_source = first.successor_run_id
            break
    assert second_source is not None
    for path in Path(scheduler_paths["artifact_root"]).rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
    second = _recovery(store, artifacts).force(second_source)
    notes = []
    for run_id in (first.successor_run_id, second.successor_run_id):
        record = json.loads(
            artifacts.run_root(run_id).joinpath(CORRECTION_RECORD_REL).read_text()
        )
        assert record["reviewer"]["session_id"] == BOOTSTRAP_ID
        assert record["effective_review_ceiling"] == 4
        assert record["submitted_max_review_iterations"] == 3
        prompt = artifacts.run_root(run_id).joinpath(EFFECTIVE_PROMPT_REL).read_bytes()
        notes.append(prompt.count(RECOVERY_NOTE.encode()))
    assert notes == [1, 1]
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        generations = conn.execute(
            """
            SELECT generation, attempt_kind, run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND ordinal = 2
            ORDER BY generation
            """,
            (FIXED_SEQUENCE_ID,),
        ).fetchall()
        successor_state, _, _ = store.load_validated_snapshot(conn, second.successor_run_id)
        assert (
            effective_review_ceiling_for_run(store, conn, successor_state, artifacts=artifacts)
            == 4
        )
    assert isinstance(sequence, ActiveSequenceState)
    assert sequence.current_ordinal == 2
    assert sequence.current_run_id == second.successor_run_id
    assert [str(row["attempt_kind"]) for row in generations] == [
        "planned_run",
        "cursor_retry",
        "cursor_retry",
    ]
    assert successor_state.codex.reviewer_session_id == BOOTSTRAP_ID
    for _ in range(250):
        tick.run_once()
        with store.begin_read() as conn:
            current = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        if isinstance(current, AwaitingFinalizationSequenceState):
            return
    raise AssertionError(type(current).__name__)


def _phase_three(store: SqliteSchedulerStore) -> list[object]:
    with store.begin_read() as conn:
        return list(
            conn.execute(
                """
                SELECT run_id FROM scheduler_sequence_run_attempts
                WHERE sequence_id = ? AND ordinal = 3
                """,
                (FIXED_SEQUENCE_ID,),
            ).fetchall()
        )


def _cursor_effects(store: SqliteSchedulerStore, successor: str) -> list[object]:
    with store.begin_read() as conn:
        return list(
            conn.execute(
                """
                SELECT effect_id, status FROM scheduler_effects
                WHERE run_id = ? AND effect_kind = 'cursor.run_turn'
                """,
                (successor,),
            ).fetchall()
        )


def _corrupt_successor_effective_config(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    source_run_id: str,
    successor: str,
) -> None:
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, source_run_id)
    relative = source.context.effective_config.effective_config_artifact_path
    path = artifacts.run_root(successor) / relative
    path.write_bytes(b'{"corrupted": true}\n')
    os.chmod(path, 0o600)


def _assert_corrupt_bundle_rejected(
    tick: TickService,
    store: SqliteSchedulerStore,
    successor: str,
    source_run_id: str,
) -> None:
    receipt = tick.run_once()
    assert any(
        item.action == "sequence_cursor_publication_rejected" for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        replacement = conn.execute(
            "SELECT adopted_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        status = conn.execute(
            """
            SELECT status FROM scheduler_cursor_initial_recoveries
            WHERE successor_run_id = ?
            """,
            (successor,),
        ).fetchone()
        assert cursor_initial_recovery_blocks_dispatch(store, conn, successor) is True
    assert isinstance(sequence, BlockedSequenceState)
    assert sequence.current_run_id == source_run_id
    assert replacement["adopted_at"] is None
    assert str(status["status"]) == "pending"
    assert _attempts(store, successor) == 0
    assert _phase_three(store) == []


def _chmod_private(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            os.chmod(path, 0o600)
        elif path.is_dir():
            os.chmod(path, 0o700)


def _blocked_correction_source(
    tick: TickService,
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
) -> str:
    exhausted = None
    for _ in range(250):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            if isinstance(sequence, (ActiveSequenceState, BlockedSequenceState)):
                state, _, _ = store.load_validated_snapshot(conn, sequence.current_run_id)
                if sequence.current_ordinal == 2 and state.kind == "max_iterations_reached":
                    exhausted = sequence.current_run_id
                    break
    assert exhausted is not None
    ReviewBudgetExtendService(store, artifacts, now_factory=lambda: NOW).extend(
        exhausted,
        target_total=4,
    )
    for _ in range(80):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, exhausted)
        if isinstance(state, BlockedState) and state.block_reason_kind == "cursor_failure":
            _chmod_private(artifacts.artifact_root)
            return exhausted
    raise AssertionError("correction source did not reach cursor_failure")


def _pause_sequence_run_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event]:
    persisted = threading.Event()
    release = threading.Event()
    original = SequenceAbortService._advance_pending_run_abort

    def paused(self: SequenceAbortService, state: AbortPendingSequenceState) -> object:
        persisted.set()
        assert release.wait(timeout=30)
        return original(self, state)

    monkeypatch.setattr(SequenceAbortService, "_advance_pending_run_abort", paused)
    return persisted, release


def test_corrupt_effective_config_is_not_adopted_by_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_artifacts").force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    _corrupt_successor_effective_config(
        store,
        artifacts,
        source_run_id=blocked.current_run_id,
        successor=successor,
    )
    _assert_corrupt_bundle_rejected(tick, store, successor, blocked.current_run_id)


def test_corrupt_correction_effective_config_is_not_adopted_by_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(
        monkeypatch,
        "success,success,success,success,fail",
        "no_findings,findings,findings,findings",
    )
    tick = _started(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    source = _blocked_correction_source(tick, store, artifacts)
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_artifacts").force(source)
    successor = _successor_of(store, source)
    _corrupt_successor_effective_config(
        store,
        artifacts,
        source_run_id=source,
        successor=successor,
    )
    _assert_corrupt_bundle_rejected(tick, store, successor, source)


@pytest.mark.parametrize(
    ("window", "target"),
    [
        ("adoption", "sequence"),
        ("adoption", "successor"),
        ("readiness", "sequence"),
        ("readiness", "successor"),
    ],
)
def test_synchronized_abort_during_adoption_or_readiness(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    window: str,
    target: str,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    fault_point = "after_artifacts" if window == "adoption" else "after_sequence_adoption"
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point=fault_point).force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    persisted: threading.Event | None = None
    release: threading.Event | None = None
    if window == "readiness" and target == "sequence":
        persisted, release = _pause_sequence_run_abort(monkeypatch)
    holder: dict[str, threading.Thread] = {}
    abort_errors: list[BaseException] = []

    def before_commit() -> None:
        if target == "sequence":
            def abort() -> None:
                try:
                    SequenceAbortService(
                        store,
                        run_abort=tick._run_abort,
                        now_factory=lambda: NOW,
                    ).abort_sequence(FIXED_SEQUENCE_ID)
                except SchedulerEngineError as exc:
                    abort_errors.append(exc)

            thread = threading.Thread(target=abort)
            holder["thread"] = thread
            thread.start()
            if window == "readiness":
                assert persisted is not None
                assert persisted.wait(timeout=30)
            else:
                thread.join(timeout=30)
                assert not thread.is_alive()
                assert abort_errors == []
            return
        assert tick._run_abort is not None
        tick._run_abort.abort_run(successor)

    hook_name = "before_adoption_commit" if window == "adoption" else "before_ready_commit"
    with pytest.raises(SchedulerEngineError):
        _recovery(store, artifacts, **{hook_name: before_commit}).force(blocked.current_run_id)
    if window == "readiness" and target == "sequence":
        with store.begin_read() as conn:
            pending = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            status = conn.execute(
                """
                SELECT status FROM scheduler_cursor_initial_recoveries
                WHERE successor_run_id = ?
                """,
                (successor,),
            ).fetchone()
        assert isinstance(pending, AbortPendingSequenceState)
        assert str(status["status"]) == "pending"
        assert _cursor_effects(store, successor) == []
        assert release is not None
        release.set()
        holder["thread"].join(timeout=30)
        assert not holder["thread"].is_alive()
        assert abort_errors == []
    for _ in range(10):
        tick.run_once()
    assert _attempts(store, successor) == 0
    assert _phase_three(store) == []
    with store.begin_read() as conn:
        replacement = conn.execute(
            "SELECT adopted_at, cancelled_at FROM scheduler_sequence_cursor_replacements"
        ).fetchone()
        active = conn.execute(
            "SELECT run_id FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchall()
    if window == "adoption":
        assert replacement["adopted_at"] is None
    if target == "sequence":
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        assert isinstance(sequence, AbortedSequenceState)
        assert active == []
    else:
        assert tick._run_abort is not None
        SequenceAbortService(
            store,
            run_abort=tick._run_abort,
            now_factory=lambda: NOW,
        ).abort_sequence(FIXED_SEQUENCE_ID)
        tick.run_once()
        assert _phase_three(store) == []
        assert _attempts(store, successor) == 0


def test_competing_recovery_tick_and_reviewer_keep_one_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_sequence_intent").force(
            blocked.current_run_id
        )
    start = threading.Barrier(3)
    errors: list[BaseException] = []

    def run_force() -> None:
        start.wait(timeout=30)
        try:
            _recovery(store, artifacts).force(blocked.current_run_id)
        except SchedulerEngineError as exc:
            errors.append(exc)

    def run_tick() -> None:
        start.wait(timeout=30)
        try:
            tick.run_once()
        except SchedulerEngineError as exc:
            errors.append(exc)

    def run_review() -> None:
        start.wait(timeout=30)
        try:
            ReviewRetryService(store, artifacts, now_factory=lambda: NOW).retry(
                blocked.current_run_id
            )
        except SchedulerEngineError as exc:
            errors.append(exc)

    workers = [threading.Thread(target=target) for target in (run_force, run_tick, run_review)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=60)
        assert not worker.is_alive()
    assert errors
    assert all(isinstance(item, SchedulerEngineError) for item in errors)
    successor = _successor_of(store, blocked.current_run_id)
    for _ in range(40):
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
            ready = conn.execute(
                """
                SELECT status FROM scheduler_cursor_initial_recoveries
                WHERE successor_run_id = ?
                """,
                (successor,),
            ).fetchone()
        if (
            ready is not None
            and str(ready["status"]) == "ready"
            and isinstance(sequence, ActiveSequenceState)
            and sequence.current_run_id == successor
        ):
            break
        tick.run_once()
    else:
        raise AssertionError(type(sequence).__name__)
    _assert_one_leaf(store, successor)
    with store.begin_read() as conn:
        review_rows = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_execution_replacements"
        ).fetchall()
        cursor_rows = conn.execute(
            "SELECT successor_run_id FROM scheduler_sequence_cursor_replacements"
        ).fetchall()
    assert review_rows == []
    assert [str(row["successor_run_id"]) for row in cursor_rows] == [successor]
    tick.run_once()
    _assert_one_leaf(store, successor)


def _queued_effect_survives_sequence_abort(
    tick: TickService,
    store: SqliteSchedulerStore,
    successor: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    direct: bool,
) -> None:
    persisted, release = _pause_sequence_run_abort(monkeypatch)
    holder: dict[str, threading.Thread] = {}

    def before_claim() -> None:
        def abort() -> None:
            SequenceAbortService(
                store,
                run_abort=tick._run_abort,
                now_factory=lambda: NOW,
            ).abort_sequence(FIXED_SEQUENCE_ID)

        thread = threading.Thread(target=abort)
        holder["thread"] = thread
        thread.start()
        assert persisted.wait(timeout=30)

    assert tick._attempt_service is not None
    tick._attempt_service._before_effect_claim = before_claim
    if direct:
        owner = "phase23-4-direct-claim"
        with store.begin_immediate() as conn:
            lease = store.acquire_global_tick_lease(
                conn,
                owner_id=owner,
                now=NOW,
                ttl_seconds=300,
            )
        assert lease is not None
        receipt = tick._attempt_service.process_run(owner, lease[0], successor)
        assert receipt is not None
        assert receipt.action == "cursor_initial_recovery_publication_pending"
        with store.begin_immediate() as conn:
            store.release_global_tick_lease(
                conn,
                owner_id=owner,
                generation=lease[0],
                now=NOW,
            )
    else:
        tick.run_once()
    assert _attempts(store, successor) == 0
    assert tick._cursor_workflow is not None
    assert tick._cursor_workflow.process_run("phase23-4-direct-workflow", 1, successor) == []
    release.set()
    holder["thread"].join(timeout=30)
    assert not holder["thread"].is_alive()
    tick._attempt_service._before_effect_claim = None
    for _ in range(10):
        tick.run_once()
    assert _attempts(store, successor) == 0
    assert _phase_three(store) == []
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        active = conn.execute(
            "SELECT run_id FROM scheduler_repository_reservations WHERE status = 'active'"
        ).fetchall()
    assert isinstance(sequence, (AbortedSequenceState, AbortPendingSequenceState))
    if isinstance(sequence, AbortedSequenceState):
        assert active == []


def test_initial_queued_effect_cannot_launch_after_sequence_abort(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(monkeypatch, "success,fail,success", "no_findings")
    tick = _started(git_repo, scheduler_paths)
    blocked = _until_sequence(tick, BlockedSequenceState)
    assert isinstance(blocked, BlockedSequenceState)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    _recovery(store, artifacts).force(blocked.current_run_id)
    successor = _successor_of(store, blocked.current_run_id)
    assert _cursor_effects(store, successor)
    _queued_effect_survives_sequence_abort(
        tick,
        store,
        successor,
        monkeypatch,
        direct=False,
    )


def test_correction_readiness_and_direct_claim_see_sequence_abort(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(
        monkeypatch,
        "success,success,success,success,fail,success",
        "no_findings,findings,findings,findings,no_findings",
    )
    tick = _started(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    source = _blocked_correction_source(tick, store, artifacts)
    with pytest.raises(CursorInitialRecoveryFault):
        _recovery(store, artifacts, fault_point="after_sequence_adoption").force(source)
    successor = _successor_of(store, source)
    persisted, release = _pause_sequence_run_abort(monkeypatch)
    holder: dict[str, threading.Thread] = {}

    def before_ready() -> None:
        def abort() -> None:
            SequenceAbortService(
                store,
                run_abort=tick._run_abort,
                now_factory=lambda: NOW,
            ).abort_sequence(FIXED_SEQUENCE_ID)

        thread = threading.Thread(target=abort)
        holder["thread"] = thread
        thread.start()
        assert persisted.wait(timeout=30)

    with pytest.raises(SchedulerEngineError):
        _recovery(store, artifacts, before_ready_commit=before_ready).force(source)
    with store.begin_read() as conn:
        pending = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        status = conn.execute(
            """
            SELECT status FROM scheduler_cursor_initial_recoveries
            WHERE successor_run_id = ?
            """,
            (successor,),
        ).fetchone()
    assert isinstance(pending, AbortPendingSequenceState)
    assert str(status["status"]) == "pending"
    assert _cursor_effects(store, successor) == []
    release.set()
    holder["thread"].join(timeout=30)
    assert not holder["thread"].is_alive()
    for _ in range(10):
        tick.run_once()
    assert _attempts(store, successor) == 0
    assert _phase_three(store) == []


def test_correction_direct_claim_cannot_launch_after_sequence_abort(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _arm(
        monkeypatch,
        "success,success,success,success,fail",
        "no_findings,findings,findings,findings",
    )
    tick = _started(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    source = _blocked_correction_source(tick, store, artifacts)
    published = _recovery(store, artifacts).force(source)
    assert _cursor_effects(store, published.successor_run_id)
    _queued_effect_survives_sequence_abort(
        tick,
        store,
        published.successor_run_id,
        monkeypatch,
        direct=True,
    )
