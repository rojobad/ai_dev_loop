"""Unit tests for the typed GitHub CLI adapter."""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from ai_dev_loop.runners.github import (
    GithubReviewThread,
    body_is_continue_command,
    check_gh_auth,
    continue_comment_authorized,
    filter_eligible_threads,
    find_issue_comment_with_marker,
    get_pull_request,
)


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log_path = tmp_path / "gh.log"
    script = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json, os, sys
        args = sys.argv[1:]
        with open({repr(str(log_path))}, "a", encoding="utf-8") as handle:
            handle.write(repr(args) + "\\n")
        if args[:2] == ["auth", "status"]:
            if os.environ.get("FAKE_GH_AUTH") == "fail":
                print("not logged in", file=sys.stderr)
                sys.exit(1)
            print("Logged in to github.com as tester")
            sys.exit(0)
        if args[:2] == ["pr", "view"]:
            payload = {{
                "number": 7,
                "url": "https://example.test/pr/7",
                "title": "Demo",
                "state": "OPEN",
                "headRefName": "feature",
                "headRefOid": "a" * 40,
                "baseRefName": "master",
                "isCrossRepository": False,
                "headRepository": {{"nameWithOwner": "acme/demo"}},
            }}
            print(json.dumps(payload))
            sys.exit(0)
        if args[0] == "api" and "graphql" in args:
            payload = {{
                "data": {{
                    "repository": {{
                        "pullRequest": {{
                            "reviewThreads": {{
                                "pageInfo": {{"hasNextPage": False, "endCursor": None}},
                                "nodes": [],
                            }}
                        }}
                    }}
                }}
            }}
            print(json.dumps(payload))
            sys.exit(0)
        print("unexpected", file=sys.stderr)
        sys.exit(2)
        """
    )
    gh = bin_dir / "gh"
    _write_executable(gh, script)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return log_path


def test_check_gh_auth_ok(fake_gh: Path) -> None:
    status = check_gh_auth("gh")
    assert status.authenticated is True
    assert status.login_redacted is not None
    assert "tester" not in (status.login_redacted or "")


def test_check_gh_auth_fail(fake_gh: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_GH_AUTH", "fail")
    status = check_gh_auth("gh")
    assert status.authenticated is False


def test_get_pull_request_shape(fake_gh: Path, tmp_path: Path) -> None:
    pr = get_pull_request("gh", cwd=str(tmp_path), pr_number=7)
    assert pr.number == 7
    assert pr.head_sha == "a" * 40
    assert pr.base_ref == "master"
    assert pr.is_cross_repository is False


def test_filter_eligible_threads() -> None:
    request_at = "2026-07-16T12:00:00+00:00"
    threads = [
        GithubReviewThread(
            thread_id="T1",
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=1,
            commit_sha="a" * 40,
            root_comment_id="C1",
            root_comment_body_sha256="b" * 64,
            created_at="2026-07-16T12:01:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id="T2",
            is_resolved=False,
            author_login="random-user",
            path="a.py",
            line=2,
            commit_sha="a" * 40,
            root_comment_id="C2",
            root_comment_body_sha256="c" * 64,
            created_at="2026-07-16T12:01:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id="T3",
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=3,
            commit_sha="b" * 40,
            root_comment_id="C3",
            root_comment_body_sha256="d" * 64,
            created_at="2026-07-16T12:01:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id="T4",
            is_resolved=True,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=4,
            commit_sha="a" * 40,
            root_comment_id="C4",
            root_comment_body_sha256="e" * 64,
            created_at="2026-07-16T12:01:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id="T5",
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=5,
            commit_sha="a" * 40,
            root_comment_id="C5",
            root_comment_body_sha256="f" * 64,
            created_at="2026-07-16T11:59:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id="T6",
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=6,
            commit_sha=None,
            root_comment_id="C6",
            root_comment_body_sha256="g" * 64,
            created_at="2026-07-16T12:01:00+00:00",
            review_id=None,
        ),
    ]
    eligible = filter_eligible_threads(
        threads,
        reviewer_logins=["chatgpt-codex-connector"],
        bound_head_sha="a" * 40,
        already_processed_thread_ids=set(),
        request_created_at=request_at,
    )
    assert [item.thread_id for item in eligible] == ["T1"]


def test_filter_eligible_threads_requires_request_marker() -> None:
    with pytest.raises(Exception, match="request_created_at"):
        filter_eligible_threads(
            [],
            reviewer_logins=["chatgpt-codex-connector"],
            bound_head_sha="a" * 40,
            already_processed_thread_ids=set(),
            request_created_at=None,
        )


def test_continue_command_matching() -> None:
    assert body_is_continue_command(
        "@rojobad /ai-dev-loop continue",
        continue_command="@rojobad /ai-dev-loop continue",
    )
    assert not body_is_continue_command(
        "@ROJOBAD /ai-dev-loop continue",
        continue_command="@rojobad /ai-dev-loop continue",
    )
    assert not body_is_continue_command(
        "please continue the loop",
        continue_command="@rojobad /ai-dev-loop continue",
    )


def test_continue_requires_configured_author() -> None:
    assert continue_comment_authorized(
        body="@rojobad /ai-dev-loop continue",
        author_login="rojobad",
        continue_command="@rojobad /ai-dev-loop continue",
        authorized_login="rojobad",
    )
    assert not continue_comment_authorized(
        body="@rojobad /ai-dev-loop continue",
        author_login="someone-else",
        continue_command="@rojobad /ai-dev-loop continue",
        authorized_login="rojobad",
    )


def test_gh_args_are_logged_without_shell(fake_gh: Path, tmp_path: Path) -> None:
    get_pull_request("gh", cwd=str(tmp_path), pr_number=7)
    logged = fake_gh.read_text(encoding="utf-8")
    assert "pr" in logged
    assert "shell=True" not in logged


def test_find_issue_comment_with_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ai_dev_loop.process import ProcessResult

    marker = "ai_dev_loop-pr-review:run-1:cycle:1:" + ("a" * 40)
    payload = json.dumps(
        [
            {"id": 1, "body": "unrelated", "created_at": "2026-07-16T11:00:00Z"},
            {
                "id": 2,
                "body": f"@codex review\n\n<!-- {marker} -->",
                "created_at": "2026-07-16T12:00:00Z",
            },
        ]
    )

    def fake_run_gh(*_a, **_k):
        return ProcessResult(
            args=["gh", "api"],
            returncode=0,
            stdout=payload,
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    found = find_issue_comment_with_marker(
        "gh",
        cwd=str(tmp_path),
        pr_number=7,
        marker=marker,
    )
    assert found == ("2", "2026-07-16T12:00:00Z")
    assert (
        find_issue_comment_with_marker(
            "gh",
            cwd=str(tmp_path),
            pr_number=7,
            marker="ai_dev_loop-pr-review:other:cycle:1:" + ("b" * 40),
        )
        is None
    )


def _no_findings_body(sha_prefix: str) -> str:
    return f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{sha_prefix}`\n"


def test_match_no_findings_completion_accepts_exact_protocol() -> None:
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        match_no_findings_completion,
    )
    from ai_dev_loop.state import sha256_text

    sha = "83b5e2a652" + ("a" * 30)
    body = _no_findings_body(sha[:12])
    comments = [
        GithubIssueCommentDetail(
            comment_id="99",
            author_login="chatgpt-codex-connector",
            body=body,
            body_sha256=sha256_text(body),
            created_at="2026-07-17T12:05:00+00:00",
        )
    ]
    match = match_no_findings_completion(
        comments,
        reviewer_logins=["chatgpt-codex-connector"],
        bound_head_sha=sha,
        request_created_at="2026-07-17T12:00:00+00:00",
        accepted_comment_prefixes=["Codex Review: Didn't find any major issues."],
        reviewed_commit_prefix_length=12,
    )
    assert match is not None
    assert match.comment_id == "99"
    assert match.rule_id == "accepted_comment_prefix:0"
    assert match.reviewed_commit_prefix == sha[:12]
    assert match.body_sha256 == sha256_text(body)


@pytest.mark.parametrize(
    ("author", "created_at", "body", "reason"),
    [
        (
            "other-bot",
            "2026-07-17T12:05:00+00:00",
            _no_findings_body("83b5e2a652aa"),
            "author",
        ),
        (
            "chatgpt-codex-connector",
            "2026-07-17T11:59:00+00:00",
            _no_findings_body("83b5e2a652aa"),
            "older",
        ),
        (
            "chatgpt-codex-connector",
            "2026-07-17T12:05:00+00:00",
            _no_findings_body("deadbeef0000"),
            "sha",
        ),
        (
            "chatgpt-codex-connector",
            None,
            _no_findings_body("83b5e2a652aa"),
            "missing_ts",
        ),
        (
            "chatgpt-codex-connector",
            "2026-07-17T12:05:00+00:00",
            "Looks good to me overall.\n\n**Reviewed commit:** `83b5e2a652aa`\n",
            "free_phrase",
        ),
        (
            "chatgpt-codex-connector",
            "2026-07-17T12:05:00+00:00",
            "Codex Review: Didn't find any major issues.\n\nNo commit line.\n",
            "incomplete",
        ),
        (
            "chatgpt-codex-connector",
            "2026-07-16T10:00:00+00:00",
            "Manual earlier review\n\n**Reviewed commit:** `83b5e2a652aa`\n",
            "manual_prior",
        ),
    ],
)
def test_match_no_findings_completion_rejects(
    author: str, created_at: str | None, body: str, reason: str
) -> None:
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        match_no_findings_completion,
    )
    from ai_dev_loop.state import sha256_text

    sha = "83b5e2a652" + ("a" * 30)
    comments = [
        GithubIssueCommentDetail(
            comment_id="42",
            author_login=author,
            body=body,
            body_sha256=sha256_text(body),
            created_at=created_at,
        )
    ]
    match = match_no_findings_completion(
        comments,
        reviewer_logins=["chatgpt-codex-connector"],
        bound_head_sha=sha,
        request_created_at="2026-07-17T12:00:00+00:00",
        accepted_comment_prefixes=["Codex Review: Didn't find any major issues."],
        reviewed_commit_prefix_length=12,
    )
    assert match is None, reason


def test_match_eyes_acknowledgement_accepts_allowed_reviewer() -> None:
    from ai_dev_loop.runners.github import (
        GithubCommentReaction,
        match_eyes_acknowledgement,
    )

    match = match_eyes_acknowledgement(
        [
            GithubCommentReaction(
                reaction_id="1",
                user_login="chatgpt-codex-connector",
                content="eyes",
            )
        ],
        trigger_comment_id="55",
        reviewer_logins=["chatgpt-codex-connector"],
        reaction="eyes",
    )
    assert match is not None
    assert match.trigger_comment_id == "55"
    assert match.reaction == "eyes"


@pytest.mark.parametrize(
    ("content", "login"),
    [
        ("rocket", "chatgpt-codex-connector"),
        ("eyes", "someone-else"),
        ("", "chatgpt-codex-connector"),
    ],
)
def test_match_eyes_acknowledgement_rejects(content: str, login: str) -> None:
    from ai_dev_loop.runners.github import (
        GithubCommentReaction,
        match_eyes_acknowledgement,
    )

    assert (
        match_eyes_acknowledgement(
            [
                GithubCommentReaction(
                    reaction_id="1",
                    user_login=login,
                    content=content,
                )
            ],
            trigger_comment_id="55",
            reviewer_logins=["chatgpt-codex-connector"],
            reaction="eyes",
        )
        is None
    )


def test_list_issue_comment_reactions_and_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import (
        list_issue_comment_details,
        list_issue_comment_reactions,
        match_no_findings_completion,
    )

    sha = "a" * 40
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{sha[:12]}`\n"
    comments_payload = json.dumps(
        [
            {
                "id": 10,
                "user": {"login": "chatgpt-codex-connector"},
                "body": body,
                "created_at": "2026-07-17T12:05:00Z",
                "html_url": "https://example.test/c/10",
            }
        ]
    )
    reactions_payload = json.dumps(
        [
            {
                "id": 7,
                "user": {"login": "chatgpt-codex-connector"},
                "content": "eyes",
                "created_at": "2026-07-17T12:01:00Z",
            }
        ]
    )
    calls: list[list[str]] = []

    def fake_run_gh(_command: str, args: list[str], **_k):
        calls.append(list(args))
        if "reactions" in " ".join(args):
            return ProcessResult(
                args=["gh", *args],
                returncode=0,
                stdout=reactions_payload,
                stderr="",
                timed_out=False,
            )
        return ProcessResult(
            args=["gh", *args],
            returncode=0,
            stdout=comments_payload,
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    details = list_issue_comment_details("gh", cwd=str(tmp_path), pr_number=7)
    assert len(details) == 1
    assert details[0].body.startswith("Codex Review:")
    match = match_no_findings_completion(
        details,
        reviewer_logins=["chatgpt-codex-connector"],
        bound_head_sha=sha,
        request_created_at="2026-07-17T12:00:00+00:00",
        accepted_comment_prefixes=["Codex Review: Didn't find any major issues."],
        reviewed_commit_prefix_length=12,
    )
    assert match is not None
    # Matcher evidence must not retain the body.
    assert not hasattr(match, "body")

    reactions = list_issue_comment_reactions("gh", cwd=str(tmp_path), comment_id="10")
    assert len(reactions) == 1
    assert reactions[0].content == "eyes"
    assert any("reactions" in " ".join(c) for c in calls)


def test_list_issue_comment_reactions_sanitizes_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.errors import AiDevLoopError
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import list_issue_comment_reactions

    secret_body = (
        "gh: HTTP 401 SECRET_TOKEN=abc123\n"
        "Codex Review: Didn't find any major issues. full comment body"
    )

    def fake_run_gh(*_a, **_k):
        return ProcessResult(
            args=["gh", "api"],
            returncode=1,
            stdout="",
            stderr=secret_body,
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    with pytest.raises(AiDevLoopError) as excinfo:
        list_issue_comment_reactions("gh", cwd=str(tmp_path), comment_id="10")
    message = str(excinfo.value)
    assert "SECRET_TOKEN=abc123" not in message
    assert "Didn't find any major issues" not in message
    assert "full comment body" not in message


def test_list_issue_comment_details_flattens_multipage_compact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import list_issue_comment_details

    page1 = [{"id": 1, "user": {"login": "a"}, "body": "page-one-body", "created_at": "t1"}]
    page2 = [{"id": 2, "user": {"login": "b"}, "body": "page-two-body", "created_at": "t2"}]
    # gh --paginate concatenates JSON page arrays without a wrapping array.
    payload = json.dumps(page1) + "\n" + json.dumps(page2)

    def fake_run_gh(*_a, **_k):
        return ProcessResult(
            args=["gh", "api"],
            returncode=0,
            stdout=payload,
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    details = list_issue_comment_details("gh", cwd=str(tmp_path), pr_number=7)
    assert [d.comment_id for d in details] == ["1", "2"]
    assert details[0].author_login == "a"
    assert details[1].author_login == "b"
    # Bodies are available ephemerally for matching but are never the list payload shape.
    assert details[0].body == "page-one-body"


def test_list_issue_comment_details_flattens_multipage_pretty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import list_issue_comment_details

    payload = (
        '[\n  {"id": 11, "user": {"login": "bot"}, "body": "pretty-one", "created_at": "t1"}\n]\n'
        '[\n  {"id": 12, "user": {"login": "bot"}, "body": "pretty-two", "created_at": "t2"}\n]\n'
    )

    def fake_run_gh(*_a, **_k):
        return ProcessResult(
            args=["gh", "api"],
            returncode=0,
            stdout=payload,
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    details = list_issue_comment_details("gh", cwd=str(tmp_path), pr_number=7)
    assert [d.comment_id for d in details] == ["11", "12"]


def test_list_issue_comment_reactions_flattens_multipage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import list_issue_comment_reactions

    page1 = [{"id": 1, "user": {"login": "chatgpt-codex-connector"}, "content": "eyes"}]
    page2 = [{"id": 2, "user": {"login": "other"}, "content": "rocket"}]
    payload = json.dumps(page1, indent=2) + "\n" + json.dumps(page2, indent=2)

    def fake_run_gh(*_a, **_k):
        return ProcessResult(
            args=["gh", "api"],
            returncode=0,
            stdout=payload,
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr("ai_dev_loop.runners.github.run_gh", fake_run_gh)
    reactions = list_issue_comment_reactions("gh", cwd=str(tmp_path), comment_id="10")
    assert [r.reaction_id for r in reactions] == ["1", "2"]
    assert reactions[0].content == "eyes"
    assert reactions[1].content == "rocket"


def test_parse_paginated_json_items_rejects_invalid_without_body_leak() -> None:
    from ai_dev_loop.errors import ValidationError
    from ai_dev_loop.process import ProcessResult
    from ai_dev_loop.runners.github import _parse_paginated_json_items

    result = ProcessResult(
        args=["gh", "api"],
        returncode=0,
        stdout='[{"id": 1}]\nnot-json-SECRET_BODY_LEAK',
        stderr="",
        timed_out=False,
    )
    with pytest.raises(ValidationError, match="invalid JSON") as excinfo:
        _parse_paginated_json_items(result)
    assert "SECRET_BODY_LEAK" not in str(excinfo.value)


def test_eyes_absence_is_not_completion_signal() -> None:
    from ai_dev_loop.runners.github import match_eyes_acknowledgement

    assert (
        match_eyes_acknowledgement(
            [],
            trigger_comment_id="1",
            reviewer_logins=["chatgpt-codex-connector"],
            reaction="eyes",
        )
        is None
    )
