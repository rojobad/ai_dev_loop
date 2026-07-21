"""Direct argv ``gh api`` read transport for PR review v2."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GatewayBlock,
    GatewayBlockKind,
    GatewayTransient,
    GatewayTransientKind,
    GhTransportResult,
    RateLimitClass,
    block_for_kind,
    transient_for_http_status,
)
from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind
from ai_dev_loop.process import ProcessResult, run_process

_ALLOWLISTED_HEADER_NAMES = frozenset(
    {
        "retry-after",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-ratelimit-resource",
    }
)

_MUTATION_RE = re.compile(r"\bmutation\b", re.IGNORECASE)
_NAMED_QUERY_RE = re.compile(r"^\s*query\s+[A-Za-z_][A-Za-z0-9_]*\b", re.DOTALL)
_HTTP_STATUS_RE = re.compile(r"(?m)^HTTP/\d(?:\.\d)?\s+(\d{3})\b.*$")
_SAFE_OWNER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_COMMENT_ID_RE = re.compile(r"^[0-9]+$")

# gh CLI: exit code 4 is the documented authentication / HTTP 401 class.
_GH_AUTH_EXIT_CODE = 4

PULL_REQUEST_IDENTITY_QUERY = """
query PullRequestIdentity($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      number
      state
      isCrossRepository
      headRefName
      baseRefName
      headRefOid
    }
  }
}
""".strip()

ISSUE_COMMENTS_QUERY = """
query PullRequestIssueComments(
  $owner: String!, $name: String!, $number: Int!, $cursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      comments(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          databaseId
          body
          createdAt
          author { login }
        }
      }
    }
  }
}
""".strip()

REVIEW_THREADS_QUERY = """
query PullRequestReviewThreads(
  $owner: String!, $name: String!, $number: Int!, $cursor: String
) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 1) {
            nodes {
              id
              body
              createdAt
              author { login }
              commit { oid }
              path
              line
              pullRequestReview { id }
            }
          }
        }
      }
    }
  }
}
""".strip()


class GhTransportError(Exception):
    """Private transport/gateway error carrying a typed block or transient."""

    def __init__(
        self, *, block: GatewayBlock | None = None, transient: GatewayTransient | None = None
    ) -> None:
        if (block is None) == (transient is None):
            raise ValueError("exactly one of block or transient is required")
        self.block = block
        self.transient = transient
        summary = block.safe_summary if block is not None else transient.safe_summary  # type: ignore[union-attr]
        super().__init__(summary)


@runtime_checkable
class GhProcessRunner(Protocol):
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult: ...


class DefaultGhProcessRunner:
    def run(
        self,
        args: Sequence[str],
        *,
        cwd: str,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        try:
            return run_process(args, cwd=cwd, timeout=timeout, env=dict(env) if env else None)
        except AiDevLoopError as exc:
            message = str(exc).lower()
            if "executable not found" in message:
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


class GhApiTransport:
    """Predefined read-only ``gh api`` transport. No arbitrary query API."""

    def __init__(
        self,
        *,
        command: str,
        cwd: str,
        per_call_timeout_seconds: float,
        runner: GhProcessRunner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._timeout = per_call_timeout_seconds
        self._runner = runner or DefaultGhProcessRunner()
        self._env = dict(env) if env is not None else None

    def fetch_pull_request_identity(
        self, *, owner: str, name: str, number: int, timeout_seconds: float | None = None
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        return self._graphql(
            operation="pull_request_identity",
            query=PULL_REQUEST_IDENTITY_QUERY,
            variables={"owner": owner, "name": name, "number": number},
            timeout_seconds=timeout_seconds,
        )

    def fetch_issue_comments_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        return self._graphql(
            operation="issue_comments",
            query=ISSUE_COMMENTS_QUERY,
            variables={"owner": owner, "name": name, "number": number, "cursor": cursor},
            timeout_seconds=timeout_seconds,
        )

    def fetch_review_threads_page(
        self,
        *,
        owner: str,
        name: str,
        number: int,
        cursor: str | None,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _validate_repo_tokens(owner, name)
        return self._graphql(
            operation="review_threads",
            query=REVIEW_THREADS_QUERY,
            variables={"owner": owner, "name": name, "number": number, "cursor": cursor},
            timeout_seconds=timeout_seconds,
        )

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
        """Fetch one reactions page. Callers must paginate serially with bounds."""

        _validate_repo_tokens(owner, name)
        if not _SAFE_COMMENT_ID_RE.match(comment_id):
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="comment_id must be a numeric GitHub issue comment id",
                )
            )
        if page < 1:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="reaction page must be >= 1",
                )
            )
        if per_page < 1 or per_page > 100:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="reaction per_page must be in 1..100",
                )
            )
        endpoint = (
            f"repos/{owner}/{name}/issues/comments/{comment_id}/reactions"
            f"?per_page={per_page}&page={page}"
        )
        return self._rest_get(
            operation="comment_reactions",
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
        )

    def _graphql(
        self,
        *,
        operation: str,
        query: str,
        variables: Mapping[str, Any],
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        _assert_read_only_graphql(query)
        args = [self._command, "api", "graphql", "--include", "-f", f"query={query}"]
        for key, value in variables.items():
            if value is None:
                args.extend(["-F", f"{key}=null"])
            elif isinstance(value, bool):
                args.extend(["-F", f"{key}={'true' if value else 'false'}"])
            elif isinstance(value, int) and not isinstance(value, bool):
                args.extend(["-F", f"{key}={value}"])
            else:
                args.extend(["-F", f"{key}={value}"])
        return self._execute(args, operation=operation, timeout_seconds=timeout_seconds)

    def _rest_get(
        self,
        *,
        operation: str,
        endpoint: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        if not endpoint or endpoint.startswith(("http://", "https://")):
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="REST endpoint must be a relative GitHub API path",
                )
            )
        if "{" in endpoint or "}" in endpoint:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                    detail="REST endpoint must not use ambient repository placeholders",
                )
            )
        args = [self._command, "api", endpoint, "--method", "GET", "--include"]
        return self._execute(args, operation=operation, timeout_seconds=timeout_seconds)

    def _execute(
        self,
        args: list[str],
        *,
        operation: str,
        timeout_seconds: float | None = None,
    ) -> GhTransportResult:
        del operation
        call_timeout = self._timeout if timeout_seconds is None else float(timeout_seconds)
        if call_timeout <= 0:
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="observation overall deadline exceeded",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )
        try:
            result = self._runner.run(
                args,
                cwd=self._cwd,
                timeout=call_timeout,
                env=self._env,
            )
        except GhTransportError:
            raise
        except Exception as exc:  # noqa: BLE001 - map unknown runner failures safely
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                    safe_summary="temporary gh CLI failure",
                    transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                )
            ) from exc

        if result.timed_out:
            raise GhTransportError(
                transient=GatewayTransient(
                    kind=GatewayTransientKind.TIMEOUT,
                    safe_summary="gh api call timed out",
                    transient_kind=TransientErrorKind.TIMEOUT,
                )
            )

        try:
            status, raw_headers, body_text = parse_include_envelope(result.stdout)
            headers = normalize_allowlisted_headers(raw_headers)
            body_json = _loads_json_body(body_text)
        except GhTransportError:
            raise
        except ValueError as exc:
            if result.returncode != 0:
                raise _classify_nonzero_without_envelope(result) from exc
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


def _validate_repo_tokens(owner: str, name: str) -> None:
    if not _SAFE_OWNER_RE.match(owner) or not _SAFE_OWNER_RE.match(name):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                detail="repository owner/name tokens are invalid",
            )
        )


def _assert_read_only_graphql(query: str) -> None:
    if _MUTATION_RE.search(query):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                detail="GraphQL mutation operations are forbidden",
            )
        )
    if not _NAMED_QUERY_RE.match(query):
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.HTTP_VALIDATION_REJECTION,
                detail="GraphQL documents must begin with a named query",
            )
        )


def parse_include_envelope(stdout: str) -> tuple[int, dict[str, list[str]], str]:
    """Split a single ``gh api --include`` stdout into status, headers, and body."""

    envelopes = parse_include_envelopes(stdout)
    if not envelopes:
        raise ValueError("missing HTTP status line")
    # Non-paginated path uses one envelope. Multi-page REST must call
    # parse_paginated_include so every successful page is retained.
    status, headers, body = envelopes[-1]
    return status, headers, body


def parse_include_envelopes(stdout: str) -> list[tuple[int, dict[str, list[str]], str]]:
    """Split concatenated ``gh api --include --paginate`` stdout into ordered envelopes."""

    text = stdout.replace("\r\n", "\n")
    if not text:
        raise ValueError("empty include envelope")
    matches = list(_HTTP_STATUS_RE.finditer(text))
    if not matches:
        raise ValueError("missing HTTP status line")
    envelopes: list[tuple[int, dict[str, list[str]], str]] = []
    for index, match in enumerate(matches):
        status = int(match.group(1))
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        segment = text[start:end]
        if segment.startswith("\n"):
            segment = segment[1:]
        header_blob, sep, body = segment.partition("\n\n")
        if not sep:
            header_blob, body = segment, ""
        # Trim trailing page separators that belong to the next HTTP line.
        body = body.rstrip("\n")
        if index + 1 < len(matches) and body.endswith("\n"):
            body = body[:-1]
        headers: dict[str, list[str]] = {}
        for line in header_blob.split("\n"):
            if not line or line.lower().startswith("http/"):
                continue
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.strip().lower()
            headers.setdefault(key, []).append(value.strip())
        envelopes.append((status, headers, body))
    return envelopes


def parse_paginated_include(stdout: str) -> tuple[int, AllowlistedHeaders, object | None]:
    """Parse every REST page envelope and combine bodies in order.

    Allowlisted headers are taken from the last successful (2xx) page when present,
    otherwise from the failing page, so rate-limit policy sees trustworthy values.
    """

    envelopes = parse_include_envelopes(stdout)
    combined_items: list[Any] = []
    saw_array = False
    saw_object = False
    last_success_headers: dict[str, list[str]] | None = None
    failure: tuple[int, dict[str, list[str]], object | None] | None = None
    for status, headers, body in envelopes:
        parsed_body: object | None = None
        if body.strip():
            try:
                parsed_body = json.loads(body)
            except json.JSONDecodeError as exc:
                raise GhTransportError(
                    block=block_for_kind(
                        GatewayBlockKind.MALFORMED_EVIDENCE,
                        detail="gh paginated response body was not valid JSON",
                    )
                ) from exc
        if status >= 400:
            if failure is None:
                failure = (status, headers, parsed_body)
            continue
        last_success_headers = headers
        if parsed_body is None:
            continue
        if isinstance(parsed_body, list):
            saw_array = True
            combined_items.extend(parsed_body)
        else:
            saw_object = True
            combined_items.append(parsed_body)
    if failure is not None and last_success_headers is None:
        status, headers, body_json = failure
        return status, normalize_allowlisted_headers(headers), body_json
    if failure is not None:
        # Partial success then failure: surface the failure status for classification.
        status, headers, body_json = failure
        return status, normalize_allowlisted_headers(headers), body_json
    if saw_array and saw_object:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="gh paginated pages mixed array and object bodies",
            )
        )
    headers_source = last_success_headers if last_success_headers is not None else envelopes[-1][1]
    allowlisted = normalize_allowlisted_headers(headers_source)
    if not combined_items:
        body_json = [] if saw_array else None
    elif saw_array or len(combined_items) > 1:
        body_json = combined_items
    else:
        body_json = combined_items[0]
    return 200, allowlisted, body_json


def _loads_json_body(body_text: str) -> object | None:
    if not body_text.strip():
        return None
    try:
        loaded: object = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="gh response body was not valid JSON",
            )
        ) from exc
    return loaded


def normalize_allowlisted_headers(raw: Mapping[str, Sequence[str]]) -> AllowlistedHeaders:
    selected: dict[str, str] = {}
    for name, values in raw.items():
        key = name.lower()
        if key not in _ALLOWLISTED_HEADER_NAMES:
            continue
        unique = list(dict.fromkeys(values))
        if len(unique) > 1:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="contradictory allowlisted response headers",
                )
            )
        if unique:
            selected[key] = unique[0]

    retry_after: int | None = None
    if "retry-after" in selected:
        try:
            retry_after = int(selected["retry-after"])
        except ValueError as exc:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="Retry-After header was malformed",
                )
            ) from exc

    remaining: int | None = None
    if "x-ratelimit-remaining" in selected:
        try:
            remaining = int(selected["x-ratelimit-remaining"])
        except ValueError as exc:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="X-RateLimit-Remaining header was malformed",
                )
            ) from exc

    reset_epoch: int | None = None
    if "x-ratelimit-reset" in selected:
        try:
            reset_epoch = int(selected["x-ratelimit-reset"])
        except ValueError as exc:
            raise GhTransportError(
                block=block_for_kind(
                    GatewayBlockKind.MALFORMED_EVIDENCE,
                    detail="X-RateLimit-Reset header was malformed",
                )
            ) from exc

    return AllowlistedHeaders(
        retry_after_seconds=retry_after,
        rate_limit_remaining=remaining,
        rate_limit_reset_epoch=reset_epoch,
        rate_limit_resource=selected.get("x-ratelimit-resource"),
    )


def classify_http_and_graphql(
    *,
    status: int,
    headers: AllowlistedHeaders,
    body_json: object | None,
    returncode: int,
) -> GhTransportError | None:
    """Map HTTP/GraphQL outcomes to typed transport errors, or None on success."""

    rate_class = _detect_rate_limit_class(status=status, headers=headers, body_json=body_json)
    # Rate limiting must win over generic 403 permissions classification.
    if rate_class is not RateLimitClass.NONE:
        effective = status if status == 429 or status >= 500 else 429
        return GhTransportError(
            transient=transient_for_http_status(
                effective,
                headers=headers,
                rate_limit_class=rate_class,
            )
        )
    if status == 429 or 500 <= status <= 599:
        return GhTransportError(
            transient=transient_for_http_status(
                status,
                headers=headers,
                rate_limit_class=rate_class,
            )
        )

    if status in {401, 403}:
        kind = GatewayBlockKind.AUTHENTICATION if status == 401 else GatewayBlockKind.PERMISSIONS
        if status == 403 and _graphql_indicates_auth(body_json):
            kind = GatewayBlockKind.AUTHENTICATION
        return GhTransportError(block=block_for_kind(kind))

    if status == 404:
        return GhTransportError(block=block_for_kind(GatewayBlockKind.NOT_FOUND))

    if status == 422:
        return GhTransportError(block=block_for_kind(GatewayBlockKind.HTTP_VALIDATION_REJECTION))

    if (
        isinstance(body_json, dict)
        and body_json.get("errors")
        and rate_class is RateLimitClass.NONE
    ):
        return GhTransportError(
            block=block_for_kind(
                GatewayBlockKind.MALFORMED_EVIDENCE,
                detail="GraphQL response contained errors without complete data",
            )
        )

    if returncode != 0 and status < 400:
        if returncode == _GH_AUTH_EXIT_CODE:
            return GhTransportError(block=block_for_kind(GatewayBlockKind.AUTHENTICATION))
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
                safe_summary="temporary gh CLI failure",
                transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                headers=headers,
                http_status=status,
            )
        )
    return None


def _detect_rate_limit_class(
    *,
    status: int,
    headers: AllowlistedHeaders,
    body_json: object | None,
) -> RateLimitClass:
    if status == 429:
        return RateLimitClass.HTTP_429
    if headers.retry_after_seconds is not None and status in {403, 429}:
        # GitHub secondary rate limits commonly return 403 + Retry-After.
        resource = (headers.rate_limit_resource or "").lower()
        if "secondary" in resource:
            return RateLimitClass.SECONDARY
        if status == 403:
            return RateLimitClass.SECONDARY
        return RateLimitClass.HTTP_429
    if headers.rate_limit_remaining is not None and headers.rate_limit_remaining <= 0:
        resource = (headers.rate_limit_resource or "").lower()
        if "secondary" in resource:
            return RateLimitClass.SECONDARY
        return RateLimitClass.PRIMARY
    if isinstance(body_json, dict):
        errors = body_json.get("errors")
        if isinstance(errors, list):
            for err in errors:
                if not isinstance(err, dict):
                    continue
                message = str(err.get("message") or "").lower()
                type_name = str(err.get("type") or "").lower()
                if "secondary rate limit" in message:
                    return RateLimitClass.SECONDARY
                if "rate limit" in message or type_name == "rate_limited":
                    return RateLimitClass.PRIMARY
        message = str(body_json.get("message") or "").lower()
        if "rate limit" in message or "secondary rate limit" in message:
            if "secondary" in message:
                return RateLimitClass.SECONDARY
            return RateLimitClass.PRIMARY if status != 429 else RateLimitClass.HTTP_429
    return RateLimitClass.NONE


def _graphql_indicates_auth(body_json: object | None) -> bool:
    if not isinstance(body_json, dict):
        return False
    errors = body_json.get("errors")
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        type_name = str(err.get("type") or "").upper()
        if type_name in {"UNAUTHORIZED", "FORBIDDEN"}:
            return type_name == "UNAUTHORIZED"
    return False


def _classify_nonzero_without_envelope(result: ProcessResult) -> GhTransportError:
    """Classify nonzero ``gh`` exits lacking an HTTP envelope without persisting stderr."""

    if result.returncode == _GH_AUTH_EXIT_CODE:
        return GhTransportError(block=block_for_kind(GatewayBlockKind.AUTHENTICATION))
    # Allowlisted network/DNS markers only; never copy raw stderr into summaries.
    stderr = (result.stderr or "").lower()
    if any(
        marker in stderr
        for marker in (
            "name or service not known",
            "nodename nor servname provided",
            "temporary failure in name resolution",
            "could not resolve host",
            "getaddrinfo failed",
        )
    ):
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.DNS_FAILURE,
                safe_summary="DNS resolution failed for gh api",
                transient_kind=TransientErrorKind.DNS_FAILURE,
            )
        )
    if "connection refused" in stderr:
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.CONNECTION_REFUSED,
                safe_summary="connection refused for gh api",
                transient_kind=TransientErrorKind.CONNECTION_REFUSED,
            )
        )
    if "connection reset" in stderr:
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.CONNECTION_RESET,
                safe_summary="connection reset for gh api",
                transient_kind=TransientErrorKind.CONNECTION_RESET,
            )
        )
    if any(
        marker in stderr
        for marker in (
            "network is unreachable",
            "no route to host",
            "network unreachable",
        )
    ):
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.NETWORK_UNAVAILABLE,
                safe_summary="network unavailable for gh api",
                transient_kind=TransientErrorKind.NETWORK_UNAVAILABLE,
            )
        )
    if any(
        marker in stderr
        for marker in (
            "client.timeout exceeded",
            "context deadline exceeded",
            "i/o timeout",
            "request timed out",
            "operation timed out",
        )
    ):
        return GhTransportError(
            transient=GatewayTransient(
                kind=GatewayTransientKind.TIMEOUT,
                safe_summary="gh api call timed out",
                transient_kind=TransientErrorKind.TIMEOUT,
            )
        )
    return GhTransportError(
        transient=GatewayTransient(
            kind=GatewayTransientKind.TEMPORARY_CLI_FAILURE,
            safe_summary="temporary gh CLI failure",
            transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
        )
    )
