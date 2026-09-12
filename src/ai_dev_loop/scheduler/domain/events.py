"""Scheduler journal event models."""

from __future__ import annotations

from typing import Annotated

from pydantic import Discriminator, Field, Tag, TypeAdapter, field_validator

from ai_dev_loop.scheduler.domain.admission_contract import (
    ADMISSION_BLOCKED_EVENT_KIND,
    ADMISSION_EVENT_KIND,
)
from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex, UuidSessionId

SUBMITTED_EVENT_KIND = "run_submitted"
AUTHORIZED_EVENT_KIND = "run_authorized"
TIMER_FIRED_EVENT_KIND = "timer_fired"
SYNTHETIC_EFFECT_COMPLETED_EVENT_KIND = "synthetic_effect_completed"
ATTEMPT_LAUNCH_REQUESTED_EVENT_KIND = "attempt_launch_requested"
ATTEMPT_COMPLETED_EVENT_KIND = "attempt_completed"
ATTEMPT_UNCERTAIN_EVENT_KIND = "attempt_uncertain"
TICK_STALE_REJECTED_EVENT_KIND = "tick_stale_rejected"
PREFLIGHT_COMPLETED_EVENT_KIND = "preflight_completed"
PREFLIGHT_BLOCKED_EVENT_KIND = "preflight_blocked"
CURSOR_CHAT_CREATED_EVENT_KIND = "cursor_chat_created"
CURSOR_CHAT_BLOCKED_EVENT_KIND = "cursor_chat_blocked"
CURSOR_TURN_COMPLETED_EVENT_KIND = "cursor_turn_completed"
CURSOR_TURN_BLOCKED_EVENT_KIND = "cursor_turn_blocked"
CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND = "cursor_usage_limit_detected"
STAGING_COMPLETED_EVENT_KIND = "staging_completed"
STAGING_BLOCKED_EVENT_KIND = "staging_blocked"
AWAITING_CODEX_REVIEW_EVENT_KIND = "awaiting_codex_review_entered"
CODEX_REVIEWER_BOUND_EVENT_KIND = "codex_reviewer_bound"
CODEX_BOOTSTRAP_UNCERTAIN_EVENT_KIND = "codex_bootstrap_uncertain"
CODEX_REVIEW_SCHEDULED_EVENT_KIND = "codex_review_scheduled"
CODEX_REVIEW_COMPLETED_EVENT_KIND = "codex_review_completed"
CODEX_REVIEW_BLOCKED_EVENT_KIND = "codex_review_blocked"
CODEX_USAGE_CAPACITY_DETECTED_EVENT_KIND = "codex_usage_capacity_detected"
CODEX_CAPACITY_AVAILABLE_EVENT_KIND = "codex_capacity_available"
WAITING_FOR_CURSOR_FIX_EVENT_KIND = "waiting_for_cursor_fix_entered"
RUN_COMPLETED_EVENT_KIND = "run_completed"
RUN_COMPLETED_WITH_RESIDUAL_RISK_EVENT_KIND = "run_completed_with_residual_risk"
MAX_ITERATIONS_REACHED_EVENT_KIND = "max_iterations_reached"
ABORT_REQUESTED_EVENT_KIND = "abort_requested"
RUN_ABORTED_EVENT_KIND = "run_aborted"
ATTEMPT_RESULT_STALE_EVENT_KIND = "attempt_result_stale"


class RunSubmittedEvent(DomainModel):
    kind: str = Field(default=SUBMITTED_EVENT_KIND)
    run_id: str
    idempotency_key: Sha256Hex
    worktree_key: Sha256Hex
    reused_existing: bool

    @field_validator("kind")
    @classmethod
    def kind_is_run_submitted(cls, value: str) -> str:
        if value != SUBMITTED_EVENT_KIND:
            raise ValueError("kind must be run_submitted")
        return value


class RunAuthorizedEvent(DomainModel):
    kind: str = Field(default=AUTHORIZED_EVENT_KIND)
    run_id: str
    controller_session_id: UuidSessionId | None = None
    idempotent_replay: bool

    @field_validator("kind")
    @classmethod
    def kind_is_run_authorized(cls, value: str) -> str:
        if value != AUTHORIZED_EVENT_KIND:
            raise ValueError("kind must be run_authorized")
        return value


class WorktreeAdmittedEvent(DomainModel):
    kind: str = Field(default=ADMISSION_EVENT_KIND)
    run_id: str
    admission_status_artifact_path: NonEmptyStr
    admission_status_sha256: Sha256Hex
    resolved_root: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_worktree_admitted(cls, value: str) -> str:
        if value != ADMISSION_EVENT_KIND:
            raise ValueError("kind must be worktree_admitted")
        return value


