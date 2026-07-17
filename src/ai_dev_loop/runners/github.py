"""Typed GitHub CLI adapter for the optional post-PR review loop.

All GitHub API access goes through this module. Callers must never invoke
``gh api`` ad hoc elsewhere in the workflow. Authentication uses the user's
existing ``gh`` session; no tokens are stored or logged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.process import ProcessResult, run_process
from ai_dev_loop.redaction import redact_text

FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


class GithubErrorKind(StrEnum):
    AUTH = "auth"
    NOT_FOUND = "not_found"
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    VALIDATION = "validation"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GithubError:
    kind: GithubErrorKind
    message: str
    exit_code: int | None = None


@dataclass(frozen=True)
class GithubPullRequest:
    number: int
    url: str
    title: str
    state: str
    head_ref: str
    head_sha: str
    base_ref: str
    is_cross_repository: bool
    repository_name_with_owner: str


@dataclass(frozen=True)
class GithubReviewThread:
    thread_id: str
    is_resolved: bool
    author_login: str
    path: str | None
    line: int | None
    commit_sha: str | None
    root_comment_id: str
    root_comment_body_sha256: str
    created_at: str | None
    review_id: str | None


@dataclass(frozen=True)
class GithubIssueComment:
    comment_id: str
    author_login: str
    body_sha256: str
    created_at: str | None
    url: str | None = None


@dataclass(frozen=True)
class GithubAuthStatus:
    authenticated: bool
    login_redacted: str | None
    scopes_ok: bool
    detail: str


@dataclass(frozen=True)
class GithubWriteResult:
    ok: bool
    resource_id: str | None
    error: GithubError | None = None


def _safe_error_message(result: ProcessResult) -> str:
    combined = f"{result.stderr}\n{result.stdout}".strip()
    if not combined:
        return f"gh exited with code {result.returncode}"
    # Never return raw bodies that may contain tokens or comment text.
    redacted = redact_text(combined)
    first_line = redacted.splitlines()[0][:200]
    return first_line


def _classify_gh_failure(result: ProcessResult) -> GithubError:
    text = f"{result.stderr}\n{result.stdout}".lower()
    if result.timed_out:
        return GithubError(GithubErrorKind.TIMEOUT, "gh command timed out", result.returncode)
    if (
        "rate limit" in text
        or "secondary rate" in text
        or result.returncode == 403
        and "abuse" in text
    ):
        return GithubError(
            GithubErrorKind.RATE_LIMIT, "GitHub rate limit exceeded", result.returncode
        )
    if (
        "auth" in text
        or "not logged" in text
        or "authentication" in text
        or "HTTP 401" in text
        or result.returncode == 4
    ):
        return GithubError(
            GithubErrorKind.AUTH,
            "gh is not authenticated or lacks required permissions",
            result.returncode,
        )
    if "not found" in text or "HTTP 404" in text:
        return GithubError(
            GithubErrorKind.NOT_FOUND, "GitHub resource not found", result.returncode
        )
    if "conflict" in text or "HTTP 409" in text:
        return GithubError(GithubErrorKind.CONFLICT, "GitHub resource conflict", result.returncode)
    return GithubError(GithubErrorKind.UNKNOWN, _safe_error_message(result), result.returncode)


def run_gh(
    command: str,
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = 60.0,
) -> ProcessResult:
    return run_process([command, *args], cwd=cwd, timeout=timeout)


def check_gh_auth(command: str = "gh", *, timeout: float = 30.0) -> GithubAuthStatus:
    result = run_gh(command, ["auth", "status"], timeout=timeout)
    if result.returncode != 0:
        return GithubAuthStatus(
            authenticated=False,
            login_redacted=None,
            scopes_ok=False,
            detail=_classify_gh_failure(result).message,
        )
    login: str | None = None
    for line in result.stdout.splitlines():
        lowered = line.lower()
        if "logged in to github.com as" in lowered:
            parts = line.strip().split()
            if parts:
                candidate = parts[-1]
                login = f"{candidate[0]}…{candidate[-1]}" if len(candidate) > 2 else "<redacted>"
            break
    return GithubAuthStatus(
        authenticated=True,
        login_redacted=login,
        scopes_ok=True,
        detail="gh authenticated (account redacted)",
    )


def _parse_json(result: ProcessResult) -> Any:
    if result.returncode != 0:
        raise AiDevLoopError(_classify_gh_failure(result).message)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError("gh returned invalid JSON") from exc


def resolve_repository_nwo(command: str, *, cwd: str, timeout: float = 30.0) -> str:
    result = run_gh(
        command,
        ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AiDevLoopError(_classify_gh_failure(result).message)
    nwo = result.stdout.strip()
    if not nwo or "/" not in nwo:
        raise ValidationError("could not resolve repository nameWithOwner via gh")
    return nwo


def get_pull_request(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    timeout: float = 60.0,
) -> GithubPullRequest:
    result = run_gh(
        command,
        [
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,url,title,state,headRefName,headRefOid,baseRefName,isCrossRepository,headRepository",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    data = _parse_json(result)
    head_sha = str(data.get("headRefOid") or "")
    if not FULL_SHA_PATTERN.match(head_sha):
        raise ValidationError("pull request head SHA is missing or invalid")
    head_repo = data.get("headRepository") or {}
    nwo = str(head_repo.get("nameWithOwner") or "")
    if not nwo:
        nwo = resolve_repository_nwo(command, cwd=cwd, timeout=timeout)
    return GithubPullRequest(
        number=int(data["number"]),
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        state=str(data.get("state") or "").upper(),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=head_sha,
        base_ref=str(data.get("baseRefName") or ""),
        is_cross_repository=bool(data.get("isCrossRepository")),
        repository_name_with_owner=nwo,
    )


def find_open_pr_for_head(
    command: str,
    *,
    cwd: str,
    head_branch: str,
    base: str = "master",
    timeout: float = 60.0,
) -> GithubPullRequest | None:
    result = run_gh(
        command,
        [
            "pr",
            "list",
            "--head",
            head_branch,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number,url,title,state,headRefName,headRefOid,baseRefName,isCrossRepository,headRepository",
            "--limit",
            "5",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    items = _parse_json(result)
    if not isinstance(items, list) or not items:
        return None
    if len(items) > 1:
        raise ValidationError(
            f"ambiguous open PRs for head {head_branch!r} targeting {base!r}; "
            "resolve manually before creating a PR-review cycle"
        )
    data = items[0]
    head_sha = str(data.get("headRefOid") or "")
    if not FULL_SHA_PATTERN.match(head_sha):
        raise ValidationError("pull request head SHA is missing or invalid")
    head_repo = data.get("headRepository") or {}
    nwo = str(head_repo.get("nameWithOwner") or "")
    if not nwo:
        nwo = resolve_repository_nwo(command, cwd=cwd, timeout=timeout)
    return GithubPullRequest(
        number=int(data["number"]),
        url=str(data.get("url") or ""),
        title=str(data.get("title") or ""),
        state=str(data.get("state") or "").upper(),
        head_ref=str(data.get("headRefName") or ""),
        head_sha=head_sha,
        base_ref=str(data.get("baseRefName") or ""),
        is_cross_repository=bool(data.get("isCrossRepository")),
        repository_name_with_owner=nwo,
    )


def create_or_update_pull_request(
    command: str,
    *,
    cwd: str,
    head_branch: str,
    base: str,
    title: str,
    body: str,
    timeout: float = 120.0,
) -> GithubPullRequest:
    existing = find_open_pr_for_head(
        command, cwd=cwd, head_branch=head_branch, base=base, timeout=timeout
    )
    if existing is not None:
        # Title/body updates are optional; binding is by number/head.
        edit = run_gh(
            command,
            ["pr", "edit", str(existing.number), "--title", title, "--body", body],
            cwd=cwd,
            timeout=timeout,
        )
        if edit.returncode != 0:
            # Non-fatal for idempotent rebinding when edit is denied; re-fetch.
            pass
        return get_pull_request(command, cwd=cwd, pr_number=existing.number, timeout=timeout)

    result = run_gh(
        command,
        [
            "pr",
            "create",
            "--base",
            base,
            "--head",
            head_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AiDevLoopError(_classify_gh_failure(result).message)
    # Recreate binding from list/view rather than parsing the create URL alone.
    created = find_open_pr_for_head(
        command, cwd=cwd, head_branch=head_branch, base=base, timeout=timeout
    )
    if created is None:
        raise ValidationError("gh pr create succeeded but no open PR was found for the head branch")
    return created


def find_issue_comment_with_marker(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    marker: str,
    timeout: float = 60.0,
) -> tuple[str, str | None] | None:
    """Return ``(comment_id, created_at)`` when a PR comment contains the marker.

    Bodies are inspected only for the exact HTML comment needle and are not
    retained beyond this search.
    """

    if not marker.strip() or "-->" in marker or "<!--" in marker:
        raise ValidationError("request marker is invalid")
    needle = f"<!-- {marker} -->"
    result = run_gh(
        command,
        [
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "--paginate",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AiDevLoopError(_classify_gh_failure(result).message)
    items = _parse_json(result)
    if not isinstance(items, list):
        text = result.stdout.strip()
        items = []
        for line in text.splitlines():
            if line.strip():
                items.append(json.loads(line))
    for item in items:
        if not isinstance(item, dict):
            continue
        body = str(item.get("body") or "")
        if needle not in body:
            continue
        comment_id = item.get("id")
        if comment_id is None:
            continue
        created_at = item.get("created_at")
        return str(comment_id), str(created_at) if created_at else None
    return None


def create_issue_comment(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    body: str,
    timeout: float = 60.0,
) -> GithubWriteResult:
    from ai_dev_loop.process import run_process_streaming

    payload = json.dumps({"body": body})
    streamed = run_process_streaming(
        [
            command,
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "--method",
            "POST",
            "--input",
            "-",
        ],
        cwd=cwd,
        timeout=timeout,
        stdin_text=payload,
    )
    if streamed.returncode != 0:
        err = _classify_gh_failure(
            ProcessResult(
                args=list(streamed.args),
                returncode=streamed.returncode,
                stdout=streamed.stdout,
                stderr=streamed.stderr,
                timed_out=streamed.timed_out,
            )
        )
        return GithubWriteResult(ok=False, resource_id=None, error=err)
    try:
        data = json.loads(streamed.stdout)
    except json.JSONDecodeError:
        return GithubWriteResult(
            ok=False,
            resource_id=None,
            error=GithubError(GithubErrorKind.VALIDATION, "invalid comment create response"),
        )
    comment_id = data.get("id")
    if comment_id is None:
        return GithubWriteResult(
            ok=False,
            resource_id=None,
            error=GithubError(GithubErrorKind.VALIDATION, "comment create response missing id"),
        )
    return GithubWriteResult(ok=True, resource_id=str(comment_id))


def reply_to_review_thread(
    command: str,
    *,
    cwd: str,
    pull_request_review_thread_id: str,
    body: str,
    timeout: float = 60.0,
) -> GithubWriteResult:
    """Post an inline reply via GraphQL ``addPullRequestReviewThreadReply``."""

    mutation = (
        "mutation($threadId:ID!,$body:String!){"
        "addPullRequestReviewThreadReply(input:{"
        "pullRequestReviewThreadId:$threadId,body:$body"
        "}){comment{id}}}"
    )
    result = run_process_streaming_json(
        command,
        [
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"threadId={pull_request_review_thread_id}",
            "-f",
            f"body={body}",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    if isinstance(result, GithubError):
        return GithubWriteResult(ok=False, resource_id=None, error=result)
    try:
        comment_id = result["data"]["addPullRequestReviewThreadReply"]["comment"]["id"]
    except (KeyError, TypeError):
        return GithubWriteResult(
            ok=False,
            resource_id=None,
            error=GithubError(
                GithubErrorKind.VALIDATION, "thread reply response missing comment id"
            ),
        )
    return GithubWriteResult(ok=True, resource_id=str(comment_id))


def resolve_review_thread(
    command: str,
    *,
    cwd: str,
    pull_request_review_thread_id: str,
    timeout: float = 60.0,
) -> GithubWriteResult:
    mutation = (
        "mutation($threadId:ID!){"
        "resolveReviewThread(input:{threadId:$threadId}){thread{id isResolved}}}"
    )
    result = run_process_streaming_json(
        command,
        [
            "api",
            "graphql",
            "-f",
            f"query={mutation}",
            "-f",
            f"threadId={pull_request_review_thread_id}",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    if isinstance(result, GithubError):
        return GithubWriteResult(ok=False, resource_id=None, error=result)
    try:
        thread = result["data"]["resolveReviewThread"]["thread"]
        thread_id = thread["id"]
        if not thread.get("isResolved"):
            return GithubWriteResult(
                ok=False,
                resource_id=str(thread_id),
                error=GithubError(GithubErrorKind.CONFLICT, "thread was not resolved"),
            )
    except (KeyError, TypeError):
        return GithubWriteResult(
            ok=False,
            resource_id=None,
            error=GithubError(GithubErrorKind.VALIDATION, "resolve thread response incomplete"),
        )
    return GithubWriteResult(ok=True, resource_id=str(thread_id))


def list_review_threads(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    timeout: float = 120.0,
) -> list[GithubReviewThread]:
    """Fetch review threads with pagination via GraphQL."""

    from ai_dev_loop.state import sha256_text

    query = """
    query($owner:String!,$name:String!,$number:Int!,$cursor:String){
      repository(owner:$owner,name:$name){
        pullRequest(number:$number){
          reviewThreads(first:50, after:$cursor){
            pageInfo{hasNextPage endCursor}
            nodes{
              id
              isResolved
              comments(first:1){
                nodes{
                  id
                  body
                  createdAt
                  author{login}
                  commit{oid}
                  path
                  line
                  pullRequestReview{id}
                }
              }
            }
          }
        }
      }
    }
    """
    nwo = resolve_repository_nwo(command, cwd=cwd, timeout=min(timeout, 30.0))
    owner, name = nwo.split("/", 1)
    threads: list[GithubReviewThread] = []
    cursor: str | None = None
    while True:
        args = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={name}",
            "-F",
            f"number={pr_number}",
        ]
        if cursor:
            args.extend(["-F", f"cursor={cursor}"])
        else:
            args.extend(["-F", "cursor=null"])
        parsed = run_process_streaming_json(command, args, cwd=cwd, timeout=timeout)
        if isinstance(parsed, GithubError):
            raise AiDevLoopError(parsed.message)
        try:
            connection = parsed["data"]["repository"]["pullRequest"]["reviewThreads"]
            nodes = connection["nodes"] or []
            page = connection["pageInfo"]
        except (KeyError, TypeError) as exc:
            raise ValidationError("unexpected GraphQL reviewThreads shape") from exc
        for node in nodes:
            comments = (node.get("comments") or {}).get("nodes") or []
            if not comments:
                continue
            root = comments[0]
            author = (root.get("author") or {}).get("login") or ""
            body = root.get("body") or ""
            commit = root.get("commit") or {}
            review = root.get("pullRequestReview") or {}
            threads.append(
                GithubReviewThread(
                    thread_id=str(node["id"]),
                    is_resolved=bool(node.get("isResolved")),
                    author_login=str(author),
                    path=root.get("path"),
                    line=root.get("line"),
                    commit_sha=commit.get("oid"),
                    root_comment_id=str(root["id"]),
                    root_comment_body_sha256=sha256_text(body),
                    created_at=root.get("createdAt"),
                    review_id=str(review["id"]) if review.get("id") else None,
                )
            )
        if not page.get("hasNextPage"):
            break
        cursor = page.get("endCursor")
        if not cursor:
            break
    return threads


def list_issue_comments_after(
    command: str,
    *,
    cwd: str,
    pr_number: int,
    after_comment_id: str | None,
    timeout: float = 60.0,
) -> list[GithubIssueComment]:
    """List issue comments; filter client-side for continue-command matching."""

    from ai_dev_loop.state import sha256_text

    result = run_gh(
        command,
        [
            "api",
            f"repos/{{owner}}/{{repo}}/issues/{pr_number}/comments",
            "--paginate",
            "-q",
            ".[] | {id, user: .user.login, body, created_at: .created_at, html_url: .html_url}",
        ],
        cwd=cwd,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise AiDevLoopError(_classify_gh_failure(result).message)
    # --paginate with -q may emit NDJSON; tolerate both.
    text = result.stdout.strip()
    if not text:
        return []
    items: list[Any] = []
    try:
        loaded = json.loads(text)
        items = loaded if isinstance(loaded, list) else [loaded]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            items.append(json.loads(line))
    comments: list[GithubIssueComment] = []
    after_int = int(after_comment_id) if after_comment_id and after_comment_id.isdigit() else None
    for item in items:
        cid = str(item.get("id"))
        if after_int is not None and cid.isdigit() and int(cid) <= after_int:
            continue
        body = item.get("body") or ""
        comments.append(
            GithubIssueComment(
                comment_id=cid,
                author_login=str(item.get("user") or ""),
                body_sha256=sha256_text(body),
                created_at=item.get("created_at"),
                url=item.get("html_url"),
            )
        )
    return comments


def body_is_continue_command(body: str, *, continue_command: str) -> bool:
    """Return True only for the exact configured continue command (whitespace-normalized)."""

    normalized = " ".join(body.strip().split())
    expected = " ".join(continue_command.strip().split())
    return bool(expected) and normalized == expected


def continue_comment_authorized(
    *,
    body: str,
    author_login: str,
    continue_command: str,
    authorized_login: str,
) -> bool:
    """Authorize continue only when author and exact command both match config."""

    if not authorized_login.strip():
        return False
    if author_login.strip().lower() != authorized_login.strip().lower():
        return False
    return body_is_continue_command(body, continue_command=continue_command)


def _parse_github_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def filter_eligible_threads(
    threads: list[GithubReviewThread],
    *,
    reviewer_logins: list[str],
    bound_head_sha: str,
    already_processed_thread_ids: set[str],
    request_created_at: str | None,
) -> list[GithubReviewThread]:
    """Select unresolved bot threads bound to the request head and newer than the request.

    Fail closed when commit SHA or created_at provenance is missing.
    """

    if not FULL_SHA_PATTERN.match(bound_head_sha):
        raise ValidationError("bound head SHA is missing or invalid for thread eligibility")
    if not request_created_at or not request_created_at.strip():
        raise ValidationError("request_created_at is required for thread eligibility")
    request_at = _parse_github_timestamp(request_created_at)
    if request_at is None:
        raise ValidationError("request_created_at is not a valid timestamp")

    allowed = {login.lower() for login in reviewer_logins}
    eligible: list[GithubReviewThread] = []
    for thread in threads:
        if thread.is_resolved:
            continue
        if thread.thread_id in already_processed_thread_ids:
            continue
        if thread.author_login.lower() not in allowed:
            continue
        # Fail closed: missing or mismatched head SHA is never eligible.
        if not thread.commit_sha or not FULL_SHA_PATTERN.match(thread.commit_sha):
            continue
        if thread.commit_sha != bound_head_sha:
            continue
        if not thread.created_at:
            continue
        created_at = _parse_github_timestamp(thread.created_at)
        if created_at is None or created_at <= request_at:
            continue
        eligible.append(thread)
    return eligible


def run_process_streaming_json(
    command: str,
    args: list[str],
    *,
    cwd: str,
    timeout: float,
) -> dict[str, Any] | GithubError:
    from ai_dev_loop.process import run_process_streaming

    streamed = run_process_streaming(
        [command, *args],
        cwd=cwd,
        timeout=timeout,
    )
    if streamed.returncode != 0:
        return _classify_gh_failure(
            ProcessResult(
                args=list(streamed.args),
                returncode=streamed.returncode,
                stdout=streamed.stdout,
                stderr=streamed.stderr,
                timed_out=streamed.timed_out,
            )
        )
    try:
        data = json.loads(streamed.stdout)
    except json.JSONDecodeError:
        return GithubError(GithubErrorKind.VALIDATION, "gh returned invalid JSON")
    if not isinstance(data, dict):
        return GithubError(GithubErrorKind.VALIDATION, "gh returned non-object JSON")
    if data.get("errors"):
        return GithubError(GithubErrorKind.UNKNOWN, "GitHub GraphQL returned errors")
    return data
