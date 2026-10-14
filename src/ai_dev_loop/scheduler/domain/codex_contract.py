"""Codex review effect and artifact contracts for Phase 17.5."""

from __future__ import annotations

from ai_dev_loop.iterations import iteration_label

BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND = "codex.bootstrap_review"
RESUME_CODEX_REVIEW_EFFECT_KIND = "codex.resume_review"
BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND = "codex.implementer_bootstrap"
RESUME_CODEX_IMPLEMENTER_EFFECT_KIND = "codex.implementer_resume"

BOOTSTRAP_CODEX_REVIEW_EFFECT_ID = "codex-bootstrap-review"
RESUME_CODEX_REVIEW_EFFECT_ID = "codex-resume-review"
BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_ID = "codex-implementer-bootstrap"
RESUME_CODEX_IMPLEMENTER_EFFECT_ID = "codex-implementer-resume"

CODEX_ATTEMPT_EFFECT_KINDS = frozenset(
    {
        BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        RESUME_CODEX_REVIEW_EFFECT_KIND,
    }
)

IMPLEMENTER_ATTEMPT_EFFECT_KINDS = frozenset(
    {
        BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
        RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    }
)

# These failures cannot establish safe implementer identity/evidence. Keep the
# repository reserved until the operator explicitly aborts the frozen run.
CODEX_IMPLEMENTER_RESERVATION_HOLD_BLOCK_KINDS = frozenset(
    {
        "codex_implementer_outcome_evidence_invalid",
        "codex_implementer_binding_invalid",
        "codex_implementer_turn_mismatch",
        "codex_implementer_output_incomplete",
        "codex_implementer_identity_uncertain",
        "codex_implementer_identity_conflict",
        "codex_implementer_effect_invalid",
        "codex_implementer_binding_conflict",
        "codex_implementer_capacity_evidence_invalid",
        "codex_implementer_launch_guard_failed",
        "codex_implementer_timeout_evidence_invalid",
    }
)

SCHEDULER_CODEX_BINDING_ARTIFACT = "codex/fresh-reviewer-binding.json"
SCHEDULER_CODEX_UNCERTAINTY_ARTIFACT = "codex/fresh-reviewer-bootstrap-uncertainty.json"

# Scheduler Codex reviews always receive workspace write access regardless of
# legacy YAML sandbox values. This lets reviewer B make requested corrections
# inside the target repository while the scheduler keeps the Git decision gates.
SCHEDULER_CODEX_REVIEW_SANDBOX = "workspace-write"
LEGACY_SCHEDULER_CODEX_REVIEW_SANDBOX = "read-only"

MAX_CODEX_EVENTS_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_CODEX_CAPTURE_STDOUT_BYTES = MAX_CODEX_EVENTS_ARTIFACT_BYTES
MAX_CODEX_CAPTURE_STDERR_BYTES = 256 * 1024
MAX_CODEX_REVIEW_RESULT_BYTES = 1 * 1024 * 1024

CODEX_CAPACITY_PROBE_TIMEOUT_SECONDS = 15.0
CODEX_CAPACITY_PROBE_STDOUT_MAX_BYTES = 256 * 1024
CODEX_CAPACITY_PROBE_STDERR_MAX_BYTES = 64 * 1024
CODEX_CAPACITY_PROBE_JSON_LINE_MAX_BYTES = 256 * 1024
CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH = 32

CODEX_IMPLEMENTER_TIMEOUT_RETRY_POLICY_VERSION = "codex-implementer-timeout-v1"
CODEX_IMPLEMENTER_TIMEOUT_RETRY_DELAY_SECONDS = 30 * 60
CODEX_IMPLEMENTER_TIMEOUT_MAX_AUTOMATIC_RETRIES = 3
MAX_CODEX_IMPLEMENTER_TIMEOUT_INTENT_BYTES = 64 * 1024


def codex_attempt_events_rel(review_iteration: int, attempt_id: str) -> str:
    return f"codex/events/{iteration_label(review_iteration)}.{attempt_id}.jsonl"


def codex_attempt_stderr_rel(review_iteration: int, attempt_id: str) -> str:
    return f"codex/events/{iteration_label(review_iteration)}.{attempt_id}.stderr.txt"


def codex_review_result_rel(review_iteration: int, attempt_id: str | None = None) -> str:
    label = iteration_label(review_iteration)
    if attempt_id:
        return f"codex/reviews/{label}.{attempt_id}.json"
    return f"codex/reviews/{label}.json"


def codex_review_metadata_rel(review_iteration: int, attempt_id: str | None = None) -> str:
    label = iteration_label(review_iteration)
    if attempt_id:
        return f"codex/reviews/{label}.{attempt_id}.metadata.json"
    return f"codex/reviews/{label}.metadata.json"


def codex_review_report_rel(review_iteration: int, attempt_id: str | None = None) -> str:
    label = iteration_label(review_iteration)
    if attempt_id:
        return f"codex/reviews/{label}.{attempt_id}.md"
    return f"codex/reviews/{label}.md"


def codex_review_prompt_rel(review_iteration: int, attempt_id: str) -> str:
    label = iteration_label(review_iteration)
    return f"codex/reviews/{label}.{attempt_id}.prompt.txt"


def codex_implementer_events_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.jsonl"


def codex_implementer_stderr_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.stderr.txt"


def codex_implementer_final_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.final.txt"


def codex_implementer_metadata_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.metadata.json"


def codex_implementer_status_before_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.status-before.txt"


def codex_implementer_status_after_rel(iteration: int, attempt_id: str) -> str:
    return f"implementer/turns/{iteration_label(iteration)}.{attempt_id}.status-after.txt"


def codex_implementer_latest_final_rel(iteration: int) -> str:
    return f"implementer/iterations/{iteration_label(iteration)}/final.txt"


def codex_implementer_session_rel() -> str:
    return "implementer/session.json"


def codex_implementer_session_capture_rel(attempt_id: str) -> str:
    return f"implementer/turns/{attempt_id}.session-capture.json"


def codex_implementer_wait_intent_rel(attempt_id: str) -> str:
    return f"implementer/waits/{attempt_id}.intent.json"


def codex_implementer_timeout_intent_rel(attempt_id: str) -> str:
    return f"implementer/timeouts/{attempt_id}.intent.json"


def codex_review_prompt_evidence_rel(review_iteration: int, attempt_id: str) -> str:
    label = iteration_label(review_iteration)
    return f"codex/reviews/{label}.{attempt_id}.prompt-evidence.json"


REVIEWER_SESSION_REF_PREFIX = "ai_dev_loop:reviewer-session:v1:"


REVIEW_RETRY_OPERATIONAL_ENVELOPE = (
    "This is an automated scheduler retry of Codex review. "
    "The prior attempt did not yield a valid structured review result. "
    "Return a fresh schema-valid review of the same staged snapshot only.\n\n"
)
