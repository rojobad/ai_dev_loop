"""Direct argv Git/SSH read+write transport for PR review v2.

Only predefined operations are exposed; there is no generic ``git`` escape hatch.
Every operation uses direct argv, ``shell=False``, an explicit cwd/timeout, a
minimal allowlisted environment, and process-group termination on timeout. The
commit message is delivered via stdin (``git commit --file -``). Push is verified
to be an explicit non-force refspec. SSH socket paths and auth values are never
logged or embedded in errors.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_dev_loop.errors import AiDevLoopError, SshAgentNoIdentityError, ValidationError
from ai_dev_loop.pr_review_v2.application.github_read import (
    GatewayBlock,
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    RemoteRefObservation,
    assert_no_force_argv,
)
from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind
from ai_dev_loop.process import run_process_bytes, run_process_streaming
from ai_dev_loop.ssh_agent import (
    choose_effective_ssh_auth_sock,
    parse_identity_agent_from_ssh_g,
    ssh_destination_from_remote_url,
)

_ALLOWLISTED_ENV_KEYS = (
    "PATH",
    "HOME",
    "SSH_AUTH_SOCK",
    "GIT_SSH_COMMAND",
    "LANG",
    "LC_ALL",
    "GIT_TERMINAL_PROMPT",
)
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SSH_NWO_RE = re.compile(r"^(?:ssh://)?[^@/]+@[^:/]+[:/](?P<nwo>[^/]+/[^/]+?)(?:\.git)?/?$")


class GitTransportError(Exception):
    """Private Git transport error carrying a typed block or transient."""

    def __init__(
        self, *, block: GatewayBlock | None = None, transient: GatewayTransient | None = None
    ) -> None:
        if (block is None) == (transient is None):
            raise ValueError("exactly one of block or transient is required")
        self.block = block
        self.transient = transient
        summary = block.safe_summary if block is not None else transient.safe_summary  # type: ignore[union-attr]
        super().__init__(summary)


@dataclass(frozen=True)
class GitProcessOutcome:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    argv: tuple[str, ...]


@runtime_checkable
class GitProcessRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str],
        stdin_text: str | None = None,
    ) -> GitProcessOutcome: ...

    def run_bytes(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str],
    ) -> tuple[int, bytes, bytes, bool]: ...


class DefaultGitProcessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str],
        stdin_text: str | None = None,
    ) -> GitProcessOutcome:
        # Always use the streaming helper so every Git subprocess (including push
        # with no stdin) runs in a terminated/reaped process group.
        try:
            result = run_process_streaming(
                list(args),
                cwd=cwd,
                timeout=timeout,
                env=dict(env),
                stdin_text=stdin_text,
            )
            return GitProcessOutcome(
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                timed_out=result.timed_out,
                argv=tuple(args),
            )
        except AiDevLoopError as exc:
            if "executable not found" in str(exc).lower():
                raise GitTransportError(
                    block=block_for_kind(GatewayBlockKind.MISSING_EXECUTABLE)
                ) from exc
            raise GitTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                    safe_summary="temporary git CLI failure",
                    transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                )
            ) from exc

    def run_bytes(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str],
    ) -> tuple[int, bytes, bytes, bool]:
        try:
            result = run_process_bytes(list(args), cwd=cwd, timeout=timeout, env=dict(env))
            return result.returncode, result.stdout, result.stderr, result.timed_out
        except AiDevLoopError as exc:
            if "executable not found" in str(exc).lower():
                raise GitTransportError(
                    block=block_for_kind(GatewayBlockKind.MISSING_EXECUTABLE)
                ) from exc
            raise GitTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                    safe_summary="temporary git CLI failure",
                    transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                )
            ) from exc


def build_minimal_git_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(base) if base is not None else dict(os.environ)
    env: dict[str, str] = {}
    for key in _ALLOWLISTED_ENV_KEYS:
        if key in source and source[key]:
            env[key] = source[key]
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


class GitWriteTransport:
    """Predefined Git operations behind an injectable process runner."""

    def __init__(
        self,
        *,
        repository_cwd: str,
        git_command: str = "git",
        ssh_command: str = "ssh",
        per_call_timeout_seconds: float,
        runner: GitProcessRunner | None = None,
        env: Mapping[str, str] | None = None,
        timeout_provider: Callable[[], float] | None = None,
    ) -> None:
        self._cwd = repository_cwd
        self._git = git_command
        self._ssh = ssh_command
        self._timeout = per_call_timeout_seconds
        self._runner = runner or DefaultGitProcessRunner()
        self._env = build_minimal_git_env(env)
        self._timeout_provider = timeout_provider

    def set_timeout_provider(self, provider: Callable[[], float] | None) -> None:
        """Inject a remaining-budget timeout provider (gateway overall deadline)."""

        self._timeout_provider = provider

    def _call_timeout(self) -> float:
        if self._timeout_provider is not None:
            return float(self._timeout_provider())
        return float(self._timeout)

    # -- helpers ---------------------------------------------------------

    def _run(self, args: Sequence[str], *, stdin_text: str | None = None) -> GitProcessOutcome:
        """Run a Git command and return the raw outcome (including timeouts).

        Callers that start a mutation must treat ``timed_out`` / unconfirmed
        nonzero exits as ``AmbiguousWriteError``. Preflight reads use ``_run_ok``.
        """

        return self._runner.run(
            args,
            cwd=self._cwd,
            timeout=self._call_timeout(),
            env=self._env,
            stdin_text=stdin_text,
        )

    def _run_ok(self, args: Sequence[str], *, stdin_text: str | None = None) -> str:
        outcome = self._run(args, stdin_text=stdin_text)
        if outcome.timed_out:
            raise GitTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="git command timed out",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )
        if outcome.returncode != 0:
            raise GitTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="git command returned a nonzero status",
                )
            )
        return outcome.stdout

    def _run_ok_bytes(self, args: Sequence[str]) -> bytes:
        returncode, stdout, _stderr, timed_out = self._runner.run_bytes(
            args, cwd=self._cwd, timeout=self._call_timeout(), env=self._env
        )
        if timed_out:
            raise GitTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="git command timed out",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )
        if returncode != 0:
            raise GitTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="git command returned a nonzero status",
                )
            )
        return stdout

    # -- reads -----------------------------------------------------------

    def inspect_repository_root(self) -> str:
        return self._run_ok([self._git, "rev-parse", "--show-toplevel"]).strip()

    def read_current_branch(self) -> str:
        return self._run_ok([self._git, "rev-parse", "--abbrev-ref", "HEAD"]).strip()

    def read_head_sha(self) -> str:
        sha = self._run_ok([self._git, "rev-parse", "HEAD"]).strip().lower()
        _require_full_sha(sha)
        return sha

    def read_status_porcelain(self) -> str:
        return self._run_ok([self._git, "status", "--porcelain=v1", "--untracked-files=all"])

    def read_staged_patch_bytes(self) -> bytes:
        return self._run_ok_bytes([self._git, "diff", "--cached", "--binary"])

    def read_commit_message_raw(self, sha: str) -> str:
        _require_full_sha(sha)
        return self._run_ok([self._git, "log", "-1", "--format=%B", sha])

    def read_commit_parents(self, sha: str) -> tuple[str, ...]:
        _require_full_sha(sha)
        raw = self._run_ok([self._git, "rev-list", "--parents", "-n", "1", sha]).strip()
        parts = raw.split()
        # First token is the commit itself; the rest are parents.
        return tuple(p.lower() for p in parts[1:])

    def read_commit_tree(self, sha: str) -> str:
        _require_full_sha(sha)
        tree = self._run_ok([self._git, "rev-parse", f"{sha}^{{tree}}"]).strip().lower()
        _require_full_sha(tree)
        return tree

    def read_commit_patch_bytes(self, base_sha: str, commit_sha: str) -> bytes:
        _require_full_sha(base_sha)
        _require_full_sha(commit_sha)
        return self._run_ok_bytes([self._git, "diff", "--binary", base_sha, commit_sha])

    def read_remote_url(self, remote_name: str) -> str:
        return self._run_ok([self._git, "remote", "get-url", remote_name]).strip()

    def read_remote_ref(self, remote_name: str, remote_ref: str) -> RemoteRefObservation:
        ref = _normalize_ref(remote_ref)
        stdout = self._run_ok([self._git, "ls-remote", remote_name, ref])
        sha: str | None = None
        seen = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            token, _, name = line.partition("\t")
            if name.strip() != ref:
                continue
            seen += 1
            sha = token.strip().lower()
        if seen > 1:
            raise GitTransportError(
                block=block_for_kind(
                    GatewayBlockKind.CONTRADICTORY_EVIDENCE,
                    detail="remote ref returned multiple entries",
                )
            )
        if sha is not None:
            _require_full_sha(sha)
        return RemoteRefObservation(
            remote_name=remote_name,
            remote_ref=ref,
            sha=sha,
            complete=True,
        )

    def is_ancestor(self, ancestor_sha: str, descendant_sha: str) -> bool:
        _require_full_sha(ancestor_sha)
        _require_full_sha(descendant_sha)
        outcome = self._run(
            [self._git, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha]
        )
        if outcome.returncode == 0:
            return True
        if outcome.returncode == 1:
            return False
        raise GitTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="ancestry check returned an ambiguous status",
            )
        )

    def prepare_ssh_agent_for_remote(self, remote_url: str) -> None:
        """Resolve effective IdentityAgent, verify via ``ssh-add -l``, pin socket.

        Uses direct-argv ``ssh -G`` for the bound remote destination, prefers a
        validated ``IdentityAgent`` over inherited ``SSH_AUTH_SOCK``, verifies
        loaded identities with a bounded ``ssh-add -l``, then stores the chosen
        socket only in this transport's per-instance minimal environment. Does
        not mutate process-global ``os.environ``. Failures raise a typed
        authentication block without embedding socket paths, agent output, or
        key material.
        """

        try:
            destination = ssh_destination_from_remote_url(remote_url)
        except ValidationError as exc:
            raise _ssh_agent_auth_block() from exc

        probe = self._run([self._ssh, "-G", destination])
        if probe.timed_out or probe.returncode != 0:
            raise _ssh_agent_auth_block()

        try:
            identity_agent = parse_identity_agent_from_ssh_g(probe.stdout)
            sock = choose_effective_ssh_auth_sock(
                identity_agent=identity_agent,
                inherited_ssh_auth_sock=self._env.get("SSH_AUTH_SOCK"),
            )
        except (ValidationError, SshAgentNoIdentityError) as exc:
            raise _ssh_agent_auth_block() from exc

        verify_env = dict(self._env)
        verify_env["SSH_AUTH_SOCK"] = sock
        # ssh-add -l: 0 => identities present, 1 => none, 2 => cannot contact agent.
        outcome = self._runner.run(
            ["ssh-add", "-l"],
            cwd=self._cwd,
            timeout=self._call_timeout(),
            env=verify_env,
        )
        if outcome.timed_out or outcome.returncode != 0:
            raise _ssh_agent_auth_block()

        self._env = verify_env

    # -- writes ----------------------------------------------------------

    def commit_with_message_stdin(self, message: str) -> GitProcessOutcome:
        if not message.strip():
            raise GitTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="commit message must be non-empty",
                )
            )
        return self._run(
            [self._git, "commit", "--no-verify", "--file", "-"],
            stdin_text=message,
        )

    def push_non_force(
        self, *, remote_name: str, commit_sha: str, remote_ref: str
    ) -> GitProcessOutcome:
        _require_full_sha(commit_sha)
        ref = _normalize_ref(remote_ref)
        refspec = f"{commit_sha}:{ref}"
        argv = [self._git, "push", remote_name, refspec]
        assert_no_force_argv(argv)
        return self._run(argv)


def extract_remote_nwo(remote_url: str) -> str | None:
    match = _SSH_NWO_RE.match(remote_url.strip())
    if match is None:
        return None
    return match.group("nwo")


def _normalize_ref(remote_ref: str) -> str:
    if remote_ref.startswith("refs/heads/"):
        return remote_ref
    return f"refs/heads/{remote_ref}"


def _require_full_sha(sha: str) -> None:
    if not _FULL_SHA.match(sha):
        raise GitTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="git returned a value that was not a full 40-character SHA",
            )
        )


def _ssh_agent_auth_block() -> GitTransportError:
    return GitTransportError(
        block=block_for_kind(
            GatewayBlockKind.AUTHENTICATION,
            detail="no usable SSH agent identity for push",
        )
    )


def path_matches_local_remote(remote_url: str, repository_cwd: str) -> bool:
    """Best-effort check that a LOCAL remote URL resolves to a real path."""

    candidate = remote_url
    for prefix in ("file://",):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
    del repository_cwd
    return Path(candidate).exists()


__all__ = [
    "DefaultGitProcessRunner",
    "GitProcessOutcome",
    "GitProcessRunner",
    "GitTransportError",
    "GitWriteTransport",
    "build_minimal_git_env",
    "extract_remote_nwo",
    "path_matches_local_remote",
]
