"""One automatic continuation for an authenticated Cursor SDK no-progress turn."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal, cast

from pydantic import ValidationError

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_sdk_attempt_events_rel,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_attempt_metadata_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_no_progress import (
    NO_PROGRESS_CATEGORY,
    NO_PROGRESS_DELAY_SECONDS,
    NO_PROGRESS_MESSAGE_SHA256,
    CursorSdkNoProgressAuthorizationV1,
    CursorSdkNoProgressWitnessV1,
    exact_status_no_progress,
    logical_turn_key,
    no_progress_witness_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkReconciliationRecordV1,
    CursorSdkReconciliationRecordV2,
)
from ai_dev_loop.scheduler.domain.events import CursorSdkNoProgressAuthorizedEvent
from ai_dev_loop.scheduler.domain.state import (
    BlockedState,
    CursorReadyState,
    SchedulerState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_file

ReconciliationRecord = CursorSdkReconciliationRecordV1 | CursorSdkReconciliationRecordV2

if TYPE_CHECKING:
    from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
        CursorInitialRecoveryResult,
    )

publish_fault_hook: Callable[[str], None] | None = None
NO_PROGRESS_REOBSERVE_EFFECT_ID = "cursor-sdk-no-progress-reobserve"
NO_PROGRESS_REPLAY_STAGING_EFFECT_ID = "cursor-normalize-staging-no-progress-replay"

MANUAL_CONTINUATION_SUMMARY = (
    "Cursor SDK native reconciliation established one safe continuation. "
    "Explicit scheduler implementer-retry --force is required."
)
EXHAUSTED_CONTINUATION_SUMMARY = (
    "Cursor SDK native reconciliation established one safe continuation. "
    "The one automatic resume-no-progress continuation for this logical turn is already used. "
    "Explicit scheduler implementer-retry --force is required."
)
UNSAFE_CONTINUATION_SUMMARY = (
    "Cursor SDK native reconciliation did not establish one safe continuation."
)


def pending_continuation_summary(due_at: str) -> str:
    return (
        "Cursor SDK native reconciliation established one safe continuation. "
        "One automatic resume-no-progress continuation is reserved until "
        f"{due_at}. Run scheduler tick after that time. "
        "Do not force another successor during this wait."
    )


@dataclass(frozen=True)
class NoProgressDecision:
    """Safe decision recorded after the adjacent reconciliation and block events."""

    attempt_id: str
    action: Literal["automatic", "exhausted", "manual"]
    consumed_count: int | None
    due_at: str | None


@dataclass(frozen=True)
class LogicalTurn:
    key: str
    turn_kind: Literal["initial", "correction"]
    conversation_key: str
    base_prompt_sha256: str
    reviews_completed: int
    fix_prompt_sha256: str | None
    review_result_sha256: str | None


@dataclass(frozen=True)
class NoProgressProjection:
    category: str
    action: Literal["automatic", "exhausted", "manual"]
    consumed: int | None
    due_at: str | None
    status: str
    source_run_id: str
    successor_run_id: str | None


def diagnose_continuation_ready(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    state: SchedulerState,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
    now: datetime,
) -> tuple[str, NoProgressDecision | None]:
    """Reserve one delayed continuation, or explain the manual path.

    Called inside the reconciliation transaction. The returned decision is
    appended after the block event so the reconciliation stays adjacent to it.
    Absence of the witness keeps the explicit retry path and consumes nothing.
    """

    if intent.failure_class != "failure" or not _native_failure_is_exact(record):
        return MANUAL_CONTINUATION_SUMMARY, None
    witness = authenticate_no_progress_witness(
        store,
        artifacts,
        conn,
        run_id=state.run_id,
        intent=intent,
        record=record,
    )
    if witness is None:
        return MANUAL_CONTINUATION_SUMMARY, None
    manual = NoProgressDecision(
        attempt_id=intent.source_attempt_id,
        action="manual",
        consumed_count=None,
        due_at=None,
    )
    if not _continuation_predicates(store, conn, state, intent):
        return MANUAL_CONTINUATION_SUMMARY, manual
    turn = logical_turn_for_failure(store, artifacts, conn, intent)
    if turn is None:
        return MANUAL_CONTINUATION_SUMMARY, manual
    existing = store.get_cursor_sdk_no_progress(conn, logical_turn_key=turn.key)
    if existing is not None:
        return EXHAUSTED_CONTINUATION_SUMMARY, NoProgressDecision(
            attempt_id=intent.source_attempt_id,
            action="exhausted",
            consumed_count=1,
            due_at=None,
        )
    authorized_at = encode_utc_instant(now)
    due_at = encode_utc_instant(now + timedelta(seconds=NO_PROGRESS_DELAY_SECONDS))
    witness_sha = hashlib.sha256(witness.canonical_bytes()).hexdigest()
    authorization = CursorSdkNoProgressAuthorizationV1(
        logical_turn_key=turn.key,
        consumed_count=1,
        status="reserved",
        source_run_id=state.run_id,
        source_attempt_id=intent.source_attempt_id,
        witness_path=no_progress_witness_rel(intent.source_attempt_id),
        witness_sha256=witness_sha,
        authorized_at=authorized_at,
        due_at=due_at,
        turn_kind=turn.turn_kind,
        conversation_key=turn.conversation_key,
        base_prompt_sha256=turn.base_prompt_sha256,
        reviews_completed=turn.reviews_completed,
        fix_prompt_sha256=turn.fix_prompt_sha256,
        review_result_sha256=turn.review_result_sha256,
    )
    payload = authorization.canonical_bytes()
    inserted = store.insert_cursor_sdk_no_progress(
        conn,
        logical_turn_key=turn.key,
        consumed_count=1,
        status="reserved",
        source_run_id=state.run_id,
        source_attempt_id=intent.source_attempt_id,
        witness_path=authorization.witness_path,
        witness_sha256=witness_sha,
        authorized_at=authorized_at,
        due_at=due_at,
        payload=payload.decode("utf-8"),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        now=now,
    )
    if not inserted:
        return EXHAUSTED_CONTINUATION_SUMMARY, NoProgressDecision(
            attempt_id=intent.source_attempt_id,
            action="exhausted",
            consumed_count=1,
            due_at=None,
        )
    return pending_continuation_summary(due_at), NoProgressDecision(
        attempt_id=intent.source_attempt_id,
        action="automatic",
        consumed_count=1,
        due_at=due_at,
    )


def publish_due_no_progress_continuations(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    now: datetime,
) -> list[TickRunReceipt]:
    """Publish at most one same-agent successor for each due authorization."""

    with store.begin_read() as conn:
        rows = list(store.list_open_cursor_sdk_no_progress(conn))
    receipts: list[TickRunReceipt] = []
    for row in rows:
        receipt = _publish_one(store, artifacts, row, now)
        if receipt is not None:
            receipts.append(receipt)
    return receipts


def pending_result_for_connection(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
    *,
    now: datetime,
) -> CursorInitialRecoveryResult | None:
    """Return the reserved continuation visible on this connection, if it is not due."""

    from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
        CursorInitialRecoveryResult,
    )

    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=source_run_id)
    if row is None or str(row["status"]) != "reserved":
        return None
    authorization = _authorization_from_row(row)
    if authorization is None:
        return None
    if now >= _instant(str(row["due_at"])):
        return None
    turn_kind: Literal["initial", "correction"] = (
        "correction" if authorization.turn_kind == "correction" else "initial"
    )
    return CursorInitialRecoveryResult(
        source_run_id=source_run_id,
        successor_run_id=source_run_id,
        changed=False,
        idempotent_replay=True,
        publication_status="pending",
        agent_execution_ready=False,
        safe_next_action=_wait_action(source_run_id, authorization.due_at),
        turn_kind=turn_kind,
        automatic_continuation=True,
        automatic_due_at=authorization.due_at,
    )


def pending_automatic_retry_result(
    store: SqliteSchedulerStore,
    source_run_id: str,
    *,
    now: datetime,
) -> CursorInitialRecoveryResult | None:
    """Report a reserved continuation instead of publishing a second successor."""

    with store.begin_read() as conn:
        return pending_result_for_connection(store, conn, source_run_id, now=now)


def source_has_open_no_progress(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    """A continuation stays abortable only until it starts or is handed off."""

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return False
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    if row is None:
        return False
    status = str(row["status"])
    if status not in {"reserved", "publishing", "published"}:
        return False
    if _continuation_departed(store, conn, run_id):
        return False
    if status in {"reserved", "publishing"}:
        return True
    return _published_continuation_unstarted(store, conn, row)


def obsolete_no_progress_source(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    """Execution or adoption makes the old source the wrong abort target."""

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return False
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    if row is None or str(row["status"]) not in {
        "reserved",
        "publishing",
        "published",
        "cancelled",
    }:
        return False
    return _continuation_departed(store, conn, run_id)


def _continuation_departed(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> bool:
    """True once a successor has an attempt or the sequence leaf has moved.

    The authorization row can still say publishing, and can still omit
    successor_run_id, after force has adopted the sequence leaf.
    """

    for successor_id in _open_successor_ids(store, conn, source_run_id):
        started = conn.execute(
            "SELECT 1 FROM scheduler_attempts WHERE run_id = ? LIMIT 1",
            (successor_id,),
        ).fetchone()
        if started is not None:
            return True
        try:
            source, _, _ = store.load_validated_snapshot(conn, source_run_id)
        except (OSError, ValueError, ValidationError):
            return False
        binding = source.context.sequence
        if binding is None:
            continue
        sequence = store.load_validated_sequence_state(conn, binding.sequence_id)
        if getattr(sequence, "current_run_id", None) == successor_id:
            return True
    return False


def _open_successor_ids(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> list[str]:
    successors: list[str] = []
    for row in store.list_cursor_initial_recoveries_for_source(conn, source_run_id=source_run_id):
        if str(row["status"]) not in {"pending", "ready"}:
            continue
        try:
            payload = json.loads(str(row["intent_payload"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != 3:
            continue
        successor_id = str(row["successor_run_id"] or "")
        if successor_id:
            successors.append(successor_id)
    return successors


def _published_continuation_unstarted(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> bool:
    successor_id = str(row["successor_run_id"] or "")
    if not successor_id:
        return True
    started = conn.execute(
        "SELECT 1 FROM scheduler_attempts WHERE run_id = ? LIMIT 1",
        (successor_id,),
    ).fetchone()
    if started is not None:
        return False
    try:
        state, _, _ = store.load_validated_snapshot(conn, successor_id)
    except (OSError, ValueError, ValidationError):
        return False
    if state.kind not in {"cursor_ready", "waiting_for_cursor_fix"}:
        return False
    binding = state.context.sequence
    if binding is not None:
        sequence = store.load_validated_sequence_state(conn, binding.sequence_id)
        if getattr(sequence, "current_run_id", None) == successor_id:
            return False
    return True


def _abort_unstarted_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    successor_id: str,
    now: datetime,
) -> None:
    """Finish cancellation by aborting a successor that never started."""

    from ai_dev_loop.scheduler.domain.events import AbortRequestedEvent, RunAbortedEvent
    from ai_dev_loop.scheduler.domain.reducer import apply_run_aborted
    from ai_dev_loop.scheduler.domain.state import AbortedState

    state, version, _ = store.load_validated_snapshot(conn, successor_id)
    if isinstance(state, AbortedState):
        return
    if not isinstance(state, (CursorReadyState, WaitingForCursorFixState)):
        return
    now_text = encode_utc_instant(now)
    request = AbortRequestedEvent(run_id=successor_id, reason="user_requested_abort")
    aborted = RunAbortedEvent(
        run_id=successor_id,
        reason="user_requested_abort",
        prior_state_kind=state.kind,
    )
    store.append_event(
        conn,
        event_id=f"evt-{secrets.token_hex(16)}",
        run_id=successor_id,
        sequence=store.next_event_sequence(conn, successor_id),
        event=request,
        now=now,
    )
    store.append_event(
        conn,
        event_id=f"evt-{secrets.token_hex(16)}",
        run_id=successor_id,
        sequence=store.next_event_sequence(conn, successor_id),
        event=aborted,
        now=now,
    )
    new_state = apply_run_aborted(state, aborted, now_text=now_text)
    store.compare_and_swap_state(
        conn,
        run_id=successor_id,
        expected_version=version,
        new_state=new_state,
        now=now,
    )


def _fence_no_progress_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    now: datetime,
) -> None:
    """Cancel the pending successor relation and release it when no attempt is live."""

    from ai_dev_loop.scheduler.application.sequence_cursor_recovery import (
        cancel_pending_cursor_sequence_recovery,
    )
    from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import CursorSdkSuccessorIntentV1

    rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=source_run_id)
    for row in rows:
        status = str(row["status"])
        if status not in {"pending", "ready"}:
            continue
        try:
            payload = json.loads(str(row["intent_payload"]))
            if not isinstance(payload, dict) or payload.get("schema_version") != 3:
                continue
            intent = CursorSdkSuccessorIntentV1.model_validate(payload)
        except (TypeError, ValueError, ValidationError):
            continue
        cancelled = intent.model_copy(
            update={
                "status": "cancelled",
                "effective_prompt_path": None,
                "effective_prompt_sha256": None,
                "record_sha256": None,
            }
        )
        cancelled_bytes = cancelled.canonical_bytes()
        if not store.update_cursor_initial_recovery_status(
            conn,
            recovery_key=str(row["recovery_key"]),
            expected_status=status,
            status="cancelled",
            intent_payload=cancelled_bytes.decode("utf-8"),
            intent_payload_sha256=hashlib.sha256(cancelled_bytes).hexdigest(),
            record_artifact_path=None,
            record_sha256=None,
            now=now,
        ):
            continue
        successor_id = str(row["successor_run_id"])
        store.cancel_live_work(conn, run_id=successor_id, now=now)
        if store.get_nonterminal_attempt_for_run(conn, successor_id) is None:
            _abort_unstarted_successor(store, conn, successor_id=successor_id, now=now)
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        sequence = state.context.sequence
        if sequence is not None:
            cancel_pending_cursor_sequence_recovery(
                store,
                conn,
                sequence_id=sequence.sequence_id,
                source_run_id=source_run_id,
                now=now,
            )
        if store.get_nonterminal_attempt_for_run(conn, successor_id) is None:
            reservation = store.get_reservation_for_run(conn, successor_id)
            if reservation is not None:
                store.release_reservation(
                    conn,
                    worktree_key=str(reservation["worktree_key"]),
                    now=now,
                )


def cancel_no_progress_for_abort(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: datetime,
) -> None:
    """Cancel a pending automatic publication without refunding its allowance."""

    if not source_has_open_no_progress(store, conn, run_id):
        return
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    if row is not None and str(row["status"]) in {"reserved", "publishing", "published"}:
        authorization = _authorization_from_row(row)
        if authorization is None:
            store.cancel_cursor_sdk_no_progress_for_run(conn, run_id=run_id, now=now)
        else:
            cancelled = authorization.model_copy(update={"status": "cancelled"})
            payload = cancelled.canonical_bytes()
            store.update_cursor_sdk_no_progress(
                conn,
                logical_turn_key=authorization.logical_turn_key,
                expected_statuses=("reserved", "publishing", "published"),
                status="cancelled",
                successor_run_id=authorization.successor_run_id,
                recovery_key=authorization.recovery_key,
                payload=payload.decode("utf-8"),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                now=now,
            )
    _fence_no_progress_successor(store, conn, source_run_id=run_id, now=now)


def cancel_adopted_replay_for_aborted_successor(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    successor_run_id: str,
    now: datetime,
) -> None:
    """Stop a replay install whose adopted successor is the run being aborted.

    The authorization may still omit successor_run_id. The ready recovery row
    and its hashes stay in place so ancestry remains authentic.
    """

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return
    recovery = store.get_cursor_initial_recovery_by_successor(
        conn, successor_run_id=successor_run_id
    )
    if recovery is None or str(recovery["status"]) not in {"pending", "ready"}:
        return
    try:
        payload = json.loads(str(recovery["intent_payload"]))
    except (TypeError, ValueError):
        return
    if not isinstance(payload, dict) or payload.get("schema_version") != 3:
        return
    source_run_id = str(recovery["source_run_id"])
    failed_attempt_id = str(recovery["failed_attempt_id"])
    row = store.get_cursor_sdk_no_progress_for_source_attempt(
        conn,
        source_run_id=source_run_id,
        source_attempt_id=failed_attempt_id,
    )
    if row is None or str(row["status"]) not in {"reserved", "publishing"}:
        return
    authorization = _authorization_from_row(row)
    if authorization is None:
        return
    cancelled = authorization.model_copy(update={"status": "cancelled"})
    body = cancelled.canonical_bytes()
    store.update_cursor_sdk_no_progress(
        conn,
        logical_turn_key=authorization.logical_turn_key,
        expected_statuses=("reserved", "publishing"),
        status="cancelled",
        successor_run_id=authorization.successor_run_id,
        recovery_key=authorization.recovery_key,
        payload=body.decode("utf-8"),
        payload_sha256=hashlib.sha256(body).hexdigest(),
        now=now,
    )


def no_progress_retains_repository(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    """Keep the repository while an automatic continuation is not yet adopted."""

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return False
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    return row is not None and str(row["status"]) in {"reserved", "publishing"}


def no_progress_projection(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> NoProgressProjection | None:
    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return None
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    role: Literal["source", "successor"] = "source"
    if row is None:
        row = store.get_cursor_sdk_no_progress_for_successor(conn, successor_run_id=run_id)
        role = "successor"
    if row is None:
        return None
    authorization = _authorization_from_row(row)
    if authorization is None:
        return None
    status = str(row["status"])
    consumed = authorization.consumed_count if authorization.consumed_count in (0, 1) else None
    if isinstance(consumed, bool):
        return None
    action: Literal["automatic", "exhausted", "manual"]
    due_at: str | None
    if status in {"reserved", "publishing"} or status == "published" and role == "source":
        action = "automatic"
        due_at = authorization.due_at
    elif status == "cancelled":
        action = "manual"
        due_at = None
    else:
        action = "exhausted"
        due_at = None
    return NoProgressProjection(
        category=NO_PROGRESS_CATEGORY,
        action=action,
        consumed=consumed,
        due_at=due_at,
        status=status,
        source_run_id=authorization.source_run_id,
        successor_run_id=str(row["successor_run_id"]) if row["successor_run_id"] else None,
    )


def no_progress_safe_action(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: BlockedState,
) -> SafeNextAction | None:
    projection = no_progress_projection(store, conn, state.run_id)
    if projection is None:
        return None
    if projection.status in {"reserved", "publishing"}:
        return _wait_action(state.run_id, projection.due_at or "")
    if projection.status == "published" and projection.source_run_id == state.run_id:
        return SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command=(
                "Scheduler tick dispatches the published Cursor SDK continuation "
                f"for run {state.run_id}. Do not start another successor."
            ),
        )
    if projection.consumed == 1:
        return SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command=(
                f"ai_dev_loop scheduler implementer-retry {state.run_id} --force "
                "(the one automatic resume-no-progress continuation for this logical "
                "turn is already used; authenticate and publish one manual successor)."
            ),
        )
    return None


def _attempt_capture_digest(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
) -> tuple[str, bool] | None:
    """Return the attempt stdout's events digest and truncation flag."""

    attempt = store.get_attempt_by_id(conn, attempt_id)
    if attempt is None or str(attempt["run_id"]) != run_id:
        return None
    stdout_rel = str(attempt["stdout_artifact_path"] or "")
    if not stdout_rel:
        return None
    try:
        path = resolve_run_relative_path(artifacts.run_root(run_id), stdout_rel)
        if not path.is_file() or path.is_symlink():
            return None
        outcome = json.loads(path.read_text(encoding="utf-8"))
    except (ProtectedArtifactError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(outcome, dict):
        return None
    digest = outcome.get("events_sha256")
    truncated = outcome.get("events_truncated")
    if not isinstance(digest, str) or len(digest) != 64:
        return None
    if truncated is not False:
        return None
    return digest, False


def no_progress_guard_native_run_id(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    failed_attempt_id: str,
) -> str | None:
    """Native run that a published no-progress continuation must recheck before send."""

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return None
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=source_run_id)
    if row is None or str(row["status"]) != "published":
        return None
    if str(row["source_attempt_id"]) != failed_attempt_id:
        return None
    relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
        conn, source_attempt_id=failed_attempt_id
    )
    if relation is None:
        return None
    result_path = str(relation["result_artifact_path"] or "")
    result_sha = str(relation["result_sha256"] or "")
    if not result_path or not result_sha:
        return None
    try:
        payload = artifacts.read_verified_bytes(
            source_run_id,
            result_path,
            expected_sha256=result_sha,
        )
        raw = json.loads(payload)
    except (ProtectedArtifactError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    native_run_id = raw.get("native_run_id")
    if not isinstance(native_run_id, str) or not native_run_id:
        return None
    return native_run_id


def no_progress_dispatch_authority(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    failed_attempt_id: str,
) -> dict[str, object] | None:
    """Frozen witness authority a published continuation must recheck before send."""

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return None
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=source_run_id)
    if row is None or str(row["status"]) != "published":
        return None
    if str(row["source_attempt_id"]) != failed_attempt_id:
        return None
    refused: dict[str, object] = {"no_progress_evidence_refused": True}
    authorization = _authorization_from_row(row)
    native_run_id = no_progress_guard_native_run_id(
        store,
        artifacts,
        conn,
        source_run_id=source_run_id,
        failed_attempt_id=failed_attempt_id,
    )
    if authorization is None or not native_run_id:
        return refused
    try:
        path = resolve_run_relative_path(
            artifacts.run_root(source_run_id), authorization.witness_path
        )
        if path.is_symlink() or not path.is_file():
            return refused
        data = path.read_bytes()
        witness = CursorSdkNoProgressWitnessV1.model_validate_json(data)
    except (OSError, ValidationError, ValueError):
        return refused
    witness_sha = hashlib.sha256(witness.canonical_bytes()).hexdigest()
    if (
        hashlib.sha256(data).hexdigest() != authorization.witness_sha256
        or witness_sha != authorization.witness_sha256
        or not witness.events_path
        or not witness.events_sha256
    ):
        return refused
    return {
        "no_progress_guard_native_run_id": native_run_id,
        "no_progress_source_run_id": source_run_id,
        "no_progress_events_path": witness.events_path,
        "no_progress_events_sha256": witness.events_sha256,
    }


