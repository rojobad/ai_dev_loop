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
    controller_session_id: UuidSessionId
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
    | Annotated[TickStaleRejectedEvent, Tag(TICK_STALE_REJECTED_EVENT_KIND)],
    Discriminator(_scheduler_event_discriminator),
]

SCHEDULER_EVENT_ADAPTER: TypeAdapter[SchedulerEvent] = TypeAdapter(SchedulerEvent)


def parse_scheduler_event(payload: object) -> SchedulerEvent:
    return SCHEDULER_EVENT_ADAPTER.validate_python(payload)
