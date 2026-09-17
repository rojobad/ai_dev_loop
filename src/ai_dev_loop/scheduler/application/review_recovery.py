"""Blocked-run review recovery evidence and successor materialization."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.codex_failure import is_integrity_review_block_kind
from ai_dev_loop.scheduler.application.attempt_envelope import read_bounded_bytes
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_outcome,
    load_validated_review_result,
    validate_codex_review_outcome_integrity,
    verify_codex_invocation_evidence,
    verify_pre_execution_codex_guards,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    load_authenticated_cursor_outcome,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    ReviewCheckpointVerificationError,
    verify_review_retry_repository_checkpoint,
)
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    MAX_CODEX_EVENTS_ARTIFACT_BYTES,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_final_rel,
)
from ai_dev_loop.scheduler.domain.events import (
    CODEX_REVIEW_COMPLETED_EVENT_KIND,
    CODEX_REVIEWER_BOUND_EVENT_KIND,
    CURSOR_CHAT_CREATED_EVENT_KIND,
    CURSOR_TURN_COMPLETED_EVENT_KIND,
    STAGING_COMPLETED_EVENT_KIND,
    CodexReviewerBoundEvent,
    CursorChatCreatedEvent,
    CursorTurnCompletedEvent,
    SchedulerEvent,
    StagingCompletedEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    AwaitingCodexReviewState,
    BlockedState,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
    FreshCodexReviewerBinding,
    ReviewRecoveryLineage,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_CONFIG_BYTES,
    MAX_PLAN_BYTES,
    MAX_PROMPT_BYTES,
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import generate_run_id

MAX_RECOVERY_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_CURSOR_FINAL_BYTES = 8 * 1024 * 1024
MAX_CURSOR_FINGERPRINT_BYTES = 1 * 1024 * 1024


@dataclass(frozen=True)
class RecoveryCopyManifestEntry:
    relative_path: str
    expected_sha256: str


@dataclass(frozen=True)
class BlockedReviewRecoveryEvidence:
    context: SubmittedRunContext
    checkpoint: AdmittedRunCheckpoint
    cursor: CursorWorkflowCheckpoint
    codex: CodexWorkflowCheckpoint
    block_reason_kind: str
    failed_attempt_id: str
    bootstrap_attempt_id: str
    recovery_key: str
    copy_manifest: tuple[RecoveryCopyManifestEntry, ...]


def _recovery_key(block_reason_kind: str, failed_attempt_id: str) -> str:
    return f"{block_reason_kind}:{failed_attempt_id}"


def _verify_event_row(row: object) -> None:
    payload_text = str(row["event_payload"])  # type: ignore[index]
    expected = str(row["event_payload_sha256"])  # type: ignore[index]
    if payload_sha256(payload_text) != expected:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "ledger event payload digest mismatch",
        )


def _parse_event_row(row: object) -> SchedulerEvent:
    _verify_event_row(row)
    payload = json.loads(str(row["event_payload"]))  # type: ignore[index]
    return parse_scheduler_event(payload)


def _manifest_entry(
    artifacts: ProtectedArtifactStore,
    run_id: str,
    relative_path: str,
    expected_sha256: str,
    *,
    max_bytes: int = MAX_RECOVERY_ARTIFACT_BYTES,
) -> RecoveryCopyManifestEntry:
    root = artifacts.run_root(run_id)
    try:
        path = resolve_run_relative_path(root, relative_path)
    except ValueError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"recovery artifact path is unsafe: {exc}",
        ) from exc
    if path.is_file():
        if path.is_symlink():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery artifact must not be a symlink",
            )
        if path.stat().st_size > max_bytes:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery artifact exceeds size bound",
            )
    try:
        artifacts.read_verified_bytes(
            run_id,
            relative_path,
            expected_sha256=expected_sha256,
        )
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    return RecoveryCopyManifestEntry(
        relative_path=relative_path,
        expected_sha256=expected_sha256,
    )


def _verify_bounded_artifact_digest(
    artifacts: ProtectedArtifactStore,
    run_id: str,
    relative_path: str,
    expected_sha256: str,
    *,
    max_bytes: int,
) -> None:
    root = artifacts.run_root(run_id)
    try:
        path = resolve_run_relative_path(root, relative_path)
    except ValueError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"recovery artifact path is unsafe: {exc}",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery artifact missing",
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery artifact missing",
        ) from exc
    if size > max_bytes:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery artifact exceeds size bound",
        )
    data = read_bounded_bytes(path, max_bytes + 1)
    if len(data) > max_bytes:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery artifact exceeds size bound",
        )
    digest = hashlib.sha256(data).hexdigest()
    if digest != expected_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery artifact digest mismatch",
        )


def _require_cursor_fingerprint_artifact(
    artifacts: ProtectedArtifactStore,
    run_id: str,
    relative_path: str,
    expected_aggregate_sha256: str,
) -> RecoveryCopyManifestEntry:
    root = artifacts.run_root(run_id)
    try:
        path = resolve_run_relative_path(root, relative_path)
    except ValueError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"cursor output fingerprint path is unsafe: {exc}",
        ) from exc
    if not path.is_file() or path.is_symlink():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint artifact missing",
        )
    if path.stat().st_size > MAX_CURSOR_FINGERPRINT_BYTES:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint artifact exceeds size bound",
        )
    raw = read_bounded_bytes(path, MAX_CURSOR_FINGERPRINT_BYTES + 1)
    if len(raw) > MAX_CURSOR_FINGERPRINT_BYTES:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint artifact exceeds size bound",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint artifact is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint artifact must be a JSON object",
        )
    if str(payload.get("aggregate_sha256", "")) != expected_aggregate_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor output fingerprint aggregate hash mismatch",
        )
    digest = hashlib.sha256(raw).hexdigest()
    return RecoveryCopyManifestEntry(relative_path=relative_path, expected_sha256=digest)


def _authenticate_frozen_context_artifacts(
    context: SubmittedRunContext,
    artifacts: ProtectedArtifactStore,
    run_id: str,
) -> list[RecoveryCopyManifestEntry]:
    entries: list[RecoveryCopyManifestEntry] = []
    bindings = (
        (context.plan_prompt.plan_artifact_path, context.plan_prompt.plan_sha256, MAX_PLAN_BYTES),
        (
            context.plan_prompt.prompt_artifact_path,
            context.plan_prompt.prompt_sha256,
            MAX_PROMPT_BYTES,
        ),
        (
            context.effective_config.effective_config_artifact_path,
            context.effective_config.effective_config_sha256,
            MAX_CONFIG_BYTES,
        ),
        (
            context.effective_config.source_config_artifact_path,
            context.effective_config.source_config_sha256,
            MAX_CONFIG_BYTES,
        ),
    )
    for relative_path, expected_sha256, max_bytes in bindings:
        root = artifacts.run_root(run_id)
        try:
            path = resolve_run_relative_path(root, relative_path)
        except ValueError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"frozen context artifact path is unsafe: {exc}",
            ) from exc
        if not path.is_file() or path.is_symlink():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "frozen context artifact missing",
            )
        if path.stat().st_size > max_bytes:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "frozen context artifact exceeds size bound",
            )
        entries.append(_manifest_entry(artifacts, run_id, relative_path, expected_sha256))
    if isinstance(context.codex, FreshCodexReviewerBinding):
        entries.append(
            _manifest_entry(
                artifacts,
                run_id,
                context.codex.binding_artifact_path,
                context.codex.binding_sha256,
                max_bytes=MAX_CONFIG_BYTES,
            )
        )
    return entries


def _authenticate_codex_attempt(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    source_run_id: str,
    attempt: object,
) -> tuple[dict[str, object], str]:
    attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
    dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
    unit_identity = str(attempt["unit_identity"])  # type: ignore[index]
    launch_nonce = str(attempt["launch_nonce"])  # type: ignore[index]
    launch_intent_sha256 = str(attempt["launch_intent_sha256"])  # type: ignore[index]
    envelope_sha = attempt["completion_envelope_sha256"]  # type: ignore[index]
    exit_code = attempt["exit_code"]  # type: ignore[index]
    observed_exit = int(exit_code) if exit_code is not None else None
    run_root = artifacts.run_root(source_run_id)
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "codex attempt dispatch record missing",
        )
    effect_kind = str(dispatch["effect_kind"])
    try:
        invocation = verify_codex_invocation_evidence(
            run_root,
            attempt_id=attempt_id,
            run_id=source_run_id,
            dispatch_id=dispatch_id,
            unit_identity=unit_identity,
            launch_nonce=launch_nonce,
            launch_intent_sha256=launch_intent_sha256,
            effect_kind=effect_kind,
        )
        verify_pre_execution_codex_guards(run_root, invocation, run_id=source_run_id)
        outcome = load_authenticated_codex_outcome(
            run_root,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=str(attempt["result_artifact_path"]),  # type: ignore[index]
            stdout_rel=str(attempt["stdout_artifact_path"]),  # type: ignore[index]
            stderr_rel=str(attempt["stderr_artifact_path"]),  # type: ignore[index]
            observed_exit_code=observed_exit,
            expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
            expected_dispatch_id=dispatch_id,
            expected_effect_kind=effect_kind,
        )
    except (CodexEvidenceError, ValueError, TypeError, KeyError, OSError) as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"codex attempt evidence authentication failed: {exc}",
        ) from exc
    return outcome, effect_kind


def _authenticate_cursor_turn_attempt(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    source_run_id: str,
    attempt: object,
    expected_iteration: int,
) -> dict[str, object]:
    attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
    dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
    unit_identity = str(attempt["unit_identity"])  # type: ignore[index]
    launch_nonce = str(attempt["launch_nonce"])  # type: ignore[index]
    launch_intent_sha256 = str(attempt["launch_intent_sha256"])  # type: ignore[index]
    envelope_sha = attempt["completion_envelope_sha256"]  # type: ignore[index]
    exit_code = attempt["exit_code"]  # type: ignore[index]
    observed_exit = int(exit_code) if exit_code is not None else None
    run_root = artifacts.run_root(source_run_id)
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor attempt dispatch record missing",
        )
    effect_kind = str(dispatch["effect_kind"])
    if effect_kind != RUN_CURSOR_TURN_EFFECT_KIND:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor attempt effect_kind mismatch",
        )
    payload = dispatch["effect_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or int(payload.get("iteration", 0)) != expected_iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor attempt iteration mismatch",
        )
    try:
        verify_cursor_invocation_evidence(
            run_root,
            attempt_id=attempt_id,
            run_id=source_run_id,
            dispatch_id=dispatch_id,
            unit_identity=unit_identity,
            launch_nonce=launch_nonce,
            launch_intent_sha256=launch_intent_sha256,
            effect_kind=effect_kind,
        )
        outcome = load_authenticated_cursor_outcome(
            run_root,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=str(attempt["result_artifact_path"]),  # type: ignore[index]
            stdout_rel=str(attempt["stdout_artifact_path"]),  # type: ignore[index]
            stderr_rel=str(attempt["stderr_artifact_path"]),  # type: ignore[index]
            observed_exit_code=observed_exit,
            expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
            expected_dispatch_id=dispatch_id,
            expected_effect_kind=effect_kind,
        )
    except (CursorEvidenceError, ValueError, TypeError, KeyError, OSError) as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"cursor attempt evidence authentication failed: {exc}",
        ) from exc
    iteration = outcome.get("iteration")
    if not isinstance(iteration, int) or iteration != expected_iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor outcome iteration mismatch",
        )
    return outcome


def _authenticate_cursor_final_response(
    artifacts: ProtectedArtifactStore,
    *,
    source_run_id: str,
    cursor_attempt_id: str,
    iteration: int,
    cursor_outcome: dict[str, object],
) -> list[RecoveryCopyManifestEntry]:
    if not cursor_outcome.get("has_completion_signal"):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor turn lacks completion signal for final response",
        )
    expected_path = str(cursor_outcome.get("final_response_path", "")).strip()
    expected_sha256 = str(cursor_outcome.get("final_response_sha256", "")).strip()
    final_rel = cursor_attempt_final_rel(iteration, cursor_attempt_id)
    if not expected_path or not expected_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor outcome missing authenticated final response binding",
        )
    if expected_path != final_rel:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor final response path disagrees with authenticated outcome",
        )
    return [
        _manifest_entry(
            artifacts,
            source_run_id,
            final_rel,
            expected_sha256,
            max_bytes=MAX_CURSOR_FINAL_BYTES,
        )
    ]


def _operational_recovery_failure_kind(
    outcome: dict[str, object],
    *,
    run_root: Path,
) -> str:
    result_path = str(outcome.get("review_result_path", "")).strip()
    result_sha = str(outcome.get("review_result_sha256", "")).strip()
    if result_path and result_sha:
        path = run_root / result_path
        if path.is_file() and not path.is_symlink():
            try:
                load_validated_review_result(run_root, outcome)
            except CodexEvidenceError:
                pass
            else:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "authenticated outcome includes a valid review decision",
                )

    if outcome.get("timed_out"):
        return "codex_review_timeout"
    review_block_reason = str(outcome.get("review_block_reason", "")).strip()
    if review_block_reason:
        return review_block_reason
    failure_kind = str(outcome.get("failure_kind", "")).strip()
    if failure_kind or outcome.get("parse_ok") is False:
        return failure_kind or "codex_attempt_failed"
    result_path = str(outcome.get("review_result_path", "")).strip()
    result_sha = str(outcome.get("review_result_sha256", "")).strip()
    if not result_path or not result_sha:
        return "codex_review_outcome_invalid"
    returncode = outcome.get("returncode", 0)
    if isinstance(returncode, bool) or not isinstance(returncode, int) or returncode != 0:
        return "codex_review_outcome_invalid"
    return "codex_review_outcome_invalid"


def _verify_operational_recovery_eligibility(
    *,
    run_root: Path,
    outcome: dict[str, object],
    review_iteration: int,
    effect_kind: str,
    bound_session_id: str,
) -> str:
    try:
        validate_codex_review_outcome_integrity(
            outcome,
            run_root=run_root,
            expected_review_iteration=review_iteration,
            expected_effect_kind=effect_kind,
            bound_session_id=bound_session_id,
        )
    except CodexEvidenceError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"recovery blocked by integrity evidence: {exc}",
        ) from exc
    return _operational_recovery_failure_kind(outcome, run_root=run_root)


def _authenticate_bootstrap_and_resume_identity(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    evidence_run_id: str,
    blocked_run_id: str,
    reviewer_bound_event: CodexReviewerBoundEvent,
    staging_iteration: int,
    bootstrap_attempt: object,
    failed_attempt: object,
    failed_outcome: dict[str, object],
    failed_effect_kind: str,
) -> tuple[str, str, RecoveryCopyManifestEntry]:
    try:
        binding_bytes = artifacts.read_verified_bytes(
            evidence_run_id,
            reviewer_bound_event.binding_artifact_path,
            expected_sha256=reviewer_bound_event.binding_artifact_sha256,
        )
    except ProtectedArtifactError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    try:
        binding_payload = json.loads(binding_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "reviewer binding artifact is not valid JSON",
        ) from exc
    if not isinstance(binding_payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "reviewer binding artifact must be a JSON object",
        )
    expected_events_sha = str(binding_payload.get("bootstrap_events_sha256", "")).strip()
    prefix = str(binding_payload.get("bootstrap_session_id_prefix", "")).strip()
    if not expected_events_sha or not prefix:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "reviewer binding artifact missing authenticated fields",
        )
    if prefix != reviewer_bound_event.reviewer_session_id_prefix:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "reviewer binding artifact prefix disagrees with ledger event",
        )

    bootstrap_outcome, bootstrap_effect_kind = _authenticate_codex_attempt(
        store,
        artifacts,
        source_run_id=evidence_run_id,
        attempt=bootstrap_attempt,
    )
    if bootstrap_effect_kind != BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "bootstrap attempt effect_kind mismatch",
        )
    bootstrap_session_id = str(bootstrap_outcome.get("bootstrap_session_id", "")).strip()
    if not bootstrap_session_id or not bootstrap_session_id.startswith(prefix):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "bootstrap outcome session disagrees with bound reviewer",
        )
    bootstrap_events_rel = str(bootstrap_outcome.get("events_path", "")).strip()
    if not bootstrap_events_rel:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "bootstrap outcome missing events artifact binding",
        )
    _verify_bounded_artifact_digest(
        artifacts,
        evidence_run_id,
        bootstrap_events_rel,
        expected_events_sha,
        max_bytes=MAX_CODEX_EVENTS_ARTIFACT_BYTES,
    )
    bootstrap_events_entry = RecoveryCopyManifestEntry(
        relative_path=bootstrap_events_rel,
        expected_sha256=expected_events_sha,
    )

    bootstrap_attempt_id = str(bootstrap_attempt["attempt_id"])  # type: ignore[index]
    failed_attempt_id = str(failed_attempt["attempt_id"])  # type: ignore[index]
    if failed_attempt_id == bootstrap_attempt_id:
        if failed_effect_kind != BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "failed attempt must match bootstrap effect when ids match",
            )
        session_id = bootstrap_session_id
    else:
        if failed_effect_kind != RESUME_CODEX_REVIEW_EFFECT_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "failed review attempt must be a resume after bootstrap",
            )
        resume_session_id = str(failed_outcome.get("resume_session_id", "")).strip()
        if resume_session_id != bootstrap_session_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "failed resume attempt session disagrees with bootstrap reviewer",
            )
        session_id = resume_session_id

    iteration = failed_outcome.get("review_iteration")
    if not isinstance(iteration, int) or iteration != staging_iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "failed attempt review_iteration mismatch",
        )

    return session_id, bootstrap_attempt_id, bootstrap_events_entry


def _dedupe_manifest(
    entries: list[RecoveryCopyManifestEntry],
) -> tuple[RecoveryCopyManifestEntry, ...]:
    deduped: dict[str, RecoveryCopyManifestEntry] = {}
    for entry in entries:
        deduped[entry.relative_path] = entry
    return tuple(deduped.values())


def _run_has_cursor_staging_ledger_evidence(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM scheduler_events
        WHERE run_id = ?
          AND event_kind IN (?, ?, ?)
        LIMIT 1
        """,
        (
            run_id,
            CURSOR_CHAT_CREATED_EVENT_KIND,
            STAGING_COMPLETED_EVENT_KIND,
            CURSOR_TURN_COMPLETED_EVENT_KIND,
        ),
    ).fetchone()
    return row is not None


