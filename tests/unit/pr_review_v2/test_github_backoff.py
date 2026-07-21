"""Backoff-focused unit tests (alias coverage for plan inventory)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    FixedJitter,
    compute_retry_backoff,
)


def test_attempt_six_not_accepted_by_backoff_calculator() -> None:
    now = datetime(2026, 7, 21, tzinfo=UTC)
    with pytest.raises(ValueError, match="1..5"):
        compute_retry_backoff(
            failed_attempt=6,
            observation_time=now,
            headers=AllowlistedHeaders(),
            jitter=FixedJitter(0.0),
        )


def test_timezone_normalization_preserves_absolute_instant() -> None:
    now = datetime(2026, 7, 21, 8, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    decision = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(retry_after_seconds=30),
        jitter=FixedJitter(0.0),
    )
    assert decision.next_attempt_at == now + timedelta(seconds=30)
    assert decision.next_attempt_at.astimezone(UTC) == datetime(2026, 7, 21, 12, 0, 30, tzinfo=UTC)


def test_absolute_one_hour_cap_clamps_injected_ceiling() -> None:
    now = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)
    decision = compute_retry_backoff(
        failed_attempt=1,
        observation_time=now,
        headers=AllowlistedHeaders(retry_after_seconds=4000),
        jitter=FixedJitter(0.0),
        max_server_directed_wait_seconds=10_000,
    )
    assert decision.clamped_to_one_hour is True
    assert decision.next_attempt_at == now + timedelta(seconds=3600)
