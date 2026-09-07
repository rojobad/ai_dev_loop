"""Pure scheduler state transitions."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.state import SubmittedState


def apply_run_submitted(
    state: SubmittedState,
    event: RunSubmittedEvent,
) -> SubmittedState:
    """Validate that a submitted event matches the queued state."""

    if event.run_id != state.run_id:
        raise ValueError("event run_id disagrees with state")
    if event.idempotency_key != state.idempotency_key:
        raise ValueError("event idempotency_key disagrees with state")
    if event.worktree_key != state.context.repository.worktree_key:
        raise ValueError("event worktree_key disagrees with state")
    if state.kind != "queued":
        raise ValueError("run_submitted applies only to queued runs")
    return state
