"""Project configuration loading, precedence, and validation."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, TypeAlias

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_serializer,
    model_validator,
)
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import global_config_path, project_config_filename

PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
CURSOR_OUTPUT_FORMATS = frozenset({"stream-json", "json", "text"})
CURSOR_SANDBOX_VALUES = frozenset({"enabled", "disabled"})
CURSOR_SDK_VERSION = "1.0.37"
CURSOR_CREDENTIAL_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
CODEX_SANDBOX_VALUES = frozenset({"read-only", "workspace-write", "danger-full-access"})
CODEX_REVIEW_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
IMPLEMENTER_PROVIDERS = frozenset({"cursor", "codex"})
CODEX_IMPLEMENTER_SANDBOX = "workspace-write"
CODEX_IMPLEMENTER_APPROVAL_POLICY: Literal["never"] = "never"
DEFAULT_CODEX_IMPLEMENTER_TIMEOUT_MINUTES = 90
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


SdkParameterValue: TypeAlias = StrictStr | StrictBool


class CursorSdkSection(BaseModel):
    """Complete explicit Cursor SDK selection for config version 2."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["sdk-local"]
    sdk_version: Literal["1.0.37"]
    model: StrictStr
    parameters: dict[str, SdkParameterValue]
    credential_ref: StrictStr
    setting_sources: list[Literal["project", "user", "team", "mdm", "plugins", "all"]]
    sandbox: Literal["enabled", "disabled"]

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("cursor.model must be a non-empty trimmed string")
        return value

    @field_validator("parameters", mode="before")
    @classmethod
    def validate_parameters(cls, value: object) -> object:
        if not isinstance(value, dict) or not value:
            raise ValueError("cursor.parameters must be a non-empty mapping")
        canonical: dict[str, str | bool] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str) or not raw_key.strip() or raw_key != raw_key.strip():
                raise ValueError("cursor.parameters keys must be non-empty trimmed strings")
            if raw_key == "fast":
                if not isinstance(raw_value, bool):
                    raise ValueError("cursor.parameters.fast must be a strict boolean")
            elif raw_key in {"context", "reasoning_effort"}:
                if not isinstance(raw_value, str) or not raw_value.strip():
                    raise ValueError(f"cursor.parameters.{raw_key} must be a non-empty string")
            elif not isinstance(raw_value, (str, bool)):
                raise ValueError(
                    "cursor parameter values must be non-empty strings or strict booleans"
                )
            if isinstance(raw_value, str) and (
                not raw_value.strip() or raw_value != raw_value.strip()
            ):
                raise ValueError("cursor parameter string values must be non-empty and trimmed")
            canonical[raw_key] = raw_value
        return {key: canonical[key] for key in sorted(canonical)}

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str) -> str:
        if value in {".", ".."} or not CURSOR_CREDENTIAL_REF_PATTERN.fullmatch(value):
            raise ValueError("cursor.credential_ref must be a safe simple name")
        return value

    @field_validator("setting_sources")
    @classmethod
    def validate_setting_sources(
        cls,
        value: list[Literal["project", "user", "team", "mdm", "plugins", "all"]],
    ) -> list[Literal["project", "user", "team", "mdm", "plugins", "all"]]:
        if not value:
            raise ValueError("cursor.setting_sources must be a non-empty list")
        if len(value) != len(set(value)):
            raise ValueError("cursor.setting_sources must not contain duplicates")
        return value

    def wire_parameters(self) -> tuple[tuple[str, str], ...]:
        """Return the pinned SDK wire form without truthiness coercion."""

        return tuple(
            (key, str(value).lower() if isinstance(value, bool) else value)
            for key, value in self.parameters.items()
        )


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


