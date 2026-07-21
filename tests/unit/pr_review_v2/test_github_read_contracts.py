"""Unit tests for GitHub read policy and backoff contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from tests.unit.pr_review_v2.github_read_helpers import policy

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    FixedJitter,
    GatewayBlockKind,
    block_for_kind,
    compute_retry_backoff,
    local_base_delay_seconds,
    transient_for_http_status,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    PauseReasonKind,
    SafeActionKind,
    TransientErrorKind,
)


def test_policy_rejects_empty_reviewers_and_bad_timeouts() -> None:
    with pytest.raises(ValidationError):
        policy(reviewer_logins=())
    with pytest.raises(ValidationError):
        policy(per_call_timeout_seconds=0)
    with pytest.raises(ValidationError):
        policy(overall_timeout_seconds=10, per_call_timeout_seconds=20)
    with pytest.raises(ValidationError):
        policy(reviewed_commit_prefix_length=6)
    with pytest.raises(ValidationError):
        policy(accepted_no_findings_prefixes=("",))


def test_policy_defaults() -> None:
    p = policy()
    assert p.reviewer_logins == ("chatgpt-codex-connector",)
    assert p.poll_interval_seconds == 60
    assert p.no_findings_enabled is False
    assert p.max_server_directed_wait_seconds == 3600


def test_local_backoff_table_and_jitter_bounds() -> None:
    assert [local_base_delay_seconds(i) for i in range(1, 6)] == [10, 30, 90, 180, 300]
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    for attempt, base in enumerate([10, 30, 90, 180, 300], start=1):
        for factor in (-0.20, 0.0, 0.20):
            decision = compute_retry_backoff(
                failed_attempt=attempt,
                observation_time=now,
                headers=AllowlistedHeaders(),
                jitter=FixedJitter(factor),
            )
            assert decision.server_directed is False
            assert decision.applied_jitter_factor == factor
            expected = now + timedelta(seconds=base * (1 + factor))
            assert decision.next_attempt_at == expected


def test_retry_after_precedes_local_and_is_not_jittered() -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    decision = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(retry_after_seconds=120),
        jitter=FixedJitter(0.20),
    )
    assert decision.server_directed is True
    assert decision.applied_jitter_factor is None
    assert decision.next_attempt_at == now + timedelta(seconds=120)


def test_rate_limit_reset_and_one_hour_clamp() -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    far = int((now + timedelta(hours=5)).timestamp())
    decision = compute_retry_backoff(
        failed_attempt=2,
        observation_time=now,
        headers=AllowlistedHeaders(rate_limit_remaining=0, rate_limit_reset_epoch=far),
        jitter=FixedJitter(-0.20),
    )
    assert decision.server_directed is True
    assert decision.clamped_to_one_hour is True
    assert decision.next_attempt_at == now + timedelta(seconds=3600)


def test_policy_rejects_max_server_directed_wait_above_one_hour() -> None:
    with pytest.raises(ValidationError):
        policy(max_server_directed_wait_seconds=3601)


def test_server_directed_wait_boundary_at_and_above_one_hour() -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    at_cap = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(retry_after_seconds=3600),
        jitter=FixedJitter(0.20),
    )
    assert at_cap.server_directed is True
    assert at_cap.clamped_to_one_hour is False
    assert at_cap.next_attempt_at == now + timedelta(seconds=3600)
    above = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(retry_after_seconds=3601),
        jitter=FixedJitter(0.20),
        max_server_directed_wait_seconds=7200,  # injected above absolute ceiling
    )
    assert above.server_directed is True
    assert above.clamped_to_one_hour is True
    assert above.next_attempt_at == now + timedelta(seconds=3600)


def test_malformed_or_past_directed_falls_back_to_local() -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    past = int((now - timedelta(seconds=30)).timestamp())
    decision = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(
            retry_after_seconds=-1,
            rate_limit_remaining=0,
            rate_limit_reset_epoch=past,
        ),
        jitter=FixedJitter(0.0),
    )
    assert decision.server_directed is False
    assert decision.next_attempt_at == now + timedelta(seconds=10)


def test_block_and_transient_mapping() -> None:
    block = block_for_kind(GatewayBlockKind.NOT_FOUND)
    assert block.pause_reason is PauseReasonKind.NOT_FOUND
    assert block.safe_action.kind is SafeActionKind.RESUME_SAME_EFFECT
    branch = block_for_kind(GatewayBlockKind.BRANCH_DRIFT)
    assert branch.pause_reason is PauseReasonKind.BRANCH_DRIFT
    assert branch.safe_action.kind is SafeActionKind.OPEN_NEW_CYCLE_OR_STOP
    t500 = transient_for_http_status(500)
    assert t500.transient_kind is TransientErrorKind.HTTP_500
    t599 = transient_for_http_status(599)
    assert t599.transient_kind is TransientErrorKind.OTHER_HTTP_5XX
    assert "599" in t599.safe_summary
