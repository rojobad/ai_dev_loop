"""Stateful scripted fake ``gh`` for production-boundary PR review v2 tests."""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

SCRIPT_PATH = Path(__file__).resolve()


def _envelope(status: int, body: object, *, headers: dict[str, str] | None = None) -> str:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    header_lines = "\n".join(f"{key}: {value}" for key, value in hdrs.items())
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return f"HTTP/2 {status}\n{header_lines}\n\n{payload}"


@dataclass
class StatefulFakeGhState:
    fixture: dict[str, Any] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    mutation_counts: dict[str, int] = field(default_factory=dict)
    failure_queue: list[dict[str, Any]] = field(default_factory=list)
    applied_markers: list[str] = field(default_factory=list)
    resolved_threads: list[str] = field(default_factory=list)
    issue_comments: list[dict[str, Any]] = field(default_factory=list)
    thread_replies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    consumed_failures: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "calls": self.calls,
            "mutation_counts": self.mutation_counts,
            "failure_queue": self.failure_queue,
            "consumed_failures": self.consumed_failures,
            "applied_markers": self.applied_markers,
            "resolved_threads": self.resolved_threads,
            "issue_comments": self.issue_comments,
            "thread_replies": self.thread_replies,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        tmp.write_text(
            json.dumps(self.to_payload(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> StatefulFakeGhState:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            fixture=raw.get("fixture") or {},
            calls=list(raw.get("calls") or []),
            mutation_counts=dict(raw.get("mutation_counts") or {}),
            failure_queue=list(raw.get("failure_queue") or []),
            applied_markers=list(raw.get("applied_markers") or []),
            resolved_threads=list(raw.get("resolved_threads") or []),
            issue_comments=list(raw.get("issue_comments") or []),
            thread_replies=dict(raw.get("thread_replies") or {}),
            consumed_failures=list(raw.get("consumed_failures") or []),
        )


class StatefulFakeGhController:
    """Manage durable fake ``gh`` state and install an argv-compatible executable."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.state = StatefulFakeGhState()
        self.state.save(state_path)

    @classmethod
    def attach(cls, state_path: Path) -> StatefulFakeGhController:
        """Attach to an existing durable state file without wiping it."""

        obj = cls.__new__(cls)
        obj.state_path = state_path
        if state_path.is_file():
            obj.state = StatefulFakeGhState.load(state_path)
        else:
            obj.state = StatefulFakeGhState()
            obj.state.save(state_path)
        return obj

    def seed_waiting_observation(
        self,
        *,
        marker: str,
        head_sha: str,
        owner: str = "acme",
        name: str = "demo",
        pr_number: int = 7,
        head_branch: str = "feature",
        base_branch: str = "main",
        trigger_comment_id: int = 101,
    ) -> None:
        self.state.fixture = {
            "owner": owner,
            "name": name,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "head_branch": head_branch,
            "base_branch": base_branch,
            "marker": marker,
            "trigger_comment_id": trigger_comment_id,
        }
        self.state.issue_comments = [
            {
                "id": f"IC_{trigger_comment_id}",
                "databaseId": trigger_comment_id,
                "body": f"please review <!-- {marker} -->",
                "createdAt": "2026-07-21T12:00:00Z",
                "author": {"login": "orchestrator"},
            }
        ]
        self.state.save(self.state_path)

    def queue_failure(self, kind: str, **extra: Any) -> None:
        self.state.failure_queue.append({"kind": kind, **extra})
        self.state.save(self.state_path)

    def queue_thumbs_up(
        self, *, actor: str = "chatgpt-codex-connector", comment_id: int = 101
    ) -> None:
        reactions = self.state.fixture.setdefault("reactions", {})
        reactions[str(comment_id)] = [
            {
                "id": 9001,
                "user": {"login": actor},
                "content": "+1",
                "created_at": "2026-07-21T12:05:00Z",
            }
        ]
        self.state.save(self.state_path)

    def install(self, bin_dir: Path) -> Path:
        bin_dir.mkdir(parents=True, exist_ok=True)
        gh_path = bin_dir / "gh"
        gh_path.write_text(
            "#!/usr/bin/env python3\n"
            f"import os, runpy, sys\n"
            f"os.environ['STATEFUL_GH_STATE'] = {str(self.state_path.resolve())!r}\n"
            f"runpy.run_path({str(SCRIPT_PATH)!r}, run_name='__main__')\n",
            encoding="utf-8",
        )
        gh_path.chmod(gh_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return gh_path

    @property
    def calls(self) -> list[str]:
        return StatefulFakeGhState.load(self.state_path).calls

    @property
    def mutation_counts(self) -> dict[str, int]:
        return StatefulFakeGhState.load(self.state_path).mutation_counts

    def reload(self) -> StatefulFakeGhState:
        self.state = StatefulFakeGhState.load(self.state_path)
        return self.state


def _is_mutating_call(argv: list[str], stdin_text: str = "") -> bool:
    joined = " ".join(argv)
    if "--method POST" in joined or "--method PATCH" in joined:
        return True
    if "graphql" in joined and "--input" in joined and stdin_text.strip():
        parsed = _parse_graphql_call(argv, stdin_text)
        if parsed is not None:
            query, _variables = parsed
            if any(
                token in query
                for token in (
                    "ResolveReviewThread",
                    "resolveReviewThread",
                    "AddReviewThreadReply",
                    "addPullRequestReviewThreadReply",
                )
            ):
                return True
    return False


def _pop_failure(state: StatefulFakeGhState) -> dict[str, Any] | None:
    if not state.failure_queue:
        return None
    item = state.failure_queue.pop(0)
    return item


def _identity_payload(state: StatefulFakeGhState) -> dict[str, Any]:
    fx = state.fixture
    return {
        "data": {
            "repository": {
                "nameWithOwner": f"{fx['owner']}/{fx['name']}",
                "pullRequest": {
                    "number": fx["pr_number"],
                    "state": "OPEN",
                    "isCrossRepository": False,
                    "headRefName": fx.get("head_branch", "feature"),
                    "baseRefName": fx.get("base_branch", "main"),
                    "headRefOid": fx["head_sha"],
                },
            }
        }
    }


def _comments_payload(state: StatefulFakeGhState) -> dict[str, Any]:
    nodes = list(state.issue_comments)
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


def _threads_payload(state: StatefulFakeGhState) -> dict[str, Any]:
    fx = state.fixture
    nodes = fx.get("threads") or []
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": nodes,
                    }
                }
            }
        }
    }


def _http_failure_status(kind: object) -> int | None:
    """Return the HTTP status for ``http_<status>`` failure fixtures."""

    if not isinstance(kind, str) or not kind.startswith("http_"):
        return None
    suffix = kind[len("http_") :]
    if not suffix.isdigit() or len(suffix) != 3:
        return None
    return int(suffix)


def _handle_failure(
    state: StatefulFakeGhState,
    failure: dict[str, Any],
    argv: list[str],
    *,
    stdin_text: str = "",
    state_path: Path | None = None,
) -> tuple[int, str, str]:
    kind = failure.get("kind")
    if kind == "timeout":
        time.sleep(float(failure.get("seconds", 3600)))
        return 1, "", "timed out"
    if kind == "dns":
        return 1, "", "Could not resolve host: api.github.com"
    if kind == "primary_rate_limit":
        reset_epoch = int(failure.get("reset_epoch", int(time.time()) + 120))
        return (
            0,
            _envelope(
                403,
                {"message": "rate limit"},
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_epoch),
                },
            ),
            "",
        )
    status = _http_failure_status(kind)
    if status == 429:
        retry_after = str(failure.get("retry_after", 10))
        return (
            0,
            _envelope(429, {"message": "rate limit"}, headers={"Retry-After": retry_after}),
            "",
        )
    if status is not None:
        return 0, _envelope(status, {"message": f"http {status}"}), ""
    if kind == "malformed":
        return 0, "not-json", ""
    if kind == "apply_then_hang":
        _apply_mutation(state, argv, stdin_text)
        if state_path is not None:
            state.save(state_path)
        time.sleep(float(failure.get("seconds", 3600)))
        return 1, "", "timed out after apply"
    return 97, "", f"unknown failure kind {kind!r}"


def _apply_mutation(state: StatefulFakeGhState, argv: list[str], stdin_text: str = "") -> None:
    joined = " ".join(argv)
    payload: dict[str, Any] = {}
    if stdin_text.strip():
        try:
            payload = json.loads(stdin_text)
        except json.JSONDecodeError:
            payload = {}
    if "graphql" in joined:
        parsed = _parse_graphql_call(argv, stdin_text)
        if parsed is not None:
            query, variables = parsed
            if "ResolveReviewThread" in query or "resolveReviewThread" in query:
                state.mutation_counts["resolve_review_thread"] = (
                    state.mutation_counts.get("resolve_review_thread", 0) + 1
                )
                thread_id = str(
                    variables.get("threadId") or state.fixture.get("default_thread_id", "PRRT_1")
                )
                if thread_id not in state.resolved_threads:
                    state.resolved_threads.append(thread_id)
                return
            if "AddReviewThreadReply" in query or "addPullRequestReviewThreadReply" in query:
                state.mutation_counts["add_review_thread_reply"] = (
                    state.mutation_counts.get("add_review_thread_reply", 0) + 1
                )
                thread_id = str(
                    variables.get("threadId") or state.fixture.get("default_thread_id", "PRRT_1")
                )
                body = str(variables.get("body") or "reply")
                state.thread_replies.setdefault(thread_id, []).append({"body": body})
                return
    if (
        "issues/" in joined
        and ("--method POST" in joined or " POST " in joined)
        and "/comments" in joined
    ):
        body = payload.get("body") if isinstance(payload, dict) else None
        if body is None:
            body_start = joined.find('{"body"')
            if body_start >= 0:
                body = json.loads(joined[body_start:]).get("body")
        if body:
            comment_id = 200 + len(state.issue_comments)
            state.issue_comments.append(
                {
                    "id": f"IC_{comment_id}",
                    "databaseId": comment_id,
                    "body": body,
                    "createdAt": "2026-07-21T12:01:00Z",
                    "author": {"login": "orchestrator"},
                }
            )
            marker = state.fixture.get("marker")
            if marker and marker in body:
                state.applied_markers.append(marker)
        state.mutation_counts["create_issue_comment"] = (
            state.mutation_counts.get("create_issue_comment", 0) + 1
        )
        return
    if "ResolveReviewThread" in joined or "resolveReviewThread" in joined:
        state.mutation_counts["resolve_review_thread"] = (
            state.mutation_counts.get("resolve_review_thread", 0) + 1
        )
        thread_id = (
            payload.get("variables", {}).get("threadId") if isinstance(payload, dict) else None
        ) or state.fixture.get("default_thread_id", "PRRT_1")
        state.resolved_threads.append(str(thread_id))
        return
    if "AddReviewThreadReply" in joined or "addPullRequestReviewThreadReply" in joined:
        state.mutation_counts["add_review_thread_reply"] = (
            state.mutation_counts.get("add_review_thread_reply", 0) + 1
        )
        thread_id = (
            payload.get("variables", {}).get("threadId") if isinstance(payload, dict) else None
        ) or state.fixture.get("default_thread_id", "PRRT_1")
        body = str(
            (payload.get("variables") or {}).get("body") if isinstance(payload, dict) else "reply"
        )
        state.thread_replies.setdefault(str(thread_id), []).append({"body": body})
        return
    if ("--method POST" in joined or "--method PATCH" in joined) and "/pulls" in joined:
        fx = state.fixture
        title = fx.get("title", "Title")
        body = fx.get("body", "Body")
        if isinstance(payload, dict):
            title = str(payload.get("title") or title)
            body = str(payload.get("body") or body)
            fx["title"] = title
            fx["body"] = body
        pr_body = {
            "number": fx.get("pr_number", 7),
            "title": title,
            "body": body,
            "state": "OPEN",
            "head": {
                "ref": fx.get("head_branch", "feature"),
                "sha": fx["head_sha"],
                "repo": {"full_name": f"{fx['owner']}/{fx['name']}"},
            },
            "base": {"ref": fx.get("base_branch", "main")},
        }
        fx["pull_requests"] = [pr_body]
        if "--method POST" in joined:
            state.mutation_counts["create_pull_request"] = (
                state.mutation_counts.get("create_pull_request", 0) + 1
            )
        else:
            state.mutation_counts["update_pull_request"] = (
                state.mutation_counts.get("update_pull_request", 0) + 1
            )
        return
    state.mutation_counts["other_mutation"] = state.mutation_counts.get("other_mutation", 0) + 1


def _thread_comments_payload(state: StatefulFakeGhState, thread_id: str) -> dict[str, Any]:
    nodes = list(state.thread_replies.get(thread_id, []))
    resolved = thread_id in state.resolved_threads
    return {
        "data": {
            "node": {
                "id": thread_id,
                "isResolved": resolved,
                "comments": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": nodes,
                },
            }
        }
    }


def _thread_resolved_payload(state: StatefulFakeGhState, thread_id: str) -> dict[str, Any]:
    resolved = thread_id in state.resolved_threads
    return {
        "data": {
            "node": {
                "id": thread_id,
                "isResolved": resolved,
                "pullRequest": {"number": state.fixture.get("pr_number", 7)},
            }
        }
    }


def _parse_graphql_call(argv: list[str], stdin_text: str) -> tuple[str, dict[str, Any]] | None:
    joined = " ".join(argv)
    if "graphql" not in joined:
        return None
    if "--input" in argv and stdin_text.strip():
        payload = json.loads(stdin_text)
        return str(payload.get("query") or ""), dict(payload.get("variables") or {})
    for index, arg in enumerate(argv):
        if arg == "-f" and index + 1 < len(argv) and argv[index + 1].startswith("query="):
            query = argv[index + 1][len("query=") :]
            variables: dict[str, Any] = {}
            cursor = index + 2
            while cursor < len(argv):
                if argv[cursor] == "-F" and cursor + 1 < len(argv):
                    key, _, raw = argv[cursor + 1].partition("=")
                    if raw == "null":
                        variables[key] = None
                    elif raw.isdigit():
                        variables[key] = int(raw)
                    else:
                        variables[key] = raw
                    cursor += 2
                else:
                    break
            return query, variables
    return None


def _graphql_response(
    state: StatefulFakeGhState, query: str, variables: dict[str, Any]
) -> tuple[int, str, str] | None:
    if "ReviewThreadComments" in query:
        thread_id = str(
            variables.get("threadId") or state.fixture.get("default_thread_id", "PRRT_1")
        )
        return 0, _envelope(200, _thread_comments_payload(state, thread_id)), ""
    if "ReviewThreadResolved" in query:
        thread_id = str(
            variables.get("threadId") or state.fixture.get("default_thread_id", "PRRT_1")
        )
        return 0, _envelope(200, _thread_resolved_payload(state, thread_id)), ""
    if "PullRequestIdentity" in query:
        return 0, _envelope(200, _identity_payload(state)), ""
    if "PullRequestIssueComments" in query:
        return 0, _envelope(200, _comments_payload(state)), ""
    if "PullRequestReviewThreads" in query:
        return 0, _envelope(200, _threads_payload(state)), ""
    if "ResolveReviewThread" in query or "resolveReviewThread" in query:
        thread_id = str(
            variables.get("threadId") or state.fixture.get("default_thread_id", "THREAD_1")
        )
        state.mutation_counts["resolve_review_thread"] = (
            state.mutation_counts.get("resolve_review_thread", 0) + 1
        )
        if thread_id not in state.resolved_threads:
            state.resolved_threads.append(thread_id)
        return (
            0,
            _envelope(
                200,
                {
                    "data": {
                        "resolveReviewThread": {"thread": {"id": thread_id, "isResolved": True}}
                    }
                },
            ),
            "",
        )
    if "AddReviewThreadReply" in query or "addPullRequestReviewThreadReply" in query:
        thread_id = str(
            variables.get("threadId") or state.fixture.get("default_thread_id", "THREAD_1")
        )
        body = str(variables.get("body") or "reply")
        state.mutation_counts["add_review_thread_reply"] = (
            state.mutation_counts.get("add_review_thread_reply", 0) + 1
        )
        state.thread_replies.setdefault(thread_id, []).append(
            {
                "id": "RC_reply",
                "body": body,
                "createdAt": "2026-07-21T12:02:00Z",
                "author": {"login": "orchestrator"},
            }
        )
        return (
            0,
            _envelope(
                200,
                {
                    "data": {
                        "addPullRequestReviewThreadReply": {
                            "comment": {"id": "RC_reply", "body": body}
                        }
                    }
                },
            ),
            "",
        )
    return None


def handle_fake_gh_argv(
    argv: list[str],
    state: StatefulFakeGhState,
    *,
    stdin_text: str | None = None,
    state_path: Path | None = None,
) -> tuple[int, str, str]:
    joined = " ".join(argv)
    state.calls.append(" ".join(argv[1:]))
    if stdin_text is None:
        stdin_text = sys.stdin.read() if "--input" in joined else ""
    failure = None
    if state.failure_queue:
        peek = state.failure_queue[0]
        mutation_only = peek.get("mutation_only") or peek.get("kind") == "apply_then_hang"
        if mutation_only:
            if _is_mutating_call(argv, stdin_text or ""):
                failure = _pop_failure(state)
        else:
            failure = _pop_failure(state)
    if failure is not None:
        state.consumed_failures.append(dict(failure))
        if state_path is not None:
            state.save(state_path)
        return _handle_failure(
            state,
            failure,
            argv,
            stdin_text=stdin_text,
            state_path=state_path,
        )

    if "graphql" in joined:
        parsed = _parse_graphql_call(argv, stdin_text)
        if parsed is not None:
            query, variables = parsed
            graphql = _graphql_response(state, query, variables)
            if graphql is not None:
                return graphql
        if "--input" in joined:
            _apply_mutation(state, argv, stdin_text)
            return 0, _envelope(200, {"data": {"ok": True}}), ""

    if "/reactions" in joined and "--method GET" in joined:
        comment_id = joined.split("/issues/comments/")[1].split("/")[0]
        reactions = state.fixture.get("reactions", {}).get(comment_id, [])
        return 0, _envelope(200, reactions), ""

    if "/pulls?" in joined and "--method GET" in joined:
        fx = state.fixture
        custom = fx.get("pull_requests")
        if custom is not None:
            items = custom
        else:
            items = [
                {
                    "number": fx.get("pr_number", 7),
                    "title": fx.get("title", "Title"),
                    "body": fx.get("body", "Body"),
                    "state": "OPEN",
                    "head": {
                        "ref": fx.get("head_branch", "feature"),
                        "sha": fx["head_sha"],
                        "repo": {"full_name": f"{fx['owner']}/{fx['name']}"},
                    },
                    "base": {"ref": fx.get("base_branch", "main")},
                }
            ]
        return (0, _envelope(200, items), "")

    if "/pulls/" in joined and "--method GET" in joined and "?" not in joined.split("/pulls/")[1]:
        fx = state.fixture
        return (
            0,
            _envelope(
                200,
                {
                    "number": fx.get("pr_number", 7),
                    "title": fx.get("title", "Title"),
                    "body": fx.get("body", "Body"),
                    "state": "OPEN",
                    "head": {
                        "ref": fx.get("head_branch", "feature"),
                        "sha": fx["head_sha"],
                        "repo": {"full_name": f"{fx['owner']}/{fx['name']}"},
                    },
                    "base": {"ref": fx.get("base_branch", "main")},
                },
            ),
            "",
        )

    if "/git/ref/heads/" in joined and "--method GET" in joined:
        fx = state.fixture
        branch = joined.split("/git/ref/heads/")[-1].split()[0]
        return (
            0,
            _envelope(200, {"ref": f"refs/heads/{branch}", "object": {"sha": fx["head_sha"]}}),
            "",
        )

    if "--method POST" in joined or "--method PATCH" in joined:
        _apply_mutation(state, argv, stdin_text)
        fx = state.fixture
        # Issue-comment creates must not overwrite durable PR title/body preimage.
        if "/issues/" in joined and "/comments" in joined:
            latest = (
                state.issue_comments[-1]
                if state.issue_comments
                else {
                    "id": 999,
                    "databaseId": 999,
                    "body": "",
                }
            )
            return 0, _envelope(201, latest), ""
        title = fx.get("title", "Title")
        body = fx.get("body", "Body")
        if stdin_text.strip() and "/pulls" in joined:
            try:
                payload = json.loads(stdin_text)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                title = str(payload.get("title") or title)
                body = str(payload.get("body") or body)
                fx["title"] = title
                fx["body"] = body
        pr_body = {
            "number": fx.get("pr_number", 7),
            "title": title,
            "body": body,
            "state": "OPEN",
            "head": {
                "ref": fx.get("head_branch", "feature"),
                "sha": fx["head_sha"],
                "repo": {"full_name": f"{fx['owner']}/{fx['name']}"},
            },
            "base": {"ref": fx.get("base_branch", "main")},
        }
        if "/pulls" in joined:
            state.fixture["pull_requests"] = [pr_body]
            return 0, _envelope(201 if "--method POST" in joined else 200, pr_body), ""
        return 0, _envelope(201, {"id": 999}), ""

    return 97, "", f"stateful fake gh: no scripted response for argv: {argv}"


def run_fake_gh(argv: list[str] | None = None, *, stdin_text: str | None = None) -> int:
    args = argv if argv is not None else sys.argv
    state_path = Path(os.environ["STATEFUL_GH_STATE"])
    state = StatefulFakeGhState.load(state_path)
    if stdin_text is None and "--input" in " ".join(args):
        stdin_text = sys.stdin.read()
    code, stdout, stderr = handle_fake_gh_argv(
        args, state, stdin_text=stdin_text, state_path=state_path
    )
    state.save(state_path)
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(run_fake_gh())
