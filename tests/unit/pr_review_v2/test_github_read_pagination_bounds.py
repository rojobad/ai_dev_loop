"""Unit tests for deadline-bounded pagination and reaction page reads."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.github_read_helpers import (
    FakeTransport,
    observe_effect,
    policy,
    queue_waiting,
)

from ai_dev_loop.pr_review_v2.application.github_read import AllowlistedHeaders, GhTransportResult
from ai_dev_loop.pr_review_v2.domain.common import TransientErrorKind
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore


class FakeMonotonic:
    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, delta: float) -> None:
        self.t += delta


def _reaction_item(reaction_id: int) -> dict:
    return {
        "id": reaction_id,
        "user": {"login": "chatgpt-codex-connector"},
        "content": "eyes",
        "created_at": "2026-07-21T12:01:00Z",
    }


def _full_reaction_page(start_id: int, *, count: int = 100) -> GhTransportResult:
    return GhTransportResult(
        http_status=200,
        headers=AllowlistedHeaders(),
        body_json=[_reaction_item(start_id + i) for i in range(count)],
        returncode=0,
    )


def _gateway(
    tmp_path: Path,
    transport: FakeTransport,
    *,
    mono: FakeMonotonic,
    **policy_kwargs,
) -> GitHubReadGateway:
    return GitHubReadGateway(
        policy=policy(**policy_kwargs),
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
        monotonic=mono,
    )


def test_reaction_page_overflow_is_malformed(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    # Two full pages with max_pages=1 => overflow on second page attempt.
    transport.reaction_pages.extend(
        [
            _full_reaction_page(1),
            _full_reaction_page(101),
        ]
    )
    mono = FakeMonotonic()
    gateway = _gateway(tmp_path, transport, mono=mono, max_pages=1, max_items=500)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(
            observe_effect(),
            observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
        )
    assert exc.value.block is not None
    assert "page limit" in exc.value.block.safe_summary
    assert sum(1 for op, _ in transport.calls if op == "reactions") == 1


def test_reaction_item_overflow_is_malformed(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    transport.reaction_pages.append(_full_reaction_page(1, count=100))
    mono = FakeMonotonic()
    gateway = _gateway(tmp_path, transport, mono=mono, max_pages=20, max_items=50)
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(
            observe_effect(),
            observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
        )
    assert exc.value.block is not None
    assert "item limit" in exc.value.block.safe_summary


def test_deadline_exhaustion_between_reaction_pages(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    transport.reaction_pages.extend(
        [
            _full_reaction_page(1),
            _full_reaction_page(101, count=1),
        ]
    )
    mono = FakeMonotonic(start=0.0)

    def advance_past_deadline() -> None:
        # After first reaction page, exhaust the overall deadline.
        if sum(1 for op, _ in transport.calls if op == "reactions") >= 1:
            mono.t = 10_000.0

    transport.on_call = advance_past_deadline
    gateway = _gateway(
        tmp_path,
        transport,
        mono=mono,
        per_call_timeout_seconds=30.0,
        overall_timeout_seconds=60.0,
    )
    with pytest.raises(GhTransportError) as exc:
        gateway.observe_with_artifact(
            observe_effect(),
            observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
        )
    assert exc.value.transient is not None
    assert exc.value.transient.transient_kind is TransientErrorKind.TIMEOUT
    assert sum(1 for op, _ in transport.calls if op == "reactions") == 1


def test_final_call_timeout_uses_remaining_deadline_not_full_per_call(tmp_path: Path) -> None:
    transport = FakeTransport()
    # First monotonic read builds deadline (0 + 50). Later reads leave 25s remaining,
    # which is below the configured per-call timeout of 40s.
    readings = {"n": 0}

    def mono() -> float:
        readings["n"] += 1
        return 0.0 if readings["n"] == 1 else 25.0

    queue_waiting(transport)
    gateway = GitHubReadGateway(
        policy=policy(per_call_timeout_seconds=40.0, overall_timeout_seconds=50.0),
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
        monotonic=mono,
    )
    gateway.observe_with_artifact(
        observe_effect(),
        observation_time=datetime(2026, 7, 21, 12, 10, tzinfo=UTC),
    )
    identity_calls = [kwargs for op, kwargs in transport.calls if op == "identity"]
    assert identity_calls
    timeout = identity_calls[0]["timeout_seconds"]
    assert timeout is not None
    assert timeout == pytest.approx(25.0)
    assert timeout < 40.0
