"""Cursor workflow effect and artifact contracts for Phase 17.4."""

from __future__ import annotations

from ai_dev_loop.iterations import iteration_label

PREFLIGHT_EFFECT_KIND = "cursor.preflight_and_probes"
CREATE_CHAT_EFFECT_KIND = "cursor.create_chat"
RUN_CURSOR_TURN_EFFECT_KIND = "cursor.run_turn"
INGEST_CURSOR_RESULT_EFFECT_KIND = "cursor.ingest_result"
NORMALIZE_STAGING_EFFECT_KIND = "cursor.normalize_staging"

PREFLIGHT_EFFECT_ID = "cursor-preflight"
CREATE_CHAT_EFFECT_ID = "cursor-create-chat"
RUN_CURSOR_TURN_EFFECT_ID = "cursor-run-turn"
INGEST_CURSOR_RESULT_EFFECT_ID = "cursor-ingest-result"
NORMALIZE_STAGING_EFFECT_ID = "cursor-normalize-staging"

CURSOR_CHAT_ARTIFACT = "cursor/chat.json"

DEFAULT_USAGE_LIMIT_RETRY_SECONDS = 18_000
MIN_RETRY_AFTER_SECONDS = 1
MAX_RETRY_AFTER_SECONDS = 86_400
SCHEDULER_PROBE_TIMEOUT_SECONDS = 30.0

INITIAL_CURSOR_ITERATION = 1


def invocation_evidence_rel(attempt_id: str) -> str:
    return f"attempts/{attempt_id}/invocation-evidence.json"


def cursor_attempt_iteration_dir(iteration_number: int, attempt_id: str) -> str:
    return f"cursor/iterations/{iteration_label(iteration_number)}/{attempt_id}"


def cursor_attempt_events_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/events.jsonl"


def cursor_attempt_stderr_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/stderr.txt"


def cursor_attempt_metadata_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/metadata.json"


def cursor_attempt_final_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/final.txt"


def git_status_before_cursor_rel(iteration_number: int, attempt_id: str) -> str:
    return f"git/status/{iteration_label(iteration_number)}.{attempt_id}-before-cursor.txt"


def git_status_after_cursor_rel(iteration_number: int, attempt_id: str) -> str:
    return f"git/status/{iteration_label(iteration_number)}.{attempt_id}-after-cursor.txt"
