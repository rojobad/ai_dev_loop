"""Bounded redacted scheduler event history projection."""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    HARD_HISTORY_MAX,
    HistoryEntry,
    HistoryResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    redacted_session_prefix,
)
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_SENSITIVE_KEY_MARKERS = (
    "prompt",
    "patch",
    "stdout",
    "stderr",
    "session_id",
    "chat_id",
    "argv",
    "environment",
    "secret",
    "token",
    "review_markdown",
    "cursor_fix_prompt",
    "binding_artifact",
    "envelope",
)
_UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_UNIT_ID_PATTERN = re.compile(r"ai-dev-loop-attempt-[0-9a-f]{32}", re.IGNORECASE)


def _redact_text(value: str) -> str:
    redacted = _UUID_PATTERN.sub(
        lambda match: redacted_session_prefix(match.group(0)),
        value,
    )
    return _UNIT_ID_PATTERN.sub("<unit-redacted>", redacted)


def _safe_detail_for_event(event_kind: str, payload_text: str) -> str:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError:
        return f"{event_kind} (payload unavailable)"
    if not isinstance(payload, dict):
        return event_kind
    for key in payload:
        lowered = key.lower()
        if any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS):
            return f"{event_kind} (sensitive fields redacted)"
    if event_kind == "abort_requested":
        return f"{event_kind}: reason={payload.get('reason', 'user_requested_abort')}"
    if event_kind == "run_aborted":
        return (
            f"{event_kind}: prior_state={payload.get('prior_state_kind')} "
            f"reason={payload.get('reason', 'user_requested_abort')}"
        )
    if event_kind == "attempt_result_stale":
        return (
            f"{event_kind}: rejection={payload.get('rejection_kind')} "
            f"summary={_redact_text(str(payload.get('safe_summary', '')))}"
        )
    if event_kind == "tick_stale_rejected":
        return (
            f"{event_kind}: rejection={payload.get('rejection_kind')} "
            f"summary={_redact_text(str(payload.get('safe_summary', '')))}"
        )
    if event_kind == "codex_reviewer_bound":
        prefix = payload.get("reviewer_session_id_prefix")
        if isinstance(prefix, str):
            return f"{event_kind}: reviewer={prefix}"
    if event_kind in {"cursor_chat_created"}:
        return f"{event_kind} (identity redacted)"
    if event_kind in {
        "worktree_admission_blocked",
        "preflight_blocked",
        "cursor_chat_blocked",
        "cursor_turn_blocked",
        "staging_blocked",
        "codex_review_blocked",
    }:
        kind = payload.get("block_reason_kind")
        return f"{event_kind}: block_reason_kind={kind}"
    if event_kind == "cursor_timeout_retry":
        return (
            f"{event_kind}: action={payload.get('action')} "
            f"automatic_retries={payload.get('automatic_retries')} "
            f"available_at={payload.get('available_at')}"
        )
    if event_kind == "cursor_usage_limit_detected":
        wait_until = payload.get("wait_until")
        return f"{event_kind}: wait_until={wait_until}"
    if event_kind in {"codex_usage_capacity_detected", "codex_capacity_available"}:
        iteration = payload.get("review_iteration")
        source = payload.get("evidence_source")
        operational = payload.get("operational_failure_kind")
        routing_eligible = payload.get("routing_auto_retry_eligible")
        routing_due = payload.get("routing_auto_retry_due_at")
        routing_used = payload.get("routing_auto_retry_authorizations_used")
        probe_status = payload.get("routing_failure_post_probe_status")
        probe_reason = payload.get("routing_failure_post_probe_reason")
        parts = [f"review_iteration={iteration}"]
        if source:
            parts.append(f"evidence_source={source}")
        if operational:
            parts.append(f"operational_failure_kind={operational}")
        if routing_eligible is not None:
            parts.append(f"routing_auto_retry_eligible={routing_eligible}")
        if routing_due:
            parts.append(f"routing_auto_retry_due_at={routing_due}")
        if routing_used is not None:
            parts.append(f"routing_auto_retry_authorizations_used={routing_used}")
        if routing_used is not None or operational:
            parts.append(f"routing_auto_retry_limit={ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS}")
        if probe_status:
            parts.append(f"routing_failure_post_probe_status={probe_status}")
        if probe_reason:
            parts.append(f"routing_failure_post_probe_reason={probe_reason}")
        return f"{event_kind}: " + " ".join(parts)
    if event_kind == "codex_capacity_retry_authorized":
        return (
            f"{event_kind}: capacity_wait_generation={payload.get('capacity_wait_generation')} "
            f"idempotent_replay={payload.get('idempotent_replay')}"
        )
    if event_kind == "codex_review_retryable_failure":
        failure_kind = payload.get("failure_kind")
        routing_used = payload.get("routing_auto_retry_authorizations_used")
        routing_eligible = payload.get("routing_auto_retry_eligible")
        limit_suffix = ""
        if (
            failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
            or routing_eligible
            or routing_used is not None
        ):
            limit_suffix = f" routing_auto_retry_limit={ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS}"
        return (
            f"{event_kind}: failure_kind={failure_kind} "
            f"retry_generation={payload.get('retry_generation')} "
            f"routing_auto_retry_eligible={routing_eligible} "
            f"routing_auto_retry_due_at={payload.get('routing_auto_retry_due_at')} "
            f"routing_auto_retry_authorizations_used={routing_used}"
            f"{limit_suffix} "
            f"routing_failure_post_probe_status={payload.get('routing_failure_post_probe_status')} "
            f"routing_failure_post_probe_reason={payload.get('routing_failure_post_probe_reason')}"
        )
    if event_kind == "codex_review_retry_requested":
        return (
            f"{event_kind}: retry_generation={payload.get('retry_generation')} "
            f"idempotent_replay={payload.get('idempotent_replay')} "
            f"authorization_source={payload.get('authorization_source')}"
        )
    if event_kind == "codex_review_recovery_successor_created":
        return (
            f"{event_kind}: source_run_id={payload.get('source_run_id')} "
            f"successor_run_id={payload.get('successor_run_id')}"
        )
    if event_kind == "review_budget_extended":
        return (
            f"{event_kind}: review_iteration={payload.get('review_iteration')} "
            f"previous_total={payload.get('previous_effective_total')} "
            f"new_total={payload.get('new_effective_total')} "
            "(artifact bindings redacted)"
        )
    if event_kind == "attempt_completed":
        return (
            f"{event_kind}: termination={payload.get('termination_class')} "
            f"exit_code={payload.get('exit_code')}"
        )
    if event_kind == "attempt_uncertain":
        return f"{event_kind}: summary={_redact_text(str(payload.get('safe_summary', '')))}"
    return event_kind


