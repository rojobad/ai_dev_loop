"""Successor termination gating tests for Phase 20.6.5 rollover abort."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.scheduler.helpers import sample_agent_led_submitted_context

from ai_dev_loop.scheduler.application.abort_reconcile import (
    successor_abort_ownership_release_allowed,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256, worktree_key
from ai_dev_loop.scheduler.domain.state import AbortedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _insert_aborted_run_with_active_attempt(store: SqliteSchedulerStore, run_id: str) -> str:
    now_text = datetime(2026, 9, 15, 18, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    attempt_id = "att-" + "a" * 32
    dispatch_id = "dispatch-abort-ownership"
    event_id = "evt-" + "b" * 32
    context = sample_agent_led_submitted_context()
    aborted = AbortedState(
        run_id=run_id,
        version=1,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key="0" * 64,
        context=context,
        aborted_at=now_text,
        abort_reason="test_setup",
        prior_state_kind="running_cursor",
    )
    payload = aborted.model_dump_json()
    payload_sha = payload_sha256(payload)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, 'aborted', ?, ?, 1, ?, ?, ?, ?)
            """,
            (
                run_id,
                payload,
                payload_sha,
                "0" * 64,
                worktree_key(str(context.repository.root)),
                now_text,
                now_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_events(
                event_id, run_id, sequence, event_kind, event_payload,
                event_payload_sha256, created_at
            ) VALUES (?, ?, 1, 'run_aborted', '{}', ?, ?)
            """,
            (event_id, run_id, "0" * 64, now_text),
        )
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, created_at, updated_at
            ) VALUES (?, ?, 0, ?, ?, ?, 'run_cursor_turn', '{}', ?, ?, ?, ?, ?)
            """,
            (
                dispatch_id,
                event_id,
                run_id,
                "eff-" + "c" * 32,
                "0" * 64,
                "0" * 64,
                "succeeded",
                now_text,
                now_text,
                now_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'cursor', 1, 'active', ?, ?)
            """,
            (attempt_id, run_id, dispatch_id, now_text, now_text),
        )
    return attempt_id


def test_successor_abort_ownership_release_requires_conclusive_attempt_state(
    tmp_path,
) -> None:
    db = tmp_path / "abort-ownership.sqlite3"
    store = SqliteSchedulerStore(db)
    run_id = "run-successor-abort"
    attempt_id = _insert_aborted_run_with_active_attempt(store, run_id)
    now_text = datetime(2026, 9, 15, 18, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_read() as conn:
        assert not successor_abort_ownership_release_allowed(
            store,
            conn,
            successor_run_id=run_id,
        )
    with store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_attempts SET status = 'cancelled', updated_at = ? WHERE attempt_id = ?",
            (now_text, attempt_id),
        )
    with store.begin_read() as conn:
        assert successor_abort_ownership_release_allowed(
            store,
            conn,
            successor_run_id=run_id,
        )
