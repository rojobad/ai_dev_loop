"""Typed Cursor sequence replacement intent.

Distinct from review-recovery replacement: an initial Cursor failure has no
staged patch and no reviewer, and a correction binds those facts only through
the accepted recovery record digest.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import ValidationInfo, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex

CURSOR_SEQUENCE_REPLACEMENT_INTENT_SCHEMA_VERSION: Literal[1] = 1
CursorSequenceAdoptionState = Literal["pending", "adopted"]
CursorSequencePublicationPostcondition = Literal["sequence_cursor_successor_dispatch_eligible"]
CURSOR_SEQUENCE_PUBLICATION_POSTCONDITION: CursorSequencePublicationPostcondition = (
    "sequence_cursor_successor_dispatch_eligible"
)


def _strict_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return value


class CursorSequenceReplacementIntent(DomainModel):
    """Durable claim that one blocked sequence leaf has a Cursor recovery successor."""

    schema_version: Literal[1] = CURSOR_SEQUENCE_REPLACEMENT_INTENT_SCHEMA_VERSION
    sequence_id: NonEmptyStr
    sequence_version: int
    ordinal: int
    source_run_id: NonEmptyStr
    source_generation: int
    successor_run_id: NonEmptyStr
    successor_generation: int
    recovery_key: NonEmptyStr
    record_digest: Sha256Hex | None = None
    entry_hash: Sha256Hex
    worktree_key: NonEmptyStr
    repository_root: NonEmptyStr
    reservation_owner_run_id: NonEmptyStr
    publication_postcondition: CursorSequencePublicationPostcondition = (
        CURSOR_SEQUENCE_PUBLICATION_POSTCONDITION
    )
    adoption_state: CursorSequenceAdoptionState = "pending"

    @field_validator(
        "schema_version",
        "sequence_version",
        "ordinal",
        "source_generation",
        "successor_generation",
        mode="before",
    )
    @classmethod
    def strict_integers(cls, value: object, info: ValidationInfo) -> object:
        number = _strict_positive_int(value, field_name=info.field_name or "value")
        if (
            info.field_name == "schema_version"
            and number != CURSOR_SEQUENCE_REPLACEMENT_INTENT_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be 1")
        return number

    @model_validator(mode="after")
    def generations_and_digest(self) -> CursorSequenceReplacementIntent:
        if self.successor_generation != self.source_generation + 1:
            raise ValueError("successor_generation must equal source_generation + 1")
        if self.reservation_owner_run_id != self.successor_run_id:
            raise ValueError("reservation owner must be the successor run")
        if self.adoption_state == "adopted" and self.record_digest is None:
            raise ValueError("adopted cursor sequence intent requires record_digest")
        if self.adoption_state == "pending" and self.record_digest is not None:
            raise ValueError("pending cursor sequence intent must not carry record_digest")
        return self

    def canonical_bytes(self) -> bytes:
        payload = self.model_dump(mode="json")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