class SchedulerHistoryService:
    def __init__(self, store: SqliteSchedulerStore) -> None:
        self.store = store

    def get_history(
        self,
        run_id: str,
        *,
        limit: int = 50,
        order: str = "oldest",
    ) -> HistoryResult:
        if limit < 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "history limit must be positive",
            )
        if order not in {"oldest", "newest"}:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "history order must be oldest or newest",
            )
        capped = min(limit, HARD_HISTORY_MAX)
        newest_first = order == "newest"
        with self.store.begin_read() as conn:
            self.store.load_validated_snapshot(conn, run_id)
            rows = self.store.list_events_for_run(
                conn,
                run_id,
                limit=capped + 1,
                newest_first=newest_first,
            )
        truncated = len(rows) > capped
        rows = rows[:capped]
        entries = tuple(
            HistoryEntry(
                sequence=int(row["sequence"]),
                event_kind=str(row["event_kind"]),
                created_at=str(row["created_at"]),
                safe_detail=_safe_detail_for_event(
                    str(row["event_kind"]),
                    str(row["event_payload"]),
                ),
            )
            for row in rows
        )
        return HistoryResult(
            run_id=run_id,
            order=order,
            limit=capped,
            truncated=truncated,
            entries=entries,
        )


def default_history_service(*, db_path: Path | None = None) -> SchedulerHistoryService:
    path = db_path or default_engine_db_path()
    return SchedulerHistoryService(SqliteSchedulerStore.open_readonly(path))


def scheduler_history(
    run_id: str,
    *,
    limit: int = 50,
    order: str = "oldest",
    db_path: Path | None = None,
) -> HistoryResult:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    return default_history_service(db_path=path).get_history(
        run_id,
        limit=limit,
        order=order,
    )
