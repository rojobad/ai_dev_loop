"""Bounded Git commit and non-force push for accepted post-PR publications."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ai_dev_loop.errors import AiDevLoopError, SshAgentNoIdentityError, ValidationError
from ai_dev_loop.process import ProcessResult, require_success, run_process
from ai_dev_loop.runners.git import discover_repository, validate_staged_patch_matches_artifact
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


def _token_looks_like_option(token: str) -> bool:
    return token.startswith("-")


def _destination_has_unsafe_characters(destination: str) -> bool:
    if not destination:
        return True
    for char in destination:
        if char in {"\0", "\n", "\r", " ", "\t", "\f", "\v"}:
            return True
        if ord(char) < 32:
            return True
    return False


def _validate_ssh_destination(destination: str) -> str:
    """Return a single argv-safe ssh destination, or raise ValidationError."""

    if _destination_has_unsafe_characters(destination):
        raise ValidationError("SSH remote destination is empty or contains unsafe characters")
    if _token_looks_like_option(destination):
        raise ValidationError("SSH remote destination looks like an option")
    if "@" in destination:
        _user, host = destination.rsplit("@", 1)
        if _token_looks_like_option(host):
            raise ValidationError("SSH remote destination looks like an option")
    return destination


def _ssh_destination_from_scp_url(url: str) -> str:
    # git@alias:owner/repo.git — preserve the Host alias, not a resolved hostname.
    rest = url[len("git@") :]
    if ":" not in rest:
        raise ValidationError("malformed SCP SSH remote URL")
    host, path = rest.split(":", 1)
    if not host or not path or path.startswith("/"):
        raise ValidationError("malformed SCP SSH remote URL")
    if "/" in host or "@" in host:
        raise ValidationError("malformed SCP SSH remote URL")
    if _token_looks_like_option(host):
        raise ValidationError("SSH remote destination looks like an option")
    return _validate_ssh_destination(f"git@{host}")


def _ssh_destination_from_ssh_url(url: str) -> str:
    # ssh://user@alias[:port]/owner/repo.git — parse authority without lowercasing.
    rest = url[len("ssh://") :]
    if "/" not in rest:
        raise ValidationError("malformed SSH remote URL")
    authority, path = rest.split("/", 1)
    if not authority or not path:
        raise ValidationError("malformed SSH remote URL")
    if authority.startswith("[") or _token_looks_like_option(authority):
        raise ValidationError("unsupported SSH remote URL authority")

    user: str | None
    hostport: str
    if "@" in authority:
        user, hostport = authority.rsplit("@", 1)
        if not user or not hostport or "@" in user:
            raise ValidationError("malformed SSH remote URL")
        if _token_looks_like_option(user):
            raise ValidationError("SSH remote destination looks like an option")
    else:
        user = None
        hostport = authority

    if ":" in hostport:
        host, port = hostport.rsplit(":", 1)
        if not host or not port.isdigit():
            raise ValidationError("malformed SSH remote URL")
    else:
        host = hostport
    if not host or "/" in host:
        raise ValidationError("malformed SSH remote URL")
    if _token_looks_like_option(host):
        raise ValidationError("SSH remote destination looks like an option")

    destination = f"{user}@{host}" if user else host
    return _validate_ssh_destination(destination)


def ssh_destination_from_remote_url(url: str) -> str:
    """Build the single ``ssh -G`` destination for a supported Git SSH remote."""

    if url.startswith("https://") or url.startswith("http://"):
        raise ValidationError(
            "GitHub publication requires an SSH remote URL; "
            "configure the remote for SSH and preload ssh-agent"
        )
    if url.startswith("git@"):
        return _ssh_destination_from_scp_url(url)
    if url.startswith("ssh://"):
        return _ssh_destination_from_ssh_url(url)
    raise ValidationError(f"unsupported remote URL scheme for publication: {url[:32]}")


def _is_usable_agent_socket(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _parse_identity_agent_from_ssh_g(stdout: str) -> str | None:
    """Return a validated IdentityAgent socket path, or None for absent/none.

    Ambiguous, relative, missing, or non-socket values raise ValidationError.
    Does not embed ``ssh -G`` output or socket paths in the error message.
    """

    values: list[str] = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        key, separator, remainder = line.partition(" ")
        if key.lower() != "identityagent":
            continue
        if not separator:
            values.append("")
            continue
        values.append(remainder.strip())

    if not values:
        return None
    if len(values) > 1:
        raise ValidationError("ambiguous IdentityAgent in effective SSH configuration")

    value = values[0]
    if not value or value.lower() == "none":
        return None

    path = Path(value)
    if not path.is_absolute():
        raise ValidationError("effective IdentityAgent must be an absolute socket path")
    if not _is_usable_agent_socket(path):
        raise ValidationError("effective IdentityAgent is not a usable SSH agent socket")
    return str(path)


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
    if identity_agent is not None:
        return identity_agent

    inherited = os.environ.get("SSH_AUTH_SOCK", "").strip()
    if inherited and _is_usable_agent_socket(Path(inherited)):
        return inherited

    raise SshAgentNoIdentityError(
        "ssh-agent has no usable keys; preload the SSH key before publication"
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
