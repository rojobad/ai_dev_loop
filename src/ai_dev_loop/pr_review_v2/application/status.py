"""Read-only status projection for PR review v2 durable runs."""

from __future__ import annotations

from datetime import datetime

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    NextActionCategory,
    PrReviewStatus,
)
from ai_dev_loop.pr_review_v2.domain.common import SafeActionKind
from ai_dev_loop.pr_review_v2.domain.state import PrReviewState, active_effect, binding_of


def shorten_sha(sha: str | None, *, length: int = 12) -> str | None:
    if sha is None:
        return None
    return sha[:length]


def next_action_for(
    state: PrReviewState,
    *,
    effect_status: DispatchStatus | None,
    next_eligible_at: datetime | None,
    now: datetime,
) -> NextActionCategory:
    kind = state.kind
    if kind == "prepared":
        return NextActionCategory.START
    if kind == "completed":
        return NextActionCategory.TERMINAL_COMPLETED
    if kind == "failed":
        return NextActionCategory.TERMINAL_FAILED
    if kind == "aborted":
        return NextActionCategory.TERMINAL_ABORTED
    if kind == "waiting_for_user":
        return NextActionCategory.WAIT_FOR_USER
    if kind == "waiting_retry":
        return NextActionCategory.WAIT_FOR_RETRY
    if kind == "paused":
        safe_action = getattr(state, "safe_action", None)
        if safe_action is not None and safe_action.kind is SafeActionKind.INSPECT_ARTIFACTS:
            return NextActionCategory.INSPECT
        if safe_action is not None and safe_action.kind is SafeActionKind.RESUME_SAME_EFFECT:
            return NextActionCategory.RESUME
        return NextActionCategory.RESUME
    if kind == "reconciling_write":
        return NextActionCategory.RECONCILE
    if (
        effect_status is DispatchStatus.PENDING
        and next_eligible_at is not None
        and next_eligible_at > now
    ):
        return NextActionCategory.WAIT_FOR_ELIGIBILITY
    if effect_status in {DispatchStatus.PENDING, DispatchStatus.CLAIMED}:
        return NextActionCategory.EXECUTE_EFFECT
    if active_effect(state) is not None:
        return NextActionCategory.EXECUTE_EFFECT
    return NextActionCategory.NONE


def build_status(
    *,
    state: PrReviewState,
    run_version: int,
    updated_at: datetime,
    effect_status: DispatchStatus | None,
    effect_attempt: int | None,
    effect_max_attempts: int | None,
    next_eligible_at: datetime | None,
    last_error_kind: str | None,
    last_error_summary: str | None,
    lease_active: bool,
    lease_generation: int,
    lease_heartbeat_at: datetime | None,
    lease_expires_at: datetime | None,
    now: datetime,
) -> PrReviewStatus:
    binding = binding_of(state)
    effect = active_effect(state)
    if effect is None and state.kind == "waiting_retry":
        effect = active_effect(state)

    repo = None
    pr_number = None
    head_short = None
    if binding is not None:
        repo = binding.repository.name_with_owner
        pr_number = binding.pr_number
        head_short = shorten_sha(binding.head_sha)
    elif hasattr(state, "origin"):
        origin = state.origin
        if hasattr(origin, "repository"):
            repo = origin.repository.name_with_owner
        if hasattr(origin, "binding"):
            repo = origin.binding.repository.name_with_owner
            pr_number = origin.binding.pr_number
            head_short = shorten_sha(origin.binding.head_sha)

    safe_action_kind = None
    safe_action_condition = None
    if state.kind in {"paused", "waiting_for_user"}:
        safe_action = getattr(state, "safe_action", None)
        if safe_action is not None:
            safe_action_kind = safe_action.kind
            safe_action_condition = safe_action.condition

    last_error_kind_out = last_error_kind
    last_error_summary_out = last_error_summary
    if state.kind == "waiting_retry":
        last_error_kind_out = str(state.last_error.kind)
        last_error_summary_out = state.last_error.safe_summary
    elif state.kind == "paused" or state.kind == "failed":
        last_error_kind_out = str(state.reason)
        last_error_summary_out = state.safe_summary

    return PrReviewStatus(
        run_id=state.run_id,
        state_kind=state.kind,
        run_version=run_version,
        cycle_number=state.cycle_number,
        updated_at=updated_at,
        repository=repo,
        pr_number=pr_number,
        head_sha_short=head_short,
        active_effect_kind=effect.kind if effect is not None else None,
        effect_status=effect_status,
        effect_attempt=effect_attempt
        if effect_attempt is not None
        else (effect.attempt if effect is not None else None),
        effect_max_attempts=effect_max_attempts
        if effect_max_attempts is not None
        else (effect.max_attempts if effect is not None else None),
        next_eligible_at=next_eligible_at,
        last_error_kind=last_error_kind_out,
        last_error_summary=last_error_summary_out,
        lease_active=lease_active,
        lease_generation=lease_generation,
        lease_heartbeat_at=lease_heartbeat_at,
        lease_expires_at=lease_expires_at,
        ambiguous_write_pending=state.kind == "reconciling_write",
        safe_action_kind=safe_action_kind,
        safe_action_condition=safe_action_condition,
        next_action=next_action_for(
            state,
            effect_status=effect_status,
            next_eligible_at=next_eligible_at,
            now=now,
        ),
    )
