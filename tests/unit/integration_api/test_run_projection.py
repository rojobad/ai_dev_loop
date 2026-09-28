"""Unit tests for integration run projection helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.unit.scheduler.helpers import sample_submitted_state

from ai_dev_loop.integration_api.run_projection import (
    build_attempt_item,
    collection_page,
)
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def test_collection_page_next_offset_when_has_more() -> None:
    page = collection_page(0, 2, True)
    assert page.has_more is True
    assert page.next_offset == 2


def test_phase_attempt_stable_across_pages(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="projection-run")
    base = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
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
        for index in range(3):
            dispatch_id = f"dispatch-{index:02d}"
            created = base + timedelta(seconds=index)
            created_text = created.isoformat().replace("+00:00", "Z")
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, 'evt-submit', ?, ?, ?, ?, 'cursor.turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    index + 1,
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
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, 'cursor', 1, 'completed', ?, ?, ?, ?)
                """,
                (
                    f"attempt-{index:02d}",
                    state.run_id,
                    dispatch_id,
                    created_text,
                    created_text,
                    created_text,
                    created_text,
                ),
            )

    with store.begin_read() as conn:
        first_page, more = store.list_integration_attempt_rows(
            conn,
            state.run_id,
            offset=0,
            limit=2,
        )
        second_page, _ = store.list_integration_attempt_rows(
            conn,
            state.run_id,
            offset=2,
            limit=2,
        )
    assert more is True
    first_items = [build_attempt_item(row) for row in first_page]
    second_items = [build_attempt_item(row) for row in second_page]
    assert [item.phase_attempt for item in first_items] == [1, 2]
    assert second_items[0].phase_attempt == 3
