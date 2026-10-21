"""Bounded, read-only scheduler ledger adapter for dashboard queries."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from ai_dev_loop.dashboard.application.ports import (
    AttemptObservation,
    DashboardObservationSet,
    DashboardReadError,
    DashboardReadErrorKind,
    DashboardReadOperation,
    DashboardReadScope,
    ProjectCatalogObservation,
    ProjectCountObservation,
    RunObservation,
    SequenceBindingObservation,
)
from ai_dev_loop.dashboard.domain.metrics import (
    execution_role,
    first_execution_launches,
    resolved_process_interval,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.domain.common import coerce_utc_instant, payload_sha256, worktree_key
from ai_dev_loop.scheduler.domain.sequence import PreparedSequenceState
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    MaterializedSequenceState,
    SequenceRunLineageValidationError,
    project_historical_lineage_from_state,
)
from ai_dev_loop.scheduler.domain.state import AuthorizedState, SchedulerState
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
    load_sequence_run_lineage_rows,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

MAX_RELEVANT_RUNS = 10_000
MAX_CONTRIBUTING_ATTEMPTS = 100_000
DEFAULT_QUERY_BUDGET_SECONDS = 5.0
DEFAULT_PROGRESS_STEPS = 1_000

_SCOPE_MESSAGE = "Dashboard scope is too large; narrow the project or time range."
_CATALOG_MESSAGE = "Dashboard project catalog is too large; retry or reduce the page size."


class _QueryBudget:
    def __init__(
        self,
        *,
        seconds: float,
        monotonic: Callable[[], float],
        cancelled: Callable[[], bool],
        absolute_deadline: float | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._cancelled = cancelled
        self._deadline = (
            absolute_deadline if absolute_deadline is not None else monotonic() + seconds
        )
        self.expired = False

    def should_interrupt(self) -> int:
        if self._cancelled() or self._monotonic() >= self._deadline:
            self.expired = True
            return 1
        return 0

    def check(self, message: str = _SCOPE_MESSAGE) -> None:
        if self.should_interrupt():
            raise DashboardReadError(DashboardReadErrorKind.INVALID_ARGUMENT, message)


def _map_scheduler_error(exc: SchedulerEngineError) -> DashboardReadError:
    if exc.kind is SchedulerEngineErrorKind.SCHEMA:
        return DashboardReadError(
            DashboardReadErrorKind.UNSUPPORTED,
            "This scheduler database version is not supported by dashboard queries.",
        )
    if exc.kind is SchedulerEngineErrorKind.CORRUPTION:
        return DashboardReadError(
            DashboardReadErrorKind.DATA_INTEGRITY,
            "Scheduler dashboard data failed integrity validation.",
        )
    if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
        return DashboardReadError(
            DashboardReadErrorKind.DATA_INTEGRITY,
            "Scheduler dashboard data failed integrity validation.",
        )
    if exc.kind is SchedulerEngineErrorKind.BUSY:
        return DashboardReadError(
            DashboardReadErrorKind.INTERNAL,
            "The scheduler database is temporarily unavailable.",
        )
    return DashboardReadError(
        DashboardReadErrorKind.INTERNAL,
        "Unable to read scheduler dashboard data.",
    )


def _map_sqlite_open_error(exc: sqlite3.DatabaseError) -> DashboardReadError:
    error_code = getattr(exc, "sqlite_errorcode", None)
    primary_code = error_code & 0xFF if isinstance(error_code, int) else None
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return DashboardReadError(
            DashboardReadErrorKind.INTERNAL,
            "The scheduler database is temporarily unavailable.",
        )
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return DashboardReadError(
            DashboardReadErrorKind.DATA_INTEGRITY,
            "Scheduler dashboard data failed integrity validation.",
        )
    if isinstance(exc, sqlite3.OperationalError):
        return DashboardReadError(
            DashboardReadErrorKind.IO,
            "Unable to read scheduler dashboard data.",
        )
    return DashboardReadError(
        DashboardReadErrorKind.DATA_INTEGRITY,
        "Scheduler dashboard data failed integrity validation.",
    )


class SqliteDashboardReadRepository:
    """Build validated observations without mutating or migrating scheduler state."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        query_budget_seconds: float = DEFAULT_QUERY_BUDGET_SECONDS,
        progress_steps: int = DEFAULT_PROGRESS_STEPS,
        monotonic: Callable[[], float] = time.monotonic,
        cancelled: Callable[[], bool] | None = None,
        absolute_deadline: float | None = None,
    ) -> None:
        if query_budget_seconds <= 0:
            raise ValueError("query_budget_seconds must be positive")
        if progress_steps < 1:
            raise ValueError("progress_steps must be positive")
        self.db_path = db_path or default_engine_db_path()
        self.query_budget_seconds = query_budget_seconds
        self.progress_steps = progress_steps
        self.monotonic = monotonic
        self.cancelled = cancelled or (lambda: False)
        self.absolute_deadline = absolute_deadline

    def _open_store(self) -> SqliteSchedulerStore | None:
        if not self.db_path.exists():
            return None
        try:
            return SqliteSchedulerStore.open_readonly(self.db_path)
        except SchedulerEngineError as exc:
            raise _map_scheduler_error(exc) from exc
        except sqlite3.DatabaseError as exc:
            raise _map_sqlite_open_error(exc) from exc
        except OSError as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.IO,
                "Unable to read scheduler dashboard data.",
            ) from exc

    def _validate_run_row(self, store: SqliteSchedulerStore, row: sqlite3.Row) -> SchedulerState:
        try:
            payload = str(row["state_payload"])
            state = store.load_state(payload)
        except (TypeError, ValueError, ValidationError) as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            ) from exc
        if (
            state.kind != str(row["state_kind"])
            or payload_sha256(payload) != str(row["state_payload_sha256"])
            or state.run_id != str(row["run_id"])
            or worktree_key(str(state.context.repository.root)) != str(row["worktree_key"])
        ):
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            )
        reservation_root = row["active_reservation_root"]
        if reservation_root is not None and str(reservation_root) != str(
            state.context.repository.root
        ):
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            )
        if isinstance(state, AuthorizedState) and (
            state.authorized_controller_session_id != state.context.controller.controller_session_id
        ):
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            )
        return state

    @staticmethod
    def _candidate_sql(project_name: str | None) -> tuple[str, tuple[object, ...]]:
        predicate = ""
        params: tuple[object, ...] = ()
        if project_name is not None:
            predicate = (
                " WHERE json_valid(r.state_payload) "
                "AND json_extract(r.state_payload, '$.context.project_name') = ?"
            )
            params = (project_name,)
        return (
            """
            SELECT r.*,
                   reservation.repository_root AS active_reservation_root
            FROM scheduler_runs AS r
            LEFT JOIN scheduler_repository_reservations AS reservation
              ON reservation.run_id = r.run_id AND reservation.status = 'active'
            """
            + predicate
            + " ORDER BY r.created_at ASC, r.run_id ASC",
            params,
        )

    def _load_runs(
        self,
        store: SqliteSchedulerStore,
        conn: sqlite3.Connection,
        *,
        project_name: str | None,
        budget: _QueryBudget,
    ) -> tuple[list[RunObservation], dict[str, SchedulerState]]:
        sql, params = self._candidate_sql(project_name)
        rows = conn.execute(sql, params).fetchall()
        states: dict[str, SchedulerState] = {}
        runs: list[RunObservation] = []
        for row in rows:
            budget.check()
            state = self._validate_run_row(store, cast(sqlite3.Row, row))
            if project_name is not None and state.context.project_name != project_name:
                raise DashboardReadError(
                    DashboardReadErrorKind.DATA_INTEGRITY,
                    "Scheduler dashboard data failed integrity validation.",
                )
            run_id = state.run_id
            states[run_id] = state
            implementer = state.context.implementer
            runs.append(
                RunObservation(
                    run_id=run_id,
                    short_name=store.run_short_name(conn, run_id),
                    project_name=state.context.project_name,
                    implementer_provider=(
                        "cursor" if implementer is None else implementer.provider
                    ),
                    state=state.kind,
                    submitted_at=coerce_utc_instant(state.submitted_at),
                    sequence_binding=None,
                )
            )
        return runs, states

    def _lineage_bindings(
        self,
        store: SqliteSchedulerStore,
        conn: sqlite3.Connection,
        states: dict[str, SchedulerState],
        budget: _QueryBudget,
    ) -> dict[str, SequenceBindingObservation]:
        sequence_ids = sorted(
            {
                state.context.sequence.sequence_id
                for state in states.values()
                if state.context.sequence is not None
            }
        )
        bindings: dict[str, SequenceBindingObservation] = {}
        for sequence_id in sequence_ids:
            budget.check()
            try:
                sequence_state = store.load_validated_sequence_state(conn, sequence_id)
                if isinstance(sequence_state, PreparedSequenceState):
                    raise DashboardReadError(
                        DashboardReadErrorKind.DATA_INTEGRITY,
                        "Scheduler dashboard lineage failed integrity validation.",
                    )
                materialized: MaterializedSequenceState = sequence_state
                rows = (
                    load_sequence_run_lineage_rows(conn, sequence_id)
                    if store.schema_supports_sequence_run_lineage(conn)
                    else []
                )
                lineage = (
                    load_sequence_run_lineage(
                        conn,
                        sequence_id,
                        definition=sequence_state.definition,
                    )
                    if rows
                    else project_historical_lineage_from_state(materialized)
                )
            except DashboardReadError:
                raise
            except SchedulerEngineError as exc:
                raise _map_scheduler_error(exc) from exc
            except (SequenceRunLineageValidationError, ValidationError, ValueError) as exc:
                raise DashboardReadError(
                    DashboardReadErrorKind.DATA_INTEGRITY,
                    "Scheduler dashboard lineage failed integrity validation.",
                ) from exc
            entries = sequence_state.definition.entries
            for phase in lineage.phase_executions:
                if phase.ordinal < 1 or phase.ordinal > len(entries):
                    raise DashboardReadError(
                        DashboardReadErrorKind.DATA_INTEGRITY,
                        "Scheduler dashboard lineage failed integrity validation.",
                    )
                entry = entries[phase.ordinal - 1]
                for attempt in phase.attempts:
                    if attempt.run_id in bindings:
                        raise DashboardReadError(
                            DashboardReadErrorKind.DATA_INTEGRITY,
                            "Scheduler dashboard lineage failed integrity validation.",
                        )
                    bindings[attempt.run_id] = SequenceBindingObservation(
                        sequence_id=sequence_id,
                        sequence_name=sequence_state.definition.name,
                        sequence_short_name=store.sequence_short_name(conn, sequence_id),
                        ordinal=phase.ordinal,
                        phase_name=entry.phase_name,
                        generation=attempt.generation,
                        attempt_kind=attempt.attempt_kind,
                        source_run_id=attempt.source_run_id,
                    )
        for run_id, state in states.items():
            context_binding = state.context.sequence
            if context_binding is None:
                continue
            binding = bindings.get(run_id)
            if (
                binding is None
                or binding.sequence_id != context_binding.sequence_id
                or binding.ordinal != context_binding.ordinal
            ):
                raise DashboardReadError(
                    DashboardReadErrorKind.DATA_INTEGRITY,
                    "Scheduler dashboard lineage failed integrity validation.",
                )
        return bindings

    def _load_attempts(
        self,
        conn: sqlite3.Connection,
        runs: list[RunObservation],
        budget: _QueryBudget,
    ) -> list[AttemptObservation]:
        if not runs:
            return []
        project_by_run = {run.run_id: run.project_name for run in runs}
        attempts: list[AttemptObservation] = []
        try:
            run_ids = tuple(project_by_run)
            for start in range(0, len(run_ids), 500):
                selected_ids = run_ids[start : start + 500]
                placeholders = ",".join("?" for _ in selected_ids)
                rows = conn.execute(
                    f"""
                    SELECT attempt.run_id, attempt.iteration, attempt.component,
                           attempt.status, attempt.launch_requested_at,
                           attempt.completed_at, attempt.ingested, effect.effect_kind,
                           effect.dispatch_id AS effect_dispatch_id,
                           effect.run_id AS effect_run_id
                    FROM scheduler_attempts AS attempt
                    LEFT JOIN scheduler_effects AS effect
                      ON effect.dispatch_id = attempt.dispatch_id
                    WHERE attempt.run_id IN ({placeholders})
                    ORDER BY attempt.run_id ASC,
                             COALESCE(attempt.launch_requested_at, attempt.created_at) ASC,
                             attempt.attempt_id ASC
                    """,
                    selected_ids,
                ).fetchall()
                for row in rows:
                    budget.check()
                    run_id = str(row["run_id"])
                    launch = row["launch_requested_at"]
                    completed = row["completed_at"]
                    ingested = int(row["ingested"])
                    effect_dispatch_id = row["effect_dispatch_id"]
                    if (
                        run_id not in project_by_run
                        or ingested not in {0, 1}
                        or (effect_dispatch_id is not None and str(row["effect_run_id"]) != run_id)
                    ):
                        raise ValueError("invalid attempt ownership")
                    attempts.append(
                        AttemptObservation(
                            run_id=run_id,
                            project_name=project_by_run[run_id],
                            component=str(row["component"]),
                            effect_kind=(
                                str(row["effect_kind"]) if row["effect_kind"] is not None else None
                            ),
                            iteration=int(row["iteration"]),
                            status=str(row["status"]),
                            launch_requested_at=(
                                coerce_utc_instant(str(launch)) if launch is not None else None
                            ),
                            completed_at=(
                                coerce_utc_instant(str(completed))
                                if completed is not None
                                else None
                            ),
                            ingested=bool(ingested),
                        )
                    )
        except (TypeError, ValueError) as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard attempt data failed integrity validation.",
            ) from exc
        return attempts

    @staticmethod
    def _contains(scope: DashboardReadScope, value: datetime) -> bool:
        return scope.from_utc <= value < scope.to_utc

    def _enforce_scope_bounds(
        self,
        scope: DashboardReadScope,
        runs: list[RunObservation],
        attempts: list[AttemptObservation],
    ) -> None:
        relevant_run_ids: set[str]
        contributing_attempts = 0
        try:
            if scope.operation in {
                DashboardReadOperation.SUMMARY,
                DashboardReadOperation.RUNS,
            }:
                launches, _ = first_execution_launches(tuple(runs), tuple(attempts))
                relevant_run_ids = {
                    run_id for run_id, launch in launches.items() if self._contains(scope, launch)
                }
                contributing_attempts = sum(
                    1
                    for attempt in attempts
                    if attempt.run_id in relevant_run_ids
                    and execution_role(attempt) is not None
                    and attempt.launch_requested_at is not None
                    and self._contains(scope, attempt.launch_requested_at)
                )
            elif scope.operation is DashboardReadOperation.ACTIVITY_SUBMISSIONS:
                relevant_run_ids = {
                    run.run_id for run in runs if self._contains(scope, run.submitted_at)
                }
            elif scope.operation is DashboardReadOperation.ACTIVITY_REVIEW_ROUNDS:
                contributors = [
                    attempt
                    for attempt in attempts
                    if execution_role(attempt) == "reviewer"
                    and attempt.launch_requested_at is not None
                    and self._contains(scope, attempt.launch_requested_at)
                ]
                relevant_run_ids = {attempt.run_id for attempt in contributors}
                contributing_attempts = len(contributors)
            else:
                contributors = []
                for attempt in attempts:
                    interval = resolved_process_interval(attempt)
                    if interval is None:
                        continue
                    launch, completed = interval
                    if launch < scope.to_utc and completed > scope.from_utc:
                        contributors.append(attempt)
                relevant_run_ids = {attempt.run_id for attempt in contributors}
                contributing_attempts = len(contributors)
        except ValueError as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard attempt data failed integrity validation.",
            ) from exc
        if (
            len(relevant_run_ids) > MAX_RELEVANT_RUNS
            or contributing_attempts > MAX_CONTRIBUTING_ATTEMPTS
        ):
            raise DashboardReadError(DashboardReadErrorKind.INVALID_ARGUMENT, _SCOPE_MESSAGE)

    def read_project_catalog(self, *, offset: int, limit: int) -> ProjectCatalogObservation:
        budget = _QueryBudget(
            seconds=self.query_budget_seconds,
            monotonic=self.monotonic,
            cancelled=self.cancelled,
            absolute_deadline=self.absolute_deadline,
        )
        counts: Counter[str] = Counter()
        try:
            store = self._open_store()
            if store is None:
                return ProjectCatalogObservation(items=(), total_count=0)
            with store.begin_read() as conn:
                conn.set_progress_handler(budget.should_interrupt, self.progress_steps)
                try:
                    cursor = conn.execute(
                        """
                        SELECT r.*,
                               reservation.repository_root AS active_reservation_root
                        FROM scheduler_runs AS r
                        LEFT JOIN scheduler_repository_reservations AS reservation
                          ON reservation.run_id = r.run_id AND reservation.status = 'active'
                        ORDER BY r.created_at ASC, r.run_id ASC
                        """
                    )
                    while True:
                        rows = cursor.fetchmany(256)
                        if not rows:
                            break
                        for row in rows:
                            budget.check(_CATALOG_MESSAGE)
                            state = self._validate_run_row(store, cast(sqlite3.Row, row))
                            counts[state.context.project_name] += 1
                    budget.check(_CATALOG_MESSAGE)
                finally:
                    conn.set_progress_handler(None, 0)
        except DashboardReadError:
            raise
        except SchedulerEngineError as exc:
            raise _map_scheduler_error(exc) from exc
        except sqlite3.OperationalError as exc:
            if budget.expired or "interrupted" in str(exc).lower():
                raise DashboardReadError(
                    DashboardReadErrorKind.INVALID_ARGUMENT,
                    _CATALOG_MESSAGE,
                ) from exc
            raise DashboardReadError(
                DashboardReadErrorKind.INTERNAL,
                "Unable to read scheduler dashboard data.",
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            ) from exc
        ordered = sorted(counts.items())
        selected = ordered[offset : offset + limit]
        return ProjectCatalogObservation(
            items=tuple(
                ProjectCountObservation(project_name=name, run_count=count)
                for name, count in selected
            ),
            total_count=len(ordered),
        )

    def read_observations(self, scope: DashboardReadScope) -> DashboardObservationSet:
        budget = _QueryBudget(
            seconds=self.query_budget_seconds,
            monotonic=self.monotonic,
            cancelled=self.cancelled,
            absolute_deadline=self.absolute_deadline,
        )
        try:
            store = self._open_store()
            if store is None:
                return DashboardObservationSet(runs=(), attempts=())
            with store.begin_read() as conn:
                conn.set_progress_handler(budget.should_interrupt, self.progress_steps)
                try:
                    runs, states = self._load_runs(
                        store,
                        conn,
                        project_name=scope.project_name,
                        budget=budget,
                    )
                    attempts = self._load_attempts(conn, runs, budget)
                    self._enforce_scope_bounds(scope, runs, attempts)
                    bindings = self._lineage_bindings(
                        store,
                        conn,
                        states,
                        budget,
                    )
                    runs = [
                        RunObservation(
                            run_id=run.run_id,
                            short_name=run.short_name,
                            project_name=run.project_name,
                            implementer_provider=run.implementer_provider,
                            state=run.state,
                            submitted_at=run.submitted_at,
                            sequence_binding=bindings.get(run.run_id),
                        )
                        for run in runs
                    ]
                    budget.check()
                finally:
                    conn.set_progress_handler(None, 0)
        except DashboardReadError:
            raise
        except SchedulerEngineError as exc:
            raise _map_scheduler_error(exc) from exc
        except sqlite3.OperationalError as exc:
            if budget.expired or "interrupted" in str(exc).lower():
                raise DashboardReadError(
                    DashboardReadErrorKind.INVALID_ARGUMENT,
                    _SCOPE_MESSAGE,
                ) from exc
            raise DashboardReadError(
                DashboardReadErrorKind.INTERNAL,
                "Unable to read scheduler dashboard data.",
            ) from exc
        except sqlite3.DatabaseError as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.DATA_INTEGRITY,
                "Scheduler dashboard data failed integrity validation.",
            ) from exc
        return DashboardObservationSet(runs=tuple(runs), attempts=tuple(attempts))
