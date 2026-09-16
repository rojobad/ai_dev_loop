"""Fresh-review recovery aggregate domain models for Phase 20.6."""

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

FRESH_REVIEW_RECOVERY_SCHEMA_VERSION = 1
RECOVERY_DEFINITION_ARTIFACT = "recovery/definition.json"
RECOVERY_START_INTENT_ARTIFACT = "recovery/start-intent.json"
RECOVERY_START_PROGRESS_ARTIFACT = "recovery/start-progress.json"
RECOVERY_ABORT_INTENT_ARTIFACT = "recovery/abort-intent.txt"
RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT = "recovery/integration-publication-inputs.json"
RECOVERY_INTEGRATION_INTENT_ARTIFACT = "recovery/integration-intent.json"
RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT = "recovery/integration-trusted-tree.json"
RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT = "recovery/integration-evidence.json"
RECOVERY_INTEGRATION_RESULT_ARTIFACT = "recovery/integration-result.json"
RECOVERY_WORKTREE_REGISTRATION_ARTIFACT = "recovery/worktree-registration.json"
RECOVERY_PRE_SEED_ADMISSION_ARTIFACT = "recovery/pre-seed-admission.json"
RECOVERY_SEED_EVIDENCE_ARTIFACT = "recovery/seed-evidence.json"
RECOVERY_CLEANUP_EVIDENCE_ARTIFACT = "recovery/cleanup-evidence.json"

PREPARED_RECOVERY_STATE_KIND = "prepared"
ACTIVE_RECOVERY_STATE_KIND = "active"
INTEGRATION_PENDING_RECOVERY_STATE_KIND = "integration_pending"
CLEANUP_PENDING_RECOVERY_STATE_KIND = "cleanup_pending"
BLOCKED_RECOVERY_STATE_KIND = "blocked"
ABORT_PENDING_RECOVERY_STATE_KIND = "abort_pending"
ABORTED_RECOVERY_STATE_KIND = "aborted"
INTEGRATED_RECOVERY_STATE_KIND = "integrated"

RECOVERY_TERMINAL_STATE_KINDS = frozenset(
    {
        BLOCKED_RECOVERY_STATE_KIND,
        ABORTED_RECOVERY_STATE_KIND,
        INTEGRATED_RECOVERY_STATE_KIND,
    }
)

MAX_RECOVERY_COMMIT_MESSAGE_BYTES = 4096
MAX_RECOVERY_PRIVATE_REF_LENGTH = 256


class RecoverySequenceBinding(DomainModel):
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


class RecoveryIntegrationPublicationInputs(DomainModel):
    """Frozen timestamp and Git identity inputs for integration publication."""

    schema_version: Literal[1] = 1
    recorded_at: NonEmptyStr
    git_identity: GitIdentitySnapshot


class RecoveryIntegrationIntent(DomainModel):
    """Immutable integration intent persisted before any Git mutation."""

    schema_version: Literal[1] = 1
    recovery_id: NonEmptyStr
    source_run_id: NonEmptyStr
    recovery_run_id: NonEmptyStr
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


class RecoveryCheckpointTrustedTree(DomainModel):
    """Trusted reviewed-tree evidence bound to an integration intent."""

    schema_version: Literal[1] = 1
    intent_sha256: Sha256Hex
    reviewed_tree_sha256: GitObjectSha
    reviewed_patch_sha256: Sha256Hex
    parent_head: GitObjectSha
    recorded_at: NonEmptyStr


class RecoveryIntegrationPolicy(DomainModel):
    commit_message: NonEmptyStr
    accepted_outcomes: tuple[Literal["completed", "completed_with_residual_risk"], ...] = (
        "completed",
        "completed_with_residual_risk",
    )


