"""Store-aware safe next action projection for scheduler runs."""

from __future__ import annotations

import sqlite3

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    aborted_pending_resource_cleanup_safe_next_action,
    aborted_pending_termination_safe_next_action,
    safe_next_action_for_state_kind,
    scheduler_status_projection_from_state,
)
from ai_dev_loop.scheduler.domain.state import AbortedState, SchedulerState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def safe_next_action_for_scheduler_state(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
) -> SafeNextAction:
    if isinstance(state, AbortedState):
        if store.has_unresolved_abort_hold(conn, state.run_id):
            return aborted_pending_termination_safe_next_action(state.run_id)
        if store.has_unreleased_abort_resources(conn, state.run_id):
            return aborted_pending_resource_cleanup_safe_next_action(state.run_id)
    projection = scheduler_status_projection_from_state(state)
    return safe_next_action_for_state_kind(
        state.kind,
        state.run_id,
        cursor_wait_until=projection["cursor_wait_until"],
        block_reason_kind=projection["block_reason_kind"],
    )
