"""Frozen execution-context and local-result DTOs for PR review v2 Phase 16.7."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import Field, PositiveInt, field_validator, model_validator

from ai_dev_loop.pr_review_v2.application.contracts import AppModel
from ai_dev_loop.pr_review_v2.application.control_contracts import OriginKind
from ai_dev_loop.pr_review_v2.application.write_contracts import reject_prohibited_controls
from ai_dev_loop.pr_review_v2.domain.common import (
    AdjudicationDecisionKind,
    GitSha40,
    LocalFixOutcomeKind,
    NonEmptyId,
    NonEmptyStr,
    Sha256Hex,
    ThreadId,
    validate_argv_safe_branch_name,
)

_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "token",
        "api_key",
        "apikey",
        "password",
        "secret",
        "authorization",
        "auth",
        "credential",
        "credentials",
        "private_key",
        "access_token",
        "refresh_token",
    }
)
_CURSOR_OUTPUT_FORMATS = frozenset({"stream-json", "json", "text"})
_CURSOR_SANDBOX_VALUES = frozenset({"enabled", "disabled"})
_CODEX_SANDBOX_VALUES = frozenset({"read-only", "workspace-write", "danger-full-access"})
_CODEX_REVIEW_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
_ARGV_SAFE_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")
_ARGV_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
_SHELL_META_RE = re.compile(r"[;&|<>`$(){}\[\]*!?\n\r\t]")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validate_argv_safe_command(value: str, *, field: str) -> str:
    text = value.strip()
    if not text or text != value:
        raise ValueError(f"{field} must be a non-empty executable name or path")
    if _SHELL_META_RE.search(text) or " " in text:
        raise ValueError(f"{field} must not be a shell snippet")
    if text.startswith("/"):
        if ".." in text.split("/"):
            raise ValueError(f"{field} must not contain ..")
        return text
    if not _ARGV_SAFE_COMMAND_RE.match(text):
        raise ValueError(f"{field} must be an argv-safe executable name or path")
    return text


def _validate_argv_safe_token(value: str, *, field: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field} must not be empty")
    if _SHELL_META_RE.search(text) or not _ARGV_SAFE_TOKEN_RE.match(text):
        raise ValueError(f"{field} contains unsafe characters")
    return text


class ExecutionContextRunBinding(AppModel):
    prepared_from: OriginKind
    source_run_id: NonEmptyId | None = None
    repository: NonEmptyStr
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    expected_head_sha: GitSha40

    @field_validator("head_branch", "base_branch")
    @classmethod
    def validate_branches(cls, value: str) -> str:
        return validate_argv_safe_branch_name(value)

    @model_validator(mode="after")
    def validate_source_run(self) -> ExecutionContextRunBinding:
        if self.prepared_from is OriginKind.SOURCE_RUN and self.source_run_id is None:
            raise ValueError("source_run origin requires source_run_id")
        if self.prepared_from is OriginKind.EXISTING_PR and self.source_run_id is not None:
            raise ValueError("existing_pr origin forbids source_run_id")
        return self


class ExecutionContextCursor(AppModel):
    chat_id: NonEmptyId | None = None
    model: NonEmptyStr
    command: NonEmptyStr
    output_format: NonEmptyStr
    force: bool
    trust_workspace: bool
    sandbox: NonEmptyStr

    @field_validator("command", "model")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        return _validate_argv_safe_command(value, field="cursor.command")

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, value: str) -> str:
        if value not in _CURSOR_OUTPUT_FORMATS:
            raise ValueError(
                f"cursor.output_format must be one of: {sorted(_CURSOR_OUTPUT_FORMATS)}"
            )
        return value

    @field_validator("sandbox")
    @classmethod
    def validate_sandbox(cls, value: str) -> str:
        if value not in _CURSOR_SANDBOX_VALUES:
            raise ValueError(f"cursor.sandbox must be one of: {sorted(_CURSOR_SANDBOX_VALUES)}")
        return value


class ExecutionContextCodex(AppModel):
    session_id: NonEmptyStr
    review_model: NonEmptyStr
    review_reasoning_effort: NonEmptyStr
    command: NonEmptyStr
    sandbox: NonEmptyStr
    review_skill: NonEmptyStr
    external_review_skill: NonEmptyStr

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _UUID_RE.match(value):
            raise ValueError("codex.session_id must be an exact UUID")
        return value.lower()

    @field_validator("command", "review_skill", "external_review_skill", "review_model")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        return _validate_argv_safe_command(value, field="codex.command")

    @field_validator("review_reasoning_effort")
    @classmethod
    def validate_reasoning(cls, value: str) -> str:
        if value not in _CODEX_REVIEW_REASONING_EFFORTS:
            raise ValueError(
                "codex.review_reasoning_effort must be one of: "
                f"{sorted(_CODEX_REVIEW_REASONING_EFFORTS)}"
            )
        return value

    @field_validator("sandbox")
    @classmethod
    def validate_sandbox(cls, value: str) -> str:
        if value not in _CODEX_SANDBOX_VALUES:
            raise ValueError(f"codex.sandbox must be one of: {sorted(_CODEX_SANDBOX_VALUES)}")
        return value


class ExecutionContextWorkflow(AppModel):
    max_local_iterations: PositiveInt
    cursor_timeout_minutes: PositiveInt
    codex_timeout_minutes: PositiveInt


class ExecutionContextWorker(AppModel):
    lease_ttl_seconds: PositiveInt
    heartbeat_interval_seconds: PositiveInt
    idle_poll_seconds: PositiveInt

    @model_validator(mode="after")
    def heartbeat_strictly_less_than_lease(self) -> ExecutionContextWorker:
        if self.heartbeat_interval_seconds >= self.lease_ttl_seconds:
            raise ValueError(
                "worker.heartbeat_interval_seconds must be strictly less than lease_ttl_seconds"
            )
        return self


class ExecutionContextPrReviewV2(AppModel):
    gh_command: NonEmptyStr
    git_command: NonEmptyStr
    ssh_command: NonEmptyStr
    remote_name: NonEmptyStr
    reviewer_logins: tuple[NonEmptyStr, ...]
    review_trigger_body: NonEmptyStr
    user_mention: NonEmptyStr
    poll_interval_seconds: PositiveInt
    max_external_cycles: PositiveInt
    per_call_timeout_seconds: PositiveInt
    overall_timeout_seconds: PositiveInt = Field(le=7200)
    max_pages: PositiveInt
    max_items: PositiveInt
    max_server_directed_wait_seconds: PositiveInt
    no_findings_enabled: bool
    no_findings_prefixes: tuple[str, ...] = ()
    accept_bot_thumbs_up: bool = False
    no_findings_prefix_length: PositiveInt = 12
    worker: ExecutionContextWorker

    @field_validator("gh_command", "git_command", "ssh_command")
    @classmethod
    def validate_commands(cls, value: str) -> str:
        return _validate_argv_safe_command(value, field="pr_review_v2 command")

    @field_validator("remote_name", "user_mention")
    @classmethod
    def validate_tokens(cls, value: str) -> str:
        return _validate_argv_safe_token(value, field="pr_review_v2 token")

    @field_validator("reviewer_logins")
    @classmethod
    def validate_reviewer_logins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("reviewer_logins must contain at least one login")
        return tuple(_validate_argv_safe_token(login, field="reviewer_logins") for login in value)

    @field_validator("review_trigger_body")
    @classmethod
    def validate_trigger_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("review_trigger_body must not be empty")
        if _SHELL_META_RE.search(value):
            raise ValueError("review_trigger_body contains unsafe characters")
        return value

    @model_validator(mode="after")
    def validate_timeout_relationship(self) -> ExecutionContextPrReviewV2:
        if self.per_call_timeout_seconds > self.overall_timeout_seconds:
            raise ValueError("per_call_timeout_seconds must be <= overall_timeout_seconds")
        if (
            self.no_findings_enabled
            and not self.no_findings_prefixes
            and not self.accept_bot_thumbs_up
        ):
            raise ValueError(
                "no_findings requires at least one evidence rule when enabled: "
                "non-empty no_findings_prefixes or accept_bot_thumbs_up"
            )
        return self


class ExecutionContextPlanPrompt(AppModel):
    plan_path: NonEmptyStr
    plan_sha256: Sha256Hex
    prompt_path: NonEmptyStr
    prompt_sha256: Sha256Hex
    accepted_patch_sha256: Sha256Hex | None = None


class ExecutionContextArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.execution_context"] = (
        "ai_dev_loop.pr_review_v2.execution_context"
    )
    schema_version: Literal[1] = 1
    run_binding: ExecutionContextRunBinding
    cursor: ExecutionContextCursor
    codex: ExecutionContextCodex
    workflow: ExecutionContextWorkflow
    pr_review_v2: ExecutionContextPrReviewV2
    plan_prompt: ExecutionContextPlanPrompt
    repository_root: NonEmptyStr
    input_artifact_hashes: dict[str, Sha256Hex] = Field(default_factory=dict)

    @field_validator("repository_root")
    @classmethod
    def validate_repository_root(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("repository_root must be an absolute path")
        if _SHELL_META_RE.search(value) or "\x00" in value:
            raise ValueError("repository_root contains unsafe characters")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_secret_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in data:
            if str(key).lower() in _FORBIDDEN_SECRET_KEYS:
                raise ValueError(f"execution context must not contain credentials field {key!r}")
        return data


class PublicationGenerationResultArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.publication_generation"] = (
        "ai_dev_loop.pr_review_v2.publication_generation"
    )
    schema_version: Literal[1] = 1
    title: NonEmptyStr
    body: str = ""
    commit_subject: NonEmptyStr
    commit_body: str = ""
    run_id: NonEmptyId
    cycle_number: PositiveInt
    effect_id: NonEmptyId
    bound_head_sha: GitSha40
    evidence_ref_sha256: Sha256Hex
    patch_ref_sha256: Sha256Hex

    @field_validator("title", "body", "commit_subject", "commit_body")
    @classmethod
    def reject_control(cls, value: str) -> str:
        return reject_prohibited_controls(value, field_name="publication generation text")


class ExternalAdjudicationDecision(AppModel):
    thread_id: ThreadId
    decision: AdjudicationDecisionKind
    safe_summary: NonEmptyStr
    reply_body: str | None = None

    @field_validator("safe_summary", "reply_body")
    @classmethod
    def reject_control(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return reject_prohibited_controls(value, field_name="adjudication text")


class ExternalAdjudicationResultArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.external_adjudication"] = (
        "ai_dev_loop.pr_review_v2.external_adjudication"
    )
    schema_version: Literal[1] = 1
    decisions: tuple[ExternalAdjudicationDecision, ...]
    fix_prompt_text: str | None = None
    run_id: NonEmptyId
    cycle_number: PositiveInt
    effect_id: NonEmptyId
    bound_head_sha: GitSha40
    frozen_thread_ids: tuple[ThreadId, ...]
    snapshot_ref_sha256: Sha256Hex
    execution_context_ref_sha256: Sha256Hex

    @field_validator("fix_prompt_text")
    @classmethod
    def reject_fix_prompt_control(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return reject_prohibited_controls(value, field_name="fix prompt text")

    @model_validator(mode="after")
    def validate_decisions(self) -> ExternalAdjudicationResultArtifact:
        if not self.decisions:
            raise ValueError("decisions must be non-empty")
        decision_ids = tuple(item.thread_id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("decisions must have unique thread IDs")
        if set(decision_ids) != set(self.frozen_thread_ids):
            raise ValueError("decisions must cover exactly the frozen thread set")
        if len(self.frozen_thread_ids) != len(set(self.frozen_thread_ids)):
            raise ValueError("frozen_thread_ids must be unique")
        has_actionable = any(
            item.decision is AdjudicationDecisionKind.ACTIONABLE for item in self.decisions
        )
        if has_actionable:
            if not self.fix_prompt_text or not self.fix_prompt_text.strip():
                raise ValueError("actionable adjudication requires fix_prompt_text")
            for item in self.decisions:
                if item.decision is AdjudicationDecisionKind.ACTIONABLE:
                    if item.reply_body is not None:
                        raise ValueError("actionable decisions must not carry reply_body")
                elif item.reply_body is None or not item.reply_body.strip():
                    raise ValueError("non-actionable decisions require reply_body")
        else:
            if self.fix_prompt_text is not None:
                raise ValueError("reply-only adjudication forbids fix_prompt_text")
            for item in self.decisions:
                if item.reply_body is None or not item.reply_body.strip():
                    raise ValueError("reply-only decisions require reply_body")
        return self


class LocalFixResultArtifact(AppModel):
    schema_name: Literal["ai_dev_loop.pr_review_v2.local_fix_result"] = (
        "ai_dev_loop.pr_review_v2.local_fix_result"
    )
    schema_version: Literal[1] = 1
    outcome: LocalFixOutcomeKind
    accepted_patch_sha256: Sha256Hex | None = None
    new_head_sha: GitSha40 | None = None
    carrier_run_id: NonEmptyId
    cursor_chat_id: NonEmptyId | None = None
    codex_session_id: NonEmptyStr
    iteration_count: PositiveInt
    result_message_safe: NonEmptyStr
    needs_external_continuation: bool
    run_id: NonEmptyId
    cycle_number: PositiveInt
    effect_id: NonEmptyId
    bound_head_sha: GitSha40
    fix_prompt_ref_sha256: Sha256Hex
    execution_context_ref_sha256: Sha256Hex

    @field_validator("codex_session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        if not _UUID_RE.match(value):
            raise ValueError("codex_session_id must be an exact UUID")
        return value.lower()

    @field_validator("result_message_safe")
    @classmethod
    def reject_sensitive_markers(cls, value: str) -> str:
        lowered = value.lower()
        banned = ("-----begin", "authorization:", "api_key", "token=", "password=")
        if any(marker in lowered for marker in banned):
            raise ValueError("result_message_safe must not contain sensitive markers")
        return value

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> LocalFixResultArtifact:
        accepted = self.outcome in {
            LocalFixOutcomeKind.ACCEPTED,
            LocalFixOutcomeKind.ACCEPTED_WITH_RESIDUAL_RISK,
        }
        if accepted:
            if self.accepted_patch_sha256 is None or self.new_head_sha is None:
                raise ValueError("accepted local fix requires patch hash and new head SHA")
        elif self.outcome is LocalFixOutcomeKind.ABORTED:
            if self.accepted_patch_sha256 is not None or self.new_head_sha is not None:
                raise ValueError("aborted local fix must not carry acceptance fields")
        else:
            if self.accepted_patch_sha256 is not None or self.new_head_sha is not None:
                raise ValueError("non-accepted local fix must not carry acceptance fields")
        return self


__all__ = [
    "ExecutionContextArtifact",
    "ExecutionContextCodex",
    "ExecutionContextCursor",
    "ExecutionContextPlanPrompt",
    "ExecutionContextPrReviewV2",
    "ExecutionContextRunBinding",
    "ExecutionContextWorker",
    "ExecutionContextWorkflow",
    "ExternalAdjudicationDecision",
    "ExternalAdjudicationResultArtifact",
    "LocalFixResultArtifact",
    "PublicationGenerationResultArtifact",
]
