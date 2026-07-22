"""Direct argv ``gh api`` mutation + reconciliation-read transport for PR review v2.

This module is intentionally separate from the strictly read-only
``gh_transport``. It exposes only predefined named operations (PR create/update,
issue-comment trigger, review-thread reply, PR text update, thread resolution) plus
narrowly named reconciliation reads. There is no public generic ``graphql``,
``rest``, endpoint, or method surface. Bodies and GraphQL variables travel through
stdin (``gh api --input -``); no comment/reply/PR text is ever placed in argv.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.pr_review_v2.application.github_read import (
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    GhTransportResult,
    block_for_kind,
)
from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
    GhTransportError,
    _validate_repo_tokens,
    build_minimal_gh_env,
    classify_http_and_graphql,
    normalize_allowlisted_headers,
    parse_include_envelope,
    parse_paginated_include,
)
from ai_dev_loop.process import (
    ProcessResult,
    StreamingProcessResult,
    run_process_streaming,
)

ADD_REPLY_MUTATION = """
mutation AddReviewThreadReply($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(
    input: {pullRequestReviewThreadId: $threadId, body: $body}
  ) {
    comment { id body createdAt author { login } }
  }
}
""".strip()

RESOLVE_THREAD_MUTATION = """
mutation ResolveReviewThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
""".strip()

THREAD_COMMENTS_QUERY = """
query ReviewThreadComments($threadId: ID!, $cursor: String) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      comments(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes { id body createdAt author { login } }
      }
    }
  }
}
""".strip()

THREAD_RESOLVED_QUERY = """
query ReviewThreadResolved($threadId: ID!) {
  node(id: $threadId) {
    ... on PullRequestReviewThread {
      id
      isResolved
      pullRequest { number }
    }
  }
}
""".strip()


@runtime_checkable
class GhWriteProcessRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> _RunnerOutcome: ...


class _RunnerOutcome(Protocol):
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


class DefaultGhWriteProcessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> ProcessResult | StreamingProcessResult:
        try:
            return run_process_streaming(
                list(args),
                cwd=cwd,
                timeout=timeout,
                env=dict(env) if env is not None else build_minimal_gh_env(),
                stdin_text=stdin_text,
            )
        except AiDevLoopError as exc:
            if "executable not found" in str(exc).lower():
                raise GhTransportError(
                    block=block_for_kind(GatewayBlockKind.MISSING_EXECUTABLE)
                ) from exc
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                    safe_summary="temporary gh CLI failure",
                    transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                )
            ) from exc


class GhWriteTransport:
    """Predefined ``gh api`` mutations and reconciliation reads."""

    def __init__(
        self,
        *,
        command: str,
        cwd: str,
        per_call_timeout_seconds: float,
        runner: GhWriteProcessRunner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._timeout = per_call_timeout_seconds
        self._runner = runner or DefaultGhWriteProcessRunner()
        self._env = dict(env) if env is not None else build_minimal_gh_env()

    # -- mutations -------------------------------------------------------

    def create_pull_request(
        self,
        *,
        owner: str,
        name: str,
        head: str,
        base: str,
        title: str,
        body: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        endpoint = f"repos/{owner}/{name}/pulls"
        payload = {"title": title, "head": head, "base": base, "body": body}
        return self._rest_write(
            endpoint=endpoint, method="POST", payload=payload, timeout_seconds=timeout_seconds
        )

    def update_pull_request(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        title: str,
        body: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        endpoint = f"repos/{owner}/{name}/pulls/{int(number)}"
        payload = {"title": title, "body": body}
        return self._rest_write(
            endpoint=endpoint, method="PATCH", payload=payload, timeout_seconds=timeout_seconds
        )

    def update_pr_text(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        title: str,
        body: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        return self.update_pull_request(
            owner=owner,
            name=name,
            number=number,
            title=title,
            body=body,
            timeout_seconds=timeout_seconds,
        )

    def create_issue_comment(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        body: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        endpoint = f"repos/{owner}/{name}/issues/{int(number)}/comments"
        return self._rest_write(
            endpoint=endpoint,
            method="POST",
            payload={"body": body},
            timeout_seconds=timeout_seconds,
        )

    def add_review_thread_reply(
        self, *, thread_id: str, body: str, timeout_seconds: float | None = None
    ) -> GhTransportResult:
        return self._graphql(
            query=ADD_REPLY_MUTATION,
            variables={"threadId": thread_id, "body": body},
            timeout_seconds=timeout_seconds,
        )

    def resolve_review_thread(
        self, *, thread_id: str, timeout_seconds: float | None = None
    ) -> GhTransportResult:
        return self._graphql(
            query=RESOLVE_THREAD_MUTATION,
            variables={"threadId": thread_id},
            timeout_seconds=timeout_seconds,
        )

    # -- reconciliation reads -------------------------------------------

    def list_prs_by_head_base(
        self,
        *,
        owner: str,
        name: str,
        head: str,
        base: str,
        page: int = 1,
        per_page: int = 100,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        if page < 1 or per_page < 1 or per_page > 100:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="PR page must be >= 1 and per_page must be in 1..100",
                )
            )
        endpoint = (
            f"repos/{owner}/{name}/pulls?head={owner}:{head}&base={base}&state=all"
            f"&per_page={per_page}&page={page}"
        )
        args = [self._command, "api", endpoint, "--method", "GET", "--include"]
        return self._execute(args, timeout_seconds=timeout_seconds)

    def fetch_pr_text(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        endpoint = f"repos/{owner}/{name}/pulls/{int(number)}"
        args = [self._command, "api", endpoint, "--method", "GET", "--include"]
        return self._execute(args, timeout_seconds=timeout_seconds)

    def fetch_branch_head_sha(
        self,
        *,
        owner: str,
        name: str,
        branch: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        """Return the exact object SHA for ``refs/heads/<branch>`` in the repository."""

        from ai_dev_loop.pr_review_v2.application.write_contracts import validate_branch_name

        _validate_repo_tokens(owner, name)
        try:
            validate_branch_name(branch)
        except ValueError as exc:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION, detail="branch name is unsafe"
                )
            ) from exc
        encoded = quote(branch, safe="")
        endpoint = f"repos/{owner}/{name}/git/ref/heads/{encoded}"
        args = [self._command, "api", endpoint, "--method", "GET", "--include"]
        return self._execute(args, timeout_seconds=timeout_seconds)

    def fetch_thread_comments_page(
        self, *, thread_id: str, cursor: str | None, timeout_seconds: float | None = None
    ) -> GhTransportResult:
        return self._graphql(
            query=THREAD_COMMENTS_QUERY,
            variables={"threadId": thread_id, "cursor": cursor},
            timeout_seconds=timeout_seconds,
        )

    def fetch_thread_resolved(
        self, *, thread_id: str, timeout_seconds: float | None = None
    ) -> GhTransportResult:
        return self._graphql(
            query=THREAD_RESOLVED_QUERY,
            variables={"threadId": thread_id},
            timeout_seconds=timeout_seconds,
        )

    # -- internals -------------------------------------------------------

    def _rest_write(
        self,
        *,
        endpoint: str,
        method: str,
        payload: Mapping[str, Any],
        timeout_seconds: float | None,
    ) -> GhTransportResult:
        args = [self._command, "api", endpoint, "--method", method, "--include", "--input", "-"]
        stdin_text = json.dumps(payload)
        return self._execute(args, timeout_seconds=timeout_seconds, stdin_text=stdin_text)

    def _graphql(
        self,
        *,
        query: str,
        variables: Mapping[str, Any],
        timeout_seconds: float | None,
    ) -> GhTransportResult:
        args = [self._command, "api", "graphql", "--include", "--input", "-"]
        stdin_text = json.dumps({"query": query, "variables": dict(variables)})
        return self._execute(args, timeout_seconds=timeout_seconds, stdin_text=stdin_text)

    def _execute(
        self,
        args: list[str],
        *,
        timeout_seconds: float | None,
        stdin_text: str | None = None,
        paginated: bool = False,
    ) -> GhTransportResult:
        call_timeout = self._timeout if timeout_seconds is None else float(timeout_seconds)
        if call_timeout <= 0:
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="write overall deadline exceeded",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )
        try:
            result = self._runner.run(
                args,
                cwd=self._cwd,
                timeout=call_timeout,
                env=self._env,
                stdin_text=stdin_text,
            )
        except GhTransportError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                    safe_summary="temporary gh CLI failure",
                    transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                )
            ) from exc

        if getattr(result, "timed_out", False):
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="gh api call timed out",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )

        try:
            if paginated:
                status, headers, body_json = parse_paginated_include(result.stdout)
            else:
                status, raw_headers, body_text = parse_include_envelope(result.stdout)
                headers = normalize_allowlisted_headers(raw_headers)
                body_json = json.loads(body_text) if body_text.strip() else None
        except GhTransportError:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            if result.returncode != 0:
                raise GhTransportError(
                    transient=GatewayTransient(
                        kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                        safe_summary="gh mutation failed without a parseable envelope",
                        transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                    )
                ) from exc
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="gh response envelope was malformed",
                )
            ) from exc

        classified = classify_http_and_graphql(
            status=status,
            headers=headers,
            body_json=body_json,
            returncode=result.returncode,
        )
        if classified is not None:
            raise classified
        return GhTransportResult(
            http_status=status,
            headers=headers,
            body_json=body_json,
            returncode=result.returncode,
            timed_out=False,
            argv=tuple(args),
        )


__all__ = [
    "ADD_REPLY_MUTATION",
    "DefaultGhWriteProcessRunner",
    "GhWriteProcessRunner",
    "GhWriteTransport",
    "RESOLVE_THREAD_MUTATION",
    "THREAD_COMMENTS_QUERY",
    "THREAD_RESOLVED_QUERY",
]
