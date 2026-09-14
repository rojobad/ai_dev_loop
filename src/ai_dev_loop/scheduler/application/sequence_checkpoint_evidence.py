"""Authenticate checkpoint evidence for sequence completion reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    GitIdentity,
    checkpoint_git_rev_parse,
    checkpoint_verify_commit_identity,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
    SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
    SEQUENCE_CHECKPOINT_RESULT_ARTIFACT,
    SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointResult,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.sequence import MaterializedSequenceEntry
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore


class SequenceCheckpointEvidenceError(ValueError):
    """Raised when checkpoint evidence is missing or conflicts with ledger bindings."""


@dataclass(frozen=True)
class AuthenticatedCheckpointEvidence:
    commit_sha256: str
    parent_head: str
    tree_sha256: str
    intent_sha256: str
    trusted_tree_sha256: str
    reviewed_patch_sha256: str


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def authenticate_checkpoint_result_bindings(
    *,
    result: SequenceCheckpointResult,
    intent: SequenceCheckpointIntent,
    trusted_tree: SequenceCheckpointTrustedTree,
    intent_sha256: str,
    trusted_tree_file_sha256: str,
    run_id: str,
    materialized: MaterializedSequenceEntry,
) -> None:
    if result.intent_sha256 != intent_sha256:
        raise SequenceCheckpointEvidenceError("checkpoint result intent hash mismatch")
    if result.predecessor_run_id != run_id:
        raise SequenceCheckpointEvidenceError("checkpoint result run binding mismatch")
    if result.predecessor_ordinal != materialized.ordinal:
        raise SequenceCheckpointEvidenceError("checkpoint result ordinal mismatch")
    if intent.predecessor_run_id != run_id:
        raise SequenceCheckpointEvidenceError("checkpoint intent run binding mismatch")
    if intent.predecessor_ordinal != materialized.ordinal:
        raise SequenceCheckpointEvidenceError("checkpoint intent ordinal mismatch")
    if result.sequence_id != intent.sequence_id:
        raise SequenceCheckpointEvidenceError("checkpoint result sequence binding mismatch")
    if result.successor_run_id != intent.successor_run_id:
        raise SequenceCheckpointEvidenceError("checkpoint result successor binding mismatch")
    if result.branch_ref != intent.branch_ref:
        raise SequenceCheckpointEvidenceError("checkpoint result branch binding mismatch")
    if result.reviewed_patch_sha256 != intent.reviewed_patch_sha256:
        raise SequenceCheckpointEvidenceError("checkpoint reviewed-patch binding mismatch")
    if trusted_tree.intent_sha256 != intent_sha256:
        raise SequenceCheckpointEvidenceError("checkpoint trusted-tree intent hash mismatch")
    if result.trusted_tree_sha256 != trusted_tree_file_sha256:
        raise SequenceCheckpointEvidenceError("checkpoint trusted-tree artifact hash mismatch")
    if result.tree_sha256 != trusted_tree.reviewed_tree_sha256:
        raise SequenceCheckpointEvidenceError("checkpoint tree binding mismatch")
    if result.parent_head != intent.parent_head:
        raise SequenceCheckpointEvidenceError("checkpoint result parent disagrees with intent")
    if result.parent_head != trusted_tree.parent_head:
        raise SequenceCheckpointEvidenceError(
            "checkpoint result parent disagrees with trusted tree"
        )


def verify_checkpoint_commit_in_repository(
    *,
    intent: SequenceCheckpointIntent,
    trusted_tree: SequenceCheckpointTrustedTree,
    commit_sha256: str,
    evidence_commit_sha256: str | None = None,
    require_head_match: bool = False,
) -> None:
    """Verify an existing commit object and optional branch HEAD binding in the repository."""
    if evidence_commit_sha256 is not None and evidence_commit_sha256 != commit_sha256:
        raise SequenceCheckpointEvidenceError(
            "checkpoint evidence commit disagrees with verified commit"
        )
    repo_root = Path(intent.repository_root)
    identity = GitIdentity(
        author_name=intent.git_identity.author_name,
        author_email=intent.git_identity.author_email,
        author_date=intent.git_identity.author_date,
        committer_name=intent.git_identity.committer_name,
        committer_email=intent.git_identity.committer_email,
        committer_date=intent.git_identity.committer_date,
    )
    try:
        checkpoint_verify_commit_identity(
            repo_root,
            commit_sha=commit_sha256,
            tree_sha=trusted_tree.reviewed_tree_sha256,
            parent_sha=intent.parent_head,
            message=intent.commit_message,
            identity=identity,
        )
    except ValidationError as exc:
        raise SequenceCheckpointEvidenceError(str(exc)) from exc
    if require_head_match:
        live_head = checkpoint_git_rev_parse(repo_root, "HEAD")
        if live_head != commit_sha256:
            raise SequenceCheckpointEvidenceError("branch HEAD does not point at checkpoint commit")


def authenticate_checkpoint_evidence(
    artifacts: ProtectedArtifactStore,
    *,
    run_id: str,
    materialized: MaterializedSequenceEntry,
    total_phases: int,
) -> AuthenticatedCheckpointEvidence | None:
    """Return authenticated checkpoint evidence for a non-final materialized phase."""
    if materialized.ordinal >= total_phases:
        return None
    run_root = artifacts.run_root(run_id)
    result_path = run_root / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
    intent_path = run_root / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
    trusted_tree_path = run_root / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
    if not result_path.is_file():
        raise SequenceCheckpointEvidenceError(
            f"missing checkpoint result artifact for ordinal {materialized.ordinal}"
        )
    if not intent_path.is_file():
        raise SequenceCheckpointEvidenceError(
            f"missing checkpoint intent artifact for ordinal {materialized.ordinal}"
        )
    if not trusted_tree_path.is_file():
        raise SequenceCheckpointEvidenceError(
            f"missing checkpoint trusted-tree artifact for ordinal {materialized.ordinal}"
        )
    result = SequenceCheckpointResult.model_validate_json(result_path.read_bytes())
    intent = SequenceCheckpointIntent.model_validate_json(intent_path.read_bytes())
    trusted_tree = SequenceCheckpointTrustedTree.model_validate_json(trusted_tree_path.read_bytes())
    intent_sha256 = _sha256_file(intent_path)
    trusted_tree_file_sha256 = _sha256_file(trusted_tree_path)
    authenticate_checkpoint_result_bindings(
        result=result,
        intent=intent,
        trusted_tree=trusted_tree,
        intent_sha256=intent_sha256,
        trusted_tree_file_sha256=trusted_tree_file_sha256,
        run_id=run_id,
        materialized=materialized,
    )
    evidence_path = run_root / SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT
    evidence_commit: str | None = None
    if evidence_path.is_file():
        evidence = SequenceCheckpointEvidence.model_validate_json(evidence_path.read_bytes())
        evidence_commit = evidence.commit_sha256
        if evidence_commit is not None and evidence_commit != result.commit_sha256:
            raise SequenceCheckpointEvidenceError(
                "checkpoint evidence commit disagrees with result artifact"
            )
    verify_checkpoint_commit_in_repository(
        intent=intent,
        trusted_tree=trusted_tree,
        commit_sha256=result.commit_sha256,
        evidence_commit_sha256=evidence_commit,
    )
    return AuthenticatedCheckpointEvidence(
        commit_sha256=result.commit_sha256,
        parent_head=result.parent_head,
        tree_sha256=result.tree_sha256,
        intent_sha256=result.intent_sha256,
        trusted_tree_sha256=result.trusted_tree_sha256,
        reviewed_patch_sha256=result.reviewed_patch_sha256,
    )


def load_checkpoint_result_summary(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_bytes())
    if not isinstance(payload, dict):
        raise SequenceCheckpointEvidenceError("checkpoint result artifact is not an object")
    return {str(key): str(value) for key, value in payload.items() if value is not None}
