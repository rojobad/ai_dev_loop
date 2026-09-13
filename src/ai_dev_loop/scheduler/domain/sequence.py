"""Scheduler sequence definition domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryTargetBinding,
    WorkflowLimits,
)

SEQUENCE_MANIFEST_SCHEMA_VERSION = 1
PREPARED_SEQUENCE_SCHEMA_VERSION = 1
FROZEN_SEQUENCE_ENTRY_SCHEMA_VERSION = 1
PREPARED_SEQUENCE_STATE_KIND = "prepared"
ACTIVE_SEQUENCE_STATE_KIND = "active"

MIN_SEQUENCE_PHASE_COUNT = 2
MAX_SEQUENCE_PHASE_COUNT = 32
MAX_SEQUENCE_NAME_LENGTH = 256
MAX_PHASE_NAME_LENGTH = 128
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_COMMIT_MESSAGE_BYTES = 4096


class SequenceManifestCursorOverrides(DomainModel):
    command: str | None = None
    model: str | None = None
    output_format: str | None = None


class SequenceManifestCodexOverrides(DomainModel):
    command: str | None = None
    review_model: NonEmptyStr
    review_reasoning_effort: NonEmptyStr
    review_skill: str | None = None


class SequenceManifestWorkflowOverrides(DomainModel):
    max_review_iterations: int | None = None
    cursor_timeout_minutes: int | None = None
    codex_timeout_minutes: int | None = None


class SequenceManifestPhase(DomainModel):
    name: NonEmptyStr
    plan_path: NonEmptyStr
    prompt_source_path: NonEmptyStr
    commit_message: str | None = None
    cursor: SequenceManifestCursorOverrides | None = None
    codex: SequenceManifestCodexOverrides
    workflow: SequenceManifestWorkflowOverrides | None = None

    @field_validator("name")
    @classmethod
    def validate_phase_name(cls, value: str) -> str:
        if len(value) > MAX_PHASE_NAME_LENGTH:
            raise ValueError(f"phase name must be at most {MAX_PHASE_NAME_LENGTH} characters")
        return value


class SequenceManifest(DomainModel):
    schema_version: Literal[1] = 1
    name: NonEmptyStr
    phases: list[SequenceManifestPhase]

    @field_validator("name")
    @classmethod
    def validate_sequence_name(cls, value: str) -> str:
        if len(value) > MAX_SEQUENCE_NAME_LENGTH:
            raise ValueError(f"sequence name must be at most {MAX_SEQUENCE_NAME_LENGTH} characters")
        return value

    @field_validator("phases")
    @classmethod
    def validate_phase_count(
        cls, value: list[SequenceManifestPhase]
    ) -> list[SequenceManifestPhase]:
        count = len(value)
        if count < MIN_SEQUENCE_PHASE_COUNT or count > MAX_SEQUENCE_PHASE_COUNT:
            raise ValueError(
                f"sequence must contain between {MIN_SEQUENCE_PHASE_COUNT} and "
                f"{MAX_SEQUENCE_PHASE_COUNT} phases"
            )
        names = [phase.name for phase in value]
        if len(set(names)) != len(names):
            raise ValueError("phase names must be unique within a sequence manifest")
        return value

    @model_validator(mode="after")
    def validate_commit_messages(self) -> SequenceManifest:
        total = len(self.phases)
        for index, phase in enumerate(self.phases, start=1):
            is_final = index == total
            message = phase.commit_message
            if is_final:
                if message is not None and message.strip():
                    raise ValueError(f"final phase {phase.name!r} must not include commit_message")
            else:
                if message is None or not message.strip():
                    raise ValueError(
                        f"non-final phase {phase.name!r} requires a non-empty commit_message"
                    )
                if len(message.encode("utf-8")) > MAX_COMMIT_MESSAGE_BYTES:
                    raise ValueError(f"commit_message for phase {phase.name!r} exceeds size limit")
        return self


class FrozenSequenceEntry(DomainModel):
    schema_version: Literal[1] = 1
    ordinal: int
    phase_name: NonEmptyStr
    planned_run_id: NonEmptyStr
    commit_message: str | None = None
    plan_prompt: PlanPromptBinding
    effective_config: EffectiveConfigBinding
    codex: FreshCodexReviewerBinding
    cursor: CursorBinding
    workflow: WorkflowLimits

    @field_validator("ordinal")
    @classmethod
    def ordinal_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ordinal must be >= 1")
        return value


class PreparedSequenceDefinition(DomainModel):
    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    name: NonEmptyStr
    project_name: NonEmptyStr
    repository: RepositoryTargetBinding
    manifest_original_artifact_path: NonEmptyStr
    manifest_original_sha256: Sha256Hex
    manifest_resolved_artifact_path: NonEmptyStr
    manifest_resolved_sha256: Sha256Hex
    controller: ControllerBinding
    entries: tuple[FrozenSequenceEntry, ...] = Field(min_length=MIN_SEQUENCE_PHASE_COUNT)

    @model_validator(mode="after")
    def validate_entry_ordinals(self) -> PreparedSequenceDefinition:
        expected = len(self.entries)
        for index, entry in enumerate(self.entries, start=1):
            if entry.ordinal != index:
                raise ValueError("entries must use contiguous one-based ordinals")
            if index == expected and entry.commit_message is not None:
                raise ValueError("final sequence entry must not include commit_message")
            if index < expected and not entry.commit_message:
                raise ValueError("non-final sequence entry requires commit_message")
        return self


class PreparedSequenceState(DomainModel):
    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value


class MaterializedSequenceEntry(DomainModel):
    ordinal: int
    run_id: NonEmptyStr
    entry_hash: Sha256Hex
    materialized_at: NonEmptyStr

    @field_validator("ordinal")
    @classmethod
    def ordinal_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("ordinal must be >= 1")
        return value


class ActiveSequenceState(DomainModel):
    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    started_at: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition
    current_ordinal: int
    current_run_id: NonEmptyStr
    materialized_entries: tuple[MaterializedSequenceEntry, ...] = Field(min_length=1)

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value

    @field_validator("current_ordinal")
    @classmethod
    def current_ordinal_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("current_ordinal must be >= 1")
        return value

    @model_validator(mode="after")
    def validate_materialized_projection(self) -> ActiveSequenceState:
        total = len(self.definition.entries)
        if self.current_ordinal > total:
            raise ValueError("current_ordinal exceeds sequence entry count")
        current_entry = next(
            (entry for entry in self.materialized_entries if entry.ordinal == self.current_ordinal),
            None,
        )
        if current_entry is None:
            raise ValueError("materialized_entries must include current_ordinal")
        if current_entry.run_id != self.current_run_id:
            raise ValueError("current_run_id disagrees with materialized entry")
        ordinals = [entry.ordinal for entry in self.materialized_entries]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("materialized entry ordinals must be unique")
        if any(entry.ordinal > self.current_ordinal for entry in self.materialized_entries):
            raise ValueError("materialized_entries must not include future ordinals")
        return self


def sequence_identity_payload(definition: PreparedSequenceDefinition) -> dict[str, object]:
    payload = definition.model_dump(mode="json")
    payload.pop("sequence_id", None)
    entries = payload.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                entry.pop("planned_run_id", None)
    controller = payload.get("controller")
    if isinstance(controller, dict):
        controller_payload = dict(controller)
        controller_payload.pop("controller_session_id", None)
        payload["controller"] = controller_payload
    return payload


PREPARED_SEQUENCE_STATE_ADAPTER: TypeAdapter[PreparedSequenceState] = TypeAdapter(
    PreparedSequenceState
)
ACTIVE_SEQUENCE_STATE_ADAPTER: TypeAdapter[ActiveSequenceState] = TypeAdapter(ActiveSequenceState)
FROZEN_SEQUENCE_ENTRY_ADAPTER: TypeAdapter[FrozenSequenceEntry] = TypeAdapter(FrozenSequenceEntry)
SEQUENCE_MANIFEST_ADAPTER: TypeAdapter[SequenceManifest] = TypeAdapter(SequenceManifest)