def no_progress_reobserve_run_ids(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
) -> list[str]:
    """Blocked runs whose fresh native read is an owned reconciliation attempt."""

    from ai_dev_loop.scheduler.domain.cursor_contract import RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND

    if not store.schema_supports_cursor_sdk_no_progress(conn):
        return []
    rows = conn.execute(
        """
        SELECT DISTINCT run_id FROM scheduler_effects
        WHERE effect_kind = ?
          AND status IN ('pending', 'claimed')
          AND json_extract(effect_payload, '$.no_progress_reobserve') = 1
        UNION
        SELECT DISTINCT scheduler_attempts.run_id
        FROM scheduler_attempts
        JOIN scheduler_effects
          ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
        WHERE scheduler_attempts.status IN ('launching', 'active', 'uncertain')
          AND scheduler_effects.effect_kind = ?
          AND json_extract(scheduler_effects.effect_payload, '$.no_progress_reobserve') = 1
        """,
        (RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND),
    ).fetchall()
    return [str(row["run_id"]) for row in rows]


def authenticate_no_progress_witness(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
) -> CursorSdkNoProgressWitnessV1 | None:
    relative = no_progress_witness_rel(intent.source_attempt_id)
    try:
        root = artifacts.run_root(run_id)
        path = resolve_run_relative_path(root, relative)
        if not path.is_file() or path.is_symlink():
            return None
        data = path.read_bytes()
        witness = CursorSdkNoProgressWitnessV1.model_validate_json(data)
        if (
            hashlib.sha256(witness.canonical_bytes()).hexdigest()
            != hashlib.sha256(data).hexdigest()
        ):
            return None
    except (ProtectedArtifactError, OSError, ValidationError, ValueError):
        return None
    if (
        witness.message_sha256 != NO_PROGRESS_MESSAGE_SHA256
        or witness.final_native_status != "error"
        or witness.attempt_id != intent.source_attempt_id
        or witness.dispatch_id != intent.source_dispatch_id
        or witness.agent_id != intent.agent_id
        or witness.agent_id != record.agent_id
        or witness.native_run_id != record.native_run_id
        or witness.journal_path != intent.source_journal_path
        or witness.journal_path != cursor_sdk_attempt_journal_rel(intent.source_attempt_id)
        or witness.events_path
        != cursor_sdk_attempt_events_rel(intent.iteration, intent.source_attempt_id)
        or witness.journal_sha256 != intent.source_journal_sha256
        or witness.journal_sha256 != record.source_journal_sha256
    ):
        return None
    try:
        events = resolve_run_relative_path(artifacts.run_root(run_id), witness.events_path)
        journal = resolve_run_relative_path(artifacts.run_root(run_id), witness.journal_path)
        if (
            events.is_symlink()
            or journal.is_symlink()
            or not events.is_file()
            or not journal.is_file()
        ):
            return None
        if sha256_file(journal) != witness.journal_sha256:
            return None
        matches = _matching_status_lines(
            events.read_bytes(),
            agent_id=witness.agent_id,
            native_run_id=witness.native_run_id,
        )
    except (OSError, ValueError):
        return None
    if len(matches) != 1:
        return None
    offset, digest = matches[0]
    events_bytes = events.read_bytes()
    if (
        offset != witness.offset
        or digest != witness.capture_sha256
        or hashlib.sha256(events_bytes).hexdigest() != witness.events_sha256
        or witness.events_truncated
    ):
        return None
    attempt_capture = _attempt_capture_digest(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=intent.source_attempt_id,
    )
    if attempt_capture is None or attempt_capture[0] != witness.events_sha256:
        return None
    try:
        metadata_path = resolve_run_relative_path(
            artifacts.run_root(run_id),
            cursor_sdk_attempt_metadata_rel(intent.iteration, intent.source_attempt_id),
        )
        if metadata_path.is_symlink() or not metadata_path.is_file():
            return None
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(metadata, dict)
        or metadata.get("events_sha256") != witness.events_sha256
        or metadata.get("events_truncated") is not False
        or metadata.get("events_path") != witness.events_path
        or metadata.get("capture_complete") is not witness.capture_complete
    ):
        return None
    return witness


