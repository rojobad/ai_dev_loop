"""Structured retry-after resolution for scheduler usage-limit continuation."""

from __future__ import annotations

from collections.abc import Mapping

from ai_dev_loop.scheduler.domain.cursor_contract import (
    DEFAULT_USAGE_LIMIT_RETRY_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    MIN_RETRY_AFTER_SECONDS,
)


def _coerce_retry_after_seconds(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    candidate = value
    if MIN_RETRY_AFTER_SECONDS <= candidate <= MAX_RETRY_AFTER_SECONDS:
        return candidate
    return None


def resolve_usage_limit_retry_seconds(
    structured_errors: tuple[Mapping[str, object], ...] | list[Mapping[str, object]],
) -> int:
    """Accept ``retry_after_seconds`` only from structured Cursor error records.

    Missing, malformed, or out-of-range values use the five-hour fallback.
    """

    for record in structured_errors:
        if not isinstance(record, Mapping):
            continue
        if "retry_after_seconds" not in record:
            continue
        resolved = _coerce_retry_after_seconds(record.get("retry_after_seconds"))
        if resolved is not None:
            return resolved
        continue
    return DEFAULT_USAGE_LIMIT_RETRY_SECONDS
