"""Scheduler run state models for the central ledger."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import (
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    NonEmptyStr,
    Sha256Hex,
    UuidSessionId,
)

SUBMITTED_STATE_SCHEMA_VERSION = 1
SCHEDULER_STATE_SCHEMA_VERSION = 2
SCHEDULER_STATE_SCHEMA_VERSION_V4 = 4
SUBMITTED_CONTEXT_SCHEMA_VERSION = 1
SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH = 2
SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED = 3


class RepositoryTargetBinding(DomainModel):
    root: NonEmptyStr
    worktree_key: Sha256Hex


class RepositoryBinding(DomainModel):
    root: NonEmptyStr
    git_common_dir: NonEmptyStr
    git_dir: NonEmptyStr
    branch: NonEmptyStr
    initial_head: NonEmptyStr
    worktree_key: Sha256Hex


class PlanPromptBinding(DomainModel):
    plan_repository_path: NonEmptyStr
    prompt_source_repository_path: NonEmptyStr
    plan_artifact_path: NonEmptyStr
    plan_sha256: Sha256Hex
    prompt_artifact_path: NonEmptyStr
    prompt_sha256: Sha256Hex


class EffectiveConfigBinding(DomainModel):
    effective_config_artifact_path: NonEmptyStr
    effective_config_sha256: Sha256Hex
    source_config_artifact_path: NonEmptyStr
    source_config_sha256: Sha256Hex


class FreshCodexReviewerBinding(DomainModel):
    review_model: NonEmptyStr
    review_reasoning_effort: NonEmptyStr
    review_model_source: Literal["explicit"]
    review_reasoning_source: Literal["explicit"]
    command: NonEmptyStr
    review_skill: NonEmptyStr
    sandbox: NonEmptyStr
    binding_artifact_path: NonEmptyStr
    binding_sha256: Sha256Hex


class CodexRuntimeBinding(DomainModel):
    session_id: UuidSessionId
    session_model: NonEmptyStr
    session_reasoning_effort: NonEmptyStr
    review_model: NonEmptyStr
    review_reasoning_effort: NonEmptyStr
    review_model_source: Literal["session", "explicit"]
    review_reasoning_source: Literal["session", "explicit"]
    model_family_warning: str | None
    session_origin: NonEmptyStr
    source_event_type: NonEmptyStr
    source_timestamp: str | None
    command: NonEmptyStr
    review_skill: NonEmptyStr
    sandbox: NonEmptyStr
    session_runtime_artifact_path: NonEmptyStr
    session_runtime_sha256: Sha256Hex


class CursorBinding(DomainModel):
    command: NonEmptyStr
    model: NonEmptyStr
    output_format: NonEmptyStr
    force: bool
    trust_workspace: bool
    sandbox: NonEmptyStr


class WorkflowLimits(DomainModel):
    max_review_iterations: int
    stage_mode: NonEmptyStr
    cursor_timeout_minutes: int
    codex_timeout_minutes: int
    require_clean_worktree: bool

    @field_validator("max_review_iterations", "cursor_timeout_minutes", "codex_timeout_minutes")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value


class ControllerBinding(DomainModel):
    controller_session_id: UuidSessionId


class SubmittedRunContext(DomainModel):
    """Frozen immutable input context for a submitted A/B scheduler run."""

    schema_version: int = Field(default=SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED)
    project_name: NonEmptyStr
    repository: RepositoryBinding | RepositoryTargetBinding
    plan_prompt: PlanPromptBinding
    effective_config: EffectiveConfigBinding
    codex: CodexRuntimeBinding | FreshCodexReviewerBinding
    cursor: CursorBinding
    workflow: WorkflowLimits
    controller: ControllerBinding
    baseline_status_artifact_path: NonEmptyStr | None = None
    baseline_status_sha256: Sha256Hex | None = None

    @field_validator("schema_version")
    @classmethod
    def schema_version_supported(cls, value: int) -> int:
        if value not in {
            SUBMITTED_CONTEXT_SCHEMA_VERSION,
            SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH,
            SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
        }:
            raise ValueError("schema_version must be 1, 2, or 3")
        return value

    @model_validator(mode="after")
    def codex_binding_matches_schema(self) -> SubmittedRunContext:
        if self.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION:
            if not isinstance(self.codex, CodexRuntimeBinding):
                raise ValueError("schema_version 1 requires CodexRuntimeBinding")
            if not isinstance(self.repository, RepositoryBinding):
                raise ValueError("schema_version 1 requires RepositoryBinding")
            if self.baseline_status_artifact_path is None or self.baseline_status_sha256 is None:
                raise ValueError("schema_version 1 requires baseline fields")
        elif self.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH:
            if not isinstance(self.codex, FreshCodexReviewerBinding):
                raise ValueError("schema_version 2 requires FreshCodexReviewerBinding")
            if not isinstance(self.repository, RepositoryBinding):
                raise ValueError("schema_version 2 requires RepositoryBinding")
            if self.baseline_status_artifact_path is None or self.baseline_status_sha256 is None:
                raise ValueError("schema_version 2 requires baseline fields")
        elif self.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED:
            if not isinstance(self.codex, FreshCodexReviewerBinding):
                raise ValueError("schema_version 3 requires FreshCodexReviewerBinding")
            if not isinstance(self.repository, RepositoryTargetBinding):
                raise ValueError("schema_version 3 requires RepositoryTargetBinding")
            if (
                self.baseline_status_artifact_path is not None
                or self.baseline_status_sha256 is not None
            ):
                raise ValueError("schema_version 3 must not include baseline fields")
        return self

    @model_serializer(mode="wrap")
    def _serialize_submitted_context(
        self,
        handler: Callable[[SubmittedRunContext], dict[str, object]],
    ) -> dict[str, object]:
        data = handler(self)
        if self.schema_version == SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED:
            data.pop("baseline_status_artifact_path", None)
            data.pop("baseline_status_sha256", None)
        return data


class SchedulerRunBase(DomainModel):
    """Shared scheduler run fields across lifecycle states."""

    run_id: NonEmptyStr
    version: int
    submitted_at: NonEmptyStr
    updated_at: NonEmptyStr
    idempotency_key: Sha256Hex
    context: SubmittedRunContext

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


class SubmittedState(SchedulerRunBase):
    """Queued scheduler run with a verified frozen context."""

    kind: Literal["queued"] = "queued"
    schema_version: int = Field(default=SUBMITTED_STATE_SCHEMA_VERSION)

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value


class AuthorizedState(SchedulerRunBase):
    """Run explicitly authorized by controller A; awaiting first tick admission."""

    kind: Literal["authorized"] = "authorized"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION)
    authorized_at: NonEmptyStr
    authorized_controller_session_id: UuidSessionId

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_two(cls, value: int) -> int:
        if value != 2:
            raise ValueError("schema_version must be 2")
        return value


class AdmittedState(SchedulerRunBase):
    """Run that passed one-time Git worktree admission."""

    kind: Literal["admitted"] = "admitted"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION)
    authorized_at: NonEmptyStr
    authorized_controller_session_id: UuidSessionId
    admitted_at: NonEmptyStr
    admission_status_artifact_path: NonEmptyStr
    admission_status_sha256: Sha256Hex

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_two(cls, value: int) -> int:
        if value != 2:
            raise ValueError("schema_version must be 2")
        return value


class BlockedState(SchedulerRunBase):
    """Run blocked by failed admission or stale tick fencing."""

    kind: Literal["blocked"] = "blocked"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION)
    blocked_at: NonEmptyStr
    block_reason_kind: NonEmptyStr
    block_reason_summary: NonEmptyStr
    authorized_at: NonEmptyStr | None = None
    authorized_controller_session_id: UuidSessionId | None = None

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_two(cls, value: int) -> int:
        if value != 2:
            raise ValueError("schema_version must be 2")
        return value


class AdmittedRunCheckpoint(DomainModel):
    """Shared admission checkpoint fields for active scheduler runs."""

    authorized_at: NonEmptyStr
    authorized_controller_session_id: UuidSessionId
    admitted_at: NonEmptyStr
    admission_status_artifact_path: NonEmptyStr
    admission_status_sha256: Sha256Hex


class CodexWorkflowCheckpoint(DomainModel):
    """Durable Codex review checkpoint carried across Phase 17.5 states."""

    review_iteration: int = Field(default=1)
    reviews_completed: int = Field(default=0)
    reviewer_session_id: UuidSessionId | None = None
    binding_artifact_path: NonEmptyStr | None = None
    binding_artifact_sha256: Sha256Hex | None = None
    bootstrap_uncertainty_reason: NonEmptyStr | None = None
    latest_fix_prompt_path: NonEmptyStr | None = None
    latest_fix_prompt_sha256: Sha256Hex | None = None
    latest_correction_envelope_path: NonEmptyStr | None = None
    latest_correction_envelope_sha256: Sha256Hex | None = None
    latest_review_result_path: NonEmptyStr | None = None
    latest_review_result_sha256: Sha256Hex | None = None

    @field_validator("review_iteration", "reviews_completed")
    @classmethod
    def non_negative_review_counters(cls, value: int) -> int:
        if value < 0:
            raise ValueError("review counters must be >= 0")
        return value


class CursorWorkflowCheckpoint(DomainModel):
    """Durable cursor/staging checkpoint carried across Phase 17.4 states."""

    iteration: int = Field(default=1)
    chat_id: UuidSessionId | None = None
    chat_artifact_path: NonEmptyStr | None = None
    chat_artifact_sha256: Sha256Hex | None = None
    original_prompt_path: NonEmptyStr | None = None
    original_prompt_sha256: Sha256Hex | None = None
    continuation_envelope_path: NonEmptyStr | None = None
    continuation_envelope_sha256: Sha256Hex | None = None
    usage_limit_fingerprint_path: NonEmptyStr | None = None
    usage_limit_fingerprint_sha256: Sha256Hex | None = None
    cursor_output_fingerprint_path: NonEmptyStr | None = None
    cursor_output_fingerprint_sha256: Sha256Hex | None = None
    staged_patch_path: NonEmptyStr | None = None
    staged_patch_sha256: Sha256Hex | None = None
    wait_until: NonEmptyStr | None = None

    @field_validator("iteration")
    @classmethod
    def iteration_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("iteration must be >= 1")
        return value


def _schema_version_is_four(value: int) -> int:
    if value != SCHEDULER_STATE_SCHEMA_VERSION_V4:
        raise ValueError("schema_version must be 4")
    return value


class PreflightCompleteState(SchedulerRunBase):
    """Run that passed bounded preflight and tool compatibility probes."""

    kind: Literal["preflight_complete"] = "preflight_complete"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint = Field(default_factory=CursorWorkflowCheckpoint)

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)


class CursorReadyState(SchedulerRunBase):
    """Run with a persisted exact Cursor chat ID bound for turns."""

    kind: Literal["cursor_ready"] = "cursor_ready"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint = Field(default_factory=CodexWorkflowCheckpoint)

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def chat_id_required(self) -> CursorReadyState:
        if not self.cursor.chat_id:
            raise ValueError("cursor_ready requires chat_id")
        return self


class WaitingUsageLimitState(SchedulerRunBase):
    """Run waiting for provider retry-after or the five-hour fallback."""

    kind: Literal["waiting_usage_limit"] = "waiting_usage_limit"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint = Field(default_factory=CodexWorkflowCheckpoint)

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def usage_limit_fields_required(self) -> WaitingUsageLimitState:
        if not self.cursor.chat_id:
            raise ValueError("waiting_usage_limit requires chat_id")
        if not self.cursor.wait_until:
            raise ValueError("waiting_usage_limit requires wait_until")
        if not self.cursor.usage_limit_fingerprint_path:
            raise ValueError("waiting_usage_limit requires usage_limit fingerprint")
        if not self.cursor.original_prompt_path:
            raise ValueError("waiting_usage_limit requires original prompt binding")
        return self


class AwaitingCodexReviewState(SchedulerRunBase):
    """Run staged and waiting for Phase 17.5 Codex review bootstrap or resume."""

    kind: Literal["awaiting_codex_review"] = "awaiting_codex_review"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint = Field(default_factory=CodexWorkflowCheckpoint)

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def staging_fields_required(self) -> AwaitingCodexReviewState:
        if not self.cursor.staged_patch_path or not self.cursor.staged_patch_sha256:
            raise ValueError("awaiting_codex_review requires staged patch artifacts")
        if self.codex.bootstrap_uncertainty_reason:
            raise ValueError("awaiting_codex_review cannot carry bootstrap uncertainty")
        return self


class WaitingForCursorFixState(SchedulerRunBase):
    """Run waiting for a Cursor correction turn after actionable Codex findings."""

    kind: Literal["waiting_for_cursor_fix"] = "waiting_for_cursor_fix"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def fix_prompt_required(self) -> WaitingForCursorFixState:
        if not self.codex.reviewer_session_id:
            raise ValueError("waiting_for_cursor_fix requires bound reviewer identity")
        if not self.codex.latest_fix_prompt_path or not self.codex.latest_fix_prompt_sha256:
            raise ValueError("waiting_for_cursor_fix requires persisted fix prompt")
        return self


class CompletedState(SchedulerRunBase):
    """Run completed with no actionable findings and acceptable tests status."""

    kind: Literal["completed"] = "completed"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def reviewer_bound(self) -> CompletedState:
        if not self.codex.reviewer_session_id:
            raise ValueError("completed requires bound reviewer identity")
        return self


class CompletedWithResidualRiskState(SchedulerRunBase):
    """Run completed with no actionable findings but residual test risk."""

    kind: Literal["completed_with_residual_risk"] = "completed_with_residual_risk"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def reviewer_bound(self) -> CompletedWithResidualRiskState:
        if not self.codex.reviewer_session_id:
            raise ValueError("completed_with_residual_risk requires bound reviewer identity")
        return self


class AbortedState(SchedulerRunBase):
    """Run aborted by operator request; preserves frozen context and checkpoints."""

    kind: Literal["aborted"] = "aborted"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    aborted_at: NonEmptyStr
    abort_reason: NonEmptyStr
    prior_state_kind: NonEmptyStr
    checkpoint: AdmittedRunCheckpoint | None = None
    cursor: CursorWorkflowCheckpoint | None = None
    codex: CodexWorkflowCheckpoint | None = None
    authorized_at: NonEmptyStr | None = None
    authorized_controller_session_id: UuidSessionId | None = None

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)


class MaxIterationsReachedState(SchedulerRunBase):
    """Run reached the review iteration budget with actionable findings remaining."""

    kind: Literal["max_iterations_reached"] = "max_iterations_reached"
    schema_version: int = Field(default=SCHEDULER_STATE_SCHEMA_VERSION_V4)
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_four(cls, value: int) -> int:
        return _schema_version_is_four(value)

    @model_validator(mode="after")
    def reviewer_bound(self) -> MaxIterationsReachedState:
        if not self.codex.reviewer_session_id:
            raise ValueError("max_iterations_reached requires bound reviewer identity")
        return self


def _scheduler_state_discriminator(value: object) -> str:
    if isinstance(value, dict):
        kind = value.get("kind")
        if isinstance(kind, str):
            return kind
    kind = getattr(value, "kind", None)
    if isinstance(kind, str):
        return kind
    raise ValueError("scheduler state payload must include kind")


SchedulerState = Annotated[
    Annotated[SubmittedState, Tag("queued")]
    | Annotated[AuthorizedState, Tag("authorized")]
    | Annotated[AdmittedState, Tag("admitted")]
    | Annotated[PreflightCompleteState, Tag("preflight_complete")]
    | Annotated[CursorReadyState, Tag("cursor_ready")]
    | Annotated[WaitingUsageLimitState, Tag("waiting_usage_limit")]
    | Annotated[AwaitingCodexReviewState, Tag("awaiting_codex_review")]
    | Annotated[WaitingForCursorFixState, Tag("waiting_for_cursor_fix")]
    | Annotated[CompletedState, Tag("completed")]
    | Annotated[CompletedWithResidualRiskState, Tag("completed_with_residual_risk")]
    | Annotated[MaxIterationsReachedState, Tag("max_iterations_reached")]
    | Annotated[AbortedState, Tag("aborted")]
    | Annotated[BlockedState, Tag("blocked")],
    Discriminator(_scheduler_state_discriminator),
]

SCHEDULER_TERMINAL_STATE_KINDS = frozenset(
    {
        "completed",
        "completed_with_residual_risk",
        "max_iterations_reached",
        "aborted",
        "blocked",
    }
)

SCHEDULER_ABORTABLE_STATE_KINDS = frozenset(
    {
        "queued",
        "authorized",
        "admitted",
        "preflight_complete",
        "cursor_ready",
        "waiting_usage_limit",
        "awaiting_codex_review",
        "waiting_for_cursor_fix",
    }
)

SUBMITTED_CONTEXT_ADAPTER: TypeAdapter[SubmittedRunContext] = TypeAdapter(SubmittedRunContext)
SUBMITTED_STATE_ADAPTER: TypeAdapter[SubmittedState] = TypeAdapter(SubmittedState)
SCHEDULER_STATE_ADAPTER: TypeAdapter[SchedulerState] = TypeAdapter(SchedulerState)


def parse_submitted_state(payload: object) -> SubmittedState:
    return SUBMITTED_STATE_ADAPTER.validate_python(payload)


def parse_scheduler_state(
    payload: object,
) -> (
    SubmittedState
    | AuthorizedState
    | AdmittedState
    | PreflightCompleteState
    | CursorReadyState
    | WaitingUsageLimitState
    | AwaitingCodexReviewState
    | WaitingForCursorFixState
    | CompletedState
    | CompletedWithResidualRiskState
    | MaxIterationsReachedState
    | AbortedState
    | BlockedState
):
    if isinstance(
        payload,
        (
            SubmittedState,
            AuthorizedState,
            AdmittedState,
            PreflightCompleteState,
            CursorReadyState,
            WaitingUsageLimitState,
            AwaitingCodexReviewState,
            WaitingForCursorFixState,
            CompletedState,
            CompletedWithResidualRiskState,
            MaxIterationsReachedState,
            AbortedState,
            BlockedState,
        ),
    ):
        return payload
    return SCHEDULER_STATE_ADAPTER.validate_python(payload)


def submission_identity_payload(context: SubmittedRunContext) -> dict[str, object]:
    """Canonical identity fields for idempotency (excludes run_id and timestamps)."""

    return context.model_dump(mode="json")