def logical_turn_for_failure(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    intent: CursorSdkReconciliationIntentV1,
) -> LogicalTurn | None:
    """Key one logical turn by its unwrapped base prompt and review binding."""

    base = intent.prompt_sha256
    try:
        from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
            authenticate_sdk_successor_authority,
        )

        parent = authenticate_sdk_successor_authority(store, artifacts, conn, intent.run_id)
    except (OSError, ProtectedArtifactError, SchedulerEngineError, ValidationError, ValueError):
        return None
    if parent is not None:
        if parent.turn_kind == intent.turn_kind and parent.iteration == intent.iteration:
            if parent.effective_prompt_sha256 == intent.prompt_sha256:
                base = parent.base_prompt_sha256
        elif parent.effective_prompt_sha256 == intent.prompt_sha256:
            return None
    fix_sha, review_sha = _review_binding(intent)
    if intent.turn_kind == "correction" and (fix_sha is None or review_sha is None):
        return None
    if intent.turn_kind == "initial" and intent.reviews_completed != 0:
        return None
    reviews = 0 if intent.turn_kind == "initial" else intent.reviews_completed
    key = logical_turn_key(
        turn_kind=intent.turn_kind,
        conversation_key=intent.conversation_key,
        base_prompt_sha256=base,
        reviews_completed=reviews,
        fix_prompt_sha256=None if intent.turn_kind == "initial" else fix_sha,
        review_result_sha256=None if intent.turn_kind == "initial" else review_sha,
    )
    return LogicalTurn(
        key=key,
        turn_kind=intent.turn_kind,
        conversation_key=intent.conversation_key,
        base_prompt_sha256=base,
        reviews_completed=reviews,
        fix_prompt_sha256=None if intent.turn_kind == "initial" else fix_sha,
        review_result_sha256=None if intent.turn_kind == "initial" else review_sha,
    )


