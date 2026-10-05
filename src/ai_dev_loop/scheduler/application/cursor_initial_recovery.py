"""Publish one same-chat successor for a failed initial standalone Cursor turn."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from ai_dev_loop.scheduler.application.contracts import (
    AppModel,
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
    active_cursor_safe_next_action,
)
from ai_dev_loop.scheduler.application.cursor_evidence import CursorEvidenceError
from ai_dev_loop.scheduler.application.cursor_recovery_evidence import (
    CursorRecoveryAnalysisResult,
    CursorRecoveryEvidenceBundle,
    _authenticate_failed_cursor_turn,
    analyze_cursor_recovery_evidence,
    resolve_decisive_failed_cursor_attempt,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    CURSOR_INITIAL_RECOVERY_INTENT_SCHEMA_VERSION,
    CURSOR_INITIAL_RECOVERY_RECORD_SCHEMA_VERSION,
    EFFECTIVE_PROMPT_REL,
    RECORD_REL,
    CursorInitialRecoveryPublicationIntentV1,
    CursorInitialRecoveryRecordV1,
    effective_prompt_bytes,
    recovery_key_for,
)
from ai_dev_loop.scheduler.domain.events import (
    CursorChatCreatedEvent,
    CursorInitialRecoverySuccessorCreatedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    CodexWorkflowCheckpoint,
    CursorReadyState,
    CursorWorkflowCheckpoint,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import generate_run_id, utc_now

MAX_RECOVERY_ARTIFACT_BYTES = 8 * 1024 * 1024
PublicationStatus = Literal["pending", "ready", "cancelled"]


class CursorInitialRecoveryFault(Exception):
    """Injected interruption between publication boundaries."""

    def __init__(self, point: str) -> None:
        super().__init__(point)
        self.point = point


class CursorInitialRecoveryResult(AppModel):
    source_run_id: str
    successor_run_id: str
    changed: bool
    idempotent_replay: bool
    publication_status: PublicationStatus
    agent_execution_ready: bool
    safe_next_action: SafeNextAction


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def _conflict(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CONFLICT, message)


def _corrupt(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, message)


def cursor_initial_recovery_blocks_dispatch(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> bool:
    row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    return row is not None and str(row["status"]) != "ready"


def authenticated_initial_recovery_launch(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
) -> CursorInitialRecoveryRecordV1 | None:
    """Return the ready record when this run is an initial recovery successor."""

    row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    if row is None:
        return None
    if str(row["status"]) != "ready":
        raise _invalid("cursor initial recovery publication is not dispatchable")
    return _verified_record(store, artifacts, row)


class CursorInitialRecoveryService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
        before_create: Callable[[], None] | None = None,
        before_ready_commit: Callable[[], None] | None = None,
        fault_point: str | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._run_id_factory = run_id_factory or _generate_successor_run_id
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"dsp-{secrets.token_hex(16)}")
        self._before_create = before_create
        self._before_ready_commit = before_ready_commit
        self._fault_point = fault_point
        self._publication_lock = threading.Lock()

    def force(self, run_id: str) -> CursorInitialRecoveryResult:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            existing_rows = self.store.list_cursor_initial_recoveries_for_source(
                conn,
                source_run_id=run_id,
            )
        active = [row for row in existing_rows if str(row["status"]) in {"pending", "ready"}]
        if len(active) > 1:
            raise _corrupt("multiple active initial recovery relations for one source")
        if len(active) == 1:
            return self._resume(active[0])
        analysis = analyze_cursor_recovery_evidence(self.store, self.artifacts, run_id)
        self._reject_unless_eligible(analysis)
        assert analysis.evidence is not None
        evidence = analysis.evidence
        key = recovery_key_for(run_id, evidence.failed_attempt_id)
        with self.store.begin_read() as conn:
            existing = self.store.get_cursor_initial_recovery_by_key(conn, recovery_key=key)
        if existing is None:
            if self._before_create is not None:
                self._before_create()
            existing = self._create_candidate(run_id, evidence, key, now)
            self._raise_fault("after_candidate")
        return self._resume(existing)

    def reconcile_pending(self) -> list[TickRunReceipt]:
        with self.store.begin_read() as conn:
            rows = list(self.store.list_pending_cursor_initial_recoveries(conn))
        receipts: list[TickRunReceipt] = []
        for row in rows:
            try:
                self._resume(row)
            except (
                SchedulerEngineError,
                ProtectedArtifactError,
                CursorEvidenceError,
                ValidationError,
                OSError,
            ) as exc:
                receipts.append(
                    TickRunReceipt(
                        run_id=str(row["successor_run_id"]),
                        action="cursor_initial_recovery_evidence_invalid",
                        detail=type(exc).__name__,
                    )
                )
        return receipts

    def _reject_unless_eligible(self, analysis: CursorRecoveryAnalysisResult) -> None:
        evidence = analysis.evidence
        receipt = analysis.receipt
        if evidence is not None and evidence.sequence_id is not None:
            raise _invalid("forced Cursor recovery does not support sequence runs")
        if evidence is not None and evidence.turn_kind == "correction":
            raise _invalid("forced Cursor recovery does not support correction turns")
        if evidence is not None and (evidence.reviewer_bound or evidence.reviews_completed != 0):
            raise _invalid(
                "forced Cursor recovery does not support a run that already has reviewer B"
            )
        if not receipt.recovery_supported or evidence is None:
            raise _invalid(f"forced Cursor recovery is not eligible: {receipt.reason_code}")

    def _create_candidate(
        self,
        source_run_id: str,
        evidence: CursorRecoveryEvidenceBundle,
        recovery_key: str,
        now: datetime,
    ) -> sqlite3.Row:
        successor_run_id = self._run_id_factory(_project_name(self.store, source_run_id), now)
        self._require_current_source_authority(
            source_run_id,
            failed_attempt_id=evidence.failed_attempt_id,
            dispatch_id=evidence.dispatch_id,
            allowed_reservation_owners={source_run_id},
        )
        parent = _parent_recovery(self.store, self.artifacts, source_run_id)
        base_path, base_sha = _base_prompt_binding(self.artifacts, evidence, parent)
        intent = CursorInitialRecoveryPublicationIntentV1(
            schema_version=CURSOR_INITIAL_RECOVERY_INTENT_SCHEMA_VERSION,
            recovery_key=recovery_key,
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            failed_attempt_id=evidence.failed_attempt_id,
            dispatch_id=evidence.dispatch_id,
            status="pending",
            parent_recovery_key=None if parent is None else parent[0],
            record_sha256=None,
            base_prompt_path=base_path,
            base_prompt_sha256=base_sha,
            chat_id=evidence.chat_id,
            chat_owner_run_id=evidence.chat_owner_run_id,
            chat_artifact_path=evidence.chat_artifact_path,
            chat_artifact_sha256=evidence.chat_artifact_sha256,
            iteration=evidence.iteration,
            admitted_artifact_path=_admission_path(self.store, source_run_id),
            admitted_artifact_sha256=_admission_sha(self.store, source_run_id),
            plan_sha256=_context_sha(self.store, source_run_id, "plan"),
            submitted_prompt_sha256=_context_sha(self.store, source_run_id, "prompt"),
            config_sha256=_context_sha(self.store, source_run_id, "config"),
        )
        intent_bytes = intent.canonical_bytes()
        try:
            return self._insert_candidate(
                source_run_id,
                evidence,
                recovery_key,
                now,
                successor_run_id,
                intent,
                intent_bytes,
            )
        except sqlite3.IntegrityError:
            with self.store.begin_read() as conn:
                current = self.store.get_cursor_initial_recovery_by_key(
                    conn,
                    recovery_key=recovery_key,
                )
            if current is None:
                raise
            return current

    def _insert_candidate(
        self,
        source_run_id: str,
        evidence: CursorRecoveryEvidenceBundle,
        recovery_key: str,
        now: datetime,
        successor_run_id: str,
        intent: CursorInitialRecoveryPublicationIntentV1,
        intent_bytes: bytes,
    ) -> sqlite3.Row:
        with self.store.begin_immediate() as conn:
            current = self.store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
            if current is not None:
                return current
            self._require_current_source_authority(
                source_run_id,
                failed_attempt_id=evidence.failed_attempt_id,
                dispatch_id=evidence.dispatch_id,
                allowed_reservation_owners={source_run_id},
            )
            self._guard_publication_ledger(
                conn,
                source_run_id=source_run_id,
                failed_attempt_id=evidence.failed_attempt_id,
                allowed_reservation_owners={source_run_id},
            )
            state, _, _ = self.store.load_validated_snapshot(conn, source_run_id)
            if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
                raise _conflict("source run changed before initial recovery publication")
            successor = _successor_state(
                source=state,
                successor_run_id=successor_run_id,
                evidence=evidence,
                admitted_path=intent.admitted_artifact_path,
                admitted_sha=intent.admitted_artifact_sha256,
                now=now,
            )
            self._insert_successor(conn, successor, evidence, recovery_key, now)
            claimed = self.store.claim_reservation_for_successor(
                conn,
                successor_run_id=successor_run_id,
                source_run_id=source_run_id,
                worktree_key=state.context.repository.worktree_key,
                repository_root=state.context.repository.root,
                now=now,
            )
            if not claimed:
                raise _conflict("repository reservation is held by another run")
            inserted = self.store.insert_cursor_initial_recovery(
                conn,
                recovery_key=recovery_key,
                source_run_id=source_run_id,
                failed_attempt_id=evidence.failed_attempt_id,
                successor_run_id=successor_run_id,
                dispatch_id=evidence.dispatch_id,
                parent_recovery_key=intent.parent_recovery_key,
                intent_payload=intent_bytes.decode("utf-8"),
                intent_payload_sha256=hashlib.sha256(intent_bytes).hexdigest(),
                now=now,
            )
            if not inserted:
                current = self.store.get_cursor_initial_recovery_by_key(
                    conn, recovery_key=recovery_key
                )
                if current is None:
                    raise _conflict("initial recovery relation was not recorded")
                return current
            row = self.store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
        if row is None:
            raise _conflict("initial recovery relation disappeared after insert")
        return row

    def _insert_successor(
        self,
        conn: sqlite3.Connection,
        successor: CursorReadyState,
        evidence: CursorRecoveryEvidenceBundle,
        recovery_key: str,
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
        created = CursorInitialRecoverySuccessorCreatedEvent(
            source_run_id=evidence.run_id,
            successor_run_id=successor.run_id,
            recovery_key=recovery_key,
            failed_attempt_id=evidence.failed_attempt_id,
            iteration=evidence.iteration,
        )
        admitted = WorktreeAdmittedEvent(
            run_id=successor.run_id,
            admission_status_artifact_path=successor.checkpoint.admission_status_artifact_path,
            admission_status_sha256=successor.checkpoint.admission_status_sha256,
            resolved_root=successor.context.repository.root,
        )
        chat = CursorChatCreatedEvent(
            run_id=successor.run_id,
            chat_id=evidence.chat_id,
            chat_artifact_path=evidence.chat_artifact_path,
            chat_artifact_sha256=evidence.chat_artifact_sha256,
        )
        for event in (created, admitted, chat):
            self.store.append_event(
                conn,
                event_id=self._event_id_factory(),
                run_id=successor.run_id,
                sequence=self.store.next_event_sequence(conn, successor.run_id),
                event=event,
                now=now,
            )

    def _resume(self, row: sqlite3.Row) -> CursorInitialRecoveryResult:
        with self._publication_lock:
            try:
                return self._resume_locked(row)
            except OSError:
                with self.store.begin_read() as conn:
                    current = self.store.get_cursor_initial_recovery_by_key(
                        conn,
                        recovery_key=str(row["recovery_key"]),
                    )
                if current is None or str(current["status"]) != "ready":
                    raise
                return self._resume_locked(current)

    def _resume_locked(self, row: sqlite3.Row) -> CursorInitialRecoveryResult:
        intent = _verified_intent(row)
        status = str(row["status"])
        if status == "cancelled":
            raise _conflict("initial recovery publication was cancelled")
        if status == "ready":
            _verified_record(self.store, self.artifacts, row)
            return _result(intent, changed=False, replay=True, status="ready")
        if self._successor_cancelled(str(row["successor_run_id"])):
            self._mark_cancelled(row, intent)
            raise _conflict("initial recovery successor was aborted before publication")
        self._publish_files(row, intent)
        self._raise_fault("after_artifacts")
        ready = self._mark_ready(row, intent)
        self._raise_fault("after_ready")
        return ready

    def _successor_cancelled(self, successor_run_id: str) -> bool:
        with self.store.begin_read() as conn:
            if self.store.has_abort_requested_for_run(conn, successor_run_id):
                return True
            state, _, _ = self.store.load_validated_snapshot(conn, successor_run_id)
        return state.kind == "aborted"

    def _publish_files(
        self,
        row: sqlite3.Row,
        intent: CursorInitialRecoveryPublicationIntentV1,
    ) -> CursorInitialRecoveryRecordV1:
        self._require_current_source_authority(
            intent.source_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            dispatch_id=intent.dispatch_id,
            allowed_reservation_owners={intent.source_run_id, intent.successor_run_id},
        )
        parent: CursorInitialRecoveryRecordV1 | None = None
        if intent.parent_recovery_key:
            with self.store.begin_read() as conn:
                parent_row = self.store.get_cursor_initial_recovery_by_key(
                    conn,
                    recovery_key=intent.parent_recovery_key,
                )
            if parent_row is None or str(parent_row["status"]) != "ready":
                raise _corrupt("parent initial recovery record is missing")
            parent = _verified_record(self.store, self.artifacts, parent_row)
            if (
                parent.base_prompt_path != intent.base_prompt_path
                or parent.base_prompt_sha256 != intent.base_prompt_sha256
                or parent.chat_id != intent.chat_id
            ):
                raise _corrupt("parent initial recovery record does not match publication intent")
        base = self.artifacts.read_verified_bytes(
            intent.source_run_id,
            intent.base_prompt_path,
            expected_sha256=intent.base_prompt_sha256,
        )
        effective = effective_prompt_bytes(base)
        effective_sha = hashlib.sha256(effective).hexdigest()
        self._copy_tree(intent)
        self.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            intent.base_prompt_path,
            base,
            max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
        )
        self.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            EFFECTIVE_PROMPT_REL,
            effective,
            max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
        )
        record = CursorInitialRecoveryRecordV1(
            schema_version=CURSOR_INITIAL_RECOVERY_RECORD_SCHEMA_VERSION,
            turn_kind="initial",
            reviewer_created=False,
            reviews_completed=0,
            source_run_id=intent.source_run_id,
            successor_run_id=intent.successor_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            dispatch_id=intent.dispatch_id,
            chat_id=intent.chat_id,
            iteration=intent.iteration,
            admitted_artifact_path=intent.admitted_artifact_path,
            admitted_artifact_sha256=intent.admitted_artifact_sha256,
            plan_sha256=intent.plan_sha256,
            submitted_prompt_sha256=intent.submitted_prompt_sha256,
            config_sha256=intent.config_sha256,
            base_prompt_path=intent.base_prompt_path,
            base_prompt_sha256=intent.base_prompt_sha256,
            effective_prompt_path=EFFECTIVE_PROMPT_REL,
            effective_prompt_sha256=effective_sha,
            parent_source_run_id=None if parent is None else parent.source_run_id,
            parent_recovery_key=None if parent is None else intent.parent_recovery_key,
            parent_record_sha256=(
                None if parent is None else hashlib.sha256(parent.canonical_bytes()).hexdigest()
            ),
        )
        self.artifacts.publish_or_verify_bytes(
            intent.successor_run_id,
            RECORD_REL,
            record.canonical_bytes(),
            max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
        )
        return record

    def _copy_tree(self, intent: CursorInitialRecoveryPublicationIntentV1) -> None:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, intent.source_run_id)
        context = state.context
        copies = [
            (
                intent.source_run_id,
                context.plan_prompt.plan_artifact_path,
                context.plan_prompt.plan_sha256,
            ),
            (
                intent.source_run_id,
                context.plan_prompt.prompt_artifact_path,
                context.plan_prompt.prompt_sha256,
            ),
            (
                intent.source_run_id,
                context.effective_config.effective_config_artifact_path,
                context.effective_config.effective_config_sha256,
            ),
            (
                intent.source_run_id,
                context.effective_config.source_config_artifact_path,
                context.effective_config.source_config_sha256,
            ),
            (
                intent.source_run_id,
                intent.admitted_artifact_path,
                intent.admitted_artifact_sha256,
            ),
            (
                intent.chat_owner_run_id,
                intent.chat_artifact_path,
                intent.chat_artifact_sha256,
            ),
        ]
        if context.baseline_status_artifact_path and context.baseline_status_sha256:
            copies.append(
                (
                    intent.source_run_id,
                    context.baseline_status_artifact_path,
                    context.baseline_status_sha256,
                )
            )
        for owner, relative_path, digest in copies:
            payload = self.artifacts.read_verified_bytes(
                owner,
                relative_path,
                expected_sha256=digest,
            )
            self.artifacts.publish_or_verify_bytes(
                intent.successor_run_id,
                relative_path,
                payload,
                max_bytes=MAX_RECOVERY_ARTIFACT_BYTES,
            )

    def _require_current_source_authority(
        self,
        source_run_id: str,
        *,
        failed_attempt_id: str,
        dispatch_id: str,
        allowed_reservation_owners: set[str],
    ) -> None:
        with self.store.begin_read() as conn:
            self._guard_publication_ledger(
                conn,
                source_run_id=source_run_id,
                failed_attempt_id=failed_attempt_id,
                allowed_reservation_owners=allowed_reservation_owners,
            )
            attempt = self.store.get_attempt_by_id(conn, failed_attempt_id)
        if attempt is None or str(attempt["run_id"]) != source_run_id:
            raise _corrupt("decisive Cursor failure attempt is missing")
        if str(attempt["dispatch_id"]) != dispatch_id:
            raise _corrupt("decisive Cursor failure dispatch does not match publication intent")
        try:
            _authenticate_failed_cursor_turn(
                self.store,
                self.artifacts,
                evidence_run_id=source_run_id,
                attempt=attempt,
            )
        except (
            CursorEvidenceError,
            ProtectedArtifactError,
            OSError,
            ValidationError,
            ValueError,
        ) as exc:
            raise _corrupt("decisive Cursor failure evidence is no longer authenticated") from exc

    def _guard_publication_ledger(
        self,
        conn: sqlite3.Connection,
        intent: CursorInitialRecoveryPublicationIntentV1 | None = None,
        *,
        source_run_id: str | None = None,
        failed_attempt_id: str | None = None,
        allowed_reservation_owners: set[str],
    ) -> None:
        source_id = source_run_id if source_run_id is not None else intent.source_run_id  # type: ignore[union-attr]
        expected_attempt = (
            failed_attempt_id if failed_attempt_id is not None else intent.failed_attempt_id  # type: ignore[union-attr]
        )
        state, _, _ = self.store.load_validated_snapshot(conn, source_id)
        if not isinstance(state, BlockedState) or state.block_reason_kind != "cursor_failure":
            raise _conflict("source run is no longer a blocked cursor failure")
        if self.store.has_abort_requested_for_run(conn, source_id):
            raise _conflict("source run has a durable abort request")
        if self.store.has_checkpoint_reconciliation_hold(conn, source_id):
            raise _conflict("source run has an unresolved checkpoint hold")
        if self.store.has_unresolved_abort_hold(conn, source_id):
            raise _conflict("source run has unresolved abort reconciliation")
        if self.store.get_nonterminal_attempt_for_run(conn, source_id) is not None:
            raise _conflict("source run termination is no longer confirmed")
        active = self.store.get_active_reservation(conn, state.context.repository.worktree_key)
        if active is not None and str(active["run_id"]) not in allowed_reservation_owners:
            raise _conflict("repository reservation is held by another run")
        events = list(
            self.store.list_events_for_run(conn, source_id, limit=500, newest_first=False)
        )
        attempt_id, detail, _sequence = resolve_decisive_failed_cursor_attempt(
            self.store,
            conn,
            source_id,
            events,
        )
        if detail != "ok" or attempt_id != expected_attempt:
            raise _corrupt("source failure no longer matches the recovery intent")

    def _mark_ready(
        self,
        row: sqlite3.Row,
        intent: CursorInitialRecoveryPublicationIntentV1,
    ) -> CursorInitialRecoveryResult:
        now = self._now_factory()
        if self._before_ready_commit is not None:
            self._before_ready_commit()
        with self.store.begin_immediate() as conn:
            self._require_current_source_authority(
                intent.source_run_id,
                failed_attempt_id=intent.failed_attempt_id,
                dispatch_id=intent.dispatch_id,
                allowed_reservation_owners={intent.source_run_id, intent.successor_run_id},
            )
            record_bytes = (
                self.artifacts.run_root(intent.successor_run_id) / RECORD_REL
            ).read_bytes()
            try:
                record = CursorInitialRecoveryRecordV1.model_validate_json(record_bytes)
            except ValidationError as exc:
                raise _corrupt("initial recovery record failed authentication") from exc
            if record.canonical_bytes() != record_bytes:
                raise _corrupt("initial recovery record is not canonical")
            _assert_record_matches_durable_authority(self.store, self.artifacts, record, intent)
            record_sha = hashlib.sha256(record_bytes).hexdigest()
            ready_intent = intent.model_copy(
                update={"status": "ready", "record_sha256": record_sha}
            )
            ready_bytes = ready_intent.canonical_bytes()
            current = self.store.get_cursor_initial_recovery_by_key(
                conn,
                recovery_key=intent.recovery_key,
            )
            if current is None:
                raise _corrupt("initial recovery relation disappeared")
            if str(current["status"]) == "ready":
                _verified_record(self.store, self.artifacts, current)
                return _result(ready_intent, changed=False, replay=True, status="ready")
            if str(current["status"]) != "pending":
                raise _conflict("initial recovery publication was cancelled")
            if self.store.has_abort_requested_for_run(conn, intent.successor_run_id):
                raise _conflict("initial recovery successor was aborted")
            self._guard_publication_ledger(
                conn,
                intent,
                allowed_reservation_owners={intent.successor_run_id},
            )
            state, version, _ = self.store.load_validated_snapshot(conn, intent.successor_run_id)
            if not isinstance(state, CursorReadyState):
                raise _conflict("initial recovery successor is no longer waiting to publish")
            if state.codex.reviewer_session_id or state.codex.reviews_completed != 0:
                raise _corrupt("initial recovery successor acquired reviewer state")
            updated = state.model_copy(
                update={
                    "version": version + 1,
                    "updated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "cursor": state.cursor.model_copy(
                        update={
                            "continuation_envelope_path": record.effective_prompt_path,
                            "continuation_envelope_sha256": record.effective_prompt_sha256,
                            "chat_id": record.chat_id,
                            "iteration": record.iteration,
                        }
                    ),
                }
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=intent.successor_run_id,
                expected_version=version,
                new_state=updated,
                now=now,
            ):
                raise _conflict("initial recovery successor changed during publication")
            changed = self.store.update_cursor_initial_recovery_status(
                conn,
                recovery_key=intent.recovery_key,
                expected_status="pending",
                status="ready",
                intent_payload=ready_bytes.decode("utf-8"),
                intent_payload_sha256=hashlib.sha256(ready_bytes).hexdigest(),
                record_artifact_path=RECORD_REL,
                record_sha256=record_sha,
                now=now,
            )
            if not changed:
                raise _conflict("initial recovery publication status changed")
            pending = [
                effect
                for effect in self.store.list_eligible_effects(
                    conn,
                    run_id=intent.successor_run_id,
                    now=now,
                )
                if str(effect["effect_kind"]) == RUN_CURSOR_TURN_EFFECT_KIND
            ]
            if not pending:
                created_event = conn.execute(
                    """
                    SELECT event_id FROM scheduler_events
                    WHERE run_id = ? AND event_kind = ?
                    ORDER BY sequence ASC
                    LIMIT 1
                    """,
                    (intent.successor_run_id, "cursor_initial_recovery_successor_created"),
                ).fetchone()
                if created_event is None:
                    raise _corrupt("initial recovery successor event is missing")
                self.store.insert_effect(
                    conn,
                    dispatch_id=self._dispatch_id_factory(),
                    source_event_id=str(created_event["event_id"]),
                    run_id=intent.successor_run_id,
                    effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                    effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                    effect_payload={"iteration": record.iteration},
                    available_at=now,
                    claimed_run_version=updated.version,
                    now=now,
                )
        return _result(ready_intent, changed=True, replay=False, status="ready")

    def _mark_cancelled(
        self,
        row: sqlite3.Row,
        intent: CursorInitialRecoveryPublicationIntentV1,
    ) -> None:
        cancelled = intent.model_copy(update={"status": "cancelled", "record_sha256": None})
        payload = cancelled.canonical_bytes()
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            self.store.update_cursor_initial_recovery_status(
                conn,
                recovery_key=str(row["recovery_key"]),
                expected_status="pending",
                status="cancelled",
                intent_payload=payload.decode("utf-8"),
                intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
                record_artifact_path=None,
                record_sha256=None,
                now=now,
            )

    def _raise_fault(self, point: str) -> None:
        if self._fault_point == point:
            raise CursorInitialRecoveryFault(point)


def reconcile_pending_cursor_initial_recoveries(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    now: datetime | None = None,
) -> list[TickRunReceipt]:
    service = CursorInitialRecoveryService(
        store,
        artifacts,
        now_factory=(lambda: now) if now is not None else None,
    )
    return service.reconcile_pending()


def render_cursor_initial_recovery_output(
    result: CursorInitialRecoveryResult,
    *,
    output: str,
) -> str:
    if output == "json":
        return result.model_dump_json(indent=2) + "\n"
    reuse = "reused" if result.idempotent_replay else "created"
    ready = "yes" if result.agent_execution_ready else "no"
    return (
        f"Cursor initial recovery: source={result.source_run_id} "
        f"successor={result.successor_run_id} {reuse} "
        f"publication={result.publication_status} agent_execution_ready={ready}\n"
    )


def scheduler_cursor_initial_recovery_force(
    run_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> CursorInitialRecoveryResult:
    store = SqliteSchedulerStore(db_path if db_path is not None else default_engine_db_path())
    artifacts = ProtectedArtifactStore(
        artifact_root if artifact_root is not None else default_artifact_root()
    )
    return CursorInitialRecoveryService(store, artifacts).force(run_id)


def _result(
    intent: CursorInitialRecoveryPublicationIntentV1,
    *,
    changed: bool,
    replay: bool,
    status: PublicationStatus,
) -> CursorInitialRecoveryResult:
    ready = status == "ready"
    if ready:
        action = active_cursor_safe_next_action()
    else:
        action = SafeNextAction(
            kind=SafeNextActionKind.SCHEDULER_TICK,
            command="ai_dev_loop scheduler tick",
        )
    return CursorInitialRecoveryResult(
        source_run_id=intent.source_run_id,
        successor_run_id=intent.successor_run_id,
        changed=changed,
        idempotent_replay=replay,
        publication_status=status,
        agent_execution_ready=ready,
        safe_next_action=action,
    )


def _verified_intent(row: sqlite3.Row) -> CursorInitialRecoveryPublicationIntentV1:
    payload = str(row["intent_payload"]).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    if digest != str(row["intent_payload_sha256"]):
        raise _corrupt("initial recovery publication intent digest mismatch")
    try:
        intent = CursorInitialRecoveryPublicationIntentV1.model_validate_json(payload)
    except ValidationError as exc:
        raise _corrupt("initial recovery publication intent failed authentication") from exc
    if intent.canonical_bytes() != payload:
        raise _corrupt("initial recovery publication intent is not canonical")
    if (
        intent.recovery_key != str(row["recovery_key"])
        or intent.source_run_id != str(row["source_run_id"])
        or intent.successor_run_id != str(row["successor_run_id"])
        or intent.failed_attempt_id != str(row["failed_attempt_id"])
        or intent.dispatch_id != str(row["dispatch_id"])
        or intent.status != str(row["status"])
    ):
        raise _corrupt("initial recovery publication intent disagrees with the ledger relation")
    stored_record = row["record_sha256"]
    if (intent.record_sha256 or None) != (str(stored_record) if stored_record else None):
        raise _corrupt("initial recovery record digest disagrees with publication intent")
    parent = row["parent_recovery_key"]
    if (intent.parent_recovery_key or None) != (str(parent) if parent else None):
        raise _corrupt("initial recovery parent provenance disagrees with the ledger relation")
    return intent


def _last_parsed_event(
    events: list[sqlite3.Row],
    event_type: type[WorktreeAdmittedEvent] | type[CursorChatCreatedEvent],
) -> WorktreeAdmittedEvent | CursorChatCreatedEvent | None:
    from ai_dev_loop.scheduler.application.review_recovery import _parse_event_row

    found: WorktreeAdmittedEvent | CursorChatCreatedEvent | None = None
    for row in events:
        if str(row["event_kind"]) != event_type.model_fields["kind"].default:
            continue
        parsed = _parse_event_row(row)
        if isinstance(parsed, event_type):
            found = parsed
    return found


def _assert_record_matches_durable_authority(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    record: CursorInitialRecoveryRecordV1,
    intent: CursorInitialRecoveryPublicationIntentV1,
) -> None:
    """Reject hash-consistent records that disagree with ledger authority."""

    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, record.source_run_id)
        attempt = store.get_attempt_by_id(conn, record.failed_attempt_id)
        events = list(
            store.list_events_for_run(conn, record.source_run_id, limit=500, newest_first=False)
        )
    if attempt is None or str(attempt["run_id"]) != record.source_run_id:
        raise _corrupt("initial recovery record attempt is not on the source run")
    if str(attempt["dispatch_id"]) != record.dispatch_id:
        raise _corrupt("initial recovery record dispatch disagrees with the failed attempt")
    context = source.context
    if (
        context.plan_prompt.plan_sha256 != record.plan_sha256
        or context.plan_prompt.prompt_sha256 != record.submitted_prompt_sha256
        or context.effective_config.effective_config_sha256 != record.config_sha256
    ):
        raise _corrupt("initial recovery record disagrees with frozen inputs")
    admitted = _last_parsed_event(events, WorktreeAdmittedEvent)
    if (
        not isinstance(admitted, WorktreeAdmittedEvent)
        or admitted.admission_status_artifact_path != record.admitted_artifact_path
        or admitted.admission_status_sha256 != record.admitted_artifact_sha256
    ):
        raise _corrupt("initial recovery admitted checkpoint disagrees with durable admission")
    chat = _last_parsed_event(events, CursorChatCreatedEvent)
    if (
        not isinstance(chat, CursorChatCreatedEvent)
        or chat.chat_id != record.chat_id
        or chat.chat_artifact_path != intent.chat_artifact_path
        or chat.chat_artifact_sha256 != intent.chat_artifact_sha256
    ):
        raise _corrupt("initial recovery chat binding disagrees with durable chat evidence")
    try:
        binding, _outcome, iteration = _authenticate_failed_cursor_turn(
            store,
            artifacts,
            evidence_run_id=record.source_run_id,
            attempt=attempt,
        )
    except (
        CursorEvidenceError,
        ProtectedArtifactError,
        OSError,
        ValidationError,
        ValueError,
    ) as exc:
        raise _corrupt(
            "initial recovery causal failure evidence is no longer authenticated"
        ) from exc
    if iteration != record.iteration:
        raise _corrupt("initial recovery iteration disagrees with the failed invocation")
    prompt_path = str(binding.get("prompt_path", "")).strip()
    prompt_sha = str(binding.get("prompt_sha256", "")).strip()
    if record.parent_recovery_key is None:
        expected_path = record.base_prompt_path
        expected_sha = record.base_prompt_sha256
        mismatch = "initial recovery base prompt disagrees with the failed invocation"
    else:
        expected_path = record.effective_prompt_path
        expected_sha = record.effective_prompt_sha256
        mismatch = "initial recovery effective prompt disagrees with the failed invocation"
    if prompt_path != expected_path or prompt_sha != expected_sha:
        raise _corrupt(mismatch)


def _verified_record(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    row: sqlite3.Row,
) -> CursorInitialRecoveryRecordV1:
    intent = _verified_intent(row)
    if intent.status != "ready" or not intent.record_sha256:
        raise _corrupt("initial recovery record is not published")
    path = str(row["record_artifact_path"] or "")
    if path != RECORD_REL:
        raise _corrupt("initial recovery record path is not the v1 artifact")
    try:
        payload = artifacts.read_verified_bytes(
            intent.successor_run_id,
            path,
            expected_sha256=intent.record_sha256,
        )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("initial recovery record failed authentication") from exc
    try:
        record = CursorInitialRecoveryRecordV1.model_validate_json(payload)
    except ValidationError as exc:
        raise _corrupt("initial recovery record failed authentication") from exc
    if hashlib.sha256(record.canonical_bytes()).hexdigest() != intent.record_sha256:
        raise _corrupt("initial recovery record bytes do not match the ledger digest")
    if (
        record.source_run_id != intent.source_run_id
        or record.successor_run_id != intent.successor_run_id
        or record.failed_attempt_id != intent.failed_attempt_id
        or record.dispatch_id != intent.dispatch_id
        or record.chat_id != intent.chat_id
        or record.iteration != intent.iteration
        or record.admitted_artifact_path != intent.admitted_artifact_path
        or record.admitted_artifact_sha256 != intent.admitted_artifact_sha256
        or record.plan_sha256 != intent.plan_sha256
        or record.submitted_prompt_sha256 != intent.submitted_prompt_sha256
        or record.config_sha256 != intent.config_sha256
        or record.base_prompt_path != intent.base_prompt_path
        or record.base_prompt_sha256 != intent.base_prompt_sha256
        or record.effective_prompt_path != EFFECTIVE_PROMPT_REL
        or (record.parent_recovery_key or None) != (intent.parent_recovery_key or None)
    ):
        raise _corrupt("initial recovery record disagrees with publication intent")
    try:
        base = artifacts.read_verified_bytes(
            record.successor_run_id,
            record.base_prompt_path,
            expected_sha256=record.base_prompt_sha256,
        )
        effective = artifacts.read_verified_bytes(
            record.successor_run_id,
            record.effective_prompt_path,
            expected_sha256=record.effective_prompt_sha256,
        )
        artifacts.read_verified_bytes(
            record.successor_run_id,
            record.admitted_artifact_path,
            expected_sha256=record.admitted_artifact_sha256,
        )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("initial recovery prompt binding failed authentication") from exc
    if effective != effective_prompt_bytes(base):
        raise _corrupt("effective recovery prompt is not the authenticated base plus one note")
    _assert_record_matches_durable_authority(store, artifacts, record, intent)
    if record.parent_recovery_key is None:
        return record
    with store.begin_read() as conn:
        immediate = store.get_cursor_initial_recovery_by_successor(
            conn,
            successor_run_id=record.source_run_id,
        )
    if immediate is None or str(immediate["status"]) != "ready":
        raise _corrupt("immediate parent initial recovery record is missing")
    if (
        str(immediate["recovery_key"]) != record.parent_recovery_key
        or str(immediate["successor_run_id"]) != record.source_run_id
    ):
        raise _corrupt("initial recovery parent is not the immediate source successor")
    parent_row = immediate
    if str(parent_row["record_sha256"] or "") != record.parent_record_sha256:
        raise _corrupt("parent initial recovery digest disagrees with the child record")
    parent = _verified_record(store, artifacts, parent_row)
    if (
        parent.source_run_id != record.parent_source_run_id
        or parent.base_prompt_path != record.base_prompt_path
        or parent.base_prompt_sha256 != record.base_prompt_sha256
        or parent.chat_id != record.chat_id
        or hashlib.sha256(parent.canonical_bytes()).hexdigest() != record.parent_record_sha256
    ):
        raise _corrupt("parent initial recovery record does not match the child record")
    return record


def _generate_successor_run_id(project_name: str, now: datetime) -> str:
    return generate_run_id(project_name, now=now)


def _parent_recovery(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    source_run_id: str,
) -> tuple[str, CursorInitialRecoveryRecordV1] | None:
    with store.begin_read() as conn:
        row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=source_run_id)
    if row is None:
        return None
    if str(row["status"]) != "ready":
        raise _invalid("parent initial recovery publication is not ready")
    return str(row["recovery_key"]), _verified_record(store, artifacts, row)


def _base_prompt_binding(
    artifacts: ProtectedArtifactStore,
    evidence: CursorRecoveryEvidenceBundle,
    parent: tuple[str, CursorInitialRecoveryRecordV1] | None,
) -> tuple[str, str]:
    if parent is None:
        artifacts.read_verified_bytes(
            evidence.run_id,
            evidence.prompt_path,
            expected_sha256=evidence.prompt_sha256,
        )
        return evidence.prompt_path, evidence.prompt_sha256
    _parent_key, parent_record = parent
    if evidence.chat_id != parent_record.chat_id:
        raise _corrupt("failed successor chat does not match the parent recovery record")
    if (
        evidence.prompt_path != parent_record.effective_prompt_path
        or evidence.prompt_sha256 != parent_record.effective_prompt_sha256
    ):
        raise _corrupt("failed successor prompt does not match the parent recovery record")
    return parent_record.base_prompt_path, parent_record.base_prompt_sha256


def _project_name(store: SqliteSchedulerStore, run_id: str) -> str:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    return state.context.project_name


def _admission_event(store: SqliteSchedulerStore, run_id: str) -> WorktreeAdmittedEvent:
    with store.begin_read() as conn:
        events = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
    for row in events:
        if str(row["event_kind"]) != "worktree_admitted":
            continue
        from ai_dev_loop.scheduler.application.review_recovery import _parse_event_row

        parsed = _parse_event_row(row)
        if isinstance(parsed, WorktreeAdmittedEvent):
            return parsed
    raise _invalid("blocked source lacks an admitted checkpoint")


def _admission_path(store: SqliteSchedulerStore, run_id: str) -> str:
    return _admission_event(store, run_id).admission_status_artifact_path


def _admission_sha(store: SqliteSchedulerStore, run_id: str) -> str:
    return _admission_event(store, run_id).admission_status_sha256


def _context_sha(store: SqliteSchedulerStore, run_id: str, kind: str) -> str:
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    if kind == "plan":
        return state.context.plan_prompt.plan_sha256
    if kind == "prompt":
        return state.context.plan_prompt.prompt_sha256
    return state.context.effective_config.effective_config_sha256


def _successor_state(
    *,
    source: BlockedState,
    successor_run_id: str,
    evidence: CursorRecoveryEvidenceBundle,
    admitted_path: str,
    admitted_sha: str,
    now: datetime,
) -> CursorReadyState:
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return CursorReadyState(
        run_id=successor_run_id,
        version=1,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=secrets.token_hex(32),
        context=source.context,
        checkpoint=AdmittedRunCheckpoint(
            authorized_at=source.authorized_at or source.blocked_at,
            authorized_controller_session_id=source.authorized_controller_session_id,
            admitted_at=source.blocked_at,
            admission_status_artifact_path=admitted_path,
            admission_status_sha256=admitted_sha,
        ),
        cursor=CursorWorkflowCheckpoint(
            iteration=evidence.iteration,
            chat_id=evidence.chat_id,
            chat_artifact_path=evidence.chat_artifact_path,
            chat_artifact_sha256=evidence.chat_artifact_sha256,
            original_prompt_path=source.context.plan_prompt.prompt_artifact_path,
            original_prompt_sha256=source.context.plan_prompt.prompt_sha256,
        ),
        codex=CodexWorkflowCheckpoint(),
    )