class WorktreeAdmissionBlockedEvent(DomainModel):
    kind: str = Field(default=ADMISSION_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_worktree_admission_blocked(cls, value: str) -> str:
        if value != ADMISSION_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be worktree_admission_blocked")
        return value


class TimerFiredEvent(DomainModel):
    kind: str = Field(default=TIMER_FIRED_EVENT_KIND)
    run_id: str
    timer_id: str
    target_effect_id: str
    expected_run_version: int

    @field_validator("kind")
    @classmethod
    def kind_is_timer_fired(cls, value: str) -> str:
        if value != TIMER_FIRED_EVENT_KIND:
            raise ValueError("kind must be timer_fired")
        return value


class SyntheticEffectCompletedEvent(DomainModel):
    kind: str = Field(default=SYNTHETIC_EFFECT_COMPLETED_EVENT_KIND)
    run_id: str
    effect_id: str
    dispatch_id: str
    claim_id: str

    @field_validator("kind")
    @classmethod
    def kind_is_synthetic_effect_completed(cls, value: str) -> str:
        if value != SYNTHETIC_EFFECT_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be synthetic_effect_completed")
        return value


class AttemptLaunchRequestedEvent(DomainModel):
    kind: str = Field(default=ATTEMPT_LAUNCH_REQUESTED_EVENT_KIND)
    run_id: str
    attempt_id: str
    dispatch_id: str
    claim_id: str
    unit_identity: str
    launch_nonce: str
    launch_intent_sha256: str

    @field_validator("kind")
    @classmethod
    def kind_is_attempt_launch_requested(cls, value: str) -> str:
        if value != ATTEMPT_LAUNCH_REQUESTED_EVENT_KIND:
            raise ValueError("kind must be attempt_launch_requested")
        return value


class AttemptCompletedEvent(DomainModel):
    kind: str = Field(default=ATTEMPT_COMPLETED_EVENT_KIND)
    run_id: str
    attempt_id: str
    dispatch_id: str
    claim_id: str
    completion_fence_id: str
    termination_class: NonEmptyStr
    exit_code: int

    @field_validator("kind")
    @classmethod
    def kind_is_attempt_completed(cls, value: str) -> str:
        if value != ATTEMPT_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be attempt_completed")
        return value


class AttemptUncertainEvent(DomainModel):
    kind: str = Field(default=ATTEMPT_UNCERTAIN_EVENT_KIND)
    run_id: str
    attempt_id: str
    dispatch_id: str
    claim_id: str
    safe_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_attempt_uncertain(cls, value: str) -> str:
        if value != ATTEMPT_UNCERTAIN_EVENT_KIND:
            raise ValueError("kind must be attempt_uncertain")
        return value


class TickStaleRejectedEvent(DomainModel):
    kind: str = Field(default=TICK_STALE_REJECTED_EVENT_KIND)
    run_id: str
    rejection_kind: NonEmptyStr
    safe_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_tick_stale_rejected(cls, value: str) -> str:
        if value != TICK_STALE_REJECTED_EVENT_KIND:
            raise ValueError("kind must be tick_stale_rejected")
        return value


class PreflightCompletedEvent(DomainModel):
    kind: str = Field(default=PREFLIGHT_COMPLETED_EVENT_KIND)
    run_id: str

    @field_validator("kind")
    @classmethod
    def kind_is_preflight_completed(cls, value: str) -> str:
        if value != PREFLIGHT_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be preflight_completed")
        return value


class PreflightBlockedEvent(DomainModel):
    kind: str = Field(default=PREFLIGHT_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_preflight_blocked(cls, value: str) -> str:
        if value != PREFLIGHT_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be preflight_blocked")
        return value


class CursorChatCreatedEvent(DomainModel):
    kind: str = Field(default=CURSOR_CHAT_CREATED_EVENT_KIND)
    run_id: str
    chat_id: UuidSessionId
    chat_artifact_path: NonEmptyStr
    chat_artifact_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_cursor_chat_created(cls, value: str) -> str:
        if value != CURSOR_CHAT_CREATED_EVENT_KIND:
            raise ValueError("kind must be cursor_chat_created")
        return value


class CursorChatBlockedEvent(DomainModel):
    kind: str = Field(default=CURSOR_CHAT_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_cursor_chat_blocked(cls, value: str) -> str:
        if value != CURSOR_CHAT_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be cursor_chat_blocked")
        return value


class CursorTurnCompletedEvent(DomainModel):
    kind: str = Field(default=CURSOR_TURN_COMPLETED_EVENT_KIND)
    run_id: str
    iteration: int
    cursor_output_fingerprint_path: NonEmptyStr
    cursor_output_fingerprint_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_cursor_turn_completed(cls, value: str) -> str:
        if value != CURSOR_TURN_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be cursor_turn_completed")
        return value


class CursorTurnBlockedEvent(DomainModel):
    kind: str = Field(default=CURSOR_TURN_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_cursor_turn_blocked(cls, value: str) -> str:
        if value != CURSOR_TURN_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be cursor_turn_blocked")
        return value


class CursorUsageLimitDetectedEvent(DomainModel):
    kind: str = Field(default=CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND)
    run_id: str
    iteration: int
    wait_until: NonEmptyStr
    usage_limit_fingerprint_path: NonEmptyStr
    usage_limit_fingerprint_sha256: Sha256Hex
    continuation_envelope_path: NonEmptyStr
    continuation_envelope_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_cursor_usage_limit_detected(cls, value: str) -> str:
        if value != CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND:
            raise ValueError("kind must be cursor_usage_limit_detected")
        return value


class StagingCompletedEvent(DomainModel):
    kind: str = Field(default=STAGING_COMPLETED_EVENT_KIND)
    run_id: str
    iteration: int
    staged_patch_path: NonEmptyStr
    staged_patch_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_staging_completed(cls, value: str) -> str:
        if value != STAGING_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be staging_completed")
        return value


class StagingBlockedEvent(DomainModel):
    kind: str = Field(default=STAGING_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_staging_blocked(cls, value: str) -> str:
        if value != STAGING_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be staging_blocked")
        return value


class AwaitingCodexReviewEnteredEvent(DomainModel):
    kind: str = Field(default=AWAITING_CODEX_REVIEW_EVENT_KIND)
    run_id: str

    @field_validator("kind")
    @classmethod
    def kind_is_awaiting_codex_review_entered(cls, value: str) -> str:
        if value != AWAITING_CODEX_REVIEW_EVENT_KIND:
            raise ValueError("kind must be awaiting_codex_review_entered")
        return value


class CodexReviewerBoundEvent(DomainModel):
    kind: str = Field(default=CODEX_REVIEWER_BOUND_EVENT_KIND)
    run_id: str
    reviewer_session_id_prefix: NonEmptyStr
    binding_artifact_path: NonEmptyStr
    binding_artifact_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_codex_reviewer_bound(cls, value: str) -> str:
        if value != CODEX_REVIEWER_BOUND_EVENT_KIND:
            raise ValueError("kind must be codex_reviewer_bound")
        return value


class CodexBootstrapUncertainEvent(DomainModel):
    kind: str = Field(default=CODEX_BOOTSTRAP_UNCERTAIN_EVENT_KIND)
    run_id: str
    uncertainty_reason: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_codex_bootstrap_uncertain(cls, value: str) -> str:
        if value != CODEX_BOOTSTRAP_UNCERTAIN_EVENT_KIND:
            raise ValueError("kind must be codex_bootstrap_uncertain")
        return value


class CodexReviewScheduledEvent(DomainModel):
    kind: str = Field(default=CODEX_REVIEW_SCHEDULED_EVENT_KIND)
    run_id: str
    review_iteration: int
    effect_kind: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_codex_review_scheduled(cls, value: str) -> str:
        if value != CODEX_REVIEW_SCHEDULED_EVENT_KIND:
            raise ValueError("kind must be codex_review_scheduled")
        return value


class CodexReviewCompletedEvent(DomainModel):
    kind: str = Field(default=CODEX_REVIEW_COMPLETED_EVENT_KIND)
    run_id: str
    review_iteration: int
    has_actionable_findings: bool
    review_result_path: NonEmptyStr
    review_result_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_codex_review_completed(cls, value: str) -> str:
        if value != CODEX_REVIEW_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be codex_review_completed")
        return value


class CodexReviewBlockedEvent(DomainModel):
    kind: str = Field(default=CODEX_REVIEW_BLOCKED_EVENT_KIND)
    run_id: str
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_codex_review_blocked(cls, value: str) -> str:
        if value != CODEX_REVIEW_BLOCKED_EVENT_KIND:
            raise ValueError("kind must be codex_review_blocked")
        return value


class CodexUsageCapacityDetectedEvent(DomainModel):
    kind: str = Field(default=CODEX_USAGE_CAPACITY_DETECTED_EVENT_KIND)
    run_id: str
    review_iteration: int

    @field_validator("kind")
    @classmethod
    def kind_is_codex_usage_capacity_detected(cls, value: str) -> str:
        if value != CODEX_USAGE_CAPACITY_DETECTED_EVENT_KIND:
            raise ValueError("kind must be codex_usage_capacity_detected")
        return value


class CodexCapacityAvailableEvent(DomainModel):
    kind: str = Field(default=CODEX_CAPACITY_AVAILABLE_EVENT_KIND)
    run_id: str
    review_iteration: int

    @field_validator("kind")
    @classmethod
    def kind_is_codex_capacity_available(cls, value: str) -> str:
        if value != CODEX_CAPACITY_AVAILABLE_EVENT_KIND:
            raise ValueError("kind must be codex_capacity_available")
        return value


class WaitingForCursorFixEnteredEvent(DomainModel):
    kind: str = Field(default=WAITING_FOR_CURSOR_FIX_EVENT_KIND)
    run_id: str
    review_iteration: int
    fix_prompt_path: NonEmptyStr
    fix_prompt_sha256: Sha256Hex
    correction_envelope_path: NonEmptyStr
    correction_envelope_sha256: Sha256Hex

    @field_validator("kind")
    @classmethod
    def kind_is_waiting_for_cursor_fix_entered(cls, value: str) -> str:
        if value != WAITING_FOR_CURSOR_FIX_EVENT_KIND:
            raise ValueError("kind must be waiting_for_cursor_fix_entered")
        return value


class RunCompletedEvent(DomainModel):
    kind: str = Field(default=RUN_COMPLETED_EVENT_KIND)
    run_id: str
    review_iteration: int

    @field_validator("kind")
    @classmethod
    def kind_is_run_completed(cls, value: str) -> str:
        if value != RUN_COMPLETED_EVENT_KIND:
            raise ValueError("kind must be run_completed")
        return value


class RunCompletedWithResidualRiskEvent(DomainModel):
    kind: str = Field(default=RUN_COMPLETED_WITH_RESIDUAL_RISK_EVENT_KIND)
    run_id: str
    review_iteration: int

    @field_validator("kind")
    @classmethod
    def kind_is_run_completed_with_residual_risk(cls, value: str) -> str:
        if value != RUN_COMPLETED_WITH_RESIDUAL_RISK_EVENT_KIND:
            raise ValueError("kind must be run_completed_with_residual_risk")
        return value


class MaxIterationsReachedEvent(DomainModel):
    kind: str = Field(default=MAX_ITERATIONS_REACHED_EVENT_KIND)
    run_id: str
    review_iteration: int

    @field_validator("kind")
    @classmethod
    def kind_is_max_iterations_reached(cls, value: str) -> str:
        if value != MAX_ITERATIONS_REACHED_EVENT_KIND:
            raise ValueError("kind must be max_iterations_reached")
        return value


class AbortRequestedEvent(DomainModel):
    kind: str = Field(default=ABORT_REQUESTED_EVENT_KIND)
    run_id: str
    reason: NonEmptyStr = "user_requested_abort"

    @field_validator("kind")
    @classmethod
    def kind_is_abort_requested(cls, value: str) -> str:
        if value != ABORT_REQUESTED_EVENT_KIND:
            raise ValueError("kind must be abort_requested")
        return value


class RunAbortedEvent(DomainModel):
    kind: str = Field(default=RUN_ABORTED_EVENT_KIND)
    run_id: str
    reason: NonEmptyStr
    prior_state_kind: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_run_aborted(cls, value: str) -> str:
        if value != RUN_ABORTED_EVENT_KIND:
            raise ValueError("kind must be run_aborted")
        return value


class AttemptResultStaleEvent(DomainModel):
    kind: str = Field(default=ATTEMPT_RESULT_STALE_EVENT_KIND)
    run_id: str
    attempt_id: str
    rejection_kind: NonEmptyStr
    safe_summary: NonEmptyStr

    @field_validator("kind")
    @classmethod
    def kind_is_attempt_result_stale(cls, value: str) -> str:
        if value != ATTEMPT_RESULT_STALE_EVENT_KIND:
            raise ValueError("kind must be attempt_result_stale")
        return value


def _scheduler_event_discriminator(value: object) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str):
            return kind
    kind = getattr(value, "kind", None)
    if isinstance(kind, str):
        return kind
    raise ValueError("scheduler event payload must include kind")


SchedulerEvent = Annotated[
    Annotated[RunSubmittedEvent, Tag(SUBMITTED_EVENT_KIND)]
    | Annotated[RunAuthorizedEvent, Tag(AUTHORIZED_EVENT_KIND)]
    | Annotated[WorktreeAdmittedEvent, Tag(ADMISSION_EVENT_KIND)]
    | Annotated[WorktreeAdmissionBlockedEvent, Tag(ADMISSION_BLOCKED_EVENT_KIND)]
    | Annotated[TimerFiredEvent, Tag(TIMER_FIRED_EVENT_KIND)]
    | Annotated[SyntheticEffectCompletedEvent, Tag(SYNTHETIC_EFFECT_COMPLETED_EVENT_KIND)]
    | Annotated[AttemptLaunchRequestedEvent, Tag(ATTEMPT_LAUNCH_REQUESTED_EVENT_KIND)]
    | Annotated[AttemptCompletedEvent, Tag(ATTEMPT_COMPLETED_EVENT_KIND)]
    | Annotated[AttemptUncertainEvent, Tag(ATTEMPT_UNCERTAIN_EVENT_KIND)]
    | Annotated[TickStaleRejectedEvent, Tag(TICK_STALE_REJECTED_EVENT_KIND)]
    | Annotated[PreflightCompletedEvent, Tag(PREFLIGHT_COMPLETED_EVENT_KIND)]
    | Annotated[PreflightBlockedEvent, Tag(PREFLIGHT_BLOCKED_EVENT_KIND)]
    | Annotated[CursorChatCreatedEvent, Tag(CURSOR_CHAT_CREATED_EVENT_KIND)]
    | Annotated[CursorChatBlockedEvent, Tag(CURSOR_CHAT_BLOCKED_EVENT_KIND)]
    | Annotated[CursorTurnCompletedEvent, Tag(CURSOR_TURN_COMPLETED_EVENT_KIND)]
    | Annotated[CursorTurnBlockedEvent, Tag(CURSOR_TURN_BLOCKED_EVENT_KIND)]
    | Annotated[CursorUsageLimitDetectedEvent, Tag(CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND)]
    | Annotated[StagingCompletedEvent, Tag(STAGING_COMPLETED_EVENT_KIND)]
    | Annotated[StagingBlockedEvent, Tag(STAGING_BLOCKED_EVENT_KIND)]
    | Annotated[AwaitingCodexReviewEnteredEvent, Tag(AWAITING_CODEX_REVIEW_EVENT_KIND)]
    | Annotated[CodexReviewerBoundEvent, Tag(CODEX_REVIEWER_BOUND_EVENT_KIND)]
    | Annotated[CodexBootstrapUncertainEvent, Tag(CODEX_BOOTSTRAP_UNCERTAIN_EVENT_KIND)]
    | Annotated[CodexReviewScheduledEvent, Tag(CODEX_REVIEW_SCHEDULED_EVENT_KIND)]
    | Annotated[CodexReviewCompletedEvent, Tag(CODEX_REVIEW_COMPLETED_EVENT_KIND)]
    | Annotated[CodexReviewBlockedEvent, Tag(CODEX_REVIEW_BLOCKED_EVENT_KIND)]
    | Annotated[CodexUsageCapacityDetectedEvent, Tag(CODEX_USAGE_CAPACITY_DETECTED_EVENT_KIND)]
    | Annotated[CodexCapacityAvailableEvent, Tag(CODEX_CAPACITY_AVAILABLE_EVENT_KIND)]
    | Annotated[WaitingForCursorFixEnteredEvent, Tag(WAITING_FOR_CURSOR_FIX_EVENT_KIND)]
    | Annotated[RunCompletedEvent, Tag(RUN_COMPLETED_EVENT_KIND)]
    | Annotated[RunCompletedWithResidualRiskEvent, Tag(RUN_COMPLETED_WITH_RESIDUAL_RISK_EVENT_KIND)]
    | Annotated[MaxIterationsReachedEvent, Tag(MAX_ITERATIONS_REACHED_EVENT_KIND)]
    | Annotated[AbortRequestedEvent, Tag(ABORT_REQUESTED_EVENT_KIND)]
    | Annotated[RunAbortedEvent, Tag(RUN_ABORTED_EVENT_KIND)]
    | Annotated[AttemptResultStaleEvent, Tag(ATTEMPT_RESULT_STALE_EVENT_KIND)],
    Discriminator(_scheduler_event_discriminator),
]

SCHEDULER_EVENT_ADAPTER: TypeAdapter[SchedulerEvent] = TypeAdapter(SchedulerEvent)


def parse_scheduler_event(payload: object) -> SchedulerEvent:
    return SCHEDULER_EVENT_ADAPTER.validate_python(payload)
