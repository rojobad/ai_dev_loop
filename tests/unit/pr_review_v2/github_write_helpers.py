"""Shared fakes and builders for Phase 16.6 write/reconciliation tests.

These helpers construct the seven ``MUTATING`` effects, their claims/tokens, an
authority guard double, and in-memory fakes for the Git and GitHub write
transports. They also provide small utilities to write protected input artifacts
under a hashed run artifact root so the real ``InputArtifactReader`` accepts them.
Only synthetic/sanitized data is used; no real credentials or network.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    GhTransportResult,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ClaimAuthorityResult,
    ClaimAuthoritySnapshot,
    WriteAuthorityStatus,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    PullRequestBinding,
    RepositoryIdentity,
    TransientErrorKind,
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
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.paths import (
    ensure_run_artifact_root,
    resolve_run_relative_path,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
RUN_ID = "run-16-6"
REPO = RepositoryIdentity(name_with_owner="acme/demo")
T0 = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def sha256_hex(data: bytes | str) -> str:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def binding(*, head_sha: str = SHA_B, pr_number: int = 7) -> PullRequestBinding:
    return PullRequestBinding(
        repository=REPO,
        pr_number=pr_number,
        head_branch="feature",
        base_branch="main",
        head_sha=head_sha,
    )


# -- protected artifact writing --------------------------------------------


def write_artifact(artifact_root: Path, run_id: str, relative: str, data: bytes):
    """Write bytes to a protected run-relative artifact and return an ArtifactRef."""

    from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef

    run_root = ensure_run_artifact_root(artifact_root, run_id)
    target = resolve_run_relative_path(run_root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return ArtifactRef(relative_path=relative, sha256=sha256_hex(data))


def write_commit_message(artifact_root: Path, run_id: str, *, subject: str, body: str = ""):
    payload = {
        "schema_name": "ai_dev_loop.pr_review_v2.commit_message",
        "schema_version": 1,
        "subject": subject,
        "body": body,
    }
    data = json.dumps(payload).encode("utf-8")
    return write_artifact(artifact_root, run_id, "artifacts/commit-message.json", data)


def write_publication_text(artifact_root: Path, run_id: str, *, title: str, body: str = ""):
    payload = {
        "schema_name": "ai_dev_loop.pr_review_v2.publication_text",
        "schema_version": 1,
        "title": title,
        "body": body,
    }
    data = json.dumps(payload).encode("utf-8")
    return write_artifact(artifact_root, run_id, "artifacts/publication.json", data)


def write_reply_text(artifact_root: Path, run_id: str, *, text: str, name: str = "reply.txt"):
    return write_artifact(artifact_root, run_id, f"artifacts/{name}", text.encode("utf-8"))


# -- effect builders --------------------------------------------------------


def commit_effect(
    *,
    patch_ref,
    commit_message_ref,
    expected_head_sha: str = SHA_A,
    expected_branch: str = "feature",
    attempt: int = 1,
    run_id: str = RUN_ID,
) -> CommitPatchEffect:
    return CommitPatchEffect(
        effect_id="pr-review:run-16-6:cycle:01:commit_patch",
        idempotency_key="pr-review:run-16-6:cycle:01:commit_patch",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=expected_head_sha,
        patch_ref=patch_ref,
        expected_head_sha=expected_head_sha,
        expected_branch=expected_branch,
        commit_message_ref=commit_message_ref,
    )


def push_effect(
    *,
    commit_sha: str,
    remote_ref: str = "feature",
    expected_remote_sha_before_push: str | None = None,
    attempt: int = 1,
    run_id: str = RUN_ID,
) -> PushCommitEffect:
    return PushCommitEffect(
        effect_id="pr-review:run-16-6:cycle:01:push_commit",
        idempotency_key="pr-review:run-16-6:cycle:01:push_commit",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=commit_sha,
        commit_sha=commit_sha,
        remote_ref=remote_ref,
        expected_remote_sha_before_push=expected_remote_sha_before_push,
        force=False,
    )


def create_pr_effect(*, publication_text_ref, attempt: int = 1, run_id: str = RUN_ID):
    return CreateOrUpdatePrEffect(
        effect_id="pr-review:run-16-6:cycle:01:create_or_update_pr",
        idempotency_key="pr-review:run-16-6:cycle:01:create_or_update_pr",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        head_branch="feature",
        base_branch="main",
        publication_text_ref=publication_text_ref,
    )


def request_review_effect(*, marker: str, attempt: int = 1, run_id: str = RUN_ID):
    return RequestBotReviewEffect(
        effect_id="pr-review:run-16-6:cycle:01:request_bot_review",
        idempotency_key="pr-review:run-16-6:cycle:01:request_bot_review",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        binding=binding(),
        marker=marker,
    )


def post_reply_effect(*, reply_ref, thread_id: str = "THREAD_1", attempt: int = 1, run_id=RUN_ID):
    return PostThreadReplyEffect(
        effect_id="pr-review:run-16-6:cycle:01:post_thread_reply",
        idempotency_key="pr-review:run-16-6:cycle:01:post_thread_reply",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        binding=binding(),
        thread_id=thread_id,
        reply_ref=reply_ref,
    )


def update_pr_text_effect(*, publication_text_ref, attempt: int = 1, run_id: str = RUN_ID):
    return UpdatePrTextEffect(
        effect_id="pr-review:run-16-6:cycle:01:update_pr_text",
        idempotency_key="pr-review:run-16-6:cycle:01:update_pr_text",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        binding=binding(),
        publication_text_ref=publication_text_ref,
    )


def resolve_thread_effect(*, thread_id: str = "THREAD_1", attempt: int = 1, run_id: str = RUN_ID):
    return ResolveThreadEffect(
        effect_id="pr-review:run-16-6:cycle:01:resolve_thread",
        idempotency_key="pr-review:run-16-6:cycle:01:resolve_thread",
        run_id=run_id,
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=REPO,
        bound_head_sha=SHA_B,
        binding=binding(),
        thread_id=thread_id,
    )


def token_for(effect, *, run_version: int = 1, lease_generation: int = 1) -> EffectCompletionToken:
    return EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=run_version,
        lease_generation=lease_generation,
        cycle_number=effect.cycle_number,
        bound_head_sha=effect.bound_head_sha,
    )


def claim_for(effect, *, owner: str = "owner-a", generation: int = 1, run_version: int = 1):
    return EffectClaim(
        dispatch_id="dispatch-1",
        claim_id="claim-1",
        run_id=effect.run_id,
        effect=effect,
        attempt=effect.attempt,
        max_attempts=effect.max_attempts,
        classification="mutating",
        claimed_run_version=run_version,
        owner_id=owner,
        lease_generation=generation,
        claimed_at=T0,
        lease_expires_at=datetime(2026, 7, 21, 12, 5, 0, tzinfo=UTC),
        completion_token=token_for(effect, run_version=run_version, lease_generation=generation),
    )


# -- authority guard double -------------------------------------------------


@dataclass
class FakeAuthority:
    """Records authority checks; can authorize, reject, or reject on Nth call."""

    status: WriteAuthorityStatus = WriteAuthorityStatus.AUTHORIZED
    reject_on_call: int | None = None
    calls: list[ClaimAuthoritySnapshot] = field(default_factory=list)

    def check_authority(self, snapshot: ClaimAuthoritySnapshot) -> ClaimAuthorityResult:
        self.calls.append(snapshot)
        if self.reject_on_call is not None and len(self.calls) >= self.reject_on_call:
            return ClaimAuthorityResult(
                status=WriteAuthorityStatus.REJECTED, safe_summary="authority rejected"
            )
        if self.status is WriteAuthorityStatus.AUTHORIZED:
            return ClaimAuthorityResult(status=WriteAuthorityStatus.AUTHORIZED)
        return ClaimAuthorityResult(
            status=WriteAuthorityStatus.REJECTED, safe_summary="authority rejected"
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)


# -- gh write transport fake ------------------------------------------------


def rest_result(body: Any, *, status: int = 200) -> GhTransportResult:
    return GhTransportResult(
        http_status=status,
        headers=AllowlistedHeaders(),
        body_json=body,
        returncode=0 if status < 400 else 1,
    )


def graphql_result(body: Any) -> GhTransportResult:
    return GhTransportResult(
        http_status=200,
        headers=AllowlistedHeaders(),
        body_json=body,
        returncode=0,
    )


def pr_dict(
    *,
    number: int = 7,
    title: str,
    body: str,
    head_ref: str = "feature",
    base_ref: str = "main",
    head_sha: str = SHA_B,
    state: str = "open",
    full_name: str = "acme/demo",
) -> dict[str, Any]:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "head": {"ref": head_ref, "sha": head_sha, "repo": {"full_name": full_name}},
        "base": {"ref": base_ref},
    }


def comment_connection(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "comments": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def thread_comment_connection(
    nodes: list[dict[str, Any]], *, thread_id: str = "THREAD_1", pr_number: int = 7
) -> dict[str, Any]:
    return {
        "data": {
            "node": {
                "id": thread_id,
                "isResolved": False,
                "pullRequest": {"number": pr_number},
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                },
            }
        }
    }


def thread_resolved_payload(
    *, thread_id: str = "THREAD_1", is_resolved: bool = False, pr_number: int = 7
) -> dict[str, Any]:
    return {
        "data": {
            "node": {
                "id": thread_id,
                "isResolved": is_resolved,
                "pullRequest": {"number": pr_number},
            }
        }
    }


def transient_error(kind: GatewayTransientKind = GatewayTransientKind.HTTP_500) -> GhTransportError:
    tk_map = {
        GatewayTransientKind.HTTP_500: TransientErrorKind.HTTP_500,
        GatewayTransientKind.TIMEOUT: TransientErrorKind.TIMEOUT,
        GatewayTransientKind.CONNECTION_RESET: TransientErrorKind.CONNECTION_RESET,
    }
    return GhTransportError(
        transient=GatewayTransient(
            kind=kind,
            safe_summary="transient gh failure",
            transient_kind=tk_map.get(kind, TransientErrorKind.TEMPORARY_CLI_FAILURE),
        )
    )


def block_error(kind: GatewayBlockKind = GatewayBlockKind.AUTHENTICATION) -> GhTransportError:
    return GhTransportError(block=block_for_kind(kind))


@dataclass
class FakeGhWriteTransport:
    """Fake GhWriteTransport. Program per-method responses; records calls."""

    responses: dict[str, Any] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def _resolve(self, name: str) -> GhTransportResult:
        value = self.responses[name]
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"no more queued responses for {name}")
            value = value.pop(0)
        if isinstance(value, GhTransportError):
            raise value
        if callable(value):
            return value()
        return value

    # -- mutations
    def create_pull_request(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("create_pull_request", kwargs))
        return self._resolve("create_pull_request")

    def update_pull_request(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("update_pull_request", kwargs))
        return self._resolve("update_pull_request")

    def update_pr_text(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("update_pr_text", kwargs))
        return self._resolve("update_pr_text")

    def create_issue_comment(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("create_issue_comment", kwargs))
        return self._resolve("create_issue_comment")

    def add_review_thread_reply(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("add_review_thread_reply", kwargs))
        return self._resolve("add_review_thread_reply")

    def resolve_review_thread(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("resolve_review_thread", kwargs))
        return self._resolve("resolve_review_thread")

    # -- reconciliation reads
    def list_prs_by_head_base(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("list_prs_by_head_base", kwargs))
        return self._resolve("list_prs_by_head_base")

    def fetch_pr_text(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_pr_text", kwargs))
        return self._resolve("fetch_pr_text")

    def fetch_branch_head_sha(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_branch_head_sha", kwargs))
        if "fetch_branch_head_sha" in self.responses:
            return self._resolve("fetch_branch_head_sha")
        # Default: branch tip matches the common test bound SHA.
        return rest_result({"ref": "refs/heads/feature", "object": {"sha": SHA_B}})

    def fetch_thread_comments_page(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_thread_comments_page", kwargs))
        return self._resolve("fetch_thread_comments_page")

    def fetch_thread_resolved(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_thread_resolved", kwargs))
        if "fetch_thread_resolved" in self.responses:
            return self._resolve("fetch_thread_resolved")
        # Default: owned thread on PR #7, unresolved (ownership fail-closed path needs PR).
        return graphql_result(thread_resolved_payload(is_resolved=False))

    def method_names(self) -> list[str]:
        return [name for name, _ in self.calls]


def identity_result(
    *,
    name_with_owner: str = "acme/demo",
    number: int = 7,
    state: str = "OPEN",
    is_cross_repository: bool = False,
    head_branch: str = "feature",
    base_branch: str = "main",
    head_sha: str = SHA_B,
) -> GhTransportResult:
    return graphql_result(
        {
            "data": {
                "repository": {
                    "nameWithOwner": name_with_owner,
                    "pullRequest": {
                        "number": number,
                        "state": state,
                        "isCrossRepository": is_cross_repository,
                        "headRefName": head_branch,
                        "baseRefName": base_branch,
                        "headRefOid": head_sha,
                    },
                }
            }
        }
    )


@dataclass
class FakeReadTransport:
    """Fake GitHubReadTransport supporting only the write-gateway needs."""

    issue_comment_pages: list[GhTransportResult] = field(default_factory=list)
    identity_responses: (
        dict[int, GhTransportResult] | list[GhTransportResult] | GhTransportResult | None
    ) = None
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def fetch_pull_request_identity(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_pull_request_identity", kwargs))
        number = int(kwargs.get("number") or 0)
        responses = self.identity_responses
        if responses is None:
            return identity_result(number=number or 7)
        if isinstance(responses, GhTransportResult):
            return responses
        if isinstance(responses, list):
            if not responses:
                raise AssertionError("no identity responses queued")
            return responses.pop(0)
        if number in responses:
            return responses[number]
        return identity_result(number=number)

    def fetch_issue_comments_page(self, **kwargs: Any) -> GhTransportResult:
        self.calls.append(("fetch_issue_comments_page", kwargs))
        if not self.issue_comment_pages:
            raise AssertionError("no issue comment pages queued")
        return self.issue_comment_pages.pop(0)


# -- scripted git process runner -------------------------------------------


@dataclass
class GitCall:
    args: list[str]
    stdin_text: str | None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class ScriptedGitRunner:
    """Fake GitProcessRunner keyed by exact argv tails (after the git command)."""

    scripts: list[tuple[tuple[str, ...], Any]] = field(default_factory=list)
    calls: list[GitCall] = field(default_factory=list)
    forbidden_tokens: tuple[str, ...] = ("--force", "--force-with-lease")

    def add(
        self,
        tail: tuple[str, ...],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        self.scripts.append((tail, (returncode, stdout, stderr, timed_out)))

    def run(self, args, *, cwd, timeout, env, stdin_text=None):
        from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitProcessOutcome

        args = list(args)
        self.calls.append(GitCall(args=args, stdin_text=stdin_text, env=dict(env)))
        joined = " ".join(args)
        for token in self.forbidden_tokens:
            assert token not in joined, f"forbidden git token used: {token}"
        # Match argv after the executable so both ``git …`` and ``ssh``/``ssh-add`` work.
        tail = tuple(args[1:])
        for match, (rc, out, err, to) in self.scripts:
            if tail == match:
                return GitProcessOutcome(
                    returncode=rc, stdout=out, stderr=err, timed_out=to, argv=tuple(args)
                )
        raise AssertionError(f"no scripted git response for argv tail: {tail}")

    def run_bytes(self, args, *, cwd, timeout, env):
        outcome = self.run(args, cwd=cwd, timeout=timeout, env=env, stdin_text=None)
        return (
            outcome.returncode,
            outcome.stdout.encode("utf-8"),
            outcome.stderr.encode("utf-8"),
            outcome.timed_out,
        )


# -- real temp git repositories --------------------------------------------


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    base_env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(repo),
    }
    if env:
        base_env.update(env)
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=base_env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@dataclass
class TempGitRepo:
    work: Path
    remote: Path
    branch: str

    def head_sha(self) -> str:
        return _git(self.work, "rev-parse", "HEAD").strip()

    def remote_sha(self, ref: str = "feature") -> str | None:
        out = _git(self.work, "ls-remote", "origin", f"refs/heads/{ref}").strip()
        if not out:
            return None
        return out.split("\t")[0].strip()

    def staged_patch_bytes(self) -> bytes:
        out = _git(self.work, "diff", "--cached", "--binary")
        return out.encode("utf-8")


def make_temp_git_repo(tmp_path: Path, *, branch: str = "feature") -> TempGitRepo:
    """Create a work repo on ``branch`` plus a bare ``origin`` remote (no push yet)."""

    remote = tmp_path / "remote.git"
    remote.mkdir(parents=True, exist_ok=True)
    _git(remote, "init", "--bare", "--initial-branch", branch)

    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    _git(work, "init", "--initial-branch", branch)
    (work / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "add", "README.md")
    _git(work, "commit", "-m", "initial commit")
    _git(work, "remote", "add", "origin", str(remote))
    return TempGitRepo(work=work, remote=remote, branch=branch)


def stage_change(repo: TempGitRepo, *, filename: str = "feature.txt", content: str = "change\n"):
    (repo.work / filename).write_text(content, encoding="utf-8")
    _git(repo.work, "add", filename)


__all__ = [
    "FakeAuthority",
    "FakeGhWriteTransport",
    "FakeReadTransport",
    "GitCall",
    "REPO",
    "RUN_ID",
    "SHA_A",
    "SHA_B",
    "SHA_C",
    "ScriptedGitRunner",
    "T0",
    "TempGitRepo",
    "binding",
    "block_error",
    "claim_for",
    "comment_connection",
    "commit_effect",
    "create_pr_effect",
    "graphql_result",
    "make_temp_git_repo",
    "post_reply_effect",
    "pr_dict",
    "push_effect",
    "request_review_effect",
    "resolve_thread_effect",
    "rest_result",
    "sha256_hex",
    "stage_change",
    "thread_comment_connection",
    "token_for",
    "transient_error",
    "update_pr_text_effect",
    "write_artifact",
    "write_commit_message",
    "write_publication_text",
    "write_reply_text",
]
