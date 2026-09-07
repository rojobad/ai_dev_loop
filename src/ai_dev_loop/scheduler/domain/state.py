"""Scheduler run state models for the central ledger."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, TypeAdapter, field_validator

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    NonEmptyStr,
    Sha256Hex,
    UuidSessionId,
)

SUBMITTED_STATE_SCHEMA_VERSION = 1
SUBMITTED_CONTEXT_SCHEMA_VERSION = 1


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

    schema_version: int = Field(default=SUBMITTED_CONTEXT_SCHEMA_VERSION)
    project_name: NonEmptyStr
    repository: RepositoryBinding
    plan_prompt: PlanPromptBinding
    effective_config: EffectiveConfigBinding
    codex: CodexRuntimeBinding
    cursor: CursorBinding
    workflow: WorkflowLimits
    controller: ControllerBinding
    baseline_status_artifact_path: NonEmptyStr
    baseline_status_sha256: Sha256Hex

    @field_validator("schema_version")
    @classmethod
    def schema_version_is_one(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value


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
