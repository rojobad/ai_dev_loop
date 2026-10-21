"""Versioned application policy and disposal records for Cursor SDK stores."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator

from ai_dev_loop.scheduler.domain.common import (
    DomainModel,
    NonEmptyStr,
    Sha256Hex,
    encode_utc_instant,
)

CURSOR_SDK_RETENTION_HOURS: Literal[168] = 168
CURSOR_SDK_RETENTION_POLICY_ID: Literal["cursor-sdk-retention"] = "cursor-sdk-retention"
CURSOR_SDK_RETENTION_DECISION_SOURCE: Literal["operator_2026-10-20"] = "operator_2026-10-20"
CURSOR_SDK_RETENTION_DECISION_AT: Literal["2026-10-20T00:00:00.000000Z"] = (
    "2026-10-20T00:00:00.000000Z"
)
CURSOR_STORE_CLEANUP_CONFIRMATION = "delete-cursor-sdk-conversation"


class CursorSdkRetentionPolicyV1(DomainModel):
    """Explicit operator retention policy. It is not inferred from directories."""

    schema_version: Literal[1] = 1
    policy_id: Literal["cursor-sdk-retention"] = CURSOR_SDK_RETENTION_POLICY_ID
    automatic_cleanup_enabled: bool
    retention_hours: Literal[168] = CURSOR_SDK_RETENTION_HOURS
    decision_source: Literal["operator_2026-10-20"] = CURSOR_SDK_RETENTION_DECISION_SOURCE
    decision_at: Literal["2026-10-20T00:00:00.000000Z"] = CURSOR_SDK_RETENTION_DECISION_AT
    updated_at: NonEmptyStr

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_utc(cls, value: str) -> str:
        return encode_utc_instant(value)


class CursorSdkDisposalIntentV1(DomainModel):
    """Durable deletion intent allocated before any native store removal."""

    schema_version: Literal[1] = 1
    intent_id: NonEmptyStr
    receipt_id: NonEmptyStr
    conversation_key: Sha256Hex
    conversation_owner_run_id: NonEmptyStr
    store_backend: Literal["jsonl", "sqlite"]
    native_store_layout_version: Literal[1] = 1
    mode: Literal["manual", "automatic"]
    definitively_finished_at: NonEmptyStr
    requested_at: NonEmptyStr

    @field_validator("definitively_finished_at", "requested_at")
    @classmethod
    def instants_are_utc(cls, value: str) -> str:
        return encode_utc_instant(value)


class CursorSdkDisposalReceiptV1(CursorSdkDisposalIntentV1):
    """Versioned receipt and tombstone for one disposed conversation root."""

    status: Literal["deleted"] = "deleted"
    deleted_at: NonEmptyStr
    intent_sha256: Sha256Hex
    receipt_sha256: Sha256Hex

    @field_validator("deleted_at")
    @classmethod
    def deleted_at_is_utc(cls, value: str) -> str:
        return encode_utc_instant(value)
