"""Read-only verification for Codex review retry and recovery checkpoints."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    git_status_porcelain,
    paths_with_unstaged_changes,
    paths_with_untracked,
    validate_staged_patch_matches_artifact,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    frozen_repository_identity,
    validate_frozen_repository_identity,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    AwaitingCodexReviewState,
    SubmittedRunContext,
    WaitingCodexCapacityState,
    WaitingCodexReviewRetryState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.state import sha256_bytes


class ReviewCheckpointVerificationError(ValidationError):
    """Raised when a review checkpoint no longer matches the live repository."""


def verify_review_retry_repository_checkpoint(
    *,
    context: SubmittedRunContext,
    run_id: str,
    artifacts: ProtectedArtifactStore,
    checkpoint: AdmittedRunCheckpoint | None,
    cursor: object,
    plan_sha256: str,
    prompt_sha256: str,
) -> None:
    identity = frozen_repository_identity(
        context,
        run_id=run_id,
        artifacts=artifacts,
        checkpoint=checkpoint,
    )
    repo_root = Path(identity.root)
    try:
        validate_frozen_repository_identity(
            repo_root,
            identity,
            context="scheduler review retry",
        )
    except (CursorEvidenceError, ValidationError) as exc:
        raise ReviewCheckpointVerificationError(str(exc)) from exc

    plan_path = artifacts.run_root(run_id) / context.plan_prompt.plan_artifact_path
    prompt_path = artifacts.run_root(run_id) / context.plan_prompt.prompt_artifact_path
    if sha256_bytes(plan_path.read_bytes()) != plan_sha256:
        raise ReviewCheckpointVerificationError("frozen plan artifact hash mismatch")
    if sha256_bytes(prompt_path.read_bytes()) != prompt_sha256:
        raise ReviewCheckpointVerificationError("frozen prompt artifact hash mismatch")

    staged_patch_path = getattr(cursor, "staged_patch_path", None)
    staged_patch_sha = getattr(cursor, "staged_patch_sha256", None)
    if not staged_patch_path or not staged_patch_sha:
        raise ReviewCheckpointVerificationError("staged patch binding missing")
    patch_abs = artifacts.run_root(run_id) / str(staged_patch_path)
    if sha256_bytes(patch_abs.read_bytes()) != str(staged_patch_sha):
        raise ReviewCheckpointVerificationError("staged patch artifact hash mismatch")
    try:
        validate_staged_patch_matches_artifact(repo_root, patch_abs)
    except ValidationError as exc:
        raise ReviewCheckpointVerificationError(str(exc)) from exc

    status = git_status_porcelain(repo_root)
    unstaged = paths_with_unstaged_changes(status)
    if unstaged:
        joined = ", ".join(sorted(unstaged))
        raise ReviewCheckpointVerificationError(
            f"tracked unstaged changes detected before review retry: {joined}"
        )
    untracked = paths_with_untracked(status)
    if untracked:
        joined = ", ".join(sorted(untracked))
        raise ReviewCheckpointVerificationError(
            f"untracked non-ignored files detected before review retry: {joined}"
        )


def verify_retry_state_repository(
    state: WaitingCodexReviewRetryState | WaitingCodexCapacityState | AwaitingCodexReviewState,
    *,
    artifacts: ProtectedArtifactStore,
) -> None:
    verify_review_retry_repository_checkpoint(
        context=state.context,
        run_id=state.run_id,
        artifacts=artifacts,
        checkpoint=state.checkpoint,
        cursor=state.cursor,
        plan_sha256=state.context.plan_prompt.plan_sha256,
        prompt_sha256=state.context.plan_prompt.prompt_sha256,
    )
