"""Review service attempt ownership and kind resolution (F-06)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.review_service import IntegrationReviewReadService
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def test_f06_owned_cursor_attempt_is_invalid_argument(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="run-f06")
    base = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    created_text = base.isoformat().replace("+00:00", "Z")
    attempt_id = "attempt-cursor-f06"
    dispatch_id = "dispatch-cursor-f06"
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=RunSubmittedEvent(
                run_id=state.run_id,
                idempotency_key=state.idempotency_key,
                worktree_key=state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=base,
        )
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (?, 'evt-submit', 1, ?, ?, ?, 'cursor.run_turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
            """,
            (
                dispatch_id,
                state.run_id,
                dispatch_id,
                f"{state.run_id}:{dispatch_id}",
                "d" * 64,
                created_text,
                created_text,
                created_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'cursor', 1, 'completed', ?, ?)
            """,
            (attempt_id, state.run_id, dispatch_id, created_text, created_text),
        )
    service = IntegrationReviewReadService(store, artifact_root=tmp_path / "artifacts")
    with pytest.raises(IntegrationApiError) as exc:
        service.inspect_review(state.run_id, attempt_id=attempt_id)
    assert exc.value.code == IntegrationErrorCode.INVALID_ARGUMENT


def test_f06_missing_attempt_is_not_found(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="run-missing")
    base = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=RunSubmittedEvent(
                run_id=state.run_id,
                idempotency_key=state.idempotency_key,
                worktree_key=state.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=base,
        )
    service = IntegrationReviewReadService(store, artifact_root=tmp_path / "artifacts")
    with pytest.raises(IntegrationApiError) as exc:
        service.inspect_review(state.run_id, attempt_id="attempt-does-not-exist")
    assert exc.value.code == IntegrationErrorCode.NOT_FOUND
