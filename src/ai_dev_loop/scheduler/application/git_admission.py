"""Git worktree admission port for Phase 17.2 one-shot tick preflight."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.process import ProcessResult, run_process

DEFAULT_GIT_ADMISSION_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class GitAdmissionEvidence:
    resolved_root: str
    branch: str
    head: str
    status_porcelain: str


@dataclass(frozen=True)
class GitAdmissionResult:
    ok: bool
    evidence: GitAdmissionEvidence | None = None
    artifact_text: str | None = None
    failure_kind: str | None = None
    failure_summary: str | None = None


@runtime_checkable
class GitAdmissionPort(Protocol):
    def admit(
        self,
        *,
        repository_root: str,
        require_clean_worktree: bool,
    ) -> GitAdmissionResult: ...


def format_admission_artifact_text(evidence: GitAdmissionEvidence) -> str:
    return (
        f"branch={evidence.branch}\n"
        f"head={evidence.head}\n"
        f"status_porcelain={evidence.status_porcelain}\n"
    )


def _require_git_success(result: ProcessResult, *, context: str) -> str:
    if result.timed_out:
        raise _GitAdmissionProbeError(
            "git_timeout",
            f"git {context} timed out",
        )
    if result.returncode != 0:
        raise _GitAdmissionProbeError(
            "git_command_failed",
            f"git {context} failed with exit code {result.returncode}",
        )
    return result.stdout.strip()


class _GitAdmissionProbeError(Exception):
    def __init__(self, kind: str, summary: str) -> None:
        self.kind = kind
        self.summary = summary
        super().__init__(summary)


def discover_repository_bounded(
    repo_path: Path,
    *,
    timeout_seconds: float = DEFAULT_GIT_ADMISSION_TIMEOUT_SECONDS,
) -> GitAdmissionEvidence:
    repo_path = repo_path.resolve()
    if not repo_path.is_dir():
        raise ValidationError(f"repository path does not exist: {repo_path}")

    def git(args: list[str]) -> str:
        result = run_process(
            ["git", *args],
            cwd=str(repo_path),
            timeout=timeout_seconds,
        )
        return _require_git_success(result, context=" ".join(args))

    root = Path(git(["rev-parse", "--show-toplevel"])).resolve()
    branch = git(["rev-parse", "--abbrev-ref", "HEAD"])
    head = git(["rev-parse", "HEAD"])
    status = git(["status", "--porcelain=v2", "--untracked-files=all"])
    staged_output = git(["diff", "--cached", "--name-only"])
    staged_paths = tuple(line.strip() for line in staged_output.splitlines() if line.strip())

    evidence = GitAdmissionEvidence(
        resolved_root=str(root),
        branch=branch,
        head=head,
        status_porcelain=status,
    )
    return _apply_clean_policy(evidence, staged_paths=staged_paths, submitted_root=str(repo_path))


def _apply_clean_policy(
    evidence: GitAdmissionEvidence,
    *,
    staged_paths: tuple[str, ...],
    submitted_root: str,
) -> GitAdmissionEvidence:
    if evidence.resolved_root != str(Path(submitted_root).resolve()):
        raise _GitAdmissionProbeError(
            "repository_root_mismatch",
            "resolved repository root does not match submitted target",
        )
    return evidence


class BoundedGitAdmissionPort:
    """Production adapter using bounded Git discovery commands."""

    def __init__(
        self,
        *,
        timeout_seconds: float = DEFAULT_GIT_ADMISSION_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout_seconds = timeout_seconds

    def admit(
        self,
        *,
        repository_root: str,
        require_clean_worktree: bool,
    ) -> GitAdmissionResult:
        repo_path = Path(repository_root)
        try:
            result = run_process(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=str(repo_path.resolve()),
                timeout=self._timeout_seconds,
            )
            if result.timed_out:
                return GitAdmissionResult(
                    ok=False,
                    failure_kind="git_timeout",
                    failure_summary="git repository probe timed out",
                )
            if result.returncode != 0:
                return GitAdmissionResult(
                    ok=False,
                    failure_kind="git_command_failed",
                    failure_summary="git repository probe failed",
                )
            root = Path(_require_git_success(result, context="rev-parse --show-toplevel")).resolve()
            branch = _git_text(
                repo_path,
                ["rev-parse", "--abbrev-ref", "HEAD"],
                timeout_seconds=self._timeout_seconds,
            )
            head = _git_text(
                repo_path,
                ["rev-parse", "HEAD"],
                timeout_seconds=self._timeout_seconds,
            )
            status = _git_text(
                repo_path,
                ["status", "--porcelain=v2", "--untracked-files=all"],
                timeout_seconds=self._timeout_seconds,
            )
            staged_output = _git_text(
                repo_path,
                ["diff", "--cached", "--name-only"],
                timeout_seconds=self._timeout_seconds,
            )
            staged_paths = tuple(
                line.strip() for line in staged_output.splitlines() if line.strip()
            )
        except ValidationError as exc:
            return GitAdmissionResult(
                ok=False,
                failure_kind="repository_discovery_failed",
                failure_summary=str(exc),
            )
        except _GitAdmissionProbeError as exc:
            return GitAdmissionResult(
                ok=False,
                failure_kind=exc.kind,
                failure_summary=exc.summary,
            )
        except AiDevLoopError:
            return GitAdmissionResult(
                ok=False,
                failure_kind="git_command_failed",
                failure_summary="git executable unavailable",
            )

        evidence = GitAdmissionEvidence(
            resolved_root=str(root),
            branch=branch,
            head=head,
            status_porcelain=status,
        )
        if str(root) != str(repo_path.resolve()):
            return GitAdmissionResult(
                ok=False,
                evidence=evidence,
                failure_kind="repository_root_mismatch",
                failure_summary="resolved repository root does not match submitted target",
            )
        if require_clean_worktree:
            if staged_paths:
                return GitAdmissionResult(
                    ok=False,
                    evidence=evidence,
                    failure_kind="dirty_worktree",
                    failure_summary="worktree has pre-existing staged changes",
                )
            if status.strip():
                return GitAdmissionResult(
                    ok=False,
                    evidence=evidence,
                    failure_kind="dirty_worktree",
                    failure_summary="worktree is not clean",
                )
        artifact_text = format_admission_artifact_text(evidence)
        return GitAdmissionResult(ok=True, evidence=evidence, artifact_text=artifact_text)


def _git_text(repo_path: Path, args: list[str], *, timeout_seconds: float) -> str:
    result = run_process(
        ["git", *args],
        cwd=str(repo_path.resolve()),
        timeout=timeout_seconds,
    )
    return _require_git_success(result, context=" ".join(args))
