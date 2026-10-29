"""Versioned provider token-usage observations and aggregate reports."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

USAGE_OBSERVATION_SCHEMA_VERSION = 1
USAGE_NORMALIZATION_VERSION: Literal["provider-usage-v1"] = "provider-usage-v1"

UsageProvider = Literal["cursor", "codex"]
UsageRole = Literal["implementer", "reviewer"]
UsageStage = Literal["initial", "correction", "review"]
UsageAvailability = Literal["known", "partial"]
UsageCoverage = Literal["complete", "partial", "unknown", "not_applicable"]


class _UsageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProviderNativeUsage(_UsageModel):
    """Known provider-native counters; absent optional counters stay unknown."""

    input_tokens: StrictInt | None = Field(None, ge=0)
    output_tokens: StrictInt | None = Field(None, ge=0)
    cache_read_tokens: StrictInt | None = Field(None, ge=0)
    cache_write_tokens: StrictInt | None = Field(None, ge=0)
    total_tokens: StrictInt | None = Field(None, ge=0)
    reasoning_tokens: StrictInt | None = Field(None, ge=0)
    cached_input_tokens: StrictInt | None = Field(None, ge=0)
    cache_write_input_tokens: StrictInt | None = Field(None, ge=0)
    reasoning_output_tokens: StrictInt | None = Field(None, ge=0)


class NormalizedUsage(_UsageModel):
    input_total: StrictInt | None = Field(None, ge=0)
    input_uncached: StrictInt | None = Field(None, ge=0)
    cache_read: StrictInt | None = Field(None, ge=0)
    cache_write: StrictInt | None = Field(None, ge=0)
    output_total: StrictInt | None = Field(None, ge=0)
    reasoning_output: StrictInt | None = Field(None, ge=0)
    total_tokens: StrictInt = Field(ge=0)


class UsageObservationV1(_UsageModel):
    schema_version: Literal[1] = 1
    normalization_version: Literal["provider-usage-v1"] = "provider-usage-v1"
    observation_id: str
    run_id: str
    provider: UsageProvider
    role: UsageRole
    stage: UsageStage
    scheduler_attempt_id: str
    publisher_attempt_id: str
    source_dispatch_id: str
    native_key: str
    source_artifact_path: str
    source_artifact_sha256: str
    source_format: str
    source_event_ordinal: StrictInt = Field(ge=1)
    source_event_offsets: tuple[str, ...] = ()
    capture_complete: StrictBool
    availability: UsageAvailability
    native: ProviderNativeUsage
    normalized: NormalizedUsage

    @model_validator(mode="after")
    def _validate_identity(self) -> UsageObservationV1:
        expected = usage_observation_id(
            provider=self.provider,
            role=self.role,
            scheduler_attempt_id=self.scheduler_attempt_id,
            native_key=self.native_key,
        )
        if self.observation_id != expected:
            raise ValueError("observation_id does not match canonical identity")
        if self.availability == "known" and not self.capture_complete:
            raise ValueError("known observation requires complete capture")
        if self.provider == "cursor" and any(
            not offset or len(offset) > 512 for offset in self.source_event_offsets
        ):
            raise ValueError("Cursor usage event offsets are invalid")
        if len(set(self.source_event_offsets)) != len(self.source_event_offsets):
            raise ValueError("usage event offsets must be unique")
        return self

    def semantic_fingerprint(self) -> str:
        """Identity and counters only; replay evidence may use another publisher."""

        payload = {
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "provider": self.provider,
            "role": self.role,
            "stage": self.stage,
            "scheduler_attempt_id": self.scheduler_attempt_id,
            "source_dispatch_id": self.source_dispatch_id,
            "native_key": self.native_key,
            "native": self.native.model_dump(mode="json"),
            "normalized": self.normalized.model_dump(mode="json"),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class UsageCategoryAggregate(_UsageModel):
    known_total: StrictInt | None = Field(None, ge=0)
    known_observations: StrictInt = Field(ge=0)
    observation_count: StrictInt = Field(ge=0)
    coverage: UsageCoverage


class UsageBreakdown(_UsageModel):
    provider: UsageProvider
    role: UsageRole
    stage: UsageStage
    known_total_tokens: StrictInt | None = Field(None, ge=0)
    inference_attempts: StrictInt = Field(ge=0)
    known_attempts: StrictInt = Field(ge=0)
    partial_attempts: StrictInt = Field(ge=0)
    unknown_attempts: StrictInt = Field(ge=0)
    coverage: UsageCoverage
    categories: dict[str, UsageCategoryAggregate]
    native_categories: dict[str, UsageCategoryAggregate]


class UsageScopeReport(_UsageModel):
    scope: Literal["direct", "recovery_lineage", "sequence"]
    run_ids: tuple[str, ...]
    known_total_tokens: StrictInt | None = Field(None, ge=0)
    inference_attempts: StrictInt = Field(ge=0)
    known_attempts: StrictInt = Field(ge=0)
    partial_attempts: StrictInt = Field(ge=0)
    unknown_attempts: StrictInt = Field(ge=0)
    coverage: UsageCoverage
    breakdowns: tuple[UsageBreakdown, ...]
    diagnostics: tuple[str, ...] = ()


class RunUsageReport(_UsageModel):
    schema_version: Literal[1] = 1
    normalization_version: Literal["provider-usage-v1"] = "provider-usage-v1"
    run_id: str
    direct: UsageScopeReport
    recovery_lineage: UsageScopeReport


class SequenceUsageReport(_UsageModel):
    schema_version: Literal[1] = 1
    normalization_version: Literal["provider-usage-v1"] = "provider-usage-v1"
    sequence_id: str
    sequence: UsageScopeReport


def usage_observation_id(
    *,
    provider: UsageProvider,
    role: UsageRole,
    scheduler_attempt_id: str,
    native_key: str,
) -> str:
    raw = f"{provider}\0{role}\0{scheduler_attempt_id}\0{native_key}".encode()
    return hashlib.sha256(raw).hexdigest()


def usage_observation_rel(observation_id: str) -> str:
    return f"usage/observations/{observation_id[:2]}/{observation_id}.json"


def _native_usage(raw: object) -> ProviderNativeUsage:
    if not isinstance(raw, dict):
        raise ValueError("provider usage must be an object")
    # Provider additions remain available in the immutable raw source artifact,
    # but only reviewed v1 categories acquire aggregation semantics.
    supported = {
        key: value for key, value in raw.items() if key in ProviderNativeUsage.model_fields
    }
    return ProviderNativeUsage.model_validate(supported)


def normalize_cursor_usage(raw: object) -> tuple[ProviderNativeUsage, NormalizedUsage]:
    native = _native_usage(raw)
    required = (
        native.input_tokens,
        native.output_tokens,
        native.cache_read_tokens,
        native.cache_write_tokens,
        native.total_tokens,
    )
    if any(value is None for value in required):
        raise ValueError("Cursor usage is missing a required native counter")
    assert native.input_tokens is not None
    assert native.output_tokens is not None
    assert native.cache_read_tokens is not None
    assert native.cache_write_tokens is not None
    assert native.total_tokens is not None
    expected = (
        native.input_tokens
        + native.output_tokens
        + native.cache_read_tokens
        + native.cache_write_tokens
    )
    if native.total_tokens != expected:
        raise ValueError("Cursor total_tokens contradicts provider categories")
    if native.reasoning_tokens is not None and native.reasoning_tokens > native.output_tokens:
        raise ValueError("Cursor reasoning_tokens exceeds output_tokens")
    return native, NormalizedUsage(
        input_total=native.input_tokens + native.cache_read_tokens + native.cache_write_tokens,
        input_uncached=native.input_tokens,
        cache_read=native.cache_read_tokens,
        cache_write=native.cache_write_tokens,
        output_total=native.output_tokens,
        reasoning_output=native.reasoning_tokens,
        total_tokens=native.total_tokens,
    )


def normalize_codex_usage(raw: object) -> tuple[ProviderNativeUsage, NormalizedUsage]:
    native = _native_usage(raw)
    if native.input_tokens is None or native.output_tokens is None:
        raise ValueError("Codex usage is missing input_tokens or output_tokens")
    cached = native.cached_input_tokens
    if cached is not None and cached > native.input_tokens:
        raise ValueError("Codex cached_input_tokens exceeds input_tokens")
    reasoning = native.reasoning_output_tokens
    if reasoning is not None and reasoning > native.output_tokens:
        raise ValueError("Codex reasoning_output_tokens exceeds output_tokens")
    expected = native.input_tokens + native.output_tokens
    if native.total_tokens is not None and native.total_tokens != expected:
        raise ValueError("Codex total_tokens contradicts input and output")
    return native, NormalizedUsage(
        input_total=native.input_tokens,
        input_uncached=None if cached is None else native.input_tokens - cached,
        cache_read=cached,
        cache_write=native.cache_write_input_tokens,
        output_total=native.output_tokens,
        reasoning_output=reasoning,
        total_tokens=expected,
    )
