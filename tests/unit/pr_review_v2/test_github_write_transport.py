"""Phase 16.6 unit tests: predefined ``gh api`` mutation + reconciliation transport."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.gh_write_transport import (
    ADD_REPLY_MUTATION,
    RESOLVE_THREAD_MUTATION,
    GhWriteTransport,
)
from ai_dev_loop.process import ProcessResult


@dataclass
class FakeCliRunner:
    stdout: str = ""
    returncode: int = 0
    stderr: str = ""
    timed_out: bool = False
    calls: list[tuple[list[str], str | None]] = field(default_factory=list)

    def run(self, args, *, cwd, timeout, env=None, stdin_text=None):
        self.calls.append((list(args), stdin_text))
        return ProcessResult(
            args=list(args),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
            timed_out=self.timed_out,
        )


def _envelope(status: int, body: object) -> str:
    return f"HTTP/2 {status}\nContent-Type: application/json\n\n{json.dumps(body)}"


def _transport(runner: FakeCliRunner) -> GhWriteTransport:
    return GhWriteTransport(
        command="fake-gh", cwd="/tmp/repo", per_call_timeout_seconds=30.0, runner=runner
    )


def test_create_pull_request_argv_and_stdin() -> None:
    body = {"number": 7, "state": "open", "head": {"ref": "feature"}, "base": {"ref": "main"}}
    runner = FakeCliRunner(stdout=_envelope(201, body))
    result = _transport(runner).create_pull_request(
        owner="acme",
        name="demo",
        head="feature",
        base="main",
        title="My PR",
        body="the body <!-- marker -->",
    )
    assert result.http_status == 201
    argv, stdin = runner.calls[-1]
    assert argv[:3] == ["fake-gh", "api", "repos/acme/demo/pulls"]
    assert "--method" in argv and "POST" in argv
    assert "--input" in argv and "-" in argv
    # Body/title travel via stdin, never argv.
    joined = " ".join(argv)
    assert "the body" not in joined
    assert "My PR" not in joined
    payload = json.loads(stdin)
    assert payload["title"] == "My PR"
    assert payload["head"] == "feature"
    assert payload["body"] == "the body <!-- marker -->"


def test_update_pull_request_uses_patch() -> None:
    body = {"number": 7, "state": "open", "head": {"ref": "feature"}, "base": {"ref": "main"}}
    runner = FakeCliRunner(stdout=_envelope(200, body))
    _transport(runner).update_pull_request(owner="acme", name="demo", number=7, title="t", body="b")
    argv, _ = runner.calls[-1]
    assert "repos/acme/demo/pulls/7" in argv
    assert "PATCH" in argv


def test_create_issue_comment_endpoint() -> None:
    runner = FakeCliRunner(stdout=_envelope(201, {"id": 1, "body": "x"}))
    _transport(runner).create_issue_comment(owner="acme", name="demo", number=7, body="hi")
    argv, stdin = runner.calls[-1]
    assert "repos/acme/demo/issues/7/comments" in argv
    assert "POST" in argv
    assert json.loads(stdin)["body"] == "hi"


def test_add_review_thread_reply_graphql_stdin() -> None:
    body = {"data": {"addPullRequestReviewThreadReply": {"comment": {"id": "C1", "body": "b"}}}}
    runner = FakeCliRunner(stdout=_envelope(200, body))
    _transport(runner).add_review_thread_reply(thread_id="THREAD_1", body="a reply")
    argv, stdin = runner.calls[-1]
    assert argv[:3] == ["fake-gh", "api", "graphql"]
    payload = json.loads(stdin)
    assert payload["query"].strip() == ADD_REPLY_MUTATION
    assert payload["variables"]["threadId"] == "THREAD_1"
    assert payload["variables"]["body"] == "a reply"
    # Reply body never in argv.
    assert "a reply" not in " ".join(argv)


def test_resolve_review_thread_graphql() -> None:
    body = {"data": {"resolveReviewThread": {"thread": {"id": "T", "isResolved": True}}}}
    runner = FakeCliRunner(stdout=_envelope(200, body))
    _transport(runner).resolve_review_thread(thread_id="THREAD_1")
    _argv, stdin = runner.calls[-1]
    payload = json.loads(stdin)
    assert payload["query"].strip() == RESOLVE_THREAD_MUTATION


def test_list_prs_by_head_base_bounded_page_get() -> None:
    runner = FakeCliRunner(stdout=_envelope(200, [{"number": 7}]))
    result = _transport(runner).list_prs_by_head_base(
        owner="acme", name="demo", head="feature", base="main", page=1, per_page=100
    )
    argv, _ = runner.calls[-1]
    assert "GET" in argv
    assert "--paginate" not in argv
    assert "page=1" in argv[2]
    assert "per_page=100" in argv[2]
    assert result.body_json == [{"number": 7}]


def test_fetch_thread_resolved_graphql() -> None:
    body = {"data": {"node": {"id": "T", "isResolved": True}}}
    runner = FakeCliRunner(stdout=_envelope(200, body))
    result = _transport(runner).fetch_thread_resolved(thread_id="THREAD_1")
    assert result.body_json["data"]["node"]["isResolved"] is True


def test_timeout_raises_transient() -> None:
    runner = FakeCliRunner(timed_out=True)
    with pytest.raises(GhTransportError) as exc:
        _transport(runner).resolve_review_thread(thread_id="THREAD_1")
    assert exc.value.transient is not None


def test_malformed_envelope_with_success_returncode_blocks() -> None:
    runner = FakeCliRunner(stdout="not-an-http-envelope", returncode=0)
    with pytest.raises(GhTransportError) as exc:
        _transport(runner).fetch_thread_resolved(thread_id="THREAD_1")
    assert exc.value.block is not None


def test_malformed_envelope_with_failure_returncode_is_transient() -> None:
    runner = FakeCliRunner(stdout="garbage", returncode=1)
    with pytest.raises(GhTransportError) as exc:
        _transport(runner).fetch_thread_resolved(thread_id="THREAD_1")
    assert exc.value.transient is not None


def test_http_500_is_transient() -> None:
    runner = FakeCliRunner(stdout=_envelope(500, {"message": "server error"}), returncode=1)
    with pytest.raises(GhTransportError) as exc:
        _transport(runner).fetch_thread_resolved(thread_id="THREAD_1")
    assert exc.value.transient is not None


def test_no_generic_query_surface() -> None:
    transport = _transport(FakeCliRunner())
    for banned in ("graphql", "rest", "request", "api", "query"):
        assert not hasattr(transport, banned), f"unexpected public method: {banned}"


def test_invalid_repo_tokens_rejected() -> None:
    runner = FakeCliRunner(stdout=_envelope(200, {}))
    with pytest.raises(GhTransportError):
        _transport(runner).create_pull_request(
            owner="bad owner", name="demo", head="f", base="m", title="t", body="b"
        )
