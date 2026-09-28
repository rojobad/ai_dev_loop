"""Minimal consumer compatibility helpers for Bridge-style clients."""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from ai_dev_loop.integration_api.version import API_MAJOR

UPDATE_REQUIRED = "UPDATE_REQUIRED"


class ConsumerGate(StrEnum):
    ACCEPT = "accept"
    UPDATE_REQUIRED = UPDATE_REQUIRED


def evaluate_envelope_major(
    payload: dict[str, Any], *, expected_major: int = API_MAJOR
) -> ConsumerGate:
    api_version = payload.get("apiVersion")
    if not isinstance(api_version, dict):
        return ConsumerGate.UPDATE_REQUIRED
    major = api_version.get("major")
    if not isinstance(major, int) or isinstance(major, bool):
        return ConsumerGate.UPDATE_REQUIRED
    if major != expected_major:
        return ConsumerGate.UPDATE_REQUIRED
    return ConsumerGate.ACCEPT


def run_gated_resource_request(
    payload: dict[str, Any],
    *,
    resource_request: Callable[[], None],
    expected_major: int = API_MAJOR,
) -> ConsumerGate:
    gate = evaluate_envelope_major(payload, expected_major=expected_major)
    if gate is ConsumerGate.UPDATE_REQUIRED:
        return gate
    resource_request()
    return gate
