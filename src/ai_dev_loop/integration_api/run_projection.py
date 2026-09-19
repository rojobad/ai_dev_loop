"""Public run inspection projections for the Integration API."""

# Pydantic wire models use camelCase aliases; populate_by_name is runtime-only for mypy.
# mypy: disable-error-code=call-arg

from __future__ import annotations

import sqlite3
from typing import Literal

from ai_dev_loop.integration_api.models import (
    IntegrationAttemptItem,
    IntegrationCollectionPage,
    IntegrationFrozenArtifactChunk,
    IntegrationHistoryItem,
    IntegrationReviewBudget,
    IntegrationRunInspectData,
    IntegrationRunListItem,
    IntegrationSafeNextAction,
    IntegrationSequenceBinding,
)
from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    bound_reviewer_session_id_from_state,
    scheduler_status_projection_from_state,
    summary_from_context,
)
from ai_dev_loop.scheduler.application.history import _safe_detail_for_event
from ai_dev_loop.scheduler.application.review_budget import (
    load_review_budget_extensions,
    review_budget_projection,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.application.timeline import _observed_duration_seconds, _timeline_status
from ai_dev_loop.scheduler.domain.state import SchedulerState, SequenceRunBinding
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

RunKindFilter = Literal["all", "standalone", "sequence"]


def _sequence_binding(binding: SequenceRunBinding | None) -> IntegrationSequenceBinding | None:
    if binding is None:
        return None
    return IntegrationSequenceBinding(
        sequence_id=binding.sequence_id,
        ordinal=binding.ordinal,
        phase_count=binding.total_phases,
    )


def _residual_risk_for_state(state_kind: str) -> bool | None:
    if state_kind == "completed_with_residual_risk":
        return True
    if state_kind == "completed":
        return False
    return None


def _integration_safe_next_action(
    state: SchedulerState,
    action_kind: SafeNextActionKind,
    *,
    cursor_wait_until: str | None,
) -> IntegrationSafeNextAction:
    sequence_id = state.context.sequence.sequence_id if state.context.sequence is not None else None
    wait_until = cursor_wait_until if action_kind is SafeNextActionKind.WAIT_UNTIL else None
    return IntegrationSafeNextAction(
        kind=action_kind.value,
        run_id=state.run_id,
        sequence_id=sequence_id,
        wait_until=wait_until,
    )


def _safe_action_from_internal(
    state: SchedulerState,
    internal: object,
    *,
    cursor_wait_until: str | None,
) -> IntegrationSafeNextAction:
    from ai_dev_loop.scheduler.application.contracts import SafeNextAction

    if not isinstance(internal, SafeNextAction):
        raise TypeError("expected SafeNextAction")
    return _integration_safe_next_action(
        state,
        internal.kind,
        cursor_wait_until=cursor_wait_until,
    )


def build_run_list_item(
    *,
    state: SchedulerState,
    submitted_at: str,
    updated_at: str,
    review_budget: IntegrationReviewBudget,
    sequence_binding: IntegrationSequenceBinding | None,
) -> IntegrationRunListItem:
    return IntegrationRunListItem(
        run_id=state.run_id,
        project_name=state.context.project_name,
        repository_root=str(state.context.repository.root),
        state=state.kind,
        submitted_at=submitted_at,
        updated_at=updated_at,
        review_budget=review_budget,
        sequence_binding=sequence_binding,
    )


def build_run_inspect_data(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
    *,
    submitted_at: str,
    updated_at: str,
) -> IntegrationRunInspectData:
    projection = scheduler_status_projection_from_state(state)
    ledger_reviews_completed = store.count_review_completion_events(conn, state.run_id)
    extension_events = load_review_budget_extensions(store, conn, state.run_id)
    reviews_completed, max_reviews, submitted_max = review_budget_projection(
        state,
        extension_events,
        ledger_reviews_completed=ledger_reviews_completed,
    )
    internal_summary = summary_from_context(
        run_id=state.run_id,
        state_kind=state.kind,
        submitted_at=submitted_at,
        updated_at=updated_at,
        context=state.context,
        safe_next_action=safe_next_action_for_scheduler_state(store, conn, state),
        bound_reviewer_session_id=bound_reviewer_session_id_from_state(state),
        review_iterations_completed=reviews_completed,
        max_review_iterations=max_reviews,
        submitted_max_review_iterations=submitted_max,
        cursor_wait_until=projection["cursor_wait_until"],
        block_reason_kind=projection["block_reason_kind"],
    )
    review_budget = IntegrationReviewBudget(
        completed=reviews_completed,
        max_reviews=max_reviews,
        submitted_max=submitted_max,
    )
    safe_action = _safe_action_from_internal(
        state,
        internal_summary.safe_next_action,
        cursor_wait_until=projection["cursor_wait_until"],
    )
    return IntegrationRunInspectData(
        run_id=state.run_id,
        project_name=state.context.project_name,
        repository_root=str(state.context.repository.root),
        state=state.kind,
        submitted_at=submitted_at,
        updated_at=updated_at,
        review_budget=review_budget,
        sequence_binding=_sequence_binding(state.context.sequence),
        residual_risk=_residual_risk_for_state(state.kind),
        block_reason=projection["block_reason_kind"],
        cursor_wait_until=projection["cursor_wait_until"],
        safe_next_action=safe_action,
        attempt_count=store.count_attempts_for_run(conn, state.run_id),
    )


def build_attempt_item(row: sqlite3.Row) -> IntegrationAttemptItem:
    launch = str(row["launch_requested_at"]) if row["launch_requested_at"] is not None else None
    completed = str(row["completed_at"]) if row["completed_at"] is not None else None
    effect_kind = row["effect_kind"]
    return IntegrationAttemptItem(
        attempt_id=str(row["attempt_id"]),
        component=str(row["component"]),
        effect_kind=str(effect_kind) if effect_kind is not None else None,
        iteration=int(row["iteration"]),
        phase_attempt=int(row["phase_attempt"]),
        status=_timeline_status(str(row["status"])),
        created_at=str(row["created_at"]),
        launch_requested_at=launch,
        completed_at=completed,
        observed_duration_seconds=_observed_duration_seconds(launch, completed),
    )


def build_history_item(row: sqlite3.Row) -> IntegrationHistoryItem:
    return IntegrationHistoryItem(
        sequence=int(row["sequence"]),
        kind=str(row["event_kind"]),
        timestamp=str(row["created_at"]),
        safe_detail=_safe_detail_for_event(
            str(row["event_kind"]),
            str(row["event_payload"]),
        ),
    )


def collection_page(offset: int, limit: int, has_more: bool) -> IntegrationCollectionPage:
    next_offset = offset + limit if has_more else None
    return IntegrationCollectionPage(
        offset=offset,
        limit=limit,
        next_offset=next_offset,
        has_more=has_more,
    )


def frozen_chunk_to_wire(chunk: object) -> IntegrationFrozenArtifactChunk:
    from ai_dev_loop.integration_api.artifact_reader import VerifiedArtifactChunk

    if not isinstance(chunk, VerifiedArtifactChunk):
        raise TypeError("expected VerifiedArtifactChunk")
    return IntegrationFrozenArtifactChunk(
        run_id=chunk.run_id,
        artifact_kind=chunk.artifact_kind,
        source_repository_path=chunk.source_repository_path,
        available=True,
        reason=None,
        encoding="base64",
        media_type=chunk.media_type,
        byte_offset=chunk.byte_offset,
        returned_bytes=chunk.returned_bytes,
        next_offset=chunk.next_offset,
        available_bytes=chunk.total_bytes,
        has_more=chunk.has_more,
        content_base64=chunk.content_base64,
        sha256=chunk.sha256,
    )
