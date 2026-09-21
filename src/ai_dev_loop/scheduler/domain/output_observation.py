"""Typed scheduler Cursor child output observation artifact."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator


class SchedulerOutputStreamObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    stored_bytes: StrictInt = Field(ge=0)
    truncated: StrictBool


class SchedulerOutputObservationV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: StrictInt
    run_id: str
    attempt_id: str
    iteration: StrictInt = Field(ge=1)
    stdout: SchedulerOutputStreamObservationV1
    stderr: SchedulerOutputStreamObservationV1

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must be 1")
        return value

    @field_validator("run_id", "attempt_id")
    @classmethod
    def _validate_non_empty(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("field must be a non-empty string")
        return value
