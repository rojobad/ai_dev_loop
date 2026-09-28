"""Public sequence inspection projections for the Integration API."""

# mypy: disable-error-code=call-arg

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ai_dev_loop.integration_api.artifact_reader import (
    FrozenArtifactReadError,
    SequenceCompletionReportMissingError,
    load_integration_sequence_completion_report,
)
from ai_dev_loop.integration_api.models import (
    IntegrationPhaseCheckpointSummary,
    IntegrationPhaseFrozenInputs,
    IntegrationReportAvailability,
    IntegrationSafeNextAction,
    IntegrationSequenceAggregateCounts,
    IntegrationSequenceInspectData,
    IntegrationSequenceListItem,
    IntegrationSequencePhaseRunItem,
    IntegrationSequencePhaseRunListData,
    IntegrationSequencePhaseSummary,
)
from ai_dev_loop.integration_api.run_projection import collection_page
from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceAggregateCounts,
    SequenceStatusResult,
    scheduler_status_projection_from_state,
)
from ai_dev_loop.scheduler.domain.checkpoint import SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
from ai_dev_loop.scheduler.domain.sequence import (
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    FrozenSequenceEntry,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    MaterializedSequenceState,
    SequencePhaseExecution,
    SequenceRunAttempt,
    SequenceRunLineageValidationError,
    project_historical_lineage_from_state,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    readonly_confined_run_artifact_root,
)
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    load_sequence_run_lineage,
    load_sequence_run_lineage_rows,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

INLINE_PHASE_RUNS_MAX = 100


def _integration_safe_next_action(
    *,
    sequence_id: str,
    internal: SafeNextAction,
    current_run_id: str | None,
    cursor_wait_until: str | None,
) -> IntegrationSafeNextAction:
    wait_until = None
    if internal.kind is SafeNextActionKind.WAIT_UNTIL:
        wait_until = cursor_wait_until
    return IntegrationSafeNextAction(
        kind=internal.kind.value,
        run_id=current_run_id,
        sequence_id=sequence_id,
        wait_until=wait_until,
    )


def _aggregate_counts_wire(counts: SequenceAggregateCounts) -> IntegrationSequenceAggregateCounts:
    return IntegrationSequenceAggregateCounts(
        planned=counts.planned,
        materialized=counts.materialized,
        accepted=counts.accepted,
        residual_risk=counts.residual_risk,
        checkpointed=counts.checkpointed,
        cancelled=counts.cancelled,
        remaining=counts.remaining,
        attempts=counts.attempts,
    )


def _readonly_checkpoint_prefix(
    artifact_root: Path,
    run_id: str,
) -> str | None:
    try:
        root = readonly_confined_run_artifact_root(artifact_root, run_id)
        result_path = root / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
        if not result_path.is_file() or result_path.is_symlink():
            return None
        result = json.loads(result_path.read_bytes())
        commit_sha = str(result.get("commit_sha256", ""))
        if commit_sha:
            return commit_sha[:12]
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    return None


def _report_availability(
    *,
    artifact_root: Path,
    status: SequenceStatusResult,
    sequence_state: AwaitingFinalizationSequenceState,
) -> IntegrationReportAvailability:
    if status.state_kind != AWAITING_FINALIZATION_SEQUENCE_STATE_KIND:
        return IntegrationReportAvailability(available=False, reason="not_yet_produced")
    try:
        validated = load_integration_sequence_completion_report(
            artifact_root=artifact_root,
            sequence_id=status.sequence_id,
            expected_sha256=sequence_state.completion_report_sha256,
        )
    except SequenceCompletionReportMissingError:
        return IntegrationReportAvailability(available=False, reason="publication_pending")
    except FrozenArtifactReadError:
        return IntegrationReportAvailability(available=False, reason=None)
    except OSError:
        return IntegrationReportAvailability(available=False, reason=None)
    return IntegrationReportAvailability(
        available=True,
        sha256_prefix=validated.sha256[:16],
    )


