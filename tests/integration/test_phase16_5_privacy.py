"""Phase 16.5 privacy checks for artifacts, events, and status."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import (
    FakeTransport,
    observe_effect,
    policy,
    result_from_fixture,
    token_for,
)

from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor

SECRET_MARKERS = (
    "ghp_",
    "token=",
    "Authorization:",
    "GH_TOKEN",
    "-----BEGIN",
)


def test_eligible_thread_artifacts_and_events_are_sanitized(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(result_from_fixture("issue_comments_waiting_and_nofindings.txt"))
    transport.thread_pages.append(result_from_fixture("review_threads_eligible.txt"))
    clock = FakeClock(datetime(2026, 7, 21, 12, 10, tzinfo=UTC))
    pol = policy()
    executor = GitHubReadExecutor(
        gateway=GitHubReadGateway(
            policy=pol,
            transport=transport,
            artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
        ),
        policy=pol,
        clock=clock,
    )
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=clock.now())
    dumped = event.model_dump_json()
    artifact_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "artifacts").rglob("*")
        if path.is_file()
    )
    for marker in SECRET_MARKERS:
        assert marker.lower() not in dumped.lower()
        assert marker.lower() not in artifact_text.lower()
    assert "<!--" not in artifact_text
    assert event.outcome.kind == "eligible_threads_observed"
