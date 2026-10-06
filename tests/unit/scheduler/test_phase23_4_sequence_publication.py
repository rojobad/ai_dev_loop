"""Phase 23.4 cursor sequence intent persistence without read-only migration."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.domain.cursor_sequence_replacement import (
    CursorSequenceReplacementIntent,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

DIGEST = "b" * 64
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _intent(*, adopted: bool) -> CursorSequenceReplacementIntent:
    return CursorSequenceReplacementIntent(
        sequence_id="seq-pub",
        sequence_version=3,
        ordinal=2,
        source_run_id="run-source",
        source_generation=1,
        successor_run_id="run-successor",
        successor_generation=2,
        recovery_key=DIGEST,
        record_digest=DIGEST if adopted else None,
        entry_hash=DIGEST,
        worktree_key="worktree-pub",
        repository_root="/tmp/repo-pub",
        reservation_owner_run_id="run-successor",
        adoption_state="adopted" if adopted else "pending",
    )


def test_pending_intent_round_trip_and_readonly_inspection_does_not_migrate(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    store.bootstrap()
    intent = _intent(adopted=False)
    payload = intent.canonical_bytes().decode("utf-8")
    with store.begin_immediate() as conn:
        assert store.insert_sequence_cursor_replacement(
            conn,
            intent=intent,
            intent_payload=payload,
            intent_digest=hashlib.sha256(payload.encode()).hexdigest(),
            now=NOW,
        )
        version_before = int(conn.execute("PRAGMA user_version").fetchone()[0])
    readonly = SqliteSchedulerStore.open_readonly(db)
    with readonly.begin_read() as conn:
        row = store.get_sequence_cursor_replacement(
            conn,
            source_run_id="run-source",
            recovery_key=DIGEST,
        )
        version_after = int(conn.execute("PRAGMA user_version").fetchone()[0])
        attempts = conn.execute("SELECT COUNT(*) FROM scheduler_sequence_run_attempts").fetchone()
    assert row is not None
    assert row["adopted_at"] is None
    assert json.loads(str(row["intent_payload"]))["record_digest"] is None
    assert version_after == version_before == 13
    assert int(attempts[0]) == 0


def test_generation_claim_is_unique(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    store.bootstrap()
    first = _intent(adopted=False)
    payload = first.canonical_bytes().decode("utf-8")
    other = first.model_copy(
        update={
            "recovery_key": "c" * 64,
            "successor_run_id": "run-other",
            "reservation_owner_run_id": "run-other",
        }
    )
    other_payload = other.canonical_bytes().decode("utf-8")
    with store.begin_immediate() as conn:
        assert store.insert_sequence_cursor_replacement(
            conn,
            intent=first,
            intent_payload=payload,
            intent_digest=hashlib.sha256(payload.encode()).hexdigest(),
            now=NOW,
        )
    import sqlite3

    rejected = False
    try:
        with store.begin_immediate() as conn:
            store.insert_sequence_cursor_replacement(
                conn,
                intent=other,
                intent_payload=other_payload,
                intent_digest=hashlib.sha256(other_payload.encode()).hexdigest(),
                now=NOW,
            )
    except sqlite3.IntegrityError:
        rejected = True
    assert rejected
