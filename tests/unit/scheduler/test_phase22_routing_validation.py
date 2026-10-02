"""Negative persistence validation for Phase 22 routing retry metadata."""

from __future__ import annotations

import pytest

from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
    ROUTING_AUTO_RETRY_POLICY_VERSION,
)
from ai_dev_loop.scheduler.domain.events import (
    CodexReviewRetryableFailureEvent,
    CodexUsageCapacityDetectedEvent,
)

_VALID_DUE = "2026-10-02T12:05:00.000000Z"


def _valid_routing_retryable_event(**overrides: object) -> CodexReviewRetryableFailureEvent:
    base = {
        "run_id": "run-1",
        "review_iteration": 1,
        "failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
        "attempt_id": "att-1",
        "retry_generation": 1,
        "routing_auto_retry_policy_version": ROUTING_AUTO_RETRY_POLICY_VERSION,
        "routing_auto_retry_eligible": True,
        "routing_auto_retry_authorizations_used": 0,
        "routing_auto_retry_due_at": _VALID_DUE,
        "routing_auto_retry_exhausted": False,
    }
    base.update(overrides)
    return CodexReviewRetryableFailureEvent(**base)  # type: ignore[arg-type]


def _valid_routing_capacity_event(**overrides: object) -> CodexUsageCapacityDetectedEvent:
    base = {
        "run_id": "run-1",
        "review_iteration": 1,
        "evidence_source": "post_failure_capacity_probe",
        "operational_failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
        "routing_auto_retry_policy_version": ROUTING_AUTO_RETRY_POLICY_VERSION,
        "routing_auto_retry_eligible": True,
        "routing_auto_retry_authorizations_used": 0,
        "routing_auto_retry_due_at": _VALID_DUE,
        "routing_auto_retry_exhausted": False,
        "routing_failure_post_probe_status": "unavailable",
        "routing_failure_post_probe_reason": "timeout",
    }
    base.update(overrides)
    return CodexUsageCapacityDetectedEvent(**base)  # type: ignore[arg-type]


def test_retryable_event_accepts_valid_routing_schedule() -> None:
    event = _valid_routing_retryable_event()
    assert event.routing_auto_retry_eligible is True
    assert event.routing_auto_retry_due_at == _VALID_DUE


def test_capacity_event_accepts_valid_routing_schedule() -> None:
    event = _valid_routing_capacity_event()
    assert event.routing_auto_retry_eligible is True


def test_historical_retryable_event_defaults_are_inactive() -> None:
    event = CodexReviewRetryableFailureEvent(
        run_id="run-1",
        review_iteration=1,
        failure_kind="codex_review_timeout",
        attempt_id="att-1",
        retry_generation=1,
    )
    assert event.routing_auto_retry_eligible is False
    assert event.routing_auto_retry_authorizations_used == 0
    assert event.routing_auto_retry_due_at is None
    assert event.routing_auto_retry_exhausted is False


def test_historical_capacity_event_defaults_are_inactive() -> None:
    event = CodexUsageCapacityDetectedEvent(run_id="run-1", review_iteration=1)
    assert event.routing_auto_retry_eligible is False
    assert event.routing_auto_retry_authorizations_used == 0
    assert event.routing_auto_retry_due_at is None


def test_retryable_event_rejects_string_authorization_counter() -> None:
    with pytest.raises(ValueError, match="integer"):
        CodexReviewRetryableFailureEvent(
            run_id="run-1",
            review_iteration=1,
            failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            attempt_id="att-1",
            retry_generation=1,
            routing_auto_retry_authorizations_used="3",  # type: ignore[arg-type]
        )


def test_retryable_event_rejects_eligible_non_routing_failure() -> None:
    with pytest.raises(ValueError, match="routing failure kind"):
        _valid_routing_retryable_event(failure_kind="codex_review_timeout")


def test_retryable_event_rejects_eligible_without_policy_version() -> None:
    with pytest.raises(ValueError, match="policy version"):
        _valid_routing_retryable_event(routing_auto_retry_policy_version=None)


def test_retryable_event_rejects_coerced_eligible_boolean() -> None:
    with pytest.raises(ValueError, match="boolean"):
        CodexReviewRetryableFailureEvent(
            run_id="run-1",
            review_iteration=1,
            failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            attempt_id="att-1",
            retry_generation=1,
            routing_auto_retry_eligible="true",  # type: ignore[arg-type]
        )


def test_retryable_event_rejects_out_of_policy_authorization_count() -> None:
    with pytest.raises(ValueError, match="exceeds policy limit"):
        CodexReviewRetryableFailureEvent(
            run_id="run-1",
            review_iteration=1,
            failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            attempt_id="att-1",
            retry_generation=1,
            routing_auto_retry_authorizations_used=ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS + 1,
        )


def test_retryable_event_rejects_eligible_at_exhausted_allowance() -> None:
    with pytest.raises(ValueError, match="remaining allowance"):
        _valid_routing_retryable_event(
            routing_auto_retry_authorizations_used=ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
        )


def test_retryable_event_rejects_due_at_without_eligibility() -> None:
    with pytest.raises(ValueError, match="requires routing_auto_retry_eligible"):
        CodexReviewRetryableFailureEvent(
            run_id="run-1",
            review_iteration=1,
            failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            attempt_id="att-1",
            retry_generation=1,
            routing_auto_retry_eligible=False,
            routing_auto_retry_due_at=_VALID_DUE,
        )


def test_retryable_event_rejects_exhausted_and_eligible() -> None:
    with pytest.raises(ValueError, match="cannot be eligible"):
        _valid_routing_retryable_event(routing_auto_retry_exhausted=True)


def test_capacity_event_rejects_negative_routing_authorization_count() -> None:
    with pytest.raises(ValueError):
        CodexUsageCapacityDetectedEvent(
            run_id="run-1",
            review_iteration=1,
            routing_auto_retry_authorizations_used=-1,
        )


def test_capacity_event_rejects_eligible_at_exhausted_allowance() -> None:
    with pytest.raises(ValueError, match="remaining allowance"):
        _valid_routing_capacity_event(
            routing_auto_retry_authorizations_used=ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
        )


def test_capacity_event_rejects_due_at_without_eligibility() -> None:
    with pytest.raises(ValueError, match="requires routing_auto_retry_eligible"):
        CodexUsageCapacityDetectedEvent(
            run_id="run-1",
            review_iteration=1,
            routing_auto_retry_eligible=False,
            routing_auto_retry_due_at=_VALID_DUE,
        )


def test_capacity_event_rejects_exhausted_and_eligible() -> None:
    with pytest.raises(ValueError, match="cannot be eligible"):
        _valid_routing_capacity_event(routing_auto_retry_exhausted=True)
