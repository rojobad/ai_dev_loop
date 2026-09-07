"""Shared immutable scheduler domain types."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Sha256Hex = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
UuidSessionId = Annotated[
    str,
    StringConstraints(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    ),
]


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def coerce_utc_instant(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must be non-empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    else:
        raise ValueError("timestamp must be datetime or ISO-8601 string")
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(UTC)


def encode_utc_instant(value: datetime | str) -> str:
    instant = coerce_utc_instant(value)
    return instant.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def payload_sha256(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def worktree_key(repository_root: str) -> str:
    return hashlib.sha256(repository_root.encode("utf-8")).hexdigest()


def canonical_json_sha256(payload: dict[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return payload_sha256(text)
