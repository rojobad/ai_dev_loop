"""Adopt one Cursor recovery successor at the original sequence ordinal."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from typing import cast

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.domain.cursor_sequence_replacement import (
    CursorSequenceReplacementIntent,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_CURSOR_RETRY,
    SequenceAttemptKind,
    SequenceRunAttempt,
)
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
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


def authenticated_cursor_sequence_intent(row: sqlite3.Row) -> CursorSequenceReplacementIntent:
    payload = str(row["intent_payload"]).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != str(row["intent_payload_sha256"]):
        raise _corrupt("cursor sequence replacement intent digest mismatch")
    try:
        intent = CursorSequenceReplacementIntent.model_validate_json(payload)
    except ValueError as exc:
        raise _corrupt("cursor sequence replacement intent failed authentication") from exc
    if intent.canonical_bytes() != payload:
        raise _corrupt("cursor sequence replacement intent is not canonical")
    if (
        intent.sequence_id != str(row["sequence_id"])
        or intent.ordinal != int(row["ordinal"])
        or intent.source_run_id != str(row["source_run_id"])
        or intent.source_generation != int(row["source_generation"])
        or intent.successor_run_id != str(row["successor_run_id"])
        or intent.successor_generation != int(row["successor_generation"])
        or intent.recovery_key != str(row["recovery_key"])
    ):
        raise _corrupt("cursor sequence replacement intent disagrees with the ledger row")
    adopted = row["adopted_at"] is not None
    cancelled = row["cancelled_at"] is not None
    if adopted and cancelled:
        raise _corrupt("cursor sequence replacement cannot be adopted and cancelled")
    if adopted != (intent.adoption_state == "adopted"):
        raise _corrupt("cursor sequence replacement adoption state disagrees with the ledger")
    return intent


def sequence_cursor_adoption_blocks_dispatch(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    """True unless this run is the adopted leaf of an active, uncancelled sequence."""

    if not store.schema_supports_cursor_sequence_replacement(conn):
        return False
    state, _, _ = store.load_validated_snapshot(conn, run_id)
    binding = state.context.sequence
    if binding is None:
        return False
    row = store.get_sequence_cursor_replacement_by_successor(conn, run_id)
    if row is None or row["adopted_at"] is None or row["cancelled_at"] is not None:
        return True
    intent = authenticated_cursor_sequence_intent(row)
    sequence = store.load_validated_sequence_state(conn, binding.sequence_id)
    if not isinstance(sequence, ActiveSequenceState) or sequence.current_run_id != run_id:
        return True
    if store.has_abort_requested_for_run(conn, run_id):
        return True
    if store.has_sequence_abort_requested_for_run(
        conn,
        run_id=run_id,
        sequence_id=binding.sequence_id,
    ):
        return True
    return store.has_sequence_abort_requested_for_run(
        conn,
        run_id=intent.source_run_id,
        sequence_id=binding.sequence_id,
    )


def assert_no_review_claim_for_leaf(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    ordinal: int,
    source_generation: int,
) -> None:
    if not store.schema_supports_sequence_review_recovery(conn):
        return
    for row in store.list_sequence_execution_replacement_intents_for_source(
        conn,
        source_run_id=source_run_id,
    ):
        if row["cancelled_at"] is not None:
            continue
        if int(row["ordinal"]) == ordinal and int(row["source_generation"]) == source_generation:
            raise _conflict("sequence leaf already has a review replacement claim")


def assert_no_cursor_claim_for_leaf(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    source_generation: int,
) -> None:
    if not store.schema_supports_cursor_sequence_replacement(conn):
        return
    row = store.get_sequence_cursor_replacement_for_generation(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        source_generation=source_generation,
    )
    if row is not None and row["cancelled_at"] is None:
        raise _conflict("sequence leaf already has a cursor replacement claim")


def bind_pending_cursor_sequence_intent(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    now: datetime,
) -> bool:
    """Record a pending Cursor sequence claim. Returns False for standalone runs."""

    state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    binding = state.context.sequence
    if binding is None:
        return False
    if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
        raise _conflict("sequence cursor recovery requires a blocked cursor failure")
    from ai_dev_loop.scheduler.application.sequence_review_recovery import (
        find_blocked_sequence_for_run,
    )

    blocked = find_blocked_sequence_for_run(store, conn, source_run_id)
    if blocked is None:
        raise _invalid("source run is not the current blocked sequence leaf")
    if blocked.sequence_id != binding.sequence_id or blocked.current_ordinal != binding.ordinal:
        raise _invalid("sequence binding disagrees with the blocked sequence leaf")
    if store.has_sequence_abort_requested_for_run(
        conn,
        run_id=source_run_id,
        sequence_id=blocked.sequence_id,
    ):
        raise _conflict("sequence abort was requested before cursor recovery")
    if store.has_abort_requested_for_run(conn, source_run_id):
        raise _conflict("source run has a durable abort request")
    leaf_run_id = _current_leaf_run_id(
        conn,
        sequence_id=blocked.sequence_id,
        ordinal=blocked.current_ordinal,
    )
    generation = _max_generation(
        conn,
        sequence_id=blocked.sequence_id,
        ordinal=blocked.current_ordinal,
    )
    if leaf_run_id != source_run_id or generation < 1:
        raise _invalid("source run is not the authenticated current sequence execution leaf")
    current_entry = next(
        (
            entry
            for entry in blocked.materialized_entries
            if entry.ordinal == blocked.current_ordinal
        ),
        None,
    )
    if (
        current_entry is None
        or current_entry.run_id != source_run_id
        or current_entry.entry_hash != binding.entry_hash
    ):
        raise _invalid("blocked sequence entry disagrees with the frozen run binding")
    assert_no_review_claim_for_leaf(
        store,
        conn,
        source_run_id=source_run_id,
        ordinal=blocked.current_ordinal,
        source_generation=generation,
    )
    existing_generation = store.get_sequence_cursor_replacement_for_generation(
        conn,
        sequence_id=blocked.sequence_id,
        ordinal=blocked.current_ordinal,
        source_generation=generation,
    )
    if existing_generation is not None and str(existing_generation["recovery_key"]) != recovery_key:
        raise _conflict("sequence leaf generation already has a cursor replacement")
    existing = store.get_sequence_cursor_replacement(
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    )
    if existing is not None:
        if existing["cancelled_at"] is not None:
            raise _conflict("cursor sequence replacement was cancelled")
        if str(existing["successor_run_id"]) != successor_run_id:
            raise _corrupt("cursor sequence replacement successor disagrees with publication")
        return True
    intent = CursorSequenceReplacementIntent(
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
        worktree_key=state.context.repository.worktree_key,
        repository_root=state.context.repository.root,
        reservation_owner_run_id=successor_run_id,
        adoption_state="pending",
    )
    payload = intent.canonical_bytes().decode("utf-8")
    try:
        inserted = store.insert_sequence_cursor_replacement(
            conn,
            intent=intent,
            intent_payload=payload,
            intent_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            now=now,
        )
    except sqlite3.IntegrityError as exc:
        raise _conflict("sequence leaf generation already has a cursor replacement") from exc
    if not inserted:
        current = store.get_sequence_cursor_replacement(
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if current is None or str(current["successor_run_id"]) != successor_run_id:
            raise _conflict("cursor sequence replacement identity conflict")
    return True


def authenticate_cursor_publication_for_adoption(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
) -> str | None:
    """Authenticate the durable relation and private record before sequence adoption.

    Returns None when the record file has not been published yet. A present but
    invalid record raises and must not be adopted.
    """

    from ai_dev_loop.scheduler.application.cursor_correction_recovery import (
        authenticate_correction_publication_for_adoption,
    )
    from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
        _intent_schema_version,
        authenticate_initial_publication_for_adoption,
    )

    row = store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
    if row is None:
        raise _corrupt("cursor recovery relation is missing")
    if str(row["source_run_id"]) != source_run_id or str(row["successor_run_id"]) != successor_run_id:
        raise _corrupt("cursor recovery relation disagrees with the sequence replacement")
    if str(row["status"]) == "cancelled":
        raise _conflict("cursor recovery publication was cancelled")
    if _intent_schema_version(row) == 2:
        return authenticate_correction_publication_for_adoption(store, artifacts, conn, row)
    return authenticate_initial_publication_for_adoption(store, artifacts, conn, row)


def adopt_cursor_sequence_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    recovery_key: str,
    record_digest: str,
    now: datetime,
) -> bool:
    """CAS the sequence leaf onto the successor. Returns True when this call adopted it."""

    row = store.get_sequence_cursor_replacement(
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    )
    if row is None:
        raise _corrupt("cursor sequence replacement intent is missing")
    if row["cancelled_at"] is not None:
        raise _conflict("cursor sequence replacement was cancelled")
    intent = authenticated_cursor_sequence_intent(row)
    if intent.adoption_state == "adopted" and intent.record_digest != record_digest:
        raise _corrupt("cursor sequence recovery record digest changed")
    sequence = store.load_validated_sequence_state(conn, intent.sequence_id)
    if (
        isinstance(sequence, ActiveSequenceState)
        and sequence.current_run_id == intent.successor_run_id
        and intent.adoption_state == "adopted"
    ):
        return False
    if store.has_sequence_abort_requested_for_run(
        conn,
        run_id=source_run_id,
        sequence_id=intent.sequence_id,
    ) or store.has_abort_requested_for_run(conn, intent.successor_run_id):
        raise _conflict("sequence abort was requested before cursor adoption")
    if not store.claim_reservation_for_successor(
        conn,
        successor_run_id=intent.successor_run_id,
        source_run_id=source_run_id,
        worktree_key=intent.worktree_key,
        repository_root=intent.repository_root,
        now=now,
    ):
        raise _conflict("repository reservation is held by another run")
    if isinstance(sequence, ActiveSequenceState) and sequence.current_run_id == intent.successor_run_id:
        _mark_adopted(store, conn, intent, record_digest=record_digest, now=now)
        return True
    if not isinstance(sequence, BlockedSequenceState):
        raise _conflict("sequence is not blocked for cursor leaf replacement")
    if sequence.current_run_id != source_run_id or sequence.current_ordinal != intent.ordinal:
        raise _invalid("source run is not the current blocked sequence leaf")
    if sequence.version != intent.sequence_version:
        raise _conflict("blocked sequence version changed during cursor recovery")
    current_entry = next(
        (entry for entry in sequence.materialized_entries if entry.ordinal == intent.ordinal),
        None,
    )
    if current_entry is None or current_entry.entry_hash != intent.entry_hash:
        raise _invalid("frozen sequence entry disagrees with the cursor replacement intent")
    if current_entry.run_id != source_run_id:
        raise _invalid("materialized sequence entry is no longer the cursor recovery source")
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    replacement = SequenceRunAttempt(
        schema_version=2,
        generation=intent.successor_generation,
        run_id=intent.successor_run_id,
        source_run_id=source_run_id,
        attempt_kind=cast(SequenceAttemptKind, SEQUENCE_ATTEMPT_KIND_CURSOR_RETRY),
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
    materialized = tuple(
        updated_entry if entry.ordinal == intent.ordinal else entry
        for entry in sequence.materialized_entries
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
        materialized_entries=materialized,
        residual_risk_ordinals=sequence.residual_risk_ordinals,
    )
    expectation = SequenceExecutionCASExpectation(
        expected_version=sequence.version,
        definition=sequence.definition,
        materialized_entries=sequence.materialized_entries,
        current_ordinal=sequence.current_ordinal,
        current_run_id=source_run_id,
    )
    adopted = compare_and_swap_sequence_execution_leaf(
        conn,
        store,
        sequence_id=sequence.sequence_id,
        ordinal=intent.ordinal,
        expectation=expectation,
        expected_current_run_id=source_run_id,
        replacement_attempt=replacement,
        updated_sequence_state=updated,
        now=now,
    )
    if not adopted:
        refreshed = store.load_validated_sequence_state(conn, sequence.sequence_id)
        if not (
            isinstance(refreshed, ActiveSequenceState)
            and refreshed.current_run_id == intent.successor_run_id
        ):
            raise _conflict("sequence cursor leaf replacement lost a concurrent update")
    _mark_adopted(store, conn, intent, record_digest=record_digest, now=now)
    return True


def _mark_adopted(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    intent: CursorSequenceReplacementIntent,
    *,
    record_digest: str,
    now: datetime,
) -> None:
    adopted = intent.model_copy(
        update={"adoption_state": "adopted", "record_digest": record_digest}
    )
    payload = adopted.canonical_bytes().decode("utf-8")
    updated = store.update_sequence_cursor_replacement_payload(
        conn,
        source_run_id=intent.source_run_id,
        recovery_key=intent.recovery_key,
        intent_payload=payload,
        intent_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        adopted_at=now,
        cancelled_at=None,
    )
    if not updated:
        raise _corrupt("cursor sequence replacement disappeared during adoption")


def ensure_sequence_cursor_adoption(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    now: datetime,
) -> bool:
    """Adopt when this recovery belongs to a sequence. Standalone publication returns False."""

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        if state.context.sequence is None:
            return False
        row = store.get_sequence_cursor_replacement(
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
    if row is None:
        raise _corrupt("sequence cursor recovery is missing its replacement intent")
    if row["cancelled_at"] is not None:
        raise _conflict("cursor sequence replacement was cancelled")
    if str(row["successor_run_id"]) != successor_run_id:
        raise _corrupt("cursor sequence replacement successor disagrees with publication")
    with store.begin_immediate() as conn:
        digest = authenticate_cursor_publication_for_adoption(
            store,
            artifacts,
            conn,
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            recovery_key=recovery_key,
        )
        if digest is None:
            raise _corrupt("cursor sequence recovery record is missing")
        adopt_cursor_sequence_successor(
            store,
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            record_digest=digest,
            now=now,
        )
    return True


def cancel_pending_cursor_sequence_recovery(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    source_run_id: str,
    now: datetime,
) -> None:
    """Cancel unadopted Cursor replacement and its pending publication."""

    if not store.schema_supports_cursor_sequence_replacement(conn):
        return
    rows = [
        row
        for row in store.list_sequence_cursor_replacements_for_source(
            conn,
            source_run_id=source_run_id,
        )
        if str(row["sequence_id"]) == sequence_id and row["adopted_at"] is None
    ]
    for row in rows:
        if row["cancelled_at"] is not None:
            continue
        intent = authenticated_cursor_sequence_intent(row)
        payload = intent.canonical_bytes().decode("utf-8")
        store.update_sequence_cursor_replacement_payload(
            conn,
            source_run_id=source_run_id,
            recovery_key=intent.recovery_key,
            intent_payload=payload,
            intent_digest=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            adopted_at=None,
            cancelled_at=now,
        )
        _cancel_pending_recovery_row(store, conn, recovery_key=intent.recovery_key, now=now)
        reservation = store.get_reservation_for_run(conn, intent.successor_run_id)
        if reservation is not None and str(reservation["worktree_key"]) == intent.worktree_key:
            store.release_reservation(conn, worktree_key=intent.worktree_key, now=now)


def _cancel_pending_recovery_row(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    recovery_key: str,
    now: datetime,
) -> None:
    row = store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
    if row is None or str(row["status"]) != "pending":
        return
    import json

    from pydantic import ValidationError

    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
        CursorInitialRecoveryPublicationIntentV1,
    )
    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import CursorRecoveryPublicationIntentV2

    raw = str(row["intent_payload"])
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _corrupt("cursor recovery publication intent is not valid JSON") from exc
    version = parsed.get("schema_version") if isinstance(parsed, dict) else None
    try:
        if version == 2:
            intent_v2 = CursorRecoveryPublicationIntentV2.model_validate_json(raw)
            cancelled_v2 = intent_v2.model_copy(update={"status": "cancelled", "record_sha256": None})
            payload = cancelled_v2.canonical_bytes()
        else:
            intent_v1 = CursorInitialRecoveryPublicationIntentV1.model_validate_json(raw)
            cancelled_v1 = intent_v1.model_copy(update={"status": "cancelled", "record_sha256": None})
            payload = cancelled_v1.canonical_bytes()
    except (ValidationError, ValueError) as exc:
        raise _corrupt("cursor recovery publication intent failed authentication") from exc
    store.update_cursor_initial_recovery_status(
        conn,
        recovery_key=recovery_key,
        expected_status="pending",
        status="cancelled",
        intent_payload=payload.decode("utf-8"),
        intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
        record_artifact_path=None,
        record_sha256=None,
        now=now,
    )


def reconcile_unadopted_cursor_sequence_intent(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> TickRunReceipt:
    """Adopt on the caller's connection only after the publication record authenticates."""

    source_run_id = str(row["source_run_id"])
    successor_run_id = str(row["successor_run_id"])
    moment = now or datetime.now(tz=UTC)
    if row["cancelled_at"] is not None:
        return TickRunReceipt(run_id=source_run_id, action="sequence_cursor_intent_cancelled")
    if row["adopted_at"] is not None:
        return TickRunReceipt(
            run_id=successor_run_id,
            action="sequence_cursor_adoption_complete",
        )
    if store.has_abort_requested_for_run(conn, successor_run_id) or store.has_sequence_abort_requested_for_run(
        conn,
        run_id=source_run_id,
        sequence_id=str(row["sequence_id"]),
    ):
        cancel_pending_cursor_sequence_recovery(
            store,
            conn,
            sequence_id=str(row["sequence_id"]),
            source_run_id=source_run_id,
            now=moment,
        )
        return TickRunReceipt(run_id=source_run_id, action="sequence_cursor_intent_cancelled")
    try:
        digest = authenticate_cursor_publication_for_adoption(
            store,
            artifacts,
            conn,
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            recovery_key=str(row["recovery_key"]),
        )
    except SchedulerEngineError as exc:
        return TickRunReceipt(
            run_id=successor_run_id,
            action="sequence_cursor_publication_rejected",
            detail=str(exc),
        )
    if digest is None:
        return TickRunReceipt(
            run_id=successor_run_id,
            action="sequence_cursor_publication_pending",
        )
    changed = adopt_cursor_sequence_successor(
        store,
        conn,
        source_run_id=source_run_id,
        recovery_key=str(row["recovery_key"]),
        record_digest=digest,
        now=moment,
    )
    return TickRunReceipt(
        run_id=successor_run_id,
        action="sequence_cursor_adoption_reconciled" if changed else "sequence_cursor_adoption_complete",
    )
