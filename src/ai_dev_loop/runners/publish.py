"""Bounded Git commit and non-force push for accepted post-PR publications."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import AiDevLoopError, SshAgentNoIdentityError, ValidationError
from ai_dev_loop.process import ProcessResult, require_success, run_process
from ai_dev_loop.runners.git import discover_repository, validate_staged_patch_matches_artifact
from ai_dev_loop.ssh_agent import (
    choose_effective_ssh_auth_sock,
    ssh_destination_from_remote_url,
)
from ai_dev_loop.ssh_agent import (
    parse_identity_agent_from_ssh_g as _parse_identity_agent_from_ssh_g,
)
from ai_dev_loop.ssh_agent import (
    validate_ssh_destination as _validate_ssh_destination,
)
from ai_dev_loop.state import FULL_SHA_PATTERN, sha256_text


@dataclass(frozen=True)
class PublicationText:
    commit_subject: str
    commit_body: str
    pr_title: str
    pr_body: str


@dataclass(frozen=True)
class PublishResult:
    commit_sha: str
    remote_name: str
    remote_ref: str
    staged_patch_sha256: str
    expected_remote_sha_before_push: str | None
    resumed_existing_commit: bool


def _git(args: list[str], *, cwd: Path) -> ProcessResult:
    return run_process(["git", *args], cwd=str(cwd))


def read_staged_patch(repo_root: Path) -> str:
    return require_success(
        _git(["diff", "--cached", "--binary"], cwd=repo_root),
        context="git staged patch",
    )


def validate_clean_except_staged(repo_root: Path) -> str:
    """Require a non-empty staged index and no unstaged/untracked work."""

    info = discover_repository(repo_root)
    if not info.staged_paths:
        raise ValidationError("publication requires a non-empty staged index")
    for line in info.status_porcelain.splitlines():
        if not line.strip():
            continue
        if line.startswith("?"):
            raise ValidationError("publication refused: untracked files present")
        if line.startswith("1 ") or line.startswith("2 "):
            parts = line.split(" ", 2)
            if len(parts) >= 2 and len(parts[1]) >= 2 and parts[1][1] not in {".", " "}:
                raise ValidationError("publication refused: unstaged tracked changes present")
    patch = read_staged_patch(repo_root)
    if not patch.strip():
        raise ValidationError("publication refused: staged patch is empty")
    return patch


def publication_staged_patch_fingerprint(repo_root: Path, patch_artifact: Path) -> str:
    """Return the raw live staged-patch sha256 used by publication checkpoints.

    Requires a clean-except-staged index and normalized equivalence with the
    durable iteration artifact (``normalize_patch_text``: CRLF and trailing
    newlines only). Never substitutes ``sha256_file(artifact)`` for the live
    fingerprint that ``publish_accepted_staged_patch`` will recompute.
    """

    live_patch = validate_clean_except_staged(repo_root)
    validate_staged_patch_matches_artifact(repo_root, patch_artifact)
    return sha256_text(live_patch)


def validate_clean_worktree(repo_root: Path) -> None:
    info = discover_repository(repo_root)
    if info.status_porcelain.strip():
        raise ValidationError("publication resume requires a clean worktree")
    if info.staged_paths:
        raise ValidationError("publication resume refused: unexpected staged index")


def resolve_upstream(repo_root: Path, branch: str) -> tuple[str, str]:
    """Return (remote_name, remote_branch) for the branch upstream, or configured origin."""

    upstream = _git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], cwd=repo_root)
    if upstream.returncode == 0 and upstream.stdout.strip():
        ref = upstream.stdout.strip()
        if "/" not in ref:
            raise ValidationError(f"invalid upstream ref: {ref}")
        remote, remote_branch = ref.split("/", 1)
        return remote, remote_branch
    remotes = require_success(_git(["remote"], cwd=repo_root), context="git remote")
    remote_names = [line.strip() for line in remotes.splitlines() if line.strip()]
    if "origin" not in remote_names:
        raise ValidationError("no upstream configured and origin remote is missing")
    return "origin", branch


def expected_remote_head(repo_root: Path, remote: str, remote_branch: str) -> str | None:
    result = _git(["ls-remote", "--heads", remote, remote_branch], cwd=repo_root)
    if result.returncode != 0:
        raise AiDevLoopError("failed to query remote head via git ls-remote")
    line = result.stdout.strip().splitlines()
    if not line:
        return None
    sha = line[0].split()[0].strip()
    if not FULL_SHA_PATTERN.match(sha):
        raise ValidationError("remote head SHA is invalid")
    return sha


def query_effective_identity_agent(destination: str, *, cwd: Path) -> str | None:
    """Resolve IdentityAgent via ``ssh -G`` for the remote destination."""

    safe_destination = _validate_ssh_destination(destination)
    result = run_process(["ssh", "-G", safe_destination], cwd=str(cwd), timeout=10.0)
    if result.returncode != 0 or result.timed_out:
        raise ValidationError(
            "unable to resolve effective SSH configuration for the publication remote"
        )
    return _parse_identity_agent_from_ssh_g(result.stdout)


def resolve_effective_ssh_auth_sock(repo_root: Path, remote_url: str) -> str:
    """Choose the agent socket OpenSSH would use for the remote.

    A valid effective IdentityAgent wins over inherited ``SSH_AUTH_SOCK``.
    When IdentityAgent is absent or ``none``, a valid inherited socket is used.
    Missing/unusable sockets raise ``SshAgentNoIdentityError``. Remote or
    configuration shape problems raise ``ValidationError``.
    """

    destination = ssh_destination_from_remote_url(remote_url)
    identity_agent = query_effective_identity_agent(destination, cwd=repo_root)
    return choose_effective_ssh_auth_sock(
        identity_agent=identity_agent,
        inherited_ssh_auth_sock=os.environ.get("SSH_AUTH_SOCK"),
    )


def verify_ssh_push_ready(repo_root: Path, remote: str = "origin") -> None:
    """Fail closed when the remote is not SSH or ssh-agent has no keys.

    Resolves the effective OpenSSH IdentityAgent for the remote via ``ssh -G``
    and runs ``ssh-add -l`` with only that ``SSH_AUTH_SOCK``. Remote URL and
    unsafe SSH configuration problems remain ``ValidationError``. Missing
    ssh-agent identity raises the typed ``SshAgentNoIdentityError`` so
    publication can interrupt without classifying every validation failure as
    recoverable.
    """

    url = require_success(
        _git(["remote", "get-url", remote], cwd=repo_root),
        context="git remote url",
    ).strip()
    sock = resolve_effective_ssh_auth_sock(repo_root, url)
    env = dict(os.environ)
    env["SSH_AUTH_SOCK"] = sock
    agent = run_process(["ssh-add", "-l"], cwd=str(repo_root), timeout=10.0, env=env)
    if agent.returncode != 0:
        # Do not embed ssh-add stdout/stderr; the typed outcome is enough.
        raise SshAgentNoIdentityError(
            "ssh-agent has no usable keys; preload the SSH key before publication"
        )


def commit_staged_patch(
    repo_root: Path,
    *,
    subject: str,
    body: str,
) -> str:
    if not subject.strip():
        raise ValidationError("commit subject must not be empty")
    if "\n" in subject.strip():
        raise ValidationError("commit subject must be a single line")
    message = subject.strip()
    if body.strip():
        message = f"{message}\n\n{body.strip()}\n"
    result = _git(["commit", "--message", message], cwd=repo_root)
    if result.returncode != 0:
        raise AiDevLoopError("git commit failed for accepted staged patch")
    sha = require_success(
        _git(["rev-parse", "HEAD"], cwd=repo_root), context="git head after commit"
    )
    if not FULL_SHA_PATTERN.match(sha):
        raise ValidationError("commit SHA is invalid")
    return sha


def push_branch_non_force(
    repo_root: Path,
    *,
    remote: str,
    local_branch: str,
    remote_branch: str,
    expected_remote_sha: str | None,
) -> None:
    current_remote = expected_remote_head(repo_root, remote, remote_branch)
    if expected_remote_sha is None:
        if current_remote is not None:
            raise ValidationError(
                "remote branch unexpectedly exists; refusing non-force push without baseline"
            )
    elif current_remote != expected_remote_sha:
        raise ValidationError("remote head advanced unexpectedly; refusing non-force push")
    result = _git(
        ["push", remote, f"{local_branch}:{remote_branch}"],
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise AiDevLoopError("git push failed; repository left with local commit")
    local_head = require_success(_git(["rev-parse", "HEAD"], cwd=repo_root), context="local head")
    remote_after = expected_remote_head(repo_root, remote, remote_branch)
    if remote_after != local_head:
        raise ValidationError(
            "remote head does not match local HEAD after push; stopping before GitHub writes"
        )


def publish_accepted_staged_patch(
    repo_root: Path,
    *,
    branch: str,
    text: PublicationText,
    resume_from_commit: str | None = None,
    expected_remote_sha_before_push: str | None = None,
    staged_patch_sha256: str | None = None,
    after_commit: Callable[[str, str | None, str, str], None] | None = None,
) -> PublishResult:
    """Commit (unless resuming) and non-force push the accepted staged patch.

    ``after_commit`` is invoked immediately after a successful new commit (not on
    resume) so callers can persist ``local_commit_sha`` before push.
    """

    remote, remote_branch = resolve_upstream(repo_root, branch)
    verify_ssh_push_ready(repo_root, remote)
    resumed = False

    if resume_from_commit is not None:
        if not FULL_SHA_PATTERN.match(resume_from_commit):
            raise ValidationError("resume_from_commit must be a full commit SHA")
        if staged_patch_sha256 is None:
            raise ValidationError("staged_patch_sha256 is required when resuming publication")
        validate_clean_worktree(repo_root)
        head = require_success(_git(["rev-parse", "HEAD"], cwd=repo_root), context="local head")
        if head != resume_from_commit:
            raise ValidationError(
                "local HEAD does not match recorded publication commit; refusing duplicate commit"
            )
        commit_sha = resume_from_commit
        patch_sha = staged_patch_sha256
        expected = expected_remote_sha_before_push
        resumed = True
    else:
        patch = validate_clean_except_staged(repo_root)
        patch_sha = sha256_text(patch)
        expected = expected_remote_head(repo_root, remote, remote_branch)
        commit_sha = commit_staged_patch(
            repo_root,
            subject=text.commit_subject,
            body=text.commit_body,
        )
        if after_commit is not None:
            after_commit(commit_sha, expected, remote, remote_branch)

    push_branch_non_force(
        repo_root,
        remote=remote,
        local_branch=branch,
        remote_branch=remote_branch,
        expected_remote_sha=expected,
    )
    return PublishResult(
        commit_sha=commit_sha,
        remote_name=remote,
        remote_ref=f"{remote}/{remote_branch}",
        staged_patch_sha256=patch_sha,
        expected_remote_sha_before_push=expected,
        resumed_existing_commit=resumed,
    )
