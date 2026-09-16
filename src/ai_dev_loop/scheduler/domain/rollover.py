"""Authenticated rollover aggregate domain models for Phase 20.6.5."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import TypeAdapter, field_validator, model_validator

from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    GitObjectSha,
    NonEmptyStr,
    Sha256Hex,
)
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    WorkflowLimits,
)

AUTHENTICATED_ROLLOVER_SCHEMA_VERSION = 1
ROLLOVER_DEFINITION_ARTIFACT = "rollover/definition.json"
ROLLOVER_START_INTENT_ARTIFACT = "rollover/start-intent.json"
ROLLOVER_START_PROGRESS_ARTIFACT = "rollover/start-progress.json"
ROLLOVER_ABORT_INTENT_ARTIFACT = "rollover/abort-intent.txt"
ROLLOVER_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT = "rollover/integration-publication-inputs.json"
ROLLOVER_INTEGRATION_INTENT_ARTIFACT = "rollover/integration-intent.json"
ROLLOVER_INTEGRATION_TRUSTED_TREE_ARTIFACT = "rollover/integration-trusted-tree.json"
ROLLOVER_INTEGRATION_EVIDENCE_ARTIFACT = "rollover/integration-evidence.json"
ROLLOVER_INTEGRATION_RESULT_ARTIFACT = "rollover/integration-result.json"
ROLLOVER_WORKTREE_REGISTRATION_ARTIFACT = "rollover/worktree-registration.json"
ROLLOVER_PRE_SEED_ADMISSION_ARTIFACT = "rollover/pre-seed-admission.json"
ROLLOVER_SEED_EVIDENCE_ARTIFACT = "rollover/seed-evidence.json"
ROLLOVER_CLEANUP_EVIDENCE_ARTIFACT = "rollover/cleanup-evidence.json"

PREPARED_ROLLOVER_STATE_KIND = "prepared"
ACTIVE_ROLLOVER_STATE_KIND = "active"
INTEGRATION_PENDING_ROLLOVER_STATE_KIND = "integration_pending"
CLEANUP_PENDING_ROLLOVER_STATE_KIND = "cleanup_pending"
BLOCKED_ROLLOVER_STATE_KIND = "blocked"
ABORT_PENDING_ROLLOVER_STATE_KIND = "abort_pending"
ABORTED_ROLLOVER_STATE_KIND = "aborted"
INTEGRATED_ROLLOVER_STATE_KIND = "integrated"

ROLLOVER_TERMINAL_STATE_KINDS = frozenset(
    {
        BLOCKED_ROLLOVER_STATE_KIND,
        ABORTED_ROLLOVER_STATE_KIND,
        INTEGRATED_ROLLOVER_STATE_KIND,
    }
)

MAX_ROLLOVER_COMMIT_MESSAGE_BYTES = 4096
MAX_ROLLOVER_PRIVATE_REF_LENGTH = 256


class RolloverSequenceBinding(DomainModel):
    sequence_id: NonEmptyStr
    ordinal: int
    total_phases: int
    is_final_phase: bool

    @field_validator("ordinal", "total_phases")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value


class RolloverIntegrationPublicationInputs(DomainModel):
    """Frozen timestamp and Git identity inputs for integration publication."""

    schema_version: Literal[1] = 1
    recorded_at: NonEmptyStr
    git_identity: GitIdentitySnapshot


class RolloverIntegrationIntent(DomainModel):
    """Immutable integration intent persisted before any Git mutation."""

    schema_version: Literal[1] = 1
    rollover_id: NonEmptyStr
    source_run_id: NonEmptyStr
    rollover_run_id: NonEmptyStr
    sequence_id: NonEmptyStr | None = None
    sequence_ordinal: int | None = None
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    parent_head: GitObjectSha
    source_tree_sha256: GitObjectSha
    accepted_tree_sha256: GitObjectSha
    reviewed_patch_sha256: Sha256Hex
    review_result_sha256: Sha256Hex
    commit_message: NonEmptyStr
    target_branch_ref: NonEmptyStr
    private_ref: NonEmptyStr
    target_repository_root: NonEmptyStr
    target_git_common_dir: NonEmptyStr
    target_git_dir: NonEmptyStr
    managed_repository_root: NonEmptyStr
    managed_git_common_dir: NonEmptyStr
    managed_git_dir: NonEmptyStr
    git_identity: GitIdentitySnapshot
    recorded_at: NonEmptyStr


class RolloverCheckpointTrustedTree(DomainModel):
    """Trusted reviewed-tree evidence bound to an integration intent."""

    schema_version: Literal[1] = 1
    intent_sha256: Sha256Hex
    reviewed_tree_sha256: GitObjectSha
    reviewed_patch_sha256: Sha256Hex
    parent_head: GitObjectSha
    recorded_at: NonEmptyStr


class RolloverIntegrationPolicy(DomainModel):
    commit_message: NonEmptyStr
    accepted_outcomes: tuple[Literal["completed", "completed_with_residual_risk"], ...] = (
        "completed",
        "completed_with_residual_risk",
    )


class AuthenticatedRolloverDefinition(DomainModel):
    """Immutable frozen rollover definition written at prepare time."""

    schema_version: Literal[1] = 1
    rollover_id: NonEmptyStr
    definition_sha256: Sha256Hex
    source_run_id: NonEmptyStr
    source_run_id_prefix: NonEmptyStr
    sequence: RolloverSequenceBinding | None = None
    repository: RepositoryBinding
    target_branch_ref: NonEmptyStr
    parent_head: GitObjectSha
    source_staged_patch_path: NonEmptyStr
    source_staged_patch_sha256: Sha256Hex
    source_staged_tree_sha256: GitObjectSha
    source_final_review_result_path: NonEmptyStr
    source_final_review_result_sha256: Sha256Hex
    plan_prompt: PlanPromptBinding
    effective_config: EffectiveConfigBinding
    codex: FreshCodexReviewerBinding
    cursor: CursorBinding
    workflow: WorkflowLimits
    controller: ControllerBinding
    integration: RolloverIntegrationPolicy
    managed_worktree_path_token: NonEmptyStr
    private_ref: NonEmptyStr
    rollover_worktree_key: Sha256Hex
    prepared_at: NonEmptyStr

    @field_validator("private_ref")
    @classmethod
    def private_ref_namespace(cls, value: str) -> str:
        if not value.startswith("refs/ai-dev-loop/rollover/"):
            raise ValueError("private_ref must use the package rollover namespace")
        if len(value) > MAX_ROLLOVER_PRIVATE_REF_LENGTH:
            raise ValueError("private_ref exceeds length bound")
        return value

    @model_validator(mode="after")
    def commit_message_bounded(self) -> AuthenticatedRolloverDefinition:
        if len(self.integration.commit_message.encode("utf-8")) > MAX_ROLLOVER_COMMIT_MESSAGE_BYTES:
            raise ValueError("commit_message exceeds size bound")
        return self


class SequenceRolloverResolution(DomainModel):
    """Projection recording how a capped source was superseded by rollover."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    ordinal: int
    source_run_id: NonEmptyStr
    rollover_id: NonEmptyStr
    rollover_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    reviewed_patch_sha256: Sha256Hex
    reviewed_tree_sha256: GitObjectSha
    commit_sha256: GitObjectSha
    recorded_at: NonEmptyStr


