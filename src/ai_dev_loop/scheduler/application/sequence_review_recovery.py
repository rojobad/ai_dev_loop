"""Sequence-aware blocked-run review recovery and execution-leaf replacement."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.review_recovery import (
    BlockedReviewRecoveryEvidence,
    _copy_recovery_artifacts,
    analyze_blocked_review_recovery,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.events import CodexReviewRecoverySuccessorCreatedEvent
from ai_dev_loop.scheduler.domain.sequence import (
    BLOCKED_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.domain.sequence_execution_replacement import (
    SequenceExecutionReplacementIntent,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
    SequenceAttemptKind,
    SequenceRunAttempt,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    BlockedState,
    PendingSequenceReviewRecoveryState,
    ReviewRecoveryLineage,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_execution_replacement_intent_store import (
    assert_sequence_blocked_for_recovery_source,
    authenticate_durable_sequence_replacement_publication,
    load_validated_sequence_execution_replacement_intent,
    sequence_recovery_publication_complete,
)
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    SequenceExecutionCASExpectation,
    _current_leaf_run_id,
    _max_generation,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import generate_run_id


@dataclass(frozen=True)
class SequenceReviewRecoveryBinding:
    sequence_id: str
    ordinal: int
    source_generation: int
    entry_hash: str


@dataclass(frozen=True)
class SequenceReviewRecoveryOutcome:
    successor_run_id: str
    changed: bool


def _reject_cancelled_replacement_intent(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    recovery_key: str,
) -> None:
    if store.is_sequence_execution_replacement_cancelled(
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "sequence replacement intent was cancelled",
        )


def find_blocked_sequence_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> BlockedSequenceState | None:
    rows = conn.execute(
        """
        SELECT sequence_id
        FROM scheduler_sequences
        WHERE state_kind = ?
          AND json_extract(payload, '$.current_run_id') = ?
        """,
        (BLOCKED_SEQUENCE_STATE_KIND, source_run_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    sequence_id = str(rows[0]["sequence_id"])
    state = store.load_validated_sequence_state(conn, sequence_id)
    if not isinstance(state, BlockedSequenceState):
        return None
    if state.current_run_id != source_run_id:
        return None
    return state


def authenticate_sequence_review_recovery_binding(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    context: SubmittedRunContext,
    blocked_sequence: BlockedSequenceState,
) -> SequenceReviewRecoveryBinding:
    binding = context.sequence
    if binding is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence recovery requires a frozen sequence binding on the source run",
        )
    if binding.sequence_id != blocked_sequence.sequence_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence binding disagrees with blocked sequence identity",
        )
    if binding.ordinal != blocked_sequence.current_ordinal:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence binding ordinal disagrees with blocked sequence ordinal",
        )
    if not store.schema_supports_sequence_run_lineage(conn):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence run lineage schema is required for review recovery",
        )
    leaf_run_id = _current_leaf_run_id(
        conn,
        sequence_id=blocked_sequence.sequence_id,
        ordinal=blocked_sequence.current_ordinal,
    )
    if leaf_run_id != source_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "source run is not the authenticated current sequence execution leaf",
        )
    source_generation = _max_generation(
        conn,
        sequence_id=blocked_sequence.sequence_id,
        ordinal=blocked_sequence.current_ordinal,
    )
    if source_generation < 1:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence lineage missing source generation",
        )
    current_entry = next(
        (
            entry
            for entry in blocked_sequence.materialized_entries
            if entry.ordinal == blocked_sequence.current_ordinal
        ),
        None,
    )
    if current_entry is None or current_entry.run_id != source_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked sequence materialized entry disagrees with source run",
        )
    if current_entry.entry_hash != binding.entry_hash:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence entry hash disagrees with frozen run binding",
        )
    return SequenceReviewRecoveryBinding(
        sequence_id=blocked_sequence.sequence_id,
        ordinal=blocked_sequence.current_ordinal,
        source_generation=source_generation,
        entry_hash=current_entry.entry_hash,
    )


def _build_replacement_intent(
    *,
    evidence: BlockedReviewRecoveryEvidence,
    blocked_sequence: BlockedSequenceState,
    recovery_binding: SequenceReviewRecoveryBinding,
    source_run_id: str,
    successor_run_id: str,
) -> SequenceExecutionReplacementIntent:
    staged_patch_sha256 = evidence.cursor.staged_patch_sha256
    if not staged_patch_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery evidence lacks staged patch hash",
        )
    reviewer_session_id = evidence.codex.reviewer_session_id
    if not reviewer_session_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery evidence lacks reviewer session identity",
        )
    review_model = evidence.context.codex.review_model
    review_reasoning = evidence.context.codex.review_reasoning_effort
    sandbox = evidence.context.codex.sandbox
    return SequenceExecutionReplacementIntent(
        sequence_id=recovery_binding.sequence_id,
        sequence_version=blocked_sequence.version,
        ordinal=recovery_binding.ordinal,
        source_run_id=source_run_id,
        source_generation=recovery_binding.source_generation,
        successor_run_id=successor_run_id,
        successor_generation=recovery_binding.source_generation + 1,
        recovery_key=evidence.recovery_key,
        entry_hash=recovery_binding.entry_hash,
        staged_patch_sha256=staged_patch_sha256,
        worktree_key=evidence.context.repository.worktree_key,
        repository_root=evidence.context.repository.root,
        reviewer_session_id=reviewer_session_id,
        review_model=review_model,
        review_reasoning_effort=review_reasoning,
        codex_sandbox=sandbox,
        reservation_owner_run_id=successor_run_id,
    )


def _sequence_recovery_publication_is_complete(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    recovery_key: str,
    successor_run_id: str,
) -> bool:
    validated = load_validated_sequence_execution_replacement_intent(
        store,
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    )
    if validated is None or validated.is_cancelled:
        return False
    if validated.intent.successor_run_id != successor_run_id:
        return False
    return authenticate_durable_sequence_replacement_publication(
        store,
        conn,
        validated=validated,
    )


def resume_sequence_execution_replacement_intent(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    conn: sqlite3.Connection,
    source_run_id: str,
    recovery_key: str,
    now: datetime,
    event_id_factory: Callable[[], str] | None = None,
) -> TickRunReceipt | None:
    validated = load_validated_sequence_execution_replacement_intent(
        store,
        conn,
        source_run_id=source_run_id,
        recovery_key=recovery_key,
    )
    if validated is None or validated.is_published or validated.is_cancelled:
        return None
    if store.has_sequence_abort_requested_for_run(
        conn,
        run_id=source_run_id,
        sequence_id=validated.intent.sequence_id,
    ):
        return TickRunReceipt(
            run_id=source_run_id,
            action="sequence_recovery_intent_cancelled",
        )
    blocked_sequence = find_blocked_sequence_for_run(store, conn, source_run_id)
    if blocked_sequence is None:
        return None
    try:
        evidence = analyze_blocked_review_recovery(store, artifacts, source_run_id)
    except SchedulerEngineError:
        return TickRunReceipt(
            run_id=source_run_id,
            action="sequence_recovery_evidence_invalid",
        )
    recovery_binding = authenticate_sequence_review_recovery_binding(
        store,
        conn,
        source_run_id=source_run_id,
        context=evidence.context,
        blocked_sequence=blocked_sequence,
    )
    successor_run_id = validated.intent.successor_run_id
    run_row = conn.execute(
        "SELECT run_id, state_kind FROM scheduler_runs WHERE run_id = ?",
        (successor_run_id,),
    ).fetchone()
    factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
    if run_row is None:
        _materialize_pending_successor(
            store,
            artifacts,
            evidence,
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            recovery_key=recovery_key,
            sequence_id=blocked_sequence.sequence_id,
            now=now,
            event_id_factory=factory,
            conn=conn,
        )
    adopted = _adopt_sequence_recovery_successor(
        store,
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


def _materialize_pending_successor(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    evidence: BlockedReviewRecoveryEvidence,
    *,
    source_run_id: str,
    successor_run_id: str,
    recovery_key: str,
    sequence_id: str,
    now: datetime,
    event_id_factory: Callable[[], str],
    conn: sqlite3.Connection | None = None,
) -> bool:
    from ai_dev_loop.scheduler.application.review_recovery import (
        resolve_recovery_ledger_evidence_run_id,
    )

    def _preflight(active_conn: sqlite3.Connection) -> str:
        existing_row = store.get_review_recovery_successor(
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if existing_row is not None and str(existing_row["successor_run_id"]) != successor_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "review recovery successor identity disagrees with replacement intent",
            )
        active_conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()
        return resolve_recovery_ledger_evidence_run_id(store, active_conn, source_run_id)

    if conn is not None:
        artifact_source_run_id = _preflight(conn)
        run_row = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()
    else:
        with store.begin_read() as read_conn:
            artifact_source_run_id = _preflight(read_conn)
            run_row = read_conn.execute(
                "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
                (successor_run_id,),
            ).fetchone()
    if run_row is None:
        artifacts.run_root(successor_run_id)
        _copy_recovery_artifacts(
            artifacts,
            artifact_source_run_id=artifact_source_run_id,
            successor_run_id=successor_run_id,
            manifest=evidence.copy_manifest,
        )
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    staged_patch_sha256 = evidence.cursor.staged_patch_sha256 or ""
    lineage = ReviewRecoveryLineage(
        source_run_id=source_run_id,
        source_block_reason_kind=evidence.block_reason_kind,
        source_review_iteration=evidence.cursor.iteration,
        source_staged_patch_sha256=staged_patch_sha256,
        source_failed_attempt_id=evidence.failed_attempt_id,
        created_at=now_text,
    )
    pending_state = PendingSequenceReviewRecoveryState(
        run_id=successor_run_id,
        version=1,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=secrets.token_hex(32),
        context=evidence.context,
        checkpoint=evidence.checkpoint,
        cursor=evidence.cursor,
        codex=evidence.codex,
        recovery=lineage,
        sequence_replacement_sequence_id=sequence_id,
        sequence_replacement_recovery_key=recovery_key,
        sequence_replacement_source_run_id=source_run_id,
    )
    event = CodexReviewRecoverySuccessorCreatedEvent(
        source_run_id=source_run_id,
        successor_run_id=successor_run_id,
        recovery_key=recovery_key,
        review_iteration=evidence.cursor.iteration,
    )
    event_id = event_id_factory()

    def _materialize_on_conn(active_conn: sqlite3.Connection) -> bool:
        _reject_cancelled_replacement_intent(
            store,
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if store.get_nonterminal_attempt_for_run(active_conn, source_run_id) is not None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "cannot materialize sequence recovery while a Codex attempt is active",
            )
        existing_row = store.get_review_recovery_successor(
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        local_run_row = active_conn.execute(
            "SELECT run_id, state_kind FROM scheduler_runs WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()
        if existing_row is None:
            if local_run_row is not None:
                if _sequence_recovery_publication_is_complete(
                    store,
                    active_conn,
                    source_run_id=source_run_id,
                    recovery_key=recovery_key,
                    successor_run_id=successor_run_id,
                ):
                    return False
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "successor run exists in unexpected state for sequence recovery",
                )
            store.insert_sequence_recovery_pending_run(
                active_conn,
                run_id=successor_run_id,
                state=pending_state,
                event_id=event_id,
                event=event,
                now=now,
            )
            store.insert_review_recovery_successor(
                active_conn,
                source_run_id=source_run_id,
                recovery_key=recovery_key,
                successor_run_id=successor_run_id,
                now=now,
            )
            return True
        if local_run_row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "review recovery successor row exists without scheduler run",
            )
        if str(local_run_row["state_kind"]) in {
            "pending_sequence_review_recovery",
            "awaiting_codex_review",
        } or _sequence_recovery_publication_is_complete(
            store,
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            successor_run_id=successor_run_id,
        ):
            return False
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "successor run exists in unexpected state for sequence recovery",
        )

    if conn is not None:
        return _materialize_on_conn(conn)
    with store.begin_immediate() as writer_conn:
        return _materialize_on_conn(writer_conn)


def _adopt_sequence_recovery_successor(
    store: SqliteSchedulerStore,
    *,
    evidence: BlockedReviewRecoveryEvidence,
    source_run_id: str,
    blocked_sequence: BlockedSequenceState,
    recovery_binding: SequenceReviewRecoveryBinding,
    successor_run_id: str,
    recovery_key: str,
    now: datetime,
    conn: sqlite3.Connection | None = None,
) -> bool:
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    current_materialized_at = next(
        entry.materialized_at
        for entry in blocked_sequence.materialized_entries
        if entry.ordinal == recovery_binding.ordinal
    )
    replacement_attempt = SequenceRunAttempt(
        schema_version=1,
        generation=recovery_binding.source_generation + 1,
        run_id=successor_run_id,
        source_run_id=source_run_id,
        attempt_kind=cast(SequenceAttemptKind, SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY),
        materialized_at=current_materialized_at,
        terminal_outcome=None,
        resolved_at=None,
    )
    updated_materialized = MaterializedSequenceEntry(
        ordinal=recovery_binding.ordinal,
        run_id=successor_run_id,
        entry_hash=recovery_binding.entry_hash,
        materialized_at=current_materialized_at,
    )
    materialized_entries = tuple(
        updated_materialized if entry.ordinal == recovery_binding.ordinal else entry
        for entry in blocked_sequence.materialized_entries
    )
    updated_sequence = ActiveSequenceState(
        schema_version=blocked_sequence.schema_version,
        sequence_id=blocked_sequence.sequence_id,
        version=blocked_sequence.version + 1,
        prepared_at=blocked_sequence.prepared_at,
        updated_at=now_text,
        started_at=blocked_sequence.started_at,
        idempotency_key=blocked_sequence.idempotency_key,
        definition=blocked_sequence.definition,
        current_ordinal=blocked_sequence.current_ordinal,
        current_run_id=successor_run_id,
        materialized_entries=materialized_entries,
        residual_risk_ordinals=blocked_sequence.residual_risk_ordinals,
    )
    expectation = SequenceExecutionCASExpectation(
        expected_version=blocked_sequence.version,
        definition=blocked_sequence.definition,
        materialized_entries=blocked_sequence.materialized_entries,
        current_ordinal=blocked_sequence.current_ordinal,
        current_run_id=source_run_id,
    )

    def _adopt_on_conn(active_conn: sqlite3.Connection) -> bool:
        _reject_cancelled_replacement_intent(
            store,
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        validated = load_validated_sequence_execution_replacement_intent(
            store,
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if validated is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence replacement intent missing before adoption",
            )
        if validated.intent.successor_run_id != successor_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence replacement intent successor disagrees with adoption target",
            )
        if sequence_recovery_publication_complete(store, active_conn, validated=validated):
            return False
        pending, pending_version, _ = store.load_validated_snapshot(active_conn, successor_run_id)
        if not isinstance(pending, PendingSequenceReviewRecoveryState):
            if _sequence_recovery_publication_is_complete(
                store,
                active_conn,
                source_run_id=source_run_id,
                recovery_key=recovery_key,
                successor_run_id=successor_run_id,
            ):
                return False
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery successor is not pending adoption",
            )
        active = AwaitingCodexReviewState(
            run_id=successor_run_id,
            version=pending.version + 1,
            submitted_at=pending.submitted_at,
            updated_at=now_text,
            idempotency_key=pending.idempotency_key,
            context=pending.context,
            checkpoint=pending.checkpoint,
            cursor=pending.cursor,
            codex=pending.codex,
            recovery=pending.recovery,
        )
        current_sequence = store.load_validated_sequence_state(
            active_conn,
            blocked_sequence.sequence_id,
        )
        if isinstance(current_sequence, ActiveSequenceState):
            if current_sequence.current_run_id == successor_run_id:
                store.activate_sequence_recovery_successor_run(
                    active_conn,
                    run_id=successor_run_id,
                    pending_state=pending,
                    active_state=active,
                    now=now,
                )
                store.mark_sequence_execution_replacement_published(
                    active_conn,
                    source_run_id=source_run_id,
                    recovery_key=recovery_key,
                    now=now,
                )
                return True
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence already active with a different current run",
            )
        if not isinstance(current_sequence, BlockedSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence is not blocked for execution-leaf replacement",
            )
        if current_sequence.version != blocked_sequence.version:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "blocked sequence version changed during review recovery",
            )
        if not store.compare_and_swap_sequence_execution_leaf(
            active_conn,
            sequence_id=blocked_sequence.sequence_id,
            ordinal=recovery_binding.ordinal,
            expectation=expectation,
            expected_current_run_id=source_run_id,
            replacement_attempt=replacement_attempt,
            updated_sequence_state=updated_sequence,
            now=now,
        ):
            refreshed = store.load_validated_sequence_state(
                active_conn, blocked_sequence.sequence_id
            )
            if (
                isinstance(refreshed, ActiveSequenceState)
                and refreshed.current_run_id == successor_run_id
            ):
                store.activate_sequence_recovery_successor_run(
                    active_conn,
                    run_id=successor_run_id,
                    pending_state=pending,
                    active_state=active,
                    now=now,
                )
                store.mark_sequence_execution_replacement_published(
                    active_conn,
                    source_run_id=source_run_id,
                    recovery_key=recovery_key,
                    now=now,
                )
                return True
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence execution-leaf replacement lost a concurrent update",
            )
        store.activate_sequence_recovery_successor_run(
            active_conn,
            run_id=successor_run_id,
            pending_state=pending,
            active_state=active,
            now=now,
        )
        store.mark_sequence_execution_replacement_published(
            active_conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
            now=now,
        )
        return True

    if conn is not None:
        return _adopt_on_conn(conn)
    with store.begin_immediate() as writer_conn:
        return _adopt_on_conn(writer_conn)


def complete_sequence_review_recovery(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    evidence: BlockedReviewRecoveryEvidence,
    *,
    source_run_id: str,
    blocked_sequence: BlockedSequenceState,
    recovery_binding: SequenceReviewRecoveryBinding,
    now: datetime,
    run_id_factory: Callable[[str, datetime], str] | None = None,
    event_id_factory: Callable[[], str] | None = None,
) -> SequenceReviewRecoveryOutcome:
    recovery_key = evidence.recovery_key
    validated: object | None = None
    with store.begin_read() as conn:
        if store.has_sequence_abort_requested_for_run(
            conn,
            run_id=source_run_id,
            sequence_id=blocked_sequence.sequence_id,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence abort was requested before review recovery authorization",
            )
        validated = load_validated_sequence_execution_replacement_intent(
            store,
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if validated is not None:
            if validated.is_cancelled:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence replacement intent was cancelled",
                )
            if authenticate_durable_sequence_replacement_publication(
                store, conn, validated=validated
            ):
                return SequenceReviewRecoveryOutcome(
                    successor_run_id=validated.intent.successor_run_id,
                    changed=False,
                )

    successor_run_id = (
        validated.intent.successor_run_id
        if validated is not None
        else (
            run_id_factory(evidence.context.project_name, now)
            if run_id_factory is not None
            else generate_run_id(evidence.context.project_name, now=now)
        )
    )
    if validated is None:
        intent = _build_replacement_intent(
            evidence=evidence,
            blocked_sequence=blocked_sequence,
            recovery_binding=recovery_binding,
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
        )
        intent_payload = intent.model_dump_json()
        intent_digest = payload_sha256(intent_payload)
        with store.begin_immediate() as conn:
            if store.has_sequence_abort_requested_for_run(
                conn,
                run_id=source_run_id,
                sequence_id=blocked_sequence.sequence_id,
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence abort was requested before review recovery authorization",
                )
            existing = load_validated_sequence_execution_replacement_intent(
                store,
                conn,
                source_run_id=source_run_id,
                recovery_key=recovery_key,
            )
            if existing is not None:
                if existing.is_cancelled:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "sequence replacement intent was cancelled",
                    )
                successor_run_id = existing.intent.successor_run_id
            else:
                inserted = store.insert_sequence_execution_replacement_intent(
                    conn,
                    intent=intent,
                    intent_payload=intent_payload,
                    intent_digest=intent_digest,
                    now=now,
                )
                if not inserted:
                    row = load_validated_sequence_execution_replacement_intent(
                        store,
                        conn,
                        source_run_id=source_run_id,
                        recovery_key=recovery_key,
                    )
                    if row is None:
                        raise SchedulerEngineError(
                            SchedulerEngineErrorKind.INTERNAL,
                            "sequence replacement intent insert failed",
                        )
                    successor_run_id = row.intent.successor_run_id

    with store.begin_immediate() as conn:
        _reject_cancelled_replacement_intent(
            store,
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        validated_before_materialize = load_validated_sequence_execution_replacement_intent(
            store,
            conn,
            source_run_id=source_run_id,
            recovery_key=recovery_key,
        )
        if validated_before_materialize is not None:
            successor_run_id = validated_before_materialize.intent.successor_run_id
            if authenticate_durable_sequence_replacement_publication(
                store,
                conn,
                validated=validated_before_materialize,
            ):
                return SequenceReviewRecoveryOutcome(
                    successor_run_id=successor_run_id,
                    changed=False,
                )

    materialized = _materialize_pending_successor(
        store,
        artifacts,
        evidence,
        source_run_id=source_run_id,
        successor_run_id=successor_run_id,
        recovery_key=recovery_key,
        sequence_id=blocked_sequence.sequence_id,
        now=now,
        event_id_factory=event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}"),
    )
    adopted = _adopt_sequence_recovery_successor(
        store,
        evidence=evidence,
        source_run_id=source_run_id,
        blocked_sequence=blocked_sequence,
        recovery_binding=recovery_binding,
        successor_run_id=successor_run_id,
        recovery_key=recovery_key,
        now=now,
    )
    return SequenceReviewRecoveryOutcome(
        successor_run_id=successor_run_id,
        changed=materialized or adopted,
    )


def recover_blocked_sequence_review_run(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
    *,
    now: datetime,
    run_id_factory: Callable[[str, datetime], str] | None = None,
    event_id_factory: Callable[[], str] | None = None,
) -> SequenceReviewRecoveryOutcome:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        if not isinstance(state, BlockedState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"source run is not blocked (state={state.kind})",
            )
        binding = state.context.sequence
        if binding is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence-bound blocked run cannot use standalone recovery routing",
            )
        for row in store.list_sequence_execution_replacement_intents_for_source(
            conn,
            source_run_id=source_run_id,
        ):
            validated = load_validated_sequence_execution_replacement_intent(
                store,
                conn,
                source_run_id=source_run_id,
                recovery_key=str(row["recovery_key"]),
            )
            if validated is not None:
                if validated.is_cancelled:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.CONFLICT,
                        "sequence replacement intent was cancelled",
                    )
                if authenticate_durable_sequence_replacement_publication(
                    store, conn, validated=validated
                ):
                    return SequenceReviewRecoveryOutcome(
                        successor_run_id=validated.intent.successor_run_id,
                        changed=False,
                    )
        blocked_sequence = assert_sequence_blocked_for_recovery_source(
            store,
            conn,
            sequence_id=binding.sequence_id,
            source_run_id=source_run_id,
        )
        recovery_binding = authenticate_sequence_review_recovery_binding(
            store,
            conn,
            source_run_id=source_run_id,
            context=state.context,
            blocked_sequence=blocked_sequence,
        )
    evidence = analyze_blocked_review_recovery(store, artifacts, source_run_id)
    return complete_sequence_review_recovery(
        store,
        artifacts,
        evidence,
        source_run_id=source_run_id,
        blocked_sequence=blocked_sequence,
        recovery_binding=recovery_binding,
        now=now,
        run_id_factory=run_id_factory,
        event_id_factory=event_id_factory,
    )
