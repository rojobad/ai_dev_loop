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
from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    }
)
RECOVERY_CHECKPOINTS = frozenset({"reviewing", "process_review"})


class RecoveryState(BaseModel):
    """Lineage for a successor run created by `ai_dev_loop recover`."""

    model_config = ConfigDict(extra="forbid")

    source_run_id: str = Field(min_length=1)
    source_status: str
    source_iteration: int = Field(ge=1)
    recovered_checkpoint: str
    source_staged_patch_sha256: str
    created_at: datetime
    runtime_migration: str
    reason_code: str

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

    @field_validator("source_staged_patch_sha256")
    @classmethod
    def validate_patch_hash(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("source_staged_patch_sha256 must be a lowercase sha256 hex digest")
        return value


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
