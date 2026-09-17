"""Typed sequence execution replacement intent for same-reviewer review recovery."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr, Sha256Hex

SEQUENCE_EXECUTION_REPLACEMENT_INTENT_SCHEMA_VERSION = 2
SequenceRecoveryPublicationPostcondition = Literal["sequence_active_successor_tick_eligible"]
SEQUENCE_RECOVERY_PUBLICATION_POSTCONDITION: SequenceRecoveryPublicationPostcondition = (
    "sequence_active_successor_tick_eligible"
)


class SequenceExecutionReplacementIntent(DomainModel):
    """Durable claim binding a blocked sequence leaf to a review-recovery successor."""

    schema_version: Literal[1, 2] = 2
    sequence_id: NonEmptyStr
    sequence_version: int
    ordinal: int
    source_run_id: NonEmptyStr
    source_generation: int
    successor_run_id: NonEmptyStr
    successor_generation: int
    recovery_key: NonEmptyStr
    entry_hash: Sha256Hex
    staged_patch_sha256: Sha256Hex
    worktree_key: NonEmptyStr
    repository_root: NonEmptyStr
    reviewer_session_id: NonEmptyStr
    review_model: NonEmptyStr
    review_reasoning_effort: NonEmptyStr
    codex_sandbox: NonEmptyStr
    reservation_owner_run_id: NonEmptyStr
    publication_postcondition: SequenceRecoveryPublicationPostcondition = (
        SEQUENCE_RECOVERY_PUBLICATION_POSTCONDITION
    )

    @field_validator("sequence_version", "ordinal", "source_generation", "successor_generation")
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @model_validator(mode="after")
    def successor_follows_source(self) -> SequenceExecutionReplacementIntent:
        if self.successor_generation != self.source_generation + 1:
            raise ValueError("successor_generation must equal source_generation + 1")
        return self
