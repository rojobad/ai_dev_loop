"""Typed events and effect outcomes for the PR review v2 pure domain."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, PositiveInt, TypeAdapter, model_validator

from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationEvidence,
    ArtifactRef,
    DomainModel,
    EffectCompletionToken,
    ErrorSummary,
    FailureReasonKind,
    FrozenThreadSet,
    GitSha40,
    LocalFixOutcomeKind,
    NonEmptyId,
    NonEmptyStr,
    PauseReasonKind,
    PullRequestBinding,
    ReconciliationResolutionKind,
    SafeAction,
    Sha256Hex,
    ThreadId,
    TransientErrorKind,
    TriggerEvidence,
    UserContinuationEvidence,
    UtcInstant,
    VerifiedNoFindingsEvidence,
)
from ai_dev_loop.pr_review_v2.domain.effects import MutatingEffect


class PublicationTextPreparedOutcome(DomainModel):
    kind: Literal["publication_text_prepared"] = "publication_text_prepared"
    publication_text_ref: ArtifactRef
    commit_message_ref: ArtifactRef


class CommitRecordedOutcome(DomainModel):
    kind: Literal["commit_recorded"] = "commit_recorded"
    commit_sha: GitSha40
    new_head_sha: GitSha40
    # Required-but-nullable: omitted serialized fields fail closed; explicit null is
    # the authoritative absent-ref baseline captured under the repository lock.
    expected_remote_sha_before_push: GitSha40 | None


class PushConfirmedOutcome(DomainModel):
    kind: Literal["push_confirmed"] = "push_confirmed"
    commit_sha: GitSha40
    remote_ref: NonEmptyStr


class PrBoundOutcome(DomainModel):
    kind: Literal["pr_bound"] = "pr_bound"
    binding: PullRequestBinding


class ReviewTriggerConfirmedOutcome(DomainModel):
    kind: Literal["review_trigger_confirmed"] = "review_trigger_confirmed"
    evidence: TriggerEvidence


class BotStillWaitingOutcome(DomainModel):
    kind: Literal["bot_still_waiting"] = "bot_still_waiting"
    next_not_before: UtcInstant
    poll_sequence: PositiveInt


class VerifiedNoFindingsOutcome(DomainModel):
    kind: Literal["verified_no_findings"] = "verified_no_findings"
    evidence: VerifiedNoFindingsEvidence


class EligibleThreadsObservedOutcome(DomainModel):
    kind: Literal["eligible_threads_observed"] = "eligible_threads_observed"
    frozen: FrozenThreadSet


class AdjudicationRecordedOutcome(DomainModel):
    kind: Literal["adjudication_recorded"] = "adjudication_recorded"
    evidence: AdjudicationEvidence


class ThreadReplyConfirmedOutcome(DomainModel):
    kind: Literal["thread_reply_confirmed"] = "thread_reply_confirmed"
    thread_id: ThreadId
    reply_ref: ArtifactRef


class LocalFixFinishedOutcome(DomainModel):
    kind: Literal["local_fix_finished"] = "local_fix_finished"
    outcome: LocalFixOutcomeKind
    accepted_patch_ref: ArtifactRef | None = None
    new_head_sha: GitSha40 | None = None
    result_ref: ArtifactRef
    pause_reason: PauseReasonKind | None = None
    safe_action: SafeAction | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> LocalFixFinishedOutcome:
        accepted = self.outcome in {
            LocalFixOutcomeKind.ACCEPTED,
            LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
        }
        if accepted:
            if self.accepted_patch_ref is None or self.new_head_sha is None:
                raise ValueError("accepted local fix requires patch ref and new head SHA")
            if self.pause_reason is not None or self.safe_action is not None:
                raise ValueError("accepted local fix must not carry pause fields")
        elif self.outcome is LocalFixOutcomeKind.ABORTED:
            if self.accepted_patch_ref is not None or self.new_head_sha is not None:
                raise ValueError("aborted local fix must not carry acceptance fields")
        else:
            if self.pause_reason is None or self.safe_action is None:
                raise ValueError("paused/failed/limit local fix requires pause fields")
            if self.accepted_patch_ref is not None or self.new_head_sha is not None:
                raise ValueError("non-accepted local fix must not carry acceptance fields")
        return self


class PrTextUpdatedOutcome(DomainModel):
    kind: Literal["pr_text_updated"] = "pr_text_updated"
    binding: PullRequestBinding
    publication_text_ref: ArtifactRef


class ThreadResolutionConfirmedOutcome(DomainModel):
    kind: Literal["thread_resolution_confirmed"] = "thread_resolution_confirmed"
    thread_id: ThreadId


ConfirmedWriteOutcome = Annotated[
    PublicationTextPreparedOutcome
    | CommitRecordedOutcome
    | PushConfirmedOutcome
    | PrBoundOutcome
    | ReviewTriggerConfirmedOutcome
    | ThreadReplyConfirmedOutcome
    | PrTextUpdatedOutcome
    | ThreadResolutionConfirmedOutcome,
    Field(discriminator="kind"),
]


class ReconciliationResolvedOutcome(DomainModel):
    kind: Literal["reconciliation_resolved"] = "reconciliation_resolved"
    resolution: ReconciliationResolutionKind
    original_effect_id: NonEmptyId
    confirmed_outcome: ConfirmedWriteOutcome | None = None
    next_attempt_at: UtcInstant | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> ReconciliationResolvedOutcome:
        if self.resolution is ReconciliationResolutionKind.APPLIED:
            if self.confirmed_outcome is None:
                raise ValueError("applied reconciliation requires confirmed_outcome")
            if self.next_attempt_at is not None:
                raise ValueError("applied reconciliation forbids next_attempt_at")
        elif self.resolution is ReconciliationResolutionKind.PROVEN_NOT_APPLIED:
            if self.confirmed_outcome is not None:
                raise ValueError("proven_not_applied forbids confirmed_outcome")
            if self.next_attempt_at is None:
                raise ValueError("proven_not_applied requires next_attempt_at")
        else:
            if self.confirmed_outcome is not None:
                raise ValueError("unresolved reconciliation forbids confirmed_outcome")
            if self.next_attempt_at is not None:
                raise ValueError("unresolved reconciliation forbids next_attempt_at")
        return self


EffectSuccessOutcome = Annotated[
    PublicationTextPreparedOutcome
    | CommitRecordedOutcome
    | PushConfirmedOutcome
    | PrBoundOutcome
    | ReviewTriggerConfirmedOutcome
    | BotStillWaitingOutcome
    | VerifiedNoFindingsOutcome
    | EligibleThreadsObservedOutcome
    | AdjudicationRecordedOutcome
    | ThreadReplyConfirmedOutcome
    | LocalFixFinishedOutcome
    | PrTextUpdatedOutcome
    | ThreadResolutionConfirmedOutcome
    | ReconciliationResolvedOutcome,
    Field(discriminator="kind"),
]


class StartRequested(DomainModel):
    kind: Literal["start_requested"] = "start_requested"
    occurred_at: UtcInstant


class EffectSucceeded(DomainModel):
    kind: Literal["effect_succeeded"] = "effect_succeeded"
    occurred_at: UtcInstant
    token: EffectCompletionToken
    outcome: EffectSuccessOutcome


class EffectRetryableFailure(DomainModel):
    kind: Literal["effect_retryable_failure"] = "effect_retryable_failure"
    occurred_at: UtcInstant
    token: EffectCompletionToken
    error: ErrorSummary
    failed_attempt: PositiveInt
    next_attempt_at: UtcInstant

    @model_validator(mode="after")
    def validate_error_kind(self) -> EffectRetryableFailure:
        if not isinstance(self.error.kind, TransientErrorKind):
            raise ValueError("retryable failure requires TransientErrorKind")
        if self.next_attempt_at <= self.occurred_at:
            raise ValueError("next_attempt_at must be strictly after occurred_at")
        return self


class EffectBlocked(DomainModel):
    kind: Literal["effect_blocked"] = "effect_blocked"
    occurred_at: UtcInstant
    token: EffectCompletionToken
    reason: PauseReasonKind
    safe_action: SafeAction
    safe_summary: NonEmptyStr


class WriteOutcomeUncertain(DomainModel):
    kind: Literal["write_outcome_uncertain"] = "write_outcome_uncertain"
    occurred_at: UtcInstant
    token: EffectCompletionToken
    error: ErrorSummary
    reconciliation_identity: NonEmptyId
    original_write: MutatingEffect


class RetryDue(DomainModel):
    kind: Literal["retry_due"] = "retry_due"
    occurred_at: UtcInstant
    pending_effect_id: NonEmptyId
    current_time: UtcInstant


class ResumeRequested(DomainModel):
    kind: Literal["resume_requested"] = "resume_requested"
    occurred_at: UtcInstant


class UserContinuationRequested(DomainModel):
    kind: Literal["user_continuation_requested"] = "user_continuation_requested"
    occurred_at: UtcInstant
    evidence: UserContinuationEvidence


class RecoverMixedAdjudicationRequested(DomainModel):
    kind: Literal["recover_mixed_adjudication_requested"] = "recover_mixed_adjudication_requested"
    occurred_at: UtcInstant
    evidence_ref: ArtifactRef
    legacy_result_ref_sha256: Sha256Hex
    superseded_reply_effect_id: NonEmptyId


class AbortRequested(DomainModel):
    kind: Literal["abort_requested"] = "abort_requested"
    occurred_at: UtcInstant
    reason: NonEmptyStr = "user_requested_abort"


class FatalFailureDetected(DomainModel):
    kind: Literal["fatal_failure_detected"] = "fatal_failure_detected"
    occurred_at: UtcInstant
    reason: FailureReasonKind
    safe_summary: NonEmptyStr


PrReviewEvent = Annotated[
    StartRequested
    | EffectSucceeded
    | EffectRetryableFailure
    | EffectBlocked
    | WriteOutcomeUncertain
    | RetryDue
    | ResumeRequested
    | UserContinuationRequested
    | RecoverMixedAdjudicationRequested
    | AbortRequested
    | FatalFailureDetected,
    Field(discriminator="kind"),
]

PR_REVIEW_EVENT_ADAPTER: TypeAdapter[PrReviewEvent] = TypeAdapter(PrReviewEvent)


def parse_pr_review_event(payload: object) -> PrReviewEvent:
    return PR_REVIEW_EVENT_ADAPTER.validate_python(payload)
