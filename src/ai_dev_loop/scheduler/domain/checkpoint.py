"""Typed models for sequence reviewed-tree checkpoint intents and results."""

from __future__ import annotations

from typing import Literal

from pydantic import field_validator, model_validator

from ai_dev_loop.scheduler.domain.common import DomainModel, GitObjectSha, NonEmptyStr, Sha256Hex

SEQUENCE_CHECKPOINT_INTENT_ARTIFACT = "sequence-checkpoints/intent.json"
SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT = "sequence-checkpoints/trusted-reviewed-tree.json"
SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT = "sequence-checkpoints/evidence.json"
SEQUENCE_CHECKPOINT_RESULT_ARTIFACT = "sequence-checkpoints/result.json"


class GitIdentitySnapshot(DomainModel):
    author_name: NonEmptyStr
    author_email: NonEmptyStr
    author_date: NonEmptyStr
    committer_name: NonEmptyStr
    committer_email: NonEmptyStr
    committer_date: NonEmptyStr


class SequenceCheckpointIntent(DomainModel):
    """Content-bound checkpoint intent persisted before any Git object/ref mutation."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    sequence_version: int
    predecessor_run_id: NonEmptyStr
    predecessor_run_version: int
    predecessor_ordinal: int
    successor_run_id: NonEmptyStr
    successor_ordinal: int
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    branch_ref: NonEmptyStr
    parent_head: GitObjectSha
    reviewed_patch_sha256: Sha256Hex
    commit_message: NonEmptyStr
    git_identity: GitIdentitySnapshot
    repository_root: NonEmptyStr
    git_common_dir: NonEmptyStr
    git_dir: NonEmptyStr
    staged_patch_artifact_path: NonEmptyStr
    review_result_artifact_path: NonEmptyStr
    review_result_sha256: Sha256Hex

    @field_validator("sequence_version", "predecessor_run_version", "predecessor_ordinal")
    @classmethod
    def positive_counters(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be >= 1")
        return value

    @field_validator("successor_ordinal")
    @classmethod
    def successor_ordinal_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("successor_ordinal must be >= 1")
        return value

    @model_validator(mode="after")
    def successor_follows_predecessor(self) -> SequenceCheckpointIntent:
        if self.successor_ordinal != self.predecessor_ordinal + 1:
            raise ValueError("successor_ordinal must equal predecessor_ordinal + 1")
        return self


class SequenceCheckpointTrustedTree(DomainModel):
    """Immutable trusted reviewed-tree evidence written before any Git mutation."""

    schema_version: Literal[1] = 1
    intent_sha256: Sha256Hex
    reviewed_tree_sha256: GitObjectSha
    reviewed_patch_sha256: Sha256Hex
    parent_head: GitObjectSha
    recorded_at: NonEmptyStr


class SequenceCheckpointEvidence(DomainModel):
    """Mutable Git mutation evidence authenticated against the trusted reviewed tree."""

    schema_version: Literal[1] = 1
    tree_sha256: GitObjectSha | None = None
    commit_sha256: GitObjectSha | None = None


class SequenceCheckpointResult(DomainModel):
    """Durable evidence after a successful reviewed-tree checkpoint commit."""

    schema_version: Literal[1] = 1
    sequence_id: NonEmptyStr
    predecessor_run_id: NonEmptyStr
    predecessor_ordinal: int
    successor_run_id: NonEmptyStr
    accepted_outcome: Literal["completed", "completed_with_residual_risk"]
    commit_sha256: GitObjectSha
    tree_sha256: GitObjectSha
    parent_head: GitObjectSha
    branch_ref: NonEmptyStr
    reviewed_patch_sha256: Sha256Hex
    intent_sha256: Sha256Hex
    trusted_tree_sha256: Sha256Hex

    @field_validator("predecessor_ordinal")
    @classmethod
    def ordinal_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("predecessor_ordinal must be >= 1")
        return value
