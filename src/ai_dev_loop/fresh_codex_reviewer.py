"""Fresh Codex reviewer bootstrap binding, validation, and identity capture."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ai_dev_loop.config import (
    normalize_optional_review_model,
    normalize_optional_review_reasoning_effort,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id

if TYPE_CHECKING:
    from ai_dev_loop.state import CodexState, FreshCodexReviewerBinding, RunState

CODEX_BOOTSTRAP_SESSION_EVENT_TYPE = "thread.started"
CODEX_REVIEWER_SANDBOX = "read-only"
FRESH_REVIEWER_INPUT_ARTIFACT = "codex/fresh-reviewer-input.json"
FRESH_REVIEWER_BINDING_ARTIFACT = "codex/fresh-reviewer-binding.json"
FRESH_REVIEWER_BOOTSTRAP_UNCERTAINTY_ARTIFACT = "codex/fresh-reviewer-bootstrap-uncertainty.json"

_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

BOOTSTRAP_UNCERTAINTY_REASONS = frozenset(
    {
        "missing_identity",
        "malformed_identity",
        "conflicting_identity",
        "duplicate_identity_events",
        "timeout_without_identity",
    }
)

LEGACY_AB_SESSION_BOUND_MESSAGE = (
    "run uses the retired session-bound A/B reviewer contract; inspect status only "
    "and prepare or scheduler submit a fresh run with explicit "
    "--codex-review-model and --codex-review-reasoning-effort (no reviewer session id)"
)

FRESH_BOOTSTRAP_UNCERTAIN_MESSAGE = (
    "fresh Codex reviewer bootstrap is uncertain; inspect preserved bootstrap evidence "
    "and prepare a new run instead of retrying bootstrap"
)


@dataclass(frozen=True)
class BootstrapIdentityCapture:
    """Result of parsing bootstrap JSONL identity events."""

    session_id: str | None
    uncertainty_reason: str | None


def require_frozen_review_model(value: str | None) -> str:
    if value is None:
        raise ValidationError(
            "--codex-review-model is required for controller prepare/submit "
            "(no YAML, session, or runtime fallback)"
        )
    try:
        normalized = normalize_optional_review_model(value)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if normalized is None:
        raise ValidationError("--codex-review-model must not be empty")
    return normalized


def require_frozen_review_reasoning_effort(value: str | None) -> str:
    if value is None:
        raise ValidationError(
            "--codex-review-reasoning-effort is required for controller prepare/submit "
            "(no YAML, session, or runtime fallback)"
        )
    try:
        normalized = normalize_optional_review_reasoning_effort(value)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if normalized is None:
        raise ValidationError("--codex-review-reasoning-effort must not be empty")
    return normalized


def is_fresh_codex_reviewer_run(codex: CodexState) -> bool:
    return codex.fresh_reviewer is not None


def is_legacy_ab_session_bound_run(state: RunState) -> bool:
    """Historical A/B runs that bound reviewer B at prepare time."""

    return (
        state.controller is not None
        and state.codex.fresh_reviewer is None
        and bool((state.codex.session_id or "").strip())
    )


def _binding_bootstrap_fields(
    binding: FreshCodexReviewerBinding,
) -> tuple[str | None, str | None, str | None]:
    return (
        (binding.bootstrap_session_id or "").strip() or None,
        (binding.bootstrap_events_sha256 or "").strip() or None,
        (binding.bootstrap_bound_at or "").strip() or None,
    )


def is_fresh_reviewer_binding_complete(binding: FreshCodexReviewerBinding) -> bool:
    session_id, events_sha256, bound_at = _binding_bootstrap_fields(binding)
    return bool(session_id and events_sha256 and bound_at)


def is_fresh_reviewer_bootstrap_uncertain(codex: CodexState) -> bool:
    binding = codex.fresh_reviewer
    if binding is None:
        return False
    reason = (binding.bootstrap_uncertainty_reason or "").strip()
    if reason:
        return True
    session_id, events_sha256, bound_at = _binding_bootstrap_fields(binding)
    present = [value for value in (session_id, events_sha256, bound_at) if value]
    return bool(present) and len(present) != 3


def codex_reviewer_is_bound(codex: CodexState) -> bool:
    if is_fresh_codex_reviewer_run(codex):
        binding = codex.fresh_reviewer
        if binding is None or is_fresh_reviewer_bootstrap_uncertain(codex):
            return False
        if not is_fresh_reviewer_binding_complete(binding):
            return False
        session_id = (codex.session_id or "").strip()
        bootstrap_session_id = (binding.bootstrap_session_id or "").strip()
        return bool(session_id and bootstrap_session_id and session_id == bootstrap_session_id)
    return bool((codex.session_id or "").strip())


def can_attempt_fresh_bootstrap(codex: CodexState) -> bool:
    return (
        is_fresh_codex_reviewer_run(codex)
        and not codex_reviewer_is_bound(codex)
        and not is_fresh_reviewer_bootstrap_uncertain(codex)
    )


def effective_codex_session_id(codex: CodexState) -> str | None:
    session_id = (codex.session_id or "").strip()
    if session_id:
        return session_id
    return None


def require_bound_codex_session_id(codex: CodexState) -> str:
    session_id = effective_codex_session_id(codex)
    if session_id is None:
        if is_fresh_reviewer_bootstrap_uncertain(codex):
            raise ValidationError(FRESH_BOOTSTRAP_UNCERTAIN_MESSAGE)
        if is_fresh_codex_reviewer_run(codex):
            raise ValidationError(
                "Codex reviewer identity is not bound yet; first review bootstrap is required"
            )
        raise ValidationError("codex session id is missing from prepared state")
    return session_id


def reject_legacy_ab_session_bound_execution(state: RunState) -> None:
    if is_legacy_ab_session_bound_run(state):
        raise ValidationError(LEGACY_AB_SESSION_BOUND_MESSAGE)


def reject_fresh_bootstrap_uncertain_execution(state: RunState) -> None:
    if is_fresh_reviewer_bootstrap_uncertain(state.codex):
        raise ValidationError(FRESH_BOOTSTRAP_UNCERTAIN_MESSAGE)


def _is_uuid_session_id(value: object) -> bool:
    return isinstance(value, str) and bool(_UUID_PATTERN.match(value.strip()))


def classify_bootstrap_session_id_from_events(events_path: Path) -> BootstrapIdentityCapture:
    """Classify Codex bootstrap identity from JSONL audit output."""

    if not events_path.is_file():
        return BootstrapIdentityCapture(session_id=None, uncertainty_reason="missing_identity")
    try:
        text = events_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return BootstrapIdentityCapture(session_id=None, uncertainty_reason="missing_identity")
    return classify_bootstrap_session_id_from_text(text)


def classify_bootstrap_session_id_from_text(text: str) -> BootstrapIdentityCapture:
    """Classify Codex bootstrap identity from JSONL audit text."""

    found: list[str] = []
    thread_started_count = 0
    malformed_thread_started_count = 0
    for _line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != CODEX_BOOTSTRAP_SESSION_EVENT_TYPE:
            continue
        thread_started_count += 1
        thread_id = payload.get("thread_id")
        if not _is_uuid_session_id(thread_id):
            malformed_thread_started_count += 1
            continue
        found.append(require_codex_session_id(thread_id))

    if thread_started_count == 0:
        return BootstrapIdentityCapture(session_id=None, uncertainty_reason="missing_identity")
    if malformed_thread_started_count > 0:
        return BootstrapIdentityCapture(session_id=None, uncertainty_reason="malformed_identity")
    if len(found) != 1:
        if len(found) > 1 and len(set(found)) == 1:
            return BootstrapIdentityCapture(
                session_id=None,
                uncertainty_reason="duplicate_identity_events",
            )
        return BootstrapIdentityCapture(session_id=None, uncertainty_reason="conflicting_identity")
    return BootstrapIdentityCapture(session_id=found[0], uncertainty_reason=None)


def parse_bootstrap_session_id_from_events(events_path: Path) -> str:
    """Extract exactly one Codex ``thread.started`` identity from JSONL audit output."""

    capture = classify_bootstrap_session_id_from_events(events_path)
    if capture.session_id is None:
        reason = capture.uncertainty_reason or "missing_identity"
        raise ValidationError(f"Codex bootstrap identity capture failed: {reason}")
    return capture.session_id


def build_fresh_reviewer_input_artifact(
    *,
    review_model: str,
    review_reasoning_effort: str,
) -> dict[str, str]:
    return {
        "review_model": review_model,
        "review_reasoning_effort": review_reasoning_effort,
    }


def validate_fresh_reviewer_input_artifact(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise ValidationError("fresh reviewer input artifact must be a JSON object")
    review_model = payload.get("review_model")
    review_reasoning = payload.get("review_reasoning_effort")
    if not isinstance(review_model, str) or not review_model.strip():
        raise ValidationError("fresh reviewer input artifact review_model is missing")
    if not isinstance(review_reasoning, str) or not review_reasoning.strip():
        raise ValidationError("fresh reviewer input artifact review_reasoning_effort is missing")
    return {
        "review_model": require_frozen_review_model(review_model),
        "review_reasoning_effort": require_frozen_review_reasoning_effort(review_reasoning),
    }


def validate_fresh_reviewer_binding_artifact(
    payload: object,
    *,
    binding: FreshCodexReviewerBinding,
    session_id: str | None,
) -> None:
    if not isinstance(payload, dict):
        raise ValidationError("fresh reviewer binding artifact must be a JSON object")
    review_model = payload.get("review_model")
    review_reasoning = payload.get("review_reasoning_effort")
    if review_model != binding.review_model:
        raise ValidationError("fresh reviewer binding artifact review_model mismatch")
    if review_reasoning != binding.review_reasoning_effort:
        raise ValidationError("fresh reviewer binding artifact review_reasoning_effort mismatch")
    artifact_session_prefix = payload.get("bootstrap_session_id_prefix")
    bound_session_id = (binding.bootstrap_session_id or "").strip()
    if bound_session_id:
        if not isinstance(artifact_session_prefix, str) or not artifact_session_prefix:
            raise ValidationError("fresh reviewer binding artifact missing session prefix")
        if bound_session_id[:8] != artifact_session_prefix:
            raise ValidationError("fresh reviewer binding artifact session prefix mismatch")
        if session_id and session_id != bound_session_id:
            raise ValidationError("fresh reviewer binding artifact session mismatch")
    events_sha256 = payload.get("bootstrap_events_sha256")
    bound_at = payload.get("bootstrap_bound_at")
    if bound_session_id:
        if events_sha256 != binding.bootstrap_events_sha256:
            raise ValidationError("fresh reviewer binding artifact events hash mismatch")
        if bound_at != binding.bootstrap_bound_at:
            raise ValidationError("fresh reviewer binding artifact bound_at mismatch")
