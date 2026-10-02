"""Fixed Phase 22 Codex workspace routing automatic retry policy constants."""

from __future__ import annotations

FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT = "codex_workspace_routing_timeout"
ROUTING_AUTO_RETRY_DELAY_SECONDS = 300
ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS = 12
ROUTING_AUTO_RETRY_POLICY_VERSION = "phase-22-v1"

ROUTING_FAILURE_POST_PROBE_REASONS = frozenset(
    {
        "response_received",
        "exhausted_window",
        "reached_state_marker",
        "timeout",
        "premature_eof",
        "protocol_error",
        "malformed_response",
        "process_failure",
        "cleanup_failure",
    }
)


def reset_routing_auto_retry_for_new_review_iteration() -> dict[str, object]:
    return {
        "routing_auto_retry_eligible": False,
        "routing_auto_retry_due_at": None,
        "routing_auto_retry_exhausted": False,
        "routing_auto_retry_authorizations_used": 0,
        "routing_auto_retry_policy_version": None,
        "routing_failure_post_probe_status": None,
        "routing_failure_post_probe_reason": None,
    }