def _publish_one(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    row: sqlite3.Row,
    now: datetime,
) -> TickRunReceipt | None:
    authorization = _authorization_from_row(row)
    if authorization is None:
        return None
    if now < _instant(str(row["due_at"])):
        return None
    status = str(row["status"])
    if status == "reserved":
        publishing = authorization.model_copy(update={"status": "publishing"})
        payload = publishing.canonical_bytes()
        with store.begin_immediate() as conn:
            claimed = store.update_cursor_sdk_no_progress(
                conn,
                logical_turn_key=authorization.logical_turn_key,
                expected_statuses=("reserved",),
                status="publishing",
                successor_run_id=None,
                recovery_key=None,
                payload=payload.decode("utf-8"),
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                now=now,
            )
        if not claimed:
            with store.begin_read() as conn:
                current = store.get_cursor_sdk_no_progress(
                    conn, logical_turn_key=authorization.logical_turn_key
                )
            if current is None or str(current["status"]) != "publishing":
                return None
        status = "publishing"
    if status != "publishing":
        return None
    hook = publish_fault_hook
    if hook is not None:
        hook("after_claim")
    observed = _reobserve_phase(store, artifacts, authorization)
    if _matching_successor(store, authorization) is not None:
        if observed == "replay":
            return finish_no_progress_terminal_replay(store, artifacts, authorization, now)
        return _resume_existing_publication(store, artifacts, authorization, now)
    if observed == "pending":
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="reobserve",
        )
    decision = _revalidate(store, artifacts, authorization)
    if decision == "abort":
        _set_status(store, authorization, "cancelled", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_cancelled",
        )
    if decision is not None:
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail=decision,
        )
    if observed in {"absent", "pending"}:
        if observed == "absent":
            _schedule_reobserve(store, authorization, now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="reobserve",
        )
    if observed == "replay":
        return finish_no_progress_terminal_replay(store, artifacts, authorization, now)
    if observed != "send":
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail=observed,
        )
    sequence_gate = _sequence_ready_for_adoption(store, authorization.source_run_id)
    if sequence_gate == "wait":
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="sequence_leaf_pending",
        )
    if sequence_gate is not None:
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail=sequence_gate,
        )
    if hook is not None:
        hook("before_force")
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
        CursorSdkSuccessorService,
    )

    try:
        result = CursorSdkSuccessorService(store, artifacts, now_factory=lambda: now).force(
            authorization.source_run_id
        )
    except (SchedulerEngineError, ProtectedArtifactError, OSError, ValueError):
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail="publication",
        )
    if result.publication_status == "cancelled" or result.automatic_continuation:
        _set_status(
            store,
            authorization,
            "cancelled" if result.publication_status == "cancelled" else "reserved",
            now=now,
        )
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
        )
    if result.publication_status != "ready" or not result.agent_execution_ready:
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
        )
    if hook is not None:
        hook("after_force")
    recovery_key = _recovery_key_for(store, authorization.source_run_id, result.successor_run_id)
    published = authorization.model_copy(
        update={
            "status": "published",
            "successor_run_id": result.successor_run_id,
            "recovery_key": recovery_key,
        }
    )
    payload = published.canonical_bytes()
    with store.begin_immediate() as conn:
        store.update_cursor_sdk_no_progress(
            conn,
            logical_turn_key=authorization.logical_turn_key,
            expected_statuses=("publishing", "reserved"),
            status="published",
            successor_run_id=result.successor_run_id,
            recovery_key=recovery_key,
            payload=payload.decode("utf-8"),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            now=now,
        )
    return TickRunReceipt(
        run_id=result.successor_run_id,
        action="cursor_sdk_no_progress_published",
    )


def _reobserve_phase(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
) -> Literal["absent", "pending", "send", "replay", "hold"]:
    """Classify the owned fresh read without calling the SDK in this process."""

    with store.begin_read() as conn:
        recovery_key = _recovery_key_for_attempt(
            store, conn, source_attempt_id=authorization.source_attempt_id
        )
        if recovery_key is None:
            return "hold"
        if _reobserve_busy(conn, authorization.source_run_id, recovery_key):
            return "pending"
        attempt = _latest_reobserve_attempt(conn, authorization.source_run_id, recovery_key)
        if attempt is None:
            return "absent"
        if str(attempt["status"]) not in {"completed", "failed"} or int(attempt["ingested"]) != 1:
            return "pending"
        record = _record_from_reobserve_attempt(
            store,
            artifacts,
            conn,
            run_id=authorization.source_run_id,
            attempt=attempt,
        )
    if record is None or record.source_attempt_id != authorization.source_attempt_id:
        return "hold"
    return _record_reobserve_kind(record)


