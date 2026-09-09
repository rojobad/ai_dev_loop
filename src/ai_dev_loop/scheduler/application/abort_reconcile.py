"""Shared abort termination reconciliation and resource-release boundaries."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ai_dev_loop.scheduler.application.attempt_backend import (
    AgentProcessBackend,
    ObserveResult,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.contracts import AbortProcessAction
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.events import AttemptResultStaleEvent
from ai_dev_loop.scheduler.domain.reducer import apply_attempt_result_stale
from ai_dev_loop.scheduler.domain.state import AbortedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_ACTIVE,
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_LAUNCHING,
    ATTEMPT_STATUS_UNCERTAIN,
    SqliteSchedulerStore,
)


@dataclass(frozen=True)
class AbortTerminationSummary:
    process_action: AbortProcessAction
    termination_pending: bool
    attempts_finalized: int


def unit_observation_stopped(observation: ObserveResult) -> bool:
    if observation.lifecycle_state in {
        UnitLifecycleState.INACTIVE,
        UnitLifecycleState.FAILED,
    }:
        return True
    return (
        observation.lifecycle_state == UnitLifecycleState.MISSING
        and observation.owned
        and observation.absence_proven
    )


def _merge_process_action(
    current: AbortProcessAction,
    new: AbortProcessAction,
) -> AbortProcessAction:
    priority = {
        AbortProcessAction.NONE: 0,
        AbortProcessAction.TERMINATED: 1,
        AbortProcessAction.REFUSED: 2,
        AbortProcessAction.UNAVAILABLE: 3,
    }
    if priority[new] >= priority[current]:
        return new
    return current


def attempt_stop_action(
    backend: AgentProcessBackend,
    *,
    unit_identity: str,
    attempt_id: str,
) -> tuple[AbortProcessAction, ObserveResult]:
    observation = backend.observe(unit_identity=unit_identity, attempt_id=attempt_id)
    if observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE:
        return AbortProcessAction.UNAVAILABLE, observation
    if unit_observation_stopped(observation):
        return AbortProcessAction.NONE, observation
    if observation.lifecycle_state == UnitLifecycleState.ACTIVE and observation.owned:
        try:
            backend.terminate(unit_identity=unit_identity, attempt_id=attempt_id)
        except RuntimeError:
            return AbortProcessAction.REFUSED, observation
        except ValueError:
            return AbortProcessAction.REFUSED, observation
        observation = backend.observe(unit_identity=unit_identity, attempt_id=attempt_id)
        if observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE:
            return AbortProcessAction.UNAVAILABLE, observation
        if unit_observation_stopped(observation):
            return AbortProcessAction.TERMINATED, observation
        return AbortProcessAction.TERMINATED, observation
    return AbortProcessAction.NONE, observation


def release_abort_hold_resources(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: datetime,
) -> bool:
    state, _, _ = store.load_validated_snapshot(conn, run_id)
    if not isinstance(state, AbortedState):
        return False
    if store.has_unresolved_abort_hold(conn, run_id):
        return False
    released_capacity = store.release_capacity_for_run(conn, run_id=run_id, now=now)
    reservation = store.get_reservation_for_run(conn, run_id)
    if reservation is not None:
        store.release_reservation(
            conn,
            worktree_key=str(reservation["worktree_key"]),
            now=now,
        )
    return released_capacity


def finalize_aborted_run_cleanup(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: datetime,
) -> bool:
    """Release capacity/reservation when an aborted run has no unresolved attempt holds."""
    return release_abort_hold_resources(store, conn, run_id=run_id, now=now)


def append_attempt_result_stale_event(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
    rejection_kind: str,
    safe_summary: str,
    now: datetime,
    event_id_factory: Callable[[], str],
) -> None:
    state, _, _ = store.load_validated_snapshot(conn, run_id)
    if isinstance(state, AbortedState):
        apply_attempt_result_stale(
            state,
            AttemptResultStaleEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                rejection_kind=rejection_kind,
                safe_summary=safe_summary,
            ),
        )
    event = AttemptResultStaleEvent(
        run_id=run_id,
        attempt_id=attempt_id,
        rejection_kind=rejection_kind,
        safe_summary=safe_summary,
    )
    sequence = store.next_event_sequence(conn, run_id)
    store.append_event(
        conn,
        event_id=event_id_factory(),
        run_id=run_id,
        sequence=sequence,
        event=event,
        now=now,
    )


def finalize_resolved_abort_attempt(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
    now: datetime,
    event_id_factory: Callable[[], str],
    stale_rejection_kind: str | None = None,
    stale_safe_summary: str | None = None,
) -> None:
    store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
    if stale_rejection_kind is not None and stale_safe_summary is not None:
        append_attempt_result_stale_event(
            store,
            conn,
            run_id=run_id,
            attempt_id=attempt_id,
            rejection_kind=stale_rejection_kind,
            safe_summary=stale_safe_summary,
            now=now,
            event_id_factory=event_id_factory,
        )
    release_abort_hold_resources(store, conn, run_id=run_id, now=now)


def reconcile_run_abort_attempts(
    store: SqliteSchedulerStore,
    backend: AgentProcessBackend,
    *,
    run_id: str,
    attempt_rows: Sequence[sqlite3.Row],
    now: datetime,
    event_id_factory: Callable[[], str],
    emit_stale_events: bool = False,
    stale_rejection_kind: str = "run_aborted",
    stale_safe_summary: str = "attempt result arrived after scheduler abort",
) -> AbortTerminationSummary:
    process_action = AbortProcessAction.NONE
    termination_pending = False
    attempts_finalized = 0

    for row in attempt_rows:
        attempt_id = str(row["attempt_id"])
        unit_identity = row["unit_identity"]
        if unit_identity is None:
            with store.begin_immediate() as conn:
                finalize_resolved_abort_attempt(
                    store,
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    now=now,
                    event_id_factory=event_id_factory,
                )
            attempts_finalized += 1
            continue

        stop_action, observation = attempt_stop_action(
            backend,
            unit_identity=str(unit_identity),
            attempt_id=attempt_id,
        )
        process_action = _merge_process_action(process_action, stop_action)
        if not unit_observation_stopped(observation):
            termination_pending = True
            continue

        with store.begin_immediate() as conn:
            status = str(row["status"])
            if status in {
                ATTEMPT_STATUS_LAUNCHING,
                ATTEMPT_STATUS_ACTIVE,
                ATTEMPT_STATUS_UNCERTAIN,
            }:
                now_text = encode_utc_instant(now)
                conn.execute(
                    """
                    UPDATE scheduler_attempts
                    SET status = ?, completion_fence_id = COALESCE(completion_fence_id, ?),
                        updated_at = ?
                    WHERE attempt_id = ? AND status IN (?, ?, ?)
                    """,
                    (
                        ATTEMPT_STATUS_CANCELLED,
                        f"fnc-abort-reconcile-{attempt_id[-8:]}",
                        now_text,
                        attempt_id,
                        ATTEMPT_STATUS_LAUNCHING,
                        ATTEMPT_STATUS_ACTIVE,
                        ATTEMPT_STATUS_UNCERTAIN,
                    ),
                )
            finalize_resolved_abort_attempt(
                store,
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                now=now,
                event_id_factory=event_id_factory,
                stale_rejection_kind=(stale_rejection_kind if emit_stale_events else None),
                stale_safe_summary=(stale_safe_summary if emit_stale_events else None),
            )
        attempts_finalized += 1

    if termination_pending and process_action == AbortProcessAction.NONE:
        process_action = AbortProcessAction.UNAVAILABLE

    return AbortTerminationSummary(
        process_action=process_action,
        termination_pending=termination_pending,
        attempts_finalized=attempts_finalized,
    )
