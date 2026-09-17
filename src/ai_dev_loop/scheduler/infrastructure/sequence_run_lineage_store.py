"""SQLite persistence helpers for sequence run-attempt lineage."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
    MaterializedSequenceState,
    SequenceAttemptKind,
    SequencePhaseExecution,
    SequenceRunAttempt,
    SequenceRunLineage,
    SequenceRunLineageValidationError,
    SequenceTerminalOutcome,
    project_historical_lineage_from_state,
    validate_persisted_lineage_aggregate,
)

if TYPE_CHECKING:
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    SequencePersistedState = (
        PreparedSequenceState
        | ActiveSequenceState
        | AbortPendingSequenceState
        | BlockedSequenceState
        | AbortedSequenceState
        | AwaitingFinalizationSequenceState
    )

_LINEAGE_ROW_CORRUPTION = "lineage row payload is invalid"


def _parse_lineage_row_ordinal(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SequenceRunLineageValidationError(_LINEAGE_ROW_CORRUPTION)
    try:
        ordinal = int(value)
    except ValueError as exc:
        raise SequenceRunLineageValidationError(_LINEAGE_ROW_CORRUPTION) from exc
    return ordinal


def _validate_stored_terminal_pair(
    existing_outcome: object | None,
    existing_resolved: object | None,
) -> None:
    outcome_present = existing_outcome is not None
    resolved_present = existing_resolved is not None
    if outcome_present != resolved_present:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            _LINEAGE_ROW_CORRUPTION,
        )


def _attempt_immutable_fields_match(
    stored: SequenceRunAttempt,
    projected: SequenceRunAttempt,
) -> bool:
    return (
        stored.generation == projected.generation
        and stored.run_id == projected.run_id
        and stored.source_run_id == projected.source_run_id
        and stored.attempt_kind == projected.attempt_kind
        and stored.materialized_at == projected.materialized_at
    )


@dataclass(frozen=True)
class SequenceExecutionCASExpectation:
    """Complete immutable execution definition required for lineage CAS."""

    expected_version: int
    definition: PreparedSequenceDefinition
    materialized_entries: tuple[MaterializedSequenceEntry, ...]
    current_ordinal: int
    current_run_id: str


def _row_to_attempt(row: sqlite3.Row) -> SequenceRunAttempt:
    try:
        terminal_outcome = row["terminal_outcome"]
        resolved_at = row["resolved_at"]
        return SequenceRunAttempt(
            schema_version=1,
            generation=int(row["generation"]),
            run_id=str(row["run_id"]),
            source_run_id=str(row["source_run_id"]) if row["source_run_id"] is not None else None,
            attempt_kind=cast(SequenceAttemptKind, str(row["attempt_kind"])),
            materialized_at=str(row["materialized_at"]),
            terminal_outcome=cast(SequenceTerminalOutcome | None, terminal_outcome),
            resolved_at=str(resolved_at) if resolved_at is not None else None,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise SequenceRunLineageValidationError("lineage row payload is invalid") from exc


def load_sequence_run_lineage_rows(
    conn: sqlite3.Connection,
    sequence_id: str,
) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT sequence_id, ordinal, generation, run_id, source_run_id,
                   attempt_kind, materialized_at, terminal_outcome, resolved_at
            FROM scheduler_sequence_run_attempts
            WHERE sequence_id = ?
            ORDER BY ordinal ASC, generation ASC
            """,
            (sequence_id,),
        ).fetchall()
    )


def _build_phase_execution(
    ordinal: int,
    attempts: list[SequenceRunAttempt],
    *,
    planned_run_id: str,
) -> SequencePhaseExecution:
    if not attempts:
        raise SequenceRunLineageValidationError("phase execution requires at least one attempt")
    leaf = attempts[-1]
    accepted_run_id = (
        leaf.run_id
        if leaf.terminal_outcome in {"completed", "completed_with_residual_risk"}
        else None
    )
    try:
        return SequencePhaseExecution(
            schema_version=1,
            ordinal=ordinal,
            planned_run_id=planned_run_id,
            attempts=tuple(attempts),
            current_run_id=leaf.run_id,
            accepted_run_id=accepted_run_id,
        )
    except ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage phase payload is invalid") from exc