def _run_state_kind(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> str:
    state, _, _ = store.load_validated_snapshot(conn, run_id)
    return state.kind


def _phase_run_item(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    attempt: SequenceRunAttempt,
) -> IntegrationSequencePhaseRunItem:
    return IntegrationSequencePhaseRunItem(
        run_id=attempt.run_id,
        generation=attempt.generation,
        source_run_id=attempt.source_run_id,
        attempt_kind=attempt.attempt_kind,
        materialized_at=attempt.materialized_at,
        resolved_at=attempt.resolved_at,
        state=_run_state_kind(store, conn, attempt.run_id),
        terminal_outcome=attempt.terminal_outcome,
    )


def paginate_runs(
    attempts: tuple[SequenceRunAttempt, ...],
    *,
    offset: int,
    limit: int,
) -> tuple[tuple[SequenceRunAttempt, ...], bool, int | None]:
    total = len(attempts)
    if offset < 0 or limit < 1:
        raise ValueError("invalid pagination bounds")
    slice_end = offset + limit
    selected = attempts[offset:slice_end]
    has_more = slice_end < total
    next_offset = slice_end if has_more else None
    return selected, has_more, next_offset


def _phase_execution_for_ordinal(
    lineage_phases: dict[int, SequencePhaseExecution],
    ordinal: int,
) -> SequencePhaseExecution | None:
    return lineage_phases.get(ordinal)


def _load_lineage_by_ordinal(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    status: SequenceStatusResult,
) -> dict[int, SequencePhaseExecution]:
    state = store.load_validated_sequence_state(
        conn,
        status.sequence_id,
        validate_lineage=False,
    )
    if isinstance(state, PreparedSequenceState):
        return {}
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
        return {}
    materialized_state: MaterializedSequenceState = state
    if not store.schema_supports_sequence_run_lineage(conn):
        lineage = project_historical_lineage_from_state(materialized_state)
        return {phase.ordinal: phase for phase in lineage.phase_executions}

    rows = load_sequence_run_lineage_rows(conn, status.sequence_id)
    if not rows:
        lineage = project_historical_lineage_from_state(materialized_state)
        return {phase.ordinal: phase for phase in lineage.phase_executions}

    try:
        lineage = load_sequence_run_lineage(
            conn,
            status.sequence_id,
            definition=state.definition,
        )
    except SequenceRunLineageValidationError as exc:
        raise SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, str(exc)) from exc
    return {phase.ordinal: phase for phase in lineage.phase_executions}


def build_sequence_list_item(status: SequenceStatusResult) -> IntegrationSequenceListItem:
    return IntegrationSequenceListItem(
        sequence_id=status.sequence_id,
        name=status.name,
        project_name=status.project_name,
        repository_root=status.repository_root,
        state=status.state_kind,
        prepared_at=status.prepared_at,
        updated_at=status.updated_at,
    )


