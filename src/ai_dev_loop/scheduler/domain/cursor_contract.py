"""Cursor workflow effect and artifact contracts for Phase 17.4."""

from __future__ import annotations

from ai_dev_loop.iterations import iteration_label

PREFLIGHT_EFFECT_KIND = "cursor.preflight_and_probes"
CREATE_CHAT_EFFECT_KIND = "cursor.create_chat"
RUN_CURSOR_TURN_EFFECT_KIND = "cursor.run_turn"
RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND = "cursor.reconcile_sdk_run"
INGEST_CURSOR_RESULT_EFFECT_KIND = "cursor.ingest_result"
NORMALIZE_STAGING_EFFECT_KIND = "cursor.normalize_staging"

PREFLIGHT_EFFECT_ID = "cursor-preflight"
CREATE_CHAT_EFFECT_ID = "cursor-create-chat"
RUN_CURSOR_TURN_EFFECT_ID = "cursor-run-turn"
RECONCILE_CURSOR_SDK_RUN_EFFECT_ID = "cursor-reconcile-sdk-run"
INGEST_CURSOR_RESULT_EFFECT_ID = "cursor-ingest-result"
NORMALIZE_STAGING_EFFECT_ID = "cursor-normalize-staging"

CURSOR_CHAT_ARTIFACT = "cursor/chat.json"
CURSOR_SDK_IDENTITY_ARTIFACT = "cursor/sdk-identity.json"
CURSOR_SDK_CAPTURE_SCHEMA_VERSION = 1
CURSOR_SDK_EVENT_STREAM_FORMAT = "cursor-sdk-envelope-ndjson-v1"
CURSOR_SDK_TEXT_STREAM_FORMAT = "utf-8-text-v1"

MAX_CURSOR_EVENTS_ARTIFACT_BYTES = 8 * 1024 * 1024

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


def cursor_sdk_attempt_journal_rel(attempt_id: str) -> str:
    return f"cursor/sdk-attempts/{attempt_id}/dispatch-journal.jsonl"


def cursor_sdk_reconciliation_record_rel(
    source_attempt_id: str,
    reconciliation_attempt_id: str | None = None,
    *,
    schema_version: int = 1,
) -> str:
    filename = "record-v2.json" if schema_version == 2 else "record-v1.json"
    directory = f"cursor/sdk-reconciliation/{source_attempt_id}"
    if reconciliation_attempt_id is None:
        return f"{directory}/{filename}"
    return f"{directory}/attempts/{reconciliation_attempt_id}/{filename}"


def cursor_sdk_attempt_events_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/sdk-envelopes.jsonl"


def cursor_sdk_attempt_metadata_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/sdk-metadata.json"


def cursor_attempt_output_observation_rel(iteration_number: int, attempt_id: str) -> str:
    return f"{cursor_attempt_iteration_dir(iteration_number, attempt_id)}/output-observation.json"


def git_status_before_cursor_rel(iteration_number: int, attempt_id: str) -> str:
    return f"git/status/{iteration_label(iteration_number)}.{attempt_id}-before-cursor.txt"


def git_status_after_cursor_rel(iteration_number: int, attempt_id: str) -> str:
    return f"git/status/{iteration_label(iteration_number)}.{attempt_id}-after-cursor.txt"
