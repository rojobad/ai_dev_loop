"""Scheduler sequence definition domain models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, TypeAdapter, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, GitObjectSha, NonEmptyStr, Sha256Hex
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
ABORT_PENDING_SEQUENCE_STATE_KIND = "abort_pending"
BLOCKED_SEQUENCE_STATE_KIND = "blocked"
ABORTED_SEQUENCE_STATE_KIND = "aborted"
AWAITING_FINALIZATION_SEQUENCE_STATE_KIND = "awaiting_finalization"

SEQUENCE_TERMINAL_STATE_KINDS = frozenset(
    {
        BLOCKED_SEQUENCE_STATE_KIND,
        ABORTED_SEQUENCE_STATE_KIND,
        AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    }
)

SEQUENCE_BLOCKING_RUN_TERMINAL_KINDS = frozenset(
    {
        "blocked",
        "aborted",
    }
)

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
    residual_risk_ordinals: tuple[int, ...] = ()

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


class AbortPendingSequenceState(DomainModel):
    """Sequence with a durable abort request; active run abort may still be in flight."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    started_at: NonEmptyStr
    abort_requested_at: NonEmptyStr
    abort_reason: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition
    current_ordinal: int
    current_run_id: NonEmptyStr
    materialized_entries: tuple[MaterializedSequenceEntry, ...] = Field(min_length=1)
    residual_risk_ordinals: tuple[int, ...] = ()
    cancelled_ordinals: tuple[int, ...] = ()

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


class BlockedSequenceState(DomainModel):
    """Sequence stopped after a non-success terminal outcome on the current materialized run."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    started_at: NonEmptyStr
    blocked_at: NonEmptyStr
    block_reason_kind: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition
    current_ordinal: int
    current_run_id: NonEmptyStr
    materialized_entries: tuple[MaterializedSequenceEntry, ...] = Field(min_length=1)
    residual_risk_ordinals: tuple[int, ...] = ()

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


class AbortedSequenceState(DomainModel):
    """Sequence aborted by explicit operator request."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    started_at: NonEmptyStr | None = None
    aborted_at: NonEmptyStr
    abort_reason: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition
    current_ordinal: int | None = None
    current_run_id: NonEmptyStr | None = None
    materialized_entries: tuple[MaterializedSequenceEntry, ...] = ()
    residual_risk_ordinals: tuple[int, ...] = ()
    cancelled_ordinals: tuple[int, ...] = ()
    preserved_current_leaf_terminal: Literal["blocked"] | None = None
    preserved_current_leaf_resolved_at: NonEmptyStr | None = None

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value

    @model_validator(mode="after")
    def preserved_leaf_fields_consistent(self) -> AbortedSequenceState:
        if self.preserved_current_leaf_terminal is None:
            if self.preserved_current_leaf_resolved_at is not None:
                raise ValueError(
                    "preserved_current_leaf_resolved_at requires preserved_current_leaf_terminal"
                )
            return self
        if self.preserved_current_leaf_resolved_at is None:
            raise ValueError(
                "preserved_current_leaf_terminal requires preserved_current_leaf_resolved_at"
            )
        if self.current_ordinal is None or self.current_run_id is None:
            raise ValueError("preserved blocked leaf abort requires current run pointers")
        return self


class SequencePhaseReportEntry(DomainModel):
    ordinal: int
    phase_name: NonEmptyStr
    run_id: NonEmptyStr
    run_id_prefix: NonEmptyStr
    accepted_run_id_prefix: NonEmptyStr | None = None
    accepted_attempt_kind: NonEmptyStr | None = None
    attempt_count: int = 1
    attempt_kind_labels: tuple[str, ...] = ()
    accepted_outcome: Literal["completed", "completed_with_residual_risk"] | None = None
    residual_risk: bool = False
    review_result_sha256: Sha256Hex | None = None
    review_result_sha256_prefix: NonEmptyStr | None = None
    checkpoint_commit_sha256: GitObjectSha | None = None
    checkpoint_commit_sha256_prefix: NonEmptyStr | None = None
    checkpoint_parent_sha256: GitObjectSha | None = None
    checkpoint_parent_sha256_prefix: NonEmptyStr | None = None
    checkpoint_tree_sha256: GitObjectSha | None = None
    checkpoint_tree_sha256_prefix: NonEmptyStr | None = None
    checkpoint_intent_sha256: Sha256Hex | None = None
    checkpoint_trusted_tree_sha256: Sha256Hex | None = None


