"""Unit tests for scheduler usage-limit retry-after resolution."""

from __future__ import annotations

from ai_dev_loop.scheduler.application.cursor_retry_after import (
    DEFAULT_USAGE_LIMIT_RETRY_SECONDS,
    resolve_usage_limit_retry_seconds,
)


def test_retry_after_accepts_valid_integer() -> None:
    assert resolve_usage_limit_retry_seconds(({"retry_after_seconds": 120},)) == 120


def test_retry_after_missing_uses_five_hour_fallback() -> None:
    assert (
        resolve_usage_limit_retry_seconds(({"message": "usage limit"},))
        == DEFAULT_USAGE_LIMIT_RETRY_SECONDS
    )


def test_retry_after_malformed_uses_five_hour_fallback() -> None:
    assert (
        resolve_usage_limit_retry_seconds(({"retry_after_seconds": "120"},))
        == DEFAULT_USAGE_LIMIT_RETRY_SECONDS
    )


def test_retry_after_out_of_range_uses_five_hour_fallback() -> None:
    assert (
        resolve_usage_limit_retry_seconds(({"retry_after_seconds": 90000},))
        == DEFAULT_USAGE_LIMIT_RETRY_SECONDS
    )


def test_retry_after_skips_malformed_record_and_uses_next_valid() -> None:
    assert (
        resolve_usage_limit_retry_seconds(
            (
                {"retry_after_seconds": "not-an-int"},
                {"retry_after_seconds": 3600},
            )
        )
        == 3600
    )


def test_retry_after_integral_float_uses_five_hour_fallback() -> None:
    assert (
        resolve_usage_limit_retry_seconds(({"retry_after_seconds": 120.0},))
        == DEFAULT_USAGE_LIMIT_RETRY_SECONDS
    )


def test_retry_after_never_parsed_from_prose_field() -> None:
    assert (
        resolve_usage_limit_retry_seconds(({"message": "retry after 120 seconds from provider"},))
        == DEFAULT_USAGE_LIMIT_RETRY_SECONDS
    )