def _reject_non_int_timeout(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")


class ImplementerSection(BaseModel):
    """Resolved implementer selection. Omitted configuration stays Cursor."""

    model_config = ConfigDict(extra="forbid")

    provider: str = "cursor"
    command: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    timeout_minutes: int | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_and_non_integer_timeout(cls, data: object) -> object:
        if data is None:
            raise ValueError("implementer must not be null")
        if not isinstance(data, dict):
            return data
        for key, value in data.items():
            if value is None:
                raise ValueError(f"implementer.{key} must not be null")
            if isinstance(value, str) and not value.strip():
                raise ValueError(f"implementer.{key} must not be empty")
            if key == "timeout_minutes":
                _reject_non_int_timeout(value, field_name="implementer.timeout_minutes")
        return data

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        if value not in IMPLEMENTER_PROVIDERS:
            raise ValueError("implementer.provider must be cursor or codex")
        return value

    @model_validator(mode="after")
    def validate_provider_fields(self) -> ImplementerSection:
        codex_fields = {
            "command": self.command,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "timeout_minutes": self.timeout_minutes,
        }
        if self.provider == "cursor":
            present = [name for name, value in codex_fields.items() if value is not None]
            if present:
                names = ", ".join(present)
                raise ValueError(
                    "Codex implementer fields are rejected for provider cursor: " + names
                )
            return self
        command = self.command or "codex"
        if self.model is None:
            raise ValueError("implementer.model is required for provider codex")
        if self.reasoning_effort is None:
            raise ValueError("implementer.reasoning_effort is required for provider codex")
        if self.reasoning_effort not in CODEX_REVIEW_REASONING_EFFORTS:
            raise ValueError(
                "implementer.reasoning_effort must be one of: "
                f"{sorted(CODEX_REVIEW_REASONING_EFFORTS)}"
            )
        timeout = (
            DEFAULT_CODEX_IMPLEMENTER_TIMEOUT_MINUTES
            if self.timeout_minutes is None
            else self.timeout_minutes
        )
        if timeout < 1:
            raise ValueError("implementer.timeout_minutes must be a positive integer")
        return self.model_copy(
            update={
                "command": command,
                "model": self.model.strip(),
                "reasoning_effort": self.reasoning_effort,
                "timeout_minutes": timeout,
            }
        )

    @model_serializer(mode="wrap")
    def _serialize_implementer(
        self,
        handler: Any,
    ) -> dict[str, object]:
        data = handler(self)
        return {key: value for key, value in data.items() if value is not None}


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


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: StrictInt = Field(alias="version")
    project: ProjectSection
    cursor: CursorSection | CursorSdkSection
    codex: CodexSection
    workflow: WorkflowSection
    prompt: PromptSection
    github: GithubSection | None = None
    implementer: ImplementerSection = Field(default_factory=ImplementerSection)

    @model_validator(mode="before")
    @classmethod
    def reject_null_implementer(cls, data: object) -> object:
        if isinstance(data, dict) and "implementer" in data and data["implementer"] is None:
            raise ValueError("implementer must not be null")
        return data

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value not in {1, 2}:
            raise ValueError("unsupported config version; expected 1 or 2")
        return value

    @model_validator(mode="after")
    def validate_cursor_runtime_for_version(self) -> ProjectConfig:
        if self.version == 1 and not isinstance(self.cursor, CursorSection):
            raise ValueError("config version 1 requires the Cursor CLI section")
        if self.version == 2 and not isinstance(self.cursor, CursorSdkSection):
            raise ValueError("config version 2 requires cursor.runtime sdk-local")
        return self

    def github_enabled(self) -> bool:
        return self.github is not None and self.github.enabled

    @model_serializer(mode="wrap")
    def _serialize_project_config(
        self,
        handler: Any,
    ) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        implementer = data.get("implementer")
        provider = "cursor"
        if isinstance(implementer, dict):
            raw_provider = implementer.get("provider", "cursor")
            if isinstance(raw_provider, str):
                provider = raw_provider
        # Omitted implementer stays out of serialized config so historical source
        # artifacts and sequence hashes remain stable. An explicit Cursor choice
        # must survive so a higher layer can replace a lower Codex selection.
        if provider == "cursor" and "implementer" not in self.model_fields_set:
            data.pop("implementer", None)
        return data


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
    implementer_provider: str | None = None
    implementer_command: str | None = None
    implementer_model: str | None = None
    implementer_reasoning_effort: str | None = None
    implementer_timeout_minutes: int | None = None

    @field_validator("implementer_timeout_minutes", mode="before")
    @classmethod
    def reject_bool_implementer_timeout(cls, value: object) -> object:
        if value is None:
            return None
        _reject_non_int_timeout(value, field_name="implementer.timeout_minutes")
        return value

    @field_validator("implementer_timeout_minutes")
    @classmethod
    def positive_implementer_timeout(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("implementer.timeout_minutes must be a positive integer")
        return value


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate settings before values are lost."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: MappingNode,
    deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ValidationError(f"configuration file not found: {path}")
    try:
        with path.open(encoding="utf-8") as handle:
            data = yaml.load(handle, Loader=_UniqueKeyLoader)
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


def default_config_dict(*, version: int = 1) -> dict[str, Any]:
    if version not in {1, 2}:
        raise ValueError("default configuration version must be 1 or 2")
    defaults: dict[str, Any] = {
        "version": version,
        "project": {"name": "example-project"},
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
    if version == 1:
        defaults["cursor"] = {
            "command": "agent",
            "model": "composer-2.5-fast",
            "output_format": "stream-json",
            "force": True,
            "trust_workspace": True,
            "sandbox": "disabled",
        }
    return defaults


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

    layers: list[dict[str, Any]] = [default_config_dict(version=source_repo_config.version)]
    global_config = load_optional_global_config()
    if global_config is not None:
        if global_config.version != source_repo_config.version:
            raise ValidationError(
                "global and repository configuration versions must match before merging"
            )
        # Only merge fields that were explicitly present so omitted optional
        # values do not wipe earlier layers. Explicit YAML null remains set.
        layers.append(global_config.model_dump(by_alias=True, exclude_unset=True))
    layers.append(source_repo_config.model_dump(by_alias=True, exclude_unset=True))

    merged = _merge_config_layers(layers)

    if overrides is not None:
        if source_repo_config.version == 2 and any(
            value is not None
            for value in (
                overrides.cursor_command,
                overrides.cursor_model,
                overrides.cursor_output_format,
            )
        ):
            raise ValidationError(
                "Cursor CLI command/model/output overrides are not valid for config version 2"
            )
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
        _apply_implementer_overrides(merged, overrides)

    effective = _parse_config(merged, source="effective")
    return effective, source_repo_config, repo_config_path


def config_to_yaml(config: ProjectConfig) -> str:
    return yaml.safe_dump(config.model_dump(by_alias=True), sort_keys=False)


def _merge_config_layers(layers: list[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if key == "implementer" and isinstance(value, dict):
                merged[key] = _merge_implementer_section(merged.get(key), value)
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def _resolved_layer_provider(section: dict[str, Any]) -> str:
    """A present implementer mapping defaults to Cursor when provider is unset.

    ``exclude_unset=True`` drops that default, so an empty mapping arrives without
    a provider key. The mapping is still an explicit Cursor selection.
    """

    provider = section.get("provider", "cursor")
    if isinstance(provider, str) and provider.strip():
        return provider
    return "cursor"


def _merge_implementer_section(current: object, incoming: dict[str, Any]) -> dict[str, Any]:
    """Replace the section when the provider changes so old fields do not leak."""

    if not isinstance(current, dict):
        return dict(incoming)
    incoming_provider = _resolved_layer_provider(incoming)
    current_provider = _resolved_layer_provider(current)
    if incoming_provider != current_provider:
        return dict(incoming)
    return {**current, **incoming}


def _apply_implementer_overrides(merged: dict[str, Any], overrides: ConfigOverrides) -> None:
    provided = (
        overrides.implementer_provider,
        overrides.implementer_command,
        overrides.implementer_model,
        overrides.implementer_reasoning_effort,
        overrides.implementer_timeout_minutes,
    )
    if all(value is None for value in provided):
        return
    current = merged.get("implementer")
    if current is None:
        section: dict[str, Any] = {}
        current_provider = "cursor"
    elif isinstance(current, dict):
        section = dict(current)
        provider = section.get("provider", "cursor")
        current_provider = provider if isinstance(provider, str) else "cursor"
    else:
        raise ValidationError("implementer must be a mapping")
    if (
        overrides.implementer_provider is not None
        and overrides.implementer_provider != current_provider
    ):
        section = {"provider": overrides.implementer_provider}
    elif overrides.implementer_provider is not None:
        section["provider"] = overrides.implementer_provider
    if overrides.implementer_command is not None:
        section["command"] = overrides.implementer_command
    if overrides.implementer_model is not None:
        section["model"] = overrides.implementer_model
    if overrides.implementer_reasoning_effort is not None:
        section["reasoning_effort"] = overrides.implementer_reasoning_effort
    if overrides.implementer_timeout_minutes is not None:
        section["timeout_minutes"] = overrides.implementer_timeout_minutes
    merged["implementer"] = section


def resolved_implementer_yaml(config: ProjectConfig) -> dict[str, Any]:
    """Canonical implementer section stored in a new effective-config artifact."""

    section = config.implementer
    if section.provider == "cursor":
        return {"provider": "cursor"}
    if (
        section.command is None
        or section.model is None
        or section.reasoning_effort is None
        or section.timeout_minutes is None
    ):
        raise ValidationError("codex implementer settings are incomplete")
    return {
        "provider": "codex",
        "command": section.command,
        "model": section.model,
        "reasoning_effort": section.reasoning_effort,
        "timeout_minutes": section.timeout_minutes,
    }


def project_config_yaml_bytes(
    config: ProjectConfig,
    *,
    include_resolved_implementer: bool,
) -> bytes:
    data = config.model_dump(by_alias=True)
    if include_resolved_implementer:
        data["implementer"] = resolved_implementer_yaml(config)
    else:
        data.pop("implementer", None)
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
