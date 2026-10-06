"""Versioned private authority for standalone Cursor correction recovery."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    Sha256Hex,
    UuidSessionId,
    canonical_json_sha256,
)

CURSOR_RECOVERY_RECORD_SCHEMA_VERSION = 2
CURSOR_RECOVERY_INTENT_SCHEMA_VERSION = 2
CURSOR_BUDGET_CARRY_SCHEMA_VERSION = 1
CORRECTION_RECORD_REL = "cursor/correction-recovery/record-v2.json"
CORRECTION_BUDGET_CARRY_REL = "cursor/correction-recovery/budget-carry-v1.json"


def _strict_json_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a JSON integer")
    return value


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _strict_sha_or_null(value: object, *, field_name: str) -> object:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{field_name} must be a sha256 hex digest or null")
    if any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be a sha256 hex digest or null")
    return value


class ArtifactBindingV2(DomainModel):
    """Protected artifact bound to the run that owns the historical bytes."""

    owner_run_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    sha256: Sha256Hex


class BoundReviewerEvidenceV2(DomainModel):
    """Authenticated reviewer B. Session evidence is required, not nullable."""

    form: Literal["bound"]
    session_id: UuidSessionId
    bootstrap_run_id: str = Field(min_length=1)
    bootstrap_attempt_id: str = Field(min_length=1)
    bootstrap_events_path: str = Field(min_length=1)
    bootstrap_events_sha256: Sha256Hex
    binding_run_id: str = Field(min_length=1)
    binding_artifact_path: str = Field(min_length=1)
    binding_artifact_sha256: Sha256Hex


class ReviewerNotCreatedV2(DomainModel):
    """Initial recovery before reviewer B exists."""

    form: Literal["not_created"]


ReviewerFormV2 = Annotated[
    BoundReviewerEvidenceV2 | ReviewerNotCreatedV2,
    Field(discriminator="form"),
]


class AncestorEdgeV2(DomainModel):
    """One authenticated recovery edge. The chain is explicit and acyclic."""

    relation: Literal["cursor_recovery", "review_recovery"]
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    recovery_key: str = Field(min_length=1)

    @model_validator(mode="after")
    def recovery_key_matches_relation(self) -> AncestorEdgeV2:
        if self.relation == "cursor_recovery":
            if not _is_sha256(self.recovery_key):
                raise ValueError("cursor recovery key must be a sha256 digest")
            return self
        kind, separator, attempt_id = self.recovery_key.partition(":")
        if (
            not separator
            or not kind
            or not attempt_id
            or ":" in attempt_id
            or any(character.isspace() for character in self.recovery_key)
        ):
            raise ValueError("review recovery key must be block_reason_kind:failed_attempt_id")
        return self


class ExtensionEvidenceV1(DomainModel):
    """Original extension event. The owner run id is not rewritten onto the successor."""

    owner_run_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    sequence: int
    payload_sha256: Sha256Hex
    previous_effective_total: int
    new_effective_total: int

    @field_validator("sequence", "previous_effective_total", "new_effective_total", mode="before")
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if number < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return number


class BudgetCarryV1(DomainModel):
    """Inherited ceiling and completed count. Submitted configuration stays unchanged."""

    schema_version: int
    successor_run_id: str = Field(min_length=1)
    source_run_id: str = Field(min_length=1)
    recovery_key: Sha256Hex
    submitted_max_review_iterations: int
    inherited_effective_ceiling: int
    inherited_reviews_completed: int
    authority: Literal["extension_events", "parent_carry"]
    extension_events: list[ExtensionEvidenceV1]
    parent_carry_owner_run_id: str | None
    parent_carry_path: str | None
    parent_carry_sha256: str | None

    @field_validator(
        "schema_version",
        "submitted_max_review_iterations",
        "inherited_effective_ceiling",
        "inherited_reviews_completed",
        mode="before",
    )
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if field_name == "schema_version" and number != CURSOR_BUDGET_CARRY_SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        if field_name == "inherited_reviews_completed" and number < 0:
            raise ValueError("inherited_reviews_completed must be >= 0")
        if field_name != "inherited_reviews_completed" and field_name != "schema_version" and number < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return number

    @field_validator("parent_carry_sha256", mode="before")
    @classmethod
    def parent_sha(cls, value: object) -> object:
        return _strict_sha_or_null(value, field_name="parent_carry_sha256")

    @field_validator("parent_carry_owner_run_id", "parent_carry_path", mode="before")
    @classmethod
    def parent_text(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field_name} must be a non-empty string or null")
        return value

    @model_validator(mode="after")
    def authority_fields_agree(self) -> BudgetCarryV1:
        parent_values = (
            self.parent_carry_owner_run_id,
            self.parent_carry_path,
            self.parent_carry_sha256,
        )
        if self.authority == "extension_events":
            if any(value is not None for value in parent_values):
                raise ValueError("extension-event carry has no parent carry reference")
        elif any(value is None for value in parent_values):
            raise ValueError("parent carry authority requires owner, path, and digest")
        if self.inherited_effective_ceiling < self.submitted_max_review_iterations:
            raise ValueError("inherited ceiling cannot be below the submitted maximum")
        if self.inherited_effective_ceiling <= self.inherited_reviews_completed:
            raise ValueError("inherited ceiling must exceed the inherited completed count")
        return self

    def canonical_bytes(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class CursorRecoveryRecordV2(DomainModel):
    """Canonical v2 recovery record. v1 initial records stay on their original model."""

    schema_version: int
    turn_kind: Literal["initial", "correction"]
    reviewer: ReviewerFormV2
    reviews_completed: int
    submitted_max_review_iterations: int
    effective_review_ceiling: int
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    chat_id: UuidSessionId
    chat_owner_run_id: str = Field(min_length=1)
    iteration: int
    admitted: ArtifactBindingV2
    plan_sha256: Sha256Hex
    submitted_prompt_sha256: Sha256Hex
    config_sha256: Sha256Hex
    base_prompt: ArtifactBindingV2
    effective_prompt_path: str = Field(min_length=1)
    effective_prompt_sha256: Sha256Hex
    raw_fix: ArtifactBindingV2 | None
    staged_patch: ArtifactBindingV2 | None
    review_result: ArtifactBindingV2 | None
    parent_source_run_id: str | None
    parent_recovery_key: str | None
    parent_record_sha256: str | None
    budget_carry_path: str = Field(min_length=1)
    budget_carry_sha256: Sha256Hex
    ancestors: list[AncestorEdgeV2]

    @field_validator(
        "schema_version",
        "reviews_completed",
        "submitted_max_review_iterations",
        "effective_review_ceiling",
        "iteration",
        mode="before",
    )
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if field_name == "schema_version" and number != CURSOR_RECOVERY_RECORD_SCHEMA_VERSION:
            raise ValueError("schema_version must be 2")
        if field_name == "reviews_completed" and number < 0:
            raise ValueError("reviews_completed must be >= 0")
        if field_name == "iteration" and number < 1:
            raise ValueError("iteration must be >= 1")
        if field_name in {"submitted_max_review_iterations", "effective_review_ceiling"} and number < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return number

    @field_validator("parent_record_sha256", "parent_recovery_key", mode="before")
    @classmethod
    def parent_sha(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        return _strict_sha_or_null(value, field_name=field_name)

    @field_validator("parent_source_run_id", mode="before")
    @classmethod
    def parent_source(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("parent_source_run_id must be a non-empty string or null")
        return value

    @model_validator(mode="after")
    def turn_and_parent_agree(self) -> CursorRecoveryRecordV2:
        parent_values = (
            self.parent_source_run_id,
            self.parent_recovery_key,
            self.parent_record_sha256,
        )
        if any(value is None for value in parent_values) and any(
            value is not None for value in parent_values
        ):
            raise ValueError("parent recovery evidence must be entirely null or entirely present")
        if self.effective_review_ceiling < self.submitted_max_review_iterations:
            raise ValueError("effective ceiling cannot be below the submitted maximum")
        if self.effective_review_ceiling <= self.reviews_completed:
            raise ValueError("effective ceiling must exceed completed reviews")
        if self.turn_kind == "correction":
            if not isinstance(self.reviewer, BoundReviewerEvidenceV2):
                raise ValueError("correction recovery requires bound reviewer evidence")
            if self.raw_fix is None or self.staged_patch is None or self.review_result is None:
                raise ValueError(
                    "correction recovery requires fix, staged patch, and review result bindings"
                )
            if self.reviews_completed < 1:
                raise ValueError("correction recovery requires a completed review")
        else:
            if not isinstance(self.reviewer, ReviewerNotCreatedV2):
                raise ValueError("initial recovery reviewer form is not_created")
            if self.raw_fix is not None or self.staged_patch is not None or self.review_result is not None:
                raise ValueError("initial recovery does not carry correction evidence")
            if self.reviews_completed != 0:
                raise ValueError("initial recovery reviews_completed must be 0")
        return self

    def canonical_bytes(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class CursorRecoveryPublicationIntentV2(DomainModel):
    """Durable correction publication intent stored before the v2 record exists."""

    schema_version: int
    turn_kind: Literal["correction"]
    recovery_key: Sha256Hex
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    status: Literal["pending", "ready", "cancelled"]
    parent_recovery_key: str | None
    record_sha256: str | None
    budget_carry_sha256: str | None
    base_prompt_path: str = Field(min_length=1)
    base_prompt_sha256: Sha256Hex
    base_prompt_owner_run_id: str = Field(min_length=1)
    chat_id: UuidSessionId
    chat_owner_run_id: str = Field(min_length=1)
    chat_artifact_path: str = Field(min_length=1)
    chat_artifact_sha256: Sha256Hex
    iteration: int
    admitted_artifact_path: str = Field(min_length=1)
    admitted_artifact_sha256: Sha256Hex
    admitted_owner_run_id: str = Field(min_length=1)
    plan_sha256: Sha256Hex
    submitted_prompt_sha256: Sha256Hex
    config_sha256: Sha256Hex
    reviews_completed: int
    submitted_max_review_iterations: int
    effective_review_ceiling: int
    reviewer_session_id: UuidSessionId
    bootstrap_run_id: str = Field(min_length=1)
    bootstrap_attempt_id: str = Field(min_length=1)
    bootstrap_events_path: str = Field(min_length=1)
    bootstrap_events_sha256: Sha256Hex
    binding_run_id: str = Field(min_length=1)
    binding_artifact_path: str = Field(min_length=1)
    binding_artifact_sha256: Sha256Hex
    fix_owner_run_id: str = Field(min_length=1)
    fix_prompt_path: str = Field(min_length=1)
    fix_prompt_sha256: Sha256Hex
    staged_owner_run_id: str = Field(min_length=1)
    staged_patch_path: str = Field(min_length=1)
    staged_patch_sha256: Sha256Hex
    review_owner_run_id: str = Field(min_length=1)
    review_result_path: str = Field(min_length=1)
    review_result_sha256: Sha256Hex

    @field_validator(
        "schema_version",
        "iteration",
        "reviews_completed",
        "submitted_max_review_iterations",
        "effective_review_ceiling",
        mode="before",
    )
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if field_name == "schema_version" and number != CURSOR_RECOVERY_INTENT_SCHEMA_VERSION:
            raise ValueError("schema_version must be 2")
        if field_name == "iteration" and number < 1:
            raise ValueError("iteration must be >= 1")
        if field_name == "reviews_completed" and number < 1:
            raise ValueError("reviews_completed must be >= 1")
        if field_name in {"submitted_max_review_iterations", "effective_review_ceiling"} and number < 1:
            raise ValueError(f"{field_name} must be >= 1")
        return number

    @field_validator("parent_recovery_key", "record_sha256", "budget_carry_sha256", mode="before")
    @classmethod
    def optional_digest(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        return _strict_sha_or_null(value, field_name=field_name)

    @model_validator(mode="after")
    def ready_requires_digests(self) -> CursorRecoveryPublicationIntentV2:
        if self.effective_review_ceiling < self.submitted_max_review_iterations:
            raise ValueError("effective ceiling cannot be below the submitted maximum")
        if self.effective_review_ceiling <= self.reviews_completed:
            raise ValueError("effective ceiling must exceed completed reviews")
        if self.status == "ready":
            if not self.record_sha256 or not self.budget_carry_sha256:
                raise ValueError("ready publication requires record and budget-carry digests")
        elif self.record_sha256 is not None or self.budget_carry_sha256 is not None:
            raise ValueError("record and budget-carry digests are null until publication is ready")
        return self

    def canonical_bytes(self) -> bytes:
        text = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))
