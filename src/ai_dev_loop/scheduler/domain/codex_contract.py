"""Codex review effect and artifact contracts for Phase 17.5."""

from __future__ import annotations

from ai_dev_loop.iterations import iteration_label

BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND = "codex.bootstrap_review"
RESUME_CODEX_REVIEW_EFFECT_KIND = "codex.resume_review"

BOOTSTRAP_CODEX_REVIEW_EFFECT_ID = "codex-bootstrap-review"
RESUME_CODEX_REVIEW_EFFECT_ID = "codex-resume-review"

CODEX_ATTEMPT_EFFECT_KINDS = frozenset(
    {
        BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        RESUME_CODEX_REVIEW_EFFECT_KIND,
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


REVIEW_RETRY_OPERATIONAL_ENVELOPE = (
    "This is an automated scheduler retry of Codex review. "
    "The prior attempt did not yield a valid structured review result. "
    "Return a fresh schema-valid review of the same staged snapshot only.\n\n"
)
