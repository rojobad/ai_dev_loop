"""Authenticated Cursor SDK no-progress witness and one-use continuation authority."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import Field, field_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex

NO_PROGRESS_CATEGORY: Literal["cursor_sdk_resume_no_progress"] = "cursor_sdk_resume_no_progress"
NO_PROGRESS_MESSAGE = "Agent turn stopped after repeated resume attempts made no progress"
NO_PROGRESS_DELAY_SECONDS = 60
NO_PROGRESS_MESSAGE_SHA256 = hashlib.sha256(NO_PROGRESS_MESSAGE.encode("utf-8")).hexdigest()
LOGICAL_TURN_SCHEMA = "cursor-sdk-logical-turn-v1"


def no_progress_witness_rel(attempt_id: str) -> str:
    return f"cursor/sdk-no-progress/{attempt_id}/witness-v1.json"


def logical_turn_key(
    *,
    turn_kind: Literal["initial", "correction"],
    conversation_key: str,
    base_prompt_sha256: str,
    reviews_completed: int,
    fix_prompt_sha256: str | None,
    review_result_sha256: str | None,
) -> str:
    """Identity of one logical implementer turn, independent of recovery wrappers."""

    if isinstance(reviews_completed, bool) or not isinstance(reviews_completed, int):
        raise ValueError("reviews_completed must be a JSON integer")
    payload = {
        "schema": LOGICAL_TURN_SCHEMA,
        "turn_kind": turn_kind,
        "conversation_key": conversation_key,
        "base_prompt_sha256": base_prompt_sha256,
        "reviews_completed": reviews_completed,
        "fix_prompt_sha256": fix_prompt_sha256,
        "review_result_sha256": review_result_sha256,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def exact_status_no_progress(
    payload: object,
    *,
    agent_id: str,
    native_run_id: str,
) -> bool:
    """True only for the public status event with the exact terminal message.

    Assistant, thinking, and tool payloads are ignored. The event's own agent
    and run identities must match the owned stream. Outer whitespace on the
    message is ignored; any other text is not this category.
    """

    if not isinstance(payload, dict):
        return False
    message = payload.get("sdk_message")
    if not isinstance(message, dict) or message.get("type") != "status":
        return False
    if message.get("status") != "ERROR":
        return False
    raw = message.get("message")
    if not isinstance(raw, str) or raw.strip() != NO_PROGRESS_MESSAGE:
        return False
    event_agent = message.get("agent_id")
    event_run = message.get("run_id")
    if not isinstance(event_agent, str) or not isinstance(event_run, str):
        return False
    return event_agent == agent_id and event_run == native_run_id


def public_terminal_status_name(payload: object) -> str | None:
    """Return a public terminal status name from one captured SDK envelope."""

    if not isinstance(payload, dict):
        return None
    message = payload.get("sdk_message")
    if not isinstance(message, dict) or message.get("type") != "status":
        return None
    status = message.get("status")
    if not isinstance(status, str):
        return None
    normalized = status.strip().upper()
    if normalized in {"ERROR", "FINISHED", "FAILED", "CANCELLED", "CANCELED"}:
        return normalized
    return None


class CursorSdkNoProgressWitnessV1(DomainModel):
    """Bounded witness that a captured status event is the known terminal failure."""

    schema_version: Literal[1] = 1
    category: Literal["cursor_sdk_resume_no_progress"] = NO_PROGRESS_CATEGORY
    attempt_id: NonEmptyStr
    dispatch_id: NonEmptyStr
    agent_id: NonEmptyStr
    native_run_id: NonEmptyStr
    offset: NonEmptyStr
    capture_sha256: Sha256Hex
    events_path: NonEmptyStr
    journal_path: NonEmptyStr
    journal_sha256: Sha256Hex
    events_sha256: Sha256Hex
    events_truncated: Literal[False] = False
    capture_complete: bool = False
    final_native_status: Literal["error"] = "error"
    event_type: Literal["status"] = "status"
    event_status: Literal["ERROR"] = "ERROR"
    message_sha256: Sha256Hex

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")


class CursorSdkNoProgressAuthorizationV1(DomainModel):
    """One reserved automatic continuation for a logical implementer turn."""

    schema_version: Literal[1] = 1
    logical_turn_key: Sha256Hex
    consumed_count: int = Field(ge=0, le=1)
    status: Literal["reserved", "publishing", "published", "held", "cancelled"]
    category: Literal["cursor_sdk_resume_no_progress"] = NO_PROGRESS_CATEGORY
    source_run_id: NonEmptyStr
    source_attempt_id: NonEmptyStr
    witness_path: NonEmptyStr
    witness_sha256: Sha256Hex
    authorized_at: NonEmptyStr
    due_at: NonEmptyStr
    turn_kind: Literal["initial", "correction"]
    conversation_key: Sha256Hex
    base_prompt_sha256: Sha256Hex
    reviews_completed: int = Field(ge=0)
    fix_prompt_sha256: Sha256Hex | None = None
    review_result_sha256: Sha256Hex | None = None
    successor_run_id: NonEmptyStr | None = None
    recovery_key: Sha256Hex | None = None

    @field_validator("consumed_count", "reviews_completed", mode="before")
    @classmethod
    def strict_ints(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("count must be a JSON integer")
        return value

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
