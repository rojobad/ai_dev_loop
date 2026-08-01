"""Project configuration loading, precedence, and validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import global_config_path, project_config_filename

PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CURSOR_OUTPUT_FORMATS = frozenset({"stream-json", "json", "text"})
CURSOR_SANDBOX_VALUES = frozenset({"enabled", "disabled"})
CODEX_SANDBOX_VALUES = frozenset({"read-only", "workspace-write", "danger-full-access"})
CODEX_REVIEW_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
STAGE_MODES = frozenset({"all"})
INHERITED_FROM_SESSION = "inherited from session"


def normalize_optional_review_model(value: str | None) -> str | None:
    """Validate an optional Codex review model override."""

    if value is None:
        return None
    if not value.strip():
        raise ValueError("codex.review_model must not be empty")
    return value


def normalize_optional_review_reasoning_effort(value: str | None) -> str | None:
    """Validate an optional Codex review reasoning-effort override."""

    if value is None:
        return None
    if not value.strip():
        raise ValueError("codex.review_reasoning_effort must not be empty")
    if value not in CODEX_REVIEW_REASONING_EFFORTS:
        raise ValueError(
            "codex.review_reasoning_effort must be one of: "
            f"{sorted(CODEX_REVIEW_REASONING_EFFORTS)}"
        )
    return value


class ProjectSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str

    @field_validator("name")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if not PROJECT_SLUG_PATTERN.match(value):
            raise ValueError("project.name must be a lowercase slug with optional hyphens")
        return value


class CursorSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "agent"
    model: str = "composer-2.5-fast"
    output_format: str = "stream-json"
    force: bool = True
    trust_workspace: bool = True
    sandbox: str = "disabled"

    @field_validator("command", "model")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, value: str) -> str:
        if value not in CURSOR_OUTPUT_FORMATS:
            raise ValueError(
                f"cursor.output_format must be one of: {sorted(CURSOR_OUTPUT_FORMATS)}"
            )
        return value

    @field_validator("sandbox")
    @classmethod
    def validate_sandbox(cls, value: str) -> str:
        if value not in CURSOR_SANDBOX_VALUES:
            raise ValueError(f"cursor.sandbox must be one of: {sorted(CURSOR_SANDBOX_VALUES)}")
        return value


class CodexSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = "codex"
    review_model: str | None = None
    review_reasoning_effort: str | None = None
    review_skill: str = "review-staged-cursor-execution"
    sandbox: str = "workspace-write"

    @field_validator("command", "review_skill")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("review_model")
    @classmethod
    def validate_review_model(cls, value: str | None) -> str | None:
        return normalize_optional_review_model(value)

    @field_validator("review_reasoning_effort")
    @classmethod
    def validate_review_reasoning_effort(cls, value: str | None) -> str | None:
        return normalize_optional_review_reasoning_effort(value)

    @field_validator("sandbox")
    @classmethod
    def validate_sandbox(cls, value: str) -> str:
        if value not in CODEX_SANDBOX_VALUES:
            raise ValueError(f"codex.sandbox must be one of: {sorted(CODEX_SANDBOX_VALUES)}")
        return value


def format_codex_override(value: str | None) -> str:
    """Human-readable label for optional Codex model/reasoning overrides."""

    return value if value is not None else INHERITED_FROM_SESSION


class WorkflowSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_review_iterations: int = 3
    require_clean_worktree: bool = True
    stage_mode: str = "all"
    cursor_timeout_minutes: int = 90
    codex_timeout_minutes: int = 90

    @field_validator("max_review_iterations", "cursor_timeout_minutes", "codex_timeout_minutes")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")
        return value

    @field_validator("stage_mode")
    @classmethod
    def validate_stage_mode(cls, value: str) -> str:
        if value not in STAGE_MODES:
            raise ValueError(f"workflow.stage_mode must be one of: {sorted(STAGE_MODES)}")
        return value


class PromptSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    directory: str = "docs/plans"
    filename_template: str = "prompt_{plan_stem}.txt"

    @field_validator("directory", "filename_template")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @model_validator(mode="after")
    def validate_template(self) -> PromptSection:
        if "{plan_stem}" not in self.filename_template:
            raise ValueError("prompt.filename_template must contain {plan_stem}")
        return self


# Reject credentials even if someone tries alternate spellings via YAML aliases.
_GITHUB_FORBIDDEN_SECRET_KEYS = frozenset(
    {
        "token",
        "pat",
        "access_token",
        "api_key",
        "apikey",
        "password",
        "authorization",
        "auth_token",
        "github_token",
        "gh_token",
        "oauth_token",
        "bearer",
        "secret",
        "credentials",
    }
)


class GithubAcknowledgementSection(BaseModel):
    """Best-effort ``eyes`` acknowledgement telemetry while awaiting bot review.

    Disabled by default. Timeout is diagnostic only: it never retries the trigger,
    never completes the cycle, and never aborts the worker.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    reaction: str = "eyes"
    timeout_seconds: int = 300
    on_timeout: str = "diagnostic_only"

    @field_validator("reaction")
    @classmethod
    def non_empty_reaction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("github.acknowledgement.reaction must not be empty")
        return value.strip()

    @field_validator("timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: int) -> int:
        if value < 1:
            raise ValueError("github.acknowledgement.timeout_seconds must be a positive integer")
        return value

    @field_validator("on_timeout")
    @classmethod
    def validate_on_timeout(cls, value: str) -> str:
        if value != "diagnostic_only":
            raise ValueError(
                "github.acknowledgement.on_timeout must be 'diagnostic_only'; "
                "acknowledgement timeout must never retry the trigger or complete the cycle"
            )
        return value


class GithubNoFindingsCompletionSection(BaseModel):
    """Explicit no-findings completion from a configured general PR comment.

    Disabled by default. When enabled, ``accepted_comment_prefixes`` must be
    non-empty. Absence of threads is never treated as success.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    accepted_comment_prefixes: list[str] = Field(default_factory=list)
    reviewed_commit_prefix_length: int = 12

    @field_validator("accepted_comment_prefixes")
    @classmethod
    def validate_prefixes(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for prefix in value:
            if not isinstance(prefix, str) or not prefix.strip():
                raise ValueError(
                    "github.no_findings_completion.accepted_comment_prefixes "
                    "entries must be non-empty"
                )
            cleaned.append(prefix)
        return cleaned

    @field_validator("reviewed_commit_prefix_length")
    @classmethod
    def validate_prefix_length(cls, value: int) -> int:
        if value < 7 or value > 40:
            raise ValueError(
                "github.no_findings_completion.reviewed_commit_prefix_length "
                "must be between 7 and 40"
            )
        return value

    @model_validator(mode="after")
    def require_prefixes_when_enabled(self) -> GithubNoFindingsCompletionSection:
        if self.enabled and not self.accepted_comment_prefixes:
            raise ValueError(
                "github.no_findings_completion.accepted_comment_prefixes must be "
                "non-empty when enabled"
            )
        return self


class GithubSection(BaseModel):
    """Optional opt-in GitHub PR review loop policy (no secrets).

    Absent or ``enabled: false`` keeps the normal local workflow unchanged.
    Authentication uses a pre-authenticated ``gh`` CLI session only.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    command: str = "gh"
    reviewer_logins: list[str] = Field(default_factory=lambda: ["chatgpt-codex-connector"])
    review_trigger_body: str = "@codex review"
    poll_interval_seconds: int = 60
    poll_timeout_hours: int = 24
    max_external_cycles: int = 8
    user_mention: str = "rojobad"
    continue_command: str = "@rojobad /ai-dev-loop continue"
    external_review_skill: str = "review-github-pr-feedback"
    max_local_review_iterations: int = 3
    pr_base: str = "master"
    acknowledgement: GithubAcknowledgementSection = Field(
        default_factory=GithubAcknowledgementSection
    )
    no_findings_completion: GithubNoFindingsCompletionSection = Field(
        default_factory=GithubNoFindingsCompletionSection
    )

    @field_validator("command", "review_trigger_body", "user_mention", "continue_command")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("external_review_skill", "pr_base")
    @classmethod
    def non_empty_skill_or_base(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("reviewer_logins")
    @classmethod
    def validate_reviewer_logins(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("github.reviewer_logins must contain at least one login")
        cleaned: list[str] = []
        for login in value:
            if not login or not login.strip():
                raise ValueError("github.reviewer_logins entries must be non-empty")
            cleaned.append(login.strip())
        return cleaned

    @field_validator(
        "poll_interval_seconds",
        "poll_timeout_hours",
        "max_external_cycles",
        "max_local_review_iterations",
    )
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_secret_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in data:
            if str(key).lower() in _GITHUB_FORBIDDEN_SECRET_KEYS:
                raise ValueError(
                    f"github configuration must not contain credentials field {key!r}; "
                    "authenticate the gh CLI separately"
                )
        return data


class PrReviewV2NoFindingsSection(BaseModel):
    """Optional no-findings completion policy for ``pr_review_v2`` (disabled by default)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    accepted_comment_prefixes: list[str] = Field(default_factory=list)
    accept_bot_thumbs_up: bool = False
    reviewed_commit_prefix_length: int = 12

    @field_validator("accepted_comment_prefixes")
    @classmethod
    def validate_prefixes(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for prefix in value:
            if not isinstance(prefix, str) or not prefix.strip():
                raise ValueError(
                    "pr_review_v2.no_findings.accepted_comment_prefixes entries must be non-empty"
                )
            cleaned.append(prefix)
        return cleaned

    @field_validator("reviewed_commit_prefix_length")
    @classmethod
    def validate_prefix_length(cls, value: int) -> int:
        if value < 7 or value > 40:
            raise ValueError(
                "pr_review_v2.no_findings.reviewed_commit_prefix_length must be between 7 and 40"
            )
        return value

    @model_validator(mode="after")
    def require_evidence_rule_when_enabled(self) -> PrReviewV2NoFindingsSection:
        if self.enabled and not self.accepted_comment_prefixes and not self.accept_bot_thumbs_up:
            raise ValueError(
                "pr_review_v2.no_findings requires at least one evidence rule when enabled: "
                "non-empty accepted_comment_prefixes or accept_bot_thumbs_up: true"
            )
        return self


class PrReviewV2WorkerSection(BaseModel):
    """Supervisor lease/heartbeat/idle timings for ``pr_review_v2``."""

    model_config = ConfigDict(extra="forbid")

    lease_ttl_seconds: int = 30
    heartbeat_interval_seconds: int = 10
    idle_poll_seconds: int = 1

    @field_validator("lease_ttl_seconds", "heartbeat_interval_seconds", "idle_poll_seconds")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="after")
    def heartbeat_strictly_less_than_lease(self) -> PrReviewV2WorkerSection:
        if self.heartbeat_interval_seconds >= self.lease_ttl_seconds:
            raise ValueError(
                "pr_review_v2.worker.heartbeat_interval_seconds must be strictly "
                "less than lease_ttl_seconds"
            )
        return self


_ARGV_SAFE_COMMAND_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]*$")
_ARGV_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$")
_SHELL_META_RE = re.compile(r"[;&|<>`$(){}\[\]*!?\n\r\t]")


def _validate_argv_safe_command(value: str, *, field: str) -> str:
    text = value.strip()
    if not text or text != value:
        raise ValueError(f"{field} must be a non-empty executable name or path")
    if _SHELL_META_RE.search(text) or " " in text:
        raise ValueError(f"{field} must not be a shell snippet")
    # Absolute paths and simple command names are allowed; reject shell composition.
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


class PrReviewV2Section(BaseModel):
    """Optional isolated PR review v2 configuration (temporary pre-cutover namespace).

    Absent or ``enabled: false`` leaves legacy ``pr-review`` / ``github`` behavior
    unchanged. Secrets and inline credentials are forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    gh_command: str = "gh"
    git_command: str = "git"
    ssh_command: str = "ssh"
    remote_name: str = "origin"
    base_branch: str = "master"
    reviewer_logins: list[str] = Field(default_factory=lambda: ["chatgpt-codex-connector"])
    review_trigger_body: str = "@codex review"
    user_mention: str = "rojobad"
    external_review_skill: str = "review-github-pr-feedback"
    poll_interval_seconds: int = 60
    max_external_cycles: int = 8
    max_local_iterations: int = 3
    per_call_timeout_seconds: int = 60
    overall_timeout_seconds: int = Field(default=180, ge=1, le=7200)
    max_pages: int = 20
    max_items: int = 500
    max_server_directed_wait_seconds: int = 3600
    no_findings: PrReviewV2NoFindingsSection = Field(default_factory=PrReviewV2NoFindingsSection)
    worker: PrReviewV2WorkerSection = Field(default_factory=PrReviewV2WorkerSection)

    @field_validator("gh_command", "git_command", "ssh_command")
    @classmethod
    def validate_commands(cls, value: str) -> str:
        return _validate_argv_safe_command(value, field="pr_review_v2 command")

    @field_validator("remote_name", "base_branch", "user_mention", "external_review_skill")
    @classmethod
    def validate_tokens(cls, value: str) -> str:
        return _validate_argv_safe_token(value, field="pr_review_v2 token")

    @field_validator("review_trigger_body")
    @classmethod
    def validate_trigger_body(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("pr_review_v2.review_trigger_body must not be empty")
        if _SHELL_META_RE.search(value):
            raise ValueError("pr_review_v2.review_trigger_body contains unsafe characters")
        return value

    @field_validator("reviewer_logins")
    @classmethod
    def validate_reviewer_logins(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("pr_review_v2.reviewer_logins must contain at least one login")
        cleaned: list[str] = []
        for login in value:
            cleaned.append(_validate_argv_safe_token(login, field="pr_review_v2.reviewer_logins"))
        return cleaned

    @field_validator(
        "poll_interval_seconds",
        "max_external_cycles",
        "max_local_iterations",
        "per_call_timeout_seconds",
        "overall_timeout_seconds",
        "max_pages",
        "max_items",
        "max_server_directed_wait_seconds",
    )
    @classmethod
    def positive_bounded(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_secret_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        for key in data:
            if str(key).lower() in _GITHUB_FORBIDDEN_SECRET_KEYS:
                raise ValueError(
                    f"pr_review_v2 configuration must not contain credentials field {key!r}; "
                    "authenticate the gh CLI separately"
                )
        return data

    @model_validator(mode="after")
    def validate_timeout_relationship(self) -> PrReviewV2Section:
        if self.per_call_timeout_seconds > self.overall_timeout_seconds:
            raise ValueError(
                "pr_review_v2.per_call_timeout_seconds must be <= overall_timeout_seconds"
            )
        if (
            self.max_server_directed_wait_seconds < 1
            or self.max_server_directed_wait_seconds > 86400
        ):
            raise ValueError(
                "pr_review_v2.max_server_directed_wait_seconds must be between 1 and 86400"
            )
        return self


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(alias="version")
    project: ProjectSection
    cursor: CursorSection
    codex: CodexSection
    workflow: WorkflowSection
    prompt: PromptSection
    github: GithubSection | None = None
    pr_review_v2: PrReviewV2Section | None = None

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported config version; expected 1")
        return value

    def github_enabled(self) -> bool:
        return self.github is not None and self.github.enabled

    def pr_review_v2_enabled(self) -> bool:
        return self.pr_review_v2 is not None and self.pr_review_v2.enabled


class ConfigOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str | None = None
    cursor_command: str | None = None
    cursor_model: str | None = None
    cursor_output_format: str | None = None
    codex_command: str | None = None
    codex_review_model: str | None = None
    codex_review_reasoning_effort: str | None = None
    review_skill: str | None = None
    max_review_iterations: int | None = None
    cursor_timeout_minutes: int | None = None
    codex_timeout_minutes: int | None = None


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"configuration file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"configuration file must contain a mapping: {path}")
    return data


def _parse_config(data: dict[str, Any], *, source: str) -> ProjectConfig:
    try:
        return ProjectConfig.model_validate(data)
    except Exception as exc:
        raise ValidationError(f"invalid configuration ({source}): {exc}") from exc


def default_config_dict() -> dict[str, Any]:
    return {
        "version": 1,
        "project": {"name": "example-project"},
        "cursor": {
            "command": "agent",
            "model": "composer-2.5-fast",
            "output_format": "stream-json",
            "force": True,
            "trust_workspace": True,
            "sandbox": "disabled",
        },
        "codex": {
            "command": "codex",
            "review_model": None,
            "review_reasoning_effort": None,
            "review_skill": "review-staged-cursor-execution",
            "sandbox": "workspace-write",
        },
        "workflow": {
            "max_review_iterations": 3,
            "require_clean_worktree": True,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "prompt": {
            "directory": "docs/plans",
            "filename_template": "prompt_{plan_stem}.txt",
        },
    }


def load_project_config(path: Path) -> ProjectConfig:
    return _parse_config(_load_yaml_file(path), source=str(path))


def load_optional_global_config() -> ProjectConfig | None:
    path = global_config_path()
    if not path.is_file():
        return None
    return _parse_config(_load_yaml_file(path), source=str(path))


def resolve_repo_config_path(repo_root: Path, config_path: Path | None) -> Path:
    if config_path is None:
        return repo_root / project_config_filename()
    from ai_dev_loop.runners.git import resolve_repo_relative_path

    return resolve_repo_relative_path(repo_root, config_path)


def resolve_effective_config(
    *,
    repo_root: Path,
    config_path: Path | None = None,
    overrides: ConfigOverrides | None = None,
) -> tuple[ProjectConfig, ProjectConfig, Path]:
    """Return (effective_config, source_repo_config, repo_config_path)."""
    repo_config_path = resolve_repo_config_path(repo_root, config_path)
    source_repo_config = load_project_config(repo_config_path)

    layers: list[dict[str, Any]] = [default_config_dict()]
    global_config = load_optional_global_config()
    if global_config is not None:
        # Only merge fields that were explicitly present so omitted optional
        # values do not wipe earlier layers. Explicit YAML null remains set.
        layers.append(global_config.model_dump(by_alias=True, exclude_unset=True))
    layers.append(source_repo_config.model_dump(by_alias=True, exclude_unset=True))

    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value

    if overrides is not None:
        if overrides.project_name is not None:
            merged.setdefault("project", {})["name"] = overrides.project_name
        if overrides.cursor_command is not None:
            merged.setdefault("cursor", {})["command"] = overrides.cursor_command
        if overrides.cursor_model is not None:
            merged.setdefault("cursor", {})["model"] = overrides.cursor_model
        if overrides.cursor_output_format is not None:
            merged.setdefault("cursor", {})["output_format"] = overrides.cursor_output_format
        if overrides.codex_command is not None:
            merged.setdefault("codex", {})["command"] = overrides.codex_command
        if overrides.codex_review_model is not None:
            merged.setdefault("codex", {})["review_model"] = overrides.codex_review_model
        if overrides.codex_review_reasoning_effort is not None:
            merged.setdefault("codex", {})["review_reasoning_effort"] = (
                overrides.codex_review_reasoning_effort
            )
        if overrides.review_skill is not None:
            merged.setdefault("codex", {})["review_skill"] = overrides.review_skill
        if overrides.max_review_iterations is not None:
            merged.setdefault("workflow", {})["max_review_iterations"] = (
                overrides.max_review_iterations
            )
        if overrides.cursor_timeout_minutes is not None:
            merged.setdefault("workflow", {})["cursor_timeout_minutes"] = (
                overrides.cursor_timeout_minutes
            )
        if overrides.codex_timeout_minutes is not None:
            merged.setdefault("workflow", {})["codex_timeout_minutes"] = (
                overrides.codex_timeout_minutes
            )

    effective = _parse_config(merged, source="effective")
    return effective, source_repo_config, repo_config_path


def config_to_yaml(config: ProjectConfig) -> str:
    return yaml.safe_dump(config.model_dump(by_alias=True), sort_keys=False)
