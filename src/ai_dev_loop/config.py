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
STAGE_MODES = frozenset({"all"})


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
    review_model: str
    review_skill: str = "review-staged-cursor-execution"
    sandbox: str = "workspace-write"

    @field_validator("command", "review_model", "review_skill")
    @classmethod
    def non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("sandbox")
    @classmethod
    def validate_sandbox(cls, value: str) -> str:
        if value not in CODEX_SANDBOX_VALUES:
            raise ValueError(f"codex.sandbox must be one of: {sorted(CODEX_SANDBOX_VALUES)}")
        return value


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


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(alias="version")
    project: ProjectSection
    cursor: CursorSection
    codex: CodexSection
    workflow: WorkflowSection
    prompt: PromptSection

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("unsupported config version; expected 1")
        return value


class ConfigOverrides(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_name: str | None = None
    cursor_command: str | None = None
    cursor_model: str | None = None
    cursor_output_format: str | None = None
    codex_command: str | None = None
    codex_review_model: str | None = None
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
            "review_model": "o4-mini",
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
        layers.append(global_config.model_dump(by_alias=True))
    layers.append(source_repo_config.model_dump(by_alias=True))

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