def resolve_recovery_ledger_evidence_run_id(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    source_run_id: str,
) -> str:
    current = source_run_id
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        if _run_has_cursor_staging_ledger_evidence(store, conn, current):
            return current
        row = store.get_review_recovery_source_for_successor(conn, successor_run_id=current)
        if row is None:
            break
        current = str(row["source_run_id"])
    return source_run_id


def analyze_blocked_review_recovery(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
) -> BlockedReviewRecoveryEvidence:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, source_run_id)
        if not isinstance(state, BlockedState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"source run is not blocked (state={state.kind})",
            )
        if is_integrity_review_block_kind(state.block_reason_kind):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"block reason is not eligible for review recovery: {state.block_reason_kind}",
            )
        if store.get_nonterminal_attempt_for_run(conn, source_run_id) is not None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "source run still has a nonterminal attempt",
            )
        evidence_run_id = resolve_recovery_ledger_evidence_run_id(store, conn, source_run_id)
        events = store.list_events_for_run(conn, evidence_run_id, limit=500, newest_first=False)

    chat_event: CursorChatCreatedEvent | None = None
    staging_event: StagingCompletedEvent | None = None
    turn_event: CursorTurnCompletedEvent | None = None
    reviewer_bound_event: CodexReviewerBoundEvent | None = None
    for row in events:
        kind = str(row["event_kind"])
        if kind == CURSOR_CHAT_CREATED_EVENT_KIND and chat_event is None:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CursorChatCreatedEvent):
                chat_event = parsed
        if kind == STAGING_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, StagingCompletedEvent):
                staging_event = parsed
        if kind == CURSOR_TURN_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CursorTurnCompletedEvent):
                turn_event = parsed
        if kind == CODEX_REVIEWER_BOUND_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CodexReviewerBoundEvent):
                reviewer_bound_event = parsed
        if kind == CODEX_REVIEW_COMPLETED_EVENT_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "blocked source already has a valid review decision",
            )

    if (
        chat_event is None
        or staging_event is None
        or turn_event is None
        or reviewer_bound_event is None
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks completed cursor/staging/reviewer evidence",
        )
    if turn_event.iteration != staging_event.iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cursor turn iteration disagrees with staged patch iteration",
        )

    with store.begin_read() as conn:
        attempt = store.get_latest_recorded_codex_attempt(conn, source_run_id)
        bootstrap_attempt = store.get_codex_bootstrap_attempt(conn, evidence_run_id)
        cursor_attempt = store.get_cursor_turn_attempt_for_iteration(
            conn,
            run_id=evidence_run_id,
            iteration=staging_event.iteration,
        )
    if attempt is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks a completed Codex attempt",
        )
    if bootstrap_attempt is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks an authenticated bootstrap Codex attempt",
        )
    if cursor_attempt is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks a completed Cursor turn for the staged iteration",
        )

    failed_attempt_id = str(attempt["attempt_id"])
    failed_outcome, failed_effect_kind = _authenticate_codex_attempt(
        store,
        artifacts,
        source_run_id=source_run_id,
        attempt=attempt,
    )
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if dispatch is None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "failed codex attempt dispatch record missing",
        )
    payload = dispatch["effect_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "failed codex attempt dispatch payload invalid",
        )
    review_iteration = int(payload.get("review_iteration", staging_event.iteration))
    if review_iteration != staging_event.iteration:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "failed codex attempt review_iteration disagrees with staged patch",
        )

    reviewer_session_id, bootstrap_attempt_id, bootstrap_events_entry = (
        _authenticate_bootstrap_and_resume_identity(
            store,
            artifacts,
            evidence_run_id=evidence_run_id,
            blocked_run_id=source_run_id,
            reviewer_bound_event=reviewer_bound_event,
            staging_iteration=staging_event.iteration,
            bootstrap_attempt=bootstrap_attempt,
            failed_attempt=attempt,
            failed_outcome=failed_outcome,
            failed_effect_kind=failed_effect_kind,
        )
    )
    failed_run_root = artifacts.run_root(source_run_id)
    _verify_operational_recovery_eligibility(
        run_root=failed_run_root,
        outcome=failed_outcome,
        review_iteration=review_iteration,
        effect_kind=failed_effect_kind,
        bound_session_id=reviewer_session_id,
    )
    recovery_key = _recovery_key(state.block_reason_kind, failed_attempt_id)

    authorized_at = state.authorized_at
    if not authorized_at:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks authorized_at for admission checkpoint reconstruction",
        )

    admission_path = None
    admission_sha = None
    for row in events:
        if str(row["event_kind"]) == "worktree_admitted":
            _verify_event_row(row)
            payload_obj = json.loads(str(row["event_payload"]))
            admission_path = str(payload_obj["admission_status_artifact_path"])
            admission_sha = str(payload_obj["admission_status_sha256"])
            break
    if not admission_path or not admission_sha:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "blocked source lacks admission evidence",
        )

    manifest_entries: list[RecoveryCopyManifestEntry] = []
    manifest_entries.extend(
        _authenticate_frozen_context_artifacts(state.context, artifacts, evidence_run_id)
    )
    manifest_entries.append(
        _manifest_entry(artifacts, evidence_run_id, admission_path, admission_sha)
    )
    manifest_entries.append(
        _manifest_entry(
            artifacts,
            evidence_run_id,
            chat_event.chat_artifact_path,
            chat_event.chat_artifact_sha256,
        )
    )
    manifest_entries.append(
        _require_cursor_fingerprint_artifact(
            artifacts,
            evidence_run_id,
            turn_event.cursor_output_fingerprint_path,
            turn_event.cursor_output_fingerprint_sha256,
        )
    )
    manifest_entries.append(
        _manifest_entry(
            artifacts,
            evidence_run_id,
            staging_event.staged_patch_path,
            staging_event.staged_patch_sha256,
        )
    )
    manifest_entries.append(
        _manifest_entry(
            artifacts,
            evidence_run_id,
            reviewer_bound_event.binding_artifact_path,
            reviewer_bound_event.binding_artifact_sha256,
        )
    )
    cursor_outcome = _authenticate_cursor_turn_attempt(
        store,
        artifacts,
        source_run_id=evidence_run_id,
        attempt=cursor_attempt,
        expected_iteration=staging_event.iteration,
    )
    cursor_attempt_id = str(cursor_attempt["attempt_id"])
    manifest_entries.extend(
        _authenticate_cursor_final_response(
            artifacts,
            source_run_id=evidence_run_id,
            cursor_attempt_id=cursor_attempt_id,
            iteration=staging_event.iteration,
            cursor_outcome=cursor_outcome,
        )
    )
    manifest_entries.append(bootstrap_events_entry)

    copy_manifest = _dedupe_manifest(manifest_entries)

    checkpoint = AdmittedRunCheckpoint(
        authorized_at=authorized_at,
        authorized_controller_session_id=state.authorized_controller_session_id,
        admitted_at=authorized_at,
        admission_status_artifact_path=admission_path,
        admission_status_sha256=admission_sha,
    )
    cursor = CursorWorkflowCheckpoint(
        iteration=int(staging_event.iteration),
        chat_id=chat_event.chat_id,
        chat_artifact_path=chat_event.chat_artifact_path,
        chat_artifact_sha256=chat_event.chat_artifact_sha256,
        staged_patch_path=staging_event.staged_patch_path,
        staged_patch_sha256=staging_event.staged_patch_sha256,
    )
    codex = CodexWorkflowCheckpoint(
        review_iteration=int(staging_event.iteration),
        reviewer_session_id=reviewer_session_id,
        binding_artifact_path=reviewer_bound_event.binding_artifact_path,
        binding_artifact_sha256=reviewer_bound_event.binding_artifact_sha256,
    )

    evidence = BlockedReviewRecoveryEvidence(
        context=state.context,
        checkpoint=checkpoint,
        cursor=cursor,
        codex=codex,
        block_reason_kind=state.block_reason_kind,
        failed_attempt_id=failed_attempt_id,
        bootstrap_attempt_id=bootstrap_attempt_id,
        recovery_key=recovery_key,
        copy_manifest=copy_manifest,
    )
    try:
        verify_review_retry_repository_checkpoint(
            context=evidence.context,
            run_id=source_run_id,
            artifacts=artifacts,
            checkpoint=evidence.checkpoint,
            cursor=evidence.cursor,
            plan_sha256=evidence.context.plan_prompt.plan_sha256,
            prompt_sha256=evidence.context.plan_prompt.prompt_sha256,
        )
    except ReviewCheckpointVerificationError as exc:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            str(exc),
        ) from exc
    return evidence


