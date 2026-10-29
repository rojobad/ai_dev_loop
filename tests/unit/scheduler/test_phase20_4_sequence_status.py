"""Unit tests for Phase 20.4 enriched sequence status projections."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service

from ai_dev_loop.commands.scheduler import render_sequence_status_output
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.sequence_service import IntegrationSequenceReadService
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.events import (
    SEQUENCE_CHECKPOINT_COMMITTED_EVENT_KIND,
    SequenceCheckpointCommittedEvent,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ABORTED_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import SequenceRunAttempt
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    insert_sequence_run_attempt,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_sequence_status_json_includes_aggregate_counts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    status = SequenceStatusService(store, artifacts=artifacts).get_status(sequence_id)
    rendered = render_sequence_status_output(status, output="json")
    payload = json.loads(rendered)
    assert payload["aggregate_counts"]["planned"] == 2
    assert payload["aggregate_counts"]["materialized"] == 1
    assert payload["aggregate_counts"]["remaining"] == 1
    assert "prompt" not in rendered.lower()
    assert "review_markdown" not in rendered


def test_aborted_sequence_status_marks_cancelled_entries(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    SequenceAbortService(store, now_factory=lambda: FIXED_NOW).abort_sequence(sequence_id)
    status = SequenceStatusService(
        SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"]),
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
    ).get_status(sequence_id)
    assert status.state_kind == ABORTED_SEQUENCE_STATE_KIND
    assert all(entry.cancelled for entry in status.entries)
    assert status.aggregate_counts is not None
    assert status.aggregate_counts.cancelled == 2


def test_f04_checkpoint_count_ignores_other_sequences_and_keeps_replacements(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    selected_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(selected_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    successor_id, source_id = _checkpoint_replaced_source(store, selected_id)
    _insert_unrelated_invalid_checkpoint_event(store, selected_id)
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    status_service = SequenceStatusService(store, artifacts=artifacts)
    reader = IntegrationSequenceReadService(
        store,
        artifact_root=scheduler_paths["artifact_root"],
    )

    status = status_service.get_status(selected_id)
    assert status.aggregate_counts is not None
    assert status.aggregate_counts.checkpointed == 1
    assert status.entries[0].materialized_run_id_prefix == successor_id[:8]
    with store.begin_read() as conn:
        successor_events = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id = ? AND event_kind = ?
            """,
            (successor_id, SEQUENCE_CHECKPOINT_COMMITTED_EVENT_KIND),
        ).fetchone()
    assert successor_events is not None
    assert int(successor_events[0]) == 0
    inspected = reader.inspect_sequence(selected_id)
    assert inspected.aggregate_counts.checkpointed == 1

    _rebind_checkpoint_sequence(store, source_id, str(uuid4()))
    with pytest.raises(SchedulerEngineError) as binding_error:
        status_service.get_status(selected_id)
    assert binding_error.value.kind is SchedulerEngineErrorKind.VALIDATION
    assert "sequence binding mismatch" in str(binding_error.value)
    with pytest.raises(IntegrationApiError) as rebound_inspect:
        reader.inspect_sequence(selected_id)
    assert rebound_inspect.value.code is IntegrationErrorCode.INVALID_ARGUMENT

    _corrupt_member_checkpoint_payload(store, source_id)
    with pytest.raises(SchedulerEngineError) as payload_error:
        status_service.get_status(selected_id)
    assert payload_error.value.kind is SchedulerEngineErrorKind.VALIDATION
    assert "payload is invalid" in str(payload_error.value)
    with pytest.raises(IntegrationApiError) as corrupt_inspect:
        reader.inspect_sequence(selected_id)
    assert corrupt_inspect.value.code is IntegrationErrorCode.INVALID_ARGUMENT


