"""Explicitly extend a run that stopped at the review-iteration limit."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.iterations import fix_prompt_path, iteration_label
from ai_dev_loop.locking import LockMetadata, RunLocks
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.state import RunStatus, append_run_log, save_run_state, transition_status


@dataclass(frozen=True)
class ExtendResult:
    run_id: str
    status: str
    current_review_iteration: int
    previous_max_review_iterations: int
    max_review_iterations: int
    next_safe_action: str


def extend_review_iterations(run_id: str, *, additional_review_iterations: int) -> ExtendResult:
    """Add review budget and restore the stored-fix checkpoint.

    This is deliberately limited to ``max_iterations_reached``.  It preserves
    the run identity, Cursor chat, reviewer session, iterations, and the exact
    fix prompt produced by the final review; it never creates a new run.
    """

    if additional_review_iterations <= 0:
        raise ValidationError("additional review iterations must be positive")

    run_directory, state = load_run(run_id)
    metadata = LockMetadata(
        pid=os.getpid(),
        run_id=run_id,
        repository_path=state.repository.root,
        started_at=datetime.now(tz=UTC),
    )
    with RunLocks(run_directory, metadata):
        run_directory, state = load_run(run_id)
        if state.status != RunStatus.MAX_ITERATIONS_REACHED:
            raise ValidationError(
                "review iterations can only be extended when status is max_iterations_reached"
            )

        current = state.workflow.current_review_iteration
        previous_max = state.workflow.max_review_iterations
        if current != previous_max:
            raise ValidationError(
                "max_iterations_reached state is inconsistent: current review iteration "
                "must equal the recorded maximum"
            )
        if current <= 0:
            raise ValidationError(
                "max_iterations_reached state is inconsistent: no review iteration was recorded"
            )

        prompt_relative = fix_prompt_path(current)
        if not (run_directory / prompt_relative).is_file():
            raise ValidationError(
                f"stored fix prompt for the final review is missing: {prompt_relative}"
            )

        new_max = previous_max + additional_review_iterations
        transition_status(state.status, RunStatus.WAITING_FOR_CURSOR_FIX)
        state.status = RunStatus.WAITING_FOR_CURSOR_FIX
        state.workflow.max_review_iterations = new_max
        state.result = (
            f"Review iteration budget extended from {previous_max} to {new_max}. "
            f"The stored fix prompt for review {iteration_label(current)} is ready for Cursor."
        )
        state.last_error = None
        save_run_state(run_directory, state)

        append_run_log(
            run_directory,
            f"review iteration budget extended from {previous_max} to {new_max} for run {run_id}",
        )
        append_orchestrator_event(
            run_directory,
            run_id=run_id,
            component="orchestrator",
            event="review_iterations_extended",
            status=state.status.value,
            iteration=current,
            artifact_path=str(prompt_relative),
            detail={
                "previous_max_review_iterations": previous_max,
                "additional_review_iterations": additional_review_iterations,
                "max_review_iterations": new_max,
            },
        )

    if state.controller is not None:
        next_safe_action = (
            "Leave the reviewer session inactive, then from the controller session run "
            f"ai_dev_loop launch {run_id} --controller-session-id <exact-controller-session-id>."
        )
    else:
        next_safe_action = f"Run ai_dev_loop resume {run_id}."
    return ExtendResult(
        run_id=run_id,
        status=state.status.value,
        current_review_iteration=current,
        previous_max_review_iterations=previous_max,
        max_review_iterations=new_max,
        next_safe_action=next_safe_action,
    )


def render_extend_output(result: ExtendResult, *, output: str = "text") -> str:
    if output == "json":
        return (
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": result.run_id,
                    "status": result.status,
                    "current_review_iteration": result.current_review_iteration,
                    "previous_max_review_iterations": result.previous_max_review_iterations,
                    "max_review_iterations": result.max_review_iterations,
                    "next_safe_action": result.next_safe_action,
                },
                indent=2,
            )
            + "\n"
        )
    return (
        "\n".join(
            (
                f"Run: {result.run_id}",
                f"Status: {result.status}",
                "Review iteration budget: "
                f"{result.previous_max_review_iterations} -> {result.max_review_iterations}",
                f"Current review iteration: {result.current_review_iteration}",
                f"Next safe action: {result.next_safe_action}",
            )
        )
        + "\n"
    )
