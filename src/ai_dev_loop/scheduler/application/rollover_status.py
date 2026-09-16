"""Read-only authenticated rollover status for Phase 20.6.5."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    RolloverStatusResult,
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    active_rollover_tick_next_action,
    integrated_rollover_safe_next_action,
    prepared_rollover_start_next_action,
)
from ai_dev_loop.scheduler.domain.rollover import (
    ACTIVE_ROLLOVER_STATE_KIND,
    CLEANUP_PENDING_ROLLOVER_STATE_KIND,
    INTEGRATED_ROLLOVER_STATE_KIND,
    INTEGRATION_PENDING_ROLLOVER_STATE_KIND,
    PREPARED_ROLLOVER_STATE_KIND,
    ROLLOVER_STATE_ADAPTERS,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _safe_action_for_kind(rollover_id: str, state_kind: str) -> SafeNextAction:
    if state_kind == PREPARED_ROLLOVER_STATE_KIND:
        return prepared_rollover_start_next_action(rollover_id)
    if state_kind in {
        ACTIVE_ROLLOVER_STATE_KIND,
        INTEGRATION_PENDING_ROLLOVER_STATE_KIND,
        CLEANUP_PENDING_ROLLOVER_STATE_KIND,
    }:
        return active_rollover_tick_next_action(rollover_id)
    if state_kind == INTEGRATED_ROLLOVER_STATE_KIND:
        return integrated_rollover_safe_next_action(rollover_id)
    return SafeNextAction(
        kind=SafeNextActionKind.INSPECT_BLOCKED,
        command=f"Rollover {rollover_id} is in state {state_kind}. Inspect protected artifacts.",
    )


def rollover_status(rollover_id: str, *, db_path: Path | None = None) -> RolloverStatusResult:
    path = db_path or default_engine_db_path()
    try:
        store = SqliteSchedulerStore.open_readonly(path)
    except Exception as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.SCHEMA,
            "rollover status requires a readable scheduler schema",
        ) from exc
    with store.begin_read() as conn:
        row = store.get_authenticated_rollover(conn, rollover_id)
    if row is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            f"rollover {rollover_id} not found",
        )
    state_kind = str(row["state_kind"])
    adapter = ROLLOVER_STATE_ADAPTERS[state_kind]
    state = adapter.validate_json(str(row["state_payload"]))
    accepted_outcome = getattr(state, "accepted_outcome", None)
    residual_risk = getattr(state, "residual_risk", None)
    integrated_prefix = getattr(state, "integrated_commit_sha256_prefix", None)
    sequence = getattr(state, "sequence", None)
    return RolloverStatusResult(
        rollover_id=rollover_id,
        rollover_id_prefix=rollover_id[:12],
        source_run_id_prefix=str(row["source_run_id"])[:8],
        state_kind=state_kind,
        rollover_run_id_prefix=(
            str(row["rollover_run_id"])[:8] if row["rollover_run_id"] else None
        ),
        sequence_id_prefix=(sequence.sequence_id[:8] if sequence is not None else None),
        sequence_ordinal=sequence.ordinal if sequence is not None else None,
        accepted_outcome=accepted_outcome,
        residual_risk=residual_risk,
        integrated_commit_prefix=integrated_prefix,
        safe_next_action=_safe_action_for_kind(rollover_id, state_kind),
    )