class SequenceCompletionReport(DomainModel):
    """Safe aggregate completion report for awaiting_finalization sequences."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    sequence_name: NonEmptyStr
    state_kind: Literal["awaiting_finalization"] = "awaiting_finalization"
    base_head_sha256: GitObjectSha | None = None
    base_head_sha256_prefix: NonEmptyStr
    finalized_at: NonEmptyStr
    final_run_id: NonEmptyStr
    final_run_id_prefix: NonEmptyStr
    final_outcome: Literal["completed", "completed_with_residual_risk"]
    final_staged_patch_sha256: Sha256Hex | None = None
    final_staged_patch_sha256_prefix: NonEmptyStr | None = None
    residual_risk_ordinals: tuple[int, ...] = ()
    residual_risk_phase_names: tuple[str, ...] = ()
    phases: tuple[SequencePhaseReportEntry, ...]
    manual_actions_remaining: tuple[str, ...] = (
        "final_commit",
        "push",
        "pr_review",
        "merge",
    )


class AwaitingFinalizationSequenceState(DomainModel):
    """Sequence whose final phase completed; staged changes await operator finalization."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    version: int
    prepared_at: NonEmptyStr
    updated_at: NonEmptyStr
    started_at: NonEmptyStr
    finalized_at: NonEmptyStr
    idempotency_key: Sha256Hex
    definition: PreparedSequenceDefinition
    final_run_id: NonEmptyStr
    final_outcome: Literal["completed", "completed_with_residual_risk"]
    completion_report_sha256: Sha256Hex | None = None
    materialized_entries: tuple[MaterializedSequenceEntry, ...] = Field(min_length=1)
    residual_risk_ordinals: tuple[int, ...] = ()

    @field_validator("version")
    @classmethod
    def version_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("version must be >= 1")
        return value

    @model_validator(mode="after")
    def validate_final_projection(self) -> AwaitingFinalizationSequenceState:
        total = len(self.definition.entries)
        final_entry = self.definition.entries[-1]
        if final_entry.ordinal != total:
            raise ValueError("final entry ordinal must equal entry count")
        materialized = next(
            (entry for entry in self.materialized_entries if entry.ordinal == total),
            None,
        )
        if materialized is None or materialized.run_id != self.final_run_id:
            raise ValueError("final_run_id must match materialized final entry")
        return self


def future_entry_ordinals(
    definition: PreparedSequenceDefinition, current_ordinal: int
) -> tuple[int, ...]:
    total = len(definition.entries)
    if current_ordinal >= total:
        return ()
    return tuple(range(current_ordinal + 1, total + 1))


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
ABORT_PENDING_SEQUENCE_STATE_ADAPTER: TypeAdapter[AbortPendingSequenceState] = TypeAdapter(
    AbortPendingSequenceState
)
BLOCKED_SEQUENCE_STATE_ADAPTER: TypeAdapter[BlockedSequenceState] = TypeAdapter(
    BlockedSequenceState
)
ABORTED_SEQUENCE_STATE_ADAPTER: TypeAdapter[AbortedSequenceState] = TypeAdapter(
    AbortedSequenceState
)
AWAITING_FINALIZATION_SEQUENCE_STATE_ADAPTER: TypeAdapter[AwaitingFinalizationSequenceState] = (
    TypeAdapter(AwaitingFinalizationSequenceState)
)
SEQUENCE_COMPLETION_REPORT_ADAPTER: TypeAdapter[SequenceCompletionReport] = TypeAdapter(
    SequenceCompletionReport
)
FROZEN_SEQUENCE_ENTRY_ADAPTER: TypeAdapter[FrozenSequenceEntry] = TypeAdapter(FrozenSequenceEntry)
SEQUENCE_MANIFEST_ADAPTER: TypeAdapter[SequenceManifest] = TypeAdapter(SequenceManifest)
