"""Authenticated source analysis for Phase 20.6.5 rollover."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_budget_artifacts import (
    load_exhausted_review_artifacts,
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
from ai_dev_loop.scheduler.domain.rollover import (
    RolloverIntegrationPolicy,
    RolloverSequenceBinding,
)
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    CodexRuntimeBinding,
    CursorWorkflowCheckpoint,
    FreshCodexReviewerBinding,
    MaxIterationsReachedState,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

AUTHENTICATED_ROLLOVER_ELIGIBLE_RUN_KINDS = frozenset({"max_iterations_reached"})


@dataclass(frozen=True)
class AuthenticatedRolloverSourceEvidence:
    context: SubmittedRunContext
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    sequence: RolloverSequenceBinding | None
    integration: RolloverIntegrationPolicy
    source_final_review_result_path: str
    source_final_review_result_sha256: str
    fresh_codex: FreshCodexReviewerBinding


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
    turns_by_iteration: dict[int, CursorTurnCompletedEvent] = {}
    staging_event: StagingCompletedEvent | None = None
    for row in events:
        kind = str(row["event_kind"])  # type: ignore[index]
        if kind == CODEX_REVIEW_COMPLETED_EVENT_KIND:
            continue
        if kind == CURSOR_TURN_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CursorTurnCompletedEvent):
                turns_by_iteration[parsed.iteration] = parsed
        if kind == STAGING_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, StagingCompletedEvent):
                staging_event = parsed
    if staging_event is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "source lacks completed cursor/staging evidence",
        )
    turn_event = turns_by_iteration.get(staging_event.iteration)
    if turn_event is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "source lacks completed cursor/staging evidence",
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
) -> RolloverSequenceBinding | None:
    if context.sequence is None:
        return None
    sequence_id = context.sequence.sequence_id
    sequence_state = store.load_validated_sequence_state(conn, sequence_id)
    if isinstance(sequence_state, ActiveSequenceState):
        if sequence_state.current_run_id != source_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "source run is not the current sequence phase",
            )
        if sequence_state.current_ordinal != context.sequence.ordinal:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence ordinal disagrees with active sequence state",
            )
    else:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence source must be the current active sequence phase",
        )
    total = context.sequence.total_phases
    is_final = context.sequence.ordinal == total
    return RolloverSequenceBinding(
        sequence_id=sequence_id,
        ordinal=context.sequence.ordinal,
        total_phases=total,
        is_final_phase=is_final,
    )


def _integration_policy(
    *,
    sequence: RolloverSequenceBinding | None,
    commit_message: str | None,
    sequence_commit_message: str | None,
) -> RolloverIntegrationPolicy:
    if sequence is not None and not sequence.is_final_phase:
        message = sequence_commit_message
        if not message or not message.strip():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "non-final sequence rollover requires frozen commit message from entry",
            )
        return RolloverIntegrationPolicy(commit_message=message.strip())
    if not commit_message or not commit_message.strip():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "standalone or final-phase rollover requires --commit-message",
        )
    return RolloverIntegrationPolicy(commit_message=commit_message.strip())


def _fresh_codex_binding_from_context(
    context: SubmittedRunContext,
    *,
    binding_artifact_path: str,
    binding_sha256: str,
) -> FreshCodexReviewerBinding:
    codex = context.codex
    if isinstance(codex, FreshCodexReviewerBinding):
        return codex
    if not isinstance(codex, CodexRuntimeBinding):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "rollover source codex binding is unsupported",
        )
    return FreshCodexReviewerBinding(
        review_model=codex.review_model,
        review_reasoning_effort=codex.review_reasoning_effort,
        review_model_source="explicit",
        review_reasoning_source="explicit",
        command=codex.command,
        review_skill=codex.review_skill,
        sandbox=codex.sandbox,
        binding_artifact_path=binding_artifact_path,
        binding_sha256=binding_sha256,
    )


def analyze_authenticated_rollover_source(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
    *,
    commit_message: str | None,
    sequence_commit_message: str | None = None,
) -> AuthenticatedRolloverSourceEvidence:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        if state.kind not in AUTHENTICATED_ROLLOVER_ELIGIBLE_RUN_KINDS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"source run state {state.kind} is not eligible for authenticated rollover",
            )
        if not isinstance(state, MaxIterationsReachedState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "authenticated rollover requires max_iterations_reached source",
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
                pass
            else:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "target worktree reservation is not owned by source run",
                )

    exhausted = load_exhausted_review_artifacts(store, artifacts, state)
    if (
        state.codex.latest_review_result_path != exhausted.review_result_path
        or state.codex.latest_review_result_sha256 != exhausted.review_result_sha256
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review artifact binding disagrees with source state",
        )

    turn_event, staging_event = _authenticate_staging_events(events)
    checkpoint = state.checkpoint
    state_cursor = state.cursor
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

    integration = _integration_policy(
        sequence=sequence,
        commit_message=commit_message,
        sequence_commit_message=sequence_commit_message,
    )

    fresh_codex = _fresh_codex_binding_from_context(
        state.context,
        binding_artifact_path="codex/fresh-reviewer-input.json",
        binding_sha256="0" * 64,
    )

    evidence = AuthenticatedRolloverSourceEvidence(
        context=state.context,
        checkpoint=checkpoint,
        cursor=cursor,
        sequence=sequence,
        integration=integration,
        source_final_review_result_path=exhausted.review_result_path,
        source_final_review_result_sha256=exhausted.review_result_sha256,
        fresh_codex=fresh_codex,
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
