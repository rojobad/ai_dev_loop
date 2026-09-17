"""Authoritative read/validate helpers for sequence execution replacement intents."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_execution_replacement import (
    SEQUENCE_RECOVERY_PUBLICATION_POSTCONDITION,
    SequenceExecutionReplacementIntent,
)
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@dataclass(frozen=True)
class ValidatedSequenceExecutionReplacementIntent:
    intent: SequenceExecutionReplacementIntent
    published_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime

    @property
    def is_published(self) -> bool:
        return self.published_at is not None

    @property
    def is_cancelled(self) -> bool:
        return self.cancelled_at is not None


def load_validated_sequence_execution_replacement_intent(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    recovery_key: str,
) -> ValidatedSequenceExecutionReplacementIntent | None:
    row = store.get_sequence_execution_replacement_intent(
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    )
    if row is None:
        return None
    payload_text = str(row["intent_payload"])
    expected_digest = str(row["intent_payload_sha256"])
    if payload_sha256(payload_text) != expected_digest:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent payload digest mismatch",
        )
    try:
        parsed = SequenceExecutionReplacementIntent.model_validate_json(payload_text)
    except Exception as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent payload is invalid",
        ) from exc
    _cross_check_intent_row(row, parsed)
    published_raw = row["published_at"]
    published_at = None
    if published_raw is not None:
        published_at = datetime.fromisoformat(str(published_raw).replace("Z", "+00:00"))
    cancelled_raw = row["cancelled_at"]
    cancelled_at = None
    if cancelled_raw is not None:
        cancelled_at = datetime.fromisoformat(str(cancelled_raw).replace("Z", "+00:00"))
    created_at = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    return ValidatedSequenceExecutionReplacementIntent(
        intent=parsed,
        published_at=published_at,
        cancelled_at=cancelled_at,
        created_at=created_at,
    )


def _cross_check_intent_row(row: sqlite3.Row, intent: SequenceExecutionReplacementIntent) -> None:
    if str(row["sequence_id"]) != intent.sequence_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent sequence_id disagrees with payload",
        )
    if int(row["ordinal"]) != intent.ordinal:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent ordinal disagrees with payload",
        )
    if str(row["source_run_id"]) != intent.source_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent source_run_id disagrees with payload",
        )
    if int(row["source_generation"]) != intent.source_generation:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent source_generation disagrees with payload",
        )
    if str(row["successor_run_id"]) != intent.successor_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent successor_run_id disagrees with payload",
        )
    if int(row["successor_generation"]) != intent.successor_generation:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent successor_generation disagrees with payload",
        )
    if str(row["recovery_key"]) != intent.recovery_key:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent recovery_key disagrees with payload",
        )
    if intent.publication_postcondition != SEQUENCE_RECOVERY_PUBLICATION_POSTCONDITION:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence replacement intent publication postcondition is unsupported",
        )


def authenticate_durable_sequence_replacement_publication(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    validated: ValidatedSequenceExecutionReplacementIntent,
) -> bool:
    if not validated.is_published or validated.is_cancelled:
        return False
    intent = validated.intent
    recovery_row = store.get_review_recovery_successor(
        conn,
        source_run_id=intent.source_run_id,
        recovery_key=intent.recovery_key,
    )
    if recovery_row is None:
        return False
    if str(recovery_row["successor_run_id"]) != intent.successor_run_id:
        return False
    successor_row = conn.execute(
        "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
        (intent.successor_run_id,),
    ).fetchone()
    if successor_row is None:
        return False
    try:
        lineage = load_sequence_run_lineage(conn, intent.sequence_id)
    except Exception:
        return False
    for phase in lineage.phase_executions:
        if phase.ordinal != intent.ordinal:
            continue
        for attempt in phase.attempts:
            if (
                attempt.generation == intent.successor_generation
                and attempt.run_id == intent.successor_run_id
            ):
                return True
    return False


def sequence_recovery_publication_complete(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    validated: ValidatedSequenceExecutionReplacementIntent,
) -> bool:
    return authenticate_durable_sequence_replacement_publication(
        store,
        conn,
        validated=validated,
    )


def find_validated_intent_for_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    successor_run_id: str,
) -> ValidatedSequenceExecutionReplacementIntent | None:
    row = store.get_sequence_execution_replacement_intent_by_successor(
        conn,
        successor_run_id=successor_run_id,
    )
    if row is None:
        return None
    return load_validated_sequence_execution_replacement_intent(
        store,
        conn,
        source_run_id=str(row["source_run_id"]),
        recovery_key=str(row["recovery_key"]),
    )


def assert_sequence_blocked_for_recovery_source(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    source_run_id: str,
) -> BlockedSequenceState:
    sequence_state = store.load_validated_sequence_state(conn, sequence_id)
    if isinstance(sequence_state, BlockedSequenceState):
        if sequence_state.current_run_id != source_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "source run is not the current leaf of the blocked sequence",
            )
        return sequence_state
    if isinstance(sequence_state, ActiveSequenceState):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence is still active; reconcile the blocked run before review recovery",
        )
    if isinstance(sequence_state, AbortPendingSequenceState):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence abort is pending; review recovery is not allowed",
        )
    if isinstance(sequence_state, AbortedSequenceState):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "aborted sequence cannot authorize review recovery",
        )
    raise SchedulerEngineError(
        SchedulerEngineErrorKind.VALIDATION,
        "sequence is not blocked for review recovery",
    )
