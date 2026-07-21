"""Unit tests for gh transport parsing, classification, and typed read argv."""

from __future__ import annotations

import pytest
from tests.unit.pr_review_v2.github_read_helpers import ScriptedGhRunner, load_fixture

from ai_dev_loop.pr_review_v2.application.github_read import GatewayBlockKind
from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind as DomainTransient
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import (
    GhApiTransport,
    GhTransportError,
    normalize_allowlisted_headers,
    parse_include_envelope,
    parse_include_envelopes,
    parse_paginated_include,
)


def test_parse_include_envelope_and_allowlisted_headers() -> None:
    status, headers, body = parse_include_envelope(load_fixture("pr_identity_ok.txt"))
    assert status == 200
    assert "x-ratelimit-remaining" in headers
    allow = normalize_allowlisted_headers(headers)
    assert allow.rate_limit_remaining == 4999
    assert body.strip().startswith("{")


def test_duplicate_header_conflict_is_malformed() -> None:
    with pytest.raises(GhTransportError) as exc:
        normalize_allowlisted_headers({"retry-after": ["10", "20"]})
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.MALFORMED_EVIDENCE


def test_typed_identity_argv_is_direct_named_query_without_shell() -> None:
    runner = ScriptedGhRunner()
    runner.add_include("graphql", "pr_identity_ok.txt")
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    result = transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert result.http_status == 200
    argv = runner.calls[0]
    assert argv[0] == "fake-gh"
    assert argv[1:4] == ["api", "graphql", "--include"]
    assert any(part.startswith("query=") for part in argv)
    assert "mutation" not in " ".join(argv).lower()
    assert not hasattr(transport, "graphql") or not callable(
        getattr(type(transport), "graphql", None)
    )


def test_mutation_query_rejected_via_private_path() -> None:
    runner = ScriptedGhRunner()
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError):
        transport._graphql(  # noqa: SLF001 - proving private path still rejects mutations
            operation="bad",
            query="mutation DoWrite { addComment(input:{}) { clientMutationId } }",
            variables={},
        )
    assert runner.calls == []


def test_reactions_use_validated_owner_repo_page_by_page_without_paginate() -> None:
    runner = ScriptedGhRunner()
    runner.add_json(
        "repos/acme/demo/issues/comments/101/reactions?per_page=100&page=1",
        [{"id": 1, "user": {"login": "bot"}, "content": "eyes"}],
    )
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    transport.fetch_issue_comment_reactions_page(
        owner="acme", name="demo", comment_id="101", page=1, per_page=100, timeout_seconds=12.5
    )
    argv = runner.calls[0]
    assert "--method" in argv
    assert argv[argv.index("--method") + 1] == "GET"
    assert "--paginate" not in argv
    assert "repos/acme/demo/issues/comments/101/reactions?per_page=100&page=1" in argv
    assert "{owner}" not in " ".join(argv)
    assert runner.calls  # timeout was applied by transport to runner


def test_http_500_is_transient() -> None:
    runner = ScriptedGhRunner()
    runner.add_include("graphql", "http_500.txt", returncode=1)
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.transient is not None
    assert exc.value.transient.transient_kind is DomainTransient.HTTP_500


def test_graphql_rate_limit_under_http_200() -> None:
    runner = ScriptedGhRunner()
    runner.add_include("graphql", "graphql_primary_rate_limit.txt")
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.transient is not None
    assert exc.value.transient.transient_kind is DomainTransient.PRIMARY_RATE_LIMIT


def test_403_rate_limit_beats_permissions() -> None:
    runner = ScriptedGhRunner()
    runner.add_json(
        "graphql",
        {"message": "You have exceeded a secondary rate limit"},
        status=403,
        headers="Retry-After: 60\nX-RateLimit-Resource: core",
    )
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.transient is not None
    assert exc.value.transient.transient_kind is DomainTransient.SECONDARY_RATE_LIMIT
    assert exc.value.block is None


def test_ordinary_403_is_permissions_block() -> None:
    runner = ScriptedGhRunner()
    runner.add_json(
        "graphql",
        {"message": "Resource not accessible by integration"},
        status=403,
        headers="Content-Type: application/json",
    )
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.PERMISSIONS


def test_gh_exit_code_4_without_envelope_is_authentication() -> None:
    runner = ScriptedGhRunner()
    runner.add_raw("graphql", stdout="not an http envelope", stderr="auth required", returncode=4)
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.AUTHENTICATION
    assert "auth required" not in exc.value.block.safe_summary


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("curl: (6) Could not resolve host: api.github.com", DomainTransient.DNS_FAILURE),
        ("dial tcp: connection refused", DomainTransient.CONNECTION_REFUSED),
        ("read: connection reset by peer", DomainTransient.CONNECTION_RESET),
        ("connect: network is unreachable", DomainTransient.NETWORK_UNAVAILABLE),
        ('Get "https://api.github.com": context deadline exceeded', DomainTransient.TIMEOUT),
    ],
)
def test_network_failures_without_envelope_are_typed(stderr: str, kind: DomainTransient) -> None:
    runner = ScriptedGhRunner()
    runner.add_raw("graphql", stdout="", stderr=stderr, returncode=1)
    transport = GhApiTransport(
        command="fake-gh",
        cwd="/tmp/repo",
        per_call_timeout_seconds=30.0,
        runner=runner,
        env={},
    )
    with pytest.raises(GhTransportError) as exc:
        transport.fetch_pull_request_identity(owner="acme", name="demo", number=7)
    assert exc.value.transient is not None
    assert exc.value.transient.transient_kind is kind
    assert stderr not in exc.value.transient.safe_summary


def test_paginated_include_combines_all_successful_pages_in_order() -> None:
    stdout = load_fixture("reactions_multi_envelope.txt")
    envelopes = parse_include_envelopes(stdout)
    assert len(envelopes) == 2
    status, headers, body = parse_paginated_include(stdout)
    assert status == 200
    assert headers.rate_limit_remaining == 40
    assert isinstance(body, list)
    assert [item["id"] for item in body] == [1, 2, 3]
    assert "secret-token" not in str(body)
