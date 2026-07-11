"""Resolve effective Codex review runtime from config overrides and session metadata."""

from __future__ import annotations

from dataclasses import dataclass

from ai_dev_loop.integrations.codex.session_runtime import (
    CodexSessionRuntime,
    cross_family_compaction_warning,
)


@dataclass(frozen=True)
class EffectiveReviewRuntime:
    session_model: str
    session_reasoning_effort: str
    review_model: str
    review_reasoning_effort: str
    review_model_source: str
    review_reasoning_source: str
    model_mismatch_warning: str | None
    model_family_warning: str | None
    session_origin: str
    source_event_type: str
    source_timestamp: str | None


def resolve_effective_review_runtime(
    *,
    session: CodexSessionRuntime,
    configured_review_model: str | None,
    configured_review_reasoning_effort: str | None,
) -> EffectiveReviewRuntime:
    """Resolve each field independently: explicit override wins, else session capture."""

    if configured_review_model is not None:
        review_model = configured_review_model
        review_model_source = "explicit"
    else:
        review_model = session.model
        review_model_source = "session"

    if configured_review_reasoning_effort is not None:
        review_reasoning_effort = configured_review_reasoning_effort
        review_reasoning_source = "explicit"
    else:
        review_reasoning_effort = session.reasoning_effort
        review_reasoning_source = "session"

    mismatch_warning: str | None = None
    if (
        configured_review_model is not None
        and configured_review_model.strip() != session.model.strip()
    ):
        mismatch_warning = (
            f"explicit codex.review_model differs from captured session model "
            f"({session.model} -> {configured_review_model})"
        )

    family_warning = cross_family_compaction_warning(session.model, review_model)

    return EffectiveReviewRuntime(
        session_model=session.model,
        session_reasoning_effort=session.reasoning_effort,
        review_model=review_model,
        review_reasoning_effort=review_reasoning_effort,
        review_model_source=review_model_source,
        review_reasoning_source=review_reasoning_source,
        model_mismatch_warning=mismatch_warning,
        model_family_warning=family_warning,
        session_origin=session.origin,
        source_event_type=session.source_event_type,
        source_timestamp=session.source_timestamp,
    )


def is_legacy_phase9_codex_state(
    *,
    review_model: str | None,
    review_reasoning_effort: str | None,
    review_model_source: str | None,
    review_reasoning_source: str | None,
) -> bool:
    """Historical Phase 9 runs omit provenance and may leave both effective fields null."""

    if review_model_source == "legacy_inherit" or review_reasoning_source == "legacy_inherit":
        return True
    return (
        review_model is None
        and review_reasoning_effort is None
        and review_model_source is None
        and review_reasoning_source is None
    )