class RolloverBaseState(DomainModel):
    rollover_id: NonEmptyStr
    version: int
    updated_at: NonEmptyStr
    definition_sha256: Sha256Hex
    definition_artifact_sha256: Sha256Hex
    source_run_id: NonEmptyStr
    source_run_id_prefix: NonEmptyStr
    rollover_run_id: NonEmptyStr | None = None
    sequence: RolloverSequenceBinding | None = None

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


class PreparedRolloverState(RolloverBaseState):
    kind: Literal["prepared"] = "prepared"
    prepared_at: NonEmptyStr


class ActiveRolloverState(RolloverBaseState):
    kind: Literal["active"] = "active"
    started_at: NonEmptyStr
    rollover_run_id: NonEmptyStr
    start_intent_artifact_sha256: Sha256Hex
    seed_evidence_artifact_sha256: Sha256Hex | None = None


class IntegrationPendingRolloverState(RolloverBaseState):
    kind: Literal["integration_pending"] = "integration_pending"
    started_at: NonEmptyStr
    rollover_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None


class CleanupPendingRolloverState(RolloverBaseState):
    kind: Literal["cleanup_pending"] = "cleanup_pending"
    started_at: NonEmptyStr
    rollover_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    integrated_commit_sha256: GitObjectSha
    integrated_commit_sha256_prefix: NonEmptyStr
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None
    integration_result_artifact_sha256: Sha256Hex | None = None
    cleanup_evidence_artifact_sha256: Sha256Hex | None = None


