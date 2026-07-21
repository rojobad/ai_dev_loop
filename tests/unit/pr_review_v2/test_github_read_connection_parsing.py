"""Strict fail-closed GraphQL connection parsing tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from tests.unit.pr_review_v2.github_read_helpers import (
    FakeTransport,
    observe_effect,
    policy,
    result_from_fixture,
)

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    GatewayBlockKind,
    GhTransportResult,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore


def _ok_identity() -> GhTransportResult:
    return result_from_fixture("pr_identity_ok.txt")


def _comments_with_trigger() -> GhTransportResult:
    return result_from_fixture("issue_comments_waiting_and_nofindings.txt")


def _threads_payload(connection: dict[str, Any]) -> GhTransportResult:
    return GhTransportResult(
        http_status=200,
        headers=AllowlistedHeaders(),
        body_json={"data": {"repository": {"pullRequest": {"reviewThreads": connection}}}},
        returncode=0,
    )


def _gateway(tmp_path: Path, transport: FakeTransport) -> GitHubReadGateway:
    return GitHubReadGateway(
        policy=policy(),
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
    )


def _observe(tmp_path: Path, transport: FakeTransport) -> None:
    _gateway(tmp_path, transport).observe_with_artifact(
        observe_effect(),
        observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("connection", "detail_fragment"),
    [
        (None, "connection was not an object"),
        ({"pageInfo": {"hasNextPage": False, "endCursor": None}}, "nodes field was missing"),
        (
            {"nodes": None, "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "nodes were null",
        ),
        (
            {"nodes": {"id": "x"}, "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "nodes were not a list",
        ),
        ({"nodes": []}, "pageInfo field was missing"),
        ({"nodes": [], "pageInfo": None}, "pageInfo was not an object"),
        ({"nodes": [], "pageInfo": {"endCursor": None}}, "hasNextPage field was missing"),
        (
            {"nodes": [], "pageInfo": {"hasNextPage": 1, "endCursor": None}},
            "hasNextPage was not a boolean",
        ),
        (
            {"nodes": [], "pageInfo": {"hasNextPage": "true", "endCursor": "c1"}},
            "hasNextPage was not a boolean",
        ),
        (
            {"nodes": [{"id": "t1"}], "pageInfo": {"hasNextPage": True}},
            "endCursor was missing",
        ),
        (
            {"nodes": [{"id": "t1"}], "pageInfo": {"hasNextPage": True, "endCursor": None}},
            "endCursor was not a non-empty string",
        ),
        (
            {"nodes": [{"id": "t1"}], "pageInfo": {"hasNextPage": True, "endCursor": 123}},
            "endCursor was not a non-empty string",
        ),
        (
            {"nodes": [{"id": "t1"}], "pageInfo": {"hasNextPage": True, "endCursor": ""}},
            "endCursor was not a non-empty string",
        ),
    ],
)
def test_malformed_review_thread_connections_fail_closed(
    tmp_path: Path, connection: object, detail_fragment: str
) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(_ok_identity())
    transport.comment_pages.append(_comments_with_trigger())
    transport.thread_pages.append(_threads_payload(connection))  # type: ignore[arg-type]
    with pytest.raises(GhTransportError) as exc:
        _observe(tmp_path, transport)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.MALFORMED_EVIDENCE
    assert detail_fragment in exc.value.block.safe_summary
    # Must not persist a waiting observation artifact from coerced empty nodes.
    artifacts = list((tmp_path / "artifacts").rglob("*.json"))
    assert artifacts == []


def test_review_thread_nodes_null_does_not_become_bot_still_waiting(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(_ok_identity())
    transport.comment_pages.append(_comments_with_trigger())
    transport.thread_pages.append(
        _threads_payload({"nodes": None, "pageInfo": {"hasNextPage": False, "endCursor": None}})
    )
    with pytest.raises(GhTransportError) as exc:
        _observe(tmp_path, transport)
    assert exc.value.block is not None
    assert exc.value.block.kind is GatewayBlockKind.MALFORMED_EVIDENCE
    assert "nodes were null" in exc.value.block.safe_summary
    assert list((tmp_path / "artifacts").rglob("*.json")) == []


def test_unchanged_cursor_is_malformed(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(_ok_identity())
    transport.comment_pages.append(_comments_with_trigger())
    transport.thread_pages.extend(
        [
            _threads_payload(
                {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-a"},
                }
            ),
            _threads_payload(
                {
                    "nodes": [],
                    "pageInfo": {"hasNextPage": True, "endCursor": "cursor-a"},
                }
            ),
        ]
    )
    with pytest.raises(GhTransportError) as exc:
        _observe(tmp_path, transport)
    assert exc.value.block is not None
    assert "cursor did not advance" in exc.value.block.safe_summary
