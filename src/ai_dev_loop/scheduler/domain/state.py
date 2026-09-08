"""Scheduler run state models for the central ledger."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import Field, TypeAdapter, field_validator, model_serializer, model_validator

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    NonEmptyStr,
    Sha256Hex,
    UuidSessionId,
)

SUBMITTED_STATE_SCHEMA_VERSION = 1
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


class SubmittedState(DomainModel):
    """Queued scheduler run with a verified frozen context."""

    kind: Literal["queued"] = "queued"
    schema_version: int = Field(default=SUBMITTED_STATE_SCHEMA_VERSION)
    run_id: NonEmptyStr
    version: int
    submitted_at: NonEmptyStr
    updated_at: NonEmptyStr
    idempotency_key: Sha256Hex
    context: SubmittedRunContext

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


SchedulerState = SubmittedState

SUBMITTED_CONTEXT_ADAPTER: TypeAdapter[SubmittedRunContext] = TypeAdapter(SubmittedRunContext)
SUBMITTED_STATE_ADAPTER: TypeAdapter[SubmittedState] = TypeAdapter(SubmittedState)
SCHEDULER_STATE_ADAPTER: TypeAdapter[SchedulerState] = TypeAdapter(SchedulerState)


def parse_submitted_state(payload: object) -> SubmittedState:
    return SUBMITTED_STATE_ADAPTER.validate_python(payload)


def submission_identity_payload(context: SubmittedRunContext) -> dict[str, object]:
    """Canonical identity fields for idempotency (excludes run_id and timestamps)."""

    return context.model_dump(mode="json")
