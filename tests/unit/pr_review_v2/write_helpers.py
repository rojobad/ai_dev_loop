"""Shared fakes/builders for Phase 16.6 write and reconciliation tests."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GatewayBlockKind,
    GhTransportResult,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ClaimAuthorityResult,
    ClaimAuthoritySnapshot,
    CommitMessageArtifact,
    GitHubWritePolicy,
    GitWritePolicy,
    PublicationTextArtifact,
    RemoteRefObservation,
    WriteAuthorityStatus,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    EffectCompletionToken,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    PostThreadReplyEffect,
    PushCommitEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    UpdatePrTextEffect,
)
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import (
    GitProcessOutcome,
    GitTransportError,
)
from ai_dev_loop.pr_review_v2.infrastructure.input_artifacts import InputArtifactReader
from ai_dev_loop.pr_review_v2.infrastructure.paths import ensure_run_artifact_root

REPO = RepositoryIdentity(name_with_owner="acme/demo")
RUN_ID = "run-1"
SHA_PARENT = "a" * 40
SHA_COMMIT = "b" * 40
SHA_OTHER = "c" * 40
NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def sha256_hex(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def write_artifact(root: Path, run_id: str, relative: str, data: bytes) -> ArtifactRef:
    run_root = ensure_run_artifact_root(root, run_id)
    target = run_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    os.chmod(target, 0o600)
    return ArtifactRef(relative_path=relative, sha256=sha256_hex(data))


def write_commit_message(root: Path, run_id: str, subject: str, body: str = "") -> ArtifactRef:
    artifact = CommitMessageArtifact(subject=subject, body=body)
    data = artifact.model_dump_json().encode("utf-8")
    return write_artifact(root, run_id, "artifacts/commit-message.json", data)


def write_publication(root: Path, run_id: str, title: str, body: str = "") -> ArtifactRef:
    artifact = PublicationTextArtifact(title=title, body=body)
    data = artifact.model_dump_json().encode("utf-8")
    return write_artifact(root, run_id, "artifacts/publication.json", data)


def write_reply(root: Path, run_id: str, text: str) -> ArtifactRef:
    return write_artifact(root, run_id, "artifacts/reply.txt", text.encode("utf-8"))


def input_reader(root: Path) -> InputArtifactReader:
    return InputArtifactReader(root)


def git_policy(repo_cwd: str, **overrides) -> GitWritePolicy:
    base = {
        "repository_cwd": repo_cwd,
        "remote_scheme": "local",
        "require_ssh_agent_identity": False,
    }
    base.update(overrides)
    return GitWritePolicy(**base)


def github_policy(**overrides) -> GitHubWritePolicy:
    base = {"repository_cwd": "/tmp/repo"}
    base.update(overrides)
    return GitHubWritePolicy(**base)


def commit_effect(
    patch_ref: ArtifactRef, message_ref: ArtifactRef, **overrides
) -> CommitPatchEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:commit_patch",
        idempotency_key="pr-review:run-1:cycle:01:commit_patch",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_PARENT,
        patch_ref=patch_ref,
        expected_head_sha=SHA_PARENT,
        expected_branch="feature",
        commit_message_ref=message_ref,
    )
    base.update(overrides)
    return CommitPatchEffect(**base)


def push_effect(**overrides) -> PushCommitEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:push_commit",
        idempotency_key="pr-review:run-1:cycle:01:push_commit",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_PARENT,
        commit_sha=SHA_COMMIT,
        remote_ref="feature",
        expected_remote_sha_before_push=None,
    )
    base.update(overrides)
    return PushCommitEffect(**base)


def binding(head_sha: str = SHA_COMMIT) -> PullRequestBinding:
    return PullRequestBinding(
        repository=REPO,
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha=head_sha,
    )


def create_pr_effect(publication_ref: ArtifactRef, **overrides) -> CreateOrUpdatePrEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:create_or_update_pr",
        idempotency_key="pr-review:run-1:cycle:01:create_or_update_pr",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_COMMIT,
        head_branch="feature",
        base_branch="main",
        publication_text_ref=publication_ref,
    )
    base.update(overrides)
    return CreateOrUpdatePrEffect(**base)


def trigger_effect(marker: str, **overrides) -> RequestBotReviewEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:request_bot_review",
        idempotency_key="pr-review:run-1:cycle:01:request_bot_review",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_COMMIT,
        binding=binding(),
        marker=marker,
    )
    base.update(overrides)
    return RequestBotReviewEffect(**base)


def reply_effect(reply_ref: ArtifactRef, **overrides) -> PostThreadReplyEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:post_thread_reply:t1",
        idempotency_key="pr-review:run-1:cycle:01:post_thread_reply:t1",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_COMMIT,
        binding=binding(),
        thread_id="THREAD_1",
        reply_ref=reply_ref,
    )
    base.update(overrides)
    return PostThreadReplyEffect(**base)


def update_pr_text_effect(publication_ref: ArtifactRef, **overrides) -> UpdatePrTextEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:update_pr_text",
        idempotency_key="pr-review:run-1:cycle:01:update_pr_text",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_COMMIT,
        binding=binding(),
        publication_text_ref=publication_ref,
    )
    base.update(overrides)
    return UpdatePrTextEffect(**base)


def resolve_thread_effect(**overrides) -> ResolveThreadEffect:
    base = dict(
        effect_id="pr-review:run-1:cycle:01:resolve_thread:t1",
        idempotency_key="pr-review:run-1:cycle:01:resolve_thread:t1",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_COMMIT,
        binding=binding(),
        thread_id="THREAD_1",
    )
    base.update(overrides)
    return ResolveThreadEffect(**base)


def reconcile_effect(original, **overrides):
    from ai_dev_loop.pr_review_v2.application.write_contracts import build_reconciliation_identity
    from ai_dev_loop.pr_review_v2.domain.effects import (
        ReconcileWriteEffect,
        strategy_for_mutating_effect,
    )

    strategy = strategy_for_mutating_effect(original)
    base = dict(
        effect_id=f"{original.effect_id}:reconcile",
        idempotency_key=f"{original.idempotency_key}:reconcile",
        run_id=original.run_id,
        cycle_number=original.cycle_number,
        attempt=1,
        max_attempts=original.max_attempts,
        repository=original.repository,
        bound_head_sha=original.bound_head_sha,
        original_write=original,
        strategy=strategy,
        reconciliation_identity=build_reconciliation_identity(
            run_id=original.run_id,
            effect_id=original.effect_id,
            attempt=original.attempt,
            strategy=strategy,
        ),
    )
    base.update(overrides)
    return ReconcileWriteEffect(**base)


def token_for(effect) -> EffectCompletionToken:
    return EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=effect.cycle_number,
        bound_head_sha=effect.bound_head_sha,
    )


def claim_for(effect) -> EffectClaim:
    return EffectClaim(
        dispatch_id="dispatch-1",
        claim_id="claim-1",
        run_id=effect.run_id,
        effect=effect,
        attempt=effect.attempt,
        max_attempts=effect.max_attempts,
        classification="mutating",
        claimed_run_version=1,
        owner_id="owner-1",
        lease_generation=1,
        claimed_at=NOW,
        lease_expires_at=NOW,
        completion_token=token_for(effect),
    )


@dataclass
class FixedAuthority:
    status: WriteAuthorityStatus = WriteAuthorityStatus.AUTHORIZED
    calls: int = 0

    def check_authority(self, snapshot: ClaimAuthoritySnapshot) -> ClaimAuthorityResult:
        self.calls += 1
        return ClaimAuthorityResult(
            status=self.status,
            safe_summary=None if self.status is WriteAuthorityStatus.AUTHORIZED else "rejected",
        )


@dataclass
class FakeGitTransport:
    """In-memory stand-in for GitWriteTransport used by gateway unit tests."""

    root: str
    branch: str = "feature"
    head: str = SHA_PARENT
    status: str = ""
    staged: bytes = b""
    remote_url: str = "git@github.com:acme/demo.git"
    remote_shas: dict[str, str | None] = field(default_factory=dict)
    parents: dict[str, tuple[str, ...]] = field(default_factory=dict)
    messages: dict[str, str] = field(default_factory=dict)
    commit_patches: dict[tuple[str, str], bytes] = field(default_factory=dict)
    ancestors: set[tuple[str, str]] = field(default_factory=set)
    ssh_ok: bool = True
    commit_result: GitProcessOutcome | None = None
    push_result: GitProcessOutcome | None = None
    commits: list[str] = field(default_factory=list)
    pushes: list[str] = field(default_factory=list)
    ssh_prepare_urls: list[str] = field(default_factory=list)

    def inspect_repository_root(self) -> str:
        return self.root

    def read_current_branch(self) -> str:
        return self.branch

    def read_head_sha(self) -> str:
        return self.head

    def read_status_porcelain(self) -> str:
        return self.status

    def read_staged_patch_bytes(self) -> bytes:
        return self.staged

    def read_commit_message_raw(self, sha: str) -> str:
        return self.messages.get(sha, "")

    def read_commit_parents(self, sha: str) -> tuple[str, ...]:
        return self.parents.get(sha, ())

    def read_commit_patch_bytes(self, base_sha: str, commit_sha: str) -> bytes:
        return self.commit_patches.get((base_sha, commit_sha), b"")

    def read_remote_url(self, remote_name: str) -> str:
        return self.remote_url

    def read_remote_ref(self, remote_name: str, remote_ref: str) -> RemoteRefObservation:
        ref = remote_ref if remote_ref.startswith("refs/heads/") else f"refs/heads/{remote_ref}"
        return RemoteRefObservation(
            remote_name=remote_name,
            remote_ref=ref,
            sha=self.remote_shas.get(ref, self.remote_shas.get(remote_ref)),
            complete=True,
        )

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        return (ancestor_sha, descendant_sha) in self.ancestors

    def prepare_ssh_agent_for_remote(self, remote_url: str) -> None:
        self.ssh_prepare_urls.append(remote_url)
        if self.ssh_ok:
            return
        raise GitTransportError(
            block=block_for_kind(
                GatewayBlockKind.AUTHENTICATION,
                detail="no usable SSH agent identity for push",
            )
        )

    def commit_with_message_stdin(self, message: str) -> GitProcessOutcome:
        self.commits.append(message)
        return self.commit_result or GitProcessOutcome(
            returncode=0, stdout="", stderr="", timed_out=False, argv=("git", "commit")
        )

    def push_non_force(
        self, *, remote_name: str, commit_sha: str, remote_ref: str
    ) -> GitProcessOutcome:
        self.pushes.append(commit_sha)
        outcome = self.push_result or GitProcessOutcome(
            returncode=0, stdout="", stderr="", timed_out=False, argv=("git", "push")
        )
        if outcome.returncode == 0 and not outcome.timed_out:
            ref = remote_ref if remote_ref.startswith("refs/heads/") else f"refs/heads/{remote_ref}"
            self.remote_shas[ref] = commit_sha
        return outcome


def gh_result(body: object, *, status: int = 200) -> GhTransportResult:
    return GhTransportResult(
        http_status=status,
        headers=AllowlistedHeaders(),
        body_json=body,
        returncode=0,
    )