def _schedule_reobserve(
    store: SqliteSchedulerStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
    now: datetime,
) -> None:
    from ai_dev_loop.scheduler.domain.cursor_contract import RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND

    with store.begin_immediate() as conn:
        relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=authorization.source_attempt_id
        )
        if relation is None or str(relation["status"]) != "ready":
            return
        recovery_key = str(relation["recovery_key"])
        if _reobserve_busy(conn, authorization.source_run_id, recovery_key):
            return
        if _latest_reobserve_attempt(conn, authorization.source_run_id, recovery_key) is not None:
            return
        state, version, _ = store.load_validated_snapshot(conn, authorization.source_run_id)
        if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
            return
        event = conn.execute(
            """
            SELECT event_id FROM scheduler_events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (authorization.source_run_id,),
        ).fetchone()
        if event is None:
            return
        store.insert_effect(
            conn,
            dispatch_id=f"np-reobserve-{secrets.token_hex(8)}",
            source_event_id=str(event["event_id"]),
            run_id=authorization.source_run_id,
            effect_id=f"{NO_PROGRESS_REOBSERVE_EFFECT_ID}-{authorization.source_attempt_id}",
            effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
            effect_payload={
                "recovery_key": recovery_key,
                "no_progress_reobserve": True,
            },
            available_at=now,
            claimed_run_version=version,
            now=now,
        )


def _recovery_key_for_attempt(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_attempt_id: str,
) -> str | None:
    relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
        conn, source_attempt_id=source_attempt_id
    )
    if relation is None or str(relation["status"]) != "ready":
        return None
    key = str(relation["recovery_key"] or "")
    return key or None


def _reobserve_busy(conn: sqlite3.Connection, run_id: str, recovery_key: str) -> bool:
    from ai_dev_loop.scheduler.domain.cursor_contract import RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND

    effect = conn.execute(
        """
        SELECT 1 FROM scheduler_effects
        WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed')
          AND json_extract(effect_payload, '$.no_progress_reobserve') = 1
          AND json_extract(effect_payload, '$.recovery_key') = ?
        LIMIT 1
        """,
        (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND, recovery_key),
    ).fetchone()
    if effect is not None:
        return True
    attempt = conn.execute(
        """
        SELECT 1 FROM scheduler_attempts
        JOIN scheduler_effects
          ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
        WHERE scheduler_attempts.run_id = ?
          AND scheduler_attempts.status IN ('launching', 'active', 'uncertain')
          AND json_extract(scheduler_effects.effect_payload, '$.no_progress_reobserve') = 1
          AND json_extract(scheduler_effects.effect_payload, '$.recovery_key') = ?
        LIMIT 1
        """,
        (run_id, recovery_key),
    ).fetchone()
    return attempt is not None


def _latest_reobserve_attempt(
    conn: sqlite3.Connection,
    run_id: str,
    recovery_key: str,
) -> sqlite3.Row | None:
    from ai_dev_loop.scheduler.domain.cursor_contract import RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND

    return cast(
        sqlite3.Row | None,
        conn.execute(
            """
            SELECT scheduler_attempts.*
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ?
              AND scheduler_effects.effect_kind = ?
              AND json_extract(scheduler_effects.effect_payload, '$.no_progress_reobserve') = 1
              AND json_extract(scheduler_effects.effect_payload, '$.recovery_key') = ?
            ORDER BY scheduler_attempts.rowid DESC
            LIMIT 1
            """,
            (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND, recovery_key),
        ).fetchone(),
    )


def _record_reobserve_kind(
    record: ReconciliationRecord,
) -> Literal["send", "replay", "hold"]:
    if (
        record.outcome == "terminal_recovered"
        and (record.final_native_status or "").lower() == "finished"
        and record.cursor_output_fingerprint_path
        and record.cursor_output_fingerprint_sha256
        and record.terminal_response_path
        and record.terminal_response_sha256
    ):
        return "replay"
    if (
        record.outcome == "continuation_ready"
        and record.final_native_status == "error"
        and not record.cancellation_requested
        and record.native_run_id
    ):
        return "send"
    return "hold"


def _record_from_reobserve_attempt(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt: sqlite3.Row,
) -> ReconciliationRecord | None:
    from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
        _authenticate_reconciliation_record,
    )

    try:
        stdout_rel = str(attempt["stdout_artifact_path"] or "")
        if not stdout_rel:
            return None
        outcome = json.loads(
            resolve_run_relative_path(artifacts.run_root(run_id), stdout_rel).read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(outcome, dict):
            return None
        record_path = str(outcome.get("reconciliation_record_path") or "")
        record_sha = str(outcome.get("reconciliation_record_sha256") or "")
        if not record_path or not record_sha:
            return None
        dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
        if dispatch is None:
            return None
        payload = json.loads(str(dispatch["effect_payload"]))
        if not isinstance(payload, dict) or not isinstance(payload.get("recovery_key"), str):
            return None
        relation = store.get_cursor_sdk_reconciliation_by_key(
            conn, recovery_key=str(payload["recovery_key"])
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        return None
    if relation is None:
        return None
    try:
        intent = CursorSdkReconciliationIntentV1.model_validate(
            json.loads(str(relation["intent_payload"]))
        )
        return _authenticate_reconciliation_record(
            artifacts,
            run_id=run_id,
            intent=intent,
            record_path=record_path,
            record_sha256=record_sha,
        )
    except (ProtectedArtifactError, OSError, ValidationError, ValueError, TypeError):
        return None


def consume_no_progress_reobserve(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
    now: datetime,
) -> Literal["replayed", "sequence", "send", "held"]:
    """Apply one owned reobserve without rewriting the historical reconciliation."""

    from ai_dev_loop.scheduler.application.usage_ingestion import bind_usage_artifacts

    bind_usage_artifacts(store, artifacts)
    state, version, _ = store.load_validated_snapshot(conn, run_id)
    kind = _record_reobserve_kind(record)
    store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
    if kind == "send":
        return "send"
    if kind != "replay" or not isinstance(state, BlockedState):
        _transition_authorization(store, conn, run_id, "held", now)
        return "held"
    if state.context.sequence is not None:
        return "sequence"
    if not _apply_standalone_replay(
        store,
        artifacts,
        conn,
        state=state,
        version=version,
        intent=intent,
        record=record,
        now=now,
    ):
        _transition_authorization(store, conn, run_id, "held", now)
        return "held"
    return "replayed"


def finish_no_progress_terminal_replay(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
    now: datetime,
) -> TickRunReceipt:
    """Ingest a finished native replay through staging, review, and sequence handoff."""

    with store.begin_read() as conn:
        recovery_key = _recovery_key_for_attempt(
            store, conn, source_attempt_id=authorization.source_attempt_id
        )
        attempt = (
            None
            if recovery_key is None
            else _latest_reobserve_attempt(conn, authorization.source_run_id, recovery_key)
        )
        if attempt is None:
            record = None
            intent = None
        else:
            record = _record_from_reobserve_attempt(
                store,
                artifacts,
                conn,
                run_id=authorization.source_run_id,
                attempt=attempt,
            )
            relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
                conn, source_attempt_id=authorization.source_attempt_id
            )
            intent = None
            if relation is not None:
                try:
                    intent = CursorSdkReconciliationIntentV1.model_validate(
                        json.loads(str(relation["intent_payload"]))
                    )
                except (ValidationError, ValueError):
                    intent = None
        state, _, _ = store.load_validated_snapshot(conn, authorization.source_run_id)
    if (
        record is None
        or intent is None
        or record.source_attempt_id != authorization.source_attempt_id
        or _record_reobserve_kind(record) != "replay"
    ):
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail="replay",
        )
    if state.context.sequence is not None:
        return _finish_sequence_replay(
            store,
            artifacts,
            authorization,
            intent=intent,
            record=record,
            now=now,
        )
    with store.begin_immediate() as conn:
        current, version, _ = store.load_validated_snapshot(conn, authorization.source_run_id)
        if (
            isinstance(current, CursorReadyState)
            and current.cursor.cursor_output_fingerprint_sha256
            == record.cursor_output_fingerprint_sha256
        ):
            _transition_authorization(store, conn, authorization.source_run_id, "cancelled", now)
            return TickRunReceipt(
                run_id=authorization.source_run_id,
                action="cursor_sdk_no_progress_replayed",
            )
        if not isinstance(current, BlockedState):
            _transition_authorization(store, conn, authorization.source_run_id, "held", now)
            return TickRunReceipt(
                run_id=authorization.source_run_id,
                action="cursor_sdk_no_progress_held",
                detail="replay",
            )
        applied = _apply_standalone_replay(
            store,
            artifacts,
            conn,
            state=current,
            version=version,
            intent=intent,
            record=record,
            now=now,
        )
    if not applied:
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail="replay",
        )
    return TickRunReceipt(
        run_id=authorization.source_run_id,
        action="cursor_sdk_no_progress_replayed",
    )


def _finish_sequence_replay(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
    *,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
    now: datetime,
) -> TickRunReceipt:
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import CursorSdkSuccessorService

    if _matching_successor(store, authorization) is None:
        gate = _sequence_ready_for_adoption(store, authorization.source_run_id)
        if gate == "wait":
            return TickRunReceipt(
                run_id=authorization.source_run_id,
                action="cursor_sdk_no_progress_not_published",
                detail="sequence_leaf_pending",
            )
        if gate is not None:
            _set_status(store, authorization, "held", now=now)
            return TickRunReceipt(
                run_id=authorization.source_run_id,
                action="cursor_sdk_no_progress_held",
                detail=gate,
            )
        try:
            CursorSdkSuccessorService(store, artifacts, now_factory=lambda: now).force(
                authorization.source_run_id
            )
        except (SchedulerEngineError, ProtectedArtifactError, OSError, ValueError):
            return TickRunReceipt(
                run_id=authorization.source_run_id,
                action="cursor_sdk_no_progress_not_published",
                detail="replay",
            )
    successor = _matching_successor(store, authorization)
    if successor is None:
        _set_status(store, authorization, "held", now=now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_held",
            detail="replay",
        )
    successor_id = str(successor["successor_run_id"])
    with store.begin_read() as conn:
        successor_state, _, _ = store.load_validated_snapshot(conn, successor_id)
    if successor_state.kind == "aborted":
        _set_status(store, authorization, "cancelled", now=now)
        return TickRunReceipt(
            run_id=successor_id,
            action="cursor_sdk_no_progress_cancelled",
        )
    _fence_replay_dispatch(store, successor_id, now)
    if not _copy_replay_artifacts(artifacts, authorization.source_run_id, successor_id, record):
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="replay_install",
        )
    with store.begin_immediate() as conn:
        if not _install_replay_turn(
            store,
            artifacts,
            conn,
            run_id=successor_id,
            intent=intent,
            record=record,
            now=now,
            drop_run_turn=True,
        ):
            applied = False
        else:
            _transition_authorization(store, conn, authorization.source_run_id, "cancelled", now)
            applied = True
    if not applied:
        _fence_replay_dispatch(store, successor_id, now)
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="replay_install",
        )
    return TickRunReceipt(
        run_id=successor_id,
        action="cursor_sdk_no_progress_replayed",
    )


def _apply_standalone_replay(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    state: BlockedState,
    version: int,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
    now: datetime,
) -> bool:
    if not _install_replay_turn(
        store,
        artifacts,
        conn,
        run_id=state.run_id,
        intent=intent,
        record=record,
        now=now,
        drop_run_turn=False,
        source_state=intent.source_state if isinstance(intent.source_state, dict) else None,
    ):
        return False
    _transition_authorization(store, conn, state.run_id, "cancelled", now)
    return True


def _install_replay_turn(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    intent: CursorSdkReconciliationIntentV1,
    record: ReconciliationRecord,
    now: datetime,
    drop_run_turn: bool,
    source_state: dict[str, object] | None = None,
) -> bool:
    from pathlib import Path

    from ai_dev_loop.scheduler.application.cursor_evidence import (
        CursorEvidenceError,
        verify_recorded_cursor_fingerprint,
    )
    from ai_dev_loop.scheduler.application.run_state_bridge import run_state_from_scheduler_context
    from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
    from ai_dev_loop.scheduler.domain.cursor_contract import (
        NORMALIZE_STAGING_EFFECT_ID,
        NORMALIZE_STAGING_EFFECT_KIND,
        RUN_CURSOR_TURN_EFFECT_KIND,
    )
    from ai_dev_loop.scheduler.domain.events import CursorSdkTurnCompletedEvent
    from ai_dev_loop.scheduler.domain.state import CursorReadyState, WaitingForCursorFixState
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
        EFFECT_STATUS_CANCELLED,
        TIMER_STATUS_CANCELLED,
        TIMER_STATUS_PENDING,
    )

    current, version, _ = store.load_validated_snapshot(conn, run_id)
    fingerprint_sha = record.cursor_output_fingerprint_sha256 or ""
    current_cursor = getattr(current, "cursor", None)
    already = (
        isinstance(current, CursorReadyState)
        and current_cursor is not None
        and current.cursor.cursor_output_fingerprint_sha256 == fingerprint_sha
    )
    if not already:
        if source_state is None:
            base: CursorReadyState | WaitingForCursorFixState
            if isinstance(current, (CursorReadyState, WaitingForCursorFixState)):
                base = current
            else:
                return False
        else:
            try:
                base = (
                    WaitingForCursorFixState.model_validate(source_state)
                    if intent.turn_kind == "correction"
                    else CursorReadyState.model_validate(source_state)
                )
            except ValidationError:
                return False
        ready = _ready_with_fingerprint(
            base,
            record,
            version=version + 1,
            now=now,
            run_id=run_id,
        )
        try:
            run_state = run_state_from_scheduler_context(
                run_id=run_id,
                context=ready.context,
                repo_root=Path(ready.context.repository.root),
                checkpoint=checkpoint_from_state(ready),
                artifacts=artifacts,
            )
            verify_recorded_cursor_fingerprint(
                run_state,
                artifacts.run_root(run_id),
                iteration_number=ready.cursor.iteration,
                recorded_path=record.cursor_output_fingerprint_path or "",
                recorded_sha256=fingerprint_sha,
            )
        except (CursorEvidenceError, OSError, ValueError, ValidationError):
            return False
        if not store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=ready,
            now=now,
        ):
            return False
        turn_event = CursorSdkTurnCompletedEvent(
            run_id=run_id,
            iteration=intent.iteration,
            native_run_id=record.native_run_id or "",
            journal_path=intent.source_journal_path,
            journal_sha256=intent.source_journal_sha256,
            cursor_output_fingerprint_path=record.cursor_output_fingerprint_path or "",
            cursor_output_fingerprint_sha256=fingerprint_sha,
        )
        store.append_event(
            conn,
            event_id=f"evt-{secrets.token_hex(16)}",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=turn_event,
            now=now,
        )
        version = ready.version
    if drop_run_turn:
        now_text = encode_utc_instant(now)
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, updated_at = ?,
                claim_id = NULL, claim_owner_id = NULL,
                claim_lease_generation = NULL, claimed_at = NULL
            WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed', 'retry_wait')
            """,
            (EFFECT_STATUS_CANCELLED, now_text, run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        )
        conn.execute(
            """
            UPDATE scheduler_timers
            SET status = ?, updated_at = ?
            WHERE run_id = ? AND status = ?
            """,
            (TIMER_STATUS_CANCELLED, now_text, run_id, TIMER_STATUS_PENDING),
        )
    pending_normalize = conn.execute(
        """
        SELECT 1 FROM scheduler_effects
        WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed')
        LIMIT 1
        """,
        (run_id, NORMALIZE_STAGING_EFFECT_KIND),
    ).fetchone()
    if pending_normalize is None:
        source_event = conn.execute(
            """
            SELECT event_id FROM scheduler_events
            WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if source_event is None:
            return False
        occupied = conn.execute(
            "SELECT 1 FROM scheduler_effects WHERE run_id = ? AND effect_id = ?",
            (run_id, NORMALIZE_STAGING_EFFECT_ID),
        ).fetchone()
        effect_id = (
            NO_PROGRESS_REPLAY_STAGING_EFFECT_ID
            if occupied is not None
            else NORMALIZE_STAGING_EFFECT_ID
        )
        store.insert_effect(
            conn,
            dispatch_id=f"np-replay-{secrets.token_hex(8)}",
            source_event_id=str(source_event["event_id"]),
            run_id=run_id,
            effect_id=effect_id,
            effect_kind=NORMALIZE_STAGING_EFFECT_KIND,
            effect_payload={"iteration": intent.iteration},
            available_at=now,
            claimed_run_version=version,
            now=now,
        )
    return True


def _ready_with_fingerprint(
    base: CursorReadyState | WaitingForCursorFixState,
    record: ReconciliationRecord,
    *,
    version: int,
    now: datetime,
    run_id: str,
) -> CursorReadyState:
    payload = base.model_dump(mode="json")
    payload["kind"] = "cursor_ready"
    payload["run_id"] = run_id
    payload["version"] = version
    payload["updated_at"] = encode_utc_instant(now)
    cursor = payload.get("cursor")
    if not isinstance(cursor, dict):
        raise ValueError("replay source has no cursor checkpoint")
    cursor["cursor_output_fingerprint_path"] = record.cursor_output_fingerprint_path
    cursor["cursor_output_fingerprint_sha256"] = record.cursor_output_fingerprint_sha256
    cursor["sdk_recovery_pending"] = None
    payload["cursor"] = cursor
    from ai_dev_loop.scheduler.domain.state import CursorReadyState

    return CursorReadyState.model_validate(payload)


def _fence_replay_dispatch(
    store: SqliteSchedulerStore,
    successor_id: str,
    now: datetime,
) -> None:
    """Keep a replay successor from sending until recovered output is installed."""

    from ai_dev_loop.scheduler.domain.cursor_contract import RUN_CURSOR_TURN_EFFECT_KIND
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import EFFECT_STATUS_CANCELLED

    now_text = encode_utc_instant(now)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = ?, updated_at = ?,
                claim_id = NULL, claim_owner_id = NULL,
                claim_lease_generation = NULL, claimed_at = NULL
            WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed', 'retry_wait')
            """,
            (EFFECT_STATUS_CANCELLED, now_text, successor_id, RUN_CURSOR_TURN_EFFECT_KIND),
        )


def schedule_lost_reobserve_replacement(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    recovery_key: str,
    lost_attempt_id: str,
    now: datetime,
) -> None:
    """Schedule one more owned read after a lost reobserve, on the same allowance."""

    from ai_dev_loop.scheduler.domain.cursor_contract import RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
    from ai_dev_loop.scheduler.domain.events import CursorSdkReconciliationRequestedEvent

    if _reobserve_busy(conn, run_id, recovery_key):
        return
    relation = store.get_cursor_sdk_reconciliation_by_key(conn, recovery_key=recovery_key)
    if relation is None or str(relation["status"]) != "ready" or str(relation["run_id"]) != run_id:
        return
    state, version, _ = store.load_validated_snapshot(conn, run_id)
    if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
        return
    event = CursorSdkReconciliationRequestedEvent(
        run_id=run_id,
        recovery_key=recovery_key,
        source_attempt_id=str(relation["source_attempt_id"]),
        source_dispatch_id=str(relation["source_dispatch_id"]),
    )
    event_id = f"evt-{secrets.token_hex(16)}"
    store.append_event(
        conn,
        event_id=event_id,
        run_id=run_id,
        sequence=store.next_event_sequence(conn, run_id),
        event=event,
        now=now,
    )
    store.insert_effect(
        conn,
        dispatch_id=f"np-reobserve-{secrets.token_hex(8)}",
        source_event_id=event_id,
        run_id=run_id,
        effect_id=f"{NO_PROGRESS_REOBSERVE_EFFECT_ID}-{lost_attempt_id}",
        effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
        effect_payload={
            "recovery_key": recovery_key,
            "no_progress_reobserve": True,
        },
        available_at=now,
        claimed_run_version=version,
        now=now,
    )


def _copy_replay_artifacts(
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
    successor_run_id: str,
    record: ReconciliationRecord,
) -> bool:
    """Copy the replayed terminal output onto the adopted sequence leaf.

    The fingerprint field is the Git aggregate inside the artifact, not the
    file digest. The terminal response digest is the file digest.
    """

    fingerprint_path = record.cursor_output_fingerprint_path or ""
    fingerprint_sha = record.cursor_output_fingerprint_sha256 or ""
    response_path = record.terminal_response_path or ""
    response_sha = record.terminal_response_sha256 or ""
    if not fingerprint_path or not fingerprint_sha or not response_path or not response_sha:
        return False
    try:
        fingerprint_file = resolve_run_relative_path(
            artifacts.run_root(source_run_id), fingerprint_path
        )
        fingerprint = fingerprint_file.read_bytes()
        payload = json.loads(fingerprint)
        if not isinstance(payload, dict) or payload.get("aggregate_sha256") != fingerprint_sha:
            return False
        response = artifacts.read_verified_bytes(
            source_run_id,
            response_path,
            expected_sha256=response_sha,
        )
        artifacts.write_bytes_or_verify(
            successor_run_id,
            fingerprint_path,
            fingerprint,
            max_bytes=8 * 1024 * 1024,
        )
        artifacts.write_bytes_or_verify(
            successor_run_id,
            response_path,
            response,
            max_bytes=8 * 1024 * 1024,
        )
    except (ProtectedArtifactError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    return True


def _transition_authorization(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    status: Literal["held", "cancelled"],
    now: datetime,
) -> None:
    row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    if row is None or str(row["status"]) not in {"reserved", "publishing"}:
        return
    authorization = _authorization_from_row(row)
    if authorization is None:
        return
    updated = authorization.model_copy(update={"status": status})
    payload = updated.canonical_bytes()
    store.update_cursor_sdk_no_progress(
        conn,
        logical_turn_key=authorization.logical_turn_key,
        expected_statuses=("reserved", "publishing"),
        status=status,
        successor_run_id=authorization.successor_run_id,
        recovery_key=authorization.recovery_key,
        payload=payload.decode("utf-8"),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        now=now,
    )


def _matching_successor(
    store: SqliteSchedulerStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
) -> sqlite3.Row | None:
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(
            conn, source_run_id=authorization.source_run_id
        )
    matches = []
    for row in rows:
        try:
            payload = json.loads(str(row["intent_payload"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("schema_version") != 3:
            continue
        if str(row["status"]) not in {"pending", "ready"}:
            continue
        if str(row["failed_attempt_id"]) != authorization.source_attempt_id:
            continue
        matches.append(row)
    if len(matches) != 1:
        return None
    return matches[0]


def _resume_existing_publication(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
    now: datetime,
) -> TickRunReceipt:
    """Bind a successor that already exists before source-ownership checks."""

    from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
        CursorSdkSuccessorService,
    )

    try:
        result = CursorSdkSuccessorService(store, artifacts, now_factory=lambda: now).force(
            authorization.source_run_id
        )
    except (SchedulerEngineError, ProtectedArtifactError, OSError, ValueError):
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="existing_successor",
        )
    if result.publication_status == "cancelled" or result.automatic_continuation:
        _set_status(
            store,
            authorization,
            "cancelled" if result.publication_status == "cancelled" else "reserved",
            now=now,
        )
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
        )
    if result.publication_status != "ready" or not result.agent_execution_ready:
        return TickRunReceipt(
            run_id=authorization.source_run_id,
            action="cursor_sdk_no_progress_not_published",
            detail="existing_successor",
        )
    hook = publish_fault_hook
    if hook is not None:
        hook("after_force")
    recovery_key = _recovery_key_for(store, authorization.source_run_id, result.successor_run_id)
    published = authorization.model_copy(
        update={
            "status": "published",
            "successor_run_id": result.successor_run_id,
            "recovery_key": recovery_key,
        }
    )
    payload = published.canonical_bytes()
    with store.begin_immediate() as conn:
        store.update_cursor_sdk_no_progress(
            conn,
            logical_turn_key=authorization.logical_turn_key,
            expected_statuses=("publishing", "reserved"),
            status="published",
            successor_run_id=result.successor_run_id,
            recovery_key=recovery_key,
            payload=payload.decode("utf-8"),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            now=now,
        )
    return TickRunReceipt(
        run_id=result.successor_run_id,
        action="cursor_sdk_no_progress_published",
    )


def _revalidate(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
) -> str | None:
    with store.begin_read() as conn:
        if store.has_abort_requested_for_run(conn, authorization.source_run_id):
            return "abort"
        state, _, _ = store.load_validated_snapshot(conn, authorization.source_run_id)
        if (
            not isinstance(state, BlockedState)
            or state.block_reason_kind != "cursor_failure"
            or store.get_reservation_for_run(conn, authorization.source_run_id) is None
            or store.has_unresolved_abort_hold(conn, authorization.source_run_id)
        ):
            return "authority"
        if not _current_leaf(store, conn, state):
            return "stale_leaf"
        if _conversation_disposed(conn, authorization.conversation_key):
            return "disposed"
        relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=authorization.source_attempt_id
        )
        if relation is None or str(relation["status"]) != "ready":
            return "reconciliation"
        intent_raw = json.loads(str(relation["intent_payload"]))
        intent = CursorSdkReconciliationIntentV1.model_validate(intent_raw)
        result_path = str(relation["result_artifact_path"] or "")
        result_sha = str(relation["result_sha256"] or "")
    if not result_path or not result_sha:
        return "reconciliation"
    try:
        payload = artifacts.read_verified_bytes(
            authorization.source_run_id,
            result_path,
            expected_sha256=result_sha,
        )
        raw_record = json.loads(payload)
        version = raw_record.get("schema_version") if isinstance(raw_record, dict) else None
        if version == 2:
            record: ReconciliationRecord = CursorSdkReconciliationRecordV2.model_validate(
                raw_record
            )
        elif version == 1:
            record = CursorSdkReconciliationRecordV1.model_validate(raw_record)
        else:
            return "reconciliation"
    except (ProtectedArtifactError, OSError, ValidationError, ValueError, TypeError):
        return "reconciliation"
    if not _native_failure_is_exact(record) or record.outcome != "continuation_ready":
        return "native"
    with store.begin_read() as conn:
        witness = authenticate_no_progress_witness(
            store,
            artifacts,
            conn,
            run_id=authorization.source_run_id,
            intent=intent,
            record=record,
        )
    if witness is None:
        return "witness"
    witness_sha = hashlib.sha256(witness.canonical_bytes()).hexdigest()
    if witness_sha != authorization.witness_sha256:
        return "witness"
    if not isinstance(state, BlockedState):
        return "authority"
    return None


def _set_status(
    store: SqliteSchedulerStore,
    authorization: CursorSdkNoProgressAuthorizationV1,
    status: Literal["reserved", "publishing", "published", "held", "cancelled"],
    *,
    now: datetime,
) -> None:
    updated = authorization.model_copy(update={"status": status})
    payload = updated.canonical_bytes()
    with store.begin_immediate() as conn:
        store.update_cursor_sdk_no_progress(
            conn,
            logical_turn_key=authorization.logical_turn_key,
            expected_statuses=("reserved", "publishing"),
            status=status,
            successor_run_id=authorization.successor_run_id,
            recovery_key=authorization.recovery_key,
            payload=payload.decode("utf-8"),
            payload_sha256=hashlib.sha256(payload).hexdigest(),
            now=now,
        )


def _recovery_key_for(
    store: SqliteSchedulerStore,
    source_run_id: str,
    successor_run_id: str,
) -> str:
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=source_run_id)
    for row in rows:
        if str(row["successor_run_id"]) == successor_run_id:
            return str(row["recovery_key"])
    raise ValueError("published SDK continuation is missing its recovery relation")


