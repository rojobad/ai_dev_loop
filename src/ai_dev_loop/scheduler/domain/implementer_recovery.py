"""Immutable authority for standalone Codex implementer failure recovery."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from pydantic import Field, field_validator, model_serializer, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, Sha256Hex, UuidSessionId
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    AncestorEdgeV2,
    ArtifactBindingV2,
    BoundReviewerEvidenceV2,
    ReviewerNotCreatedV2,
)

IMPLEMENTER_RECOVERY_RECORD_SCHEMA_VERSION = 1
IMPLEMENTER_RECOVERY_INTENT_SCHEMA_VERSION = 1
IMPLEMENTER_RECOVERY_RECORD_REL = "implementer/failure-recovery/record-v1.json"
IMPLEMENTER_RECOVERY_INTENT_REL = "implementer/failure-recovery/intent-v1.json"
MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES = 16 * 1024 * 1024


def _strict_int(value: object, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a JSON integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value


class CodexImplementerIdentityV1(DomainModel):
    """Authenticated implementer identity plus its frozen launch settings."""

    session_id: UuidSessionId
    binding: ArtifactBindingV2
    bootstrap_attempt_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: str = Field(min_length=1)
    timeout_seconds: int
    sandbox: Literal["workspace-write"]
    approval_policy: Literal["never"]

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def timeout_is_positive_int(cls, value: object) -> object:
        return _strict_int(value, field_name="timeout_seconds", minimum=1)


class CodexImplementerRecoveryRecordV1(DomainModel):
    """Complete immutable authority needed to resume one exact pending turn."""

    schema_version: int
    recovery_key: Sha256Hex
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    failed_dispatch_id: str = Field(min_length=1)
    turn_kind: Literal["initial", "correction"]
    iteration: int
    invocation_evidence_sha256: Sha256Hex
    completion_envelope_sha256: Sha256Hex
    invocation_evidence: ArtifactBindingV2
    completion_envelope: ArtifactBindingV2
    implementer: CodexImplementerIdentityV1
    reviewer: BoundReviewerEvidenceV2 | ReviewerNotCreatedV2
    reviews_completed: int
    submitted_max_review_iterations: int
    effective_review_ceiling: int
    timeout_automatic_retries_consumed: int = Field(default=0, ge=0, le=3)
    admitted: ArtifactBindingV2
    plan: ArtifactBindingV2
    submitted_prompt: ArtifactBindingV2
    effective_config: ArtifactBindingV2
    source_config: ArtifactBindingV2
    baseline_status: ArtifactBindingV2 | None = None
    partial_work_status: ArtifactBindingV2
    partial_work_fingerprint_sha256: Sha256Hex
    exact_prompt: ArtifactBindingV2
    raw_fix: ArtifactBindingV2 | None = None
    staged_patch: ArtifactBindingV2 | None = None
    review_result: ArtifactBindingV2 | None = None
    parent_source_run_id: str | None = None
    parent_recovery_key: Sha256Hex | None = None
    parent_record_sha256: Sha256Hex | None = None
    ancestors: list[AncestorEdgeV2] = Field(min_length=1, max_length=128)

    @field_validator(
        "schema_version",
        "iteration",
        "reviews_completed",
        "submitted_max_review_iterations",
        "effective_review_ceiling",
        "timeout_automatic_retries_consumed",
        mode="before",
    )
    @classmethod
    def strict_numbers(cls, value: object, info: object) -> object:
        name = str(getattr(info, "field_name", "field"))
        minimum = 0 if name in {"reviews_completed", "timeout_automatic_retries_consumed"} else 1
        number = _strict_int(value, field_name=name, minimum=minimum)
        if name == "schema_version" and number != IMPLEMENTER_RECOVERY_RECORD_SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        return number

    @model_serializer(mode="wrap")
    def omit_historical_zero_timeout_consumption(
        self,
        handler: Callable[[CodexImplementerRecoveryRecordV1], dict[str, object]],
    ) -> dict[str, object]:
        data = handler(self)
        if self.timeout_automatic_retries_consumed == 0:
            data.pop("timeout_automatic_retries_consumed", None)
        return data

    @model_validator(mode="after")
    def evidence_matches_turn(self) -> CodexImplementerRecoveryRecordV1:
        parent = (
            self.parent_source_run_id,
            self.parent_recovery_key,
            self.parent_record_sha256,
        )
        if any(value is None for value in parent) and any(value is not None for value in parent):
            raise ValueError("parent recovery authority must be complete or absent")
        if self.source_run_id == self.successor_run_id:
            raise ValueError("recovery successor must differ from its source")
        if self.effective_review_ceiling < self.submitted_max_review_iterations:
            raise ValueError("effective review ceiling cannot shrink")
        if self.reviews_completed >= self.effective_review_ceiling:
            raise ValueError("recovery requires remaining review budget")
        first = self.ancestors[0]
        if (
            first.relation != "implementer_recovery"
            or first.source_run_id != self.source_run_id
            or first.successor_run_id != self.successor_run_id
            or first.recovery_key != self.recovery_key
        ):
            raise ValueError("first ancestry edge must bind this recovery relation")
        visited = {self.successor_run_id}
        for index, edge in enumerate(self.ancestors):
            if edge.source_run_id in visited:
                raise ValueError("recovery ancestry contains a cycle")
            visited.add(edge.source_run_id)
            if index + 1 < len(self.ancestors):
                older = self.ancestors[index + 1]
                if edge.source_run_id != older.successor_run_id:
                    raise ValueError("recovery ancestry is not a contiguous chain")
        if self.parent_recovery_key is not None:
            if len(self.ancestors) < 2:
                raise ValueError("parent recovery edge is missing from ancestry")
            immediate_parent = self.ancestors[1]
            if (
                immediate_parent.relation != "implementer_recovery"
                or immediate_parent.source_run_id != self.parent_source_run_id
                or immediate_parent.successor_run_id != self.source_run_id
                or immediate_parent.recovery_key != self.parent_recovery_key
            ):
                raise ValueError("parent recovery authority disagrees with ancestry")
        source_owned = (
            self.admitted,
            self.invocation_evidence,
            self.completion_envelope,
            self.plan,
            self.submitted_prompt,
            self.effective_config,
            self.source_config,
            self.baseline_status,
            self.partial_work_status,
            self.exact_prompt,
            self.implementer.binding,
        )
        if any(
            binding is not None and binding.owner_run_id != self.source_run_id
            for binding in source_owned
        ):
            raise ValueError("current recovery authority must be owned by its source run")
        correction = (self.raw_fix, self.staged_patch, self.review_result)
        if self.turn_kind == "initial":
            if self.iteration != 1 or self.reviews_completed != 0:
                raise ValueError("initial recovery must precede all reviews")
            if not isinstance(self.reviewer, ReviewerNotCreatedV2):
                raise ValueError("initial recovery must not create reviewer B")
            if any(item is not None for item in correction):
                raise ValueError("initial recovery must not carry correction evidence")
        else:
            if self.iteration <= 1 or self.reviews_completed < 1:
                raise ValueError("correction recovery requires completed review history")
            if not isinstance(self.reviewer, BoundReviewerEvidenceV2):
                raise ValueError("correction recovery requires authenticated reviewer B")
            if any(item is None for item in correction):
                raise ValueError("correction recovery requires fix, patch, and review evidence")
            if self.reviewer.session_id == self.implementer.session_id:
                raise ValueError("implementer and reviewer identities must remain distinct")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CodexImplementerRecoveryIntentV1(DomainModel):
    """Immutable pending-publication decision persisted before artifact publication."""

    schema_version: int
    recovery_key: Sha256Hex
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    failed_dispatch_id: str = Field(min_length=1)
    turn_kind: Literal["initial", "correction"]
    iteration: int
    exact_prompt_path: str = Field(min_length=1)
    exact_prompt_sha256: Sha256Hex
    implementer_session_id: UuidSessionId
    record: CodexImplementerRecoveryRecordV1

    @field_validator("schema_version", "iteration", mode="before")
    @classmethod
    def strict_numbers(cls, value: object, info: object) -> object:
        name = str(getattr(info, "field_name", "field"))
        number = _strict_int(value, field_name=name, minimum=1)
        if name == "schema_version" and number != IMPLEMENTER_RECOVERY_INTENT_SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        return number

    @model_validator(mode="after")
    def record_matches_intent(self) -> CodexImplementerRecoveryIntentV1:
        record = self.record
        expected = (
            record.recovery_key,
            record.source_run_id,
            record.successor_run_id,
            record.failed_attempt_id,
            record.failed_dispatch_id,
            record.turn_kind,
            record.iteration,
            record.exact_prompt.relative_path,
            record.exact_prompt.sha256,
            record.implementer.session_id,
        )
        actual = (
            self.recovery_key,
            self.source_run_id,
            self.successor_run_id,
            self.failed_attempt_id,
            self.failed_dispatch_id,
            self.turn_kind,
            self.iteration,
            self.exact_prompt_path,
            self.exact_prompt_sha256,
            self.implementer_session_id,
        )
        if expected != actual:
            raise ValueError("recovery intent disagrees with its immutable record")
        return self

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
