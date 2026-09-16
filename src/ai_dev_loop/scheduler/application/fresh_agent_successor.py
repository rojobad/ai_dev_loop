"""Shared successor context shaping for fresh recovery and rollover runs."""

from __future__ import annotations

import sqlite3

from ai_dev_loop.scheduler.application.review_budget import effective_review_ceiling_for_run
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
    SchedulerState,
    SubmittedRunContext,
    WorkflowLimits,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def fresh_agent_successor_workflow_limits(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_state: SchedulerState,
) -> WorkflowLimits:
    """Freeze the source's effective authorized review ceiling for a fresh successor."""

    effective_ceiling = effective_review_ceiling_for_run(store, conn, source_state)
    return source_state.context.workflow.model_copy(
        update={"max_review_iterations": effective_ceiling},
    )


def fresh_agent_successor_context(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_state: SchedulerState,
    *,
    context: SubmittedRunContext,
    clear_sequence_binding: bool = False,
) -> SubmittedRunContext:
    workflow = fresh_agent_successor_workflow_limits(store, conn, source_state)
    if clear_sequence_binding:
        updates: dict[str, object] = {"workflow": workflow, "sequence": None}
        if context.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE:
            updates["schema_version"] = SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED
        return context.model_copy(update=updates)
    return context.model_copy(update={"workflow": workflow, "sequence": context.sequence})


def fresh_agent_successor_codex_checkpoint() -> CodexWorkflowCheckpoint:
    """Successor reviews begin at iteration 1 with zero completed reviews."""

    return CodexWorkflowCheckpoint(review_iteration=1, reviews_completed=0)


def fresh_agent_successor_cursor_checkpoint(
    source_cursor: CursorWorkflowCheckpoint,
) -> CursorWorkflowCheckpoint:
    """Preserve staged patch identity while resetting cursor numbering for fresh reviews."""

    return CursorWorkflowCheckpoint(
        iteration=1,
        staged_patch_path=source_cursor.staged_patch_path,
        staged_patch_sha256=source_cursor.staged_patch_sha256,
        chat_id=None,
    )
