"""Reconcile active sequences with terminal materialized run outcomes."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from ai_dev_loop.scheduler.application.abort_reconcile import finalize_aborted_run_cleanup
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.domain.events import SequenceAbortedEvent, SequenceBlockedEvent
from ai_dev_loop.scheduler.domain.sequence import (
    ABORT_PENDING_SEQUENCE_STATE_KIND,
    ACTIVE_SEQUENCE_STATE_KIND,
    SEQUENCE_BLOCKING_RUN_TERMINAL_KINDS,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_TERMINAL_STATE_KINDS,
    AbortedState,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SequenceReconcileService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._now_factory = now_factory or (lambda: datetime.now(tz=UTC))
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")

    def reconcile_pending_sequences(self, conn: sqlite3.Connection) -> list[TickRunReceipt]:
        receipts: list[TickRunReceipt] = []
        for sequence_id in self.store.list_reconcilable_sequence_ids(conn):
            receipt = self.reconcile_sequence(conn, sequence_id)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def reconcile_run(self, conn: sqlite3.Connection, run_id: str) -> TickRunReceipt | None:
        row = conn.execute(
            """
            SELECT sequence_id
            FROM scheduler_sequences
            WHERE state_kind IN (?, ?)
            AND json_extract(payload, '$.current_run_id') = ?
            """,
            (ACTIVE_SEQUENCE_STATE_KIND, ABORT_PENDING_SEQUENCE_STATE_KIND, run_id),
        ).fetchone()
        if row is None:
            return None
        return self.reconcile_sequence(conn, str(row["sequence_id"]))

    def reconcile_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> TickRunReceipt | None:
        sequence_state = self.store.load_validated_sequence_state(conn, sequence_id)
        if isinstance(sequence_state, AbortPendingSequenceState):
            return self._reconcile_abort_pending(conn, sequence_state)
        if not isinstance(sequence_state, ActiveSequenceState):
            return None
        state, _, _ = self.store.load_validated_snapshot(conn, sequence_state.current_run_id)
        if state.kind not in SCHEDULER_TERMINAL_STATE_KINDS:
            return None
        if state.kind in {"completed", "completed_with_residual_risk"}:
            return None
        if state.kind not in SEQUENCE_BLOCKING_RUN_TERMINAL_KINDS:
            return None
        run_id = sequence_state.current_run_id
        if self._sequence_reconciliation_hold_pending(conn, run_id):
            return TickRunReceipt(
                run_id=run_id,
                action="sequence_reconciliation_pending",
            )
        if state.kind == "aborted" and isinstance(state, AbortedState):
            finalize_aborted_run_cleanup(
                self.store,
                conn,
                run_id=run_id,
                now=self._now_factory(),
            )
            if self._sequence_reconciliation_hold_pending(conn, run_id):
                return TickRunReceipt(
                    run_id=run_id,
                    action="sequence_reconciliation_pending",
                )
        return self._block_sequence(
            conn,
            sequence_state=sequence_state,
            run_id=run_id,
            block_reason_kind=state.kind,
            now=self._now_factory(),
        )

    def _sequence_reconciliation_hold_pending(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> bool:
        return self.store.has_unresolved_abort_hold(
            conn, run_id
        ) or self.store.has_checkpoint_reconciliation_hold(conn, run_id)

    def _reconcile_abort_pending(
        self,
        conn: sqlite3.Connection,
        sequence_state: AbortPendingSequenceState,
    ) -> TickRunReceipt | None:
        run_id = sequence_state.current_run_id
        state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, AbortedState):
            if state.kind in SCHEDULER_TERMINAL_STATE_KINDS:
                if self.store.has_unresolved_abort_hold(conn, run_id):
                    return TickRunReceipt(
                        run_id=run_id,
                        action="sequence_abort_termination_pending",
                    )
                if self.store.has_checkpoint_reconciliation_hold(conn, run_id):
                    return TickRunReceipt(
                        run_id=run_id,
                        action="sequence_abort_termination_pending",
                    )
                return self._complete_sequence_abort(
                    conn,
                    sequence_state=sequence_state,
                    run_id=run_id,
                    now=self._now_factory(),
                )
            return TickRunReceipt(run_id=run_id, action="sequence_abort_run_pending")
        if self.store.has_unresolved_abort_hold(conn, run_id):
            return TickRunReceipt(run_id=run_id, action="sequence_abort_termination_pending")
        finalize_aborted_run_cleanup(self.store, conn, run_id=run_id, now=self._now_factory())
        if self.store.has_unresolved_abort_hold(conn, run_id):
            return TickRunReceipt(run_id=run_id, action="sequence_abort_termination_pending")
        return self._complete_sequence_abort(
            conn,
            sequence_state=sequence_state,
            run_id=run_id,
            now=self._now_factory(),
        )

    def _release_sequence_reservation_if_owned(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        worktree_key: str,
        now: datetime,
    ) -> None:
        reservation = self.store.get_reservation_for_run(conn, run_id)
        if reservation is None:
            return
        if str(reservation["worktree_key"]) != worktree_key:
            return
        self.store.release_reservation(conn, worktree_key=worktree_key, now=now)

    def _block_sequence(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        run_id: str,
        block_reason_kind: str,
        now: datetime,
    ) -> TickRunReceipt:
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        blocked = BlockedSequenceState(
            schema_version=sequence_state.schema_version,
            sequence_id=sequence_state.sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            blocked_at=now_text,
            block_reason_kind=block_reason_kind,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            current_ordinal=sequence_state.current_ordinal,
            current_run_id=run_id,
            materialized_entries=sequence_state.materialized_entries,
            residual_risk_ordinals=sequence_state.residual_risk_ordinals,
        )
        blocked_event = SequenceBlockedEvent(
            sequence_id=sequence_state.sequence_id,
            current_run_id=run_id,
            block_reason_kind=block_reason_kind,
        )
        event_id = self._event_id_factory()
        sequence_num = self.store.next_event_sequence(conn, run_id)
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence_num,
            event=blocked_event,
            now=now,
        )
        if not self.store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_state.sequence_id,
            expected_version=sequence_state.version,
            new_state=blocked,
            now=now,
        ):
            return TickRunReceipt(run_id=run_id, action="sequence_block_cas_lost")
        worktree_key = blocked.definition.repository.worktree_key
        if not self._sequence_reconciliation_hold_pending(conn, run_id):
            self._release_sequence_reservation_if_owned(
                conn,
                run_id=run_id,
                worktree_key=worktree_key,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="sequence_blocked", detail=block_reason_kind)

    def _complete_sequence_abort(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: AbortPendingSequenceState,
        run_id: str,
        now: datetime,
    ) -> TickRunReceipt:
        refreshed = self.store.load_validated_sequence_state(conn, sequence_state.sequence_id)
        if isinstance(refreshed, AbortedSequenceState):
            return TickRunReceipt(run_id=run_id, action="sequence_aborted")
        if not isinstance(refreshed, AbortPendingSequenceState):
            return TickRunReceipt(run_id=run_id, action="sequence_abort_cas_lost")
        sequence_state = refreshed
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cancelled = sequence_state.cancelled_ordinals
        aborted = AbortedSequenceState(
            schema_version=sequence_state.schema_version,
            sequence_id=sequence_state.sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            aborted_at=now_text,
            abort_reason=sequence_state.abort_reason,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            current_ordinal=sequence_state.current_ordinal,
            current_run_id=run_id,
            materialized_entries=sequence_state.materialized_entries,
            residual_risk_ordinals=sequence_state.residual_risk_ordinals,
            cancelled_ordinals=cancelled,
        )
        aborted_event = SequenceAbortedEvent(
            sequence_id=sequence_state.sequence_id,
            reason=sequence_state.abort_reason,
            current_run_id=run_id,
            cancelled_ordinal_count=len(cancelled),
        )
        event_id = self._event_id_factory()
        sequence_num = self.store.next_event_sequence(conn, run_id)
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=run_id,
            sequence=sequence_num,
            event=aborted_event,
            now=now,
        )
        if not self.store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_state.sequence_id,
            expected_version=sequence_state.version,
            new_state=aborted,
            now=now,
        ):
            return TickRunReceipt(run_id=run_id, action="sequence_abort_cas_lost")
        worktree_key = aborted.definition.repository.worktree_key
        self._release_sequence_reservation_if_owned(
            conn,
            run_id=run_id,
            worktree_key=worktree_key,
            now=now,
        )
        return TickRunReceipt(run_id=run_id, action="sequence_aborted")
