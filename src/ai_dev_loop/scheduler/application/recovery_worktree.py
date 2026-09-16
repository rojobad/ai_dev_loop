"""Managed recovery worktree port for Phase 20.6."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_rev_parse,
    checkpoint_git_status_porcelain,
    checkpoint_git_write_tree,
    checkpoint_validate_repository_layout,
    checkpoint_validate_staged_patch_matches_artifact,
    paths_with_unstaged_changes,
    paths_with_untracked,
    recovery_git_apply_staged_patch,
    recovery_git_create_private_ref,
    recovery_git_delete_ref,
    recovery_git_private_ref_peek,
    recovery_git_worktree_add,
    recovery_git_worktree_contains,
    recovery_git_worktree_remove,
)
from ai_dev_loop.scheduler.application.git_admission import (
    discover_repository_bounded,
    format_admission_artifact_text,
)
from ai_dev_loop.scheduler.domain.recovery import FreshReviewRecoveryDefinition


class RecoveryWorktreeError(ValidationError):
    """Raised when a recovery worktree operation cannot complete safely."""


@dataclass(frozen=True)
class RecoverySeedEvidence:
    parent_head: str
    staged_patch_sha256: str
    staged_tree_sha256: str
    worktree_path: str
    private_ref: str
    target_repository_root: str
    managed_git_common_dir: str
    managed_git_dir: str
    managed_branch: str
    pre_seed_admission_status: str
    post_seed_status_porcelain: str = ""
    worktree_registration_artifact_sha256: str | None = None


class RecoveryWorktreePort(Protocol):
    def create_and_seed(
        self,
        definition: FreshReviewRecoveryDefinition,
        *,
        patch_path: Path,
        worktree_path: Path,
    ) -> RecoverySeedEvidence:
        """Create private ref/worktree and seed the authenticated patch."""

    def remove_worktree(
        self,
        target_repo_root: Path,
        *,
        worktree_path: Path,
    ) -> None:
        """Remove an exact owned recovery worktree."""

    def delete_private_ref(
        self,
        target_repo_root: Path,
        *,
        private_ref: str,
        expected_sha: str,
    ) -> None:
        """Delete the private recovery ref after identity verification."""


class ProductionRecoveryWorktreePort:
    def create_and_seed(
        self,
        definition: FreshReviewRecoveryDefinition,
        *,
        patch_path: Path,
        worktree_path: Path,
    ) -> RecoverySeedEvidence:
        repo_root = Path(definition.repository.root)
        checkpoint_validate_repository_layout(
            repo_root,
            expected_root=definition.repository.root,
            expected_git_common_dir=definition.repository.git_common_dir,
            expected_git_dir=definition.repository.git_dir,
            expected_branch=definition.repository.branch,
            context="recovery target repository identity",
        )
        if worktree_path.exists():
            return self._reconcile_partial_worktree(
                definition,
                patch_path=patch_path,
                worktree_path=worktree_path,
                repo_root=repo_root,
            )
        recovery_git_create_private_ref(
            repo_root,
            ref=definition.private_ref,
            parent_head=definition.parent_head,
        )
        recovery_git_worktree_add(
            repo_root,
            worktree_path=worktree_path,
            parent_head=definition.parent_head,
        )
        pre_seed = discover_repository_bounded(worktree_path)
        if pre_seed.status_porcelain.strip():
            raise RecoveryWorktreeError("recovery worktree is not clean before patch seeding")
        patch_bytes = patch_path.read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != definition.source_staged_patch_sha256:
            raise RecoveryWorktreeError("patch artifact digest mismatch")
        recovery_git_apply_staged_patch(worktree_path, patch_bytes=patch_bytes)
        checkpoint_validate_staged_patch_matches_artifact(worktree_path, patch_path)
        live_patch = checkpoint_git_diff_cached_patch_bytes(worktree_path)
        live_patch_sha = hashlib.sha256(live_patch).hexdigest()
        if live_patch_sha != definition.source_staged_patch_sha256:
            raise RecoveryWorktreeError("seeded staged patch hash mismatch")
        tree_sha = checkpoint_git_write_tree(worktree_path)
        if tree_sha != definition.source_staged_tree_sha256:
            raise RecoveryWorktreeError("seeded tree SHA mismatch")
        status = checkpoint_git_status_porcelain(worktree_path)
        if paths_with_unstaged_changes(status) or paths_with_untracked(status):
            raise RecoveryWorktreeError("recovery worktree is not clean after seeding")
        head = checkpoint_git_rev_parse(worktree_path, "HEAD")
        if head != definition.parent_head:
            raise RecoveryWorktreeError("recovery worktree HEAD drift after seeding")
        post_seed = discover_repository_bounded(worktree_path)
        return RecoverySeedEvidence(
            parent_head=definition.parent_head,
            staged_patch_sha256=live_patch_sha,
            staged_tree_sha256=tree_sha,
            worktree_path=str(worktree_path.resolve()),
            private_ref=definition.private_ref,
            target_repository_root=definition.repository.root,
            managed_git_common_dir=post_seed.git_common_dir,
            managed_git_dir=post_seed.git_dir,
            managed_branch=post_seed.branch,
            pre_seed_admission_status=format_admission_artifact_text(pre_seed),
        )

    def _reconcile_partial_worktree(
        self,
        definition: FreshReviewRecoveryDefinition,
        *,
        patch_path: Path,
        worktree_path: Path,
        repo_root: Path,
    ) -> RecoverySeedEvidence:
        private_sha = recovery_git_private_ref_peek(repo_root, ref=definition.private_ref)
        if private_sha is None:
            recovery_git_create_private_ref(
                repo_root,
                ref=definition.private_ref,
                parent_head=definition.parent_head,
            )
        elif private_sha != definition.parent_head:
            raise RecoveryWorktreeError("partial recovery private ref drift")
        if not recovery_git_worktree_contains(repo_root, worktree_path=worktree_path):
            raise RecoveryWorktreeError("partial recovery worktree is not registered")
        pre_seed = discover_repository_bounded(worktree_path)
        checkpoint_validate_repository_layout(
            worktree_path,
            expected_root=str(worktree_path.resolve()),
            expected_git_common_dir=pre_seed.git_common_dir,
            expected_git_dir=pre_seed.git_dir,
            expected_branch="HEAD",
            context="partial recovery worktree identity",
        )
        head = checkpoint_git_rev_parse(worktree_path, "HEAD")
        if head != definition.parent_head:
            raise RecoveryWorktreeError("partial recovery worktree HEAD drift")
        live_patch = checkpoint_git_diff_cached_patch_bytes(worktree_path)
        live_patch_sha = hashlib.sha256(live_patch).hexdigest()
        tree_sha = checkpoint_git_write_tree(worktree_path)
        patch_bytes = patch_path.read_bytes()
        if hashlib.sha256(patch_bytes).hexdigest() != definition.source_staged_patch_sha256:
            raise RecoveryWorktreeError("patch artifact digest mismatch")
        status_before_apply = checkpoint_git_status_porcelain(worktree_path)
        needs_worktree_apply = paths_with_unstaged_changes(status_before_apply)
        if live_patch_sha != definition.source_staged_patch_sha256:
            if pre_seed.status_porcelain.strip():
                raise RecoveryWorktreeError("partial recovery worktree is not clean before reseed")
            recovery_git_apply_staged_patch(worktree_path, patch_bytes=patch_bytes)
            live_patch = checkpoint_git_diff_cached_patch_bytes(worktree_path)
            live_patch_sha = hashlib.sha256(live_patch).hexdigest()
        elif needs_worktree_apply:
            recovery_git_apply_staged_patch(worktree_path, patch_bytes=patch_bytes)
        if live_patch_sha != definition.source_staged_patch_sha256:
            raise RecoveryWorktreeError("partial recovery seeded patch hash mismatch")
        checkpoint_validate_staged_patch_matches_artifact(worktree_path, patch_path)
        tree_sha = checkpoint_git_write_tree(worktree_path)
        if tree_sha != definition.source_staged_tree_sha256:
            raise RecoveryWorktreeError("partial recovery seeded tree SHA mismatch")
        status = checkpoint_git_status_porcelain(worktree_path)
        if paths_with_unstaged_changes(status) or paths_with_untracked(status):
            raise RecoveryWorktreeError("partial recovery worktree is not clean after reconcile")
        post_seed = discover_repository_bounded(worktree_path)
        return RecoverySeedEvidence(
            parent_head=definition.parent_head,
            staged_patch_sha256=live_patch_sha,
            staged_tree_sha256=tree_sha,
            worktree_path=str(worktree_path.resolve()),
            private_ref=definition.private_ref,
            target_repository_root=definition.repository.root,
            managed_git_common_dir=post_seed.git_common_dir,
            managed_git_dir=post_seed.git_dir,
            managed_branch=post_seed.branch,
            pre_seed_admission_status=format_admission_artifact_text(pre_seed),
        )

    def remove_worktree(self, target_repo_root: Path, *, worktree_path: Path) -> None:
        recovery_git_worktree_remove(target_repo_root, worktree_path=worktree_path)

    def delete_private_ref(
        self,
        target_repo_root: Path,
        *,
        private_ref: str,
        expected_sha: str,
    ) -> None:
        recovery_git_delete_ref(
            target_repo_root,
            ref=private_ref,
            expected_sha=expected_sha,
        )


class FakeRecoveryWorktreePort:
    """In-memory fake for hermetic tests."""

    def __init__(
        self,
        *,
        on_create: Callable[..., RecoverySeedEvidence] | None = None,
        fail_on: str | None = None,
    ) -> None:
        self._on_create = on_create
        self._fail_on = fail_on
        self.created: list[tuple[str, Path]] = []
        self.removed: list[Path] = []

    def create_and_seed(
        self,
        definition: FreshReviewRecoveryDefinition,
        *,
        patch_path: Path,
        worktree_path: Path,
    ) -> RecoverySeedEvidence:
        if self._fail_on == "create":
            raise RecoveryWorktreeError("injected worktree failure")
        self.created.append((definition.recovery_id, worktree_path))
        if self._on_create is not None:
            return self._on_create(definition, patch_path=patch_path, worktree_path=worktree_path)
        from ai_dev_loop.scheduler.application.git_admission import GitAdmissionEvidence

        pre_seed = format_admission_artifact_text(
            GitAdmissionEvidence(
                resolved_root=str(worktree_path),
                branch="HEAD",
                head=definition.parent_head,
                git_common_dir=definition.repository.git_common_dir,
                git_dir=definition.repository.git_dir,
                status_porcelain="",
            )
        )
        return RecoverySeedEvidence(
            parent_head=definition.parent_head,
            staged_patch_sha256=definition.source_staged_patch_sha256,
            staged_tree_sha256=definition.source_staged_tree_sha256,
            worktree_path=str(worktree_path),
            private_ref=definition.private_ref,
            target_repository_root=definition.repository.root,
            managed_git_common_dir=definition.repository.git_common_dir,
            managed_git_dir=definition.repository.git_dir,
            managed_branch="HEAD",
            pre_seed_admission_status=pre_seed,
        )

    def remove_worktree(self, target_repo_root: Path, *, worktree_path: Path) -> None:
        self.removed.append(worktree_path)

    def delete_private_ref(
        self,
        target_repo_root: Path,
        *,
        private_ref: str,
        expected_sha: str,
    ) -> None:
        return None
