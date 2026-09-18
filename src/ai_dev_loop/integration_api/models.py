"""Public wire DTOs for the local Integration API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ai_dev_loop.integration_api.version import API_MAJOR, API_MINOR


class PublicWireModel(BaseModel):
    """Public JSON DTO; unknown additive fields are tolerated on ingest."""

    model_config = ConfigDict(extra="allow", frozen=True, populate_by_name=True, strict=True)


class ApiVersion(PublicWireModel):
    major: StrictInt
    minor: StrictInt


class IntegrationErrorBody(PublicWireModel):
    code: str
    message: str


class IntegrationCapabilities(PublicWireModel):
    runs: StrictBool = False
    sequences: StrictBool = False
    review_inspection: StrictBool = Field(False, alias="reviewInspection")
    process_output: StrictBool = Field(False, alias="processOutput")
    codex_capacity: StrictBool = Field(False, alias="codexCapacity")


class IntegrationInfoData(PublicWireModel):
    ai_dev_loop_version: str = Field(alias="aiDevLoopVersion")
    capabilities: IntegrationCapabilities


class IntegrationEnvelope(PublicWireModel):
    api_version: ApiVersion = Field(alias="apiVersion")
    ok: StrictBool
    observed_at: datetime = Field(alias="observedAt")
    data: Any | None
    error: IntegrationErrorBody | None

    @field_validator("observed_at", mode="before")
    @classmethod
    def _validate_observed_at(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("observedAt must be a UTC RFC3339 timestamp.")
            return value.astimezone(UTC)
        if isinstance(value, str):
            from ai_dev_loop.integration_api.errors import IntegrationApiError
            from ai_dev_loop.integration_api.validation import parse_observed_at_rfc3339_utc

            try:
                return parse_observed_at_rfc3339_utc(value)
            except IntegrationApiError as exc:
                raise ValueError(exc.message) from exc
        raise ValueError("observedAt must be a UTC RFC3339 timestamp.")

    @model_validator(mode="after")
    def _check_ok_error_data(self) -> IntegrationEnvelope:
        if self.ok:
            if self.error is not None:
                raise ValueError("error must be null when ok is true")
            if self.data is None:
                raise ValueError("data must be present when ok is true")
        else:
            if self.data is not None:
                raise ValueError("data must be null when ok is false")
            if self.error is None:
                raise ValueError("error must be present when ok is false")
        return self


class ArtifactChunk(PublicWireModel):
    """Shared artifact byte-chunk shape for later integration read commands."""

    available: StrictBool
    reason: str | None
    encoding: str = "base64"
    media_type: str | None = Field(None, alias="mediaType")
    byte_offset: StrictInt = Field(0, alias="byteOffset", ge=0)
    returned_bytes: StrictInt = Field(0, alias="returnedBytes", ge=0)
    next_offset: StrictInt | None = Field(None, alias="nextOffset", ge=0)
    available_bytes: StrictInt | None = Field(None, alias="availableBytes", ge=0)
    has_more: StrictBool = Field(False, alias="hasMore")
    content_base64: str = Field("", alias="contentBase64")
    sha256: str | None = None

    @model_validator(mode="after")
    def _check_available_invariants(self) -> ArtifactChunk:
        if self.encoding != "base64":
            raise ValueError("encoding must be base64")
        if self.available:
            if self.reason is not None:
                raise ValueError("reason must be null when available is true")
            if self.available_bytes is None:
                raise ValueError("availableBytes required when available is true")
            if self.returned_bytes > self.available_bytes:
                raise ValueError("returnedBytes cannot exceed availableBytes")
            if self.has_more and self.next_offset is None:
                raise ValueError("nextOffset required when hasMore is true")
        else:
            if self.returned_bytes != 0:
                raise ValueError("returnedBytes must be 0 when unavailable")
            if self.content_base64:
                raise ValueError("contentBase64 must be empty when unavailable")
            if self.next_offset is not None:
                raise ValueError("nextOffset must be null when unavailable")
            if self.has_more:
                raise ValueError("hasMore must be false when unavailable")
            if not self.reason:
                raise ValueError("reason required when unavailable")
            if self.available_bytes is not None or self.sha256 is not None:
                raise ValueError("availableBytes and sha256 must be null when unavailable")
        return self


class CollectionPageMeta(PublicWireModel):
    """Pagination metadata for later collection commands."""

    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=500)
    next_offset: StrictInt | None = Field(None, alias="nextOffset", ge=0)
    has_more: StrictBool = Field(alias="hasMore")

    @model_validator(mode="after")
    def _check_pagination_offsets(self) -> CollectionPageMeta:
        if not self.has_more and self.next_offset is not None:
            raise ValueError("nextOffset must be null when hasMore is false")
        if self.has_more and self.next_offset is None:
            raise ValueError("nextOffset is required when hasMore is true")
        return self


def current_api_version() -> ApiVersion:
    return ApiVersion(major=API_MAJOR, minor=API_MINOR)
