"""One-way Phase 15 bridge: PR review callers -> local review/fix capability.

Reusable local modules must never import this adapter. This module may inspect
Phase 15 ``github_pr_review`` state only to replace the removed local coupling.
It must not import ``commands.pr_review``; callers own worker spawn and other
external continuation after the local boundary returns and locks are released.
"""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.event_log import append_orchestrator_event
from ai_dev_loop.iterations import iteration_label, max_iteration_number
from ai_dev_loop.local_review_loop import (
    AcceptedFinalizationResult,
    AcceptedReviewDelivery,
    LocalReviewFixRequest,
    LocalReviewFixResult,
    LocalReviewOperation,
    ScheduledCursorTurn,
    run_local_review_fix,
    validate_scheduled_cursor_turn,
)
from ai_dev_loop.resume_planner import cursor_turn_complete
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.git import validate_external_feedback_pre_cursor
from ai_dev_loop.runners.publish import publication_staged_patch_fingerprint
from ai_dev_loop.runners.tool_updates import ToolCompatibilityPolicy
from ai_dev_loop.state import RunState, RunStatus, save_run_state, transition_status


def next_external_cursor_iteration(state: RunState) -> int:
    """Allocate the next durable iteration number for an external correction.

    Always greater than any persisted iteration so historical Cursor/Codex
    artifacts are never reused or overwritten.
    """

    return max_iteration_number(state) + 1


def pending_external_cursor_iteration(state: RunState) -> int | None:
    """Return the scheduled external Cursor iteration when one is pending.

    Prefers the typed ``external_cursor_iteration`` field. For historical
    independent first turns (no persisted iterations yet) without that field,
    iteration ``1`` is the external prompt turn. Never derives ``max+1`` here:
    that would keep scheduling new Cursor turns after a completed historical
    iteration while lifecycle remains ``fixing_external_feedback``.
    """

    github = state.github_pr_review
    if github is None:
        return None
    if github.lifecycle != "fixing_external_feedback":
        return None
    if not github.external_fix_prompt_path:
        return None
    if github.external_cursor_iteration is not None:
        return github.external_cursor_iteration
    if max_iteration_number(state) == 0:
        return 1
    return None


def derive_external_cursor_iteration_for_recovery(state: RunState) -> int | None:
    """Derive the fresh external Cursor iteration for recovery classification.

    Used only when structured evidence shows Cursor never started for the
    current external prompt. Prefers the typed field; otherwise allocates
    ``max(persisted iterations) + 1``.
    """

    github = state.github_pr_review
    if github is None:
        return None
    if github.lifecycle != "fixing_external_feedback":
        return None
    if not github.external_fix_prompt_path:
        return None
    if github.external_cursor_iteration is not None:
        return github.external_cursor_iteration
    return next_external_cursor_iteration(state)


def is_external_cursor_prompt_iteration(state: RunState, iteration_number: int) -> bool:
    """True when this iteration must deliver the exact external fix prompt."""

    github = state.github_pr_review
    if github is None or not github.external_fix_prompt_path:
        return False
    if github.lifecycle != "fixing_external_feedback":
        return False
    pending = pending_external_cursor_iteration(state)
    if pending is not None:
        return pending == iteration_number
    # Historical independent first turn without typed field.
    return iteration_number == 1 and max_iteration_number(state) == 0


def begin_external_local_review_budget(state: RunState) -> None:
    """Start a fresh local review budget for a post-external Cursor segment.

    Artifact iteration numbers remain monotonic via
    ``external_cursor_iteration``; only the budget counter is reset.
    """

    state.workflow.local_review_count = 0


def refresh_external_publication_patch_fingerprint(
    run_directory: Path,
    state: RunState,
    *,
    iteration_number: int,
) -> str:
    """Update ``gpr.staged_patch_sha256`` to the live raw fingerprint after acceptance.

    Call only for external-feedback corrections that completed a durable local
    iteration with no actionable findings, under run/repository locks, before
    transitioning to ``publishing_external_fix``. Validates baseline and
    normalized artifact equivalence first; fails closed on real content drift.
    """

    gpr = state.github_pr_review
    if gpr is None:
        raise ValidationError(
            "github_pr_review is required to refresh publication patch fingerprint"
        )
    if gpr.lifecycle != "fixing_external_feedback":
        raise ValidationError(
            "publication patch fingerprint refresh requires lifecycle fixing_external_feedback"
        )
    label = iteration_label(iteration_number)
    patch_artifact = run_directory / f"git/diffs/{label}.patch"
    if not patch_artifact.is_file():
        raise ValidationError(
            f"staged patch artifact missing for publication fingerprint refresh: "
            f"git/diffs/{label}.patch"
        )
    repo_root = Path(state.repository.root)
    live_hash = publication_staged_patch_fingerprint(repo_root, patch_artifact)
    state.github_pr_review = gpr.model_copy(update={"staged_patch_sha256": live_hash})
    return live_hash


