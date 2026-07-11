"""Unit tests for effective Codex review-runtime resolution."""

from __future__ import annotations

import pytest

from ai_dev_loop.integrations.codex.session_runtime import (
    CROSS_FAMILY_COMPACTION_WARNING,
    CodexSessionRuntime,
)
from ai_dev_loop.review_runtime import (
    is_legacy_phase9_codex_state,
    resolve_effective_review_runtime,
)


@pytest.fixture
def session_runtime() -> CodexSessionRuntime:
    return CodexSessionRuntime(
        session_id="019abc00-0000-0000-0000-000000000000",
        model="gpt-5.6-sol",
        reasoning_effort="high",
        origin="desktop_bridge",
        source_event_type="turn_context",
        source_timestamp="2026-07-10T12:00:02.000Z",
    )


@pytest.mark.parametrize(
    (
        "configured_model",
        "configured_reasoning",
        "expected_model",
        "expected_reasoning",
        "model_source",
        "reasoning_source",
    ),
    [
        (None, None, "gpt-5.6-sol", "high", "session", "session"),
        ("gpt-5.5", None, "gpt-5.5", "high", "explicit", "session"),
        (None, "low", "gpt-5.6-sol", "low", "session", "explicit"),
        ("o4-mini", "medium", "o4-mini", "medium", "explicit", "explicit"),
    ],
)
def test_resolves_model_and_reasoning_precedence_independently(
    session_runtime: CodexSessionRuntime,
    configured_model: str | None,
    configured_reasoning: str | None,
    expected_model: str,
    expected_reasoning: str,
    model_source: str,
    reasoning_source: str,
) -> None:
    resolved = resolve_effective_review_runtime(
        session=session_runtime,
        configured_review_model=configured_model,
        configured_review_reasoning_effort=configured_reasoning,
    )

    assert resolved.review_model == expected_model
    assert resolved.review_reasoning_effort == expected_reasoning
    assert resolved.review_model_source == model_source
    assert resolved.review_reasoning_source == reasoning_source
    assert resolved.session_model == session_runtime.model
    assert resolved.session_reasoning_effort == session_runtime.reasoning_effort
    assert resolved.session_origin == "desktop_bridge"
    assert resolved.source_event_type == "turn_context"
    assert resolved.source_timestamp == "2026-07-10T12:00:02.000Z"


def test_explicit_different_model_reports_mismatch_and_cross_family_warning(
    session_runtime: CodexSessionRuntime,
) -> None:
    resolved = resolve_effective_review_runtime(
        session=session_runtime,
        configured_review_model="gpt-5.5",
        configured_review_reasoning_effort=None,
    )

    assert resolved.model_mismatch_warning is not None
    assert "gpt-5.6-sol -> gpt-5.5" in resolved.model_mismatch_warning
    assert resolved.model_family_warning == CROSS_FAMILY_COMPACTION_WARNING


def test_same_model_does_not_report_mismatch(
    session_runtime: CodexSessionRuntime,
) -> None:
    resolved = resolve_effective_review_runtime(
        session=session_runtime,
        configured_review_model="gpt-5.6-sol",
        configured_review_reasoning_effort="xhigh",
    )

    assert resolved.model_mismatch_warning is None
    assert resolved.model_family_warning is None


@pytest.mark.parametrize(
    (
        "review_model",
        "review_reasoning",
        "model_source",
        "reasoning_source",
        "expected",
    ),
    [
        (None, None, None, None, True),
        (None, None, "legacy_inherit", None, True),
        (None, None, None, "legacy_inherit", True),
        ("gpt-5.5", "high", "explicit", "explicit", False),
        ("gpt-5.6-sol", "high", "session", "session", False),
        (None, "high", None, "explicit", False),
    ],
)
def test_legacy_phase9_detection_is_explicit(
    review_model: str | None,
    review_reasoning: str | None,
    model_source: str | None,
    reasoning_source: str | None,
    expected: bool,
) -> None:
    assert (
        is_legacy_phase9_codex_state(
            review_model=review_model,
            review_reasoning_effort=review_reasoning,
            review_model_source=model_source,
            review_reasoning_source=reasoning_source,
        )
        is expected
    )
