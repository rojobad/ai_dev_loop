"""Compact SQLite usage facts. Payloads are typed counters, never source text."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from ai_dev_loop.scheduler.domain.token_usage import (
    USAGE_NORMALIZATION_VERSION,
    NormalizedUsage,
    ProviderNativeUsage,
    UsageAvailability,
    UsageCoverage,
    UsageProvider,
    UsageRole,
    UsageStage,
)

USAGE_FACT_SCHEMA_VERSION = 1
MAX_SQLITE_COUNTER = 2**63 - 1
MAX_USAGE_DIAGNOSTICS = 16
_DIAGNOSTIC_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

UsageTerminalStatus = Literal["completed", "failed", "cancelled", "uncertain"]
UsageSettlement = Literal["settled", "pending", "debt", "conflict"]
UsageImportOrigin = Literal["ingestion", "reconciliation"]
UsageBindingKind = Literal["completion_envelope", "ledger_attempt"]
UsageWriteResult = Literal["inserted", "replayed", "enriched", "conflict"]


class _FactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _reject_overflow(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        return
    if value < 0 or value > MAX_SQLITE_COUNTER:
        raise ValueError("usage counter overflow")


def assert_counter_payload(payload: object) -> None:
    if not isinstance(payload, dict):
        raise ValueError("usage counters must be an object")
    for value in payload.values():
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("usage counter type")
        _reject_overflow(value)


def canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(payload: object) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def safe_diagnostic_codes(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    codes: list[str] = []
    for item in values:
        if not isinstance(item, str) or not _DIAGNOSTIC_CODE.fullmatch(item):
            continue
        if item not in codes:
            codes.append(item)
        if len(codes) == MAX_USAGE_DIAGNOSTICS:
            break
    return tuple(codes)


def require_relative_usage_path(path: str) -> str:
    if not path or path.startswith(("/", "\\")) or "\\" in path:
        raise ValueError("usage source path is not relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("usage source path is not relative")
    return path


def require_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("usage digest is invalid")
    return value


class UsageFactPayloadV1(_FactModel):
    schema_version: Literal[1] = 1
    normalization_version: Literal["provider-usage-v1"] = USAGE_NORMALIZATION_VERSION
    logical_attempt_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=128)
    provider: UsageProvider
    role: UsageRole
    stage: UsageStage
    configured_model: str | None = Field(default=None, min_length=1, max_length=256)
    terminal_status: UsageTerminalStatus
    coverage: UsageCoverage | None
    settlement_status: UsageSettlement
    diagnostic_codes: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    import_origin: UsageImportOrigin

    @model_validator(mode="after")
    def _bounded_codes(self) -> UsageFactPayloadV1:
        if len(self.diagnostic_codes) > MAX_USAGE_DIAGNOSTICS:
            raise ValueError("too many usage diagnostics")
        if any(_DIAGNOSTIC_CODE.fullmatch(code) is None for code in self.diagnostic_codes):
            raise ValueError("usage diagnostic code is invalid")
        if len(set(self.diagnostic_codes)) != len(self.diagnostic_codes):
            raise ValueError("usage diagnostic codes must be unique")
        if any(_SHA256.fullmatch(item) is None for item in self.observation_ids):
            raise ValueError("usage observation id is invalid")
        if self.settlement_status == "pending" and self.coverage is not None:
            raise ValueError("pending usage reconciliation is not provider coverage")
        if self.settlement_status == "debt" and self.coverage is not None:
            raise ValueError("usage debt is not provider coverage")
        if self.coverage == "complete" and self.settlement_status == "pending":
            raise ValueError("pending usage cannot be complete")
        return self


class UsageObservationCountersV1(_FactModel):
    schema_version: Literal[1] = 1
    normalization_version: Literal["provider-usage-v1"] = USAGE_NORMALIZATION_VERSION
    observation_id: str = Field(min_length=64, max_length=64)
    availability: UsageAvailability
    capture_complete: StrictBool
    native: ProviderNativeUsage
    normalized: NormalizedUsage

    @model_validator(mode="after")
    def _counters(self) -> UsageObservationCountersV1:
        if _SHA256.fullmatch(self.observation_id) is None:
            raise ValueError("usage observation id is invalid")
        assert_counter_payload(self.native.model_dump(mode="json"))
        assert_counter_payload(self.normalized.model_dump(mode="json"))
        if self.availability == "known" and not self.capture_complete:
            raise ValueError("known usage observation requires complete capture")
        return self

    def semantic_payload(self) -> dict[str, object]:
        return {
            "availability": self.availability,
            "capture_complete": self.capture_complete,
            "native": self.native.model_dump(mode="json"),
            "normalization_version": self.normalization_version,
            "observation_id": self.observation_id,
            "schema_version": self.schema_version,
            "normalized": self.normalized.model_dump(mode="json"),
        }


def counter_relation(existing: dict[str, object], candidate: dict[str, object]) -> UsageWriteResult:
    """Same counters replay; null optionals may fill; disagreements conflict."""

    enriched = False
    for key in set(existing) | set(candidate):
        left = existing.get(key)
        right = candidate.get(key)
        if left == right:
            continue
        if left is None and right is not None:
            enriched = True
            continue
        return "conflict"
    return "enriched" if enriched else "replayed"


def observation_relation(
    existing: UsageObservationCountersV1,
    candidate: UsageObservationCountersV1,
) -> UsageWriteResult:
    if existing.observation_id != candidate.observation_id:
        return "conflict"
    native = counter_relation(
        existing.native.model_dump(mode="json"),
        candidate.native.model_dump(mode="json"),
    )
    normalized = counter_relation(
        existing.normalized.model_dump(mode="json"),
        candidate.normalized.model_dump(mode="json"),
    )
    if native == "conflict" or normalized == "conflict":
        return "conflict"
    completeness = existing.capture_complete == candidate.capture_complete
    availability = existing.availability == candidate.availability
    if not completeness and not (existing.capture_complete is False and candidate.capture_complete):
        return "conflict"
    if not availability and not (
        existing.availability == "partial" and candidate.availability == "known"
    ):
        return "conflict"
    if native == "enriched" or normalized == "enriched" or not completeness or not availability:
        return "enriched"
    return "replayed"
