"""Unit tests for Phase 20.3 sequence handoff store contracts."""

from __future__ import annotations

from ai_dev_loop.scheduler.application.contracts import (
    awaiting_finalization_sequence_safe_next_action,
    checkpoint_pending_safe_next_action,
)


def test_checkpoint_pending_safe_action_requests_tick() -> None:
    action = checkpoint_pending_safe_next_action()
    assert action.kind == "scheduler_tick"
    assert "checkpoint" in (action.command or "")


def test_awaiting_finalization_safe_action_is_inspect_only() -> None:
    action = awaiting_finalization_sequence_safe_next_action("seq-123")
    assert action.kind == "inspect_blocked"
    assert "awaiting finalization" in (action.command or "").lower()


def test_sequence_exposes_checkpoint_boundary_only_for_terminal_non_final_runs() -> None:
    from ai_dev_loop.scheduler.application.contracts import sequence_exposes_checkpoint_boundary

    assert sequence_exposes_checkpoint_boundary(
        run_state_kind="completed",
        current_ordinal=1,
        total_phases=2,
    )
    assert not sequence_exposes_checkpoint_boundary(
        run_state_kind="checkpoint_pending",
        current_ordinal=1,
        total_phases=2,
    )