class FreshReviewRecoveryDefinition(DomainModel):
    """Immutable frozen recovery definition written at prepare time."""

    schema_version: Literal[1] = 1
    recovery_id: NonEmptyStr
    definition_sha256: Sha256Hex
    source_run_id: NonEmptyStr
    source_run_id_prefix: NonEmptyStr
    sequence: RecoverySequenceBinding | None = None
    repository: RepositoryBinding
    target_branch_ref: NonEmptyStr
    parent_head: GitObjectSha
    source_staged_patch_path: NonEmptyStr
    source_staged_patch_sha256: Sha256Hex
    source_staged_tree_sha256: GitObjectSha
    plan_prompt: PlanPromptBinding
    effective_config: EffectiveConfigBinding
    codex: FreshCodexReviewerBinding
    cursor: CursorBinding
    workflow: WorkflowLimits
    controller: ControllerBinding
    integration: RecoveryIntegrationPolicy
    managed_worktree_path_token: NonEmptyStr
    private_ref: NonEmptyStr
    recovery_worktree_key: Sha256Hex
    prepared_at: NonEmptyStr

    @field_validator("private_ref")
    @classmethod
    def private_ref_namespace(cls, value: str) -> str:
        if not value.startswith("refs/ai-dev-loop/recovery/"):
            raise ValueError("private_ref must use the package recovery namespace")
        if len(value) > MAX_RECOVERY_PRIVATE_REF_LENGTH:
            raise ValueError("private_ref exceeds length bound")
        return value

    @model_validator(mode="after")
    def commit_message_bounded(self) -> FreshReviewRecoveryDefinition:
        if len(self.integration.commit_message.encode("utf-8")) > MAX_RECOVERY_COMMIT_MESSAGE_BYTES:
            raise ValueError("commit_message exceeds size bound")
        return self


