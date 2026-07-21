"""Unit tests for ObserveBotReviewEffect executor outcomes."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import (
    MARKER,
    SHA_B,
    FakeTransport,
    observe_effect,
    policy,
    queue_waiting,
    result_from_fixture,
    token_for,
)

from ai_dev_loop.pr_review_v2.application.github_read import AllowlistedHeaders, GhTransportResult
from ai_dev_loop.pr_review_v2.domain.common import RepositoryIdentity, TransientErrorKind
from ai_dev_loop.pr_review_v2.domain.effects import RequestBotReviewEffect
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.workers.github_read_executor import (
    GitHubReadExecutor,
    UnsupportedEffectError,
)


def _executor(tmp_path: Path, transport: FakeTransport, **policy_kwargs) -> GitHubReadExecutor:
    pol = policy(**policy_kwargs)
    gateway = GitHubReadGateway(
        policy=pol,
        transport=transport,
        artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
    )
    return GitHubReadExecutor(
        gateway=gateway,
        policy=pol,
        clock=FakeClock(datetime(2026, 7, 21, 12, 10, tzinfo=UTC)),
    )


def test_unsupported_effect_makes_zero_transport_calls(tmp_path: Path) -> None:
    transport = FakeTransport()
    executor = _executor(tmp_path, transport)
    effect = RequestBotReviewEffect(
        effect_id="e",
        idempotency_key="e",
        run_id="run-1",
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=SHA_B,
        binding=observe_effect().binding,
        marker=MARKER,
    )
    with pytest.raises(UnsupportedEffectError):
        executor.execute(effect, token_for(observe_effect()), now=datetime.now(tz=UTC))
    assert transport.calls == []


def test_waiting_outcome_increments_poll_not_attempt(tmp_path: Path) -> None:
    transport = FakeTransport()
    queue_waiting(transport)
    executor = _executor(tmp_path, transport)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind == "effect_succeeded"
    assert event.outcome.kind == "bot_still_waiting"
    assert event.outcome.poll_sequence == 2
    assert event.outcome.next_not_before == datetime(2026, 7, 21, 12, 11, tzinfo=UTC)


def test_eligible_threads_and_privacy(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(result_from_fixture("issue_comments_waiting_and_nofindings.txt"))
    transport.thread_pages.append(result_from_fixture("review_threads_eligible.txt"))
    executor = _executor(tmp_path, transport)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind == "effect_succeeded"
    assert event.outcome.kind == "eligible_threads_observed"
    frozen = event.outcome.frozen
    assert frozen.thread_ids == ("PRRT_1",)
    text = (tmp_path / "artifacts").rglob("*.json")
    contents = "\n".join(p.read_text(encoding="utf-8") for p in text)
    assert "ghp_" not in contents
    assert "token=" not in contents
    assert "<!--" not in contents


def test_no_findings_requires_opt_in(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(result_from_fixture("issue_comments_waiting_and_nofindings.txt"))
    transport.thread_pages.append(
        GhTransportResult(
            http_status=200,
            headers=AllowlistedHeaders(),
            body_json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                }
            },
            returncode=0,
        )
    )
    executor = _executor(
        tmp_path,
        transport,
        accepted_no_findings_prefixes=("No findings found.",),
    )
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.outcome.kind == "verified_no_findings"


def test_transient_maps_to_retryable_failure(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("http_500.txt"))
    executor = _executor(tmp_path, transport)
    effect = observe_effect(attempt=1)
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind == "effect_retryable_failure"
    assert event.error.kind is TransientErrorKind.HTTP_500
    assert event.failed_attempt == 1
    assert event.next_attempt_at == datetime(2026, 7, 21, 12, 10, 10, tzinfo=UTC)


def test_head_drift_blocks(tmp_path: Path) -> None:
    transport = FakeTransport()
    body = result_from_fixture("pr_identity_ok.txt")
    assert isinstance(body.body_json, dict)
    body.body_json["data"]["repository"]["pullRequest"]["headRefOid"] = "c" * 40
    transport.identity_pages.append(body)
    executor = _executor(tmp_path, transport)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind == "effect_blocked"
    assert event.reason.value == "head_drift"