def _external_pre_cursor_validator(state: RunState, run_directory: Path) -> None:
    del run_directory
    validate_external_feedback_pre_cursor(state)


def scheduled_cursor_turn_from_legacy_pr_state(
    state: RunState,
    run_directory: Path,
) -> ScheduledCursorTurn | None:
    """Convert pending Phase 15 external scheduling into a generic local turn.

    Returns ``None`` when no incomplete external Cursor turn is pending so the
    local planner can continue from durable staging/review artifacts alone.
    """

    pending = pending_external_cursor_iteration(state)
    if pending is None:
        return None
    if cursor_turn_complete(run_directory, pending):
        return None
    github = state.github_pr_review
    if github is None or not github.external_fix_prompt_path:
        return None
    turn = ScheduledCursorTurn(
        iteration_number=pending,
        prompt_path=github.external_fix_prompt_path,
        pre_cursor_validator=_external_pre_cursor_validator,
    )
    validate_scheduled_cursor_turn(turn, run_directory=run_directory, state=state)
    return turn


def finalize_legacy_pr_accepted_local_result(
    delivery: AcceptedReviewDelivery,
) -> AcceptedFinalizationResult:
    """Deterministic PR acceptance finalization under local locks (no worker spawn)."""

    run_directory = delivery.run_directory
    state = delivery.state
    iteration_number = delivery.iteration_number
    review = delivery.review
    result_message = delivery.result_message

    if state.github_pr_review is None:
        raise ValidationError("legacy PR acceptance requires github_pr_review state")
    if state.github_pr_review.lifecycle != "fixing_external_feedback":
        raise ValidationError("legacy PR acceptance requires lifecycle fixing_external_feedback")

    patch_sha = refresh_external_publication_patch_fingerprint(
        run_directory,
        state,
        iteration_number=iteration_number,
    )
    transition_status(state.status, RunStatus.PUBLISHING_EXTERNAL_FIX)
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "publishing_external_fix",
            "staged_patch_sha256": patch_sha,
        }
    )
    # Residual-risk tests may continue when Codex recorded no corrective action.
    if review.tests_status in {"failed", "blocked_environment", "skipped_findings_present"}:
        state.result = f"{result_message} Residual risk recorded; continuing automatic publication."
    else:
        state.result = result_message
    state.last_error = None
    save_run_state(run_directory, state)
    append_orchestrator_event(
        run_directory,
        run_id=state.run_id,
        component="orchestrator",
        event="pr_review_ready_to_publish",
        status=state.status.value,
        iteration=iteration_number,
        detail={"staged_patch_sha256": patch_sha},
    )
    return AcceptedFinalizationResult(
        needs_external_continuation=True,
        result_message=state.result or result_message,
    )


def build_legacy_pr_local_request(
    run_id: str,
    *,
    operation: LocalReviewOperation = LocalReviewOperation.RESUME,
    tool_policy: ToolCompatibilityPolicy | None = None,
) -> LocalReviewFixRequest:
    """Build a local request from Phase 15 scheduling fields (PR -> local only)."""

    run_directory, state = load_run(run_id)
    scheduled = scheduled_cursor_turn_from_legacy_pr_state(state, run_directory)
    on_accepted = None
    if state.github_pr_review is not None and (
        state.github_pr_review.lifecycle == "fixing_external_feedback" or scheduled is not None
    ):
        on_accepted = finalize_legacy_pr_accepted_local_result
    return LocalReviewFixRequest(
        run_id=run_id,
        operation=operation,
        tool_policy=tool_policy,
        scheduled_first_cursor_turn=scheduled,
        on_accepted=on_accepted,
    )


def resume_legacy_pr_local_fix_loop(
    run_id: str,
    *,
    tool_policy: ToolCompatibilityPolicy | None = None,
) -> LocalReviewFixResult:
    """Resume the local fix loop for a Phase 15 PR caller (no worker spawn)."""

    request = build_legacy_pr_local_request(
        run_id,
        operation=LocalReviewOperation.RESUME,
        tool_policy=tool_policy,
    )
    return run_local_review_fix(request)