class BlockedRolloverState(RolloverBaseState):
    kind: Literal["blocked"] = "blocked"
    started_at: NonEmptyStr | None = None
    blocked_at: NonEmptyStr
    block_reason_kind: NonEmptyStr
    rollover_run_id: NonEmptyStr | None = None


class AbortPendingRolloverState(RolloverBaseState):
    kind: Literal["abort_pending"] = "abort_pending"
    started_at: NonEmptyStr
    abort_requested_at: NonEmptyStr
    rollover_run_id: NonEmptyStr | None = None


class AbortedRolloverState(RolloverBaseState):
    kind: Literal["aborted"] = "aborted"
    started_at: NonEmptyStr | None = None
    aborted_at: NonEmptyStr
    rollover_run_id: NonEmptyStr | None = None


class IntegratedRolloverState(RolloverBaseState):
    kind: Literal["integrated"] = "integrated"
    started_at: NonEmptyStr
    integrated_at: NonEmptyStr
    rollover_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    integrated_commit_sha256: GitObjectSha
    integrated_commit_sha256_prefix: NonEmptyStr
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None
    integration_result_artifact_sha256: Sha256Hex | None = None
    cleanup_evidence_artifact_sha256: Sha256Hex | None = None


RolloverState = (
    PreparedRolloverState
    | ActiveRolloverState
    | IntegrationPendingRolloverState
    | CleanupPendingRolloverState
    | BlockedRolloverState
    | AbortPendingRolloverState
    | AbortedRolloverState
    | IntegratedRolloverState
)

PREPARED_ROLLOVER_STATE_ADAPTER: TypeAdapter[PreparedRolloverState] = TypeAdapter(
    PreparedRolloverState
)
ACTIVE_ROLLOVER_STATE_ADAPTER: TypeAdapter[ActiveRolloverState] = TypeAdapter(ActiveRolloverState)
INTEGRATION_PENDING_ROLLOVER_STATE_ADAPTER: TypeAdapter[IntegrationPendingRolloverState] = (
    TypeAdapter(IntegrationPendingRolloverState)
)
CLEANUP_PENDING_ROLLOVER_STATE_ADAPTER: TypeAdapter[CleanupPendingRolloverState] = TypeAdapter(
    CleanupPendingRolloverState
)
BLOCKED_ROLLOVER_STATE_ADAPTER: TypeAdapter[BlockedRolloverState] = TypeAdapter(
    BlockedRolloverState
)
ABORT_PENDING_ROLLOVER_STATE_ADAPTER: TypeAdapter[AbortPendingRolloverState] = TypeAdapter(
    AbortPendingRolloverState
)
ABORTED_ROLLOVER_STATE_ADAPTER: TypeAdapter[AbortedRolloverState] = TypeAdapter(
    AbortedRolloverState
)
INTEGRATED_ROLLOVER_STATE_ADAPTER: TypeAdapter[IntegratedRolloverState] = TypeAdapter(
    IntegratedRolloverState
)

ROLLOVER_STATE_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    PREPARED_ROLLOVER_STATE_KIND: PREPARED_ROLLOVER_STATE_ADAPTER,
    ACTIVE_ROLLOVER_STATE_KIND: ACTIVE_ROLLOVER_STATE_ADAPTER,
    INTEGRATION_PENDING_ROLLOVER_STATE_KIND: INTEGRATION_PENDING_ROLLOVER_STATE_ADAPTER,
    CLEANUP_PENDING_ROLLOVER_STATE_KIND: CLEANUP_PENDING_ROLLOVER_STATE_ADAPTER,
    BLOCKED_ROLLOVER_STATE_KIND: BLOCKED_ROLLOVER_STATE_ADAPTER,
    ABORT_PENDING_ROLLOVER_STATE_KIND: ABORT_PENDING_ROLLOVER_STATE_ADAPTER,
    ABORTED_ROLLOVER_STATE_KIND: ABORTED_ROLLOVER_STATE_ADAPTER,
    INTEGRATED_ROLLOVER_STATE_KIND: INTEGRATED_ROLLOVER_STATE_ADAPTER,
}
