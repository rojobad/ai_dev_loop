"""Read-only discovery of an already-open pull request for ``pr-review-v2 prepare``."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.pr_review_v2.domain.common import PullRequestBinding, RepositoryIdentity

_OWNER_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA40_RE = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(frozen=True)
class DiscoveredExistingPr:
    binding: PullRequestBinding


class GhJsonRunner(Protocol):
    def run(self, argv: list[str], *, cwd: str | None, timeout: float) -> tuple[int, str, str]: ...


class ProcessGhJsonRunner:
    """Production runner using ``run_process`` (no shell)."""

    def run(self, argv: list[str], *, cwd: str | None, timeout: float) -> tuple[int, str, str]:
        from ai_dev_loop.process import run_process

        result = run_process(argv, cwd=cwd, timeout=timeout)
        return result.returncode, result.stdout or "", result.stderr or ""


class ExistingPrDiscoverer:
    """Fetch open-PR identity via ``gh api`` (read-only; never mutates)."""

    def __init__(
        self,
        *,
        gh_command: str = "gh",
        runner: GhJsonRunner | None = None,
        timeout_seconds: float = 60.0,
        repository_cwd: str | None = None,
    ) -> None:
        self._gh = gh_command
        self._runner = runner or ProcessGhJsonRunner()
        self._timeout = timeout_seconds
        self._cwd = repository_cwd

    def discover(self, *, owner_repo: str, pr_number: int) -> DiscoveredExistingPr:
        if not _OWNER_REPO_RE.match(owner_repo):
            raise ValidationError("repository must be OWNER/REPO")
        if pr_number < 1:
            raise ValidationError("pull request number must be positive")
        owner, name = owner_repo.split("/", 1)
        argv = [
            self._gh,
            "api",
            f"repos/{owner}/{name}/pulls/{pr_number}",
            "--jq",
            (
                "{state:.state,number:.number,"
                "head_ref:.head.ref,base_ref:.base.ref,head_sha:.head.sha,"
                "head_repo:.head.repo.full_name}"
            ),
        ]
        code, stdout, _stderr = self._runner.run(argv, cwd=self._cwd, timeout=self._timeout)
        if code != 0:
            raise ValidationError("failed to read pull request identity (read-only gh api)")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError("pull request identity response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValidationError("pull request identity response must be an object")
        state = str(payload.get("state") or "")
        if state != "open":
            raise ValidationError("pull request must be open")
        head_sha = str(payload.get("head_sha") or "")
        if not _SHA40_RE.match(head_sha):
            raise ValidationError("pull request head SHA is missing or invalid")
        head_ref = str(payload.get("head_ref") or "").strip()
        base_ref = str(payload.get("base_ref") or "").strip()
        if not head_ref or not base_ref:
            raise ValidationError("pull request branch refs are missing")
        raw_head_repo = payload.get("head_repo")
        if raw_head_repo is None or not str(raw_head_repo).strip():
            raise ValidationError("pull request head_repo ownership evidence is missing")
        head_repo = str(raw_head_repo).strip()
        if head_repo.lower() != owner_repo.lower():
            raise ValidationError("pull request head repository must match --repo")
        number = int(payload.get("number") or 0)
        if number != pr_number:
            raise ValidationError("pull request number mismatch")
        return DiscoveredExistingPr(
            binding=PullRequestBinding(
                repository=RepositoryIdentity(name_with_owner=owner_repo),
                pr_number=pr_number,
                head_branch=head_ref,
                base_branch=base_ref,
                head_sha=head_sha.lower(),
            )
        )


__all__ = [
    "DiscoveredExistingPr",
    "ExistingPrDiscoverer",
    "GhJsonRunner",
    "ProcessGhJsonRunner",
]