def build_sequence_inspect_data(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    status_result: SequenceStatusResult,
    *,
    artifact_root: Path,
) -> IntegrationSequenceInspectData:
    lineage_by_ordinal = _load_lineage_by_ordinal(store, conn, status_result)
    phases: list[IntegrationSequencePhaseSummary] = []
    for entry_summary, definition_entry in zip(
        status_result.entries,
        _definition_entries(store, conn, status_result.sequence_id),
        strict=True,
    ):
        phase_exec = lineage_by_ordinal.get(definition_entry.ordinal)
        attempts: tuple[SequenceRunAttempt, ...] = ()
        if phase_exec is not None:
            attempts = phase_exec.attempts
        inline, has_more, next_off = paginate_runs(
            attempts,
            offset=0,
            limit=INLINE_PHASE_RUNS_MAX,
        )
        checkpoint_prefix = entry_summary.checkpoint_commit_sha256_prefix
        if checkpoint_prefix is None and entry_summary.materialized:
            materialized_run = _materialized_run_id(
                store,
                conn,
                status_result.sequence_id,
                definition_entry.ordinal,
            )
            if materialized_run is not None and definition_entry.commit_message is not None:
                checkpoint_prefix = _readonly_checkpoint_prefix(artifact_root, materialized_run)
        phases.append(
            IntegrationSequencePhaseSummary(
                ordinal=definition_entry.ordinal,
                name=definition_entry.phase_name,
                initial_planned_run_id=definition_entry.planned_run_id,
                materialized=entry_summary.materialized,
                current_run_id=phase_exec.current_run_id if phase_exec else None,
                accepted_run_id=phase_exec.accepted_run_id if phase_exec else None,
                cancelled=entry_summary.cancelled,
                accepted_outcome=entry_summary.accepted_outcome,
                residual_risk=entry_summary.residual_risk,
                checkpoint=IntegrationPhaseCheckpointSummary(
                    available=checkpoint_prefix is not None,
                    commit_sha256_prefix=checkpoint_prefix,
                ),
                frozen_inputs=IntegrationPhaseFrozenInputs(
                    plan_available=True,
                    prompt_available=True,
                ),
                runs=tuple(_phase_run_item(store, conn, attempt) for attempt in inline),
                runs_has_more=has_more,
                runs_next_offset=next_off,
                run_count=len(attempts),
            )
        )
    counts = status_result.aggregate_counts
    assert counts is not None
    checkpointed = sum(1 for phase in phases if phase.checkpoint.available)
    counts_wire = _aggregate_counts_wire(
        SequenceAggregateCounts(
            planned=counts.planned,
            materialized=counts.materialized,
            accepted=counts.accepted,
            residual_risk=counts.residual_risk,
            checkpointed=checkpointed,
            cancelled=counts.cancelled,
            remaining=counts.remaining,
            attempts=counts.attempts,
        )
    )
    cursor_wait_until: str | None = None
    if status_result.current_run_id is not None:
        run_state, _, _ = store.load_validated_snapshot(conn, status_result.current_run_id)
        cursor_wait_until = scheduler_status_projection_from_state(run_state)["cursor_wait_until"]
    sequence_state = store.load_validated_sequence_state(conn, status_result.sequence_id)
    report_summary = IntegrationReportAvailability(available=False, reason="not_yet_produced")
    if isinstance(sequence_state, AwaitingFinalizationSequenceState):
        report_summary = _report_availability(
            artifact_root=artifact_root,
            status=status_result,
            sequence_state=sequence_state,
        )
    block_reason = status_result.block_reason_kind or status_result.abort_reason
    return IntegrationSequenceInspectData(
        sequence_id=status_result.sequence_id,
        name=status_result.name,
        project_name=status_result.project_name,
        repository_root=status_result.repository_root,
        state=status_result.state_kind,
        prepared_at=status_result.prepared_at,
        started_at=status_result.started_at,
        updated_at=status_result.updated_at,
        finalized_at=status_result.finalized_at,
        current_phase_ordinal=status_result.current_ordinal,
        current_run_id=status_result.current_run_id,
        current_run_state=status_result.current_run_state_kind,
        aggregate_counts=counts_wire,
        residual_risk=status_result.residual_risk,
        residual_risk_ordinals=status_result.residual_risk_ordinals,
        block_reason=block_reason,
        safe_next_action=_integration_safe_next_action(
            sequence_id=status_result.sequence_id,
            internal=status_result.safe_next_action,
            current_run_id=status_result.current_run_id,
            cursor_wait_until=cursor_wait_until,
        ),
        report=report_summary,
        phases=tuple(phases),
    )


def _definition_entries(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    sequence_id: str,
) -> tuple[FrozenSequenceEntry, ...]:
    state = store.load_validated_sequence_state(conn, sequence_id)
    return state.definition.entries


def _materialized_run_id(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    sequence_id: str,
    ordinal: int,
) -> str | None:
    state = store.load_validated_sequence_state(conn, sequence_id)
    if not hasattr(state, "materialized_entries"):
        return None
    for entry in state.materialized_entries:
        if entry.ordinal == ordinal:
            return entry.run_id
    return None


def build_sequence_phase_runs_data(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    status_result: SequenceStatusResult,
    *,
    ordinal: int,
    offset: int,
    limit: int,
) -> IntegrationSequencePhaseRunListData:
    definition = store.load_validated_sequence_state(conn, status_result.sequence_id).definition
    if ordinal < 1 or ordinal > len(definition.entries):
        raise ValueError("ordinal out of range")
    lineage_by_ordinal = _load_lineage_by_ordinal(store, conn, status_result)
    phase_exec = lineage_by_ordinal.get(ordinal)
    attempts: tuple[SequenceRunAttempt, ...] = ()
    if phase_exec is not None:
        attempts = phase_exec.attempts
    selected, has_more, next_off = paginate_runs(attempts, offset=offset, limit=limit)
    return IntegrationSequencePhaseRunListData(
        sequence_id=status_result.sequence_id,
        ordinal=ordinal,
        items=tuple(_phase_run_item(store, conn, attempt) for attempt in selected),
        page=collection_page(offset, limit, has_more),
        run_count=len(attempts),
    )