def build_lineage_from_rows(
    sequence_id: str,
    rows: list[sqlite3.Row],
    *,
    definition: PreparedSequenceDefinition | None = None,
) -> SequenceRunLineage:
    if not rows:
        return SequenceRunLineage(
            schema_version=1,
            sequence_id=sequence_id,
            phase_executions=(),
        )
    for row in rows:
        if str(row["sequence_id"]) != sequence_id:
            raise SequenceRunLineageValidationError("lineage row references another sequence")
    phases: list[SequencePhaseExecution] = []
    current_ordinal: int | None = None
    current_attempts: list[SequenceRunAttempt] = []
    for row in rows:
        ordinal = _parse_lineage_row_ordinal(row["ordinal"])
        if ordinal < 1:
            raise SequenceRunLineageValidationError(_LINEAGE_ROW_CORRUPTION)
        if current_ordinal is None:
            current_ordinal = ordinal
        if ordinal != current_ordinal:
            planned_run_id = (
                definition.entries[current_ordinal - 1].planned_run_id
                if definition is not None
                else current_attempts[0].run_id
            )
            phases.append(
                _build_phase_execution(
                    current_ordinal,
                    current_attempts,
                    planned_run_id=planned_run_id,
                )
            )
            current_ordinal = ordinal
            current_attempts = []
        current_attempts.append(_row_to_attempt(row))
    if current_ordinal is not None and current_attempts:
        planned_run_id = (
            definition.entries[current_ordinal - 1].planned_run_id
            if definition is not None
            else current_attempts[0].run_id
        )
        phases.append(
            _build_phase_execution(
                current_ordinal,
                current_attempts,
                planned_run_id=planned_run_id,
            )
        )
    try:
        return SequenceRunLineage(
            schema_version=1,
            sequence_id=sequence_id,
            phase_executions=tuple(phases),
        )
    except ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc


def load_sequence_run_lineage(
    conn: sqlite3.Connection,
    sequence_id: str,
    *,
    definition: PreparedSequenceDefinition | None = None,
) -> SequenceRunLineage:
    rows = load_sequence_run_lineage_rows(conn, sequence_id)
    return build_lineage_from_rows(sequence_id, rows, definition=definition)


def validate_no_extra_lineage_rows(
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    definition_entry_count: int,
    materialized_ordinals: set[int],
) -> None:
    rows = load_sequence_run_lineage_rows(conn, sequence_id)
    for row in rows:
        try:
            ordinal = _parse_lineage_row_ordinal(row["ordinal"])
        except SequenceRunLineageValidationError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                str(exc),
            ) from exc
        if ordinal < 1 or ordinal > definition_entry_count:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "lineage ordinal exceeds sequence definition",
            )
        if ordinal not in materialized_ordinals:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "lineage row references non-materialized ordinal",
            )


