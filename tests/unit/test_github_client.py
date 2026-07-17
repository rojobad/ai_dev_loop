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
