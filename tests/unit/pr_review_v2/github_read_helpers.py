"""Shared fakes and builders for Phase 16.5 GitHub read tests."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GhTransportResult,
    GitHubReadPolicy,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    PullRequestBinding,
    RepositoryIdentity,
)
from ai_dev_loop.pr_review_v2.domain.effects import ObserveBotReviewEffect
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import parse_include_envelope
from ai_dev_loop.process import ProcessResult

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pr_review_v2" / "github"
SHA_B = "b" * 40
MARKER = "pr-review:run-1:cycle:01:review-trigger"
T_OBS = datetime(2026, 7, 21, 12, 10, 0, tzinfo=UTC)


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def policy(**overrides: Any) -> GitHubReadPolicy:
    base = {
        "gh_command": "fake-gh",
        "repository_cwd": "/tmp/repo",
        "accepted_no_findings_prefixes": (),
    }
    base.update(overrides)
    return GitHubReadPolicy(**base)


def binding() -> PullRequestBinding:
    return PullRequestBinding(
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_B,
    )


def observe_effect(*, attempt: int = 1, poll_sequence: int = 1) -> ObserveBotReviewEffect:
    return ObserveBotReviewEffect(
        effect_id="pr-review:run-1:cycle:01:observe_bot_review:poll-0001",
        idempotency_key="pr-review:run-1:cycle:01:observe_bot_review:poll-0001",
        run_id="run-1",
        cycle_number=1,
        attempt=attempt,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_B,
        binding=binding(),
        poll_sequence=poll_sequence,
        trigger_marker=MARKER,
    )


def token_for(effect: ObserveBotReviewEffect) -> EffectCompletionToken:
    return EffectCompletionToken(
        effect_id=effect.effect_id,
        expected_run_version=1,
        lease_generation=1,
        cycle_number=effect.cycle_number,
        bound_head_sha=effect.bound_head_sha,
    )


@dataclass
class ScriptedGhRunner:
    """Fake gh process runner keyed by operation markers in argv."""

    scripts: dict[str, ProcessResult] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)

    def add_include(self, key: str, fixture_name: str, *, returncode: int = 0) -> None:
        stdout = load_fixture(fixture_name)
        self.scripts[key] = ProcessResult(
            args=["fake-gh"],
            returncode=returncode,
            stdout=stdout,
            stderr="",
            timed_out=False,
        )

    def add_raw(
        self,
        key: str,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 1,
        timed_out: bool = False,
    ) -> None:
        self.scripts[key] = ProcessResult(
            args=["fake-gh"],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
        )

    def add_json(self, key: str, payload: object, *, status: int = 200, headers: str = "") -> None:
        header_block = headers or "Content-Type: application/json"
        body = json.dumps(payload)
        stdout = f"HTTP/2 {status}\n{header_block}\n\n{body}"
        self.scripts[key] = ProcessResult(
            args=["fake-gh"],
            returncode=0 if status < 400 else 1,
            stdout=stdout,
            stderr="",
            timed_out=False,
        )

    def run(
        self,
        args: list[str],
        *,
        cwd: str,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> ProcessResult:
        del cwd, timeout, env
        self.calls.append(list(args))
        joined = " ".join(args)
        assert "mutation" not in joined.lower()
        assert "--method POST" not in joined
        assert "--method PATCH" not in joined
        assert "--method PUT" not in joined
        assert "--method DELETE" not in joined
        assert "{owner}" not in joined
        assert "{repo}" not in joined
        for key, result in self.scripts.items():
            if key in joined:
                return ProcessResult(
                    args=list(args),
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    timed_out=result.timed_out,
                )
        raise AssertionError(f"no scripted response for argv: {args}")


@dataclass
class FakeTransport:
    """Direct fake transport implementing the narrow typed read ports."""

    identity_pages: list[GhTransportResult] = field(default_factory=list)
    comment_pages: list[GhTransportResult] = field(default_factory=list)
    thread_pages: list[GhTransportResult] = field(default_factory=list)
    reaction_pages: list[GhTransportResult] = field(default_factory=list)
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    on_call: Any | None = None

    # Back-compat read-only view of queued pages for diagnostics.
    @property
    def graphql_pages(self) -> dict[str, list[GhTransportResult]]:
        return {
            "pull_request_identity": self.identity_pages,
            "issue_comments": self.comment_pages,
            "review_threads": self.thread_pages,
        }

    def _maybe_hook(self) -> None:
        if self.on_call is not None:
            self.on_call()

    def fetch_pull_request_identity(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        self.calls.append(
            (
                "identity",
                {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        self._maybe_hook()
        if not self.identity_pages:
            raise AssertionError("missing identity pages")
        return _classify_or_return(self.identity_pages.pop(0))

    def fetch_issue_comments_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        self.calls.append(
            (
                "issue_comments",
                {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "cursor": cursor,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        self._maybe_hook()
        if not self.comment_pages:
            raise AssertionError("missing issue comment pages")
        return _classify_or_return(self.comment_pages.pop(0))

    def fetch_review_threads_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        self.calls.append(
            (
                "review_threads",
                {
                    "owner": owner,
                    "name": name,
                    "number": number,
                    "cursor": cursor,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        self._maybe_hook()
        if not self.thread_pages:
            raise AssertionError("missing review thread pages")
        return _classify_or_return(self.thread_pages.pop(0))

    def fetch_issue_comment_reactions_page(
        self,
        *,
        owner: str,
        name: str,
        comment_id: str,
        page: int,
        per_page: int = 100,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        self.calls.append(
            (
                "reactions",
                {
                    "owner": owner,
                    "name": name,
                    "comment_id": comment_id,
                    "page": page,
                    "per_page": per_page,
                    "timeout_seconds": timeout_seconds,
                },
            )
        )
        self._maybe_hook()
        if not self.reaction_pages:
            return GhTransportResult(
                http_status=200,
                headers=AllowlistedHeaders(),
                body_json=[],
                returncode=0,
            )
        return _classify_or_return(self.reaction_pages.pop(0))


def _classify_or_return(result: GhTransportResult) -> GhTransportResult:
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
        classify_http_and_graphql,
    )

    classified = classify_http_and_graphql(
        status=result.http_status,
        headers=result.headers,
        body_json=result.body_json,
        returncode=result.returncode,
    )
    if classified is not None:
        raise classified
    return result


def result_from_fixture(name: str, *, returncode: int | None = None) -> GhTransportResult:
    stdout = load_fixture(name)
    status, headers, body = parse_include_envelope(stdout)
    body_json = json.loads(body) if body.strip() else None
    from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import normalize_allowlisted_headers

    rc = returncode
    if rc is None:
        rc = 0 if status < 400 else 1
    return GhTransportResult(
        http_status=status,
        headers=normalize_allowlisted_headers(headers),
        body_json=body_json,
        returncode=rc,
    )


def queue_waiting(transport: FakeTransport) -> None:
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(result_from_fixture("issue_comments_waiting_and_nofindings.txt"))
    transport.thread_pages.append(
        GhTransportResult(
            http_status=200,
            headers=AllowlistedHeaders(),
            body_json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                }
            },
            returncode=0,
        )
    )
