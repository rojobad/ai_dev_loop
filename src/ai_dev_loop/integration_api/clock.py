"""Injectable clock for integration API timestamps."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

_observed_at_provider: Callable[[], datetime] | None = None


def set_observed_at_provider(provider: Callable[[], datetime] | None) -> None:
    global _observed_at_provider
    _observed_at_provider = provider


def observed_at_now() -> datetime:
    if _observed_at_provider is not None:
        return _observed_at_provider()
    return datetime.now(tz=UTC)
