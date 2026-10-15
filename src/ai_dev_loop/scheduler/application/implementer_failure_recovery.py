"""Standalone Codex implementer generic-failure recovery and publication."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.runners.probes import capture_git_status
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_authenticated_codex_implementer_outcome,
    verify_codex_implementer_invocation_evidence,
    verify_pre_execution_implementer_guards,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.cursor_evidence import CursorEvidenceError
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    _authenticate_correction_turn_evidence,
    _load_all_verified_events,
    _resolve_active_correction_fix,
    _resolve_codex_attempt_for_correction_review,
    _resolve_reviewer_bound_for_run,
    _resolve_staging_for_correction,
    _review_budget_for_blocked_run,
)
from ai_dev_loop.scheduler.application.recovery_ancestry import (
    RecoveryAncestryError,
    recovery_ancestor_edges,
)
from ai_dev_loop.scheduler.application.worktree_boundary import worktree_content_fingerprint
from ai_dev_loop.scheduler.domain.codex_contract import (
    IMPLEMENTER_ATTEMPT_EFFECT_KINDS,
    RESUME_CODEX_IMPLEMENTER_EFFECT_ID,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    codex_implementer_session_rel,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    AncestorEdgeV2,
    ArtifactBindingV2,
    BoundReviewerEvidenceV2,
    ReviewerNotCreatedV2,
)
from ai_dev_loop.scheduler.domain.events import (
    CODEX_IMPLEMENTER_BLOCKED_EVENT_KIND,
    CodexImplementerBlockedEvent,
    CodexImplementerRecoverySuccessorCreatedEvent,
    CodexImplementerTimeoutRetryEvent,
    CodexReviewerBoundEvent,
    WorktreeAdmittedEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.implementer_recovery import (
    IMPLEMENTER_RECOVERY_INTENT_REL,
    IMPLEMENTER_RECOVERY_INTENT_SCHEMA_VERSION,
    IMPLEMENTER_RECOVERY_RECORD_REL,
    IMPLEMENTER_RECOVERY_RECORD_SCHEMA_VERSION,
    MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES,
    CodexImplementerIdentityV1,
    CodexImplementerRecoveryIntentV1,
    CodexImplementerRecoveryRecordV1,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
    PreflightCompleteState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    CODEX_IMPLEMENTER_RECOVERY_SCHEMA_VERSION,
    SqliteSchedulerStore,
)
from ai_dev_loop.state import utc_now

_RECOVERY_REASON = "codex_implementer_failure"


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def _conflict(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CONFLICT, message)


def _corrupt(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, message)


def implementer_recovery_key(source_run_id: str, failed_attempt_id: str) -> str:
    return hashlib.sha256(
        f"codex-implementer-recovery-v1:{source_run_id}:{failed_attempt_id}".encode()
    ).hexdigest()


def _parse_verified_event(row: sqlite3.Row) -> object:
    try:
        return parse_scheduler_event(json.loads(str(row["event_payload"])))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise _corrupt("implementer recovery event payload is invalid") from exc


def _successor_run_id(project_name: str, now: datetime) -> str:
    timestamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{project_name}-implementer-recovery-{timestamp}-{secrets.token_hex(4)}"


@dataclass(frozen=True)
class _Evidence:
    state: BlockedState
    attempt_id: str
    dispatch_id: str
    turn: str
    iteration: int
    prompt_path: str
    prompt_sha256: str
    invocation_sha256: str
    invocation_artifact_path: str
    invocation_artifact_sha256: str
    completion_sha256: str
    completion_artifact_path: str
    implementer_session_id: str
    implementer_binding_sha256: str
    implementer_bootstrap_attempt_id: str
    admitted_path: str
    admitted_sha256: str
    partial_work_status_path: str
    partial_work_status_sha256: str
    partial_work_fingerprint_sha256: str
    reviews_completed: int
    effective_ceiling: int
    timeout_automatic_retries_consumed: int
    reviewer: BoundReviewerEvidenceV2 | ReviewerNotCreatedV2
    raw_fix: ArtifactBindingV2 | None
    staged_patch: ArtifactBindingV2 | None
    review_result: ArtifactBindingV2 | None
    parent: CodexImplementerRecoveryRecordV1 | None


def _verified_recovery_intent(row: sqlite3.Row) -> CodexImplementerRecoveryIntentV1:
    payload = str(row["intent_payload"]).encode("utf-8")
    if hashlib.sha256(payload).hexdigest() != str(row["intent_payload_sha256"]):
        raise _corrupt("implementer recovery intent digest mismatch")
    try:
        intent = CodexImplementerRecoveryIntentV1.model_validate_json(payload)
    except ValidationError as exc:
        raise _corrupt("implementer recovery intent is invalid") from exc
    if intent.canonical_bytes() != payload:
        raise _corrupt("implementer recovery intent is not canonical")
    if (
        intent.recovery_key != str(row["recovery_key"])
        or intent.source_run_id != str(row["source_run_id"])
        or intent.successor_run_id != str(row["successor_run_id"])
        or intent.failed_attempt_id != str(row["failed_attempt_id"])
        or intent.failed_dispatch_id != str(row["failed_dispatch_id"])
    ):
        raise _corrupt("implementer recovery intent disagrees with the ledger relation")
    return intent


def _authenticated_timeout_consumption(
    events: list[sqlite3.Row],
    *,
    run_id: str,
    turn: str,
    iteration: int,
    parent: CodexImplementerRecoveryRecordV1 | None,
) -> int:
    """Fold authenticated timeout policy events for one exact logical turn."""

    consumed = (
        parent.timeout_automatic_retries_consumed
        if parent is not None and parent.turn_kind == turn and parent.iteration == iteration
        else 0
    )
    for row in events:
        parsed = _parse_verified_event(row)
        if not isinstance(parsed, CodexImplementerTimeoutRetryEvent):
            continue
        if parsed.run_id != run_id:
            raise _corrupt("timeout retry event run binding is invalid")
        if parsed.turn != turn or parsed.iteration != iteration:
            continue
        if parsed.action == "automatic":
            if consumed >= 3 or parsed.automatic_retries != consumed + 1:
                raise _corrupt("automatic timeout retry consumption is not contiguous")
        elif parsed.action == "exhausted":
            if consumed != 3 or parsed.automatic_retries != consumed:
                raise _corrupt("timeout exhaustion disagrees with consumed policy")
        elif parsed.automatic_retries not in {consumed, max(consumed - 1, 0)}:
            raise _corrupt("manual timeout retry consumption is not contiguous")
        consumed = parsed.automatic_retries
    return consumed


def load_implementer_recovery_record(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> CodexImplementerRecoveryRecordV1 | None:
    """Authenticate the ready immutable recovery authority for ``run_id``."""

    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < (
        CODEX_IMPLEMENTER_RECOVERY_SCHEMA_VERSION
    ):
        return None
    row = store.get_codex_implementer_recovery_by_successor(
        conn,
        successor_run_id=run_id,
    )
    if row is None:
        return None
    if str(row["status"]) != "ready":
        raise _invalid("implementer recovery publication is not dispatchable")
    intent = _verified_recovery_intent(row)
    record_sha = str(row["record_sha256"] or "")
    record_path = str(row["record_artifact_path"] or "")
    if record_path != IMPLEMENTER_RECOVERY_RECORD_REL or not record_sha:
        raise _corrupt("implementer recovery record binding is missing")
    try:
        payload = artifacts.read_verified_bytes(
            run_id,
            record_path,
            expected_sha256=record_sha,
        )
        record = CodexImplementerRecoveryRecordV1.model_validate_json(payload)
    except (ProtectedArtifactError, OSError, UnicodeError, ValidationError) as exc:
        raise _corrupt("implementer recovery record failed authentication") from exc
    if record.canonical_bytes() != payload or record != intent.record:
        raise _corrupt("implementer recovery record disagrees with publication intent")
    if record.parent_recovery_key is not None:
        parent_row = store.get_codex_implementer_recovery_by_successor(
            conn,
            successor_run_id=record.source_run_id,
        )
        if (
            parent_row is None
            or str(parent_row["status"]) != "ready"
            or str(parent_row["source_run_id"]) != record.parent_source_run_id
            or str(parent_row["recovery_key"]) != record.parent_recovery_key
            or str(parent_row["record_sha256"] or "") != record.parent_record_sha256
        ):
            raise _corrupt("implementer recovery parent authority is invalid")
    return record


def implementer_recovery_blocks_dispatch(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < (
        CODEX_IMPLEMENTER_RECOVERY_SCHEMA_VERSION
    ):
        return False
    row = store.get_codex_implementer_recovery_by_successor(
        conn,
        successor_run_id=run_id,
    )
    return row is not None and str(row["status"]) != "ready"


def authenticate_implementer_recovery_dispatch(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    state: PreflightCompleteState | WaitingForCursorFixState,
    *,
    effect_payload: dict[str, object],
) -> CodexImplementerRecoveryRecordV1 | None:
    """Re-authenticate recovery identity, prompt, and relation just before launch."""

    record = load_implementer_recovery_record(store, artifacts, conn, state.run_id)
    if record is None:
        return None
    effect_turn = effect_payload.get("turn")
    effect_iteration = effect_payload.get("iteration")
    recovery_fields = {
        "implementer_recovery_key": record.recovery_key,
        "implementer_recovery_source": record.source_run_id,
        "implementer_recovery_attempt": record.failed_attempt_id,
    }
    has_recovery_fields = any(key in effect_payload for key in recovery_fields)
    if effect_turn != record.turn_kind or effect_iteration != record.iteration:
        if has_recovery_fields:
            raise _invalid("stale implementer recovery authority cannot authorize a later turn")
        return None
    if effect_payload.get("prompt_path") != record.exact_prompt.relative_path:
        raise _invalid("implementer recovery effect disagrees with immutable authority")
    codex = state.codex or CodexWorkflowCheckpoint()
    if has_recovery_fields:
        if any(effect_payload.get(key) != value for key, value in recovery_fields.items()):
            raise _invalid("implementer recovery effect disagrees with immutable authority")
    else:
        capacity = codex.capacity_continuation
        timeout = codex.implementer_timeout_continuation
        capacity_matches = (
            capacity is not None
            and capacity.role == "implementer"
            and capacity.turn == record.turn_kind
            and capacity.prompt_artifact_path == record.exact_prompt.relative_path
            and capacity.prompt_sha256 == record.exact_prompt.sha256
            and capacity.session_id == record.implementer.session_id
        )
        timeout_matches = (
            timeout is not None
            and timeout.turn == record.turn_kind
            and timeout.iteration == record.iteration
            and timeout.prompt_artifact_path == record.exact_prompt.relative_path
            and timeout.prompt_sha256 == record.exact_prompt.sha256
            and timeout.session_id == record.implementer.session_id
        )
        if not capacity_matches and not timeout_matches:
            raise _invalid("recovered turn dispatch lacks its immutable recovery authority")
    implementer = state.context.implementer
    if implementer is None or implementer.provider != "codex":
        raise _corrupt("successor lost frozen Codex implementer settings")
    frozen_settings = (
        implementer.command,
        implementer.model,
        implementer.reasoning_effort,
        implementer.timeout_minutes * 60,
        implementer.sandbox,
        implementer.approval_policy,
    )
    recorded_settings = (
        record.implementer.command,
        record.implementer.model,
        record.implementer.reasoning_effort,
        record.implementer.timeout_seconds,
        record.implementer.sandbox,
        record.implementer.approval_policy,
    )
    context_bindings = (
        (
            state.context.plan_prompt.plan_artifact_path,
            state.context.plan_prompt.plan_sha256,
            record.plan,
        ),
        (
            state.context.plan_prompt.prompt_artifact_path,
            state.context.plan_prompt.prompt_sha256,
            record.submitted_prompt,
        ),
        (
            state.context.effective_config.effective_config_artifact_path,
            state.context.effective_config.effective_config_sha256,
            record.effective_config,
        ),
        (
            state.context.effective_config.source_config_artifact_path,
            state.context.effective_config.source_config_sha256,
            record.source_config,
        ),
    )
    if frozen_settings != recorded_settings or any(
        path != binding.relative_path or sha != binding.sha256
        for path, sha, binding in context_bindings
    ):
        raise _corrupt("successor changed frozen implementation authority")
    if (
        codex.implementer_session_id != record.implementer.session_id
        or codex.implementer_binding_artifact_path != record.implementer.binding.relative_path
        or codex.implementer_binding_artifact_sha256 != record.implementer.binding.sha256
    ):
        raise _corrupt("successor lost its exact implementer identity")
    events, complete = _load_all_verified_events(store, conn, state.run_id)
    if not complete:
        raise _corrupt("successor event history is incomplete")
    authenticated_timeout_consumption = _authenticated_timeout_consumption(
        events,
        run_id=state.run_id,
        turn=record.turn_kind,
        iteration=record.iteration,
        parent=record,
    )
    if codex.implementer_timeout_automatic_retries_consumed != authenticated_timeout_consumption:
        raise _corrupt("successor changed recovered timeout policy consumption")
    if (
        isinstance(record.reviewer, BoundReviewerEvidenceV2)
        and codex.reviewer_session_id != record.reviewer.session_id
    ):
        raise _corrupt("successor lost reviewer B identity")
    launch_bindings: list[ArtifactBindingV2 | None] = [
        record.admitted,
        record.invocation_evidence,
        record.completion_envelope,
        record.plan,
        record.submitted_prompt,
        record.effective_config,
        record.source_config,
        record.baseline_status,
        record.partial_work_status,
        record.exact_prompt,
        record.implementer.binding,
        record.raw_fix,
        record.staged_patch,
        record.review_result,
    ]
    if isinstance(record.reviewer, BoundReviewerEvidenceV2):
        launch_bindings.extend(
            (
                ArtifactBindingV2(
                    owner_run_id=state.run_id,
                    relative_path=record.reviewer.binding_artifact_path,
                    sha256=record.reviewer.binding_artifact_sha256,
                ),
                ArtifactBindingV2(
                    owner_run_id=state.run_id,
                    relative_path=record.reviewer.bootstrap_events_path,
                    sha256=record.reviewer.bootstrap_events_sha256,
                ),
            )
        )
    try:
        for binding in launch_bindings:
            if binding is None:
                continue
            artifacts.read_verified_bytes(
                state.run_id,
                binding.relative_path,
                expected_sha256=binding.sha256,
            )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("implementer recovery launch artifacts failed authentication") from exc
    return record


class CodexImplementerFailureRecoveryService:
    """Create or converge one standalone successor for a generic Codex failure."""

    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
        run_id_factory: Callable[[str, datetime], str] = _successor_run_id,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
        fault_point: str | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory
        self.run_id = run_id_factory
        self.event_id = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self.dispatch_id = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")
        self.fault_point = fault_point
        self._publication_lock = threading.Lock()

    def force(self, run_id: str) -> tuple[str, bool]:
        with self.store.begin_read() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scheduler_codex_implementer_recoveries
                WHERE source_run_id = ? AND status IN ('pending','ready')
                ORDER BY created_at, recovery_key
                """,
                (run_id,),
            ).fetchall()
        if len(rows) > 1:
            raise _corrupt("source has multiple active implementer recovery relations")
        if rows:
            return self._resume(rows[0]), False
        evidence = self._analyze(run_id)
        key = implementer_recovery_key(run_id, evidence.attempt_id)
        now = self.now()
        successor_id = self.run_id(evidence.state.context.project_name, now)
        record = self._record(evidence, key=key, successor_run_id=successor_id)
        intent = CodexImplementerRecoveryIntentV1(
            schema_version=IMPLEMENTER_RECOVERY_INTENT_SCHEMA_VERSION,
            recovery_key=key,
            source_run_id=run_id,
            successor_run_id=successor_id,
            failed_attempt_id=evidence.attempt_id,
            failed_dispatch_id=evidence.dispatch_id,
            turn_kind=evidence.turn,  # type: ignore[arg-type]
            iteration=evidence.iteration,
            exact_prompt_path=evidence.prompt_path,
            exact_prompt_sha256=evidence.prompt_sha256,
            implementer_session_id=evidence.implementer_session_id,
            record=record,
        )
        row = self._insert_candidate(evidence, intent, now)
        self._raise_fault("after_candidate")
        return self._resume(row), True

    def reconcile_pending(self) -> list[TickRunReceipt]:
        with self.store.begin_read() as conn:
            rows = list(self.store.list_pending_codex_implementer_recoveries(conn))
        receipts: list[TickRunReceipt] = []
        for row in rows:
            try:
                successor = self._resume(row)
                receipts.append(
                    TickRunReceipt(
                        run_id=successor,
                        action="codex_implementer_recovery_ready",
                    )
                )
            except (SchedulerEngineError, ProtectedArtifactError, OSError, ValidationError) as exc:
                with self.store.begin_read() as conn:
                    current = self.store.get_codex_implementer_recovery_by_key(
                        conn,
                        recovery_key=str(row["recovery_key"]),
                    )
                cancelled = current is not None and str(current["status"]) == "cancelled"
                receipts.append(
                    TickRunReceipt(
                        run_id=str(row["successor_run_id"]),
                        action=(
                            "codex_implementer_recovery_cancelled"
                            if cancelled
                            else "codex_implementer_recovery_pending"
                        ),
                        detail=type(exc).__name__,
                    )
                )
        return receipts

    def _analyze(self, run_id: str) -> _Evidence:
        try:
            with self.store.begin_read() as conn:
                state, _, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, BlockedState):
                    raise _invalid("forced implementer recovery requires a blocked run")
                if state.context.sequence is not None:
                    raise _invalid("sequence implementer recovery is deferred to Phase 27.6")
                implementer = state.context.implementer
                if implementer is None or implementer.provider != "codex":
                    raise _invalid("forced implementer recovery requires a Codex implementer")
                if state.block_reason_kind != _RECOVERY_REASON:
                    raise _invalid("blocked run is not an eligible generic implementer failure")
                if (
                    self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None
                    or self.store.has_abort_requested_for_run(conn, run_id)
                    or self.store.has_unresolved_abort_hold(conn, run_id)
                    or self.store.has_checkpoint_reconciliation_hold(conn, run_id)
                ):
                    raise _invalid(
                        "implementer termination and abort state are not safely resolved"
                    )
                active = self.store.get_active_reservation(
                    conn,
                    state.context.repository.worktree_key,
                )
                if active is not None and str(active["run_id"]) != run_id:
                    raise _invalid("repository reservation is held by another run")
                events, complete = _load_all_verified_events(self.store, conn, run_id)
                if not complete:
                    raise _corrupt("implementer failure event history is incomplete")
                block: CodexImplementerBlockedEvent | None = None
                for row in events:
                    if str(row["event_kind"]) != CODEX_IMPLEMENTER_BLOCKED_EVENT_KIND:
                        continue
                    parsed = _parse_verified_event(row)
                    if isinstance(parsed, CodexImplementerBlockedEvent):
                        block = parsed
                if (
                    block is None
                    or block.run_id != run_id
                    or block.block_reason_kind != _RECOVERY_REASON
                ):
                    raise _corrupt("generic implementer block event is missing")
                if not block.attempt_id:
                    raise _invalid(
                        "historical generic failure lacks a bound attempt and is not recoverable"
                    )
                attempt = self.store.get_attempt_by_id(conn, block.attempt_id)
                if (
                    attempt is None
                    or str(attempt["run_id"]) != run_id
                    or str(attempt["component"]) != "codex_implementer"
                    or str(attempt["status"]) not in {"completed", "failed"}
                    or not int(attempt["ingested"])
                    or attempt["exit_code"] in {None, 0, 124}
                    or attempt["termination_class"] == TerminationClass.TIMEOUT.value
                ):
                    raise _invalid("generic implementer failure is not terminal and authenticated")
                dispatch = self.store.get_effect_by_dispatch_id(
                    conn,
                    str(attempt["dispatch_id"]),
                )
                if dispatch is None or str(dispatch["effect_kind"]) not in (
                    IMPLEMENTER_ATTEMPT_EFFECT_KINDS
                ):
                    raise _corrupt("failed implementer dispatch is missing")
                parent = load_implementer_recovery_record(
                    self.store,
                    self.artifacts,
                    conn,
                    run_id,
                )
                try:
                    edges = recovery_ancestor_edges(self.store, conn, run_id)
                except RecoveryAncestryError as exc:
                    raise _corrupt(str(exc)) from exc
                if len(edges) >= 128:
                    raise _invalid("implementer recovery ancestry is exhausted")
                reviews_completed, effective_ceiling = _review_budget_for_blocked_run(
                    self.store,
                    self.artifacts,
                    conn,
                    state,
                )
        except (CursorEvidenceError, CodexEvidenceError, ProtectedArtifactError) as exc:
            raise _corrupt("implementer recovery source evidence failed authentication") from exc

        attempt_id = str(attempt["attempt_id"])
        dispatch_id = str(attempt["dispatch_id"])
        effect_kind = str(dispatch["effect_kind"])
        root = self.artifacts.run_root(run_id)
        invocation_path = invocation_evidence_rel(attempt_id)
        try:
            invocation = verify_codex_implementer_invocation_evidence(
                root,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                unit_identity=str(attempt["unit_identity"]),
                launch_nonce=str(attempt["launch_nonce"]),
                launch_intent_sha256=str(attempt["launch_intent_sha256"]),
                effect_kind=effect_kind,
            )
            verify_pre_execution_implementer_guards(root, invocation, run_id=run_id)
            termination = attempt["termination_class"]
            outcome = load_authenticated_codex_implementer_outcome(
                root,
                attempt_id=attempt_id,
                unit_identity=str(attempt["unit_identity"]),
                result_rel=str(attempt["result_artifact_path"]),
                stdout_rel=str(attempt["stdout_artifact_path"]),
                stderr_rel=str(attempt["stderr_artifact_path"]),
                observed_exit_code=int(attempt["exit_code"]),
                observed_termination=(
                    TerminationClass(str(termination)) if termination is not None else None
                ),
                expected_envelope_sha256=str(attempt["completion_envelope_sha256"]),
                expected_dispatch_id=dispatch_id,
                expected_effect_kind=effect_kind,
            )
        except (CodexEvidenceError, ValueError, OSError) as exc:
            raise _corrupt("failed implementer invocation or outcome is invalid") from exc
        try:
            invocation_artifact_sha = hashlib.sha256(
                self.artifacts.read_verified_bytes(
                    run_id,
                    invocation_path,
                    expected_sha256=hashlib.sha256(
                        (root / invocation_path).read_bytes()
                    ).hexdigest(),
                )
            ).hexdigest()
        except (ProtectedArtifactError, OSError) as exc:
            raise _corrupt("failed implementer invocation artifact is invalid") from exc
        if any(
            (
                bool(outcome.get("timed_out")),
                bool(outcome.get("usage_limit")),
                bool(outcome.get("output_incomplete")),
                bool(str(outcome.get("bootstrap_uncertainty_reason", "")).strip()),
            )
        ):
            raise _invalid("timeout, capacity, incomplete, or uncertain failures cannot use force")
        partial_work_status_path = str(outcome.get("status_after_path", "")).strip()
        partial_work_status_sha = str(outcome.get("status_after_sha256", "")).strip()
        partial_work_fingerprint_sha = str(outcome.get("partial_work_sha256", "")).strip()
        if (
            not partial_work_status_path
            or not partial_work_status_sha
            or not partial_work_fingerprint_sha
        ):
            raise _invalid("generic failure lacks authenticated partial-work evidence")
        partial_work = ArtifactBindingV2(
            owner_run_id=run_id,
            relative_path=partial_work_status_path,
            sha256=partial_work_status_sha,
        )
        self._verify_partial_work_current(
            partial_work,
            repository_root=state.context.repository.root,
            expected_fingerprint_sha256=partial_work_fingerprint_sha,
        )
        session_id = str(outcome.get("session_id", "")).strip()
        if not session_id:
            raise _invalid("generic failure has no authenticated implementer identity")
        invocation_session = str(invocation.get("session_id", "")).strip()
        if effect_kind == RESUME_CODEX_IMPLEMENTER_EFFECT_KIND and invocation_session != session_id:
            raise _corrupt("failed resume did not preserve implementer identity")
        turn = str(invocation.get("turn", ""))
        iteration = invocation.get("iteration")
        if (
            turn not in {"initial", "correction"}
            or isinstance(iteration, bool)
            or not isinstance(iteration, int)
        ):
            raise _corrupt("failed implementer turn binding is invalid")
        if str(outcome.get("turn", "")) != turn:
            raise _corrupt("implementer invocation and outcome turn disagree")
        prompt_path = str(invocation.get("prompt_path", ""))
        prompt_sha = str(invocation.get("prompt_sha256", ""))
        frozen = {
            "codex_command": implementer.command,
            "model": implementer.model,
            "reasoning_effort": implementer.reasoning_effort,
            "timeout_seconds": implementer.timeout_minutes * 60,
            "codex_sandbox": implementer.sandbox,
            "approval_policy": implementer.approval_policy,
        }
        if any(invocation.get(key) != value for key, value in frozen.items()):
            raise _corrupt("failed implementer invocation changed frozen settings")
        same_recovered_turn = False
        if parent is not None:
            if parent.implementer.session_id != session_id:
                raise _corrupt("failed successor changed the recovered implementer identity")
            same_recovered_turn = parent.turn_kind == turn and parent.iteration == iteration
            if same_recovered_turn and (
                parent.exact_prompt.relative_path != prompt_path
                or parent.exact_prompt.sha256 != prompt_sha
            ):
                raise _corrupt("failed successor changed the recovered turn prompt")
            if not same_recovered_turn and (turn != "correction" or iteration <= parent.iteration):
                raise _corrupt("failed successor does not follow its recovered-turn ancestry")
        elif turn == "initial" and (
            prompt_path != state.context.plan_prompt.prompt_artifact_path
            or prompt_sha != state.context.plan_prompt.prompt_sha256
            or iteration != 1
        ):
            raise _corrupt("initial recovery changed original prompt authority")
        timeout_automatic_retries_consumed = _authenticated_timeout_consumption(
            events,
            run_id=run_id,
            turn=turn,
            iteration=iteration,
            parent=parent,
        )
        self.artifacts.read_verified_bytes(run_id, prompt_path, expected_sha256=prompt_sha)
        binding_sha, bootstrap_attempt = self._implementer_binding(
            run_id,
            state,
            block=block,
            session_id=session_id,
        )
        admitted_path, admitted_sha = self._admission(run_id, events)
        self._verify_frozen_context_artifacts(state)
        reviewer: BoundReviewerEvidenceV2 | ReviewerNotCreatedV2 = ReviewerNotCreatedV2(
            form="not_created"
        )
        raw_fix = staged = review = None
        if turn == "initial":
            if reviews_completed != 0 or parent is not None and parent.turn_kind != "initial":
                raise _invalid("initial recovery cannot inherit reviewer history")
        elif same_recovered_turn:
            assert parent is not None
            reviewer = parent.reviewer
            raw_fix, staged, review = parent.raw_fix, parent.staged_patch, parent.review_result
            if not isinstance(reviewer, BoundReviewerEvidenceV2):
                raise _corrupt("correction parent is missing reviewer B")
            self._verify_record_artifacts(parent)
        else:
            reviewer, raw_fix, staged, review = self._correction_evidence(
                run_id,
                events,
                iteration,
                prompt_path=prompt_path,
                prompt_sha256=prompt_sha,
            )
        if isinstance(reviewer, BoundReviewerEvidenceV2) and reviewer.session_id == session_id:
            raise _invalid("implementer identity collides with reviewer B")
        return _Evidence(
            state=state,
            attempt_id=attempt_id,
            dispatch_id=dispatch_id,
            turn=turn,
            iteration=iteration,
            prompt_path=prompt_path,
            prompt_sha256=prompt_sha,
            invocation_sha256=payload_sha256(
                json.dumps(invocation, sort_keys=True, separators=(",", ":"))
            ),
            invocation_artifact_path=invocation_path,
            invocation_artifact_sha256=invocation_artifact_sha,
            completion_sha256=str(attempt["completion_envelope_sha256"]),
            completion_artifact_path=str(attempt["result_artifact_path"]),
            implementer_session_id=session_id,
            implementer_binding_sha256=binding_sha,
            implementer_bootstrap_attempt_id=bootstrap_attempt,
            admitted_path=admitted_path,
            admitted_sha256=admitted_sha,
            partial_work_status_path=partial_work_status_path,
            partial_work_status_sha256=partial_work_status_sha,
            partial_work_fingerprint_sha256=partial_work_fingerprint_sha,
            reviews_completed=reviews_completed,
            effective_ceiling=effective_ceiling,
            timeout_automatic_retries_consumed=timeout_automatic_retries_consumed,
            reviewer=reviewer,
            raw_fix=raw_fix,
            staged_patch=staged,
            review_result=review,
            parent=parent,
        )

    def _implementer_binding(
        self,
        run_id: str,
        state: BlockedState,
        *,
        block: CodexImplementerBlockedEvent,
        session_id: str,
    ) -> tuple[str, str]:
        implementer = state.context.implementer
        assert implementer is not None
        path = block.binding_artifact_path
        expected_sha = block.binding_artifact_sha256
        if (
            block.implementer_session_id_prefix != session_id[:8]
            or path != codex_implementer_session_rel()
            or expected_sha is None
        ):
            raise _corrupt("implementer identity binding is missing from source state")
        try:
            data = self.artifacts.read_verified_bytes(
                run_id,
                path,
                expected_sha256=expected_sha,
            )
            payload = json.loads(data.decode("utf-8"))
        except (ProtectedArtifactError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise _corrupt("implementer identity artifact is invalid") from exc
        if not isinstance(payload, dict) or any(
            payload.get(key) != value
            for key, value in {
                "role": "implementer",
                "purpose": "implementation",
                "session_id": session_id,
                "model": implementer.model,
                "reasoning_effort": implementer.reasoning_effort,
            }.items()
        ):
            raise _corrupt("implementer identity artifact conflicts with failure evidence")
        bootstrap = str(payload.get("bootstrap_attempt_id", "")).strip()
        if not bootstrap:
            raise _corrupt("implementer identity artifact lacks bootstrap attempt")
        return expected_sha, bootstrap

    def _verify_partial_work_current(
        self,
        binding: ArtifactBindingV2,
        *,
        repository_root: str,
        expected_fingerprint_sha256: str,
    ) -> None:
        try:
            captured = self.artifacts.read_verified_bytes(
                binding.owner_run_id,
                binding.relative_path,
                expected_sha256=binding.sha256,
            ).decode("utf-8")
            current_raw = capture_git_status(repository_root)
            current = (
                current_raw.decode("utf-8", errors="replace")
                if isinstance(current_raw, bytes)
                else str(current_raw)
            )
            current_fingerprint = worktree_content_fingerprint(
                repository_root,
                status_text=current,
            )
        except (AiDevLoopError, ProtectedArtifactError, OSError, UnicodeError) as exc:
            raise _corrupt("partial-work boundary evidence failed authentication") from exc
        if current != captured:
            raise _invalid("worktree changed after the failed implementer boundary")
        if current_fingerprint != expected_fingerprint_sha256:
            raise _invalid("partial work changed after the failed implementer boundary")

    def _admission(
        self,
        run_id: str,
        events: list[sqlite3.Row],
    ) -> tuple[str, str]:
        admitted: WorktreeAdmittedEvent | None = None
        for row in events:
            parsed = _parse_verified_event(row)
            if isinstance(parsed, WorktreeAdmittedEvent):
                admitted = parsed
        if admitted is None:
            raise _corrupt("blocked source lacks admitted worktree authority")
        self.artifacts.read_verified_bytes(
            run_id,
            admitted.admission_status_artifact_path,
            expected_sha256=admitted.admission_status_sha256,
        )
        return admitted.admission_status_artifact_path, admitted.admission_status_sha256

    def _verify_frozen_context_artifacts(self, state: BlockedState) -> None:
        context = state.context
        bindings = [
            (
                context.plan_prompt.plan_artifact_path,
                context.plan_prompt.plan_sha256,
            ),
            (
                context.plan_prompt.prompt_artifact_path,
                context.plan_prompt.prompt_sha256,
            ),
            (
                context.effective_config.effective_config_artifact_path,
                context.effective_config.effective_config_sha256,
            ),
            (
                context.effective_config.source_config_artifact_path,
                context.effective_config.source_config_sha256,
            ),
        ]
        if context.baseline_status_artifact_path is not None:
            if context.baseline_status_sha256 is None:
                raise _corrupt("source baseline binding is incomplete")
            bindings.append(
                (
                    context.baseline_status_artifact_path,
                    context.baseline_status_sha256,
                )
            )
        try:
            for relative_path, sha256 in bindings:
                self.artifacts.read_verified_bytes(
                    state.run_id,
                    relative_path,
                    expected_sha256=sha256,
                )
        except (ProtectedArtifactError, OSError) as exc:
            raise _corrupt("frozen source artifacts failed authentication") from exc

    def _correction_evidence(
        self,
        run_id: str,
        events: list[sqlite3.Row],
        iteration: int,
        *,
        prompt_path: str,
        prompt_sha256: str,
    ) -> tuple[
        BoundReviewerEvidenceV2,
        ArtifactBindingV2,
        ArtifactBindingV2,
        ArtifactBindingV2,
    ]:
        fix = _resolve_active_correction_fix(
            events,
            events,
            run_id=run_id,
            evidence_run_id=run_id,
            block_sequence=None,
        )
        if fix is None or fix.correction_envelope_path == "":
            raise _corrupt("correction failure lacks exact fix-envelope evidence")
        if (
            fix.correction_envelope_path != prompt_path
            or fix.correction_envelope_sha256 != prompt_sha256
        ):
            raise _corrupt("correction recovery changed exact pending prompt authority")
        with self.store.begin_read() as conn:
            bound, owner = _resolve_reviewer_bound_for_run(
                self.store,
                conn,
                run_id,
                events,
                block_sequence=None,
            )
            staging, staging_owner = _resolve_staging_for_correction(
                [(run_id, events)],
                fix=fix,
                block_sequence=None,
            )
            if bound is None or owner is None or staging is None or staging_owner is None:
                raise _corrupt("correction failure lacks reviewer or reviewed-patch evidence")
            codex_attempt = _resolve_codex_attempt_for_correction_review(
                self.store,
                conn,
                events,
                fix_owner_run_id=fix.owner_run_id,
                fix=fix,
                block_sequence=None,
                artifacts=self.artifacts,
            )
        proof, review_path, review_sha = _authenticate_correction_turn_evidence(
            self.store,
            self.artifacts,
            fix=fix,
            b_owner_run_id=owner,
            reviewer_bound=bound,
            staging_event=staging,
            staging_owner_run_id=staging_owner,
            codex_attempt=codex_attempt,
            cursor_iteration=iteration,
        )
        reviewer = BoundReviewerEvidenceV2(
            form="bound",
            session_id=proof.session_id,
            bootstrap_run_id=proof.bootstrap_run_id,
            bootstrap_attempt_id=proof.bootstrap_attempt_id,
            bootstrap_events_path=proof.bootstrap_events_path,
            bootstrap_events_sha256=proof.bootstrap_events_sha256,
            binding_run_id=proof.binding_run_id,
            binding_artifact_path=proof.binding_artifact_path,
            binding_artifact_sha256=proof.binding_artifact_sha256,
        )
        return (
            reviewer,
            ArtifactBindingV2(
                owner_run_id=fix.owner_run_id,
                relative_path=fix.fix_prompt_path,
                sha256=fix.fix_prompt_sha256,
            ),
            ArtifactBindingV2(
                owner_run_id=staging_owner,
                relative_path=staging.staged_patch_path,
                sha256=staging.staged_patch_sha256,
            ),
            ArtifactBindingV2(
                owner_run_id=fix.owner_run_id,
                relative_path=review_path,
                sha256=review_sha,
            ),
        )

    def _record(
        self,
        evidence: _Evidence,
        *,
        key: str,
        successor_run_id: str,
    ) -> CodexImplementerRecoveryRecordV1:
        state = evidence.state
        implementer = state.context.implementer
        assert implementer is not None
        assert implementer.reasoning_effort is not None
        baseline_path = state.context.baseline_status_artifact_path
        baseline_sha = state.context.baseline_status_sha256
        if bool(baseline_path) != bool(baseline_sha):
            raise _corrupt("source baseline binding is incomplete")
        parent = evidence.parent
        with self.store.begin_read() as conn:
            edges = recovery_ancestor_edges(self.store, conn, state.run_id)
        ancestors = [
            AncestorEdgeV2(
                relation=edge.relation,
                source_run_id=edge.source_run_id,
                successor_run_id=edge.successor_run_id,
                recovery_key=edge.recovery_key,
            )
            for edge in edges
        ]
        ancestors.insert(
            0,
            AncestorEdgeV2(
                relation="implementer_recovery",
                source_run_id=state.run_id,
                successor_run_id=successor_run_id,
                recovery_key=key,
            ),
        )
        return CodexImplementerRecoveryRecordV1(
            schema_version=IMPLEMENTER_RECOVERY_RECORD_SCHEMA_VERSION,
            recovery_key=key,
            source_run_id=state.run_id,
            successor_run_id=successor_run_id,
            failed_attempt_id=evidence.attempt_id,
            failed_dispatch_id=evidence.dispatch_id,
            turn_kind=evidence.turn,  # type: ignore[arg-type]
            iteration=evidence.iteration,
            invocation_evidence_sha256=evidence.invocation_sha256,
            completion_envelope_sha256=evidence.completion_sha256,
            invocation_evidence=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=evidence.invocation_artifact_path,
                sha256=evidence.invocation_artifact_sha256,
            ),
            completion_envelope=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=evidence.completion_artifact_path,
                sha256=evidence.completion_sha256,
            ),
            implementer=CodexImplementerIdentityV1(
                session_id=evidence.implementer_session_id,
                binding=ArtifactBindingV2(
                    owner_run_id=state.run_id,
                    relative_path=codex_implementer_session_rel(),
                    sha256=evidence.implementer_binding_sha256,
                ),
                bootstrap_attempt_id=evidence.implementer_bootstrap_attempt_id,
                command=implementer.command,
                model=implementer.model,
                reasoning_effort=implementer.reasoning_effort,
                timeout_seconds=implementer.timeout_minutes * 60,
                sandbox=implementer.sandbox,  # type: ignore[arg-type]
                approval_policy=implementer.approval_policy,  # type: ignore[arg-type]
            ),
            reviewer=evidence.reviewer,
            reviews_completed=evidence.reviews_completed,
            submitted_max_review_iterations=state.context.workflow.max_review_iterations,
            effective_review_ceiling=evidence.effective_ceiling,
            timeout_automatic_retries_consumed=(evidence.timeout_automatic_retries_consumed),
            admitted=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=evidence.admitted_path,
                sha256=evidence.admitted_sha256,
            ),
            plan=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=state.context.plan_prompt.plan_artifact_path,
                sha256=state.context.plan_prompt.plan_sha256,
            ),
            submitted_prompt=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=state.context.plan_prompt.prompt_artifact_path,
                sha256=state.context.plan_prompt.prompt_sha256,
            ),
            effective_config=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=state.context.effective_config.effective_config_artifact_path,
                sha256=state.context.effective_config.effective_config_sha256,
            ),
            source_config=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=state.context.effective_config.source_config_artifact_path,
                sha256=state.context.effective_config.source_config_sha256,
            ),
            baseline_status=(
                None
                if baseline_path is None or baseline_sha is None
                else ArtifactBindingV2(
                    owner_run_id=state.run_id,
                    relative_path=baseline_path,
                    sha256=baseline_sha,
                )
            ),
            partial_work_status=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=evidence.partial_work_status_path,
                sha256=evidence.partial_work_status_sha256,
            ),
            partial_work_fingerprint_sha256=evidence.partial_work_fingerprint_sha256,
            exact_prompt=ArtifactBindingV2(
                owner_run_id=state.run_id,
                relative_path=evidence.prompt_path,
                sha256=evidence.prompt_sha256,
            ),
            raw_fix=evidence.raw_fix,
            staged_patch=evidence.staged_patch,
            review_result=evidence.review_result,
            parent_source_run_id=None if parent is None else parent.source_run_id,
            parent_recovery_key=None if parent is None else parent.recovery_key,
            parent_record_sha256=(
                None if parent is None else hashlib.sha256(parent.canonical_bytes()).hexdigest()
            ),
            ancestors=ancestors,
        )

    def _insert_candidate(
        self,
        evidence: _Evidence,
        intent: CodexImplementerRecoveryIntentV1,
        now: datetime,
    ) -> sqlite3.Row:
        payload = intent.canonical_bytes()
        try:
            with self.store.begin_immediate() as conn:
                existing = self.store.get_codex_implementer_recovery_by_source_attempt(
                    conn,
                    source_run_id=intent.source_run_id,
                    failed_attempt_id=intent.failed_attempt_id,
                )
                if existing is not None:
                    return existing
                source, _, _ = self.store.load_validated_snapshot(conn, intent.source_run_id)
                if (
                    not isinstance(source, BlockedState)
                    or source.block_reason_kind != _RECOVERY_REASON
                    or source.context.sequence is not None
                ):
                    raise _conflict("implementer recovery source changed before publication")
                attempt = self.store.get_attempt_by_id(conn, intent.failed_attempt_id)
                if attempt is None or not int(attempt["ingested"]):
                    raise _conflict("implementer recovery failure authority changed")
                successor = self._successor_state(source, intent, now)
                self._insert_successor(conn, successor, intent, now)
                if not self.store.claim_reservation_for_successor(
                    conn,
                    successor_run_id=intent.successor_run_id,
                    source_run_id=intent.source_run_id,
                    worktree_key=source.context.repository.worktree_key,
                    repository_root=source.context.repository.root,
                    now=now,
                ):
                    raise _conflict("repository reservation is held by another run")
                inserted = self.store.insert_codex_implementer_recovery(
                    conn,
                    recovery_key=intent.recovery_key,
                    source_run_id=intent.source_run_id,
                    failed_attempt_id=intent.failed_attempt_id,
                    successor_run_id=intent.successor_run_id,
                    failed_dispatch_id=intent.failed_dispatch_id,
                    intent_payload=payload.decode("utf-8"),
                    intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
                    now=now,
                )
                if not inserted:
                    existing = self.store.get_codex_implementer_recovery_by_source_attempt(
                        conn,
                        source_run_id=intent.source_run_id,
                        failed_attempt_id=intent.failed_attempt_id,
                    )
                    if existing is None:
                        raise _conflict("implementer recovery relation was not recorded")
                    return existing
                row = self.store.get_codex_implementer_recovery_by_key(
                    conn,
                    recovery_key=intent.recovery_key,
                )
            if row is None:
                raise _corrupt("implementer recovery relation disappeared")
            return row
        except sqlite3.IntegrityError:
            with self.store.begin_read() as conn:
                existing = self.store.get_codex_implementer_recovery_by_source_attempt(
                    conn,
                    source_run_id=intent.source_run_id,
                    failed_attempt_id=intent.failed_attempt_id,
                )
            if existing is None:
                raise
            return existing

    def _successor_state(
        self,
        source: BlockedState,
        intent: CodexImplementerRecoveryIntentV1,
        now: datetime,
    ) -> PreflightCompleteState | WaitingForCursorFixState:
        record = intent.record
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        checkpoint = AdmittedRunCheckpoint(
            authorized_at=source.authorized_at or source.blocked_at,
            authorized_controller_session_id=source.authorized_controller_session_id,
            admitted_at=source.blocked_at,
            admission_status_artifact_path=record.admitted.relative_path,
            admission_status_sha256=record.admitted.sha256,
        )
        cursor = CursorWorkflowCheckpoint(
            iteration=record.iteration,
            original_prompt_path=source.context.plan_prompt.prompt_artifact_path,
            original_prompt_sha256=source.context.plan_prompt.prompt_sha256,
            staged_patch_path=(
                None if record.staged_patch is None else record.staged_patch.relative_path
            ),
            staged_patch_sha256=(
                None if record.staged_patch is None else record.staged_patch.sha256
            ),
        )
        reviewer = record.reviewer
        codex = CodexWorkflowCheckpoint(
            review_iteration=max(record.reviews_completed + 1, 1),
            reviews_completed=record.reviews_completed,
            reviewer_session_id=(
                reviewer.session_id if isinstance(reviewer, BoundReviewerEvidenceV2) else None
            ),
            binding_artifact_path=(
                reviewer.binding_artifact_path
                if isinstance(reviewer, BoundReviewerEvidenceV2)
                else None
            ),
            binding_artifact_sha256=(
                reviewer.binding_artifact_sha256
                if isinstance(reviewer, BoundReviewerEvidenceV2)
                else None
            ),
            latest_fix_prompt_path=(
                None if record.raw_fix is None else record.raw_fix.relative_path
            ),
            latest_fix_prompt_sha256=(None if record.raw_fix is None else record.raw_fix.sha256),
            latest_correction_envelope_path=(
                record.exact_prompt.relative_path if record.turn_kind == "correction" else None
            ),
            latest_correction_envelope_sha256=(
                record.exact_prompt.sha256 if record.turn_kind == "correction" else None
            ),
            latest_review_result_path=(
                None if record.review_result is None else record.review_result.relative_path
            ),
            latest_review_result_sha256=(
                None if record.review_result is None else record.review_result.sha256
            ),
            implementer_session_id=record.implementer.session_id,
            implementer_binding_artifact_path=record.implementer.binding.relative_path,
            implementer_binding_artifact_sha256=record.implementer.binding.sha256,
            implementer_timeout_automatic_retries_consumed=(
                record.timeout_automatic_retries_consumed
            ),
        )
        if record.turn_kind == "initial":
            return PreflightCompleteState(
                run_id=intent.successor_run_id,
                version=1,
                submitted_at=now_text,
                updated_at=now_text,
                idempotency_key=secrets.token_hex(32),
                context=source.context,
                checkpoint=checkpoint,
                cursor=cursor,
                codex=codex,
            )
        return WaitingForCursorFixState(
            run_id=intent.successor_run_id,
            version=1,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=secrets.token_hex(32),
            context=source.context,
            checkpoint=checkpoint,
            cursor=cursor,
            codex=codex,
        )

    def _insert_successor(
        self,
        conn: sqlite3.Connection,
        successor: PreflightCompleteState | WaitingForCursorFixState,
        intent: CodexImplementerRecoveryIntentV1,
        now: datetime,
    ) -> None:
        kind, payload, digest = self.store.dump_state(successor)
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                successor.run_id,
                kind,
                payload,
                digest,
                successor.version,
                successor.idempotency_key,
                successor.context.repository.worktree_key,
                now_text,
                now_text,
            ),
        )
        events: list[object] = [
            CodexImplementerRecoverySuccessorCreatedEvent(
                source_run_id=intent.source_run_id,
                successor_run_id=intent.successor_run_id,
                recovery_key=intent.recovery_key,
                failed_attempt_id=intent.failed_attempt_id,
                turn=intent.turn_kind,
                iteration=intent.iteration,
            ),
            WorktreeAdmittedEvent(
                run_id=intent.successor_run_id,
                admission_status_artifact_path=intent.record.admitted.relative_path,
                admission_status_sha256=intent.record.admitted.sha256,
                resolved_root=successor.context.repository.root,
            ),
        ]
        reviewer = intent.record.reviewer
        if isinstance(reviewer, BoundReviewerEvidenceV2):
            events.append(
                CodexReviewerBoundEvent(
                    run_id=intent.successor_run_id,
                    reviewer_session_id_prefix=reviewer.session_id[:8],
                    binding_artifact_path=reviewer.binding_artifact_path,
                    binding_artifact_sha256=reviewer.binding_artifact_sha256,
                )
            )
        for event in events:
            self.store.append_event(
                conn,
                event_id=self.event_id(),
                run_id=intent.successor_run_id,
                sequence=self.store.next_event_sequence(conn, intent.successor_run_id),
                event=event,  # type: ignore[arg-type]
                now=now,
            )

    def _resume(self, row: sqlite3.Row) -> str:
        with self._publication_lock:
            intent = _verified_recovery_intent(row)
            if str(row["status"]) == "cancelled":
                raise _conflict("implementer recovery publication was cancelled")
            if str(row["status"]) == "ready":
                with self.store.begin_read() as conn:
                    load_implementer_recovery_record(
                        self.store,
                        self.artifacts,
                        conn,
                        intent.successor_run_id,
                    )
                return intent.successor_run_id
            if self._successor_cancelled(intent.successor_run_id):
                self._mark_cancelled(intent.recovery_key)
                raise _conflict("implementer recovery successor was aborted before publication")
            self._publish(intent)
            self._raise_fault("after_artifacts")
            self._mark_ready(intent)
            self._raise_fault("after_ready")
            return intent.successor_run_id

    def _publish(self, intent: CodexImplementerRecoveryIntentV1) -> None:
        self._verify_record_artifacts(intent.record)
        self._verify_partial_work_current(
            intent.record.partial_work_status,
            repository_root=self._source_repository_root(intent.source_run_id),
            expected_fingerprint_sha256=intent.record.partial_work_fingerprint_sha256,
        )
        source_root = self.artifacts.run_root(intent.source_run_id)
        for path in sorted(source_root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(source_root).as_posix()
            if relative in {IMPLEMENTER_RECOVERY_INTENT_REL, IMPLEMENTER_RECOVERY_RECORD_REL}:
                continue
            data = path.read_bytes()
            if not data:
                # Empty diagnostic streams are not runtime authority and the
                # protected publisher intentionally rejects empty payloads.
                continue
            self.artifacts.publish_or_verify_bytes(
                intent.successor_run_id,
                relative,
                data,
                max_bytes=MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES,
            )
        for binding in self._record_artifact_bindings(intent.record):
            data = self.artifacts.read_verified_bytes(
                binding.owner_run_id,
                binding.relative_path,
                expected_sha256=binding.sha256,
            )
            if data:
                self.artifacts.publish_or_verify_bytes(
                    intent.successor_run_id,
                    binding.relative_path,
                    data,
                    max_bytes=MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES,
                )
            else:
                self.artifacts.publish_or_verify_empty_bytes(
                    intent.successor_run_id,
                    binding.relative_path,
                    expected_sha256=binding.sha256,
                )
        intent_bytes = intent.canonical_bytes()
        self.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            IMPLEMENTER_RECOVERY_INTENT_REL,
            intent_bytes,
            max_bytes=MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES,
        )
        self.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            IMPLEMENTER_RECOVERY_RECORD_REL,
            intent.record.canonical_bytes(),
            max_bytes=MAX_IMPLEMENTER_RECOVERY_ARTIFACT_BYTES,
        )

    def _mark_ready(self, intent: CodexImplementerRecoveryIntentV1) -> None:
        now = self.now()
        intent_sha = hashlib.sha256(intent.canonical_bytes()).hexdigest()
        record_sha = hashlib.sha256(intent.record.canonical_bytes()).hexdigest()
        with self.store.begin_immediate() as conn:
            row = self.store.get_codex_implementer_recovery_by_key(
                conn,
                recovery_key=intent.recovery_key,
            )
            if row is None:
                raise _corrupt("implementer recovery relation disappeared")
            if str(row["status"]) == "ready":
                return
            if str(row["status"]) != "pending":
                raise _conflict("implementer recovery publication was cancelled")
            if self.store.has_abort_requested_for_run(conn, intent.successor_run_id):
                raise _conflict("implementer recovery successor was aborted")
            reservation = self.store.get_reservation_for_run(conn, intent.successor_run_id)
            if reservation is None:
                raise _conflict("implementer recovery successor lost repository reservation")
            state, version, _ = self.store.load_validated_snapshot(
                conn,
                intent.successor_run_id,
            )
            if not isinstance(state, (PreflightCompleteState, WaitingForCursorFixState)):
                raise _conflict("implementer recovery successor state changed")
            self.artifacts.read_verified_bytes(
                intent.successor_run_id,
                IMPLEMENTER_RECOVERY_INTENT_REL,
                expected_sha256=intent_sha,
            )
            self.artifacts.read_verified_bytes(
                intent.successor_run_id,
                IMPLEMENTER_RECOVERY_RECORD_REL,
                expected_sha256=record_sha,
            )
            changed = self.store.update_codex_implementer_recovery_status(
                conn,
                recovery_key=intent.recovery_key,
                expected_status="pending",
                status="ready",
                intent_artifact_path=IMPLEMENTER_RECOVERY_INTENT_REL,
                intent_artifact_sha256=intent_sha,
                record_artifact_path=IMPLEMENTER_RECOVERY_RECORD_REL,
                record_sha256=record_sha,
                now=now,
            )
            if not changed:
                raise _conflict("implementer recovery publication changed concurrently")
            event = conn.execute(
                """
                SELECT event_id FROM scheduler_events
                WHERE run_id = ? AND event_kind = ? ORDER BY sequence LIMIT 1
                """,
                (intent.successor_run_id, "codex_implementer_recovery_successor_created"),
            ).fetchone()
            if event is None:
                raise _corrupt("implementer recovery successor event is missing")
            self.store.insert_effect(
                conn,
                dispatch_id=self.dispatch_id(),
                source_event_id=str(event["event_id"]),
                run_id=intent.successor_run_id,
                effect_id=RESUME_CODEX_IMPLEMENTER_EFFECT_ID,
                effect_kind=RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
                effect_payload={
                    "iteration": intent.iteration,
                    "turn": intent.turn_kind,
                    "prompt_path": intent.exact_prompt_path,
                    "implementer_recovery_key": intent.recovery_key,
                    "implementer_recovery_source": intent.source_run_id,
                    "implementer_recovery_attempt": intent.failed_attempt_id,
                },
                available_at=now,
                claimed_run_version=version,
                now=now,
            )

    def _successor_cancelled(self, run_id: str) -> bool:
        with self.store.begin_read() as conn:
            if self.store.has_abort_requested_for_run(conn, run_id):
                return True
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        return state.kind == "aborted"

    def _mark_cancelled(self, recovery_key: str) -> None:
        with self.store.begin_immediate() as conn:
            row = self.store.get_codex_implementer_recovery_by_key(
                conn,
                recovery_key=recovery_key,
            )
            if row is None or str(row["status"]) != "pending":
                return
            self.store.update_codex_implementer_recovery_status(
                conn,
                recovery_key=recovery_key,
                expected_status="pending",
                status="cancelled",
                intent_artifact_path=None,
                intent_artifact_sha256=None,
                record_artifact_path=None,
                record_sha256=None,
                now=self.now(),
            )

    def _verify_record_artifacts(self, record: CodexImplementerRecoveryRecordV1) -> None:
        for binding in self._record_artifact_bindings(record):
            self.artifacts.read_verified_bytes(
                binding.owner_run_id,
                binding.relative_path,
                expected_sha256=binding.sha256,
            )

    @staticmethod
    def _record_artifact_bindings(
        record: CodexImplementerRecoveryRecordV1,
    ) -> list[ArtifactBindingV2]:
        optional_bindings = [
            record.admitted,
            record.invocation_evidence,
            record.completion_envelope,
            record.plan,
            record.submitted_prompt,
            record.effective_config,
            record.source_config,
            record.baseline_status,
            record.partial_work_status,
            record.exact_prompt,
            record.implementer.binding,
            record.raw_fix,
            record.staged_patch,
            record.review_result,
        ]
        if isinstance(record.reviewer, BoundReviewerEvidenceV2):
            optional_bindings.append(
                ArtifactBindingV2(
                    owner_run_id=record.reviewer.binding_run_id,
                    relative_path=record.reviewer.binding_artifact_path,
                    sha256=record.reviewer.binding_artifact_sha256,
                )
            )
            optional_bindings.append(
                ArtifactBindingV2(
                    owner_run_id=record.reviewer.bootstrap_run_id,
                    relative_path=record.reviewer.bootstrap_events_path,
                    sha256=record.reviewer.bootstrap_events_sha256,
                )
            )
        return [binding for binding in optional_bindings if binding is not None]

    def _source_repository_root(self, run_id: str) -> str:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        return state.context.repository.root

    def _raise_fault(self, point: str) -> None:
        if self.fault_point == point:
            raise OSError(f"injected implementer recovery fault: {point}")


def reconcile_pending_codex_implementer_recoveries(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    now_factory: Callable[[], datetime] = utc_now,
) -> list[TickRunReceipt]:
    return CodexImplementerFailureRecoveryService(
        store,
        artifacts,
        now_factory=now_factory,
    ).reconcile_pending()
