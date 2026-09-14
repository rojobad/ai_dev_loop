"""Injectable Git checkpoint port for reviewed sequence-tree commits."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    CheckpointGitDeadline,
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_rev_parse,
    checkpoint_git_status_porcelain,
    checkpoint_git_symbolic_ref,
    checkpoint_git_update_ref_cas,
    checkpoint_git_write_tree,
    checkpoint_validate_checked_out_branch,
    checkpoint_validate_repository_layout,
    checkpoint_validate_staged_patch_matches_artifact,
    checkpoint_verify_commit_identity,
    paths_with_unstaged_changes,
    paths_with_untracked,
    validate_checkpoint_postconditions,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointResult,
    SequenceCheckpointTrustedTree,
)

CheckpointMutationBoundary = Literal["pre_commit_tree", "pre_update_ref"]


class GitCheckpointError(ValidationError):
    """Raised when a reviewed-tree checkpoint cannot be created safely."""


class CheckpointFenceError(GitCheckpointError):
    """Raised when abort or lease fencing blocks checkpoint mutation."""


@dataclass(frozen=True)
class GitCheckpointCommit:
    commit_sha: str
    tree_sha: str
    parent_head: str
    already_applied: bool = False
    ref_updated: bool = False


class CheckpointMutationFence(Protocol):
    def __call__(self, boundary: CheckpointMutationBoundary) -> None:
        """Raise CheckpointFenceError when mutation must not proceed."""


class GitCheckpointPort(Protocol):
    def execute_checkpoint(
        self,
        intent: SequenceCheckpointIntent,
        *,
        patch_path: Path,
        trusted_tree: SequenceCheckpointTrustedTree,
        evidence: SequenceCheckpointEvidence | None,
        persist_tree_sha: Callable[[str], None],
        persist_commit_sha: Callable[[str], None],
        mutation_fence: CheckpointMutationFence | None = None,
        authorize_ref_update: Callable[[], None] | None = None,
        on_ref_advanced: Callable[[], None] | None = None,
        deadline: CheckpointGitDeadline,
        now_factory: Callable[[], datetime],
    ) -> GitCheckpointCommit:
        """Create or adopt the exact intent-bound checkpoint commit."""


class ProductionGitCheckpointPort:
    def execute_checkpoint(
        self,
        intent: SequenceCheckpointIntent,
        *,
        patch_path: Path,
        trusted_tree: SequenceCheckpointTrustedTree,
        evidence: SequenceCheckpointEvidence | None,
        persist_tree_sha: Callable[[str], None],
        persist_commit_sha: Callable[[str], None],
        mutation_fence: CheckpointMutationFence | None = None,
        authorize_ref_update: Callable[[], None] | None = None,
        on_ref_advanced: Callable[[], None] | None = None,
        deadline: CheckpointGitDeadline,
        now_factory: Callable[[], datetime],
    ) -> GitCheckpointCommit:
        repo_root = Path(intent.repository_root)
        identity = GitIdentity(
            author_name=intent.git_identity.author_name,
            author_email=intent.git_identity.author_email,
            author_date=intent.git_identity.author_date,
            committer_name=intent.git_identity.committer_name,
            committer_email=intent.git_identity.committer_email,
            committer_date=intent.git_identity.committer_date,
        )
        timeout = deadline.remaining_timeout(now_factory())
        checkpoint_validate_repository_layout(
            repo_root,
            expected_root=intent.repository_root,
            expected_git_common_dir=intent.git_common_dir,
            expected_git_dir=intent.git_dir,
            expected_branch=intent.branch_ref.removeprefix("refs/heads/"),
            context="sequence checkpoint repository identity",
            timeout=timeout,
        )
        timeout = deadline.remaining_timeout(now_factory())
        checkpoint_validate_checked_out_branch(
            repo_root,
            expected_branch=intent.branch_ref.removeprefix("refs/heads/"),
            timeout=timeout,
        )
        timeout = deadline.remaining_timeout(now_factory())
        if checkpoint_git_symbolic_ref(repo_root, "HEAD", timeout=timeout) != intent.branch_ref:
            raise GitCheckpointError("symbolic HEAD ref drift")
        timeout = deadline.remaining_timeout(now_factory())
        current_head = checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout)

        if (
            evidence is not None
            and evidence.commit_sha256
            and current_head == evidence.commit_sha256
        ):
            return self._adopt_already_applied_checkpoint(
                repo_root,
                intent=intent,
                trusted_tree=trusted_tree,
                evidence=evidence,
                identity=identity,
                deadline=deadline,
                now_factory=now_factory,
            )

        if current_head != intent.parent_head:
            raise GitCheckpointError("parent HEAD drift before checkpoint")

        timeout = deadline.remaining_timeout(now_factory())
        patch_bytes = checkpoint_git_diff_cached_patch_bytes(repo_root, timeout=timeout)
        if not patch_bytes:
            raise GitCheckpointError("staged index is empty")
        patch_sha = hashlib.sha256(patch_bytes).hexdigest()
        if patch_sha != intent.reviewed_patch_sha256:
            raise GitCheckpointError("staged patch hash drift")
        if patch_sha != trusted_tree.reviewed_patch_sha256:
            raise GitCheckpointError("trusted reviewed patch hash drift")
        timeout = deadline.remaining_timeout(now_factory())
        checkpoint_validate_staged_patch_matches_artifact(
            repo_root,
            patch_path,
            timeout=timeout,
        )
        timeout = deadline.remaining_timeout(now_factory())
        status = checkpoint_git_status_porcelain(repo_root, timeout=timeout)
        unstaged = paths_with_unstaged_changes(status)
        if unstaged:
            raise GitCheckpointError("tracked unstaged changes block checkpoint")
        untracked = paths_with_untracked(status)
        if untracked:
            raise GitCheckpointError("untracked non-ignored files block checkpoint")

        tree_sha = trusted_tree.reviewed_tree_sha256
        timeout = deadline.remaining_timeout(now_factory())
        live_tree_sha = checkpoint_git_write_tree(repo_root, timeout=timeout)
        if live_tree_sha != tree_sha:
            raise GitCheckpointError("live tree SHA drift from trusted reviewed tree")
        if (
            evidence is not None
            and evidence.tree_sha256 is not None
            and tree_sha != evidence.tree_sha256
        ):
            raise GitCheckpointError("tree SHA drift from checkpoint evidence")
        persist_tree_sha(tree_sha)

        if evidence is not None and evidence.commit_sha256 is not None:
            commit_sha = evidence.commit_sha256
            timeout = deadline.remaining_timeout(now_factory())
            checkpoint_verify_commit_identity(
                repo_root,
                commit_sha=commit_sha,
                tree_sha=tree_sha,
                parent_sha=intent.parent_head,
                message=intent.commit_message,
                identity=identity,
                timeout=timeout,
            )
        else:
            if mutation_fence is not None:
                mutation_fence("pre_commit_tree")
            timeout = deadline.remaining_timeout(now_factory())
            commit_sha = checkpoint_git_commit_tree(
                repo_root,
                tree_sha=tree_sha,
                parent_sha=intent.parent_head,
                message=intent.commit_message,
                identity=identity,
                timeout=timeout,
            )
            persist_commit_sha(commit_sha)

        timeout = deadline.remaining_timeout(now_factory())
        if checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout) == intent.parent_head:
            if authorize_ref_update is not None:
                authorize_ref_update()
            if mutation_fence is not None:
                mutation_fence("pre_update_ref")
            timeout = deadline.remaining_timeout(now_factory())
            checkpoint_git_update_ref_cas(
                repo_root,
                ref=intent.branch_ref,
                new_sha=commit_sha,
                old_sha=intent.parent_head,
                timeout=timeout,
            )
            ref_updated = True
            if on_ref_advanced is not None:
                on_ref_advanced()
        else:
            timeout = deadline.remaining_timeout(now_factory())
            if checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout) != commit_sha:
                raise GitCheckpointError("branch ref moved to unrelated commit")
            ref_updated = False

        timeout = deadline.remaining_timeout(now_factory())
        validate_checkpoint_postconditions(
            repo_root,
            expected_tree=tree_sha,
            timeout=timeout,
        )
        return GitCheckpointCommit(
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            parent_head=intent.parent_head,
            already_applied=False,
            ref_updated=ref_updated,
        )

    def _adopt_already_applied_checkpoint(
        self,
        repo_root: Path,
        *,
        intent: SequenceCheckpointIntent,
        trusted_tree: SequenceCheckpointTrustedTree,
        evidence: SequenceCheckpointEvidence,
        identity: GitIdentity,
        deadline: CheckpointGitDeadline,
        now_factory: Callable[[], datetime],
    ) -> GitCheckpointCommit:
        tree_sha = trusted_tree.reviewed_tree_sha256
        if evidence.tree_sha256 is not None and evidence.tree_sha256 != tree_sha:
            raise GitCheckpointError("checkpoint evidence tree SHA disagrees with trusted tree")
        commit_sha = evidence.commit_sha256
        if commit_sha is None:
            raise GitCheckpointError("checkpoint evidence missing commit SHA")
        timeout = deadline.remaining_timeout(now_factory())
        checkpoint_verify_commit_identity(
            repo_root,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            parent_sha=intent.parent_head,
            message=intent.commit_message,
            identity=identity,
            timeout=timeout,
        )
        timeout = deadline.remaining_timeout(now_factory())
        if checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout) != commit_sha:
            raise GitCheckpointError("branch ref does not point at checkpoint commit")
        timeout = deadline.remaining_timeout(now_factory())
        validate_checkpoint_postconditions(
            repo_root,
            expected_tree=tree_sha,
            timeout=timeout,
        )
        return GitCheckpointCommit(
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            parent_head=intent.parent_head,
            already_applied=True,
            ref_updated=False,
        )


def checkpoint_result_from_commit(
    intent: SequenceCheckpointIntent,
    *,
    commit: GitCheckpointCommit,
    intent_sha256: str,
    trusted_tree_sha256: str,
) -> SequenceCheckpointResult:
    return SequenceCheckpointResult(
        sequence_id=intent.sequence_id,
        predecessor_run_id=intent.predecessor_run_id,
        predecessor_ordinal=intent.predecessor_ordinal,
        successor_run_id=intent.successor_run_id,
        accepted_outcome=intent.accepted_outcome,
        commit_sha256=commit.commit_sha,
        tree_sha256=commit.tree_sha,
        parent_head=commit.parent_head,
        branch_ref=intent.branch_ref,
        reviewed_patch_sha256=intent.reviewed_patch_sha256,
        intent_sha256=intent_sha256,
        trusted_tree_sha256=trusted_tree_sha256,
    )