def _continuation_predicates(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
    intent: CursorSdkReconciliationIntentV1,
) -> bool:
    if store.has_abort_requested_for_run(conn, state.run_id):
        return False
    if store.has_unresolved_abort_hold(conn, state.run_id):
        return False
    if store.get_nonterminal_attempt_for_run(conn, state.run_id) is not None:
        return False
    if store.get_reservation_for_run(conn, state.run_id) is None:
        return False
    if _conversation_disposed(conn, intent.conversation_key):
        return False
    return _current_leaf(store, conn, state)


def _current_leaf(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
) -> bool:
    binding = state.context.sequence
    if binding is None:
        return True
    sequence = store.load_validated_sequence_state(conn, binding.sequence_id)
    return getattr(sequence, "current_run_id", None) == state.run_id


def _sequence_ready_for_adoption(
    store: SqliteSchedulerStore,
    run_id: str,
) -> str | None:
    """Wait until the existing sequence blocker can adopt this current leaf.

    An active sequence with this run still current is not stale. Publication
    retries after the ordinary leaf block. A different current run is stale.
    """

    from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState, BlockedSequenceState

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        binding = state.context.sequence
        if binding is None:
            return None
        sequence = store.load_validated_sequence_state(conn, binding.sequence_id)
    if isinstance(sequence, BlockedSequenceState) and sequence.current_run_id == run_id:
        return None
    if isinstance(sequence, ActiveSequenceState) and sequence.current_run_id == run_id:
        return "wait"
    return "stale_leaf"


