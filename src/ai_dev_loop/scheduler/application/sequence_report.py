"""Deterministic safe completion reports for awaiting_finalization sequences."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from ai_dev_loop.scheduler.application.sequence_checkpoint_evidence import (
    SequenceCheckpointEvidenceError,
    authenticate_checkpoint_evidence,
)
from ai_dev_loop.scheduler.application.sequence_lineage_projection import (
    load_phase_lineage_projections,
)
from ai_dev_loop.scheduler.domain.sequence import (
    AwaitingFinalizationSequenceState,
    SequenceCompletionReport,
    SequencePhaseReportEntry,
)
from ai_dev_loop.scheduler.domain.state import CompletedState, CompletedWithResidualRiskState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

SEQUENCE_COMPLETION_REPORT_ARTIFACT = "reports/completion-v1.json"

_publication_step_hook: Callable[[str], None] | None = None


def set_completion_report_publication_step_hook(
    hook: Callable[[str], None] | None,
) -> None:
    """Test hook for deterministic publication interruption points."""
    global _publication_step_hook
    _publication_step_hook = hook


def _publication_step(name: str) -> None:
    if _publication_step_hook is not None:
        _publication_step_hook(name)


def _prefix(value: str | None, length: int = 12) -> str | None:
    if value is None:
        return None
    return value[:length]


def build_completion_report(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    state: AwaitingFinalizationSequenceState,
    *,
    conn: sqlite3.Connection | None = None,
) -> SequenceCompletionReport:
    if conn is None:
        with store.begin_read() as read_conn:
            return _build_completion_report_inner(store, artifacts, state, read_conn)
    return _build_completion_report_inner(store, artifacts, state, conn)


def _build_completion_report_inner(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    state: AwaitingFinalizationSequenceState,
    conn: sqlite3.Connection,
) -> SequenceCompletionReport:
    definition = state.definition
    residual_names = tuple(
        definition.entries[ordinal - 1].phase_name for ordinal in state.residual_risk_ordinals
    )
    phases: list[SequencePhaseReportEntry] = []
    base_head: str | None = None
    final_patch: str | None = None
    total_phases = len(definition.entries)
    lineage_by_ordinal = load_phase_lineage_projections(
        store,
        conn,
        sequence_id=state.sequence_id,
        definition=definition,
        sequence_state=state,
    )
    for materialized in state.materialized_entries:
        entry = definition.entries[materialized.ordinal - 1]
        run_id = materialized.run_id
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        accepted: Literal["completed", "completed_with_residual_risk"] | None = None
        review_sha: str | None = None
        if isinstance(run_state, (CompletedState, CompletedWithResidualRiskState)):
            if isinstance(run_state, CompletedWithResidualRiskState):
                accepted = "completed_with_residual_risk"
            else:
                accepted = "completed"
            review_sha = run_state.codex.latest_review_result_sha256
        checkpoint_commit: str | None = None
        checkpoint_parent: str | None = None
        checkpoint_tree: str | None = None
        checkpoint_intent: str | None = None
        checkpoint_trusted_tree: str | None = None
        if materialized.ordinal < total_phases:
            authenticated = authenticate_checkpoint_evidence(
                artifacts,
                run_id=run_id,
                materialized=materialized,
                total_phases=total_phases,
            )
            if authenticated is None:
                raise SequenceCheckpointEvidenceError(
                    f"checkpoint evidence required for ordinal {materialized.ordinal}"
                )
            checkpoint_commit = authenticated.commit_sha256
            checkpoint_parent = authenticated.parent_head
            checkpoint_tree = authenticated.tree_sha256
            checkpoint_intent = authenticated.intent_sha256
            checkpoint_trusted_tree = authenticated.trusted_tree_sha256
        if materialized.ordinal == 1 and checkpoint_parent is None:
            admission_path = artifacts.run_root(run_id) / "git/admission/baseline-head.txt"
            if admission_path.is_file():
                base_head = admission_path.read_text(encoding="utf-8").strip()
        if materialized.ordinal == total_phases and isinstance(
            run_state, (CompletedState, CompletedWithResidualRiskState)
        ):
            final_patch = run_state.cursor.staged_patch_sha256
        lineage = lineage_by_ordinal.get(materialized.ordinal)
        accepted_prefix = lineage.accepted_run_id_prefix if lineage is not None else run_id[:8]
        phases.append(
            SequencePhaseReportEntry(
                ordinal=materialized.ordinal,
                phase_name=entry.phase_name,
                run_id=run_id,
                run_id_prefix=run_id[:8],
                accepted_run_id_prefix=accepted_prefix,
                accepted_attempt_kind=(
                    lineage.accepted_attempt_kind if lineage is not None else None
                ),
                attempt_count=lineage.attempt_count if lineage is not None else 1,
                attempt_kind_labels=(lineage.attempt_kind_labels if lineage is not None else ()),
                accepted_outcome=accepted,
                residual_risk=materialized.ordinal in state.residual_risk_ordinals,
                review_result_sha256=review_sha,
                review_result_sha256_prefix=_prefix(review_sha),
                checkpoint_commit_sha256=checkpoint_commit,
                checkpoint_commit_sha256_prefix=_prefix(checkpoint_commit),
                checkpoint_parent_sha256=checkpoint_parent,
                checkpoint_parent_sha256_prefix=_prefix(checkpoint_parent),
                checkpoint_tree_sha256=checkpoint_tree,
                checkpoint_tree_sha256_prefix=_prefix(checkpoint_tree),
                checkpoint_intent_sha256=checkpoint_intent,
                checkpoint_trusted_tree_sha256=checkpoint_trusted_tree,
            )
        )
    if base_head is None and phases:
        base_head = phases[0].checkpoint_parent_sha256
    final_run_state, _, _ = store.load_validated_snapshot(conn, state.final_run_id)
    if not isinstance(final_run_state, (CompletedState, CompletedWithResidualRiskState)):
        raise ValueError("final run must be in an accepted terminal state")
    if final_run_state.kind != state.final_outcome:
        raise ValueError("final_outcome disagrees with authoritative final run state")
    if final_run_state.codex.latest_review_result_sha256 is None:
        raise ValueError("final run is missing review result hash")
    if final_patch is None:
        final_patch = final_run_state.cursor.staged_patch_sha256
    if base_head is None:
        raise ValueError("completion report requires authenticated base head evidence")
    return SequenceCompletionReport(
        schema_version=1,
        sequence_id=state.sequence_id,
        sequence_name=definition.name,
        base_head_sha256=base_head,
        base_head_sha256_prefix=_prefix(base_head) or "unknown",
        finalized_at=state.finalized_at,
        final_run_id=state.final_run_id,
        final_run_id_prefix=state.final_run_id[:8],
        final_outcome=state.final_outcome,
        final_staged_patch_sha256=final_patch,
        final_staged_patch_sha256_prefix=_prefix(final_patch),
        residual_risk_ordinals=state.residual_risk_ordinals,
        residual_risk_phase_names=residual_names,
        phases=tuple(phases),
    )


def persist_completion_report_if_absent(
    artifacts: ProtectedArtifactStore,
    report: SequenceCompletionReport,
) -> str:
    text = report.model_dump(mode="json", exclude_none=True)
    import json

    serialized = json.dumps(text, indent=2, sort_keys=True) + "\n"
    stored = artifacts.write_sequence_text_or_verify(
        report.sequence_id,
        SEQUENCE_COMPLETION_REPORT_ARTIFACT,
        serialized,
        max_bytes=131_072,
    )
    return stored.sha256


def ensure_completion_report(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    state: AwaitingFinalizationSequenceState,
    *,
    conn: sqlite3.Connection | None = None,
) -> SequenceCompletionReport:
    if state.final_outcome not in {"completed", "completed_with_residual_risk"}:
        raise ValueError("completion report requires accepted final outcome")
    report = build_completion_report(store, artifacts, state, conn=conn)
    persist_completion_report_if_absent(artifacts, report)
    return report


def reconcile_completion_report_publication(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    state: AwaitingFinalizationSequenceState,
    *,
    conn: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> AwaitingFinalizationSequenceState:
    """Write and durably record the completion report for awaiting_finalization sequences."""
    from datetime import UTC
    from datetime import datetime as dt

    if state.completion_report_sha256 is not None:
        return state
    _publication_step("before_report_build")
    report = build_completion_report(store, artifacts, state, conn=conn)
    _publication_step("before_report_write")
    report_sha = persist_completion_report_if_absent(artifacts, report)
    _publication_step("after_report_write")
    now_value = now or dt.now(tz=UTC)
    now_text = now_value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    def _record_publication(active_conn: sqlite3.Connection) -> AwaitingFinalizationSequenceState:
        current = store.load_validated_sequence_state(active_conn, state.sequence_id)
        if not isinstance(current, AwaitingFinalizationSequenceState):
            return current  # type: ignore[return-value]
        if current.completion_report_sha256 == report_sha:
            return current
        _publication_step("before_db_record")
        published = current.model_copy(
            update={
                "version": current.version + 1,
                "updated_at": now_text,
                "completion_report_sha256": report_sha,
            }
        )
        if not store.compare_and_swap_sequence_state(
            active_conn,
            sequence_id=state.sequence_id,
            expected_version=current.version,
            new_state=published,
            now=now_value,
        ):
            refreshed = store.load_validated_sequence_state(active_conn, state.sequence_id)
            if isinstance(refreshed, AwaitingFinalizationSequenceState):
                return refreshed
            raise ValueError("completion report publication CAS lost")
        return published

    if conn is not None:
        return _record_publication(conn)
    with store.begin_immediate() as write_conn:
        return _record_publication(write_conn)