def _copy_recovery_artifacts(
    artifacts: ProtectedArtifactStore,
    *,
    artifact_source_run_id: str,
    successor_run_id: str,
    manifest: tuple[RecoveryCopyManifestEntry, ...],
) -> None:
    for entry in manifest:
        content = artifacts.read_verified_bytes(
            artifact_source_run_id,
            entry.relative_path,
            expected_sha256=entry.expected_sha256,
        )
        if len(content) > MAX_RECOVERY_ARTIFACT_BYTES:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"recovery artifact exceeds size bound: {entry.relative_path}",
            )
        artifacts.publish_or_verify_bytes(
            successor_run_id,
            entry.relative_path,
            content,
            max_bytes=len(content) + 1,
        )


def materialize_review_recovery_successor(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    evidence: BlockedReviewRecoveryEvidence,
    *,
    source_run_id: str,
    now: datetime,
    run_id_factory: Callable[[str, datetime], str] | None = None,
    event_id_factory: Callable[[], str] | None = None,
) -> str:
    from ai_dev_loop.scheduler.domain.events import CodexReviewRecoverySuccessorCreatedEvent

    successor_run_id = (
        run_id_factory(evidence.context.project_name, now)
        if run_id_factory is not None
        else generate_run_id(evidence.context.project_name, now=now)
    )
    staged_patch_sha256 = evidence.cursor.staged_patch_sha256
    if not staged_patch_sha256:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "recovery evidence lacks staged patch hash",
        )
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    lineage = ReviewRecoveryLineage(
        source_run_id=source_run_id,
        source_block_reason_kind=evidence.block_reason_kind,
        source_review_iteration=evidence.cursor.iteration,
        source_staged_patch_sha256=staged_patch_sha256,
        source_failed_attempt_id=evidence.failed_attempt_id,
        created_at=now_text,
    )
    successor_state = AwaitingCodexReviewState(
        run_id=successor_run_id,
        version=1,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=secrets.token_hex(32),
        context=evidence.context,
        checkpoint=evidence.checkpoint,
        cursor=evidence.cursor,
        codex=evidence.codex,
        recovery=lineage,
    )
    event = CodexReviewRecoverySuccessorCreatedEvent(
        source_run_id=source_run_id,
        successor_run_id=successor_run_id,
        recovery_key=evidence.recovery_key,
        review_iteration=evidence.cursor.iteration,
    )
    event_id = (
        event_id_factory() if event_id_factory is not None else f"evt-{secrets.token_hex(16)}"
    )

    with store.begin_immediate() as conn:
        existing = store.get_review_recovery_successor(
            conn,
            source_run_id=source_run_id,
            recovery_key=evidence.recovery_key,
        )
        if existing is not None:
            return str(existing["successor_run_id"])

    with store.begin_read() as conn:
        artifact_source_run_id = resolve_recovery_ledger_evidence_run_id(store, conn, source_run_id)
    _copy_recovery_artifacts(
        artifacts,
        artifact_source_run_id=artifact_source_run_id,
        successor_run_id=successor_run_id,
        manifest=evidence.copy_manifest,
    )

    with store.begin_immediate() as conn:
        existing = store.get_review_recovery_successor(
            conn,
            source_run_id=source_run_id,
            recovery_key=evidence.recovery_key,
        )
        if existing is not None:
            return str(existing["successor_run_id"])
        store.insert_recovery_review_run(
            conn,
            run_id=successor_run_id,
            state=successor_state,
            event_id=event_id,
            event=event,
            now=now,
        )
        inserted = store.insert_review_recovery_successor(
            conn,
            source_run_id=source_run_id,
            recovery_key=evidence.recovery_key,
            successor_run_id=successor_run_id,
            now=now,
        )
        if not inserted:
            row = store.get_review_recovery_successor(
                conn,
                source_run_id=source_run_id,
                recovery_key=evidence.recovery_key,
            )
            if row is None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.INTERNAL,
                    "review recovery successor insert failed",
                )
            return str(row["successor_run_id"])
    return successor_run_id
