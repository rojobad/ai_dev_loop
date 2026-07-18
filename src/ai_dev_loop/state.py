"""Run state models, hashing, manifests, and atomic persistence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_dev_loop.config import (
    normalize_optional_review_model,
    normalize_optional_review_reasoning_effort,
)
from ai_dev_loop.paths import set_sensitive_file_mode


class RunStatus(StrEnum):
    PREPARED = "prepared"
    VALIDATING = "validating"
    RUNNING_CURSOR = "running_cursor"
    STAGING = "staging"
    REVIEWING = "reviewing"
    WAITING_FOR_CURSOR_FIX = "waiting_for_cursor_fix"
    COMPLETED = "completed"
    COMPLETED_WITH_RESIDUAL_RISK = "completed_with_residual_risk"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    ABORTED = "aborted"
    # Optional post-PR GitHub review cycle (Phase 15). Local Cursor/staging/review
    # segments still use the existing statuses above while github_pr_review is set.
    AWAITING_BOT_REVIEW = "awaiting_bot_review"
    EVALUATING_BOT_FEEDBACK = "evaluating_bot_feedback"
    WAITING_FOR_USER_ATTENTION = "waiting_for_user_attention"
    PUBLISHING_EXTERNAL_FIX = "publishing_external_fix"


ALLOWED_STATUS_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARED: frozenset(
        {
            RunStatus.VALIDATING,
            RunStatus.AWAITING_BOT_REVIEW,
            RunStatus.ABORTED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.VALIDATING: frozenset(
        {
            RunStatus.RUNNING_CURSOR,
            RunStatus.STAGING,
            RunStatus.REVIEWING,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }
    ),
    RunStatus.RUNNING_CURSOR: frozenset(
        {
            RunStatus.STAGING,
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }
    ),
    RunStatus.STAGING: frozenset({RunStatus.REVIEWING, RunStatus.FAILED, RunStatus.ABORTED}),
    RunStatus.REVIEWING: frozenset(
        {
            RunStatus.WAITING_FOR_CURSOR_FIX,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
            RunStatus.MAX_ITERATIONS_REACHED,
            RunStatus.PUBLISHING_EXTERNAL_FIX,
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }
    ),
    RunStatus.WAITING_FOR_CURSOR_FIX: frozenset(
        {
            RunStatus.RUNNING_CURSOR,
            RunStatus.MAX_ITERATIONS_REACHED,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.COMPLETED_WITH_RESIDUAL_RISK: frozenset(),
    # A user can explicitly increase the budget for this one terminal outcome.
    # The extension command records the decision and restores the durable
    # checkpoint that already has a stored Cursor fix prompt.
    RunStatus.MAX_ITERATIONS_REACHED: frozenset({RunStatus.WAITING_FOR_CURSOR_FIX}),
    RunStatus.INTERRUPTED: frozenset(
        {
            RunStatus.VALIDATING,
            RunStatus.RUNNING_CURSOR,
            RunStatus.STAGING,
            RunStatus.REVIEWING,
            RunStatus.AWAITING_BOT_REVIEW,
            RunStatus.EVALUATING_BOT_FEEDBACK,
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.PUBLISHING_EXTERNAL_FIX,
            RunStatus.ABORTED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.FAILED: frozenset(),
    RunStatus.ABORTED: frozenset(),
    RunStatus.AWAITING_BOT_REVIEW: frozenset(
        {
            RunStatus.EVALUATING_BOT_FEEDBACK,
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        }
    ),
    RunStatus.EVALUATING_BOT_FEEDBACK: frozenset(
        {
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.RUNNING_CURSOR,
            RunStatus.AWAITING_BOT_REVIEW,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        }
    ),
    RunStatus.WAITING_FOR_USER_ATTENTION: frozenset(
        {
            RunStatus.EVALUATING_BOT_FEEDBACK,
            RunStatus.AWAITING_BOT_REVIEW,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        }
    ),
    RunStatus.PUBLISHING_EXTERNAL_FIX: frozenset(
        {
            RunStatus.AWAITING_BOT_REVIEW,
            RunStatus.WAITING_FOR_USER_ATTENTION,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
            RunStatus.INTERRUPTED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
        }
    ),
}


class ProjectRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


class RepositoryState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    git_common_dir: str
    git_dir: str
    branch: str
    initial_head: str
    baseline_status_path: str


class PlanState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_path: str
    snapshot_path: str
    sha256: str


class PromptState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_repository_path: str
    snapshot_path: str
    sha256: str


REVIEW_RUNTIME_SOURCES = frozenset({"session", "explicit", "legacy_inherit"})


class CodexState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    session_id: str
    session_model: str | None = None
    session_reasoning_effort: str | None = None
    # Effective values passed to Codex for Phase 10 runs. Historical Phase 9 runs may
    # still store null with review_*_source absent or legacy_inherit.
    review_model: str | None
    review_reasoning_effort: str | None = None
    review_model_source: str | None = None
    review_reasoning_source: str | None = None
    model_family_warning: str | None = None
    review_skill: str
    sandbox: str

    @field_validator("review_model")
    @classmethod
    def validate_review_model(cls, value: str | None) -> str | None:
        return normalize_optional_review_model(value)

    @field_validator("review_reasoning_effort", "session_reasoning_effort")
    @classmethod
    def validate_review_reasoning_effort(cls, value: str | None) -> str | None:
        return normalize_optional_review_reasoning_effort(value)

    @field_validator("session_model")
    @classmethod
    def validate_session_model(cls, value: str | None) -> str | None:
        return normalize_optional_review_model(value)

    @field_validator("review_model_source", "review_reasoning_source")
    @classmethod
    def validate_runtime_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in REVIEW_RUNTIME_SOURCES:
            raise ValueError(
                f"review runtime source must be one of: {sorted(REVIEW_RUNTIME_SOURCES)}"
            )
        return value


class CursorState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str
    model: str
    output_format: str
    force: bool
    trust_workspace: bool
    sandbox: str
    chat_id: str | None = None


class WorkflowState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_review_iterations: int
    current_review_iteration: int = 0
    # Counts local Codex review passes toward max_review_iterations when set.
    # None preserves legacy behavior where the artifact iteration number is the
    # budget. External-feedback scheduling sets this to 0 so a high-numbered
    # fresh Cursor iteration does not immediately exhaust the local budget.
    local_review_count: int | None = Field(default=None, ge=0)
    stage_mode: str
    cursor_timeout_minutes: int
    codex_timeout_minutes: int


RECOVERY_RUNTIME_MIGRATIONS = frozenset({"none", "phase9_session_capture"})
RECOVERY_REASON_CODES = frozenset(
    {
        "codex_review_failed",
        "codex_review_result_invalid",
        "codex_review_result_artifact_missing",
        "codex_review_processing_failed",
        "correction_staging_failed",
        "initial_staging_failed",
        "cursor_usage_limit",
        "github_adjudication_schema_incompatible",
        "external_feedback_cursor_not_started",
    }
)
RECOVERY_CHECKPOINTS = frozenset(
    {
        "staging",
        "reviewing",
        "process_review",
        "cursor",
        "external_adjudication",
        "external_feedback_cursor",
    }
)


class RecoveryState(BaseModel):
    """Lineage for a successor run created by `ai_dev_loop recover`."""

    model_config = ConfigDict(extra="forbid")

    source_run_id: str = Field(min_length=1)
    source_status: str
    source_iteration: int = Field(ge=1)
    recovered_checkpoint: str
    source_staged_patch_sha256: str | None = None
    created_at: datetime
    runtime_migration: str
    reason_code: str
    cursor_output_fingerprint_sha256: str | None = None
    previous_staged_patch_sha256: str | None = None
    legacy_cursor_output_adopted: bool | None = None
    legacy_cursor_usage_limit_adopted: bool | None = None
    source_cursor_model: str | None = None
    cursor_model_fallback: str | None = None
    source_prompt_path: str | None = None
    source_prompt_sha256: str | None = None
    usage_limit_fingerprint_sha256: str | None = None
    usage_limit_fingerprint_path: str | None = None
    continuation_envelope_path: str | None = None
    continuation_envelope_sha256: str | None = None
    expected_eligible_thread_ids: list[str] | None = None

    @field_validator("source_run_id")
    @classmethod
    def validate_source_run_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source_run_id must be a non-empty string")
        return value

    @field_validator("source_status")
    @classmethod
    def validate_source_status(cls, value: str) -> str:
        if value != RunStatus.FAILED.value:
            raise ValueError("recovery source_status must be failed")
        return value

    @field_validator("recovered_checkpoint")
    @classmethod
    def validate_recovered_checkpoint(cls, value: str) -> str:
        if value not in RECOVERY_CHECKPOINTS:
            raise ValueError(f"recovered_checkpoint must be one of: {sorted(RECOVERY_CHECKPOINTS)}")
        return value

    @field_validator("runtime_migration")
    @classmethod
    def validate_runtime_migration(cls, value: str) -> str:
        if value not in RECOVERY_RUNTIME_MIGRATIONS:
            raise ValueError(
                f"runtime_migration must be one of: {sorted(RECOVERY_RUNTIME_MIGRATIONS)}"
            )
        return value

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, value: str) -> str:
        if value not in RECOVERY_REASON_CODES:
            raise ValueError(f"reason_code must be one of: {sorted(RECOVERY_REASON_CODES)}")
        return value

    @field_validator(
        "source_staged_patch_sha256",
        "cursor_output_fingerprint_sha256",
        "previous_staged_patch_sha256",
        "source_prompt_sha256",
        "usage_limit_fingerprint_sha256",
        "continuation_envelope_sha256",
    )
    @classmethod
    def validate_patch_hash(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("hash fields must be lowercase sha256 hex digests")
        return value

    @field_validator(
        "source_cursor_model",
        "cursor_model_fallback",
        "source_prompt_path",
        "usage_limit_fingerprint_path",
        "continuation_envelope_path",
    )
    @classmethod
    def validate_nonempty_optional_str(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not value.strip():
            raise ValueError("optional string recovery fields must be non-empty when set")
        return value

    @field_validator("expected_eligible_thread_ids")
    @classmethod
    def validate_expected_thread_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("expected_eligible_thread_ids must be non-empty when set")
        if any(not item or not str(item).strip() for item in value):
            raise ValueError("expected_eligible_thread_ids entries must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("expected_eligible_thread_ids must contain unique thread IDs")
        return value

    @model_validator(mode="after")
    def validate_checkpoint_fields(self) -> RecoveryState:
        cursor_only = (
            self.source_cursor_model,
            self.cursor_model_fallback,
            self.source_prompt_path,
            self.source_prompt_sha256,
            self.usage_limit_fingerprint_sha256,
            self.usage_limit_fingerprint_path,
            self.continuation_envelope_path,
            self.continuation_envelope_sha256,
        )
        if self.recovered_checkpoint == "cursor":
            if self.reason_code != "cursor_usage_limit":
                raise ValueError("cursor recovery reason_code must be cursor_usage_limit")
            if self.source_staged_patch_sha256 is not None:
                raise ValueError(
                    "source_staged_patch_sha256 must be null for cursor recovery checkpoints"
                )
            if self.expected_eligible_thread_ids is not None:
                raise ValueError(
                    "expected_eligible_thread_ids is only valid for external_adjudication "
                    "and external_feedback_cursor recovery checkpoints"
                )
            if self.cursor_output_fingerprint_sha256 is not None:
                raise ValueError(
                    "cursor_output_fingerprint_sha256 is only valid for staging recovery checkpoints"
                )
            if self.previous_staged_patch_sha256 is not None:
                raise ValueError(
                    "previous_staged_patch_sha256 is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_output_adopted:
                raise ValueError(
                    "legacy_cursor_output_adopted is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_usage_limit_adopted is True:
                if (
                    self.usage_limit_fingerprint_path is None
                    or not self.usage_limit_fingerprint_path.endswith(".usage-limit-adopted.json")
                ):
                    raise ValueError(
                        "legacy cursor usage-limit adoption requires a usage-limit-adopted "
                        "fingerprint artifact path"
                    )
            elif self.legacy_cursor_usage_limit_adopted is not None:
                raise ValueError(
                    "legacy_cursor_usage_limit_adopted must be true or null for cursor recovery"
                )
            if not self.cursor_model_fallback or not self.cursor_model_fallback.strip():
                raise ValueError(
                    "cursor_model_fallback is required for cursor recovery checkpoints"
                )
            if not self.source_cursor_model or not self.source_cursor_model.strip():
                raise ValueError("source_cursor_model is required for cursor recovery checkpoints")
            if not self.source_prompt_path or not self.source_prompt_sha256:
                raise ValueError(
                    "source_prompt_path and source_prompt_sha256 are required for "
                    "cursor recovery checkpoints"
                )
            if not self.usage_limit_fingerprint_sha256 or not self.usage_limit_fingerprint_path:
                raise ValueError(
                    "usage_limit fingerprint path and sha256 are required for "
                    "cursor recovery checkpoints"
                )
            if not self.continuation_envelope_path or not self.continuation_envelope_sha256:
                raise ValueError(
                    "continuation_envelope_path and continuation_envelope_sha256 are "
                    "required for cursor recovery checkpoints"
                )
            return self

        if self.recovered_checkpoint == "external_adjudication":
            if self.reason_code != "github_adjudication_schema_incompatible":
                raise ValueError(
                    "external_adjudication reason_code must be "
                    "github_adjudication_schema_incompatible"
                )
            if self.source_staged_patch_sha256 is not None:
                raise ValueError(
                    "source_staged_patch_sha256 must be null for external_adjudication checkpoints"
                )
            if any(value is not None for value in cursor_only):
                raise ValueError(
                    "cursor recovery fields are only valid for cursor recovery checkpoints"
                )
            if self.cursor_output_fingerprint_sha256 is not None:
                raise ValueError(
                    "cursor_output_fingerprint_sha256 is only valid for staging recovery checkpoints"
                )
            if self.previous_staged_patch_sha256 is not None:
                raise ValueError(
                    "previous_staged_patch_sha256 is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_output_adopted:
                raise ValueError(
                    "legacy_cursor_output_adopted is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_usage_limit_adopted:
                raise ValueError(
                    "legacy_cursor_usage_limit_adopted is only valid for cursor recovery checkpoints"
                )
            if not self.expected_eligible_thread_ids:
                raise ValueError(
                    "expected_eligible_thread_ids is required for external_adjudication checkpoints"
                )
            return self

        if self.recovered_checkpoint == "external_feedback_cursor":
            if self.reason_code != "external_feedback_cursor_not_started":
                raise ValueError(
                    "external_feedback_cursor reason_code must be "
                    "external_feedback_cursor_not_started"
                )
            if self.source_staged_patch_sha256 is not None:
                raise ValueError(
                    "source_staged_patch_sha256 must be null for "
                    "external_feedback_cursor checkpoints"
                )
            if not self.expected_eligible_thread_ids:
                raise ValueError(
                    "expected_eligible_thread_ids is required for "
                    "external_feedback_cursor checkpoints"
                )
            if not self.source_prompt_path or not self.source_prompt_sha256:
                raise ValueError(
                    "source_prompt_path and source_prompt_sha256 are required for "
                    "external_feedback_cursor checkpoints"
                )
            usage_limit_only = (
                self.source_cursor_model,
                self.cursor_model_fallback,
                self.usage_limit_fingerprint_sha256,
                self.usage_limit_fingerprint_path,
                self.continuation_envelope_path,
                self.continuation_envelope_sha256,
            )
            if any(value is not None for value in usage_limit_only):
                raise ValueError(
                    "cursor usage-limit recovery fields are only valid for "
                    "cursor recovery checkpoints"
                )
            if self.cursor_output_fingerprint_sha256 is not None:
                raise ValueError(
                    "cursor_output_fingerprint_sha256 is only valid for staging recovery checkpoints"
                )
            if self.previous_staged_patch_sha256 is not None:
                raise ValueError(
                    "previous_staged_patch_sha256 is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_output_adopted:
                raise ValueError(
                    "legacy_cursor_output_adopted is only valid for staging recovery checkpoints"
                )
            if self.legacy_cursor_usage_limit_adopted:
                raise ValueError(
                    "legacy_cursor_usage_limit_adopted is only valid for cursor recovery checkpoints"
                )
            return self

        if self.expected_eligible_thread_ids is not None:
            raise ValueError(
                "expected_eligible_thread_ids is only valid for external_adjudication "
                "and external_feedback_cursor recovery checkpoints"
            )

        if any(value is not None for value in cursor_only):
            raise ValueError(
                "cursor recovery fields are only valid for cursor recovery checkpoints"
            )

        if self.recovered_checkpoint == "staging":
            if not self.cursor_output_fingerprint_sha256:
                raise ValueError(
                    "cursor_output_fingerprint_sha256 is required for staging recovery checkpoints"
                )
            if self.reason_code == "initial_staging_failed":
                if self.source_iteration != 1:
                    raise ValueError("initial_staging_failed requires source_iteration == 1")
                if self.source_staged_patch_sha256 is not None:
                    raise ValueError(
                        "source_staged_patch_sha256 must be null for initial_staging_failed"
                    )
                if self.previous_staged_patch_sha256 is not None:
                    raise ValueError(
                        "previous_staged_patch_sha256 must be null for initial_staging_failed"
                    )
                if self.legacy_cursor_output_adopted:
                    raise ValueError(
                        "legacy_cursor_output_adopted is not supported for initial_staging_failed"
                    )
            elif self.reason_code == "correction_staging_failed":
                if self.source_iteration < 2:
                    raise ValueError("correction_staging_failed requires source_iteration >= 2")
                if self.source_staged_patch_sha256 is None:
                    raise ValueError(
                        "source_staged_patch_sha256 is required for correction staging recovery"
                    )
                if self.previous_staged_patch_sha256 is None:
                    raise ValueError(
                        "previous_staged_patch_sha256 is required for correction staging recovery"
                    )
                if self.previous_staged_patch_sha256 != self.source_staged_patch_sha256:
                    raise ValueError(
                        "for correction staging recovery, source_staged_patch_sha256 must equal "
                        "previous_staged_patch_sha256 (previous completed iteration patch)"
                    )
            else:
                raise ValueError(
                    "staging recovery reason_code must be initial_staging_failed or "
                    "correction_staging_failed"
                )
        elif self.legacy_cursor_output_adopted:
            raise ValueError(
                "legacy_cursor_output_adopted is only valid for staging recovery checkpoints"
            )
        elif self.legacy_cursor_usage_limit_adopted:
            raise ValueError(
                "legacy_cursor_usage_limit_adopted is only valid for cursor recovery checkpoints"
            )
        elif self.cursor_output_fingerprint_sha256 is not None:
            raise ValueError(
                "cursor_output_fingerprint_sha256 is only valid for staging recovery checkpoints"
            )
        elif self.previous_staged_patch_sha256 is not None:
            raise ValueError(
                "previous_staged_patch_sha256 is only valid for staging recovery checkpoints"
            )
        elif self.source_staged_patch_sha256 is None:
            raise ValueError(
                "source_staged_patch_sha256 is required for review recovery checkpoints"
            )
        return self


class ControllerState(BaseModel):
    """Optional A/B controller identity for remote launch and status lookup.

    Reviewer identity remains ``codex.session_id``. Historical runs omit this
    section entirely and keep the legacy start/resume workflow.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    controller_session_id: str


FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

GITHUB_PR_ORIGINS = frozenset({"source_run", "independent_pr"})
GITHUB_PR_LIFECYCLES = frozenset(
    {
        "prepared_independent",
        "publishing_initial",
        "awaiting_bot_review",
        "evaluating_bot_feedback",
        "waiting_for_user_attention",
        "fixing_external_feedback",
        "publishing_external_fix",
        "max_external_cycles_reached",
        "completed",
        "failed",
        "aborted",
        "interrupted",
    }
)
GITHUB_PUBLICATION_PHASES = frozenset(
    {
        "pre_commit",
        "committed",
        "pushed",
        "pr_bound",
    }
)


class GithubBotAcknowledgementState(BaseModel):
    """Best-effort trigger acknowledgement telemetry (never completion evidence)."""

    model_config = ConfigDict(extra="forbid")

    trigger_comment_id: str = Field(min_length=1)
    reaction: str = Field(min_length=1)
    first_observed_at: str | None = None
    acknowledgement_cleared_at: str | None = None
    timeout_diagnostic_at: str | None = None


class GithubNoFindingsCompletionEvidence(BaseModel):
    """Auditable metadata for a verified no-findings completion comment.

    Never stores the comment body—only ``body_sha256`` and rule identity.
    """

    model_config = ConfigDict(extra="forbid")

    comment_id: str = Field(min_length=1)
    created_at: str = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    body_sha256: str = Field(min_length=64, max_length=64)
    reviewed_commit_prefix: str = Field(min_length=7, max_length=40)

    @field_validator("body_sha256")
    @classmethod
    def validate_body_hash(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("body_sha256 must be a lowercase hex SHA-256 digest")
        return value


class GithubPrReviewState(BaseModel):
    """Optional post-PR cycle binding and lineage (Phase 15 / 15.5).

    Source completed runs remain terminal and immutable. Source-run successors
    store this section with the inherited exact Cursor chat and Codex reviewer
    session. Independent cycles bind an already-open PR with no source run and
    create a Cursor chat only after actionable external feedback.

    Historical Phase 15 payloads without ``origin`` deserialize as
    ``source_run``. Sensitive comment bodies live only in dedicated artifacts.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    origin: str = "source_run"
    source_run_id: str | None = None
    lifecycle: str
    cycle_number: int = Field(ge=1)
    max_external_cycles: int = Field(ge=1)
    pr_number: int | None = Field(default=None, ge=1)
    pr_url: str | None = None
    repository_name_with_owner: str | None = None
    head_branch: str = Field(min_length=1)
    base_branch: str = "master"
    bound_head_sha: str
    request_comment_id: str | None = None
    request_marker: str | None = None
    request_created_at: str | None = None
    eligible_thread_ids: list[str] = Field(default_factory=list)
    processed_thread_ids: list[str] = Field(default_factory=list)
    replied_thread_ids: list[str] = Field(default_factory=list)
    resolved_thread_ids: list[str] = Field(default_factory=list)
    publication_commit_sha: str | None = None
    staged_patch_sha256: str | None = None
    last_external_result_path: str | None = None
    last_snapshot_path: str | None = None
    continue_comment_id: str | None = None
    worker_outcome: str | None = None
    expected_eligible_thread_ids: list[str] | None = None
    external_fix_prompt_path: str | None = None
    external_cursor_iteration: int | None = Field(default=None, ge=1)
    publication_phase: str | None = None
    local_commit_sha: str | None = None
    expected_remote_sha_before_push: str | None = None
    publication_remote: str | None = None
    publication_remote_branch: str | None = None
    publication_text_path: str | None = None
    bot_acknowledgement: GithubBotAcknowledgementState | None = None
    no_findings_completion: GithubNoFindingsCompletionEvidence | None = None

    @field_validator("origin")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        if value not in GITHUB_PR_ORIGINS:
            raise ValueError(f"github_pr_review.origin must be one of: {sorted(GITHUB_PR_ORIGINS)}")
        return value

    @field_validator("lifecycle")
    @classmethod
    def validate_lifecycle(cls, value: str) -> str:
        if value not in GITHUB_PR_LIFECYCLES:
            raise ValueError(
                f"github_pr_review.lifecycle must be one of: {sorted(GITHUB_PR_LIFECYCLES)}"
            )
        return value

    @field_validator("publication_phase")
    @classmethod
    def validate_publication_phase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value not in GITHUB_PUBLICATION_PHASES:
            raise ValueError(
                "github_pr_review.publication_phase must be one of: "
                f"{sorted(GITHUB_PUBLICATION_PHASES)}"
            )
        return value

    @field_validator(
        "bound_head_sha",
        "publication_commit_sha",
        "local_commit_sha",
        "expected_remote_sha_before_push",
    )
    @classmethod
    def validate_sha(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not FULL_SHA_PATTERN.match(value):
            raise ValueError("SHA must be a 40-character lowercase hex digest")
        return value

    @field_validator("source_run_id")
    @classmethod
    def validate_source_run_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("source_run_id must be a non-empty string when set")
        return value

    @model_validator(mode="after")
    def validate_origin_and_binding(self) -> GithubPrReviewState:
        for field_name in (
            "eligible_thread_ids",
            "processed_thread_ids",
            "replied_thread_ids",
            "resolved_thread_ids",
        ):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must contain unique thread IDs")
        if self.expected_eligible_thread_ids is not None:
            expected = self.expected_eligible_thread_ids
            if not expected:
                raise ValueError("expected_eligible_thread_ids must be non-empty when set")
            if len(expected) != len(set(expected)):
                raise ValueError("expected_eligible_thread_ids must contain unique thread IDs")
            if any(not item or not str(item).strip() for item in expected):
                raise ValueError("expected_eligible_thread_ids entries must be non-empty")

        if self.origin == "source_run":
            if self.source_run_id is None:
                raise ValueError("source_run_id is required when origin is source_run")
            if self.lifecycle == "prepared_independent":
                raise ValueError(
                    "prepared_independent lifecycle is only valid for independent_pr origin"
                )
        elif self.origin == "independent_pr":
            if self.source_run_id is not None:
                raise ValueError("source_run_id must be null when origin is independent_pr")
            if self.lifecycle == "publishing_initial":
                raise ValueError("publishing_initial lifecycle is only valid for source_run origin")
            if self.pr_number is None:
                raise ValueError("pr_number is required for independent_pr origin")
            if not self.repository_name_with_owner:
                raise ValueError("repository_name_with_owner is required for independent_pr origin")

        if self.lifecycle == "publishing_initial" and self.pr_number is None:
            return self
        if (
            self.lifecycle not in {"publishing_initial", "failed", "aborted", "interrupted"}
            and self.pr_number is None
        ):
            raise ValueError("pr_number is required after the PR is bound")
        return self


class RunState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    run_id: str
    project: ProjectRef
    status: RunStatus
    created_at: datetime
    updated_at: datetime
    repository: RepositoryState
    plan: PlanState
    prompt: PromptState
    codex: CodexState
    cursor: CursorState
    workflow: WorkflowState
    iterations: list[dict[str, Any]] = Field(default_factory=list)
    result: str | None = None
    last_error: str | None = None
    recovery: RecoveryState | None = None
    controller: ControllerState | None = None
    github_pr_review: GithubPrReviewState | None = None


class ManifestArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    sha256: str


class RunManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, alias="schema_version")
    run_id: str
    project: str
    created_at: datetime
    artifacts: list[ManifestArtifact]


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def format_utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def generate_run_id(project_slug: str, *, now: datetime | None = None) -> str:
    timestamp = format_utc_timestamp(now or utc_now())
    suffix = secrets.token_hex(3)
    return f"{project_slug}-{timestamp}-{suffix}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, *, sensitive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
        if sensitive:
            set_sensitive_file_mode(path)
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, *, sensitive: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), sensitive=sensitive)


def atomic_write_json(path: Path, payload: dict[str, Any], *, sensitive: bool = False) -> None:
    text = json.dumps(payload, indent=2, sort_keys=False)
    text += "\n"
    atomic_write_text(path, text, sensitive=sensitive)


def atomic_write_yaml(path: Path, payload: dict[str, Any], *, sensitive: bool = False) -> None:
    text = yaml.safe_dump(payload, sort_keys=False)
    atomic_write_text(path, text, sensitive=sensitive)


def load_run_state(path: Path) -> RunState:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return RunState.model_validate(data)


def load_manifest(path: Path) -> RunManifest:
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return RunManifest.model_validate(data)


def transition_status(current: RunStatus, new: RunStatus) -> None:
    allowed = ALLOWED_STATUS_TRANSITIONS.get(current, frozenset())
    if new not in allowed:
        raise ValueError(f"invalid status transition: {current.value} -> {new.value}")


def serialize_run_state(state: RunState) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(state.model_dump_json(by_alias=True))
    return data


def shorten_session_id(session_id: str) -> str:
    if len(session_id) <= 12:
        return session_id
    return f"{session_id[:8]}…{session_id[-4:]}"


def save_run_state(run_directory: Path, state: RunState) -> None:
    state.updated_at = utc_now()
    atomic_write_json(
        run_directory / "state.json",
        serialize_run_state(state),
        sensitive=True,
    )


def append_run_log(run_directory: Path, message: str) -> None:
    log_path = run_directory / "logs" / "ai_dev_loop.log"
    timestamp = utc_now().isoformat()
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")
    set_sensitive_file_mode(log_path)
