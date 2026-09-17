"""Read-only lineage projections for sequence status and completion reports."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.sequence import PreparedSequenceDefinition, PreparedSequenceState
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_PLANNED,
    MaterializedSequenceState,
    SequencePhaseExecution,
    SequenceRunLineageValidationError,
    project_historical_lineage_from_state,
)
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
    load_sequence_run_lineage_rows,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@dataclass(frozen=True)
class PhaseLineageProjection:
    ordinal: int
    attempt_count: int
    current_run_id_prefix: str
    accepted_run_id_prefix: str | None
    accepted_attempt_kind: str | None
    attempt_kind_labels: tuple[str, ...]


def _phase_projection(phase: SequencePhaseExecution) -> PhaseLineageProjection:
    kinds = tuple(attempt.attempt_kind for attempt in phase.attempts)
    accepted_kind: str | None = None
    accepted_prefix: str | None = None
    if phase.accepted_run_id is not None:
        accepted_prefix = phase.accepted_run_id[:8]
        for attempt in phase.attempts:
            if attempt.run_id == phase.accepted_run_id:
                accepted_kind = attempt.attempt_kind
                break
    leaf = phase.attempts[-1]
    return PhaseLineageProjection(
        ordinal=phase.ordinal,
        attempt_count=len(phase.attempts),
        current_run_id_prefix=leaf.run_id[:8],
        accepted_run_id_prefix=accepted_prefix,
        accepted_attempt_kind=accepted_kind,
        attempt_kind_labels=kinds,
    )


def load_phase_lineage_projections(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    definition: PreparedSequenceDefinition,
    sequence_state: MaterializedSequenceState | PreparedSequenceState | None = None,
) -> dict[int, PhaseLineageProjection]:
    if not store.schema_supports_sequence_run_lineage(conn):
        if sequence_state is None or isinstance(sequence_state, PreparedSequenceState):
            return {}
        lineage = project_historical_lineage_from_state(sequence_state)
        return {phase.ordinal: _phase_projection(phase) for phase in lineage.phase_executions}

    rows = load_sequence_run_lineage_rows(conn, sequence_id)
    if not rows:
        if sequence_state is None or isinstance(sequence_state, PreparedSequenceState):
            return {}
        lineage = project_historical_lineage_from_state(sequence_state)
        return {phase.ordinal: _phase_projection(phase) for phase in lineage.phase_executions}

    try:
        lineage = load_sequence_run_lineage(conn, sequence_id, definition=definition)
    except SequenceRunLineageValidationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            str(exc),
        ) from exc
    return {phase.ordinal: _phase_projection(phase) for phase in lineage.phase_executions}


def default_accepted_attempt_kind(attempt_count: int) -> str:
    if attempt_count <= 1:
        return SEQUENCE_ATTEMPT_KIND_PLANNED
    return "same_reviewer_retry"
