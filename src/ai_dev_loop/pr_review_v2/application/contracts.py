"""Typed application contracts for the PR review v2 durable engine."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    NonEmptyId,
    NonEmptyStr,
    SafeAction,
    SafeActionKind,
    UtcInstant,
)
from ai_dev_loop.pr_review_v2.domain.effects import PrReviewEffect
from ai_dev_loop.pr_review_v2.domain.events import PrReviewEvent
from ai_dev_loop.pr_review_v2.domain.state import PrReviewState


class AppModel(BaseModel):
    """Frozen application DTO base."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EventDisposition(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    STALE = "stale"
    DUPLICATE = "duplicate"


class DispatchStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class TimerStatus(StrEnum):
    PENDING = "pending"
    FIRED = "fired"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class LeaseStatus(StrEnum):
    INACTIVE = "inactive"
    ACTIVE = "active"
    EXPIRED = "expired"
    ABORTED = "aborted"


class NextActionCategory(StrEnum):
    START = "start"
    EXECUTE_EFFECT = "execute_effect"
    WAIT_FOR_ELIGIBILITY = "wait_for_eligibility"
    WAIT_FOR_RETRY = "wait_for_retry"
    WAIT_FOR_USER = "wait_for_user"
    RESUME = "resume"
    RECONCILE = "reconcile"
    INSPECT = "inspect"
    TERMINAL_COMPLETED = "terminal_completed"
    TERMINAL_FAILED = "terminal_failed"
    TERMINAL_ABORTED = "terminal_aborted"
    NONE = "none"


class PrReviewEngineErrorKind(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    COLLISION = "collision"
    CORRUPTION = "corruption"
    BUSY = "busy"
    FENCED = "fenced"
    SCHEMA = "schema"
    INTERNAL = "internal"


class PrReviewEngineError(Exception):
    """Safe typed application/infrastructure error."""

    def __init__(
        self,
        kind: PrReviewEngineErrorKind,
        safe_message: str,
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(safe_message)
        self.kind = kind
        self.safe_message = safe_message
        self.code = code


class EventSubmission(AppModel):
    submission_id: NonEmptyId
    run_id: NonEmptyId
    expected_version: PositiveInt
    event: PrReviewEvent


class ApplicationReceipt(AppModel):
    disposition: EventDisposition
    submission_id: NonEmptyId
    event_id: NonEmptyId
    run_id: NonEmptyId
    sequence: PositiveInt | None = None
    expected_run_version: PositiveInt | None = None
    observed_run_version: PositiveInt
    resulting_run_version: PositiveInt | None = None
    rejection_code: str | None = None
    safe_detail: NonEmptyStr | None = None
    state: PrReviewState | None = None
    effects: tuple[PrReviewEffect, ...] = ()
    duplicate_of_submission: bool = False


class LeaseAcquireResult(AppModel):
    run_id: NonEmptyId
    owner_id: NonEmptyId
    generation: int = Field(ge=0)
    status: LeaseStatus
    acquired_at: UtcInstant
    heartbeat_at: UtcInstant
    expires_at: UtcInstant


class LeaseHeartbeatResult(AppModel):
    run_id: NonEmptyId
    owner_id: NonEmptyId
    generation: int = Field(ge=0)
    status: LeaseStatus
    heartbeat_at: UtcInstant
    expires_at: UtcInstant
    accepted: bool


class LeaseReleaseResult(AppModel):
    run_id: NonEmptyId
    owner_id: NonEmptyId
    generation: int = Field(ge=0)
    status: LeaseStatus
    accepted: bool


class EffectClaim(AppModel):
    dispatch_id: NonEmptyId
    claim_id: NonEmptyId
    run_id: NonEmptyId
    effect: PrReviewEffect
    attempt: PositiveInt
    max_attempts: PositiveInt
    classification: NonEmptyStr
    claimed_run_version: PositiveInt
    owner_id: NonEmptyId
    lease_generation: PositiveInt
    claimed_at: UtcInstant
    lease_expires_at: UtcInstant
    completion_token: EffectCompletionToken


class EffectClaimResult(AppModel):
    claim: EffectClaim | None = None
    recovered: bool = False
    reason: NonEmptyStr | None = None


class EffectCompletionRequest(AppModel):
    submission_id: NonEmptyId
    dispatch_id: NonEmptyId
    claim_id: NonEmptyId
    owner_id: NonEmptyId
    lease_generation: PositiveInt
    event: PrReviewEvent
    # When True, complete_claim must fence as STALE/REJECTED before reducer mutation
    # even if the durable lease row still appears active (e.g. best-effort release failed).
    lease_authority_lost: bool = False


class TimerFireReceipt(AppModel):
    timer_id: NonEmptyId
    run_id: NonEmptyId
    disposition: EventDisposition
    event_id: NonEmptyId | None = None
    resulting_run_version: PositiveInt | None = None
    superseded: bool = False


class PrReviewStatus(AppModel):
    run_id: NonEmptyId
    state_kind: NonEmptyStr
    run_version: PositiveInt
    cycle_number: PositiveInt
    updated_at: UtcInstant
    repository: NonEmptyStr | None = None
    pr_number: PositiveInt | None = None
    head_sha_short: str | None = None
    active_effect_kind: NonEmptyStr | None = None
    effect_status: DispatchStatus | None = None
    effect_attempt: PositiveInt | None = None
    effect_max_attempts: PositiveInt | None = None
    next_eligible_at: UtcInstant | None = None
    last_error_kind: NonEmptyStr | None = None
    last_error_summary: NonEmptyStr | None = None
    lease_active: bool
    lease_generation: int = Field(ge=0)
    lease_heartbeat_at: UtcInstant | None = None
    lease_expires_at: UtcInstant | None = None
    ambiguous_write_pending: bool
    safe_action_kind: SafeActionKind | None = None
    safe_action_condition: NonEmptyStr | None = None
    next_action: NextActionCategory


class WorkerStepResult(AppModel):
    run_id: NonEmptyId
    claimed: bool
    completed: bool
    disposition: EventDisposition | None = None
    dispatch_id: NonEmptyId | None = None
    effect_kind: NonEmptyStr | None = None
    safe_detail: NonEmptyStr | None = None


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


@runtime_checkable
class IdFactory(Protocol):
    def new_id(self, prefix: str) -> str: ...


@runtime_checkable
class FaultHook(Protocol):
    def maybe_raise(self, checkpoint: str) -> None: ...


@runtime_checkable
class EffectExecutor(Protocol):
    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> PrReviewEvent: ...


# Re-export SafeAction for status builders without widening domain imports in callers.
__all__ = [
    "AppModel",
    "ApplicationReceipt",
    "Clock",
    "DispatchStatus",
    "EffectClaim",
    "EffectClaimResult",
    "EffectCompletionRequest",
    "EffectExecutor",
    "EventDisposition",
    "EventSubmission",
    "FaultHook",
    "IdFactory",
    "LeaseAcquireResult",
    "LeaseHeartbeatResult",
    "LeaseReleaseResult",
    "LeaseStatus",
    "NextActionCategory",
    "PrReviewEngineError",
    "PrReviewEngineErrorKind",
    "PrReviewStatus",
    "SafeAction",
    "TimerFireReceipt",
    "TimerStatus",
    "WorkerStepResult",
]
