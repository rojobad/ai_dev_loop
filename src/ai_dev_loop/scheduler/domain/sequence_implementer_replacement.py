"""Typed sequence replacement authority for Codex implementer recovery."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import ValidationInfo, field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex

SEQUENCE_IMPLEMENTER_REPLACEMENT_INTENT_SCHEMA_VERSION: Literal[1] = 1
SequenceImplementerAdoptionState = Literal["pending", "adopted"]
SequenceImplementerPublicationPostcondition = Literal[
    "sequence_implementer_successor_dispatch_eligible"
]
SEQUENCE_IMPLEMENTER_PUBLICATION_POSTCONDITION: SequenceImplementerPublicationPostcondition = (
    "sequence_implementer_successor_dispatch_eligible"
)


def _strict_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 1:
        raise ValueError(f"{field_name} must be >= 1")
    return value


class SequenceImplementerReplacementIntent(DomainModel):
    """Durable claim for one Codex successor at the current sequence ordinal."""

    schema_version: Literal[1] = SEQUENCE_IMPLEMENTER_REPLACEMENT_INTENT_SCHEMA_VERSION
    provider: Literal["codex"] = "codex"
    sequence_id: NonEmptyStr
    sequence_version: int
    ordinal: int
    source_run_id: NonEmptyStr
    source_generation: int
    successor_run_id: NonEmptyStr
    successor_generation: int
    recovery_key: Sha256Hex
    record_digest: Sha256Hex | None = None
    entry_hash: Sha256Hex
    worktree_key: NonEmptyStr
    repository_root: NonEmptyStr
    reservation_owner_run_id: NonEmptyStr
    publication_postcondition: SequenceImplementerPublicationPostcondition = (
        SEQUENCE_IMPLEMENTER_PUBLICATION_POSTCONDITION
    )
    adoption_state: SequenceImplementerAdoptionState = "pending"

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
            and number != SEQUENCE_IMPLEMENTER_REPLACEMENT_INTENT_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be 1")
        return number

    @model_validator(mode="after")
    def generations_and_digest(self) -> SequenceImplementerReplacementIntent:
        if self.successor_generation != self.source_generation + 1:
            raise ValueError("successor_generation must equal source_generation + 1")
        if self.reservation_owner_run_id != self.successor_run_id:
            raise ValueError("reservation owner must be the successor run")
        if self.adoption_state == "adopted" and self.record_digest is None:
            raise ValueError("adopted sequence implementer intent requires record_digest")
        if self.adoption_state == "pending" and self.record_digest is not None:
            raise ValueError("pending sequence implementer intent must not carry record_digest")
        return self

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
