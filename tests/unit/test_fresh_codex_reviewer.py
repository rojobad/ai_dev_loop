"""Unit tests for fresh Codex reviewer bootstrap identity parsing and state contracts."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.fresh_codex_reviewer import classify_bootstrap_session_id_from_text
from ai_dev_loop.state import CodexState, FreshCodexReviewerBinding

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
OTHER_ID = "019def00-1111-1111-1111-0000000000cc"
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING = "high"


def _thread_started(session_id: str) -> str:
    return json.dumps({"type": "thread.started", "thread_id": session_id})


def _fresh_binding(**updates: object) -> FreshCodexReviewerBinding:
    payload = {
        "review_model": REVIEW_MODEL,
        "review_reasoning_effort": REVIEW_REASONING,
    }
    payload.update(updates)
    return FreshCodexReviewerBinding(**payload)


def _fresh_codex(**updates: object) -> CodexState:
    payload = {
        "command": "codex",
        "session_id": None,
        "review_model": REVIEW_MODEL,
        "review_reasoning_effort": REVIEW_REASONING,
        "review_model_source": "explicit",
        "review_reasoning_source": "explicit",
        "review_skill": "review-staged-cursor-execution",
        "sandbox": "read-only",
        "fresh_reviewer": _fresh_binding(),
    }
    payload.update(updates)
    return CodexState(**payload)


def test_bootstrap_accepts_exactly_one_valid_thread_started() -> None:
    capture = classify_bootstrap_session_id_from_text(_thread_started(BOOTSTRAP_ID) + "\n")
    assert capture.session_id == BOOTSTRAP_ID
    assert capture.uncertainty_reason is None


def test_bootstrap_duplicate_identical_events_are_uncertain() -> None:
    text = "\n".join([_thread_started(BOOTSTRAP_ID), _thread_started(BOOTSTRAP_ID)])
    capture = classify_bootstrap_session_id_from_text(text)
    assert capture.session_id is None
    assert capture.uncertainty_reason == "duplicate_identity_events"


def test_bootstrap_malformed_plus_valid_is_uncertain() -> None:
    text = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "not-a-uuid"}),
            _thread_started(BOOTSTRAP_ID),
        ]
    )
    capture = classify_bootstrap_session_id_from_text(text)
    assert capture.session_id is None
    assert capture.uncertainty_reason == "malformed_identity"


def test_bootstrap_conflicting_valid_events_are_uncertain() -> None:
    text = "\n".join([_thread_started(BOOTSTRAP_ID), _thread_started(OTHER_ID)])
    capture = classify_bootstrap_session_id_from_text(text)
    assert capture.session_id is None
    assert capture.uncertainty_reason == "conflicting_identity"


def test_codex_state_rejects_complete_binding_without_session_id() -> None:
    with pytest.raises(PydanticValidationError, match="requires codex.session_id"):
        _fresh_codex(
            fresh_reviewer=_fresh_binding(
                bootstrap_session_id=BOOTSTRAP_ID,
                bootstrap_events_sha256="a" * 64,
                bootstrap_bound_at="2026-09-07T00:00:00+00:00",
            )
        )


def test_codex_state_rejects_session_id_without_binding_evidence() -> None:
    with pytest.raises(PydanticValidationError, match="must not have codex.session_id"):
        _fresh_codex(session_id=BOOTSTRAP_ID)


def test_codex_state_rejects_uncertainty_with_session_id() -> None:
    with pytest.raises(PydanticValidationError, match="bootstrap uncertainty"):
        _fresh_codex(
            session_id=BOOTSTRAP_ID,
            fresh_reviewer=_fresh_binding(bootstrap_uncertainty_reason="missing_identity"),
        )


def test_codex_state_rejects_uncertainty_with_binding_evidence() -> None:
    with pytest.raises(PydanticValidationError, match="bootstrap uncertainty"):
        _fresh_codex(
            fresh_reviewer=_fresh_binding(
                bootstrap_uncertainty_reason="missing_identity",
                bootstrap_session_id=BOOTSTRAP_ID,
            )
        )


def test_codex_state_accepts_complete_binding_with_matching_session_id() -> None:
    state = _fresh_codex(
        session_id=BOOTSTRAP_ID,
        fresh_reviewer=_fresh_binding(
            bootstrap_session_id=BOOTSTRAP_ID,
            bootstrap_events_sha256="a" * 64,
            bootstrap_bound_at="2026-09-07T00:00:00+00:00",
        ),
    )
    assert state.session_id == BOOTSTRAP_ID