def insert_sequence_run_attempt(
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    attempt: SequenceRunAttempt,
    planned_run_id: str,
) -> None:
    if attempt.generation == 1 and attempt.run_id != planned_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "generation 1 run_id must equal planned_run_id",
        )
    existing = conn.execute(
        """
        SELECT sequence_id, ordinal, generation, run_id, source_run_id,
               attempt_kind, materialized_at, terminal_outcome, resolved_at
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ? AND generation = ?
        """,
        (sequence_id, ordinal, attempt.generation),
    ).fetchone()
    if existing is not None:
        stored = _row_to_attempt(cast(sqlite3.Row, existing))
        if stored != attempt:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence run attempt replay disagrees with stored row",
            )
        return
    try:
        conn.execute(
            """
            INSERT INTO scheduler_sequence_run_attempts(
                sequence_id, ordinal, generation, run_id, source_run_id,
                attempt_kind, materialized_at, terminal_outcome, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sequence_id,
                ordinal,
                attempt.generation,
                attempt.run_id,
                attempt.source_run_id,
                attempt.attempt_kind,
                attempt.materialized_at,
                attempt.terminal_outcome,
                attempt.resolved_at,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "sequence run attempt insert conflict",
        ) from exc


def resolve_sequence_attempt_terminal(
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
    run_id: str,
    terminal_outcome: SequenceTerminalOutcome,
    resolved_at: datetime,
) -> None:
    row = conn.execute(
        """
        SELECT terminal_outcome, resolved_at
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ? AND run_id = ?
        """,
        (sequence_id, ordinal, run_id),
    ).fetchone()
    if row is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "terminal resolution target attempt not found",
        )
    existing_outcome = row["terminal_outcome"]
    existing_resolved = row["resolved_at"]
    resolved_text = encode_utc_instant(resolved_at)
    _validate_stored_terminal_pair(existing_outcome, existing_resolved)
    if existing_outcome is not None:
        if str(existing_outcome) != terminal_outcome:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "terminal resolution replay disagrees with stored outcome",
            )
        if str(existing_resolved) != resolved_text:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "terminal resolution replay disagrees with stored resolved_at",
            )
        return
    cursor = conn.execute(
        """
        UPDATE scheduler_sequence_run_attempts
        SET terminal_outcome = ?, resolved_at = ?
        WHERE sequence_id = ? AND ordinal = ? AND run_id = ?
          AND terminal_outcome IS NULL
          AND resolved_at IS NULL
        """,
        (terminal_outcome, resolved_text, sequence_id, ordinal, run_id),
    )
    if cursor.rowcount == 1:
        return
    replay = conn.execute(
        """
        SELECT terminal_outcome, resolved_at
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ? AND run_id = ?
        """,
        (sequence_id, ordinal, run_id),
    ).fetchone()
    if replay is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "terminal resolution target attempt not found",
        )
    replay_outcome = replay["terminal_outcome"]
    replay_resolved = replay["resolved_at"]
    _validate_stored_terminal_pair(replay_outcome, replay_resolved)
    if str(replay_outcome) != terminal_outcome:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "terminal resolution replay disagrees with stored outcome",
        )
    if str(replay_resolved) != resolved_text:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "terminal resolution replay disagrees with stored resolved_at",
        )


def _current_leaf_run_id(
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
) -> str | None:
    row = conn.execute(
        """
        SELECT run_id
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ?
        ORDER BY generation DESC
        LIMIT 1
        """,
        (sequence_id, ordinal),
    ).fetchone()
    if row is None:
        return None
    return str(row["run_id"])


def _max_generation(
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    ordinal: int,
) -> int:
    row = conn.execute(
        """
        SELECT MAX(generation) AS max_generation
        FROM scheduler_sequence_run_attempts
        WHERE sequence_id = ? AND ordinal = ?
        """,
        (sequence_id, ordinal),
    ).fetchone()
    if row is None or row["max_generation"] is None:
        return 0
    return int(row["max_generation"])


def compare_and_swap_sequence_execution_leaf(
    conn: sqlite3.Connection,
    store: SqliteSchedulerStore,
    *,
    sequence_id: str,
    ordinal: int,
    expectation: SequenceExecutionCASExpectation,
    expected_current_run_id: str,
    replacement_attempt: SequenceRunAttempt,
    updated_sequence_state: ActiveSequenceState,
    now: datetime,
    superseded_terminal_outcome: SequenceTerminalOutcome = "blocked",
) -> bool:
    current = store.load_sequence_state_only(conn, sequence_id)
    if not isinstance(current, ActiveSequenceState):
        return False
    if not isinstance(updated_sequence_state, ActiveSequenceState):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "execution leaf replacement requires active sequence state",
        )
    if current.version != expectation.expected_version:
        return False
    if current.definition != expectation.definition:
        return False
    if current.materialized_entries != expectation.materialized_entries:
        return False
    if current.current_ordinal != expectation.current_ordinal:
        return False
    if current.current_run_id != expectation.current_run_id:
        return False
    if ordinal != expectation.current_ordinal:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "replacement ordinal must equal current ordinal",
        )
    leaf_run_id = _current_leaf_run_id(conn, sequence_id=sequence_id, ordinal=ordinal)
    if leaf_run_id != expected_current_run_id:
        return False
    next_generation = _max_generation(conn, sequence_id=sequence_id, ordinal=ordinal) + 1
    if replacement_attempt.generation != next_generation:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "replacement generation must be contiguous",
        )
    if replacement_attempt.source_run_id != expected_current_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "replacement attempt must reference expected current leaf",
        )
    if replacement_attempt.attempt_kind != SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "replacement attempt kind must be same_reviewer_retry",
        )
    if updated_sequence_state.sequence_id != sequence_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "updated sequence state disagrees with target sequence",
        )
    planned_run_id = expectation.definition.entries[ordinal - 1].planned_run_id
    if not store.compare_and_swap_sequence_state(
        conn,
        sequence_id=sequence_id,
        expected_version=expectation.expected_version,
        new_state=updated_sequence_state,
        now=now,
        validate_lineage=False,
    ):
        return False
    resolve_sequence_attempt_terminal(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        run_id=expected_current_run_id,
        terminal_outcome=superseded_terminal_outcome,
        resolved_at=now,
    )
    insert_sequence_run_attempt(
        conn,
        sequence_id=sequence_id,
        ordinal=ordinal,
        attempt=replacement_attempt,
        planned_run_id=planned_run_id,
    )
    load_and_validate_sequence_lineage(conn, updated_sequence_state)
    return True


def persist_authoritative_lineage_from_state(
    conn: sqlite3.Connection,
    state: MaterializedSequenceState,
) -> None:
    try:
        projected = project_historical_lineage_from_state(state)
    except SequenceRunLineageValidationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            str(exc),
        ) from exc
    for phase in projected.phase_executions:
        frozen = state.definition.entries[phase.ordinal - 1]
        for attempt in phase.attempts:
            existing = conn.execute(
                """
                SELECT sequence_id, ordinal, generation, run_id, source_run_id,
                       attempt_kind, materialized_at, terminal_outcome, resolved_at
                FROM scheduler_sequence_run_attempts
                WHERE sequence_id = ? AND ordinal = ? AND generation = ?
                """,
                (state.sequence_id, phase.ordinal, attempt.generation),
            ).fetchone()
            if existing is not None:
                stored = _row_to_attempt(cast(sqlite3.Row, existing))
                if stored == attempt:
                    continue
                if (
                    stored.terminal_outcome is None
                    and stored.resolved_at is None
                    and attempt.terminal_outcome is not None
                    and _attempt_immutable_fields_match(stored, attempt)
                ):
                    if attempt.resolved_at is None:
                        raise SchedulerEngineError(
                            SchedulerEngineErrorKind.CORRUPTION,
                            "projected terminal attempt missing resolved_at",
                        )
                    resolve_sequence_attempt_terminal(
                        conn,
                        sequence_id=state.sequence_id,
                        ordinal=phase.ordinal,
                        run_id=attempt.run_id,
                        terminal_outcome=attempt.terminal_outcome,
                        resolved_at=datetime.fromisoformat(
                            attempt.resolved_at.replace("Z", "+00:00")
                        ),
                    )
                    continue
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence run attempt replay disagrees with stored row",
                )
            insert_sequence_run_attempt(
                conn,
                sequence_id=state.sequence_id,
                ordinal=phase.ordinal,
                attempt=attempt,
                planned_run_id=frozen.planned_run_id,
            )


def backfill_historical_lineage_for_sequence(
    conn: sqlite3.Connection,
    store: SqliteSchedulerStore,
    sequence_id: str,
) -> None:
    state = store.load_validated_sequence_state(conn, sequence_id, validate_lineage=False)
    if isinstance(state, PreparedSequenceState):
        return
    if not isinstance(
        state,
        (
            ActiveSequenceState,
            AbortPendingSequenceState,
            BlockedSequenceState,
            AbortedSequenceState,
            AwaitingFinalizationSequenceState,
        ),
    ):
        return
    persist_authoritative_lineage_from_state(conn, state)


def load_and_validate_sequence_lineage(
    conn: sqlite3.Connection,
    state: SequencePersistedState,
) -> SequenceRunLineage:
    if isinstance(state, PreparedSequenceState):
        lineage = load_sequence_run_lineage(conn, state.sequence_id)
        if lineage.phase_executions:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "prepared sequence must not retain lineage rows",
            )
        return lineage

    materialized_ordinals = {entry.ordinal for entry in state.materialized_entries}
    validate_no_extra_lineage_rows(
        conn,
        sequence_id=state.sequence_id,
        definition_entry_count=len(state.definition.entries),
        materialized_ordinals=materialized_ordinals,
    )
    try:
        lineage = load_sequence_run_lineage(
            conn,
            state.sequence_id,
            definition=state.definition,
        )
    except SequenceRunLineageValidationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            str(exc),
        ) from exc
    if len(lineage.phase_executions) != len(materialized_ordinals):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "authoritative lineage is missing materialized phase rows",
        )
    try:
        validate_persisted_lineage_aggregate(state, lineage)
    except SequenceRunLineageValidationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            str(exc),
        ) from exc
    return lineage
