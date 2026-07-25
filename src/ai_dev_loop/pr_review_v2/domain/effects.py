"""Declarative effects for the PR review v2 pure domain."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, PositiveInt, TypeAdapter, field_validator, model_validator

from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    DomainModel,
    EffectClassification,
    GitSha40,
    NonEmptyId,
    NonEmptyStr,
    PublicationStep,
    PullRequestBinding,
    ReconciliationStrategyKind,
    RepositoryIdentity,
    ThreadId,
    UtcInstant,
    build_effect_identity,
    validate_argv_safe_branch_name,
    validate_argv_safe_remote_ref,
)


class EffectCommon(DomainModel):
    effect_id: NonEmptyId
    idempotency_key: NonEmptyId
    run_id: NonEmptyId
    cycle_number: PositiveInt
    attempt: PositiveInt
    max_attempts: PositiveInt
    repository: RepositoryIdentity
    bound_head_sha: GitSha40

    @model_validator(mode="after")
    def validate_attempts(self) -> EffectCommon:
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.attempt > self.max_attempts:
            raise ValueError("attempt cannot exceed max_attempts")
        return self


class BindingAlignedEffect(EffectCommon):
    """Effects that carry a PR binding must align repository and head SHA."""

    binding: PullRequestBinding

    @model_validator(mode="after")
    def validate_binding_alignment(self) -> BindingAlignedEffect:
        if self.repository != self.binding.repository:
            raise ValueError("repository must equal binding.repository")
        if self.bound_head_sha != self.binding.head_sha:
            raise ValueError("bound_head_sha must equal binding.head_sha")
        return self


class GeneratePublicationTextEffect(EffectCommon):
    kind: Literal["generate_publication_text"] = "generate_publication_text"
    evidence_ref: ArtifactRef
    patch_ref: ArtifactRef


class CommitPatchEffect(EffectCommon):
    kind: Literal["commit_patch"] = "commit_patch"
    patch_ref: ArtifactRef
    expected_head_sha: GitSha40
    expected_branch: NonEmptyStr
    commit_message_ref: ArtifactRef

    @field_validator("expected_branch")
    @classmethod
    def validate_expected_branch(cls, value: str) -> str:
        return validate_argv_safe_branch_name(value)


class PushCommitEffect(EffectCommon):
    kind: Literal["push_commit"] = "push_commit"
    commit_sha: GitSha40
    remote_ref: NonEmptyStr
    # Required-but-nullable: omitted serialized fields fail closed; explicit null is
    # the authoritative absent-ref baseline captured before the preceding commit.
    expected_remote_sha_before_push: GitSha40 | None
    force: Literal[False] = False

    @field_validator("remote_ref")
    @classmethod
    def validate_remote_ref_field(cls, value: str) -> str:
        return validate_argv_safe_remote_ref(value)


class CreateOrUpdatePrEffect(EffectCommon):
    kind: Literal["create_or_update_pr"] = "create_or_update_pr"
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    publication_text_ref: ArtifactRef


class RequestBotReviewEffect(BindingAlignedEffect):
    kind: Literal["request_bot_review"] = "request_bot_review"
    marker: NonEmptyId


class ObserveBotReviewEffect(BindingAlignedEffect):
    kind: Literal["observe_bot_review"] = "observe_bot_review"
    poll_sequence: PositiveInt
    not_before: UtcInstant | None = None
    trigger_marker: NonEmptyId | None = None


class AdjudicateThreadsEffect(BindingAlignedEffect):
    kind: Literal["adjudicate_threads"] = "adjudicate_threads"
    frozen_thread_ids: tuple[ThreadId, ...]
    snapshot_ref: ArtifactRef
    execution_context_ref: ArtifactRef

    @model_validator(mode="after")
    def validate_frozen_ids(self) -> AdjudicateThreadsEffect:
        if not self.frozen_thread_ids:
            raise ValueError("frozen_thread_ids must be non-empty")
        if len(self.frozen_thread_ids) != len(set(self.frozen_thread_ids)):
            raise ValueError("frozen_thread_ids must be unique")
        return self


class PostThreadReplyEffect(BindingAlignedEffect):
    kind: Literal["post_thread_reply"] = "post_thread_reply"
    thread_id: ThreadId
    reply_ref: ArtifactRef


class RunLocalFixEffect(BindingAlignedEffect):
    kind: Literal["run_local_fix"] = "run_local_fix"
    actionable_thread_ids: tuple[ThreadId, ...]
    fix_prompt_ref: ArtifactRef
    execution_context_ref: ArtifactRef

    @model_validator(mode="after")
    def validate_threads(self) -> RunLocalFixEffect:
        if not self.actionable_thread_ids:
            raise ValueError("actionable_thread_ids must be non-empty")
        if len(self.actionable_thread_ids) != len(set(self.actionable_thread_ids)):
            raise ValueError("actionable_thread_ids must be unique")
        return self


class UpdatePrTextEffect(BindingAlignedEffect):
    kind: Literal["update_pr_text"] = "update_pr_text"
    publication_text_ref: ArtifactRef


class ResolveThreadEffect(BindingAlignedEffect):
    kind: Literal["resolve_thread"] = "resolve_thread"
    thread_id: ThreadId


MutatingEffect = Annotated[
    CommitPatchEffect
    | PushCommitEffect
    | CreateOrUpdatePrEffect
    | RequestBotReviewEffect
    | PostThreadReplyEffect
    | UpdatePrTextEffect
    | ResolveThreadEffect,
    Field(discriminator="kind"),
]

_STRATEGY_FOR_MUTATING_KIND: dict[str, ReconciliationStrategyKind] = {
    "commit_patch": ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
    "push_commit": ReconciliationStrategyKind.FIND_REMOTE_REF,
    "create_or_update_pr": ReconciliationStrategyKind.FIND_PR_BY_HEAD_BASE,
    "request_bot_review": ReconciliationStrategyKind.FIND_REVIEW_MARKER,
    "post_thread_reply": ReconciliationStrategyKind.FIND_THREAD_REPLY,
    "update_pr_text": ReconciliationStrategyKind.FIND_PR_TEXT,
    "resolve_thread": ReconciliationStrategyKind.FIND_THREAD_RESOLVED,
}


class ReconcileWriteEffect(EffectCommon):
    kind: Literal["reconcile_write"] = "reconcile_write"
    original_write: MutatingEffect
    strategy: ReconciliationStrategyKind
    reconciliation_identity: NonEmptyId

    @model_validator(mode="after")
    def validate_original(self) -> ReconcileWriteEffect:
        original = self.original_write
        if original.kind == "reconcile_write":  # type: ignore[comparison-overlap]
            raise ValueError("reconcile_write cannot embed another reconcile_write")
        if original.effect_id == self.effect_id:
            raise ValueError("reconcile_write must not reuse the original write effect_id")
        if self.run_id != original.run_id:
            raise ValueError("reconcile_write run_id must match original_write.run_id")
        if self.cycle_number != original.cycle_number:
            raise ValueError("reconcile_write cycle_number must match original_write")
        if self.repository != original.repository:
            raise ValueError("reconcile_write repository must match original_write")
        if self.bound_head_sha != original.bound_head_sha:
            raise ValueError("reconcile_write bound_head_sha must match original_write")
        expected_strategy = _STRATEGY_FOR_MUTATING_KIND[original.kind]
        if self.strategy != expected_strategy:
            raise ValueError("reconcile_write strategy must match the original write kind")
        return self


PrReviewEffect = Annotated[
    GeneratePublicationTextEffect
    | CommitPatchEffect
    | PushCommitEffect
    | CreateOrUpdatePrEffect
    | RequestBotReviewEffect
    | ObserveBotReviewEffect
    | AdjudicateThreadsEffect
    | PostThreadReplyEffect
    | RunLocalFixEffect
    | UpdatePrTextEffect
    | ResolveThreadEffect
    | ReconcileWriteEffect,
    Field(discriminator="kind"),
]

PR_REVIEW_EFFECT_ADAPTER: TypeAdapter[PrReviewEffect] = TypeAdapter(PrReviewEffect)

MUTATING_KINDS = frozenset(
    {
        "commit_patch",
        "push_commit",
        "create_or_update_pr",
        "request_bot_review",
        "post_thread_reply",
        "update_pr_text",
        "resolve_thread",
    }
)
READ_ONLY_KINDS = frozenset({"observe_bot_review"})
LOCAL_KINDS = frozenset({"generate_publication_text", "adjudicate_threads", "run_local_fix"})
RECONCILING_KINDS = frozenset({"reconcile_write"})


def all_pr_review_effect_kinds() -> frozenset[str]:
    """Return every persisted PR review effect kind from the domain registry."""

    return READ_ONLY_KINDS | LOCAL_KINDS | MUTATING_KINDS | RECONCILING_KINDS


def parse_pr_review_effect(payload: object) -> PrReviewEffect:
    return PR_REVIEW_EFFECT_ADAPTER.validate_python(payload)


def classify_effect(effect: PrReviewEffect) -> EffectClassification:
    kind = effect.kind
    if kind in RECONCILING_KINDS:
        return EffectClassification.RECONCILING
    if kind in MUTATING_KINDS:
        return EffectClassification.MUTATING
    if kind in LOCAL_KINDS:
        return EffectClassification.LOCAL
    if kind in READ_ONLY_KINDS:
        return EffectClassification.READ_ONLY
    raise ValueError(f"unknown effect kind: {kind}")


def is_mutating_effect(effect: PrReviewEffect) -> bool:
    return classify_effect(effect) is EffectClassification.MUTATING


def is_read_only_effect(effect: PrReviewEffect) -> bool:
    return classify_effect(effect) is EffectClassification.READ_ONLY


def is_local_effect(effect: PrReviewEffect) -> bool:
    return classify_effect(effect) is EffectClassification.LOCAL


def is_reconciling_effect(effect: PrReviewEffect) -> bool:
    return classify_effect(effect) is EffectClassification.RECONCILING


def with_attempt(effect: PrReviewEffect, attempt: int) -> PrReviewEffect:
    """Return a copy with a validated attempt, preserving identity fields."""
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    if attempt > effect.max_attempts:
        raise ValueError("attempt cannot exceed max_attempts")
    payload = effect.model_dump(mode="python")
    payload["attempt"] = attempt
    return PR_REVIEW_EFFECT_ADAPTER.validate_python(payload)


def stable_effect_ids(
    *,
    run_id: str,
    cycle_number: int,
    operation: str,
    target: str | None = None,
) -> tuple[str, str]:
    identity = build_effect_identity(
        run_id=run_id,
        cycle_number=cycle_number,
        operation=operation,
        target=target,
    )
    return identity, identity


def commit_patch_effect_target(*, expected_head_sha: str, patch_sha256: str) -> str:
    """Content-bound target so initial vs fix commits never share an effect identity."""

    if not expected_head_sha or not patch_sha256:
        raise ValueError("commit_patch effect target requires parent HEAD and patch digest")
    return f"{expected_head_sha}:{patch_sha256}"


def strategy_for_mutating_effect(effect: MutatingEffect) -> ReconciliationStrategyKind:
    return _STRATEGY_FOR_MUTATING_KIND[effect.kind]


def publication_step_for_effect_kind(kind: str) -> PublicationStep | None:
    mapping = {
        "generate_publication_text": PublicationStep.GENERATE_PUBLICATION_TEXT,
        "commit_patch": PublicationStep.COMMIT_PATCH,
        "push_commit": PublicationStep.PUSH_COMMIT,
        "create_or_update_pr": PublicationStep.CREATE_OR_UPDATE_PR,
        "update_pr_text": PublicationStep.UPDATE_PR_TEXT,
        "resolve_thread": PublicationStep.RESOLVE_THREAD,
    }
    return mapping.get(kind)
