"""Public Codex capacity observation projection."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_dev_loop.integration_api.models import (
    IntegrationCodexCapacityData,
    IntegrationCodexCapacityLimit,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityStatus,
)


def _format_resets_at_utc(unix_seconds: int | None) -> str | None:
    if unix_seconds is None:
        return None
    try:
        instant = datetime.fromtimestamp(unix_seconds, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None
    text = instant.strftime("%Y-%m-%dT%H:%M:%S")
    if instant.microsecond:
        text += f".{instant.microsecond:06d}".rstrip("0").rstrip(".")
    return f"{text}Z"


def map_capacity_observation_to_data(
    observation: CodexCapacityObservation,
) -> IntegrationCodexCapacityData:
    reason: str | None = None
    if observation.status == CodexCapacityStatus.UNAVAILABLE and observation.reason is not None:
        reason = observation.reason.value

    limits = tuple(
        IntegrationCodexCapacityLimit.model_validate(
            {
                "id": entry.limit_id,
                "window": entry.window,
                "usedPercent": entry.used_percent,
                "remainingPercent": entry.remaining_percent,
                "windowDurationMinutes": entry.window_duration_minutes,
                "resetsAt": _format_resets_at_utc(entry.resets_at_unix),
            }
        )
        for entry in observation.limits
    )
    return IntegrationCodexCapacityData(
        status=observation.status.value,
        reason=reason,
        limits=limits,
    )