def _checkpoint_replaced_source(store: SqliteSchedulerStore, sequence_id: str) -> tuple[str, str]:
    """Checkpoint the replaced source while the current leaf is the successor.

    The active sequence's materialized run is the unresolved successor. The
    checkpoint event stays on the blocked source, so the count is 1 only when
    replacement lineage remains in the membership.
    """

    with store.begin_immediate() as conn:
        state = store.load_validated_sequence_state(conn, sequence_id, validate_lineage=False)
        assert isinstance(state, ActiveSequenceState)
        entry = state.materialized_entries[0]
        planned_id = entry.run_id
        materialized_at = entry.materialized_at
        source = conn.execute(
            "SELECT * FROM scheduler_runs WHERE run_id = ?",
            (planned_id,),
        ).fetchone()
        assert source is not None
        successor_id = str(uuid4())
        cloned = store.load_state(str(source["state_payload"])).model_copy(
            update={"run_id": successor_id}
        )
        kind, payload, digest = store.dump_state(cloned)
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                successor_id,
                kind,
                payload,
                digest,
                cloned.version,
                str(uuid4()),
                source["worktree_key"],
                source["created_at"],
                source["updated_at"],
            ),
        )
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET terminal_outcome = 'blocked', resolved_at = ?
            WHERE sequence_id = ? AND ordinal = 1 AND generation = 1
            """,
            (materialized_at, sequence_id),
        )
        insert_sequence_run_attempt(
            conn,
            sequence_id=sequence_id,
            ordinal=1,
            planned_run_id=planned_id,
            attempt=SequenceRunAttempt(
                schema_version=2,
                generation=2,
                run_id=successor_id,
                source_run_id=planned_id,
                attempt_kind="implementer_retry",
                materialized_at=materialized_at,
                terminal_outcome=None,
                resolved_at=None,
            ),
        )
        updated = state.model_copy(
            update={
                "version": state.version + 1,
                "current_run_id": successor_id,
                "materialized_entries": (entry.model_copy(update={"run_id": successor_id}),),
            }
        )
        sequence_kind, sequence_payload, sequence_digest = store.dump_sequence_state(updated)
        replaced = conn.execute(
            """
            UPDATE scheduler_sequences
            SET state_kind = ?, payload = ?, payload_sha256 = ?, version = ?
            WHERE sequence_id = ? AND version = ?
            """,
            (
                sequence_kind,
                sequence_payload,
                sequence_digest,
                updated.version,
                sequence_id,
                state.version,
            ),
        )
        assert replaced.rowcount == 1
        store.append_event(
            conn,
            event_id=str(uuid4()),
            run_id=planned_id,
            sequence=store.next_event_sequence(conn, planned_id),
            event=SequenceCheckpointCommittedEvent(
                run_id=planned_id,
                sequence_id=sequence_id,
                commit_sha256_prefix="a" * 12,
                tree_sha256_prefix="b" * 12,
            ),
            now=FIXED_NOW,
        )
    return successor_id, planned_id


def _insert_unrelated_invalid_checkpoint_event(
    store: SqliteSchedulerStore,
    sequence_id: str,
) -> None:
    """Store a damaged checkpoint on a run outside this sequence's lineage."""

    with store.begin_immediate() as conn:
        planned = conn.execute(
            """
            SELECT run_id FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ? AND generation = 1
            """,
            (sequence_id,),
        ).fetchone()
        assert planned is not None
        source = conn.execute(
            "SELECT * FROM scheduler_runs WHERE run_id = ?",
            (str(planned["run_id"]),),
        ).fetchone()
        assert source is not None
        unrelated_id = str(uuid4())
        cloned = store.load_state(str(source["state_payload"])).model_copy(
            update={"run_id": unrelated_id}
        )
        kind, payload, digest = store.dump_state(cloned)
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                unrelated_id,
                kind,
                payload,
                digest,
                cloned.version,
                str(uuid4()),
                source["worktree_key"],
                source["created_at"],
                source["updated_at"],
            ),
        )
        damaged = "{"
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind,
                event_payload, event_payload_sha256, created_at
            ) VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                unrelated_id,
                SEQUENCE_CHECKPOINT_COMMITTED_EVENT_KIND,
                damaged,
                payload_sha256("not-the-payload"),
                "2026-10-14T12:00:00.000000Z",
            ),
        )


def _corrupt_member_checkpoint_payload(store: SqliteSchedulerStore, run_id: str) -> None:
    damaged = "{"
    with store.begin_immediate() as conn:
        updated = conn.execute(
            """
            UPDATE scheduler_events
            SET event_payload = ?, event_payload_sha256 = ?
            WHERE run_id = ? AND event_kind = ?
            """,
            (
                damaged,
                payload_sha256(damaged),
                run_id,
                SEQUENCE_CHECKPOINT_COMMITTED_EVENT_KIND,
            ),
        )
        assert updated.rowcount == 1


def _rebind_checkpoint_sequence(
    store: SqliteSchedulerStore,
    run_id: str,
    other_sequence_id: str,
) -> None:
    with store.begin_immediate() as conn:
        _kind, payload, digest = store.dump_event(
            SequenceCheckpointCommittedEvent(
                run_id=run_id,
                sequence_id=other_sequence_id,
                commit_sha256_prefix="a" * 12,
                tree_sha256_prefix="b" * 12,
            )
        )
        updated = conn.execute(
            """
            UPDATE scheduler_events
            SET event_payload = ?, event_payload_sha256 = ?
            WHERE run_id = ? AND event_kind = ?
            """,
            (payload, digest, run_id, SEQUENCE_CHECKPOINT_COMMITTED_EVENT_KIND),
        )
        assert updated.rowcount == 1
