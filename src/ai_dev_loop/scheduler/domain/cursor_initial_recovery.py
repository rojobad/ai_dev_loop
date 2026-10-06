"""Versioned private authority for initial standalone Cursor recovery."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    Sha256Hex,
    UuidSessionId,
    canonical_json_sha256,
)

CURSOR_INITIAL_RECOVERY_RECORD_SCHEMA_VERSION = 1
CURSOR_INITIAL_RECOVERY_INTENT_SCHEMA_VERSION = 1
RECOVERY_NOTE = (
    "Recovery note: A previous attempt of this turn was interrupted and may have left "
    "partial work in this repository. Inspect the existing changes and continue according "
    "to the original plan and instructions. Preserve the work already present; determine "
    "what is complete and what still needs implementation or verification. Do not reset or "
    "discard existing work merely to start over."
)
EFFECTIVE_PROMPT_REL = "prompts/cursor-initial-recovery/effective.txt"
RECORD_REL = "cursor/initial-recovery/record-v1.json"


def recovery_key_for(source_run_id: str, failed_attempt_id: str) -> str:
    return canonical_json_sha256(
        {
            "schema": "cursor-initial-recovery-key-v1",
            "source_run_id": source_run_id,
            "failed_attempt_id": failed_attempt_id,
        }
    )


def effective_prompt_bytes(base: bytes) -> bytes:
    """Append the exact operational note once to the failed invocation bytes."""

    if not isinstance(base, bytes):
        raise TypeError("base prompt must be raw bytes")
    return base + b"\n\n" + RECOVERY_NOTE.encode("utf-8") + b"\n"


def _sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _strict_json_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a JSON integer")
    return value


class CursorInitialRecoveryRecordV1(DomainModel):
    """Canonical private record hashed independently of the prompt."""

    schema_version: int
    turn_kind: Literal["initial"]
    reviewer_created: Literal[False]
    reviews_completed: Literal[0]
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    chat_id: UuidSessionId
    iteration: int
    admitted_artifact_path: str = Field(min_length=1)
    admitted_artifact_sha256: Sha256Hex
    plan_sha256: Sha256Hex
    submitted_prompt_sha256: Sha256Hex
    config_sha256: Sha256Hex
    base_prompt_path: str = Field(min_length=1)
    base_prompt_sha256: Sha256Hex
    effective_prompt_path: str = Field(min_length=1)
    effective_prompt_sha256: Sha256Hex
    parent_source_run_id: str | None
    parent_recovery_key: str | None
    parent_record_sha256: str | None

    @field_validator("schema_version", "iteration", "reviews_completed", mode="before")
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if (
            field_name == "schema_version"
            and number != CURSOR_INITIAL_RECOVERY_RECORD_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be 1")
        if field_name == "iteration" and number < 1:
            raise ValueError("iteration must be >= 1")
        if field_name == "reviews_completed" and number != 0:
            raise ValueError("reviews_completed must be 0")
        return number

    @field_validator("reviewer_created", mode="before")
    @classmethod
    def reviewer_not_created(cls, value: object) -> object:
        if value is not False:
            raise ValueError("reviewer_created must be false")
        return value

    @field_validator("parent_source_run_id", mode="before")
    @classmethod
    def parent_source_or_null(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("parent_source_run_id must be a non-empty string or null")
        return value

    @field_validator("parent_record_sha256", "parent_recovery_key", mode="before")
    @classmethod
    def parent_sha_or_null(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError("parent evidence must be a non-empty string or null")
        return value

    @model_validator(mode="after")
    def parent_fields_agree(self) -> CursorInitialRecoveryRecordV1:
        parent_values = (
            self.parent_source_run_id,
            self.parent_recovery_key,
            self.parent_record_sha256,
        )
        if any(value is None for value in parent_values) and any(
            value is not None for value in parent_values
        ):
            raise ValueError("parent recovery evidence must be entirely null or entirely present")
        if self.parent_record_sha256 is not None and not _sha256_hex(self.parent_record_sha256):
            raise ValueError("parent_record_sha256 must be a sha256 hex digest")
        if self.parent_recovery_key is not None and not _sha256_hex(self.parent_recovery_key):
            raise ValueError("parent_recovery_key must be a sha256 hex digest")
        return self

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class CursorInitialRecoveryPublicationIntentV1(DomainModel):
    """Durable publication intent stored before private artifacts exist."""

    schema_version: int
    recovery_key: Sha256Hex
    source_run_id: str = Field(min_length=1)
    successor_run_id: str = Field(min_length=1)
    failed_attempt_id: str = Field(min_length=1)
    dispatch_id: str = Field(min_length=1)
    status: Literal["pending", "ready", "cancelled"]
    parent_recovery_key: str | None
    record_sha256: str | None
    base_prompt_path: str = Field(min_length=1)
    base_prompt_sha256: Sha256Hex
    chat_id: UuidSessionId
    chat_owner_run_id: str = Field(min_length=1)
    chat_artifact_path: str = Field(min_length=1)
    chat_artifact_sha256: Sha256Hex
    iteration: int
    admitted_artifact_path: str = Field(min_length=1)
    admitted_artifact_sha256: Sha256Hex
    plan_sha256: Sha256Hex
    submitted_prompt_sha256: Sha256Hex
    config_sha256: Sha256Hex

    @field_validator("schema_version", "iteration", mode="before")
    @classmethod
    def strict_ints(cls, value: object, info: object) -> object:
        field_name = str(getattr(info, "field_name", "field"))
        number = _strict_json_int(value, field_name=field_name)
        if (
            field_name == "schema_version"
            and number != CURSOR_INITIAL_RECOVERY_INTENT_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be 1")
        if field_name == "iteration" and number < 1:
            raise ValueError("iteration must be >= 1")
        return number

    @field_validator("parent_recovery_key", "record_sha256", mode="before")
    @classmethod
    def optional_digest(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str) or not _sha256_hex(value):
            raise ValueError("optional digest must be a sha256 hex digest or null")
        return value

    @model_validator(mode="after")
    def ready_requires_record_digest(self) -> CursorInitialRecoveryPublicationIntentV1:
        if self.status == "ready" and not self.record_sha256:
            raise ValueError("ready publication requires record_sha256")
        if self.status != "ready" and self.record_sha256 is not None:
            raise ValueError("record_sha256 is null until publication is ready")
        return self

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return text.encode("utf-8")

    def canonical_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))
