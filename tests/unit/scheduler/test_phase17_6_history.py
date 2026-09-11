"""Unit tests for redacted scheduler history projection."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.unit.scheduler.helpers import REVIEWER_SESSION
from tests.unit.scheduler.test_tick import _bootstrap_run

from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.domain.events import CursorChatCreatedEvent


def test_history_redacts_sensitive_event_payloads(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        sequence = store.next_event_sequence(conn, run_id)
        store.append_event(
            conn,
            event_id="evt-chat",
            run_id=run_id,
            sequence=sequence,
            event=CursorChatCreatedEvent(
                run_id=run_id,
                chat_id=REVIEWER_SESSION,
                chat_artifact_path="prompts/secret.txt",
                chat_artifact_sha256="a" * 64,
            ),
            now=now,
        )
    result = scheduler_history(run_id, db_path=store.db_path, order="newest", limit=10)
    rendered = "\n".join(entry.safe_detail for entry in result.entries)
    assert REVIEWER_SESSION not in rendered
    assert "prompts/secret.txt" not in rendered
    assert "sensitive fields redacted" in rendered
