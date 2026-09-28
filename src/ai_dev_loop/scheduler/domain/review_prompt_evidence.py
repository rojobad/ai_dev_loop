"""Typed scheduler Codex review prompt evidence artifact."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

_SHA256_LOWER = re.compile(r"^[0-9a-f]{64}$")


class SchedulerReviewPromptEvidenceV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: StrictInt
    run_id: str
    attempt_id: str
    review_iteration: StrictInt = Field(ge=1)
    prompt_path: str
    prompt_sha256: str
    prompt_size_bytes: StrictInt = Field(ge=1)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value

    @field_validator("run_id", "attempt_id", "prompt_path")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value

    @field_validator("prompt_sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        if not _SHA256_LOWER.match(value):
            raise ValueError("prompt_sha256 must be lowercase hex")
        return value
