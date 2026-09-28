"""Unit tests for Codex capacity public projection (Phase 21.6)."""

from __future__ import annotations

import json
import math

import pytest

from ai_dev_loop.integration_api.capacity_projection import map_capacity_observation_to_data
from ai_dev_loop.integration_api.schemas import (
    CODEX_CAPACITY_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityReason,
    CodexCapacityStatus,
    capacity_from_rate_limits_payload,
    extract_capacity_limit_diagnostics,
)


def _observation_from_payload(payload: dict[str, object]) -> CodexCapacityObservation:
    status = capacity_from_rate_limits_payload(payload)
    if status is None:
        return CodexCapacityObservation(
            status=CodexCapacityStatus.UNAVAILABLE,
            reason=CodexCapacityReason.MALFORMED_RESPONSE,
        )
    reason = CodexCapacityReason.RESPONSE_RECEIVED
    if status == CodexCapacityStatus.EXHAUSTED:
        reason = CodexCapacityReason.EXHAUSTED_WINDOW
    limits = extract_capacity_limit_diagnostics(payload)
    return CodexCapacityObservation(status=status, reason=reason, limits=limits)


def test_c03_map_and_singleton_default_id() -> None:
    payload = {"rateLimits": {"primary": {"usedPercent": 12.5}}}
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.status == "available"
    assert data.reason is None
    assert len(data.limits) == 1
    assert data.limits[0].id == "default"
    assert data.limits[0].window == "primary"
    assert data.limits[0].used_percent == 12.5
    assert data.limits[0].remaining_percent == 87.5


def test_c03_map_precedence_over_legacy_when_present() -> None:
    obs = CodexCapacityObservation(
        status=CodexCapacityStatus.UNAVAILABLE,
        reason=CodexCapacityReason.MALFORMED_RESPONSE,
    )
    data = map_capacity_observation_to_data(obs)
    assert data.status == "unavailable"
    assert data.reason == "malformed_response"
    assert data.limits == ()


def test_c03_primary_secondary_sort_and_fractional_percent() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "beta": {"secondary": {"usedPercent": 33.3}},
            "alpha": {"primary": {"usedPercent": 0.5}},
        }
    }
    limits = extract_capacity_limit_diagnostics(payload)
    assert [item.limit_id for item in limits] == ["alpha", "beta"]
    assert limits[0].window == "primary"
    assert limits[1].window == "secondary"
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.limits[0].remaining_percent == pytest.approx(99.5)


def test_c03_above_one_hundred_percent_preserved() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {"primary": {"usedPercent": 150}},
        }
    }
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.status == "exhausted"
    assert data.limits[0].used_percent == 150
    assert data.limits[0].remaining_percent == 0


def test_c03_optional_duration_and_reset_normalized() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {
                "primary": {
                    "usedPercent": 10,
                    "windowDurationMins": 60,
                    "resetsAt": 1_700_000_000,
                }
            }
        }
    }
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.limits[0].window_duration_minutes == 60
    assert data.limits[0].resets_at == "2023-11-14T22:13:20Z"


def test_c03_invalid_optional_fields_become_null() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {
                "primary": {
                    "usedPercent": 5,
                    "windowDurationMins": "sixty",
                    "resetsAt": True,
                }
            }
        }
    }
    limits = extract_capacity_limit_diagnostics(payload)
    assert limits[0].window_duration_minutes is None
    assert limits[0].resets_at_unix is None
    assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE


def test_c03_bool_and_string_percent_rejected_from_limits() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {"primary": {"usedPercent": True}},
        }
    }
    assert extract_capacity_limit_diagnostics(payload) == ()
    assert capacity_from_rate_limits_payload(payload) is None


def test_c03_nan_percent_rejected() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {"primary": {"usedPercent": math.nan}},
        }
    }
    assert extract_capacity_limit_diagnostics(payload) == ()


def test_c03_exhausted_marker_without_windows_empty_limits() -> None:
    payload = {"rateLimitsByLimitId": {"default": {"rateLimitReachedType": "weekly"}}}
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.status == "exhausted"
    assert data.limits == ()


def test_c03_unavailable_observation_maps_safe_reason() -> None:
    obs = CodexCapacityObservation(
        status=CodexCapacityStatus.UNAVAILABLE,
        reason=CodexCapacityReason.TIMEOUT,
    )
    data = map_capacity_observation_to_data(obs)
    assert data.status == "unavailable"
    assert data.reason == "timeout"
    assert data.limits == ()


def test_c02_status_matches_classifier_on_golden_payloads() -> None:
    fixtures = [
        {"rateLimitsByLimitId": {"default": {"primary": {"usedPercent": 0}}}},
        {"rateLimitsByLimitId": {"default": {"primary": {"usedPercent": 100}}}},
        {"rateLimitsByLimitId": {"default": {"rateLimitReachedType": "weekly"}}},
        {"rateLimitsByLimitId": {"default": {"primary": {"usedPercent": "full"}}}},
    ]
    expected = ["available", "exhausted", "exhausted", "unavailable"]
    for payload, status in zip(fixtures, expected, strict=True):
        data = map_capacity_observation_to_data(_observation_from_payload(payload))
        assert data.status == status


def test_c03_unrepresentable_resets_at_normalizes_to_null() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {
                "primary": {"usedPercent": 5, "resetsAt": 253402300800},
                "secondary": {"usedPercent": 10, "resetsAt": 2**62},
            }
        }
    }
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    assert data.status == "available"
    assert data.limits[0].resets_at is None
    assert data.limits[1].resets_at is None


def test_c03_wire_schema_validates_projection() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "default": {
                "primary": {"usedPercent": 1},
                "secondary": {"usedPercent": 2},
            }
        }
    }
    data = map_capacity_observation_to_data(_observation_from_payload(payload))
    wire = data.model_dump(by_alias=True, mode="json")
    validate_integration_instance(wire, CODEX_CAPACITY_DATA_SCHEMA)
    assert json.dumps(wire)
