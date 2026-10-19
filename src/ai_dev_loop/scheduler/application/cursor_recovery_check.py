"""Read-only CLI service for Cursor recovery evidence inspection (Phase 23.1)."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    CursorRecoveryAnalysisResult,
    CursorRecoveryCheckReceipt,
    analyze_cursor_recovery_evidence,
)
from ai_dev_loop.scheduler.application.cursor_runtime_policy import (
    CURSOR_CLI_RETIRED_SUMMARY,
    context_uses_cursor_cli,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
    ReadOnlyProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def render_cursor_recovery_check_output(receipt: CursorRecoveryCheckReceipt, *, output: str) -> str:
    if output == "json":
        return receipt.model_dump_json(indent=2) + "\n"
    parts = [
        f"Cursor recovery check: {receipt.run_id}",
        f"evidence={receipt.evidence_status}",
        f"reason={receipt.reason_code}",
    ]
    if receipt.turn_kind is not None:
        parts.append(f"turn={receipt.turn_kind}")
    parts.append(receipt.safe_summary)
    parts.append(
        "recovery_supported=yes" if receipt.recovery_supported else "recovery_supported=no"
    )
    return "; ".join(parts) + "\n"


def scheduler_cursor_recovery_check(
    run_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    event_page_size: int | None = None,
) -> CursorRecoveryCheckReceipt:
    resolved_db = db_path or default_engine_db_path()
    store = SqliteSchedulerStore.open_readonly(resolved_db)
    retired = _retired_cursor_cli_analysis(store, run_id)
    if retired is not None:
        return retired.receipt
    artifacts = ReadOnlyProtectedArtifactStore(artifact_root or default_artifact_root())
    try:
        if event_page_size is None:
            analysis = analyze_cursor_recovery_evidence(store, artifacts, run_id)
        else:
            analysis = analyze_cursor_recovery_evidence(
                store,
                artifacts,
                run_id,
                event_page_size=event_page_size,
            )
    except SchedulerEngineError:
        raise
    except Exception as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.INTERNAL,
            "cursor recovery check failed",
        ) from exc
    return analysis.receipt


def analyze_cursor_recovery_for_inspection(
    run_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    event_page_size: int | None = None,
) -> CursorRecoveryAnalysisResult:
    """Production analysis entry returning receipt plus optional internal bundle."""

    resolved_db = db_path or default_engine_db_path()
    store = SqliteSchedulerStore.open_readonly(resolved_db)
    retired = _retired_cursor_cli_analysis(store, run_id)
    if retired is not None:
        return retired
    artifacts = ReadOnlyProtectedArtifactStore(artifact_root or default_artifact_root())
    if event_page_size is None:
        return analyze_cursor_recovery_evidence(store, artifacts, run_id)
    return analyze_cursor_recovery_evidence(
        store,
        artifacts,
        run_id,
        event_page_size=event_page_size,
    )


def _retired_cursor_cli_analysis(
    store: SqliteSchedulerStore,
    run_id: str,
) -> CursorRecoveryAnalysisResult | None:
    """Classify the frozen runtime before inspecting provider recovery artifacts."""

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    if not context_uses_cursor_cli(state.context):
        return None
    sequence = state.context.sequence
    return CursorRecoveryAnalysisResult(
        receipt=CursorRecoveryCheckReceipt(
            run_id=run_id,
            evidence_status="ineligible",
            recovery_supported=False,
            turn_kind=None,
            reason_code="ineligible_cursor_cli_runtime_retired",
            safe_summary=CURSOR_CLI_RETIRED_SUMMARY,
            sequence_id=None if sequence is None else sequence.sequence_id,
            ordinal=None if sequence is None else sequence.ordinal,
        )
    )