class SequenceRecoveryResolution(DomainModel):
    """Projection recording how a blocked sequence phase was replaced by recovery."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    ordinal: int
    source_run_id: NonEmptyStr
    recovery_id: NonEmptyStr
    recovery_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    reviewed_patch_sha256: Sha256Hex
    reviewed_tree_sha256: GitObjectSha
    commit_sha256: GitObjectSha
    recorded_at: NonEmptyStr


class RecoveryBaseState(DomainModel):
    recovery_id: NonEmptyStr
    version: int
    updated_at: NonEmptyStr
    definition_sha256: Sha256Hex
    definition_artifact_sha256: Sha256Hex
    source_run_id: NonEmptyStr
    source_run_id_prefix: NonEmptyStr
    recovery_run_id: NonEmptyStr | None = None
    sequence: RecoverySequenceBinding | None = None

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


class PreparedRecoveryState(RecoveryBaseState):
    kind: Literal["prepared"] = "prepared"
    prepared_at: NonEmptyStr


class ActiveRecoveryState(RecoveryBaseState):
    kind: Literal["active"] = "active"
    started_at: NonEmptyStr
    recovery_run_id: NonEmptyStr
    start_intent_artifact_sha256: Sha256Hex
    seed_evidence_artifact_sha256: Sha256Hex | None = None


class IntegrationPendingRecoveryState(RecoveryBaseState):
    kind: Literal["integration_pending"] = "integration_pending"
    started_at: NonEmptyStr
    recovery_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None


class CleanupPendingRecoveryState(RecoveryBaseState):
    kind: Literal["cleanup_pending"] = "cleanup_pending"
    started_at: NonEmptyStr
    recovery_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    integrated_commit_sha256: GitObjectSha
    integrated_commit_sha256_prefix: NonEmptyStr
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None
    integration_result_artifact_sha256: Sha256Hex | None = None
    cleanup_evidence_artifact_sha256: Sha256Hex | None = None


class BlockedRecoveryState(RecoveryBaseState):
    kind: Literal["blocked"] = "blocked"
    started_at: NonEmptyStr | None = None
    blocked_at: NonEmptyStr
    block_reason_kind: NonEmptyStr
    recovery_run_id: NonEmptyStr | None = None


class AbortPendingRecoveryState(RecoveryBaseState):
    kind: Literal["abort_pending"] = "abort_pending"
    started_at: NonEmptyStr
    abort_requested_at: NonEmptyStr
    recovery_run_id: NonEmptyStr | None = None


class AbortedRecoveryState(RecoveryBaseState):
    kind: Literal["aborted"] = "aborted"
    started_at: NonEmptyStr | None = None
    aborted_at: NonEmptyStr
    recovery_run_id: NonEmptyStr | None = None


class IntegratedRecoveryState(RecoveryBaseState):
    kind: Literal["integrated"] = "integrated"
    started_at: NonEmptyStr
    integrated_at: NonEmptyStr
    recovery_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    residual_risk: bool
    integrated_commit_sha256: GitObjectSha
    integrated_commit_sha256_prefix: NonEmptyStr
    seed_evidence_artifact_sha256: Sha256Hex | None = None
    integration_intent_artifact_sha256: Sha256Hex | None = None
    integration_trusted_tree_artifact_sha256: Sha256Hex | None = None
    integration_result_artifact_sha256: Sha256Hex | None = None
    cleanup_evidence_artifact_sha256: Sha256Hex | None = None


RecoveryState = (
    PreparedRecoveryState
    | ActiveRecoveryState
    | IntegrationPendingRecoveryState
    | CleanupPendingRecoveryState
    | BlockedRecoveryState
    | AbortPendingRecoveryState
    | AbortedRecoveryState
    | IntegratedRecoveryState
)

PREPARED_RECOVERY_STATE_ADAPTER: TypeAdapter[PreparedRecoveryState] = TypeAdapter(
    PreparedRecoveryState
)
ACTIVE_RECOVERY_STATE_ADAPTER: TypeAdapter[ActiveRecoveryState] = TypeAdapter(ActiveRecoveryState)
INTEGRATION_PENDING_RECOVERY_STATE_ADAPTER: TypeAdapter[IntegrationPendingRecoveryState] = (
    TypeAdapter(IntegrationPendingRecoveryState)
)
CLEANUP_PENDING_RECOVERY_STATE_ADAPTER: TypeAdapter[CleanupPendingRecoveryState] = TypeAdapter(
    CleanupPendingRecoveryState
)
BLOCKED_RECOVERY_STATE_ADAPTER: TypeAdapter[BlockedRecoveryState] = TypeAdapter(
    BlockedRecoveryState
)
ABORT_PENDING_RECOVERY_STATE_ADAPTER: TypeAdapter[AbortPendingRecoveryState] = TypeAdapter(
    AbortPendingRecoveryState
)
ABORTED_RECOVERY_STATE_ADAPTER: TypeAdapter[AbortedRecoveryState] = TypeAdapter(
    AbortedRecoveryState
)
INTEGRATED_RECOVERY_STATE_ADAPTER: TypeAdapter[IntegratedRecoveryState] = TypeAdapter(
    IntegratedRecoveryState
)

RECOVERY_STATE_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    PREPARED_RECOVERY_STATE_KIND: PREPARED_RECOVERY_STATE_ADAPTER,
    ACTIVE_RECOVERY_STATE_KIND: ACTIVE_RECOVERY_STATE_ADAPTER,
    INTEGRATION_PENDING_RECOVERY_STATE_KIND: INTEGRATION_PENDING_RECOVERY_STATE_ADAPTER,
    CLEANUP_PENDING_RECOVERY_STATE_KIND: CLEANUP_PENDING_RECOVERY_STATE_ADAPTER,
    BLOCKED_RECOVERY_STATE_KIND: BLOCKED_RECOVERY_STATE_ADAPTER,
    ABORT_PENDING_RECOVERY_STATE_KIND: ABORT_PENDING_RECOVERY_STATE_ADAPTER,
    ABORTED_RECOVERY_STATE_KIND: ABORTED_RECOVERY_STATE_ADAPTER,
    INTEGRATED_RECOVERY_STATE_KIND: INTEGRATED_RECOVERY_STATE_ADAPTER,
}
