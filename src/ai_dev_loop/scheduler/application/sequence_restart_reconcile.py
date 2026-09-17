"""Restart-safe reconciliation for interrupted sequence finalization and recovery adoption."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.review_recovery import analyze_blocked_review_recovery
from ai_dev_loop.scheduler.application.sequence_review_recovery import (
    authenticate_sequence_review_recovery_binding,
    find_blocked_sequence_for_run,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    CompletedState,
    CompletedWithResidualRiskState,
    PendingSequenceReviewRecoveryState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_execution_replacement_intent_store import (
    authenticate_durable_sequence_replacement_publication,
    load_validated_sequence_execution_replacement_intent,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

if TYPE_CHECKING:
    from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService


class SequenceRestartReconcileService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        handoff: SequenceHandoffService | None = None,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._handoff = handoff
        self._now_factory = now_factory or (lambda: datetime.now(tz=UTC))

    def reconcile_pending_restart_work(self, conn: sqlite3.Connection) -> list[TickRunReceipt]:
        receipts: list[TickRunReceipt] = []
        for row in self.store.list_unpublished_sequence_execution_replacement_intents(conn):
            receipt = self.reconcile_unpublished_replacement_intent(
                conn,
                source_run_id=str(row["source_run_id"]),
                recovery_key=str(row["recovery_key"]),
            )
            if receipt is not None:
                receipts.append(receipt)
        for run_id in self.store.list_pending_sequence_review_recovery_run_ids(conn):
            receipt = self.reconcile_pending_review_recovery_successor(conn, run_id)
            if receipt is not None:
                receipts.append(receipt)
        for run_id in self.store.list_sequence_terminal_current_run_ids(conn):
            receipt = self.reconcile_terminal_current_leaf(conn, run_id)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def reconcile_run(self, conn: sqlite3.Connection, run_id: str) -> TickRunReceipt | None:
        receipt = self.reconcile_pending_review_recovery_successor(conn, run_id)
        if receipt is not None:
            return receipt
        return self.reconcile_terminal_current_leaf(conn, run_id)

    def reconcile_unpublished_replacement_intent(
        self,
        conn: sqlite3.Connection,
        *,
        source_run_id: str,
        recovery_key: str,
    ) -> TickRunReceipt | None:
        from ai_dev_loop.scheduler.application.sequence_review_recovery import (
            resume_sequence_execution_replacement_intent,
        )

        return resume_sequence_execution_replacement_intent(
            self.store,
            self.artifacts,
            conn=conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            now=self._now_factory(),
        )

    def reconcile_pending_review_recovery_successor(
        self,
        conn: sqlite3.Connection,
        successor_run_id: str,
    ) -> TickRunReceipt | None:
        state, _, _ = self.store.load_validated_snapshot(conn, successor_run_id)
        if not isinstance(state, PendingSequenceReviewRecoveryState):
            return None
        row = conn.execute(
            """
            SELECT source_run_id, recovery_key
            FROM scheduler_review_recovery_successors
            WHERE successor_run_id = ?
            LIMIT 1
            """,
            (successor_run_id,),
        ).fetchone()
        if row is None:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_successor_orphan",
            )
        source_run_id = str(row["source_run_id"])
        recovery_key = str(row["recovery_key"])
        blocked_sequence = find_blocked_sequence_for_run(self.store, conn, source_run_id)
        if blocked_sequence is None:
            sequence_row = conn.execute(
                """
                SELECT sequence_id, state_kind
                FROM scheduler_sequences
                WHERE json_extract(payload, '$.current_run_id') = ?
                LIMIT 1
                """,
                (successor_run_id,),
            ).fetchone()
            if sequence_row is not None and str(sequence_row["state_kind"]) == "active":
                return TickRunReceipt(
                    run_id=successor_run_id,
                    action="sequence_recovery_adoption_complete",
                )
            return None
        validated = load_validated_sequence_execution_replacement_intent(
            self.store,
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if validated is None:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_intent_missing",
            )
        if validated.is_cancelled:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_intent_cancelled",
            )
        if authenticate_durable_sequence_replacement_publication(
            self.store, conn, validated=validated
        ):
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_adoption_complete",
            )
        if validated.intent.successor_run_id != successor_run_id:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_successor_mismatch",
            )
        now = self._now_factory()
        try:
            evidence = analyze_blocked_review_recovery(
                self.store,
                self.artifacts,
                source_run_id,
            )
        except SchedulerEngineError:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_evidence_invalid",
            )
        recovery_binding = authenticate_sequence_review_recovery_binding(
            self.store,
            conn,
            source_run_id=source_run_id,
            context=evidence.context,
            blocked_sequence=blocked_sequence,
        )
        from ai_dev_loop.scheduler.application.sequence_review_recovery import (
            _adopt_sequence_recovery_successor,
        )

        adopted = _adopt_sequence_recovery_successor(
            self.store,
            evidence=evidence,
            source_run_id=source_run_id,
            blocked_sequence=blocked_sequence,
            recovery_binding=recovery_binding,
            successor_run_id=successor_run_id,
            recovery_key=recovery_key,
            now=now,
            conn=conn,
        )
        if adopted:
            return TickRunReceipt(
                run_id=successor_run_id,
                action="sequence_recovery_adoption_reconciled",
            )
        return TickRunReceipt(
            run_id=successor_run_id,
            action="sequence_recovery_adoption_complete",
        )

    def reconcile_terminal_current_leaf(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> TickRunReceipt | None:
        sequence_row = conn.execute(
            """
            SELECT sequence_id
            FROM scheduler_sequences
            WHERE state_kind = 'active'
              AND json_extract(payload, '$.current_run_id') = ?
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if sequence_row is None:
            return None
        sequence_state = self.store.load_validated_sequence_state(
            conn, str(sequence_row["sequence_id"])
        )
        if not isinstance(sequence_state, ActiveSequenceState):
            return None
        if sequence_state.current_run_id != run_id:
            return None
        run_state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if not isinstance(run_state, (CompletedState, CompletedWithResidualRiskState)):
            return None
        if self._handoff is None:
            return TickRunReceipt(run_id=run_id, action="sequence_terminal_leaf_pending")
        return self._handoff.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=self._now_factory(),
        )
