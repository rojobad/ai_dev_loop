"""Cross-field and bounds validation for Integration API wire types."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from ai_dev_loop.integration_api.errors import IntegrationApiError
from ai_dev_loop.integration_api.models import (
    ArtifactChunk,
    CollectionPageMeta,
    IntegrationEnvelope,
    PublicWireModel,
)

COLLECTION_DEFAULT_LIMIT = 100
COLLECTION_HARD_MAX_LIMIT = 500
ARTIFACT_DEFAULT_LIMIT = 65536
ARTIFACT_HARD_MAX_LIMIT = 262144

RFC3339_UTC_PATTERN = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?Z$")


def parse_observed_at_rfc3339_utc(value: str) -> datetime:
    match = RFC3339_UTC_PATTERN.fullmatch(value)
    if match is None:
        raise IntegrationApiError.invalid_argument("observedAt must be a UTC RFC3339 timestamp.")
    year, month, day, hour, minute, second, fraction = match.groups()
    month_i = int(month)
    day_i = int(day)
    hour_i = int(hour)
    minute_i = int(minute)
    second_i = int(second)
    if not 1 <= month_i <= 12:
        raise IntegrationApiError.invalid_argument("observedAt must be a UTC RFC3339 timestamp.")
    if not 0 <= hour_i <= 23 or not 0 <= minute_i <= 59 or not 0 <= second_i <= 59:
        raise IntegrationApiError.invalid_argument("observedAt must be a UTC RFC3339 timestamp.")
    microsecond = 0
    if fraction:
        microsecond = int(fraction[1:].ljust(6, "0")[:6])
    try:
        parsed = datetime(
            int(year),
            month_i,
            day_i,
            hour_i,
            minute_i,
            second_i,
            microsecond,
            tzinfo=UTC,
        )
    except ValueError as exc:
        raise IntegrationApiError.invalid_argument(
            "observedAt must be a UTC RFC3339 timestamp."
        ) from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S") != f"{year}-{month}-{day}T{hour}:{minute}:{second}":
        raise IntegrationApiError.invalid_argument("observedAt must be a UTC RFC3339 timestamp.")
    return parsed


def validate_collection_bounds(offset: int, limit: int) -> None:
    if offset < 0:
        raise IntegrationApiError.invalid_argument("offset must be a nonnegative integer.")
    if limit < 1 or limit > COLLECTION_HARD_MAX_LIMIT:
        raise IntegrationApiError.invalid_argument(
            f"limit must be between 1 and {COLLECTION_HARD_MAX_LIMIT}.",
        )


def validate_artifact_read_bounds(byte_offset: int, limit: int) -> None:
    if byte_offset < 0:
        raise IntegrationApiError.invalid_argument("byteOffset must be a nonnegative integer.")
    if limit < 1 or limit > ARTIFACT_HARD_MAX_LIMIT:
        raise IntegrationApiError.invalid_argument(
            f"limit must be between 1 and {ARTIFACT_HARD_MAX_LIMIT}.",
        )


def validate_envelope_wire_payload(payload: dict[str, Any]) -> IntegrationEnvelope:
    normalized = dict(payload)
    observed_at = normalized.get("observedAt")
    if not isinstance(observed_at, str):
        raise IntegrationApiError.invalid_argument("observedAt must be a UTC RFC3339 timestamp.")
    normalized["observedAt"] = parse_observed_at_rfc3339_utc(observed_at)
    try:
        envelope = IntegrationEnvelope.model_validate(normalized)
    except ValidationError as exc:
        raise IntegrationApiError.invalid_argument(
            "Integration envelope failed validation."
        ) from exc
    if envelope.ok:
        if envelope.error is not None:
            raise IntegrationApiError.invalid_argument(
                "Successful envelopes must set error to null."
            )
        if envelope.data is None:
            raise IntegrationApiError.invalid_argument("Successful envelopes must include data.")
    else:
        if envelope.data is not None:
            raise IntegrationApiError.invalid_argument("Failed envelopes must set data to null.")
        if envelope.error is None:
            raise IntegrationApiError.invalid_argument("Failed envelopes must include error.")
    return envelope


def validate_artifact_chunk_wire(payload: dict[str, Any]) -> ArtifactChunk:
    try:
        chunk = ArtifactChunk.model_validate(payload)
    except ValidationError as exc:
        raise IntegrationApiError.invalid_argument("Artifact chunk failed validation.") from exc
    if chunk.encoding != "base64":
        raise IntegrationApiError.invalid_argument("Artifact chunk encoding must be base64.")
    if chunk.available:
        if chunk.reason is not None:
            raise IntegrationApiError.invalid_argument(
                "Available artifact chunks must set reason to null."
            )
        if chunk.available_bytes is None or chunk.available_bytes < 0:
            raise IntegrationApiError.invalid_argument(
                "Available artifact chunks require nonnegative availableBytes.",
            )
        if chunk.returned_bytes < 0 or chunk.returned_bytes > chunk.available_bytes:
            raise IntegrationApiError.invalid_argument(
                "returnedBytes must be between 0 and availableBytes for available artifacts.",
            )
        if chunk.has_more and chunk.next_offset is None:
            raise IntegrationApiError.invalid_argument(
                "Available artifact chunks with hasMore require nextOffset.",
            )
    else:
        if chunk.returned_bytes != 0:
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require returnedBytes 0."
            )
        if chunk.content_base64 != "":
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require empty contentBase64.",
            )
        if chunk.next_offset is not None:
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require nextOffset null."
            )
        if chunk.has_more:
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require hasMore false."
            )
        if chunk.reason is None or not chunk.reason.strip():
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require a stable reason."
            )
        if chunk.available_bytes is not None or chunk.sha256 is not None:
            raise IntegrationApiError.invalid_argument(
                "Unavailable artifact chunks require availableBytes and sha256 to be null.",
            )
    return chunk


def validate_collection_page_meta_wire(payload: dict[str, Any]) -> CollectionPageMeta:
    try:
        page = CollectionPageMeta.model_validate(payload)
    except ValidationError as exc:
        raise IntegrationApiError.invalid_argument(
            "Collection page metadata failed validation."
        ) from exc
    validate_collection_bounds(page.offset, page.limit)
    if not page.has_more and page.next_offset is not None:
        raise IntegrationApiError.invalid_argument("nextOffset must be null when hasMore is false.")
    if page.has_more and page.next_offset is None:
        raise IntegrationApiError.invalid_argument("nextOffset is required when hasMore is true.")
    return page


def validate_public_wire_model(
    model: type[PublicWireModel], payload: dict[str, Any]
) -> PublicWireModel:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise IntegrationApiError.invalid_argument(f"{model.__name__} failed validation.") from exc
