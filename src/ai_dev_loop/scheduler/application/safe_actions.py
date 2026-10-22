"""Store-aware safe next action projection for scheduler runs."""

from __future__ import annotations

import sqlite3

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SafeNextActionKind,
    aborted_pending_resource_cleanup_safe_next_action,
    aborted_pending_termination_safe_next_action,
    safe_next_action_for_state_kind,
    scheduler_status_projection_from_state,
    waiting_codex_capacity_safe_next_action,
    waiting_codex_review_retry_safe_next_action,
)
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    BlockedState,
    SchedulerState,
    WaitingCodexCapacityState,
    WaitingCodexReviewRetryState,
    is_codex_implementer_reservation_hold,
    is_cursor_sdk_reservation_hold,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def safe_next_action_for_scheduler_state(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
) -> SafeNextAction:
    if isinstance(state, BlockedState) and state.block_reason_kind == "cursor_failure":
        from ai_dev_loop.scheduler.application.cursor_sdk_no_progress import (
            no_progress_safe_action,
        )

        specialized = no_progress_safe_action(store, conn, state)
        if specialized is not None:
            return specialized
    if isinstance(state, BlockedState) and state.block_reason_kind in {
        "cursor_failure",
        "codex_implementer_failure",
    }:
        return SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command=(
                f"ai_dev_loop scheduler implementer-retry {state.run_id} --force "
                "(authenticate and publish one recovery successor; tick dispatches it)."
            ),
        )
    if (
        is_codex_implementer_reservation_hold(state) or is_cursor_sdk_reservation_hold(state)
    ) and store.get_reservation_for_run(conn, state.run_id) is not None:
        return SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_ABORT,
            command=(
                f"Inspect protected implementer evidence for run {state.run_id}; "
                f"ai_dev_loop scheduler abort {state.run_id} explicitly releases "
                "the repository hold once attempt termination is resolved."
            ),
        )
    if isinstance(state, AbortedState):
        if store.has_unresolved_abort_hold(conn, state.run_id):
            return aborted_pending_termination_safe_next_action(state.run_id)
        if store.has_unreleased_abort_resources(conn, state.run_id):
            return aborted_pending_resource_cleanup_safe_next_action(state.run_id)
    projection = scheduler_status_projection_from_state(state)
    if isinstance(state, WaitingCodexReviewRetryState):
        return waiting_codex_review_retry_safe_next_action(
            state.run_id,
            routing_auto_retry_eligible=state.codex.routing_auto_retry_eligible,
            routing_auto_retry_due_at=state.codex.routing_auto_retry_due_at,
            routing_auto_retry_exhausted=state.codex.routing_auto_retry_exhausted,
            routing_auto_retry_authorizations_used=state.codex.routing_auto_retry_authorizations_used,
        )
    if isinstance(state, WaitingCodexCapacityState):
        continuation = state.codex.capacity_continuation
        if continuation is not None and continuation.role == "implementer":
            return waiting_codex_capacity_safe_next_action(
                state.run_id,
                capacity_role="implementer",
            )
    return safe_next_action_for_state_kind(
        state.kind,
        state.run_id,
        cursor_wait_until=projection["cursor_wait_until"],
        implementer_retry_wait_until=projection["implementer_retry_wait_until"],
        block_reason_kind=projection["block_reason_kind"],
    )
