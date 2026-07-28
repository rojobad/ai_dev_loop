"""Authoritative discriminated state variants for PR review v2."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, PositiveInt, TypeAdapter, model_validator

from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationEvidence,
    ArtifactRef,
    DomainModel,
    ErrorSummary,
    ExistingPrOrigin,
    FailureReasonKind,
    FrozenThreadSet,
    GitSha40,
    NonEmptyId,
    NonEmptyStr,
    PauseReasonKind,
    PrReviewOrigin,
    PublicationStep,
    PullRequestBinding,
    ReplyIntent,
    SafeAction,
    SourceRunOrigin,
    ThreadId,
    TriggerEvidence,
    UtcInstant,
    VerifiedNoFindingsEvidence,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    AdjudicateThreadsEffect,
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    GeneratePublicationTextEffect,
    MutatingEffect,
    ObserveBotReviewEffect,
    PostThreadReplyEffect,
    PrReviewEffect,
    PushCommitEffect,
    ReconcileWriteEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    RunLocalFixEffect,
    UpdatePrTextEffect,
)

PublishingInitialActiveEffect = Annotated[
    GeneratePublicationTextEffect | CommitPatchEffect | PushCommitEffect | CreateOrUpdatePrEffect,
    Field(discriminator="kind"),
]

WaitingForBotActiveEffect = Annotated[
    RequestBotReviewEffect | ObserveBotReviewEffect,
    Field(discriminator="kind"),
]

PublishingFixActiveEffect = Annotated[
    GeneratePublicationTextEffect
    | CommitPatchEffect
    | PushCommitEffect
    | UpdatePrTextEffect
    | ResolveThreadEffect,
    Field(discriminator="kind"),
]


class PreparedState(DomainModel):
    kind: Literal["prepared"] = "prepared"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: Literal[1] = 1
    entered_at: UtcInstant


class PublishingInitialState(DomainModel):
    kind: Literal["publishing_initial"] = "publishing_initial"
    run_id: NonEmptyId
    origin: SourceRunOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    entered_at: UtcInstant
    step: PublicationStep
    active_effect: PublishingInitialActiveEffect
    publication_text_ref: ArtifactRef | None = None
    commit_message_ref: ArtifactRef | None = None
    commit_sha: GitSha40 | None = None

    @model_validator(mode="after")
    def validate_step_effect(self) -> PublishingInitialState:
        expected = {
            PublicationStep.GENERATE_PUBLICATION_TEXT: "generate_publication_text",
            PublicationStep.COMMIT_PATCH: "commit_patch",
            PublicationStep.PUSH_COMMIT: "push_commit",
            PublicationStep.CREATE_OR_UPDATE_PR: "create_or_update_pr",
        }
        if self.step not in expected:
            raise ValueError("publishing_initial step must be an initial publication step")
        if self.active_effect.kind != expected[self.step]:
            raise ValueError("publishing_initial active_effect must match step")
        if self.active_effect.run_id != self.run_id:
            raise ValueError("active_effect.run_id must match state.run_id")
        if self.active_effect.cycle_number != self.cycle_number:
            raise ValueError("active_effect.cycle_number must match state.cycle_number")
        if self.active_effect.repository != self.origin.repository:
            raise ValueError("active_effect.repository must match origin.repository")

        if self.step is PublicationStep.GENERATE_PUBLICATION_TEXT:
            if (
                self.publication_text_ref is not None
                or self.commit_message_ref is not None
                or self.commit_sha is not None
            ):
                raise ValueError("generate_publication_text step must not carry later progress")
            if self.active_effect.kind == "generate_publication_text":
                if self.active_effect.patch_ref != self.origin.accepted_patch:
                    raise ValueError("generate effect patch_ref must match origin.accepted_patch")
                if self.active_effect.bound_head_sha != self.origin.expected_head_sha:
                    raise ValueError("generate effect must bind origin.expected_head_sha")
        elif self.step is PublicationStep.COMMIT_PATCH:
            if self.publication_text_ref is None or self.commit_message_ref is None:
                raise ValueError("commit_patch step requires publication and commit message refs")
            if self.commit_sha is not None:
                raise ValueError("commit_patch step must not carry commit_sha yet")
            if self.active_effect.kind == "commit_patch":
                if self.active_effect.commit_message_ref != self.commit_message_ref:
                    raise ValueError("commit effect message ref must match state")
                if self.active_effect.patch_ref != self.origin.accepted_patch:
                    raise ValueError("commit effect patch_ref must match origin.accepted_patch")
                if self.active_effect.expected_head_sha != self.origin.expected_head_sha:
                    raise ValueError("commit effect expected_head_sha must match origin")
                if self.active_effect.bound_head_sha != self.origin.expected_head_sha:
                    raise ValueError("commit effect bound_head_sha must match origin")
        elif self.step is PublicationStep.PUSH_COMMIT:
            if (
                self.publication_text_ref is None
                or self.commit_message_ref is None
                or self.commit_sha is None
            ):
                raise ValueError("push_commit step requires publication refs and commit_sha")
            if self.active_effect.kind == "push_commit":
                if self.active_effect.commit_sha != self.commit_sha:
                    raise ValueError("push effect commit_sha must match state.commit_sha")
                if self.active_effect.remote_ref != self.origin.head_branch:
                    raise ValueError("push effect remote_ref must match origin.head_branch")
                if self.active_effect.bound_head_sha != self.commit_sha:
                    raise ValueError("push effect bound_head_sha must match commit_sha")
        elif self.step is PublicationStep.CREATE_OR_UPDATE_PR:
            if (
                self.publication_text_ref is None
                or self.commit_message_ref is None
                or self.commit_sha is None
            ):
                raise ValueError(
                    "create_or_update_pr step requires publication refs and commit_sha"
                )
            if self.active_effect.kind == "create_or_update_pr":
                if self.active_effect.publication_text_ref != self.publication_text_ref:
                    raise ValueError("PR effect publication_text_ref must match state")
                if self.active_effect.head_branch != self.origin.head_branch:
                    raise ValueError("PR effect head_branch must match origin")
                if self.active_effect.base_branch != self.origin.base_branch:
                    raise ValueError("PR effect base_branch must match origin")
                if self.active_effect.bound_head_sha != self.commit_sha:
                    raise ValueError("PR effect bound_head_sha must match commit_sha")
        return self


class WaitingForBotState(DomainModel):
    kind: Literal["waiting_for_bot"] = "waiting_for_bot"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    entered_at: UtcInstant
    poll_sequence: PositiveInt
    active_effect: WaitingForBotActiveEffect
    trigger_evidence: TriggerEvidence | None = None

    @model_validator(mode="after")
    def validate_trigger(self) -> WaitingForBotState:
        if self.active_effect.run_id != self.run_id:
            raise ValueError("active_effect.run_id must match state.run_id")
        if self.active_effect.cycle_number != self.cycle_number:
            raise ValueError("active_effect.cycle_number must match state.cycle_number")
        if self.active_effect.kind == "observe_bot_review":
            if self.trigger_evidence is None:
                raise ValueError("observe_bot_review requires confirmed trigger evidence")
            if self.active_effect.poll_sequence != self.poll_sequence:
                raise ValueError("observe effect poll_sequence must match state.poll_sequence")
            if self.active_effect.binding != self.binding:
                raise ValueError("observe effect binding must match state.binding")
            if self.active_effect.trigger_marker != self.trigger_evidence.marker:
                raise ValueError("observe effect trigger_marker must match trigger evidence")
            if self.active_effect.bound_head_sha != self.binding.head_sha:
                raise ValueError("observe effect bound_head_sha must match binding.head_sha")
            if self.trigger_evidence.head_sha != self.binding.head_sha:
                raise ValueError("trigger evidence head_sha must match binding.head_sha")
        elif self.active_effect.kind == "request_bot_review":
            if self.trigger_evidence is not None:
                raise ValueError("request_bot_review must not carry trigger evidence yet")
            if self.active_effect.binding != self.binding:
                raise ValueError("request effect binding must match state.binding")
            if self.active_effect.bound_head_sha != self.binding.head_sha:
                raise ValueError("request effect bound_head_sha must match binding.head_sha")
            if self.poll_sequence != 1:
                raise ValueError("request_bot_review state must start at poll_sequence 1")
        return self


class AdjudicatingState(DomainModel):
    kind: Literal["adjudicating"] = "adjudicating"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    entered_at: UtcInstant
    frozen: FrozenThreadSet
    trigger_evidence: TriggerEvidence
    active_effect: AdjudicateThreadsEffect

    @model_validator(mode="after")
    def validate_adjudication_binding(self) -> AdjudicatingState:
        effect = self.active_effect
        if effect.run_id != self.run_id or effect.cycle_number != self.cycle_number:
            raise ValueError("adjudicate effect identity must match state")
        if effect.binding != self.binding:
            raise ValueError("adjudicate effect binding must match state.binding")
        if effect.bound_head_sha != self.binding.head_sha:
            raise ValueError("adjudicate effect bound_head_sha must match binding")
        if effect.frozen_thread_ids != self.frozen.thread_ids:
            raise ValueError("adjudicate effect frozen_thread_ids must match frozen set")
        if effect.snapshot_ref != self.frozen.snapshot_ref:
            raise ValueError("adjudicate effect snapshot_ref must match frozen snapshot")
        if self.frozen.head_sha != self.binding.head_sha:
            raise ValueError("frozen head_sha must match binding.head_sha")
        if self.frozen.cycle_number != self.cycle_number:
            raise ValueError("frozen cycle_number must match state.cycle_number")
        if self.frozen.trigger_marker != self.trigger_evidence.marker:
            raise ValueError("frozen trigger_marker must match trigger evidence")
        if self.trigger_evidence.head_sha != self.binding.head_sha:
            raise ValueError("trigger evidence head_sha must match binding")
        return self


class WaitingForUserState(DomainModel):
    kind: Literal["waiting_for_user"] = "waiting_for_user"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    entered_at: UtcInstant
    adjudication: AdjudicationEvidence
    remaining_replies: tuple[ReplyIntent, ...]
    active_effect: PostThreadReplyEffect | None
    safe_action: SafeAction
    trigger_evidence: TriggerEvidence

    @model_validator(mode="after")
    def validate_reply_queue(self) -> WaitingForUserState:
        if self.adjudication.frozen.head_sha != self.binding.head_sha:
            raise ValueError("adjudication frozen head must match binding")
        if self.adjudication.frozen.cycle_number != self.cycle_number:
            raise ValueError("adjudication frozen cycle must match state")
        if self.trigger_evidence.head_sha != self.binding.head_sha:
            raise ValueError("trigger evidence head_sha must match binding")
        if self.active_effect is None:
            if self.remaining_replies:
                raise ValueError("remaining replies require an active reply effect")
        else:
            if not self.remaining_replies:
                raise ValueError("active reply effect requires a non-empty remaining queue")
            head = self.remaining_replies[0]
            if (
                self.active_effect.thread_id != head.thread_id
                or self.active_effect.reply_ref != head.reply_ref
            ):
                raise ValueError("active reply effect must match the head of remaining_replies")
            if self.active_effect.binding != self.binding:
                raise ValueError("reply effect binding must match state.binding")
            if self.active_effect.bound_head_sha != self.binding.head_sha:
                raise ValueError("reply effect bound_head_sha must match binding")
            if self.active_effect.run_id != self.run_id:
                raise ValueError("reply effect run_id must match state")
            if self.active_effect.cycle_number != self.cycle_number:
                raise ValueError("reply effect cycle_number must match state")
        return self


class RunningLocalFixState(DomainModel):
    kind: Literal["running_local_fix"] = "running_local_fix"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    entered_at: UtcInstant
    actionable_thread_ids: tuple[ThreadId, ...]
    fix_prompt_ref: ArtifactRef
    active_effect: RunLocalFixEffect
    trigger_evidence: TriggerEvidence
    adjudication: AdjudicationEvidence

    @model_validator(mode="after")
    def validate_local_fix(self) -> RunningLocalFixState:
        effect = self.active_effect
        if effect.run_id != self.run_id or effect.cycle_number != self.cycle_number:
            raise ValueError("local fix effect identity must match state")
        if effect.binding != self.binding:
            raise ValueError("local fix effect binding must match state.binding")
        if effect.bound_head_sha != self.binding.head_sha:
            raise ValueError("local fix effect bound_head_sha must match binding")
        if effect.fix_prompt_ref != self.fix_prompt_ref:
            raise ValueError("local fix effect fix_prompt_ref must match state")
        if effect.actionable_thread_ids != self.actionable_thread_ids:
            raise ValueError("local fix effect threads must match state")
        if self.adjudication.fix_prompt_ref != self.fix_prompt_ref:
            raise ValueError("adjudication fix_prompt_ref must match state")
        if self.trigger_evidence.head_sha != self.binding.head_sha:
            raise ValueError("trigger evidence head_sha must match binding")
        return self


class PublishingFixState(DomainModel):
    kind: Literal["publishing_fix"] = "publishing_fix"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    entered_at: UtcInstant
    old_head_sha: GitSha40
    new_head_sha: GitSha40 | None
    accepted_patch_ref: ArtifactRef
    corrected_thread_ids: tuple[ThreadId, ...]
    remaining_resolutions: tuple[ThreadId, ...]
    step: PublicationStep
    active_effect: PublishingFixActiveEffect | None
    publication_text_ref: ArtifactRef | None = None
    commit_message_ref: ArtifactRef | None = None
    commit_sha: GitSha40 | None = None
    trigger_evidence: TriggerEvidence

    @model_validator(mode="after")
    def validate_progress(self) -> PublishingFixState:
        if self.trigger_evidence.head_sha != self.old_head_sha:
            raise ValueError("trigger evidence must bind the pre-fix head SHA")
        if self.step is PublicationStep.COMPLETE:
            if self.active_effect is not None:
                raise ValueError("complete publishing_fix must not own an active effect")
            if self.new_head_sha is None:
                raise ValueError("complete publishing_fix requires new_head_sha")
            if self.remaining_resolutions:
                raise ValueError("complete publishing_fix must have empty remaining_resolutions")
            return self
        if self.active_effect is None:
            raise ValueError("incomplete publishing_fix requires an active effect")
        expected = {
            PublicationStep.GENERATE_PUBLICATION_TEXT: "generate_publication_text",
            PublicationStep.COMMIT_PATCH: "commit_patch",
            PublicationStep.PUSH_COMMIT: "push_commit",
            PublicationStep.UPDATE_PR_TEXT: "update_pr_text",
            PublicationStep.RESOLVE_THREAD: "resolve_thread",
        }
        if self.step not in expected:
            raise ValueError("invalid publishing_fix step")
        if self.active_effect.kind != expected[self.step]:
            raise ValueError("publishing_fix active_effect must match step")
        if self.active_effect.run_id != self.run_id:
            raise ValueError("active_effect.run_id must match state.run_id")
        if self.active_effect.cycle_number != self.cycle_number:
            raise ValueError("active_effect.cycle_number must match state.cycle_number")

        if self.step is PublicationStep.GENERATE_PUBLICATION_TEXT:
            if (
                self.publication_text_ref is not None
                or self.commit_message_ref is not None
                or self.commit_sha is not None
            ):
                raise ValueError("generate_publication_text step must not carry later progress")
            if self.new_head_sha is None:
                raise ValueError("publishing_fix requires new_head_sha from local acceptance")
            if self.active_effect.kind == "generate_publication_text":
                if self.active_effect.patch_ref != self.accepted_patch_ref:
                    raise ValueError("generate effect patch_ref must match accepted_patch_ref")
                if self.active_effect.bound_head_sha != self.new_head_sha:
                    raise ValueError("generate effect must bind the accepted new head")
        elif self.step is PublicationStep.COMMIT_PATCH:
            if self.publication_text_ref is None or self.commit_message_ref is None:
                raise ValueError("commit_patch step requires publication and commit message refs")
            if self.commit_sha is not None:
                raise ValueError("commit_patch step must not carry commit_sha yet")
            if self.active_effect.kind == "commit_patch":
                if self.active_effect.patch_ref != self.accepted_patch_ref:
                    raise ValueError("commit effect patch_ref must match accepted_patch_ref")
                if self.active_effect.expected_head_sha != self.old_head_sha:
                    raise ValueError("commit effect expected_head_sha must match old_head_sha")
                if self.active_effect.commit_message_ref != self.commit_message_ref:
                    raise ValueError("commit effect message ref must match state")
        elif self.step is PublicationStep.PUSH_COMMIT:
            if (
                self.publication_text_ref is None
                or self.commit_message_ref is None
                or self.commit_sha is None
                or self.new_head_sha is None
            ):
                raise ValueError("push_commit step requires publication refs, commit, and new head")
            if self.active_effect.kind == "push_commit":
                if self.active_effect.commit_sha != self.commit_sha:
                    raise ValueError("push effect commit_sha must match state")
                if self.active_effect.remote_ref != self.binding.head_branch:
                    raise ValueError("push effect remote_ref must match binding.head_branch")
                if self.active_effect.bound_head_sha != self.new_head_sha:
                    raise ValueError("push effect bound_head_sha must match new_head_sha")
        elif self.step is PublicationStep.UPDATE_PR_TEXT:
            if (
                self.publication_text_ref is None
                or self.commit_message_ref is None
                or self.commit_sha is None
                or self.new_head_sha is None
            ):
                raise ValueError("update_pr_text step requires completed push progress")
            if self.binding.head_sha != self.new_head_sha:
                raise ValueError("update_pr_text binding.head_sha must equal new_head_sha")
            if self.active_effect.kind == "update_pr_text":
                if self.active_effect.binding != self.binding:
                    raise ValueError("update_pr_text effect binding must match state")
                if self.active_effect.publication_text_ref != self.publication_text_ref:
                    raise ValueError("update_pr_text effect text ref must match state")
                adopted = self.active_effect.adopted_preimage_ref
                if adopted is not None:
                    if not isinstance(self.origin, ExistingPrOrigin):
                        raise ValueError(
                            "adopted_preimage_ref is only valid for existing_pr origin"
                        )
                    if self.origin.adopted_preimage_ref != adopted:
                        raise ValueError(
                            "adopted_preimage_ref must match origin.adopted_preimage_ref"
                        )
                    if self.cycle_number != 1:
                        raise ValueError(
                            "adopted_preimage_ref is only valid on the first fix cycle"
                        )
        elif self.step is PublicationStep.RESOLVE_THREAD:
            if not self.remaining_resolutions:
                raise ValueError("resolve step requires remaining resolutions")
            if self.new_head_sha is None or self.binding.head_sha != self.new_head_sha:
                raise ValueError("resolve step binding must already use new_head_sha")
            if (
                self.active_effect.kind == "resolve_thread"
                and self.active_effect.thread_id != self.remaining_resolutions[0]
            ):
                raise ValueError("resolve effect must target the next thread")
            if (
                self.active_effect.kind == "resolve_thread"
                and self.active_effect.binding != self.binding
            ):
                raise ValueError("resolve effect binding must match state")
        return self


# Finite suspended union excludes waiting_retry, paused, and terminals.
SuspendableOperationalState = Annotated[
    PublishingInitialState
    | WaitingForBotState
    | AdjudicatingState
    | WaitingForUserState
    | RunningLocalFixState
    | PublishingFixState,
    Field(discriminator="kind"),
]


def _nested_binding(nested: Any) -> PullRequestBinding | None:
    if nested.kind == "publishing_initial":
        return None
    return getattr(nested, "binding", None)


def _require_envelope_matches_nested(
    *,
    run_id: NonEmptyId,
    origin: PrReviewOrigin,
    limits: WorkflowLimits,
    cycle_number: PositiveInt,
    binding: PullRequestBinding | None,
    nested: Any,
    label: str,
) -> None:
    if run_id != nested.run_id:
        raise ValueError(f"{label} run_id must match nested state")
    if origin != nested.origin:
        raise ValueError(f"{label} origin must match nested state")
    if limits != nested.limits:
        raise ValueError(f"{label} limits must match nested state")
    if cycle_number != nested.cycle_number:
        raise ValueError(f"{label} cycle_number must match nested state")
    if binding != _nested_binding(nested):
        raise ValueError(f"{label} binding must match nested state binding")


class ReconcilingWriteState(DomainModel):
    kind: Literal["reconciling_write"] = "reconciling_write"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    entered_at: UtcInstant
    suspended: SuspendableOperationalState
    original_write: MutatingEffect
    active_effect: ReconcileWriteEffect
    last_error: ErrorSummary
    binding: PullRequestBinding | None = None

    @model_validator(mode="after")
    def validate_reconcile(self) -> ReconcilingWriteState:
        _require_envelope_matches_nested(
            run_id=self.run_id,
            origin=self.origin,
            limits=self.limits,
            cycle_number=self.cycle_number,
            binding=self.binding,
            nested=self.suspended,
            label="reconciling_write",
        )
        if self.active_effect.kind != "reconcile_write":
            raise ValueError("reconciling_write requires reconcile_write effect")
        if self.active_effect.original_write != self.original_write:
            raise ValueError(
                "active_effect.original_write must fully equal top-level original_write"
            )
        suspended_active = getattr(self.suspended, "active_effect", None)
        if suspended_active is None:
            raise ValueError("reconciling_write suspended state must own an active effect")
        if suspended_active != self.original_write:
            raise ValueError("suspended active mutating effect must fully equal original_write")
        if (
            self.active_effect.run_id != self.original_write.run_id
            or self.active_effect.cycle_number != self.original_write.cycle_number
            or self.active_effect.repository != self.original_write.repository
            or self.active_effect.bound_head_sha != self.original_write.bound_head_sha
        ):
            raise ValueError("reconcile effect identity fields must match the exact original write")
        return self


RetrySuspendableState = Annotated[
    PublishingInitialState
    | WaitingForBotState
    | AdjudicatingState
    | WaitingForUserState
    | RunningLocalFixState
    | PublishingFixState
    | ReconcilingWriteState,
    Field(discriminator="kind"),
]


class WaitingRetryState(DomainModel):
    kind: Literal["waiting_retry"] = "waiting_retry"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    entered_at: UtcInstant
    suspended: RetrySuspendableState
    retrying_effect_id: NonEmptyId
    attempt: PositiveInt
    max_attempts: PositiveInt
    next_attempt_at: UtcInstant
    last_error: ErrorSummary
    binding: PullRequestBinding | None = None

    @model_validator(mode="after")
    def validate_retry(self) -> WaitingRetryState:
        _require_envelope_matches_nested(
            run_id=self.run_id,
            origin=self.origin,
            limits=self.limits,
            cycle_number=self.cycle_number,
            binding=self.binding,
            nested=self.suspended,
            label="waiting_retry",
        )
        active = _active_effect_of(self.suspended)
        if active is None:
            raise ValueError("waiting_retry suspended state must own an active effect")
        if active.effect_id != self.retrying_effect_id:
            raise ValueError("retrying_effect_id must match suspended active effect")
        if active.attempt != self.attempt:
            raise ValueError("retry attempt must match suspended active effect attempt")
        if active.max_attempts != self.max_attempts:
            raise ValueError("retry max_attempts must match suspended active effect")
        return self


ResumableContinuation = Annotated[
    PublishingInitialState
    | WaitingForBotState
    | AdjudicatingState
    | WaitingForUserState
    | RunningLocalFixState
    | PublishingFixState
    | ReconcilingWriteState,
    Field(discriminator="kind"),
]


class CompletedState(DomainModel):
    kind: Literal["completed"] = "completed"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    binding: PullRequestBinding
    cycle_number: PositiveInt
    evidence: VerifiedNoFindingsEvidence
    completed_at: UtcInstant

    @model_validator(mode="after")
    def validate_completion_evidence(self) -> CompletedState:
        if self.evidence.head_sha != self.binding.head_sha:
            raise ValueError("verified-no-findings evidence head_sha must equal binding.head_sha")
        return self


class PausedState(DomainModel):
    kind: Literal["paused"] = "paused"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    reason: PauseReasonKind
    safe_action: SafeAction
    safe_summary: NonEmptyStr
    paused_at: UtcInstant
    binding: PullRequestBinding | None = None
    resumable: ResumableContinuation | None = None

    @model_validator(mode="after")
    def validate_resumable_envelope(self) -> PausedState:
        if self.resumable is None:
            return self
        _require_envelope_matches_nested(
            run_id=self.run_id,
            origin=self.origin,
            limits=self.limits,
            cycle_number=self.cycle_number,
            binding=self.binding,
            nested=self.resumable,
            label="paused",
        )
        return self


class FailedState(DomainModel):
    kind: Literal["failed"] = "failed"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    reason: FailureReasonKind
    safe_summary: NonEmptyStr
    failed_at: UtcInstant
    binding: PullRequestBinding | None = None


class AbortedState(DomainModel):
    kind: Literal["aborted"] = "aborted"
    run_id: NonEmptyId
    origin: PrReviewOrigin
    limits: WorkflowLimits
    cycle_number: PositiveInt
    reason: NonEmptyStr
    aborted_at: UtcInstant
    binding: PullRequestBinding | None = None


PrReviewState = Annotated[
    PreparedState
    | PublishingInitialState
    | WaitingForBotState
    | AdjudicatingState
    | WaitingForUserState
    | RunningLocalFixState
    | PublishingFixState
    | ReconcilingWriteState
    | WaitingRetryState
    | CompletedState
    | PausedState
    | FailedState
    | AbortedState,
    Field(discriminator="kind"),
]

PR_REVIEW_STATE_ADAPTER: TypeAdapter[PrReviewState] = TypeAdapter(PrReviewState)

TERMINAL_STATE_KINDS = frozenset({"completed", "failed", "aborted"})
STATE_KINDS = frozenset(
    {
        "prepared",
        "publishing_initial",
        "waiting_for_bot",
        "adjudicating",
        "waiting_for_user",
        "running_local_fix",
        "publishing_fix",
        "reconciling_write",
        "waiting_retry",
        "completed",
        "paused",
        "failed",
        "aborted",
    }
)


def parse_pr_review_state(payload: object) -> PrReviewState:
    return PR_REVIEW_STATE_ADAPTER.validate_python(payload)


def _active_effect_of(state: RetrySuspendableState) -> PrReviewEffect | None:
    if isinstance(state, WaitingForUserState):
        return state.active_effect
    if isinstance(state, PublishingFixState):
        return state.active_effect
    return state.active_effect


def active_effect(state: PrReviewState) -> PrReviewEffect | None:
    if state.kind in TERMINAL_STATE_KINDS or state.kind in {"prepared", "paused"}:
        return None
    if state.kind == "waiting_retry":
        return _active_effect_of(state.suspended)
    if state.kind == "waiting_for_user":
        return state.active_effect
    if state.kind == "publishing_fix":
        return state.active_effect
    return getattr(state, "active_effect", None)


def binding_of(state: PrReviewState) -> PullRequestBinding | None:
    if hasattr(state, "binding"):
        return state.binding
    if state.kind == "publishing_initial":
        return None
    if state.kind in {"reconciling_write", "waiting_retry", "paused", "failed", "aborted"}:
        return getattr(state, "binding", None)
    return None
