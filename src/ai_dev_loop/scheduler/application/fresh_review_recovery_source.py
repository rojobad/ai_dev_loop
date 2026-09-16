"""Authenticated source analysis for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from ai_dev_loop.runners.codex_failure import is_integrity_review_block_kind
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    ReviewCheckpointVerificationError,
    verify_review_retry_repository_checkpoint,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.events import (
    CODEX_REVIEW_COMPLETED_EVENT_KIND,
    CURSOR_TURN_COMPLETED_EVENT_KIND,
    STAGING_COMPLETED_EVENT_KIND,
    CursorTurnCompletedEvent,
    StagingCompletedEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.recovery import (
    RecoveryIntegrationPolicy,
    RecoverySequenceBinding,
)
from ai_dev_loop.scheduler.domain.sequence import BlockedSequenceState
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    CursorWorkflowCheckpoint,
    FreshCodexReviewerBinding,
    SubmittedRunContext,
    WaitingCodexCapacityState,
    WaitingCodexReviewRetryState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FRESH_REVIEW_RECOVERY_ELIGIBLE_RUN_KINDS = frozenset({"blocked"})

FRESH_REVIEW_OPERATIONAL_BLOCK_KINDS = frozenset(
    {
        "codex_usage_limit",
        "codex_review_timeout",
        "codex_review_transport",
        "codex_capacity_exhausted",
        "codex_account_capacity",
        "cursor_usage_limit",
    }
)


@dataclass(frozen=True)
class FreshReviewRecoverySourceEvidence:
    context: SubmittedRunContext
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    block_reason_kind: str
    sequence: RecoverySequenceBinding | None
    integration: RecoveryIntegrationPolicy


def _verify_event_row(row: object) -> None:
    payload_text = str(row["event_payload"])  # type: ignore[index]
    expected = str(row["event_payload_sha256"])  # type: ignore[index]
    if payload_sha256(payload_text) != expected:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "ledger event payload digest mismatch",
        )


def _parse_event_row(row: object) -> object:
    _verify_event_row(row)
    payload = json.loads(str(row["event_payload"]))  # type: ignore[index]
    return parse_scheduler_event(payload)


def _authenticate_staging_events(
    events: Sequence[object],
) -> tuple[CursorTurnCompletedEvent, StagingCompletedEvent]:
    turn_event: CursorTurnCompletedEvent | None = None
    staging_event: StagingCompletedEvent | None = None
    for row in events:
        kind = str(row["event_kind"])  # type: ignore[index]
        if kind == CODEX_REVIEW_COMPLETED_EVENT_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "source already has a valid review decision",
            )
        if kind == CURSOR_TURN_COMPLETED_EVENT_KIND and turn_event is None:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CursorTurnCompletedEvent):
                turn_event = parsed
        if kind == STAGING_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, StagingCompletedEvent):
                staging_event = parsed
    if turn_event is None or staging_event is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "source lacks completed cursor/staging evidence",
        )
    if turn_event.iteration != staging_event.iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor turn iteration disagrees with staged patch iteration",
        )
    if not staging_event.staged_patch_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "staged patch is empty",
        )
    return turn_event, staging_event


def _resolve_sequence_binding(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    source_run_id: str,
    context: SubmittedRunContext,
) -> RecoverySequenceBinding | None:
    if context.sequence is None:
        return None
    sequence_id = context.sequence.sequence_id
    sequence_state = store.load_validated_sequence_state(conn, sequence_id)
    if isinstance(sequence_state, BlockedSequenceState):
        if sequence_state.current_run_id != source_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "source run is not the current blocked sequence phase",
            )
        if sequence_state.current_ordinal != context.sequence.ordinal:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence ordinal disagrees with blocked sequence state",
            )
    else:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence source must be the current blocked sequence phase",
        )
    total = context.sequence.total_phases
    is_final = context.sequence.ordinal == total
    return RecoverySequenceBinding(
        sequence_id=sequence_id,
        ordinal=context.sequence.ordinal,
        total_phases=total,
        is_final_phase=is_final,
    )


def _integration_policy(
    *,
    sequence: RecoverySequenceBinding | None,
    commit_message: str | None,
    sequence_commit_message: str | None,
) -> RecoveryIntegrationPolicy:
    if sequence is not None and not sequence.is_final_phase:
        message = sequence_commit_message
        if not message or not message.strip():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "non-final sequence recovery requires frozen commit message from entry",
            )
        return RecoveryIntegrationPolicy(commit_message=message.strip())
    if not commit_message or not commit_message.strip():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "standalone or final-phase recovery requires --commit-message",
        )
    return RecoveryIntegrationPolicy(commit_message=commit_message.strip())


def _block_reason_for_state(state: object) -> str:
    if isinstance(state, BlockedState):
        if is_integrity_review_block_kind(state.block_reason_kind):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"block reason is not eligible for fresh recovery: {state.block_reason_kind}",
            )
        if state.block_reason_kind not in FRESH_REVIEW_OPERATIONAL_BLOCK_KINDS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"block reason is not on the operational allowlist: {state.block_reason_kind}",
            )
        return state.block_reason_kind
    if isinstance(state, WaitingCodexReviewRetryState):
        kind = state.codex.review_retry_failure_kind
        if not kind:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "waiting_codex_review_retry lacks failure kind",
            )
        return kind
    if isinstance(state, WaitingCodexCapacityState):
        return state.codex.inferred_operational_failure_kind or "codex_usage_limit"
    raise SchedulerEngineError(
        SchedulerEngineErrorKind.VALIDATION,
        f"run state {getattr(state, 'kind', '?')} is not eligible for fresh recovery",
    )


def _reconstruct_checkpoint(state: object, events: Sequence[object]) -> AdmittedRunCheckpoint:
    checkpoint = getattr(state, "checkpoint", None)
    if checkpoint is not None:
        return AdmittedRunCheckpoint(
            authorized_at=checkpoint.authorized_at,
            authorized_controller_session_id=checkpoint.authorized_controller_session_id,
            admitted_at=checkpoint.admitted_at,
            admission_status_artifact_path=checkpoint.admission_status_artifact_path,
            admission_status_sha256=checkpoint.admission_status_sha256,
        )
    authorized_at = getattr(state, "authorized_at", None)
    controller_id = getattr(state, "authorized_controller_session_id", None)
    admission_path = None
    admission_sha = None
    for row in events:
        if str(row["event_kind"]) == "worktree_admitted":  # type: ignore[index]
            _verify_event_row(row)
            payload = json.loads(str(row["event_payload"]))  # type: ignore[index]
            admission_path = str(payload["admission_status_artifact_path"])
            admission_sha = str(payload["admission_status_sha256"])
            break
    if not authorized_at or not admission_path or not admission_sha:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "source lacks admission checkpoint evidence",
        )
    return AdmittedRunCheckpoint(
        authorized_at=authorized_at,
        authorized_controller_session_id=controller_id,
        admitted_at=authorized_at,
        admission_status_artifact_path=admission_path,
        admission_status_sha256=admission_sha,
    )


def analyze_fresh_review_recovery_source(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
    *,
    commit_message: str | None,
    sequence_commit_message: str | None = None,
) -> FreshReviewRecoverySourceEvidence:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        if state.kind not in FRESH_REVIEW_RECOVERY_ELIGIBLE_RUN_KINDS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"source run state {state.kind} is not eligible for fresh recovery",
            )
        if store.get_nonterminal_attempt_for_run(conn, source_run_id) is not None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "source run still has a nonterminal attempt",
            )
        live_effect = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND status IN ('pending', 'claimed')
            LIMIT 1
            """,
            (source_run_id,),
        ).fetchone()
        if live_effect is not None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "source run still has a live effect",
            )
        if store.has_checkpoint_reconciliation_hold(conn, source_run_id):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "source run holds an active checkpoint reconciliation hold",
            )
        events = store.list_events_for_run(conn, source_run_id, limit=500, newest_first=False)
        sequence = None
        if state.context.sequence is not None:
            sequence = _resolve_sequence_binding(
                store,
                conn,
                source_run_id=source_run_id,
                context=state.context,
            )
        if store.get_worktree_reservation(conn, state.context.repository.worktree_key):
            reservation = store.get_worktree_reservation(
                conn, state.context.repository.worktree_key
            )
            if reservation is not None and str(reservation["run_id"]) == source_run_id:
                pass  # source owns reservation
            else:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "target worktree reservation is not owned by source run",
                )

    turn_event, staging_event = _authenticate_staging_events(events)
    checkpoint = _reconstruct_checkpoint(state, events)
    state_cursor = getattr(state, "cursor", None)
    cursor = CursorWorkflowCheckpoint(
        iteration=int(staging_event.iteration),
        chat_id=state_cursor.chat_id if state_cursor is not None else None,
        chat_artifact_path=(state_cursor.chat_artifact_path if state_cursor is not None else None),
        chat_artifact_sha256=(
            state_cursor.chat_artifact_sha256 if state_cursor is not None else None
        ),
        staged_patch_path=staging_event.staged_patch_path,
        staged_patch_sha256=staging_event.staged_patch_sha256,
    )
    if not isinstance(state.context.codex, FreshCodexReviewerBinding):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "fresh recovery requires FreshCodexReviewerBinding",
        )

    integration = _integration_policy(
        sequence=sequence,
        commit_message=commit_message,
        sequence_commit_message=sequence_commit_message,
    )

    evidence = FreshReviewRecoverySourceEvidence(
        context=state.context,
        checkpoint=checkpoint,
        cursor=cursor,
        block_reason_kind=_block_reason_for_state(state),
        sequence=sequence,
        integration=integration,
    )
    try:
        verify_review_retry_repository_checkpoint(
            context=evidence.context,
            run_id=source_run_id,
            artifacts=artifacts,
            checkpoint=evidence.checkpoint,
            cursor=evidence.cursor,
            plan_sha256=evidence.context.plan_prompt.plan_sha256,
            prompt_sha256=evidence.context.plan_prompt.prompt_sha256,
        )
    except ReviewCheckpointVerificationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    return evidence
