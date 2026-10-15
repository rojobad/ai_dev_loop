"""Adopt one Codex implementer recovery successor at its current sequence leaf."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import cast

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.domain.sequence_implementer_replacement import (
    SequenceImplementerReplacementIntent,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_IMPLEMENTER_RETRY,
    SequenceAttemptKind,
    SequenceRunAttempt,
)
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    SequenceExecutionCASExpectation,
    _current_leaf_run_id,
    _max_generation,
    compare_and_swap_sequence_execution_leaf,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _conflict(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CONFLICT, message)


def _corrupt(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, message)


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def authenticated_sequence_implementer_intent(
    row: sqlite3.Row,
) -> SequenceImplementerReplacementIntent:
    payload = str(row["intent_payload"]).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != str(row["intent_payload_sha256"]):
        raise _corrupt("sequence implementer replacement intent digest mismatch")
    try:
        intent = SequenceImplementerReplacementIntent.model_validate_json(payload)
    except ValueError as exc:
        raise _corrupt("sequence implementer replacement intent failed authentication") from exc
    if intent.canonical_bytes() != payload:
        raise _corrupt("sequence implementer replacement intent is not canonical")
    expected = (
        intent.sequence_id,
        intent.ordinal,
        intent.source_run_id,
        intent.source_generation,
        intent.recovery_key,
        intent.successor_run_id,
        intent.successor_generation,
        intent.provider,
    )
    actual = (
        str(row["sequence_id"]),
        int(row["ordinal"]),
        str(row["source_run_id"]),
        int(row["source_generation"]),
        str(row["recovery_key"]),
        str(row["successor_run_id"]),
        int(row["successor_generation"]),
        str(row["provider"]),
    )
    if expected != actual:
        raise _corrupt("sequence implementer replacement disagrees with the ledger row")
    adopted = row["adopted_at"] is not None
    cancelled = row["cancelled_at"] is not None
    if adopted and cancelled:
        raise _corrupt("sequence implementer replacement cannot be adopted and cancelled")
    if adopted != (intent.adoption_state == "adopted"):
        raise _corrupt("sequence implementer adoption state disagrees with the ledger")
    return intent


def authenticate_adopted_sequence_implementer_authority(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    record_digest: str,
    require_current_leaf: bool,
) -> bool:
    """Authenticate an adopted replacement and report current dispatch authority.

    Historical callers authenticate the immutable adoption without requiring the
    old leaf to remain active. Dispatch callers additionally require the exact
    successor generation to still be the active sequence leaf and free of a
    durable abort request.
    """

    row = store.get_sequence_implementer_replacement_by_successor(conn, successor_run_id)
    if row is None:
        raise _corrupt("sequence implementer replacement authority is missing")
    intent = authenticated_sequence_implementer_intent(row)
    if (
        intent.source_run_id != source_run_id
        or intent.successor_run_id != successor_run_id
        or intent.recovery_key != recovery_key
        or intent.record_digest != record_digest
        or intent.adoption_state != "adopted"
        or row["adopted_at"] is None
        or row["cancelled_at"] is not None
    ):
        raise _corrupt("sequence implementer replacement authority is invalid")

    source, _, _ = store.load_validated_snapshot(conn, source_run_id)
    successor, _, _ = store.load_validated_snapshot(conn, successor_run_id)
    source_binding = source.context.sequence
    successor_binding = successor.context.sequence
    expected_binding = (intent.sequence_id, intent.ordinal, intent.entry_hash)
    if source_binding is None or successor_binding is None:
        raise _corrupt("sequence implementer replacement lost its run binding")
    if (
        (
            source_binding.sequence_id,
            source_binding.ordinal,
            source_binding.entry_hash,
        )
        != expected_binding
        or (
            successor_binding.sequence_id,
            successor_binding.ordinal,
            successor_binding.entry_hash,
        )
        != expected_binding
        or source.context != successor.context
        or source.context.repository.worktree_key != intent.worktree_key
        or source.context.repository.root != intent.repository_root
        or intent.reservation_owner_run_id != successor_run_id
    ):
        raise _corrupt("sequence implementer replacement changed frozen run authority")

    sequence = store.load_validated_sequence_state(conn, intent.sequence_id)
    if intent.ordinal > len(sequence.definition.entries):
        raise _corrupt("sequence implementer replacement ordinal exceeds its definition")
    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    frozen = sequence.definition.entries[intent.ordinal - 1]
    if frozen_entry_hash(frozen) != intent.entry_hash:
        raise _corrupt("sequence implementer replacement changed its frozen entry")
    materialized_entries = getattr(sequence, "materialized_entries", ())
    materialized = next(
        (entry for entry in materialized_entries if entry.ordinal == intent.ordinal),
        None,
    )
    if materialized is None or materialized.entry_hash != intent.entry_hash:
        raise _corrupt("sequence implementer replacement lost its materialized entry")

    lineage_rows = conn.execute(
        """
        SELECT generation, run_id, source_run_id, attempt_kind
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ?
          AND generation IN (?, ?)
        ORDER BY generation
        """,
        (
            intent.sequence_id,
            intent.ordinal,
            intent.source_generation,
            intent.successor_generation,
        ),
    ).fetchall()
    if len(lineage_rows) != 2:
        raise _corrupt("sequence implementer replacement lineage is incomplete")
    source_row, successor_row = lineage_rows
    if (
        int(source_row["generation"]) != intent.source_generation
        or str(source_row["run_id"]) != source_run_id
        or int(successor_row["generation"]) != intent.successor_generation
        or str(successor_row["run_id"]) != successor_run_id
        or str(successor_row["source_run_id"] or "") != source_run_id
        or str(successor_row["attempt_kind"]) != SEQUENCE_ATTEMPT_KIND_IMPLEMENTER_RETRY
    ):
        raise _corrupt("sequence implementer replacement lineage disagrees with its intent")

    if not require_current_leaf:
        return True
    if (
        not isinstance(sequence, ActiveSequenceState)
        or sequence.current_ordinal != intent.ordinal
        or sequence.current_run_id != successor_run_id
        or materialized.run_id != successor_run_id
        or _current_leaf_run_id(
            conn,
            sequence_id=intent.sequence_id,
            ordinal=intent.ordinal,
        )
        != successor_run_id
        or _max_generation(
            conn,
            sequence_id=intent.sequence_id,
            ordinal=intent.ordinal,
        )
        != intent.successor_generation
    ):
        return False
    reservation = store.get_reservation_for_run(conn, successor_run_id)
    if (
        reservation is None
        or str(reservation["worktree_key"]) != intent.worktree_key
        or str(reservation["repository_root"]) != intent.repository_root
    ):
        raise _corrupt("active sequence implementer replacement lost its reservation")
    return not (
        store.has_abort_requested_for_run(conn, successor_run_id)
        or store.has_sequence_abort_requested_for_run(
            conn,
            run_id=successor_run_id,
            sequence_id=intent.sequence_id,
        )
        or store.has_sequence_abort_requested_for_run(
            conn,
            run_id=source_run_id,
            sequence_id=intent.sequence_id,
        )
    )


def _assert_no_competing_claim(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    source_run_id: str,
    generation: int,
) -> None:
    if store.schema_supports_sequence_review_recovery(conn):
        for review_row in store.list_sequence_execution_replacement_intents_for_source(
            conn, source_run_id=source_run_id
        ):
            if (
                review_row["cancelled_at"] is None
                and int(review_row["ordinal"]) == ordinal
                and int(review_row["source_generation"]) == generation
            ):
                raise _conflict("sequence leaf already has a review replacement claim")
    if store.schema_supports_cursor_sequence_replacement(conn):
        cursor_row = store.get_sequence_cursor_replacement_for_generation(
            conn,
            sequence_id=sequence_id,
            ordinal=ordinal,
            source_generation=generation,
        )
        if cursor_row is not None and cursor_row["cancelled_at"] is None:
            raise _conflict("sequence leaf already has a cursor replacement claim")
    implementer_row = store.get_sequence_implementer_replacement_for_generation(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        source_generation=generation,
    )
    if implementer_row is not None and implementer_row["cancelled_at"] is None:
        raise _conflict("sequence leaf already has an implementer replacement claim")


def bind_pending_sequence_implementer_intent(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    now: datetime,
) -> bool:
    """Bind a pending current-leaf claim. Return False for standalone recovery."""

    source, _, _ = store.load_validated_snapshot(conn, source_run_id)
    binding = source.context.sequence
    if binding is None:
        return False
    if not isinstance(source, BlockedState) or source.block_reason_kind != (
        "codex_implementer_failure"
    ):
        raise _conflict("sequence implementer recovery requires a blocked Codex failure")
    from ai_dev_loop.scheduler.application.sequence_review_recovery import (
        find_blocked_sequence_for_run,
    )

    blocked = find_blocked_sequence_for_run(store, conn, source_run_id)
    if blocked is None:
        raise _invalid("source run is not the current blocked sequence leaf")
    if blocked.sequence_id != binding.sequence_id or blocked.current_ordinal != binding.ordinal:
        raise _invalid("sequence binding disagrees with the blocked sequence leaf")
    if store.has_abort_requested_for_run(conn, source_run_id) or (
        store.has_sequence_abort_requested_for_run(
            conn, run_id=source_run_id, sequence_id=blocked.sequence_id
        )
    ):
        raise _conflict("sequence abort was requested before implementer recovery")
    leaf = _current_leaf_run_id(
        conn, sequence_id=blocked.sequence_id, ordinal=blocked.current_ordinal
    )
    generation = _max_generation(
        conn, sequence_id=blocked.sequence_id, ordinal=blocked.current_ordinal
    )
    if leaf != source_run_id or generation < 1:
        raise _invalid("source run is not the authenticated current sequence execution leaf")
    entry = next(
        (item for item in blocked.materialized_entries if item.ordinal == blocked.current_ordinal),
        None,
    )
    if entry is None or entry.run_id != source_run_id or entry.entry_hash != binding.entry_hash:
        raise _invalid("blocked sequence entry disagrees with the frozen run binding")
    existing = store.get_sequence_implementer_replacement(
        conn, source_run_id=source_run_id, recovery_key=recovery_key
    )
    if existing is not None:
        if existing["cancelled_at"] is not None:
            raise _conflict("sequence implementer replacement was cancelled")
        if str(existing["successor_run_id"]) != successor_run_id:
            raise _corrupt("sequence implementer successor disagrees with publication")
        return True
    _assert_no_competing_claim(
        store,
        conn,
        sequence_id=blocked.sequence_id,
        ordinal=blocked.current_ordinal,
        source_run_id=source_run_id,
        generation=generation,
    )
    intent = SequenceImplementerReplacementIntent(
        sequence_id=blocked.sequence_id,
        sequence_version=blocked.version,
        ordinal=blocked.current_ordinal,
        source_run_id=source_run_id,
        source_generation=generation,
        successor_run_id=successor_run_id,
        successor_generation=generation + 1,
        recovery_key=recovery_key,
        record_digest=None,
        entry_hash=binding.entry_hash,
        worktree_key=source.context.repository.worktree_key,
        repository_root=source.context.repository.root,
        reservation_owner_run_id=successor_run_id,
        adoption_state="pending",
    )
    payload = intent.canonical_bytes()
    try:
        inserted = store.insert_sequence_implementer_replacement(
            conn,
            intent=intent,
            intent_payload=payload.decode("utf-8"),
            intent_digest=hashlib.sha256(payload).hexdigest(),
            now=now,
        )
    except sqlite3.IntegrityError as exc:
        raise _conflict("sequence leaf generation already has a replacement") from exc
    if not inserted:
        current = store.get_sequence_implementer_replacement(
            conn, source_run_id=source_run_id, recovery_key=recovery_key
        )
        if current is None or str(current["successor_run_id"]) != successor_run_id:
            raise _conflict("sequence implementer replacement identity conflict")
    return True


def adopt_sequence_implementer_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    recovery_key: str,
    record_digest: str,
    now: datetime,
) -> bool:
    """CAS the current leaf to the authenticated Codex recovery successor."""

    row = store.get_sequence_implementer_replacement(
        conn, source_run_id=source_run_id, recovery_key=recovery_key
    )
    if row is None:
        raise _corrupt("sequence implementer replacement intent is missing")
    if row["cancelled_at"] is not None:
        raise _conflict("sequence implementer replacement was cancelled")
    intent = authenticated_sequence_implementer_intent(row)
    if intent.adoption_state == "adopted" and intent.record_digest != record_digest:
        raise _corrupt("sequence implementer recovery record digest changed")
    sequence = store.load_validated_sequence_state(conn, intent.sequence_id)
    if (
        isinstance(sequence, ActiveSequenceState)
        and sequence.current_run_id == intent.successor_run_id
        and intent.adoption_state == "adopted"
    ):
        return False
    if store.has_abort_requested_for_run(conn, intent.successor_run_id) or (
        store.has_sequence_abort_requested_for_run(
            conn, run_id=source_run_id, sequence_id=intent.sequence_id
        )
    ):
        raise _conflict("sequence abort was requested before implementer adoption")
    if not store.claim_reservation_for_successor(
        conn,
        successor_run_id=intent.successor_run_id,
        source_run_id=source_run_id,
        worktree_key=intent.worktree_key,
        repository_root=intent.repository_root,
        now=now,
    ):
        raise _conflict("repository reservation is held by another run")
    if not isinstance(sequence, BlockedSequenceState):
        raise _conflict("sequence is not blocked for implementer leaf replacement")
    if (
        sequence.current_run_id != source_run_id
        or sequence.current_ordinal != intent.ordinal
        or sequence.version != intent.sequence_version
    ):
        raise _invalid("source run is no longer the current blocked sequence leaf")
    current_entry = next(
        (item for item in sequence.materialized_entries if item.ordinal == intent.ordinal),
        None,
    )
    if (
        current_entry is None
        or current_entry.run_id != source_run_id
        or current_entry.entry_hash != intent.entry_hash
    ):
        raise _invalid("frozen sequence entry disagrees with implementer replacement")
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    replacement = SequenceRunAttempt(
        schema_version=2,
        generation=intent.successor_generation,
        run_id=intent.successor_run_id,
        source_run_id=source_run_id,
        attempt_kind=cast(SequenceAttemptKind, SEQUENCE_ATTEMPT_KIND_IMPLEMENTER_RETRY),
        materialized_at=current_entry.materialized_at,
        terminal_outcome=None,
        resolved_at=None,
    )
    updated_entry = MaterializedSequenceEntry(
        ordinal=intent.ordinal,
        run_id=intent.successor_run_id,
        entry_hash=intent.entry_hash,
        materialized_at=current_entry.materialized_at,
    )
    updated = ActiveSequenceState(
        schema_version=sequence.schema_version,
        sequence_id=sequence.sequence_id,
        version=sequence.version + 1,
        prepared_at=sequence.prepared_at,
        updated_at=now_text,
        started_at=sequence.started_at,
        idempotency_key=sequence.idempotency_key,
        definition=sequence.definition,
        current_ordinal=sequence.current_ordinal,
        current_run_id=intent.successor_run_id,
        materialized_entries=tuple(
            updated_entry if item.ordinal == intent.ordinal else item
            for item in sequence.materialized_entries
        ),
        residual_risk_ordinals=sequence.residual_risk_ordinals,
    )
    expectation = SequenceExecutionCASExpectation(
        expected_version=sequence.version,
        definition=sequence.definition,
        materialized_entries=sequence.materialized_entries,
        current_ordinal=sequence.current_ordinal,
        current_run_id=source_run_id,
    )
    if not compare_and_swap_sequence_execution_leaf(
        conn,
        store,
        sequence_id=sequence.sequence_id,
        ordinal=intent.ordinal,
        expectation=expectation,
        expected_current_run_id=source_run_id,
        replacement_attempt=replacement,
        updated_sequence_state=updated,
        now=now,
    ):
        raise _conflict("sequence implementer leaf replacement lost a concurrent update")
    adopted = intent.model_copy(
        update={"adoption_state": "adopted", "record_digest": record_digest}
    )
    payload = adopted.canonical_bytes()
    if not store.update_sequence_implementer_replacement_payload(
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
        intent_payload=payload.decode("utf-8"),
        intent_digest=hashlib.sha256(payload).hexdigest(),
        adopted_at=now,
        cancelled_at=None,
    ):
        raise _corrupt("sequence implementer replacement disappeared during adoption")
    return True


def cancel_pending_sequence_implementer_recovery(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    source_run_id: str,
    now: datetime,
) -> None:
    """Fence an unadopted sequence successor and its publication relation."""

    if not store.schema_supports_sequence_implementer_replacement(conn):
        return
    rows = store.list_sequence_implementer_replacements_for_source(
        conn, source_run_id=source_run_id
    )
    for row in rows:
        if (
            str(row["sequence_id"]) != sequence_id
            or row["adopted_at"] is not None
            or row["cancelled_at"] is not None
        ):
            continue
        intent = authenticated_sequence_implementer_intent(row)
        payload = intent.canonical_bytes()
        store.update_sequence_implementer_replacement_payload(
            conn,
            source_run_id=source_run_id,
            recovery_key=intent.recovery_key,
            intent_payload=payload.decode("utf-8"),
            intent_digest=hashlib.sha256(payload).hexdigest(),
            adopted_at=None,
            cancelled_at=now,
        )
        recovery = store.get_codex_implementer_recovery_by_key(
            conn, recovery_key=intent.recovery_key
        )
        if recovery is not None and str(recovery["status"]) == "pending":
            store.update_codex_implementer_recovery_status(
                conn,
                recovery_key=intent.recovery_key,
                expected_status="pending",
                status="cancelled",
                intent_artifact_path=None,
                intent_artifact_sha256=None,
                record_artifact_path=None,
                record_sha256=None,
                now=now,
            )
        reservation = store.get_reservation_for_run(conn, intent.successor_run_id)
        if reservation is not None and str(reservation["worktree_key"]) == intent.worktree_key:
            store.release_reservation(conn, worktree_key=intent.worktree_key, now=now)