def _conversation_disposed(conn: sqlite3.Connection, conversation_key: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'scheduler_cursor_sdk_disposals'
        """
    ).fetchone()
    if row is None:
        return False
    disposed = conn.execute(
        "SELECT 1 FROM scheduler_cursor_sdk_disposals WHERE conversation_key = ?",
        (conversation_key,),
    ).fetchone()
    return disposed is not None


def _native_failure_is_exact(record: ReconciliationRecord) -> bool:
    if record.outcome != "continuation_ready":
        return False
    if record.cancellation_requested or record.final_native_status != "error":
        return False
    if not record.native_run_id:
        return False
    return not record.candidate_run_ids or (
        len(record.candidate_run_ids) == 1 and record.candidate_run_ids[0] == record.native_run_id
    )


def _review_binding(
    intent: CursorSdkReconciliationIntentV1,
) -> tuple[str | None, str | None]:
    raw = intent.source_state
    if not isinstance(raw, dict):
        return None, None
    codex = raw.get("codex")
    if not isinstance(codex, dict):
        return None, None
    fix = codex.get("latest_fix_prompt_sha256")
    review = codex.get("latest_review_result_sha256")
    return (fix if isinstance(fix, str) else None, review if isinstance(review, str) else None)


def _matching_status_lines(
    payload: bytes,
    *,
    agent_id: str,
    native_run_id: str,
) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for line in payload.splitlines(keepends=True):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            continue
        body = parsed.get("payload")
        if not exact_status_no_progress(body, agent_id=agent_id, native_run_id=native_run_id):
            continue
        offset = parsed.get("offset")
        if not isinstance(offset, str) or not offset:
            continue
        matches.append((offset, hashlib.sha256(line).hexdigest()))
    return matches


def _authorization_from_row(row: sqlite3.Row) -> CursorSdkNoProgressAuthorizationV1 | None:
    try:
        payload = str(row["payload"]).encode("utf-8")
        if hashlib.sha256(payload).hexdigest() != str(row["payload_sha256"]):
            return None
        model = CursorSdkNoProgressAuthorizationV1.model_validate_json(payload)
    except (ValidationError, ValueError, UnicodeError):
        return None
    if (
        model.logical_turn_key != str(row["logical_turn_key"])
        or model.consumed_count != int(row["consumed_count"])
        or model.status != str(row["status"])
        or model.source_run_id != str(row["source_run_id"])
        or model.source_attempt_id != str(row["source_attempt_id"])
    ):
        return None
    if isinstance(model.consumed_count, bool) or model.consumed_count not in (0, 1):
        return None
    return model


def record_no_progress_decision(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    decision: NoProgressDecision,
    now: datetime,
) -> None:
    """Persist the safe decision after the block event preserves reconciliation adjacency."""

    _append_decision(
        store,
        conn,
        run_id=run_id,
        attempt_id=decision.attempt_id,
        action=decision.action,
        consumed_count=decision.consumed_count,
        due_at=decision.due_at,
        now=now,
    )


def _append_decision(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
    action: Literal["automatic", "exhausted", "manual"],
    consumed_count: int | None,
    due_at: str | None,
    now: datetime,
) -> None:
    event = CursorSdkNoProgressAuthorizedEvent(
        run_id=run_id,
        attempt_id=attempt_id,
        action=action,
        consumed_count=consumed_count,
        due_at=due_at,
    )
    store.append_event(
        conn,
        event_id=f"evt-{secrets.token_hex(16)}",
        run_id=run_id,
        sequence=store.next_event_sequence(conn, run_id),
        event=event,
        now=now,
    )


def _wait_action(run_id: str, due_at: str) -> SafeNextAction:
    return SafeNextAction(
        kind=SafeNextActionKind.WAIT_UNTIL,
        command=(
            "Cursor SDK resume-no-progress continuation for run "
            f"{run_id} is reserved until {due_at}. "
            "Run ai_dev_loop scheduler tick after that time. "
            "Do not force another successor during this wait."
        ),
    )


def _instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
