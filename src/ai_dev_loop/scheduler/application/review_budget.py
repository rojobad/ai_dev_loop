"""Effective review ceiling derived from frozen submit context plus extension events."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.events import (
    REVIEW_BUDGET_EXTENDED_EVENT_KIND,
    ReviewBudgetExtendedEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.state import MaxIterationsReachedState, SchedulerState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def fold_review_budget_extensions(
    base_ceiling: int,
    events: Iterable[ReviewBudgetExtendedEvent],
) -> int:
    effective = base_ceiling
    for event in events:
        if event.previous_effective_total != effective:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "review budget extension event has inconsistent previous total",
            )
        if event.new_effective_total <= effective:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "review budget extension event does not raise the effective ceiling",
            )
        if event.review_iteration != event.previous_effective_total:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "review budget extension event exhausted iteration disagrees with previous total",
            )
        effective = event.new_effective_total
    return effective


def _validate_extension_event_row(
    row: object,
    *,
    expected_run_id: str,
    prior_sequence: int | None,
) -> ReviewBudgetExtendedEvent:
    row_run_id = str(row["run_id"])  # type: ignore[index]
    if row_run_id != expected_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event run_id disagrees with ledger run",
        )
    sequence = int(row["sequence"])  # type: ignore[index]
    if prior_sequence is not None and sequence <= prior_sequence:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension events are out of chronological order",
        )
    payload_text = str(row["event_payload"])  # type: ignore[index]
    stored_digest = str(row["event_payload_sha256"])  # type: ignore[index]
    if payload_sha256(payload_text) != stored_digest:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event payload digest mismatch",
        )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event payload is not valid JSON",
        ) from exc
    event = parse_scheduler_event(payload)
    if not isinstance(event, ReviewBudgetExtendedEvent):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event payload has unexpected kind",
        )
    if event.run_id != expected_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event payload run_id disagrees with ledger run",
        )
    if event.review_iteration != event.previous_effective_total:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "review budget extension event has invalid exhausted review iteration",
        )
    return event


def load_validated_review_budget_extensions(
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[ReviewBudgetExtendedEvent, ...]:
    rows = conn.execute(
        """
        SELECT event_id, run_id, sequence, event_kind,
               event_payload, event_payload_sha256, created_at
        FROM scheduler_events
        WHERE run_id = ? AND event_kind = ?
        ORDER BY sequence ASC
        """,
        (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
    ).fetchall()
    extensions: list[ReviewBudgetExtendedEvent] = []
    prior_sequence: int | None = None
    for row in rows:
        extensions.append(
            _validate_extension_event_row(
                row,
                expected_run_id=run_id,
                prior_sequence=prior_sequence,
            )
        )
        prior_sequence = int(row["sequence"])
    return tuple(extensions)


def load_review_budget_extensions(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[ReviewBudgetExtendedEvent, ...]:
    return load_validated_review_budget_extensions(conn, run_id)


def effective_review_ceiling_for_state(
    state: SchedulerState,
    extension_events: Iterable[ReviewBudgetExtendedEvent],
) -> int:
    base = state.context.workflow.max_review_iterations
    return fold_review_budget_extensions(base, extension_events)


def effective_review_ceiling_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: SchedulerState,
) -> int:
    extensions = load_review_budget_extensions(store, conn, state.run_id)
    return effective_review_ceiling_for_state(state, extensions)


def validate_extension_target_for_exhausted_state(
    state: MaxIterationsReachedState,
    *,
    target_total: int,
    extension_events: Iterable[ReviewBudgetExtendedEvent],
) -> int:
    effective_total = effective_review_ceiling_for_state(state, extension_events)
    if state.codex.review_iteration != state.codex.reviews_completed:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review checkpoint disagrees with completed review count",
        )
    if state.codex.review_iteration != effective_total:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "exhausted review iteration does not match the effective review ceiling",
        )
    if target_total <= effective_total:
        if target_total < effective_total:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "requested review ceiling is lower than the current effective total",
            )
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "requested review ceiling equals the current effective total",
        )
    return effective_total


def find_extension_event_for_target(
    events: Iterable[ReviewBudgetExtendedEvent],
    target_total: int,
) -> ReviewBudgetExtendedEvent | None:
    for event in events:
        if event.new_effective_total == target_total:
            return event
    return None


def review_budget_extension_applied(
    state: SchedulerState,
    extension_events: Iterable[ReviewBudgetExtendedEvent],
) -> bool:
    base = state.context.workflow.max_review_iterations
    try:
        effective = fold_review_budget_extensions(base, extension_events)
    except SchedulerEngineError:
        return False
    return effective > base


def review_budget_projection(
    state: SchedulerState,
    extension_events: Iterable[ReviewBudgetExtendedEvent],
    *,
    ledger_reviews_completed: int | None = None,
) -> tuple[int, int, int | None]:
    from ai_dev_loop.scheduler.application.contracts import review_budget_from_state

    base = state.context.workflow.max_review_iterations
    effective = effective_review_ceiling_for_state(state, extension_events)
    reviews_completed, _ = review_budget_from_state(
        state,
        ledger_reviews_completed=ledger_reviews_completed,
        effective_max_review_iterations=effective,
    )
    submitted_max = base if effective > base else None
    return reviews_completed, effective, submitted_max
