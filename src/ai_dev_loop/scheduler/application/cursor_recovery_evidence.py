"""Read-only causal evidence analysis for blocked Cursor turn failures (Phase 23.1)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

from pydantic import Field, ValidationError, field_validator

from ai_dev_loop.fresh_codex_reviewer import classify_bootstrap_session_id_from_text
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_outcome,
    load_validated_review_result,
    validate_codex_review_outcome_integrity,
    verify_codex_invocation_evidence,
    verify_pre_execution_codex_guards,
)
from ai_dev_loop.scheduler.application.contracts import AppModel
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    frozen_repository_identity,
    load_authenticated_cursor_outcome,
    validate_frozen_repository_identity,
    verify_correction_envelope_binding,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.application.recovery_ancestry import RecoveryAncestorEdge
from ai_dev_loop.scheduler.application.review_budget import (
    load_review_budget_extensions,
    review_budget_projection,
)
from ai_dev_loop.scheduler.application.review_recovery import (
    _parse_event_row,
    _verify_event_row,
    resolve_recovery_ledger_evidence_run_id,
)
from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import (
    ATTEMPT_COMPLETED_EVENT_KIND,
    CODEX_REVIEW_COMPLETED_EVENT_KIND,
    CODEX_REVIEWER_BOUND_EVENT_KIND,
    CURSOR_CHAT_CREATED_EVENT_KIND,
    CURSOR_TURN_BLOCKED_EVENT_KIND,
    CURSOR_TURN_COMPLETED_EVENT_KIND,
    CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND,
    STAGING_COMPLETED_EVENT_KIND,
    WAITING_FOR_CURSOR_FIX_EVENT_KIND,
    AttemptCompletedEvent,
    CodexReviewCompletedEvent,
    CodexReviewerBoundEvent,
    CursorChatCreatedEvent,
    CursorTurnBlockedEvent,
    CursorUsageLimitDetectedEvent,
    ReviewBudgetExtendedEvent,
    StagingCompletedEvent,
    WaitingForCursorFixEnteredEvent,
)
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    ActiveSequenceState,
    BlockedSequenceState,
    PreparedSequenceDefinition,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    SequenceRunBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactError
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_FAILED,
    TIMER_STATUS_FIRED,
    SqliteSchedulerStore,
)

EVENT_PAGE_SIZE = 50
CURSOR_RECOVERY_CHECK_SCHEMA_VERSION = 1
SAFE_SUMMARY_MAX_LEN = 320

EvidenceStatus = Literal["authenticated", "insufficient", "ineligible", "corrupt"]
TurnKind = Literal["initial", "correction"]


class _ArtifactReader(Protocol):
    def run_root(self, run_id: str) -> Path: ...

    def read_verified_bytes(
        self, run_id: str, relative_path: str, *, expected_sha256: str
    ) -> bytes: ...


def _strict_positive_int(value: object, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, (bool, str, float)):
        raise ValueError(f"{field_name} must be a JSON integer")
    if not isinstance(value, int) or value < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


class CursorRecoveryCheckReceipt(AppModel):
    schema_version: int = CURSOR_RECOVERY_CHECK_SCHEMA_VERSION
    run_id: str
    evidence_status: EvidenceStatus
    recovery_supported: bool = False
    turn_kind: TurnKind | None
    reason_code: str
    safe_summary: str = Field(max_length=SAFE_SUMMARY_MAX_LEN)
    sequence_id: str | None
    ordinal: int | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def schema_version_is_strict_int(cls, value: object) -> object:
        if isinstance(value, (bool, str, float)):
            raise ValueError("schema_version must be a JSON integer")
        if value != CURSOR_RECOVERY_CHECK_SCHEMA_VERSION:
            raise ValueError("schema_version must be 1")
        return value

    @field_validator("ordinal", mode="before")
    @classmethod
    def ordinal_is_strict_int_or_null(cls, value: object) -> object:
        if value is None:
            return None
        return _strict_positive_int(value, field_name="ordinal")

    @field_validator("safe_summary")
    @classmethod
    def summary_bounded(cls, value: str) -> str:
        if not value or len(value) > SAFE_SUMMARY_MAX_LEN:
            raise ValueError("safe_summary out of bounds")
        return value


@dataclass(frozen=True)
class CursorRecoveryEvidenceBundle:
    run_id: str
    evidence_run_id: str
    failed_attempt_id: str
    dispatch_id: str
    iteration: int
    turn_kind: TurnKind
    chat_id: str
    prompt_path: str
    prompt_sha256: str
    reviews_completed: int
    effective_review_ceiling: int
    reviewer_bound: bool
    sequence_id: str | None
    ordinal: int | None
    sequence_leaf_matches: bool | None
    chat_owner_run_id: str
    chat_artifact_path: str
    chat_artifact_sha256: str
    submitted_max_review_iterations: int = 0
    fix_owner_run_id: str | None = None
    fix_prompt_path: str | None = None
    fix_prompt_sha256: str | None = None
    staged_owner_run_id: str | None = None
    staged_patch_path: str | None = None
    staged_patch_sha256: str | None = None
    review_result_owner_run_id: str | None = None
    review_result_path: str | None = None
    review_result_sha256: str | None = None
    reviewer_session_id: str | None = None
    reviewer_binding_run_id: str | None = None
    reviewer_bootstrap_run_id: str | None = None
    binding_artifact_path: str | None = None
    binding_artifact_sha256: str | None = None
    bootstrap_attempt_id: str | None = None
    bootstrap_events_path: str | None = None
    bootstrap_events_sha256: str | None = None
    admitted_owner_run_id: str | None = None
    admitted_artifact_path: str | None = None
    admitted_artifact_sha256: str | None = None


@dataclass(frozen=True)
class CursorRecoveryAnalysisResult:
    receipt: CursorRecoveryCheckReceipt
    evidence: CursorRecoveryEvidenceBundle | None = None


@dataclass(frozen=True)
class ReviewerBootstrapProof:
    session_id: str
    binding_run_id: str
    bootstrap_run_id: str
    bootstrap_attempt_id: str
    bootstrap_events_path: str
    bootstrap_events_sha256: str
    binding_artifact_path: str
    binding_artifact_sha256: str


@dataclass(frozen=True)
class _CorrectionFixEvidence:
    owner_run_id: str
    review_iteration: int
    fix_prompt_path: str
    fix_prompt_sha256: str
    correction_envelope_path: str
    correction_envelope_sha256: str
    review_result_path: str | None
    review_result_sha256: str | None
    causal_sequence: int


def _bounded_summary(text: str) -> str:
    trimmed = text.strip()
    if not trimmed:
        return "Cursor recovery evidence inspection completed."
    if len(trimmed) <= SAFE_SUMMARY_MAX_LEN:
        return trimmed
    return trimmed[: SAFE_SUMMARY_MAX_LEN - 1] + "…"


def receipt(
    run_id: str,
    *,
    evidence_status: EvidenceStatus,
    turn_kind: TurnKind | None,
    reason_code: str,
    safe_summary: str,
    sequence_id: str | None = None,
    ordinal: int | None = None,
    recovery_supported: bool = False,
) -> CursorRecoveryCheckReceipt:
    return CursorRecoveryCheckReceipt(
        run_id=run_id,
        evidence_status=evidence_status,
        turn_kind=turn_kind,
        reason_code=reason_code,
        safe_summary=_bounded_summary(safe_summary),
        sequence_id=sequence_id,
        ordinal=ordinal,
        recovery_supported=recovery_supported,
    )


def _load_all_verified_events(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    *,
    page_size: int = EVENT_PAGE_SIZE,
) -> tuple[list[sqlite3.Row], bool]:
    offset = 0
    collected: list[sqlite3.Row] = []
    previous_sequence: int | None = None
    expecting_more = False
    while True:
        page, has_more = store.list_events_for_run_offset(
            conn,
            run_id,
            offset=offset,
            limit=page_size,
        )
        if not page:
            return collected, not expecting_more
        for row in page:
            _verify_event_row(row)
            sequence = int(row["sequence"])
            if previous_sequence is not None and sequence <= previous_sequence:
                return collected, False
            previous_sequence = sequence
        collected.extend(page)
        if not has_more:
            return collected, True
        expecting_more = True
        offset += len(page)


def _attempt_row_matches_completion(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    completed: AttemptCompletedEvent,
) -> bool:
    if completed.run_id != run_id:
        return False
    row = store.get_attempt_by_id(conn, completed.attempt_id)
    if row is None or str(row["run_id"]) != run_id:
        return False
    if str(row["attempt_id"]) != completed.attempt_id:
        return False
    if str(row["status"]) != ATTEMPT_STATUS_FAILED:
        return False
    if str(row["dispatch_id"]) != completed.dispatch_id:
        return False
    try:
        capacity_claim_id = row["capacity_claim_id"]
    except (KeyError, IndexError):
        return False
    if capacity_claim_id is None:
        return False
    capacity_claim_id = str(capacity_claim_id)
    if not capacity_claim_id or completed.claim_id != capacity_claim_id:
        return False
    fence = row["completion_fence_id"]
    if fence is None or str(fence) != completed.completion_fence_id:
        return False
    if str(row["termination_class"]) != completed.termination_class:
        return False
    row_exit = row["exit_code"]
    if row_exit is None:
        return False
    if int(row_exit) != int(completed.exit_code):
        return False
    if int(row_exit) == 124:
        return False
    dispatch = store.get_effect_by_dispatch_id(conn, completed.dispatch_id)
    if dispatch is None or str(dispatch["effect_kind"]) != RUN_CURSOR_TURN_EFFECT_KIND:
        return False
    try:
        effect_claim_id = dispatch["claim_id"]
    except (KeyError, IndexError):
        return False
    if effect_claim_id is None or str(effect_claim_id) != capacity_claim_id:
        return False
    return bool(row["ingested"])


def resolve_decisive_failed_cursor_attempt(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    events: list[sqlite3.Row],
) -> tuple[str | None, str, int | None]:
    last_turn_completed_sequence = 0
    pending_failed: list[tuple[int, str]] = []
    cursor_failure_blocks: list[tuple[int, list[tuple[int, str]]]] = []

    for row in events:
        sequence = int(row["sequence"])
        kind = str(row["event_kind"])
        if kind == CURSOR_TURN_COMPLETED_EVENT_KIND:
            last_turn_completed_sequence = sequence
            pending_failed = []
            continue
        if kind == ATTEMPT_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if not isinstance(parsed, AttemptCompletedEvent):
                continue
            if _attempt_row_matches_completion(store, conn, run_id, parsed):
                pending_failed.append((sequence, parsed.attempt_id))
            continue
        if kind == CURSOR_TURN_BLOCKED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if not isinstance(parsed, CursorTurnBlockedEvent):
                continue
            if parsed.run_id != run_id:
                continue
            if parsed.block_reason_kind != "cursor_failure":
                continue
            cursor_failure_blocks.append((sequence, list(pending_failed)))

    if not cursor_failure_blocks:
        return None, "no_cursor_failure_block_in_history", None
    if len(cursor_failure_blocks) > 1:
        return None, "ambiguous_cursor_failure_blocks", None

    block_sequence, pending_at_block = cursor_failure_blocks[-1]
    candidates = [
        attempt_id
        for seq, attempt_id in pending_at_block
        if seq > last_turn_completed_sequence and seq < block_sequence
    ]
    if not candidates:
        return None, "no_failed_attempt_precedes_block", None
    if len(candidates) > 1:
        return None, "ambiguous_failed_attempts", block_sequence
    return candidates[0], "ok", block_sequence


def _correction_recovery_row(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    """Return a correction-recovery row, or none when that relation table is absent.

    Historical captures predate the table. Absence is not a record, so ambiguous
    cursor histories stay unresolved.
    """

    try:
        return store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None


def _attempt_completed_sequence(events: list[sqlite3.Row], attempt_id: str) -> int | None:
    for row in events:
        if str(row["event_kind"]) != ATTEMPT_COMPLETED_EVENT_KIND:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, AttemptCompletedEvent) and parsed.attempt_id == attempt_id:
            return int(row["sequence"])
    return None


def _cursor_attempt_outcome(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    attempt = store.get_attempt_by_id(conn, attempt_id)
    if attempt is None or str(attempt["run_id"]) != run_id:
        raise CursorEvidenceError("usage-limit continuation attempt is missing")
    dispatch_id = str(attempt["dispatch_id"])
    dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None:
        raise CursorEvidenceError("usage-limit continuation dispatch is missing")
    run_root = artifacts.run_root(run_id)
    unit_identity = str(attempt["unit_identity"])
    exit_code = attempt["exit_code"]
    observed_exit = int(exit_code) if exit_code is not None else None
    observed_termination: TerminationClass | None = None
    termination_raw = attempt["termination_class"]
    if termination_raw is not None:
        observed_termination = TerminationClass(str(termination_raw))
    envelope_sha = attempt["completion_envelope_sha256"]
    binding = verify_cursor_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=str(attempt["launch_nonce"]),
        launch_intent_sha256=str(attempt["launch_intent_sha256"]),
        effect_kind=str(dispatch["effect_kind"]),
    )
    outcome = load_authenticated_cursor_outcome(
        run_root,
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        result_rel=str(attempt["result_artifact_path"]),
        stdout_rel=str(attempt["stdout_artifact_path"]),
        stderr_rel=str(attempt["stderr_artifact_path"]),
        observed_exit_code=observed_exit,
        observed_termination=observed_termination,
        expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
        expected_dispatch_id=dispatch_id,
        expected_effect_kind=str(dispatch["effect_kind"]),
    )
    return binding, outcome


def _usage_limit_transition(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    events: list[sqlite3.Row],
    predecessor_id: str,
    successor_id: str,
) -> CursorUsageLimitDetectedEvent | None:
    """Return the usage-limit event that durably scheduled the successor attempt."""

    predecessor_sequence = _attempt_completed_sequence(events, predecessor_id)
    successor_sequence = _attempt_completed_sequence(events, successor_id)
    if (
        predecessor_sequence is None
        or successor_sequence is None
        or predecessor_sequence >= successor_sequence
    ):
        return None
    detected: list[tuple[sqlite3.Row, CursorUsageLimitDetectedEvent]] = []
    for row in events:
        sequence = int(row["sequence"])
        if sequence <= predecessor_sequence or sequence >= successor_sequence:
            continue
        if str(row["event_kind"]) != CURSOR_USAGE_LIMIT_DETECTED_EVENT_KIND:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, CursorUsageLimitDetectedEvent) and parsed.run_id == run_id:
            detected.append((row, parsed))
    if len(detected) != 1:
        return None
    event_row, event = detected[0]
    timers = conn.execute(
        """
        SELECT * FROM scheduler_timers
        WHERE source_event_id = ? AND run_id = ? AND timer_kind = 'retry_due'
        """,
        (str(event_row["event_id"]), run_id),
    ).fetchall()
    if len(timers) != 1:
        return None
    timer = timers[0]
    fired_event_id = timer["fired_event_id"]
    if str(timer["status"]) != TIMER_STATUS_FIRED or fired_event_id is None:
        return None
    if str(timer["target_effect_id"]) != RUN_CURSOR_TURN_EFFECT_ID:
        return None
    effects = conn.execute(
        """
        SELECT * FROM scheduler_effects
        WHERE source_event_id = ? AND run_id = ? AND effect_kind = ?
        """,
        (str(fired_event_id), run_id, RUN_CURSOR_TURN_EFFECT_KIND),
    ).fetchall()
    if len(effects) != 1:
        return None
    successor = store.get_attempt_by_id(conn, successor_id)
    if successor is None or str(successor["dispatch_id"]) != str(effects[0]["dispatch_id"]):
        return None
    predecessor = store.get_attempt_by_id(conn, predecessor_id)
    if predecessor is None or not int(predecessor["ingested"]):
        return None
    _predecessor_binding, predecessor_outcome = _cursor_attempt_outcome(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=predecessor_id,
    )
    if predecessor_outcome.get("failure_code") != "cursor_usage_limit":
        return None
    successor_binding, _successor_outcome = _cursor_attempt_outcome(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=successor_id,
    )
    prompt_path = str(successor_binding.get("prompt_path", "")).strip()
    prompt_sha = str(successor_binding.get("prompt_sha256", "")).strip()
    if (
        prompt_path != event.continuation_envelope_path
        or prompt_sha != event.continuation_envelope_sha256
    ):
        return None
    return event


def _usage_limit_candidate_chain(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    events: list[sqlite3.Row],
) -> tuple[list[str], CursorUsageLimitDetectedEvent, int] | None:
    """Return one timer-linked usage-limit chain and its non-limit terminal attempt."""

    _attempt_id, detail, block_sequence = resolve_decisive_failed_cursor_attempt(
        store,
        conn,
        run_id,
        events,
    )
    if detail != "ambiguous_failed_attempts" or block_sequence is None:
        return None
    last_turn_completed_sequence = 0
    pending_failed: list[tuple[int, str]] = []
    for row in events:
        sequence = int(row["sequence"])
        if sequence >= block_sequence:
            break
        kind = str(row["event_kind"])
        if kind == CURSOR_TURN_COMPLETED_EVENT_KIND:
            last_turn_completed_sequence = sequence
            pending_failed = []
            continue
        if kind != ATTEMPT_COMPLETED_EVENT_KIND:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, AttemptCompletedEvent) and _attempt_row_matches_completion(
            store,
            conn,
            run_id,
            parsed,
        ):
            pending_failed.append((sequence, parsed.attempt_id))
    candidates = [
        attempt_id
        for sequence, attempt_id in pending_failed
        if sequence > last_turn_completed_sequence
    ]
    if len(candidates) < 2:
        return None
    last_event: CursorUsageLimitDetectedEvent | None = None
    for predecessor_id, successor_id in zip(candidates, candidates[1:], strict=False):
        last_event = _usage_limit_transition(
            store,
            artifacts,
            conn,
            run_id=run_id,
            events=events,
            predecessor_id=predecessor_id,
            successor_id=successor_id,
        )
        if last_event is None:
            return None
    terminal_id = candidates[-1]
    _terminal_binding, terminal_outcome = _cursor_attempt_outcome(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=terminal_id,
    )
    if terminal_outcome.get("failure_code") == "cursor_usage_limit":
        return None
    assert last_event is not None
    return candidates, last_event, last_turn_completed_sequence


def _authenticated_recovery_prompt(
    artifacts: _ArtifactReader,
    *,
    run_id: str,
    record_path: str,
    record_sha: str,
) -> tuple[str, str, bytes, bytes | None]:
    """Authenticate a ready recovery record and return its prompt authority.

    The last item is the raw fix when the record is a correction. Authentication
    failure raises; it is not treated as a different turn.
    """

    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
        RECORD_REL,
        CursorInitialRecoveryRecordV1,
    )
    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
        CORRECTION_RECORD_REL,
        CursorRecoveryRecordV2,
    )

    if not record_sha or record_path not in {CORRECTION_RECORD_REL, RECORD_REL}:
        raise CursorEvidenceError("recovery record failed authentication")
    try:
        payload = artifacts.read_verified_bytes(
            run_id,
            record_path,
            expected_sha256=record_sha,
        )
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise CursorEvidenceError("recovery record failed authentication")
        schema = decoded.get("schema_version")
        if record_path == CORRECTION_RECORD_REL or schema == 2:
            record = CursorRecoveryRecordV2.model_validate(decoded)
            effective_path = record.effective_prompt_path
            effective_sha = record.effective_prompt_sha256
            raw_fix = record.raw_fix if record.turn_kind == "correction" else None
        elif schema == 1:
            initial = CursorInitialRecoveryRecordV1.model_validate(decoded)
            effective_path = initial.effective_prompt_path
            effective_sha = initial.effective_prompt_sha256
            raw_fix = None
        else:
            raise CursorEvidenceError("recovery record failed authentication")
        effective = artifacts.read_verified_bytes(
            run_id,
            effective_path,
            expected_sha256=effective_sha,
        )
        raw_fix_bytes = None
        if raw_fix is not None:
            raw_fix_bytes = artifacts.read_verified_bytes(
                raw_fix.owner_run_id,
                raw_fix.relative_path,
                expected_sha256=raw_fix.sha256,
            )
    except (
        CursorEvidenceError,
        ProtectedArtifactError,
        OSError,
        ValidationError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise CursorEvidenceError("recovery record failed authentication") from exc
    return effective_path, effective_sha, effective, raw_fix_bytes


def _verified_initial_recovery_authority(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    row: sqlite3.Row,
) -> tuple[str, str]:
    """Apply the same initial record authority used by force replay.

    Schema-1 publication intents still carry v2 initial records. Intent version
    does not skip budget, carry, or parent checks. A valid later correction may
    continue only after this authority succeeds.
    """

    from ai_dev_loop.scheduler.application.cursor_initial_recovery import _verified_record
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
        ReadOnlyProtectedArtifactStore,
    )

    if not isinstance(artifacts, (ProtectedArtifactStore, ReadOnlyProtectedArtifactStore)):
        raise CursorEvidenceError("recovery record failed authentication")
    authority = _verified_record(store, cast(ProtectedArtifactStore, artifacts), row)
    return authority.effective_prompt_path, authority.effective_prompt_sha256


def _require_initial_ancestor_authority(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    run_id: str,
) -> None:
    """Authenticate required initial ancestors before any causal classification.

    One ordinary failed attempt never reaches usage-limit attribution. The
    ancestor check still runs, including when the record belongs to an earlier
    turn and its publication intent remains schema 1.
    """

    from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
        authenticate_required_initial_ancestors,
    )
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
    from ai_dev_loop.scheduler.infrastructure.readonly_protected_artifacts import (
        ReadOnlyProtectedArtifactStore,
    )

    if not isinstance(artifacts, (ProtectedArtifactStore, ReadOnlyProtectedArtifactStore)):
        return
    authenticate_required_initial_ancestors(
        store,
        cast(ProtectedArtifactStore, artifacts),
        run_id,
    )


def _recovery_record_attributes_continuation(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    first_attempt_id: str,
    continuation: bytes,
) -> bool | None:
    """Return whether the ready recovery record owns this continuation.

    True means an authenticated correction record launched this chain and its
    effective prompt and raw fix are inside the continuation. False means that
    governing record does not prove the continuation. None means there is no
    ready record, or a fully authenticated record belongs to another logical
    turn. Initial authority, including v2 budget and carry, is required first.
    """

    row = _correction_recovery_row(store, conn, run_id)
    if row is None or str(row["status"]) != "ready":
        return None
    record_path = str(row["record_artifact_path"] or "")
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECORD_REL

    if record_path == RECORD_REL:
        effective_path, effective_sha = _verified_initial_recovery_authority(
            store,
            artifacts,
            row,
        )
        effective = artifacts.read_verified_bytes(
            run_id,
            effective_path,
            expected_sha256=effective_sha,
        )
        raw_fix = None
    else:
        effective_path, effective_sha, effective, raw_fix = _authenticated_recovery_prompt(
            artifacts,
            run_id=run_id,
            record_path=record_path,
            record_sha=str(row["record_sha256"] or ""),
        )
    binding, _outcome = _cursor_attempt_outcome(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=first_attempt_id,
    )
    prompt_path = str(binding.get("prompt_path", "")).strip()
    prompt_sha = str(binding.get("prompt_sha256", "")).strip()
    if prompt_path != effective_path or prompt_sha != effective_sha:
        return None
    if raw_fix is None:
        return False
    return effective in continuation and raw_fix in effective


def _original_correction_checkpoint_attributes(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    events: list[sqlite3.Row],
    candidates: list[str],
    last_turn_completed_sequence: int,
    continuation: bytes,
) -> bool:
    """Bind the chain to the correction checkpoint that scheduled its first attempt."""

    first_id = candidates[0]
    first_sequence = _attempt_completed_sequence(events, first_id)
    attempt = store.get_attempt_by_id(conn, first_id)
    if attempt is None or first_sequence is None:
        return False
    dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if dispatch is None or dispatch["source_event_id"] is None:
        return False
    source_event_id = str(dispatch["source_event_id"])
    checkpoints = [
        (int(row["sequence"]), parsed)
        for row in events
        if str(row["event_id"]) == source_event_id
        and isinstance(
            parsed := _parse_event_row(row),
            (WaitingForCursorFixEnteredEvent, ReviewBudgetExtendedEvent),
        )
        and parsed.run_id == run_id
    ]
    if len(checkpoints) != 1:
        return False
    sequence, checkpoint = checkpoints[0]
    if sequence <= last_turn_completed_sequence or sequence >= first_sequence:
        return False
    binding, outcome = _cursor_attempt_outcome(
        store,
        artifacts,
        conn,
        run_id=run_id,
        attempt_id=first_id,
    )
    prompt_path = str(binding.get("prompt_path", "")).strip()
    prompt_sha = str(binding.get("prompt_sha256", "")).strip()
    if (
        prompt_path != checkpoint.correction_envelope_path
        or prompt_sha != checkpoint.correction_envelope_sha256
        or outcome.get("failure_code") != "cursor_usage_limit"
    ):
        return False
    envelope = artifacts.read_verified_bytes(
        run_id,
        checkpoint.correction_envelope_path,
        expected_sha256=checkpoint.correction_envelope_sha256,
    )
    raw_fix = artifacts.read_verified_bytes(
        run_id,
        checkpoint.fix_prompt_path,
        expected_sha256=checkpoint.fix_prompt_sha256,
    )
    return envelope in continuation and raw_fix in envelope


def _attributed_usage_limit_continuation_attempt(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    events: list[sqlite3.Row],
) -> str | None:
    """Select the terminal failure of one usage-limit chain attributed to a correction.

    A ready record owns the chain only when it launched the first attempt.
    An authenticated record for an earlier turn does not. The current
    waiting-for-fix or budget-extension checkpoint can then own the chain.
    A record that fails authentication is rejected instead of bypassed.
    """

    chain = _usage_limit_candidate_chain(
        store,
        artifacts,
        conn,
        run_id=run_id,
        events=events,
    )
    if chain is None:
        return None
    candidates, last_event, last_turn_completed_sequence = chain
    continuation = artifacts.read_verified_bytes(
        run_id,
        last_event.continuation_envelope_path,
        expected_sha256=last_event.continuation_envelope_sha256,
    )
    recorded = _recovery_record_attributes_continuation(
        store,
        artifacts,
        conn,
        run_id=run_id,
        first_attempt_id=candidates[0],
        continuation=continuation,
    )
    if recorded is True:
        return candidates[-1]
    if recorded is False:
        return None
    if _original_correction_checkpoint_attributes(
        store,
        artifacts,
        conn,
        run_id=run_id,
        events=events,
        candidates=candidates,
        last_turn_completed_sequence=last_turn_completed_sequence,
        continuation=continuation,
    ):
        return candidates[-1]
    return None


def assert_successor_reviewer_continuity(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    successor_run_id: str,
    evidence: CursorRecoveryEvidenceBundle,
) -> None:
    """Require the successor checkpoint or its own bound event to match derived B."""

    state, _, _ = store.load_validated_snapshot(conn, successor_run_id)
    codex = getattr(state, "codex", None)
    session = getattr(codex, "reviewer_session_id", None) if codex is not None else None
    if session:
        if (
            session != evidence.reviewer_session_id
            or getattr(codex, "binding_artifact_path", None) != evidence.binding_artifact_path
            or getattr(codex, "binding_artifact_sha256", None) != evidence.binding_artifact_sha256
        ):
            raise CursorEvidenceError("successor checkpoint lost reviewer continuity")
        return
    events, complete = _load_all_verified_events(store, conn, successor_run_id)
    if not complete:
        raise CursorEvidenceError("successor checkpoint lost reviewer continuity")
    bound = [
        parsed
        for row in events
        if str(row["event_kind"]) == CODEX_REVIEWER_BOUND_EVENT_KIND
        and isinstance(parsed := _parse_event_row(row), CodexReviewerBoundEvent)
        and parsed.run_id == successor_run_id
    ]
    expected = evidence.reviewer_session_id or ""
    if (
        len(bound) != 1
        or not expected
        or bound[0].reviewer_session_id_prefix != expected[:8]
        or bound[0].binding_artifact_path != evidence.binding_artifact_path
        or bound[0].binding_artifact_sha256 != evidence.binding_artifact_sha256
    ):
        raise CursorEvidenceError("successor checkpoint lost reviewer continuity")


def resolve_authenticated_decisive_attempt(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    run_id: str,
    *,
    event_page_size: int = EVENT_PAGE_SIZE,
) -> tuple[str | None, str, int | None]:
    """Resolve one failed cursor attempt from the complete authenticated event history."""

    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

    try:
        events, complete = _load_all_verified_events(
            store,
            conn,
            run_id,
            page_size=event_page_size,
        )
    except SchedulerEngineError as exc:
        raise CursorEvidenceError("event page failed authentication") from exc
    if not complete:
        raise CursorEvidenceError("event history pagination did not complete")
    attempt_id, detail, block_sequence = resolve_decisive_failed_cursor_attempt(
        store,
        conn,
        run_id,
        events,
    )
    if attempt_id is not None or detail != "ambiguous_failed_attempts":
        return attempt_id, detail, block_sequence
    continued = _attributed_usage_limit_continuation_attempt(
        store,
        artifacts,
        conn,
        run_id=run_id,
        events=events,
    )
    if continued is None:
        return None, detail, block_sequence
    return continued, "ok", block_sequence


def _normalize_effect_payload(payload: object) -> dict[str, object]:
    loaded = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(loaded, dict):
        raise CursorEvidenceError("cursor turn dispatch payload invalid")
    return loaded


def _verify_authenticated_cursor_dispatch(
    dispatch: sqlite3.Row,
    *,
    run_id: str,
    expected_effect_kind: str,
) -> dict[str, object]:
    if str(dispatch["run_id"]) != run_id:
        raise CursorEvidenceError("cursor dispatch run_id mismatch")
    if str(dispatch["effect_kind"]) != expected_effect_kind:
        raise CursorEvidenceError("cursor dispatch effect_kind mismatch")
    payload = _normalize_effect_payload(dispatch["effect_payload"])
    stored_digest = str(dispatch["effect_payload_sha256"])
    if canonical_json_sha256(payload) != stored_digest:
        raise CursorEvidenceError("cursor dispatch payload digest mismatch")
    iteration = payload.get("iteration")
    if isinstance(iteration, bool) or not isinstance(iteration, (int, str)):
        raise CursorEvidenceError("cursor turn iteration invalid")
    iteration_number = int(iteration)
    if iteration_number < 1:
        raise CursorEvidenceError("cursor turn iteration invalid")
    return payload


def _fix_evidence_from_waiting(
    event: WaitingForCursorFixEnteredEvent,
    *,
    causal_sequence: int,
) -> _CorrectionFixEvidence:
    return _CorrectionFixEvidence(
        owner_run_id=event.run_id,
        review_iteration=event.review_iteration,
        fix_prompt_path=event.fix_prompt_path,
        fix_prompt_sha256=event.fix_prompt_sha256,
        correction_envelope_path=event.correction_envelope_path,
        correction_envelope_sha256=event.correction_envelope_sha256,
        review_result_path=None,
        review_result_sha256=None,
        causal_sequence=causal_sequence,
    )


def _fix_evidence_from_extension(
    event: ReviewBudgetExtendedEvent,
    *,
    causal_sequence: int,
) -> _CorrectionFixEvidence:
    return _CorrectionFixEvidence(
        owner_run_id=event.run_id,
        review_iteration=event.review_iteration,
        fix_prompt_path=event.fix_prompt_path,
        fix_prompt_sha256=event.fix_prompt_sha256,
        correction_envelope_path=event.correction_envelope_path,
        correction_envelope_sha256=event.correction_envelope_sha256,
        review_result_path=event.review_result_path,
        review_result_sha256=event.review_result_sha256,
        causal_sequence=causal_sequence,
    )


def _resolve_correction_fix_binding(
    events: list[sqlite3.Row],
    *,
    owner_run_id: str,
    block_sequence: int | None,
) -> _CorrectionFixEvidence | None:
    last_wait: WaitingForCursorFixEnteredEvent | None = None
    last_wait_sequence = -1
    last_extension: ReviewBudgetExtendedEvent | None = None
    last_extension_sequence = -1
    for row in events:
        sequence = int(row["sequence"])
        if block_sequence is not None and sequence >= block_sequence:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, WaitingForCursorFixEnteredEvent) and parsed.run_id == owner_run_id:
            last_wait = parsed
            last_wait_sequence = sequence
        if isinstance(parsed, ReviewBudgetExtendedEvent) and parsed.run_id == owner_run_id:
            last_extension = parsed
            last_extension_sequence = sequence
    if last_extension is not None and (
        last_wait is None or last_extension_sequence > last_wait_sequence
    ):
        return _fix_evidence_from_extension(last_extension, causal_sequence=last_extension_sequence)
    if last_wait is not None:
        return _fix_evidence_from_waiting(last_wait, causal_sequence=last_wait_sequence)
    return None


def _fix_from_matching_recovery_record(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    prompt_path: str,
    prompt_sha256: str,
) -> tuple[_CorrectionFixEvidence, list[sqlite3.Row]] | None:
    """Treat a failed recovered correction retry as the same causal correction."""

    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
        CORRECTION_RECORD_REL,
        CursorRecoveryRecordV2,
    )

    row = _correction_recovery_row(store, conn, run_id)
    if row is None or str(row["status"]) != "ready":
        return None
    record_sha = str(row["record_sha256"] or "")
    record_path = str(row["record_artifact_path"] or "")
    if not record_sha or record_path != CORRECTION_RECORD_REL:
        return None
    try:
        payload = artifacts.read_verified_bytes(
            run_id,
            record_path,
            expected_sha256=record_sha,
        )
        record = CursorRecoveryRecordV2.model_validate_json(payload)
    except (ProtectedArtifactError, OSError, ValidationError, UnicodeError) as exc:
        raise CursorEvidenceError("correction recovery record failed authentication") from exc
    if record.turn_kind != "correction" or record.raw_fix is None:
        return None
    if record.effective_prompt_sha256 != prompt_sha256:
        if not prompt_path:
            return None
        sent = artifacts.read_verified_bytes(
            run_id,
            prompt_path,
            expected_sha256=prompt_sha256,
        )
        effective = artifacts.read_verified_bytes(
            run_id,
            record.effective_prompt_path,
            expected_sha256=record.effective_prompt_sha256,
        )
        raw_fix = artifacts.read_verified_bytes(
            record.raw_fix.owner_run_id,
            record.raw_fix.relative_path,
            expected_sha256=record.raw_fix.sha256,
        )
        if effective not in sent or raw_fix not in effective:
            return None
    owner_events, complete = _load_all_verified_events(store, conn, record.raw_fix.owner_run_id)
    if not complete:
        raise CursorEvidenceError("incomplete event history")
    fix = _resolve_correction_fix_binding(
        owner_events,
        owner_run_id=record.raw_fix.owner_run_id,
        block_sequence=None,
    )
    if (
        fix is None
        or fix.fix_prompt_sha256 != record.raw_fix.sha256
        or fix.correction_envelope_sha256 != record.base_prompt.sha256
    ):
        return None
    return fix, owner_events


def _resolve_active_correction_fix(
    source_events: list[sqlite3.Row],
    inherited_events: list[sqlite3.Row],
    *,
    run_id: str,
    evidence_run_id: str,
    block_sequence: int | None,
) -> _CorrectionFixEvidence | None:
    on_source = _resolve_correction_fix_binding(
        source_events,
        owner_run_id=run_id,
        block_sequence=block_sequence,
    )
    if on_source is not None:
        return on_source
    if evidence_run_id == run_id:
        return None
    return _resolve_correction_fix_binding(
        inherited_events,
        owner_run_id=evidence_run_id,
        block_sequence=None,
    )


def _fix_binding_matches_waiting(
    event: WaitingForCursorFixEnteredEvent,
    fix: _CorrectionFixEvidence,
) -> bool:
    return (
        event.run_id == fix.owner_run_id
        and event.review_iteration == fix.review_iteration
        and event.fix_prompt_sha256 == fix.fix_prompt_sha256
        and event.correction_envelope_sha256 == fix.correction_envelope_sha256
    )


def _fix_binding_matches_extension(
    event: ReviewBudgetExtendedEvent,
    fix: _CorrectionFixEvidence,
) -> bool:
    return (
        event.run_id == fix.owner_run_id
        and event.review_iteration == fix.review_iteration
        and event.fix_prompt_sha256 == fix.fix_prompt_sha256
        and event.correction_envelope_sha256 == fix.correction_envelope_sha256
        and event.review_result_path == (fix.review_result_path or event.review_result_path)
        and event.review_result_sha256 == (fix.review_result_sha256 or event.review_result_sha256)
    )


def _row_matches_fix_binding(parsed: object, fix: _CorrectionFixEvidence) -> bool:
    if isinstance(parsed, WaitingForCursorFixEnteredEvent):
        return _fix_binding_matches_waiting(parsed, fix)
    if isinstance(parsed, ReviewBudgetExtendedEvent):
        return _fix_binding_matches_extension(parsed, fix)
    return False


def _scan_staging_before_correction(
    events: list[sqlite3.Row],
    *,
    before_sequence: int | None,
    max_sequence_exclusive: int | None,
) -> StagingCompletedEvent | None:
    staging_event: StagingCompletedEvent | None = None
    for row in events:
        sequence = int(row["sequence"])
        if max_sequence_exclusive is not None and sequence >= max_sequence_exclusive:
            continue
        if before_sequence is not None and sequence >= before_sequence:
            continue
        if str(row["event_kind"]) != STAGING_COMPLETED_EVENT_KIND:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, StagingCompletedEvent):
            staging_event = parsed
    return staging_event


def _resolve_staging_for_correction(
    event_sources: list[tuple[str, list[sqlite3.Row]]],
    *,
    fix: _CorrectionFixEvidence,
    block_sequence: int | None,
) -> tuple[StagingCompletedEvent | None, str | None]:
    for owner_run_id, events in event_sources:
        owner_max = block_sequence if owner_run_id == fix.owner_run_id else None
        before_sequence = fix.causal_sequence if owner_run_id == fix.owner_run_id else None
        staging = _scan_staging_before_correction(
            events,
            before_sequence=before_sequence,
            max_sequence_exclusive=owner_max,
        )
        if staging is not None:
            return staging, owner_run_id
    return None, None


def _resolve_codex_attempt_for_correction_review(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    events: list[sqlite3.Row],
    *,
    fix_owner_run_id: str,
    fix: _CorrectionFixEvidence,
    block_sequence: int | None,
    artifacts: _ArtifactReader,
) -> sqlite3.Row:
    target_review: CodexReviewCompletedEvent | None = None
    codex_attempt_ids: list[str] = []
    for row in events:
        sequence = int(row["sequence"])
        if block_sequence is not None and sequence >= block_sequence:
            continue
        kind = str(row["event_kind"])
        parsed = _parse_event_row(row)
        if kind == ATTEMPT_COMPLETED_EVENT_KIND and isinstance(parsed, AttemptCompletedEvent):
            if parsed.run_id != fix_owner_run_id:
                continue
            attempt = store.get_attempt_by_id(conn, parsed.attempt_id)
            if attempt is not None and str(attempt["component"]) == "codex":
                codex_attempt_ids.append(parsed.attempt_id)
            continue
        if kind != CODEX_REVIEW_COMPLETED_EVENT_KIND:
            continue
        if not isinstance(parsed, CodexReviewCompletedEvent):
            continue
        if parsed.run_id != fix_owner_run_id:
            continue
        if parsed.review_iteration != fix.review_iteration:
            continue
        if (
            fix.review_result_path is not None
            and parsed.review_result_path != fix.review_result_path
        ):
            continue
        if (
            fix.review_result_sha256 is not None
            and parsed.review_result_sha256 != fix.review_result_sha256
        ):
            continue
        target_review = parsed
    codex_root = artifacts.run_root(fix_owner_run_id)
    if target_review is not None:
        for attempt_id in reversed(codex_attempt_ids):
            attempt = store.get_attempt_by_id(conn, attempt_id)
            if attempt is None:
                continue
            try:
                outcome = load_authenticated_codex_outcome(
                    codex_root,
                    attempt_id=attempt_id,
                    unit_identity=str(attempt["unit_identity"]),
                    result_rel=str(attempt["result_artifact_path"]),
                    stdout_rel=str(attempt["stdout_artifact_path"]),
                    stderr_rel=str(attempt["stderr_artifact_path"]),
                    observed_exit_code=int(attempt["exit_code"])
                    if attempt["exit_code"] is not None
                    else None,
                    expected_envelope_sha256=str(attempt["completion_envelope_sha256"])
                    if attempt["completion_envelope_sha256"]
                    else None,
                    expected_dispatch_id=str(attempt["dispatch_id"]),
                    expected_effect_kind=None,
                )
            except (CodexEvidenceError, ValueError, OSError):
                continue
            review_path = str(outcome.get("review_result_path", "")).strip()
            review_sha = str(outcome.get("review_result_sha256", "")).strip()
            if (
                review_path == target_review.review_result_path
                and review_sha == target_review.review_result_sha256
            ):
                return attempt
        raise CursorEvidenceError("correction lacks authenticated codex review attempt binding")

    last_codex_attempt_id: str | None = None
    for row in events:
        sequence = int(row["sequence"])
        if block_sequence is not None and sequence >= block_sequence:
            continue
        kind = str(row["event_kind"])
        parsed = _parse_event_row(row)
        if kind == ATTEMPT_COMPLETED_EVENT_KIND and isinstance(parsed, AttemptCompletedEvent):
            if parsed.run_id != fix_owner_run_id:
                continue
            attempt = store.get_attempt_by_id(conn, parsed.attempt_id)
            if attempt is not None and str(attempt["component"]) == "codex":
                last_codex_attempt_id = parsed.attempt_id
            continue
        if sequence != fix.causal_sequence:
            continue
        if not _row_matches_fix_binding(parsed, fix):
            continue
        if last_codex_attempt_id is None:
            raise CursorEvidenceError("correction lacks causally linked codex review completion")
        attempt = store.get_attempt_by_id(conn, last_codex_attempt_id)
        if attempt is None:
            raise CursorEvidenceError("codex review attempt record missing")
        return attempt
    raise CursorEvidenceError("correction lacks causally linked codex review completion")


def _authenticate_failed_cursor_turn(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    *,
    evidence_run_id: str,
    attempt: sqlite3.Row,
) -> tuple[dict[str, object], dict[str, object], int]:
    attempt_id = str(attempt["attempt_id"])
    dispatch_id = str(attempt["dispatch_id"])
    run_root = artifacts.run_root(evidence_run_id)
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None:
        raise CursorEvidenceError("failed attempt must be cursor.run_turn")
    payload = _verify_authenticated_cursor_dispatch(
        dispatch,
        run_id=evidence_run_id,
        expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    iteration_raw = payload.get("iteration")
    if isinstance(iteration_raw, bool) or not isinstance(iteration_raw, (int, str)):
        raise CursorEvidenceError("cursor turn iteration invalid")
    iteration = int(iteration_raw)
    unit_identity = str(attempt["unit_identity"])
    launch_nonce = str(attempt["launch_nonce"])
    launch_intent_sha256 = str(attempt["launch_intent_sha256"])
    envelope_sha = attempt["completion_envelope_sha256"]
    exit_code = attempt["exit_code"]
    observed_exit = int(exit_code) if exit_code is not None else None
    observed_termination: TerminationClass | None = None
    termination_raw = attempt["termination_class"]
    if termination_raw is not None:
        try:
            observed_termination = TerminationClass(str(termination_raw))
        except ValueError as exc:
            raise CursorEvidenceError("attempt termination_class is invalid") from exc
    binding = verify_cursor_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=evidence_run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=launch_nonce,
        launch_intent_sha256=launch_intent_sha256,
        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    invocation_iteration = binding.get("iteration")
    if isinstance(invocation_iteration, bool) or not isinstance(invocation_iteration, (int, str)):
        raise CursorEvidenceError("invocation evidence iteration must be numeric")
    if int(invocation_iteration) != iteration:
        raise CursorEvidenceError("cursor dispatch iteration disagrees with invocation evidence")
    outcome = load_authenticated_cursor_outcome(
        run_root,
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        result_rel=str(attempt["result_artifact_path"]),
        stdout_rel=str(attempt["stdout_artifact_path"]),
        stderr_rel=str(attempt["stderr_artifact_path"]),
        observed_exit_code=observed_exit,
        observed_termination=observed_termination,
        expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
        expected_dispatch_id=dispatch_id,
        expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    if outcome.get("timed_out") is True or observed_exit == 124:
        raise CursorEvidenceError("cursor failure recovery does not apply to timeouts")
    if outcome.get("failure_code") == "cursor_usage_limit":
        raise CursorEvidenceError("cursor failure recovery does not apply to usage-limit failures")
    if observed_exit == 0:
        raise CursorEvidenceError("decisive Cursor failure requires a nonzero exit")
    return binding, outcome, iteration


def _scan_turn_context(
    events: list[sqlite3.Row],
    *,
    max_sequence_exclusive: int | None = None,
) -> tuple[
    StagingCompletedEvent | None,
    WaitingForCursorFixEnteredEvent | None,
    CodexReviewerBoundEvent | None,
]:
    staging_event: StagingCompletedEvent | None = None
    fix_event: WaitingForCursorFixEnteredEvent | None = None
    reviewer_bound: CodexReviewerBoundEvent | None = None
    for row in events:
        sequence = int(row["sequence"])
        if max_sequence_exclusive is not None and sequence >= max_sequence_exclusive:
            continue
        kind = str(row["event_kind"])
        if kind == STAGING_COMPLETED_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, StagingCompletedEvent):
                staging_event = parsed
        if kind == WAITING_FOR_CURSOR_FIX_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, WaitingForCursorFixEnteredEvent):
                fix_event = parsed
        if kind == CODEX_REVIEWER_BOUND_EVENT_KIND:
            parsed = _parse_event_row(row)
            if isinstance(parsed, CodexReviewerBoundEvent):
                reviewer_bound = parsed
    return staging_event, fix_event, reviewer_bound


def _resolve_cursor_chat_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    events: list[sqlite3.Row],
    *,
    block_sequence: int | None,
) -> tuple[CursorChatCreatedEvent | None, str | None]:
    for row in events:
        sequence = int(row["sequence"])
        if block_sequence is not None and sequence >= block_sequence:
            continue
        if str(row["event_kind"]) != CURSOR_CHAT_CREATED_EVENT_KIND:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, CursorChatCreatedEvent):
            return parsed, run_id
    for edge in _ancestor_edges(store, conn, run_id):
        ancestor_events, complete = _load_all_verified_events(store, conn, edge.source_run_id)
        if not complete:
            raise CursorEvidenceError("incomplete event history")
        for ancestor_row in ancestor_events:
            if str(ancestor_row["event_kind"]) != CURSOR_CHAT_CREATED_EVENT_KIND:
                continue
            parsed = _parse_event_row(ancestor_row)
            if isinstance(parsed, CursorChatCreatedEvent):
                return parsed, edge.source_run_id
    return None, None


def _resolve_reviewer_bound_for_run(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
    events: list[sqlite3.Row],
    *,
    block_sequence: int | None,
) -> tuple[CodexReviewerBoundEvent | None, str | None]:
    _, _, reviewer_bound = _scan_turn_context(
        events,
        max_sequence_exclusive=block_sequence,
    )
    if reviewer_bound is not None:
        return reviewer_bound, run_id
    for edge in _ancestor_edges(store, conn, run_id):
        ancestor_events, complete = _load_all_verified_events(store, conn, edge.source_run_id)
        if not complete:
            raise CursorEvidenceError("incomplete event history")
        _, _, ancestor_bound = _scan_turn_context(ancestor_events)
        if ancestor_bound is not None:
            return ancestor_bound, edge.source_run_id
    return None, None


def _ancestor_edges(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[RecoveryAncestorEdge, ...]:
    from ai_dev_loop.scheduler.application.recovery_ancestry import (
        RecoveryAncestryError,
        recovery_ancestor_edges,
    )

    try:
        return recovery_ancestor_edges(store, conn, run_id)
    except RecoveryAncestryError as exc:
        raise CursorEvidenceError(str(exc)) from exc


def _find_bootstrap_run_id(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    start_run_id: str,
) -> str:
    current = start_run_id
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        if store.get_codex_bootstrap_attempt(conn, current) is not None:
            return current
        edges = _ancestor_edges(store, conn, current)
        if not edges:
            break
        current = edges[0].source_run_id
    raise CursorEvidenceError("reviewer B bootstrap attempt is missing")


def _ledger_reviews_completed_with_ancestry(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> int:
    total = store.count_review_completion_events(conn, run_id)
    for edge in _ancestor_edges(store, conn, run_id):
        total += store.count_review_completion_events(conn, edge.source_run_id)
    return total


def _review_budget_extensions_with_ancestry(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> tuple[ReviewBudgetExtendedEvent, ...]:
    collected: list[ReviewBudgetExtendedEvent] = []
    runs = [run_id, *[edge.source_run_id for edge in _ancestor_edges(store, conn, run_id)]]
    for current in reversed(runs):
        collected.extend(load_review_budget_extensions(store, conn, current))
    return tuple(collected)


def _artifact_access_receipt(
    run_id: str,
    exc: Exception,
    *,
    sequence_id: str | None,
    ordinal: int | None,
    turn_kind: TurnKind | None,
) -> CursorRecoveryAnalysisResult | None:
    if isinstance(exc, OSError):
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="corrupt",
                turn_kind=turn_kind,
                reason_code="corrupt_artifacts_missing",
                safe_summary="Required completion artifact is missing or unreadable.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )
    if isinstance(exc, CodexEvidenceError):
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="corrupt",
                turn_kind=turn_kind,
                reason_code="corrupt_review_evidence",
                safe_summary="Review artifacts failed authentication for correction inspection.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )
    if isinstance(exc, CursorEvidenceError):
        message = str(exc).lower()
        evidence_status: EvidenceStatus = "corrupt"
        code = "corrupt_artifacts"
        if "envelope" in message or "fix prompt" in message:
            code = "corrupt_correction_evidence"
        elif "reviewer binding" in message or "bound reviewer" in message:
            evidence_status = "insufficient"
            code = "insufficient_reviewer_binding"
        elif "correction lacks" in message or "codex review" in message:
            evidence_status = "insufficient"
            code = "insufficient_correction_evidence"
        elif "staged patch" in message:
            code = "corrupt_correction_evidence"
        elif "dispatch" in message or "iteration disagrees" in message:
            code = "corrupt_dispatch_evidence"
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status=evidence_status,
                turn_kind=turn_kind,
                reason_code=code,
                safe_summary="Cursor failure artifacts failed authentication.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )
    if isinstance(exc, ValueError):
        message = str(exc)
        if "result envelope" in message:
            if "not valid JSON" in message or "schema" in message:
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="corrupt",
                        turn_kind=turn_kind,
                        reason_code="corrupt_completion_envelope",
                        safe_summary="Completion result envelope is malformed.",
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_completion_envelope",
                    safe_summary="Completion result envelope failed validation.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if "artifacts are missing" in message:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_artifacts_missing",
                    safe_summary="Completion stdout/stderr artifacts are missing.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if "artifact hash mismatch" in message or "hash mismatch" in message:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_artifacts_tampered",
                    safe_summary="Protected artifact content does not match its authenticated hash.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if message in {"artifact root missing", "runs artifact root missing"}:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=turn_kind,
                    reason_code="insufficient_artifact_store",
                    safe_summary="Protected artifact store is missing or incomplete for inspection.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if message == "run artifact root missing":
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_run_artifacts_missing",
                    safe_summary="Run artifact directory is missing from the protected store.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
    if isinstance(exc, ProtectedArtifactError):
        message = str(exc)
        if message == "artifact missing":
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_artifacts_missing",
                    safe_summary="Required protected artifact is missing.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if message in {"artifact hash mismatch", "artifact has unsafe permissions"}:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_artifacts_tampered",
                    safe_summary="Protected artifact content does not match its authenticated hash.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
    return None


def _sequence_leaf_matches(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    sequence_id: str,
) -> bool | None:
    try:
        store.require_sequence_schema(conn)
        loaded = store.load_validated_sequence_state(conn, sequence_id)
    except Exception:
        return None
    if isinstance(loaded, ActiveSequenceState):
        return loaded.current_run_id == run_id
    if isinstance(loaded, BlockedSequenceState):
        return loaded.current_run_id == run_id
    return None


def _verify_sequence_binding(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    binding: SequenceRunBinding,
    *,
    require_current_ordinal: bool = True,
) -> str | None:
    """Authenticate the frozen entry. Current-ordinal equality is fresh eligibility."""

    try:
        store.require_sequence_schema(conn)
        sequence_state = store.load_validated_sequence_state(conn, binding.sequence_id)
    except Exception:
        return "insufficient_sequence_evidence"
    if isinstance(sequence_state, AbortedSequenceState):
        return "ineligible_sequence_aborted"
    definition = getattr(sequence_state, "definition", None)
    if not isinstance(definition, PreparedSequenceDefinition):
        return "insufficient_sequence_evidence"
    if binding.ordinal < 1 or binding.ordinal > len(definition.entries):
        return "insufficient_sequence_ordinal"
    entry = definition.entries[binding.ordinal - 1]
    if frozen_entry_hash(entry) != binding.entry_hash:
        return "insufficient_sequence_entry_hash"
    if not require_current_ordinal:
        return None
    current_ordinal = getattr(sequence_state, "current_ordinal", None)
    if current_ordinal is not None and int(current_ordinal) != binding.ordinal:
        return "insufficient_sequence_ordinal"
    return None


def _admission_checkpoint_from_events(
    events: list[sqlite3.Row],
    *,
    authorized_at: str,
    authorized_controller_session_id: str | None,
) -> AdmittedRunCheckpoint | None:
    for row in events:
        if str(row["event_kind"]) != "worktree_admitted":
            continue
        _verify_event_row(row)
        payload = json.loads(str(row["event_payload"]))
        return AdmittedRunCheckpoint(
            authorized_at=authorized_at,
            authorized_controller_session_id=authorized_controller_session_id,
            admitted_at=str(row["created_at"]),
            admission_status_artifact_path=str(payload["admission_status_artifact_path"]),
            admission_status_sha256=str(payload["admission_status_sha256"]),
        )
    return None


def _authenticate_chat_binding(
    artifacts: _ArtifactReader,
    *,
    evidence_run_id: str,
    chat_event: CursorChatCreatedEvent,
    invocation_chat_id: str | None = None,
) -> str:
    raw = artifacts.read_verified_bytes(
        evidence_run_id,
        chat_event.chat_artifact_path,
        expected_sha256=chat_event.chat_artifact_sha256,
    )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorEvidenceError("cursor chat artifact is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise CursorEvidenceError("cursor chat artifact must be a JSON object")
    artifact_chat_id = str(payload.get("chat_id", "")).strip()
    if not artifact_chat_id or artifact_chat_id != chat_event.chat_id:
        raise CursorEvidenceError("cursor chat artifact chat_id mismatch")
    if invocation_chat_id is not None and artifact_chat_id != invocation_chat_id:
        raise CursorEvidenceError("cursor chat artifact does not match failed invocation")
    return artifact_chat_id


def _authenticate_frozen_submitted_bindings(
    artifacts: _ArtifactReader,
    *,
    evidence_run_id: str,
    context: SubmittedRunContext,
) -> None:
    artifacts.read_verified_bytes(
        evidence_run_id,
        context.plan_prompt.plan_artifact_path,
        expected_sha256=context.plan_prompt.plan_sha256,
    )
    artifacts.read_verified_bytes(
        evidence_run_id,
        context.plan_prompt.prompt_artifact_path,
        expected_sha256=context.plan_prompt.prompt_sha256,
    )
    artifacts.read_verified_bytes(
        evidence_run_id,
        context.effective_config.effective_config_artifact_path,
        expected_sha256=context.effective_config.effective_config_sha256,
    )
    artifacts.read_verified_bytes(
        evidence_run_id,
        context.effective_config.source_config_artifact_path,
        expected_sha256=context.effective_config.source_config_sha256,
    )
    if context.baseline_status_artifact_path and context.baseline_status_sha256:
        artifacts.read_verified_bytes(
            evidence_run_id,
            context.baseline_status_artifact_path,
            expected_sha256=context.baseline_status_sha256,
        )


def _authenticate_reviewer_b_binding(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    *,
    b_owner_run_id: str,
    reviewer_bound: CodexReviewerBoundEvent,
) -> ReviewerBootstrapProof:
    binding_bytes = artifacts.read_verified_bytes(
        b_owner_run_id,
        reviewer_bound.binding_artifact_path,
        expected_sha256=reviewer_bound.binding_artifact_sha256,
    )
    try:
        binding_payload = json.loads(binding_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CursorEvidenceError("reviewer binding artifact is not valid JSON") from exc
    if not isinstance(binding_payload, dict):
        raise CursorEvidenceError("reviewer binding artifact must be a JSON object")
    prefix = str(binding_payload.get("bootstrap_session_id_prefix", "")).strip()
    if not prefix or prefix != reviewer_bound.reviewer_session_id_prefix:
        raise CursorEvidenceError("reviewer binding artifact prefix disagrees with ledger event")
    with store.begin_read() as conn:
        bootstrap_run_id = _find_bootstrap_run_id(store, conn, b_owner_run_id)
        bootstrap_attempt = store.get_codex_bootstrap_attempt(conn, bootstrap_run_id)
    if bootstrap_attempt is None:
        raise CursorEvidenceError("reviewer B bootstrap attempt is missing")
    run_root = artifacts.run_root(bootstrap_run_id)
    attempt_id = str(bootstrap_attempt["attempt_id"])
    dispatch_id = str(bootstrap_attempt["dispatch_id"])
    unit_identity = str(bootstrap_attempt["unit_identity"])
    envelope_sha = bootstrap_attempt["completion_envelope_sha256"]
    exit_code = bootstrap_attempt["exit_code"]
    observed_exit = int(exit_code) if exit_code is not None else None
    with store.begin_read() as conn:
        dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None:
        raise CursorEvidenceError("reviewer bootstrap dispatch record missing")
    effect_kind = str(dispatch["effect_kind"])
    launch_intent_sha256 = str(bootstrap_attempt["launch_intent_sha256"])
    launch_nonce = str(bootstrap_attempt["launch_nonce"])
    invocation = verify_codex_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=bootstrap_run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=launch_nonce,
        launch_intent_sha256=launch_intent_sha256,
        effect_kind=effect_kind,
    )
    verify_pre_execution_codex_guards(run_root, invocation, run_id=bootstrap_run_id)
    outcome = load_authenticated_codex_outcome(
        run_root,
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        result_rel=str(bootstrap_attempt["result_artifact_path"]),
        stdout_rel=str(bootstrap_attempt["stdout_artifact_path"]),
        stderr_rel=str(bootstrap_attempt["stderr_artifact_path"]),
        observed_exit_code=observed_exit,
        expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
        expected_dispatch_id=dispatch_id,
        expected_effect_kind=effect_kind,
    )
    bootstrap_session_id = str(outcome.get("bootstrap_session_id", "")).strip()
    if not bootstrap_session_id or not bootstrap_session_id.startswith(prefix):
        raise CursorEvidenceError("bootstrap outcome session disagrees with bound reviewer")
    expected_events_sha = str(binding_payload.get("bootstrap_events_sha256", "")).strip()
    if not expected_events_sha:
        raise CursorEvidenceError("reviewer binding artifact missing bootstrap events digest")
    bootstrap_events_rel = str(outcome.get("events_path", "")).strip()
    if not bootstrap_events_rel:
        raise CursorEvidenceError("bootstrap outcome missing events artifact binding")
    events_bytes = artifacts.read_verified_bytes(
        bootstrap_run_id,
        bootstrap_events_rel,
        expected_sha256=expected_events_sha,
    )
    try:
        events_text = events_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorEvidenceError("bootstrap events artifact is not valid text") from exc
    capture = classify_bootstrap_session_id_from_text(events_text)
    authenticated_session_id = str(capture.session_id or "").strip()
    if capture.uncertainty_reason or not authenticated_session_id:
        raise CursorEvidenceError("bootstrap events do not authenticate a reviewer session")
    if authenticated_session_id != bootstrap_session_id or not authenticated_session_id.startswith(
        prefix
    ):
        raise CodexEvidenceError("bootstrap outcome session disagrees with authenticated events")
    return ReviewerBootstrapProof(
        session_id=authenticated_session_id,
        binding_run_id=b_owner_run_id,
        bootstrap_run_id=bootstrap_run_id,
        bootstrap_attempt_id=attempt_id,
        bootstrap_events_path=bootstrap_events_rel,
        bootstrap_events_sha256=expected_events_sha,
        binding_artifact_path=reviewer_bound.binding_artifact_path,
        binding_artifact_sha256=reviewer_bound.binding_artifact_sha256,
    )


def _authenticate_correction_turn_evidence(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    *,
    fix: _CorrectionFixEvidence,
    b_owner_run_id: str,
    reviewer_bound: CodexReviewerBoundEvent,
    staging_event: StagingCompletedEvent,
    staging_owner_run_id: str,
    codex_attempt: sqlite3.Row,
    cursor_iteration: int,
) -> tuple[ReviewerBootstrapProof, str, str]:
    if cursor_iteration <= fix.review_iteration:
        raise CursorEvidenceError("failed cursor iteration disagrees with correction review")
    fix_root = artifacts.run_root(fix.owner_run_id)
    artifacts.read_verified_bytes(
        fix.owner_run_id,
        fix.fix_prompt_path,
        expected_sha256=fix.fix_prompt_sha256,
    )
    artifacts.read_verified_bytes(
        fix.owner_run_id,
        fix.correction_envelope_path,
        expected_sha256=fix.correction_envelope_sha256,
    )
    verify_correction_envelope_binding(
        fix_root,
        envelope_path=fix.correction_envelope_path,
        envelope_sha256=fix.correction_envelope_sha256,
        fix_prompt_path=fix.fix_prompt_path,
        fix_prompt_sha256=fix.fix_prompt_sha256,
    )
    artifacts.read_verified_bytes(
        staging_owner_run_id,
        staging_event.staged_patch_path,
        expected_sha256=staging_event.staged_patch_sha256,
    )
    reviewer_proof = _authenticate_reviewer_b_binding(
        store,
        artifacts,
        b_owner_run_id=b_owner_run_id,
        reviewer_bound=reviewer_bound,
    )
    bound_reviewer_session_id = reviewer_proof.session_id
    fix_owner_run_id = fix.owner_run_id
    codex_root = artifacts.run_root(fix_owner_run_id)
    review_attempt_id = str(codex_attempt["attempt_id"])
    review_dispatch_id = str(codex_attempt["dispatch_id"])
    review_unit_identity = str(codex_attempt["unit_identity"])
    review_launch_nonce = str(codex_attempt["launch_nonce"])
    review_launch_intent_sha256 = str(codex_attempt["launch_intent_sha256"])
    with store.begin_read() as conn:
        review_dispatch = store.get_effect_by_dispatch_id(conn, review_dispatch_id)
    if review_dispatch is None:
        raise CursorEvidenceError("codex review dispatch record missing")
    review_effect_kind = str(review_dispatch["effect_kind"])
    review_invocation = verify_codex_invocation_evidence(
        codex_root,
        attempt_id=review_attempt_id,
        run_id=fix_owner_run_id,
        dispatch_id=review_dispatch_id,
        unit_identity=review_unit_identity,
        launch_nonce=review_launch_nonce,
        launch_intent_sha256=review_launch_intent_sha256,
        effect_kind=review_effect_kind,
    )
    verify_pre_execution_codex_guards(codex_root, review_invocation, run_id=fix_owner_run_id)
    pinned_session_id = str(review_invocation.get("reviewer_session_id", "")).strip()
    if review_effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND and (
        not pinned_session_id or pinned_session_id != bound_reviewer_session_id
    ):
        raise CursorEvidenceError("pinned invocation reviewer session disagrees with bootstrap")
    if review_effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND and pinned_session_id:
        raise CursorEvidenceError("bootstrap invocation must not pin a reviewer session")
    codex_outcome = load_authenticated_codex_outcome(
        codex_root,
        attempt_id=review_attempt_id,
        unit_identity=review_unit_identity,
        result_rel=str(codex_attempt["result_artifact_path"]),
        stdout_rel=str(codex_attempt["stdout_artifact_path"]),
        stderr_rel=str(codex_attempt["stderr_artifact_path"]),
        observed_exit_code=int(codex_attempt["exit_code"])
        if codex_attempt["exit_code"] is not None
        else None,
        expected_envelope_sha256=str(codex_attempt["completion_envelope_sha256"])
        if codex_attempt["completion_envelope_sha256"]
        else None,
        expected_dispatch_id=review_dispatch_id,
        expected_effect_kind=review_effect_kind,
    )
    validate_codex_review_outcome_integrity(
        codex_outcome,
        run_root=codex_root,
        expected_review_iteration=fix.review_iteration,
        expected_effect_kind=review_effect_kind,
        bound_session_id=bound_reviewer_session_id,
    )
    review_result_path = str(codex_outcome.get("review_result_path", "")).strip()
    review_result_sha = str(codex_outcome.get("review_result_sha256", "")).strip()
    if not review_result_path or not review_result_sha:
        raise CursorEvidenceError("codex review outcome lacks authenticated review result binding")
    artifacts.read_verified_bytes(
        fix_owner_run_id,
        review_result_path,
        expected_sha256=review_result_sha,
    )
    load_validated_review_result(codex_root, codex_outcome)
    return reviewer_proof, review_result_path, review_result_sha


def _review_budget_for_blocked_run(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    state: BlockedState,
) -> tuple[int, int]:
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
    from ai_dev_loop.scheduler.application.cursor_budget_carry import load_budget_carry_for_run
    from ai_dev_loop.scheduler.application.review_budget import fold_review_budget_extensions

    try:
        carry = load_budget_carry_for_run(
            store,
            artifacts,  # type: ignore[arg-type]
            conn,
            state.run_id,
        )
    except SchedulerEngineError as exc:
        raise CursorEvidenceError("review budget carry failed authentication") from exc
    if carry is not None:
        local_completed = store.count_review_completion_events(conn, state.run_id)
        local_extensions = load_review_budget_extensions(store, conn, state.run_id)
        ceiling = fold_review_budget_extensions(
            carry.inherited_effective_ceiling,
            local_extensions,
        )
        return carry.inherited_reviews_completed + local_completed, ceiling
    ledger_reviews_completed = _ledger_reviews_completed_with_ancestry(store, conn, state.run_id)
    extensions = _review_budget_extensions_with_ancestry(store, conn, state.run_id)
    reviews_completed, effective_ceiling, _submitted = review_budget_projection(
        state,
        extensions,
        ledger_reviews_completed=ledger_reviews_completed,
    )
    return reviews_completed, effective_ceiling


def analyze_cursor_recovery_evidence(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    run_id: str,
    *,
    event_page_size: int = EVENT_PAGE_SIZE,
    reservation_must_match_run: bool = True,
    require_current_sequence_leaf: bool = True,
) -> CursorRecoveryAnalysisResult:
    sequence_id: str | None = None
    ordinal: int | None = None

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)

        if not isinstance(state, BlockedState):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_not_blocked",
                    safe_summary="Run is not in blocked state; Cursor failure evidence does not apply.",
                )
            )
        _require_initial_ancestor_authority(store, artifacts, run_id)

        binding = state.context.sequence
        if binding is not None:
            sequence_id = binding.sequence_id
            ordinal = binding.ordinal
            sequence_issue = _verify_sequence_binding(
                store,
                conn,
                binding,
                require_current_ordinal=require_current_sequence_leaf,
            )
            if sequence_issue is not None:
                if sequence_issue == "ineligible_sequence_aborted":
                    return CursorRecoveryAnalysisResult(
                        receipt=receipt(
                            run_id,
                            evidence_status="ineligible",
                            turn_kind=None,
                            reason_code=sequence_issue,
                            safe_summary="Sequence was aborted; inspection cannot authenticate evidence.",
                            sequence_id=sequence_id,
                            ordinal=ordinal,
                        )
                    )
                if store.has_sequence_abort_requested_for_run(
                    conn,
                    run_id=run_id,
                    sequence_id=binding.sequence_id,
                ):
                    return CursorRecoveryAnalysisResult(
                        receipt=receipt(
                            run_id,
                            evidence_status="ineligible",
                            turn_kind=None,
                            reason_code="ineligible_sequence_abort_pending",
                            safe_summary="Sequence has a durable abort request.",
                            sequence_id=sequence_id,
                            ordinal=ordinal,
                        )
                    )
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="insufficient",
                        turn_kind=None,
                        reason_code=sequence_issue,
                        safe_summary="Sequence binding could not be authenticated for inspection.",
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )
            if store.has_sequence_abort_requested_for_run(
                conn,
                run_id=run_id,
                sequence_id=binding.sequence_id,
            ):
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="ineligible",
                        turn_kind=None,
                        reason_code="ineligible_sequence_abort_pending",
                        safe_summary="Sequence has a durable abort request.",
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )

        if state.block_reason_kind != "cursor_failure":
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_block_reason",
                    safe_summary=(
                        "Block reason is not cursor_failure; inspect the run's safe next action."
                    ),
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        if store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_active_process",
                    safe_summary="Run still has a nonterminal attempt; termination is not confirmed.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if store.has_abort_requested_for_run(conn, run_id):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_abort_pending",
                    safe_summary="Run has a durable abort request.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if store.has_checkpoint_reconciliation_hold(conn, run_id):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_checkpoint_hold",
                    safe_summary="Run has unresolved checkpoint reconciliation.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if store.has_unresolved_abort_hold(conn, run_id):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_abort_hold",
                    safe_summary="Run has unresolved abort reconciliation.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        worktree_key = state.context.repository.worktree_key
        active = store.get_active_reservation(conn, worktree_key)
        if (
            reservation_must_match_run
            and active is not None
            and str(active["run_id"]) != run_id
        ):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=None,
                    reason_code="ineligible_reservation_conflict",
                    safe_summary="Another run holds the repository reservation for this worktree.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        evidence_run_id = resolve_recovery_ledger_evidence_run_id(store, conn, run_id)
        from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

        try:
            source_events, history_complete = _load_all_verified_events(
                store,
                conn,
                run_id,
                page_size=event_page_size,
            )
        except SchedulerEngineError as exc:
            if "digest" not in str(exc):
                raise
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=None,
                    reason_code="corrupt_event_page",
                    safe_summary="Event page failed authentication.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if not history_complete:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=None,
                    reason_code="incomplete_event_history",
                    safe_summary="Event history pagination did not complete; causal evidence is incomplete.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        review_recovery_source = store.get_review_recovery_source_for_successor(
            conn,
            successor_run_id=run_id,
        )
        try:
            failed_attempt_id, causal_detail, block_sequence = (
                resolve_authenticated_decisive_attempt(
                    store,
                    artifacts,
                    conn,
                    run_id,
                    event_page_size=event_page_size,
                )
            )
        except CursorEvidenceError as exc:
            if str(exc) == "event history pagination did not complete":
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="insufficient",
                        turn_kind=None,
                        reason_code="incomplete_event_history",
                        safe_summary=(
                            "Event history pagination did not complete; causal evidence is incomplete."
                        ),
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )
            if str(exc) == "event page failed authentication":
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="corrupt",
                        turn_kind=None,
                        reason_code="corrupt_event_page",
                        safe_summary="Event page failed authentication.",
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=None,
                    reason_code="corrupt_artifacts",
                    safe_summary="Usage-limit continuation evidence failed authentication.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        except (
            CodexEvidenceError,
            ProtectedArtifactError,
            ValidationError,
            ValueError,
            OSError,
        ):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=None,
                    reason_code="corrupt_artifacts",
                    safe_summary="Usage-limit continuation evidence failed authentication.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        if failed_attempt_id is None:
            code = {
                "ambiguous_failed_attempts": "insufficient_evidence_ambiguous_failure",
                "ambiguous_cursor_failure_blocks": "insufficient_evidence_ambiguous_failure",
            }.get(causal_detail, "insufficient_evidence_history")
            summary = {
                "no_cursor_failure_block_in_history": (
                    "Ledger lacks an authenticated cursor_failure block for this run."
                ),
                "no_failed_attempt_precedes_block": (
                    "cursor_failure block is not linked to a preceding failed cursor.run_turn."
                ),
                "ambiguous_failed_attempts": (
                    "Multiple failed cursor turns precede the block; causality is ambiguous."
                ),
                "ambiguous_cursor_failure_blocks": (
                    "Multiple cursor_failure blocks exist; causality is ambiguous."
                ),
            }.get(causal_detail, "Causal cursor failure evidence is insufficient.")
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=None,
                    reason_code=code,
                    safe_summary=summary,
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        attempt = store.get_attempt_by_id(conn, failed_attempt_id)
        if attempt is None:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=None,
                    reason_code="corrupt_ledger",
                    safe_summary="Decisive failed attempt record is missing from the ledger.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

        try:
            reviews_completed, effective_ceiling = _review_budget_for_blocked_run(
                store,
                artifacts,
                conn,
                state,
            )
        except CursorEvidenceError:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=None,
                    reason_code="corrupt_review_evidence",
                    safe_summary="Review budget evidence failed authentication.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        inherited_events, inherited_complete = _load_all_verified_events(
            store,
            conn,
            evidence_run_id,
            page_size=event_page_size,
        )
        if not inherited_complete:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=None,
                    reason_code="incomplete_event_history",
                    safe_summary="Inherited event history is incomplete for frozen-input inspection.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

    fix_binding = _resolve_active_correction_fix(
        source_events,
        inherited_events,
        run_id=run_id,
        evidence_run_id=evidence_run_id,
        block_sequence=block_sequence,
    )
    with store.begin_read() as conn:
        reviewer_bound_event, b_owner_run_id = _resolve_reviewer_bound_for_run(
            store,
            conn,
            evidence_run_id,
            inherited_events,
            block_sequence=None,
        )
        if reviewer_bound_event is None:
            reviewer_bound_event, b_owner_run_id = _resolve_reviewer_bound_for_run(
                store,
                conn,
                run_id,
                source_events,
                block_sequence=block_sequence,
            )
        if reviewer_bound_event is not None:
            b_owner_run_id = reviewer_bound_event.run_id
    turn_kind: TurnKind = "initial"
    if fix_binding is not None or review_recovery_source is not None:
        turn_kind = "correction"
    if turn_kind == "correction" and reviewer_bound_event is None:
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="insufficient",
                turn_kind=None,
                reason_code="insufficient_reviewer_binding",
                safe_summary="Correction turn requires authenticated reviewer B binding evidence.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )

    artifact_run_id = run_id
    try:
        binding_evidence, _outcome, iteration = _authenticate_failed_cursor_turn(
            store,
            artifacts,
            evidence_run_id=artifact_run_id,
            attempt=attempt,
        )
    except (
        CursorEvidenceError,
        CodexEvidenceError,
        ProtectedArtifactError,
        ValueError,
        OSError,
    ) as exc:
        mapped = _artifact_access_receipt(
            run_id,
            exc,
            sequence_id=sequence_id,
            ordinal=ordinal,
            turn_kind=turn_kind,
        )
        if mapped is not None:
            return mapped
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="corrupt",
                turn_kind=turn_kind,
                reason_code="corrupt_artifacts",
                safe_summary="Cursor failure artifacts failed authentication.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )

    fix_owner_events: list[sqlite3.Row] | None = None
    if turn_kind == "initial" and fix_binding is None:
        with store.begin_read() as conn:
            recovered = _fix_from_matching_recovery_record(
                store,
                artifacts,
                conn,
                run_id=run_id,
                prompt_path=str(binding_evidence.get("prompt_path") or ""),
                prompt_sha256=str(binding_evidence.get("prompt_sha256") or ""),
            )
        if recovered is not None:
            fix_binding, fix_owner_events = recovered
            turn_kind = "correction"

    with store.begin_read() as conn:
        chat_event, chat_owner_run_id = _resolve_cursor_chat_for_run(
            store,
            conn,
            run_id,
            source_events,
            block_sequence=block_sequence,
        )
        if chat_event is None:
            chat_event, chat_owner_run_id = _resolve_cursor_chat_for_run(
                store,
                conn,
                evidence_run_id,
                inherited_events,
                block_sequence=None,
            )
    if chat_event is None or chat_owner_run_id is None:
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="insufficient",
                turn_kind=turn_kind,
                reason_code="insufficient_chat_binding",
                safe_summary="Blocked source lacks authenticated Cursor chat creation evidence.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )

    invocation_chat_id = str(binding_evidence.get("chat_id", "")).strip() or None
    try:
        _authenticate_chat_binding(
            artifacts,
            evidence_run_id=chat_owner_run_id,
            chat_event=chat_event,
            invocation_chat_id=invocation_chat_id,
        )
        context = state.context
        _authenticate_frozen_submitted_bindings(
            artifacts,
            evidence_run_id=run_id,
            context=context,
        )
    except CursorEvidenceError:
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="corrupt",
                turn_kind=turn_kind,
                reason_code="corrupt_frozen_inputs",
                safe_summary="Frozen submitted inputs failed authentication.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )
    except (ProtectedArtifactError, ValueError) as exc:
        mapped = _artifact_access_receipt(
            run_id,
            exc,
            sequence_id=sequence_id,
            ordinal=ordinal,
            turn_kind=turn_kind,
        )
        if mapped is not None:
            return mapped
        return CursorRecoveryAnalysisResult(
            receipt=receipt(
                run_id,
                evidence_status="corrupt",
                turn_kind=turn_kind,
                reason_code="corrupt_frozen_inputs",
                safe_summary="Frozen submitted inputs failed authentication.",
                sequence_id=sequence_id,
                ordinal=ordinal,
            )
        )

    reviewer_proof: ReviewerBootstrapProof | None = None
    captured_fix: _CorrectionFixEvidence | None = None
    captured_staging: StagingCompletedEvent | None = None
    captured_staging_owner: str | None = None
    captured_review_result_path = ""
    captured_review_result_sha = ""
    if turn_kind == "correction":
        if fix_binding is None:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=turn_kind,
                    reason_code="insufficient_correction_envelope",
                    safe_summary="Correction turn lacks waiting_for_cursor_fix evidence.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        assert reviewer_bound_event is not None
        b_owner_run_id = reviewer_bound_event.run_id
        if fix_owner_events is not None:
            fix_context_events = fix_owner_events
        else:
            fix_context_events = (
                source_events if fix_binding.owner_run_id == run_id else inherited_events
            )
        staging_sources: list[tuple[str, list[sqlite3.Row]]] = []
        if evidence_run_id != fix_binding.owner_run_id:
            staging_sources.append((evidence_run_id, inherited_events))
        staging_sources.append((fix_binding.owner_run_id, fix_context_events))
        causal_block = block_sequence if fix_binding.owner_run_id == run_id else None
        staging_for_fix, staging_owner_run_id = _resolve_staging_for_correction(
            staging_sources,
            fix=fix_binding,
            block_sequence=causal_block,
        )
        if staging_for_fix is None or staging_owner_run_id is None:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=turn_kind,
                    reason_code="insufficient_correction_evidence",
                    safe_summary="Correction lacks authenticated staging or review evidence.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        with store.begin_read() as conn:
            try:
                codex_attempt = _resolve_codex_attempt_for_correction_review(
                    store,
                    conn,
                    fix_context_events,
                    fix_owner_run_id=fix_binding.owner_run_id,
                    fix=fix_binding,
                    block_sequence=causal_block,
                    artifacts=artifacts,
                )
            except CursorEvidenceError as exc:
                mapped = _artifact_access_receipt(
                    run_id,
                    exc,
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                    turn_kind=turn_kind,
                )
                if mapped is not None:
                    return mapped
                return CursorRecoveryAnalysisResult(
                    receipt=receipt(
                        run_id,
                        evidence_status="insufficient",
                        turn_kind=turn_kind,
                        reason_code="insufficient_correction_evidence",
                        safe_summary="Correction lacks causally linked Codex review evidence.",
                        sequence_id=sequence_id,
                        ordinal=ordinal,
                    )
                )
        try:
            correction_proof_result = _authenticate_correction_turn_evidence(
                store,
                artifacts,
                fix=fix_binding,
                b_owner_run_id=b_owner_run_id,
                reviewer_bound=reviewer_bound_event,
                staging_event=staging_for_fix,
                staging_owner_run_id=staging_owner_run_id,
                codex_attempt=codex_attempt,
                cursor_iteration=iteration,
            )
        except (
            CursorEvidenceError,
            CodexEvidenceError,
            ProtectedArtifactError,
            ValueError,
            OSError,
        ) as exc:
            mapped = _artifact_access_receipt(
                run_id,
                exc,
                sequence_id=sequence_id,
                ordinal=ordinal,
                turn_kind=turn_kind,
            )
            if mapped is not None:
                return mapped
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_correction_evidence",
                    safe_summary="Correction or reviewer artifacts failed authentication.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        reviewer_proof, captured_review_result_path, captured_review_result_sha = (
            correction_proof_result
        )
        captured_fix = fix_binding
        captured_staging = staging_for_fix
        captured_staging_owner = staging_owner_run_id
        binding_path = str(binding_evidence.get("prompt_path", "")).strip()
        binding_sha = str(binding_evidence.get("prompt_sha256", "")).strip()
        if fix_owner_events is not None:
            prompt_path = binding_path
            prompt_sha = binding_sha
        else:
            prompt_path = fix_binding.correction_envelope_path
            prompt_sha = fix_binding.correction_envelope_sha256
        if fix_owner_events is None and (binding_path != prompt_path or binding_sha != prompt_sha):
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_invocation_prompt",
                    safe_summary="Failed invocation prompt does not match correction envelope.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
    else:
        prompt_path = str(binding_evidence.get("prompt_path", "")).strip()
        prompt_sha = str(binding_evidence.get("prompt_sha256", "")).strip()
        if not prompt_path or not prompt_sha:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_invocation_prompt",
                    safe_summary="Failed invocation lacks authenticated prompt binding.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, BlockedState)
        with store.begin_read() as conn:
            chat_owner_events, history_complete = _load_all_verified_events(
                store,
                conn,
                chat_owner_run_id,
            )
        if not history_complete:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="insufficient",
                    turn_kind=turn_kind,
                    reason_code="incomplete_event_history",
                    safe_summary="Chat owner event history is incomplete.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )
        checkpoint = _admission_checkpoint_from_events(
            chat_owner_events,
            authorized_at=state.authorized_at or state.blocked_at,
            authorized_controller_session_id=state.authorized_controller_session_id,
        )
        try:
            identity = frozen_repository_identity(
                state.context,
                run_id=chat_owner_run_id,
                artifacts=artifacts,  # type: ignore[arg-type]
                checkpoint=checkpoint,
            )
            if require_current_sequence_leaf:
                validate_frozen_repository_identity(
                    Path(state.context.repository.root),
                    identity,
                    context="cursor recovery evidence",
                )
            else:
                from ai_dev_loop.runners.git import validate_repository_layout

                validate_repository_layout(
                    Path(state.context.repository.root),
                    expected_root=identity.root,
                    expected_git_common_dir=identity.git_common_dir,
                    expected_git_dir=identity.git_dir,
                    expected_branch=identity.branch,
                    context="cursor recovery evidence",
                )
        except CursorEvidenceError:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="corrupt",
                    turn_kind=turn_kind,
                    reason_code="corrupt_repository_identity",
                    safe_summary="Frozen repository identity check failed.",
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

    sequence_leaf_matches: bool | None = None
    if sequence_id is not None:
        with store.begin_read() as conn:
            sequence_leaf_matches = _sequence_leaf_matches(
                store,
                conn,
                run_id=run_id,
                sequence_id=sequence_id,
            )
        if require_current_sequence_leaf and sequence_leaf_matches is False:
            return CursorRecoveryAnalysisResult(
                receipt=receipt(
                    run_id,
                    evidence_status="ineligible",
                    turn_kind=turn_kind,
                    reason_code="ineligible_sequence_stale_leaf",
                    safe_summary=(
                        "Sequence current leaf no longer matches this run; concurrent sequence "
                        "progress may have advanced."
                    ),
                    sequence_id=sequence_id,
                    ordinal=ordinal,
                )
            )

    bundle = CursorRecoveryEvidenceBundle(
        run_id=run_id,
        evidence_run_id=evidence_run_id,
        failed_attempt_id=failed_attempt_id,
        dispatch_id=str(attempt["dispatch_id"]),
        iteration=iteration,
        turn_kind=turn_kind,
        chat_id=chat_event.chat_id,
        prompt_path=prompt_path,
        prompt_sha256=prompt_sha,
        reviews_completed=reviews_completed,
        effective_review_ceiling=effective_ceiling,
        reviewer_bound=reviewer_bound_event is not None,
        sequence_id=sequence_id,
        ordinal=ordinal,
        sequence_leaf_matches=sequence_leaf_matches,
        chat_owner_run_id=chat_owner_run_id,
        chat_artifact_path=chat_event.chat_artifact_path,
        chat_artifact_sha256=chat_event.chat_artifact_sha256,
        submitted_max_review_iterations=state.context.workflow.max_review_iterations,
        fix_owner_run_id=None if captured_fix is None else captured_fix.owner_run_id,
        fix_prompt_path=None if captured_fix is None else captured_fix.fix_prompt_path,
        fix_prompt_sha256=None if captured_fix is None else captured_fix.fix_prompt_sha256,
        staged_owner_run_id=captured_staging_owner,
        staged_patch_path=None if captured_staging is None else captured_staging.staged_patch_path,
        staged_patch_sha256=(
            None if captured_staging is None else captured_staging.staged_patch_sha256
        ),
        review_result_owner_run_id=None if captured_fix is None else captured_fix.owner_run_id,
        review_result_path=captured_review_result_path or None,
        review_result_sha256=captured_review_result_sha or None,
        reviewer_session_id=None if reviewer_proof is None else reviewer_proof.session_id,
        reviewer_binding_run_id=None if reviewer_proof is None else reviewer_proof.binding_run_id,
        reviewer_bootstrap_run_id=(
            None if reviewer_proof is None else reviewer_proof.bootstrap_run_id
        ),
        binding_artifact_path=(
            None if reviewer_proof is None else reviewer_proof.binding_artifact_path
        ),
        binding_artifact_sha256=(
            None if reviewer_proof is None else reviewer_proof.binding_artifact_sha256
        ),
        bootstrap_attempt_id=None if reviewer_proof is None else reviewer_proof.bootstrap_attempt_id,
        bootstrap_events_path=(
            None if reviewer_proof is None else reviewer_proof.bootstrap_events_path
        ),
        bootstrap_events_sha256=(
            None if reviewer_proof is None else reviewer_proof.bootstrap_events_sha256
        ),
        admitted_owner_run_id=chat_owner_run_id if checkpoint is not None else None,
        admitted_artifact_path=(
            None if checkpoint is None else checkpoint.admission_status_artifact_path
        ),
        admitted_artifact_sha256=(
            None if checkpoint is None else checkpoint.admission_status_sha256
        ),
    )
    kind_label = "initial" if turn_kind == "initial" else "correction"
    sequence_leaf_ok = sequence_id is None or sequence_leaf_matches is True
    initial_supported = (
        turn_kind == "initial"
        and sequence_leaf_ok
        and reviewer_bound_event is None
        and reviews_completed == 0
        and review_recovery_source is None
    )
    correction_supported = (
        turn_kind == "correction"
        and sequence_leaf_ok
        and reviewer_proof is not None
        and captured_fix is not None
        and captured_staging is not None
    )
    mutation_supported = initial_supported or correction_supported
    if initial_supported and sequence_id is not None:
        support_text = "forced initial sequence recovery is supported."
    elif correction_supported and sequence_id is not None:
        support_text = "forced correction sequence recovery is supported."
    elif initial_supported:
        support_text = "forced initial standalone recovery is supported."
    elif correction_supported:
        support_text = "forced correction standalone recovery is supported."
    else:
        support_text = "forced recovery is not supported for this turn."
    return CursorRecoveryAnalysisResult(
        receipt=receipt(
            run_id,
            evidence_status="authenticated",
            turn_kind=turn_kind,
            reason_code="authenticated_cursor_failure",
            safe_summary=(
                f"Authenticated decisive {kind_label} cursor_failure at iteration {iteration}; "
                f"{support_text}"
            ),
            sequence_id=sequence_id,
            ordinal=ordinal,
            recovery_supported=mutation_supported,
        ),
        evidence=bundle,
    )
