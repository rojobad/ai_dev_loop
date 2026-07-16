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


ALLOWED_STATUS_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PREPARED: frozenset({RunStatus.VALIDATING, RunStatus.ABORTED, RunStatus.FAILED}),
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
        {RunStatus.STAGING, RunStatus.INTERRUPTED, RunStatus.FAILED, RunStatus.ABORTED}
    ),
    RunStatus.STAGING: frozenset({RunStatus.REVIEWING, RunStatus.FAILED, RunStatus.ABORTED}),
    RunStatus.REVIEWING: frozenset(
        {
            RunStatus.WAITING_FOR_CURSOR_FIX,
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
            RunStatus.MAX_ITERATIONS_REACHED,
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
    RunStatus.MAX_ITERATIONS_REACHED: frozenset(),
    RunStatus.INTERRUPTED: frozenset(
        {
            RunStatus.VALIDATING,
            RunStatus.RUNNING_CURSOR,
            RunStatus.STAGING,
            RunStatus.REVIEWING,
            RunStatus.ABORTED,
            RunStatus.FAILED,
        }
    ),
    RunStatus.FAILED: frozenset(),
    RunStatus.ABORTED: frozenset(),
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
    stage_mode: str
    cursor_timeout_minutes: int
    codex_timeout_minutes: int


RECOVERY_RUNTIME_MIGRATIONS = frozenset({"none", "phase9_session_capture"})
RECOVERY_REASON_CODES = frozenset(
    {
        "codex_review_failed",
        "codex_review_result_invalid",
        "codex_review_processing_failed",
        "correction_staging_failed",
        "initial_staging_failed",
        "cursor_usage_limit",
    }
)
RECOVERY_CHECKPOINTS = frozenset({"staging", "reviewing", "process_review", "cursor"})


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
                        "legacy_cursor_output_adopted is not supported for "
                        "initial_staging_failed"
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
