"""Bounded automatic same-run retry for Codex workspace routing discovery timeouts."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.response_schema import events_text_indicates_workspace_routing_timeout
from ai_dev_loop.runners.codex_failure import (
    FAILURE_CODE_CODEX_USAGE_LIMIT,
    is_codex_usage_limit_recovery_eligible,
)
from ai_dev_loop.scheduler.application.attempt_envelope import read_bounded_bytes, sha256_file
from ai_dev_loop.scheduler.domain.codex_contract import MAX_CODEX_EVENTS_ARTIFACT_BYTES
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
    ROUTING_AUTO_RETRY_POLICY_VERSION,
)

_OUTCOME_TRUNCATION_KEYS = (
    "timed_out",
    "stdout_truncated",
    "stderr_truncated",
    "review_output_truncated",
)


def outcome_has_incomplete_codex_transport(outcome: dict[str, object]) -> bool:
    return any(bool(outcome.get(key)) for key in _OUTCOME_TRUNCATION_KEYS)


def annotate_codex_outcome_routing_evidence(
    outcome: dict[str, object],
    events_path: Path,
) -> None:
    """Persist authenticated routing classification on the completion stdout payload."""

    if outcome_has_incomplete_codex_transport(outcome):
        return
    if is_codex_usage_limit_recovery_eligible(outcome):
        return
    if str(outcome.get("failure_code", "")).strip() == FAILURE_CODE_CODEX_USAGE_LIMIT:
        return
    if str(outcome.get("review_result_sha256", "")).strip():
        return
    if not events_path.is_file() or events_path.is_symlink():
        return
    try:
        size = events_path.stat().st_size
    except OSError:
        return
    if size > MAX_CODEX_EVENTS_ARTIFACT_BYTES:
        return
    try:
        text = read_bounded_bytes(events_path, MAX_CODEX_EVENTS_ARTIFACT_BYTES).decode("utf-8")
    except (OSError, UnicodeError):
        return
    if not events_text_indicates_workspace_routing_timeout(text):
        return
    outcome["operational_failure_kind"] = FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
    outcome["codex_events_sha256"] = sha256_file(events_path)


def classify_codex_workspace_routing_timeout_from_outcome(
    run_root: Path,
    outcome: dict[str, object],
) -> bool:
    """Classify routing timeout only from authenticated stdout plus bound events digest."""

    if outcome_has_incomplete_codex_transport(outcome):
        return False
    if is_codex_usage_limit_recovery_eligible(outcome):
        return False
    if str(outcome.get("failure_code", "")).strip() == FAILURE_CODE_CODEX_USAGE_LIMIT:
        return False
    if str(outcome.get("bootstrap_uncertainty_reason", "")).strip():
        return False
    if str(outcome.get("review_block_reason", "")).strip():
        return False
    if str(outcome.get("review_result_sha256", "")).strip():
        return False
    if str(outcome.get("operational_failure_kind", "")).strip() != (
        FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
    ):
        return False
    stored_sha = str(outcome.get("codex_events_sha256", "")).strip()
    if not stored_sha:
        return False
    events_rel = str(outcome.get("events_path", "")).strip()
    if not events_rel:
        return False
    events_path = run_root / events_rel
    if events_path.is_symlink() or not events_path.is_file():
        return False
    try:
        if events_path.stat().st_size > MAX_CODEX_EVENTS_ARTIFACT_BYTES:
            return False
    except OSError:
        return False
    if sha256_file(events_path) != stored_sha:
        return False
    try:
        text = read_bounded_bytes(events_path, MAX_CODEX_EVENTS_ARTIFACT_BYTES).decode("utf-8")
    except (OSError, UnicodeError):
        return False
    return events_text_indicates_workspace_routing_timeout(text)


def routing_auto_retry_remaining_authorizations(authorizations_used: int) -> int:
    return max(0, ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS - authorizations_used)


def routing_auto_retry_fields_for_new_failure(
    *,
    authorizations_used: int,
    failure_kind: str,
    due_at_text: str | None,
) -> dict[str, object]:
    remaining = routing_auto_retry_remaining_authorizations(authorizations_used)
    is_routing = failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
    eligible = is_routing and remaining > 0 and due_at_text is not None
    return {
        "routing_auto_retry_policy_version": (
            ROUTING_AUTO_RETRY_POLICY_VERSION
            if failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
            else None
        ),
        "routing_auto_retry_eligible": eligible,
        "routing_auto_retry_authorizations_used": authorizations_used,
        "routing_auto_retry_due_at": due_at_text if eligible else None,
        "routing_auto_retry_exhausted": (
            failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT and remaining <= 0
        ),
    }
