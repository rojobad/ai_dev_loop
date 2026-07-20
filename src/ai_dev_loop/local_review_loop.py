"""Typed reusable local Cursor -> staging -> Codex review/fix boundary.

Invocation types are non-persisted. Durable identities and configuration remain
authoritative in ``RunState`` selected by ``run_id``. This module must not import
PR commands, workers, GitHub/publication runners, or inspect ``github_pr_review``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.runners.tool_updates import ToolCompatibilityPolicy
from ai_dev_loop.state import RunState

# Generic read-only pre-Cursor validator supplied by the caller.
PreCursorValidator = Callable[[RunState, Path], None]


class LocalReviewOperation(StrEnum):
    START = "start"
    RESUME = "resume"


class LocalReviewOutcome(StrEnum):
    ACCEPTED = "accepted"
    ACCEPTED_WITH_RESIDUAL_RISK = "accepted_with_residual_risk"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    ABORTED = "aborted"
    # Mid-loop / non-terminal public statuses retained for adapter compatibility.
    WAITING_FOR_CURSOR_FIX = "waiting_for_cursor_fix"
    RUNNING_CURSOR = "running_cursor"
    STAGING = "staging"
    REVIEWING = "reviewing"
    VALIDATING = "validating"
    PREPARED = "prepared"
    OTHER = "other"


@dataclass(frozen=True)
class ScheduledCursorTurn:
    """Caller-scheduled first Cursor turn for this local invocation.

    The prompt path is relative to the run directory. The optional validator is
    read-only with respect to GitHub/network and must run before attempt artifacts.
    """

    iteration_number: int
    prompt_path: str
    pre_cursor_validator: PreCursorValidator | None = None


@dataclass(frozen=True)
class AcceptedReviewDelivery:
    """Validated no-findings review delivered to a caller finalizer under locks."""

    run_directory: Path
    state: RunState
    iteration_number: int
    review: CodexReviewResult
    review_artifact_path: str
    result_message: str


@dataclass(frozen=True)
class AcceptedFinalizationResult:
    """Deterministic state/artifact finalization outcome (no external I/O)."""

    needs_external_continuation: bool = False
    result_message: str | None = None


AcceptedResultFinalizer = Callable[[AcceptedReviewDelivery], AcceptedFinalizationResult]


@dataclass(frozen=True)
class LocalReviewFixRequest:
    """Non-persisted request to start or resume a local review/fix execution."""

    run_id: str
    operation: LocalReviewOperation
    tool_policy: ToolCompatibilityPolicy | None = None
    scheduled_first_cursor_turn: ScheduledCursorTurn | None = None
    on_accepted: AcceptedResultFinalizer | None = None


@dataclass(frozen=True)
class LocalReviewFixResult:
    """Typed local boundary result. Public A/B adapters reuse these fields."""

    run_id: str
    status: str
    chat_id: str
    iteration_count: int
    latest_staged_diff_path: str | None
    latest_review_path: str | None
    result_message: str
    outcome: LocalReviewOutcome
    needs_external_continuation: bool = False

    @property
    def staged_diff_path(self) -> str | None:
        return self.latest_staged_diff_path

    @property
    def iteration_dir(self) -> str:
        if self.iteration_count <= 0:
            return "cursor/iterations/01"
        return f"cursor/iterations/{self.iteration_count:02d}"


def outcome_from_status(status: str) -> LocalReviewOutcome:
    mapping = {
        "completed": LocalReviewOutcome.ACCEPTED,
        "completed_with_residual_risk": LocalReviewOutcome.ACCEPTED_WITH_RESIDUAL_RISK,
        "max_iterations_reached": LocalReviewOutcome.MAX_ITERATIONS_REACHED,
        "interrupted": LocalReviewOutcome.INTERRUPTED,
        "failed": LocalReviewOutcome.FAILED,
        "aborted": LocalReviewOutcome.ABORTED,
        "waiting_for_cursor_fix": LocalReviewOutcome.WAITING_FOR_CURSOR_FIX,
        "running_cursor": LocalReviewOutcome.RUNNING_CURSOR,
        "staging": LocalReviewOutcome.STAGING,
        "reviewing": LocalReviewOutcome.REVIEWING,
        "validating": LocalReviewOutcome.VALIDATING,
        "prepared": LocalReviewOutcome.PREPARED,
        # Typed outcome names also accepted for direct use.
        "accepted": LocalReviewOutcome.ACCEPTED,
        "accepted_with_residual_risk": LocalReviewOutcome.ACCEPTED_WITH_RESIDUAL_RISK,
    }
    return mapping.get(status, LocalReviewOutcome.OTHER)


def validate_scheduled_cursor_turn(
    turn: ScheduledCursorTurn,
    *,
    run_directory: Path | None = None,
    state: RunState | None = None,
) -> None:
    """Validate explicit scheduled-turn inputs before Cursor attempt artifacts."""

    if not isinstance(turn.iteration_number, int) or turn.iteration_number < 1:
        raise ValidationError("scheduled Cursor turn requires a positive iteration number")
    prompt_path = turn.prompt_path
    if not isinstance(prompt_path, str) or not prompt_path.strip():
        raise ValidationError("scheduled Cursor turn prompt path is missing or empty")
    if prompt_path.strip() != prompt_path:
        raise ValidationError(
            "scheduled Cursor turn prompt path has leading or trailing whitespace"
        )
    candidate = Path(prompt_path)
    if candidate.is_absolute():
        raise ValidationError(
            "scheduled Cursor turn prompt path must be relative to the run directory"
        )
    if ".." in candidate.parts:
        raise ValidationError("scheduled Cursor turn prompt path must not escape the run directory")

    if run_directory is not None:
        prompt_file = (run_directory / prompt_path).resolve()
        run_root = run_directory.resolve()
        try:
            prompt_file.relative_to(run_root)
        except ValueError as exc:
            raise ValidationError(
                "scheduled Cursor turn prompt path must stay under the run directory"
            ) from exc
        if not prompt_file.is_file():
            raise ValidationError(f"scheduled Cursor turn prompt missing: {prompt_path}")
        text = prompt_file.read_bytes().decode("utf-8")
        if not text.strip():
            raise ValidationError(f"scheduled Cursor turn prompt is empty: {prompt_path}")

    if state is not None and run_directory is not None:
        from ai_dev_loop.resume_planner import cursor_turn_complete

        if cursor_turn_complete(run_directory, turn.iteration_number):
            raise ValidationError(
                "scheduled Cursor turn collides with a completed iteration "
                f"{turn.iteration_number:02d}"
            )
        for entry in state.iterations:
            if entry.get("number") != turn.iteration_number:
                continue
            cursor_section = entry.get("cursor")
            if isinstance(cursor_section, dict) and cursor_section.get("exit_code") == 0:
                raise ValidationError(
                    "scheduled Cursor turn collides with a completed iteration "
                    f"{turn.iteration_number:02d}"
                )


def run_local_review_fix(request: LocalReviewFixRequest) -> LocalReviewFixResult:
    """Authoritative entry point for the reusable local review/fix capability."""

    from ai_dev_loop.workflow_engine import execute_local_review_fix

    return execute_local_review_fix(request)
