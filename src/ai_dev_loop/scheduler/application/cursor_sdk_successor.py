"""Manual same-agent recovery successors for blocked Cursor SDK turns."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError as ModelValidationError

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
    CursorInitialRecoveryResult,
)
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECOVERY_NOTE
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    SDK_SUCCESSOR_BASE_PROMPT_REL,
    SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
    SDK_SUCCESSOR_RECORD_REL,
    CursorSdkReconciliationIntentV1,
    CursorSdkSuccessorIntentV1,
    CursorSdkSuccessorRecordV1,
)
from ai_dev_loop.scheduler.domain.events import (
    CursorInitialRecoverySuccessorCreatedEvent,
    CursorSdkAgentCreatedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    BlockedState,
    CursorReadyState,
    CursorSdkBinding,
    SchedulerState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import generate_run_id, sha256_bytes, utc_now


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def _conflict(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CONFLICT, message)


def _corrupt(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.CORRUPTION, message)


def _row_intent(row: sqlite3.Row) -> CursorSdkSuccessorIntentV1:
    payload = str(row["intent_payload"])
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != str(row["intent_payload_sha256"]):
        raise _corrupt("Cursor SDK successor intent digest mismatch")
    try:
        intent = CursorSdkSuccessorIntentV1.model_validate_json(payload)
    except ModelValidationError as exc:
        raise _corrupt("Cursor SDK successor intent is invalid") from exc
    if (
        intent.recovery_key != str(row["recovery_key"])
        or intent.source_run_id != str(row["source_run_id"])
        or intent.successor_run_id != str(row["successor_run_id"])
        or intent.failed_attempt_id != str(row["failed_attempt_id"])
    ):
        raise _corrupt("Cursor SDK successor intent disagrees with ledger")
    return intent


def _reconciliation_intent(row: sqlite3.Row) -> CursorSdkReconciliationIntentV1:
    payload = str(row["intent_payload"])
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != str(row["intent_payload_sha256"]):
        raise _corrupt("Cursor SDK reconciliation intent digest mismatch")
    try:
        return CursorSdkReconciliationIntentV1.model_validate_json(payload)
    except ModelValidationError as exc:
        raise _corrupt("Cursor SDK reconciliation intent is invalid") from exc


class CursorSdkSuccessorService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory
        self.run_id = run_id_factory or (lambda project, now: generate_run_id(project, now=now))
        self.event_id = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self.dispatch_id = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def force(self, source_run_id: str) -> CursorInitialRecoveryResult:
        with self.store.begin_read() as conn:
            existing = self.store.list_cursor_initial_recoveries_for_source(
                conn, source_run_id=source_run_id
            )
            sdk_rows = [
                row
                for row in existing
                if json.loads(str(row["intent_payload"])).get("schema_version") == 3
            ]
        if len(sdk_rows) > 1:
            raise _corrupt("multiple Cursor SDK successors exist for one source")
        if sdk_rows:
            return self._resume(sdk_rows[0], replay=True)
        now = self.now()
        with self.store.begin_immediate() as conn:
            source, _, _ = self.store.load_validated_snapshot(conn, source_run_id)
            if (
                not isinstance(source, BlockedState)
                or source.block_reason_kind != "cursor_failure"
                or not isinstance(source.context.cursor, CursorSdkBinding)
            ):
                raise _invalid("Cursor SDK forced recovery requires a cursor_failure block")
            if (
                self.store.get_nonterminal_attempt_for_run(conn, source_run_id) is not None
                or self.store.has_abort_requested_for_run(conn, source_run_id)
                or self.store.has_unresolved_abort_hold(conn, source_run_id)
                or self.store.has_checkpoint_reconciliation_hold(conn, source_run_id)
            ):
                raise _invalid(
                    "Cursor SDK forced recovery requires proved process termination and no abort hold"
                )
            from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
                decisive_ready_sdk_reconciliation,
            )

            relation, rec_intent, result = decisive_ready_sdk_reconciliation(
                self.store,
                self.artifacts,
                conn,
                state=source,
            )
            result_path = str(relation["result_artifact_path"] or "")
            result_sha = str(relation["result_sha256"] or "")
            if not result_path or not result_sha:
                raise _corrupt("Cursor SDK reconciliation result binding is incomplete")
            if result.outcome != "continuation_ready":
                raise _invalid("Cursor SDK successor requires a reconciled failed native turn")
            source_turn: CursorReadyState | WaitingForCursorFixState
            try:
                if rec_intent.turn_kind == "correction":
                    source_turn = WaitingForCursorFixState.model_validate(rec_intent.source_state)
                else:
                    source_turn = CursorReadyState.model_validate(rec_intent.source_state)
            except ModelValidationError as exc:
                raise _corrupt("Cursor SDK predecessor checkpoint is invalid") from exc
            if source_turn.run_id != source_run_id or source_turn.cursor.sdk is None:
                raise _corrupt("Cursor SDK predecessor checkpoint identity is invalid")
            successor_id = self.run_id(source.context.project_name, now)
            recovery_key = hashlib.sha256(
                json.dumps(
                    {
                        "schema": "cursor-sdk-successor-key-v1",
                        "source_run_id": source_run_id,
                        "failed_attempt_id": rec_intent.source_attempt_id,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            pending = self._pending_intent(
                source_run_id=source_run_id,
                successor_run_id=successor_id,
                rec_intent=rec_intent,
                relation_path=result_path,
                relation_sha=result_sha,
                source_turn=source_turn,
                recovery_key=recovery_key,
            )
            successor = self._successor_state(
                successor_run_id=successor_id,
                source_turn=source_turn,
                now=now,
            )
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
                    successor_id,
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
            if not self.store.claim_reservation_for_successor(
                conn,
                successor_run_id=successor_id,
                source_run_id=source_run_id,
                worktree_key=source.context.repository.worktree_key,
                repository_root=source.context.repository.root,
                now=now,
            ):
                raise _conflict("repository reservation is held by another run")
            pending_bytes = pending.canonical_bytes()
            if not self.store.insert_cursor_initial_recovery(
                conn,
                recovery_key=recovery_key,
                source_run_id=source_run_id,
                failed_attempt_id=rec_intent.source_attempt_id,
                successor_run_id=successor_id,
                dispatch_id=rec_intent.source_dispatch_id,
                parent_recovery_key=None,
                intent_payload=pending_bytes.decode("utf-8"),
                intent_payload_sha256=hashlib.sha256(pending_bytes).hexdigest(),
                now=now,
            ):
                raise _conflict("Cursor SDK successor relation already exists")
            self.store.copy_run_short_name_if_absent(
                conn,
                source_run_id=source_run_id,
                successor_run_id=successor_id,
                now=now,
            )
            created = CursorInitialRecoverySuccessorCreatedEvent(
                source_run_id=source_run_id,
                successor_run_id=successor_id,
                recovery_key=recovery_key,
                failed_attempt_id=rec_intent.source_attempt_id,
                iteration=rec_intent.iteration,
            )
            admitted = WorktreeAdmittedEvent(
                run_id=successor_id,
                admission_status_artifact_path=source_turn.checkpoint.admission_status_artifact_path,
                admission_status_sha256=source_turn.checkpoint.admission_status_sha256,
                resolved_root=source.context.repository.root,
            )
            agent = CursorSdkAgentCreatedEvent(
                run_id=successor_id,
                agent_id=source_turn.cursor.sdk.agent_id,
                conversation_key=source_turn.cursor.sdk.conversation_key,
                identity_artifact_path=source_turn.cursor.sdk.identity_artifact_path,
                identity_artifact_sha256=source_turn.cursor.sdk.identity_artifact_sha256,
                journal_path=source_turn.cursor.sdk.latest_journal_path,
                journal_sha256=source_turn.cursor.sdk.latest_journal_sha256,
            )
            for event in (created, admitted, agent):
                self.store.append_event(
                    conn,
                    event_id=self.event_id(),
                    run_id=successor_id,
                    sequence=self.store.next_event_sequence(conn, successor_id),
                    event=event,
                    now=now,
                )
            if source.context.sequence is not None:
                from ai_dev_loop.scheduler.application.sequence_cursor_recovery import (
                    bind_pending_cursor_sequence_intent,
                )

                bind_pending_cursor_sequence_intent(
                    self.store,
                    conn,
                    source_run_id=source_run_id,
                    successor_run_id=successor_id,
                    recovery_key=recovery_key,
                    now=now,
                )
            row = self.store.get_cursor_initial_recovery_by_key(conn, recovery_key=recovery_key)
        if row is None:
            raise _conflict("Cursor SDK successor relation disappeared")
        return self._resume(row, replay=False)

    def _pending_intent(
        self,
        *,
        source_run_id: str,
        successor_run_id: str,
        rec_intent: CursorSdkReconciliationIntentV1,
        relation_path: str,
        relation_sha: str,
        source_turn: CursorReadyState | WaitingForCursorFixState,
        recovery_key: str,
    ) -> CursorSdkSuccessorIntentV1:
        codex = source_turn.codex
        correction = rec_intent.turn_kind == "correction"
        return CursorSdkSuccessorIntentV1(
            recovery_key=recovery_key,
            status="pending",
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            failed_attempt_id=rec_intent.source_attempt_id,
            dispatch_id=rec_intent.source_dispatch_id,
            turn_kind=rec_intent.turn_kind,
            iteration=rec_intent.iteration,
            conversation_owner_run_id=rec_intent.conversation_owner_run_id,
            conversation_key=rec_intent.conversation_key,
            agent_id=rec_intent.agent_id,
            identity_owner_run_id=source_run_id,
            identity_artifact_path=rec_intent.identity_artifact_path,
            identity_artifact_sha256=rec_intent.identity_artifact_sha256,
            reconciliation_owner_run_id=source_run_id,
            reconciliation_artifact_path=relation_path,
            reconciliation_artifact_sha256=relation_sha,
            base_prompt_owner_run_id=source_run_id,
            base_prompt_path=rec_intent.prompt_path,
            base_prompt_sha256=rec_intent.prompt_sha256,
            admitted_owner_run_id=source_run_id,
            admitted_artifact_path=source_turn.checkpoint.admission_status_artifact_path,
            admitted_artifact_sha256=source_turn.checkpoint.admission_status_sha256,
            reviews_completed=rec_intent.reviews_completed,
            submitted_max_review_iterations=rec_intent.submitted_max_review_iterations,
            effective_review_ceiling=rec_intent.effective_review_ceiling,
            reviewer_session_id=codex.reviewer_session_id if correction else None,
            reviewer_binding_owner_run_id=source_run_id if correction else None,
            reviewer_binding_artifact_path=codex.binding_artifact_path if correction else None,
            reviewer_binding_artifact_sha256=codex.binding_artifact_sha256 if correction else None,
            fix_owner_run_id=source_run_id if correction else None,
            fix_prompt_path=codex.latest_fix_prompt_path if correction else None,
            fix_prompt_sha256=codex.latest_fix_prompt_sha256 if correction else None,
            staged_owner_run_id=source_run_id if correction else None,
            staged_patch_path=source_turn.cursor.staged_patch_path if correction else None,
            staged_patch_sha256=source_turn.cursor.staged_patch_sha256 if correction else None,
            review_owner_run_id=source_run_id if correction else None,
            review_result_path=codex.latest_review_result_path if correction else None,
            review_result_sha256=codex.latest_review_result_sha256 if correction else None,
        )

    def _successor_state(
        self,
        *,
        successor_run_id: str,
        source_turn: CursorReadyState | WaitingForCursorFixState,
        now: datetime,
    ) -> CursorReadyState | WaitingForCursorFixState:
        cursor = source_turn.cursor.model_copy(
            update={
                "sdk_recovery_pending": None,
                "sdk_reconciliation_path": None,
                "sdk_reconciliation_sha256": None,
                "timeout_attempt_id": None,
                "wait_until": None,
                "continuation_envelope_path": None,
                "continuation_envelope_sha256": None,
            }
        )
        return source_turn.model_copy(
            update={
                "run_id": successor_run_id,
                "version": 1,
                "submitted_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "updated_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "idempotency_key": secrets.token_hex(32),
                "cursor": cursor,
            }
        )

    def _resume(
        self,
        row: sqlite3.Row,
        *,
        replay: bool,
    ) -> CursorInitialRecoveryResult:
        intent = _row_intent(row)
        if str(row["status"]) == "ready":
            authenticate_sdk_successor_authority(
                self.store, self.artifacts, None, intent.successor_run_id
            )
            return self._result(intent, changed=False, replay=True)
        if str(row["status"]) != "pending":
            raise _conflict("Cursor SDK successor publication was cancelled")
        pending_sha = str(row["intent_payload_sha256"])
        self._publish_artifacts(intent, pending_sha=pending_sha)
        record = CursorSdkSuccessorRecordV1(
            recovery_key=intent.recovery_key,
            source_run_id=intent.source_run_id,
            successor_run_id=intent.successor_run_id,
            failed_attempt_id=intent.failed_attempt_id,
            turn_kind=intent.turn_kind,
            iteration=intent.iteration,
            conversation_owner_run_id=intent.conversation_owner_run_id,
            conversation_key=intent.conversation_key,
            agent_id=intent.agent_id,
            base_prompt_path=SDK_SUCCESSOR_BASE_PROMPT_REL,
            base_prompt_sha256=self._base_bytes(intent)[1],
            effective_prompt_path=SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
            effective_prompt_sha256=sha256_bytes(self._effective_bytes(intent)),
            reconciliation_artifact_path=intent.reconciliation_artifact_path,
            reconciliation_artifact_sha256=intent.reconciliation_artifact_sha256,
            reviews_completed=intent.reviews_completed,
            submitted_max_review_iterations=intent.submitted_max_review_iterations,
            effective_review_ceiling=intent.effective_review_ceiling,
            pending_intent_sha256=pending_sha,
        )
        record_bytes = record.canonical_bytes()
        record_sha = sha256_bytes(record_bytes)
        self.artifacts.write_bytes_or_verify(
            intent.successor_run_id,
            SDK_SUCCESSOR_RECORD_REL,
            record_bytes,
            max_bytes=128_000,
        )
        ready = intent.model_copy(
            update={
                "status": "ready",
                "effective_prompt_path": SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
                "effective_prompt_sha256": record.effective_prompt_sha256,
                "record_sha256": record_sha,
            }
        )
        ready_bytes = ready.canonical_bytes()
        now = self.now()
        with self.store.begin_immediate() as conn:
            current = self.store.get_cursor_initial_recovery_by_key(
                conn, recovery_key=intent.recovery_key
            )
            if current is None:
                raise _conflict("Cursor SDK successor relation disappeared")
            if str(current["status"]) == "pending":
                if not self.store.update_cursor_initial_recovery_status(
                    conn,
                    recovery_key=intent.recovery_key,
                    expected_status="pending",
                    status="ready",
                    intent_payload=ready_bytes.decode("utf-8"),
                    intent_payload_sha256=hashlib.sha256(ready_bytes).hexdigest(),
                    record_artifact_path=SDK_SUCCESSOR_RECORD_REL,
                    record_sha256=record_sha,
                    now=now,
                ):
                    raise _conflict("Cursor SDK successor status changed")
                state, version, _ = self.store.load_validated_snapshot(
                    conn, intent.successor_run_id
                )
                source_event = conn.execute(
                    """
                    SELECT event_id FROM scheduler_events
                    WHERE run_id = ? ORDER BY sequence DESC LIMIT 1
                    """,
                    (intent.successor_run_id,),
                ).fetchone()
                if source_event is None:
                    raise _corrupt("Cursor SDK successor publication event is missing")
                self.store.insert_effect(
                    conn,
                    dispatch_id=self.dispatch_id(),
                    source_event_id=str(source_event["event_id"]),
                    run_id=intent.successor_run_id,
                    effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                    effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                    effect_payload={"iteration": intent.iteration},
                    available_at=now,
                    claimed_run_version=version,
                    now=now,
                )
        if intent.source_run_id and self._source_has_sequence(intent.source_run_id):
            from ai_dev_loop.scheduler.application.sequence_cursor_recovery import (
                ensure_sequence_cursor_adoption,
            )

            ensure_sequence_cursor_adoption(
                self.store,
                self.artifacts,
                source_run_id=intent.source_run_id,
                successor_run_id=intent.successor_run_id,
                recovery_key=intent.recovery_key,
                now=now,
            )
        authenticated_sdk_successor_launch(
            self.store,
            self.artifacts,
            None,
            intent.successor_run_id,
        )
        return self._result(ready, changed=not replay, replay=replay)

    def _source_has_sequence(self, run_id: str) -> bool:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        return state.context.sequence is not None

    def _base_bytes(self, intent: CursorSdkSuccessorIntentV1) -> tuple[bytes, str]:
        with self.store.begin_read() as conn:
            parent = self.store.get_cursor_initial_recovery_by_successor(
                conn, successor_run_id=intent.source_run_id
            )
        if parent is not None:
            try:
                parent_intent = _row_intent(parent)
                parent_record = authenticate_sdk_successor_authority(
                    self.store, self.artifacts, None, parent_intent.successor_run_id
                )
                if parent_record is None:
                    raise _corrupt("Cursor SDK parent successor authority is missing")
                if (
                    parent_record.turn_kind == intent.turn_kind
                    and parent_record.iteration == intent.iteration
                    and parent_record.effective_prompt_sha256 == intent.base_prompt_sha256
                ):
                    base = self.artifacts.read_verified_bytes(
                        intent.source_run_id,
                        parent_record.base_prompt_path,
                        expected_sha256=parent_record.base_prompt_sha256,
                    )
                    return base, parent_record.base_prompt_sha256
            except (SchedulerEngineError, ProtectedArtifactError, OSError):
                raise
        base = self.artifacts.read_verified_bytes(
            intent.base_prompt_owner_run_id,
            intent.base_prompt_path,
            expected_sha256=intent.base_prompt_sha256,
        )
        return base, intent.base_prompt_sha256

    def _effective_bytes(self, intent: CursorSdkSuccessorIntentV1) -> bytes:
        base, _ = self._base_bytes(intent)
        return base + b"\n\n" + RECOVERY_NOTE.encode("utf-8") + b"\n"

    def _copy(
        self,
        intent: CursorSdkSuccessorIntentV1,
        owner: str,
        path: str,
        digest: str,
    ) -> None:
        content = self.artifacts.read_verified_bytes(owner, path, expected_sha256=digest)
        self.artifacts.write_bytes_or_verify(
            intent.successor_run_id, path, content, max_bytes=max(len(content), 1)
        )

    def _publish_artifacts(self, intent: CursorSdkSuccessorIntentV1, *, pending_sha: str) -> None:
        del pending_sha
        with self.store.begin_read() as conn:
            source, _, _ = self.store.load_validated_snapshot(conn, intent.source_run_id)
        context = source.context
        context_artifacts = [
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
        if context.baseline_status_artifact_path and context.baseline_status_sha256:
            context_artifacts.append(
                (
                    context.baseline_status_artifact_path,
                    context.baseline_status_sha256,
                )
            )
        for path, digest in context_artifacts:
            self._copy(intent, intent.source_run_id, path, digest)
        from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
            _publish_fresh_reviewer_input,
        )

        _publish_fresh_reviewer_input(
            self.store,
            self.artifacts,
            successor_run_id=intent.successor_run_id,
            start_run_id=intent.source_run_id,
            context=context,
        )
        base, _ = self._base_bytes(intent)
        self.artifacts.write_bytes_or_verify(
            intent.successor_run_id,
            SDK_SUCCESSOR_BASE_PROMPT_REL,
            base,
            max_bytes=max(len(base), 1),
        )
        effective = self._effective_bytes(intent)
        self.artifacts.write_bytes_or_verify(
            intent.successor_run_id,
            SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
            effective,
            max_bytes=max(len(effective), 1),
        )
        for owner, path, digest in (
            (
                intent.identity_owner_run_id,
                intent.identity_artifact_path,
                intent.identity_artifact_sha256,
            ),
            (
                intent.reconciliation_owner_run_id,
                intent.reconciliation_artifact_path,
                intent.reconciliation_artifact_sha256,
            ),
            (
                intent.admitted_owner_run_id,
                intent.admitted_artifact_path,
                intent.admitted_artifact_sha256,
            ),
        ):
            self._copy(intent, owner, path, digest)
        optional_artifacts: tuple[tuple[str | None, str | None, str | None], ...] = (
            (
                intent.reviewer_binding_owner_run_id,
                intent.reviewer_binding_artifact_path,
                intent.reviewer_binding_artifact_sha256,
            ),
            (intent.fix_owner_run_id, intent.fix_prompt_path, intent.fix_prompt_sha256),
            (
                intent.staged_owner_run_id,
                intent.staged_patch_path,
                intent.staged_patch_sha256,
            ),
            (
                intent.review_owner_run_id,
                intent.review_result_path,
                intent.review_result_sha256,
            ),
        )
        for optional_owner, optional_path, optional_digest in optional_artifacts:
            if (
                optional_owner is not None
                and optional_path is not None
                and optional_digest is not None
            ):
                self._copy(intent, optional_owner, optional_path, optional_digest)
        # The latest journal is independently bound by the copied identity checkpoint.
        with self.store.begin_read() as conn:
            relation = self.store.get_cursor_sdk_reconciliation_by_source_attempt(
                conn, source_attempt_id=intent.failed_attempt_id
            )
        if relation is not None:
            rec = _reconciliation_intent(relation)
            self._copy(
                intent,
                intent.source_run_id,
                rec.source_journal_path,
                rec.source_journal_sha256,
            )

    def _result(
        self,
        intent: CursorSdkSuccessorIntentV1,
        *,
        changed: bool,
        replay: bool,
    ) -> CursorInitialRecoveryResult:
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, intent.successor_run_id)
            action = safe_next_action_for_scheduler_state(self.store, conn, state)
        return CursorInitialRecoveryResult(
            source_run_id=intent.source_run_id,
            successor_run_id=intent.successor_run_id,
            changed=changed,
            idempotent_replay=replay,
            publication_status="ready",
            agent_execution_ready=True,
            safe_next_action=action,
            turn_kind=intent.turn_kind,
        )


def authenticate_sdk_successor_authority(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection | None,
    run_id: str,
) -> CursorSdkSuccessorRecordV1 | None:
    """Authenticate immutable SDK successor ancestry in any downstream state."""

    if conn is None:
        with store.begin_read() as opened:
            return authenticate_sdk_successor_authority(store, artifacts, opened, run_id)
    row = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
    if row is None:
        return None
    raw = json.loads(str(row["intent_payload"]))
    if raw.get("schema_version") != 3:
        return None
    intent = _row_intent(row)
    if str(row["status"]) != "ready" or intent.status != "ready":
        raise _invalid("Cursor SDK successor publication is not ready")
    record_path = str(row["record_artifact_path"] or "")
    record_sha = str(row["record_sha256"] or "")
    if record_path != SDK_SUCCESSOR_RECORD_REL or record_sha != intent.record_sha256:
        raise _corrupt("Cursor SDK successor record binding is invalid")
    try:
        payload = artifacts.read_verified_bytes(run_id, record_path, expected_sha256=record_sha)
        record = CursorSdkSuccessorRecordV1.model_validate_json(payload)
        base = artifacts.read_verified_bytes(
            run_id, record.base_prompt_path, expected_sha256=record.base_prompt_sha256
        )
        effective = artifacts.read_verified_bytes(
            run_id,
            record.effective_prompt_path,
            expected_sha256=record.effective_prompt_sha256,
        )
    except (ProtectedArtifactError, OSError, ModelValidationError) as exc:
        raise _corrupt("Cursor SDK successor artifacts failed authentication") from exc
    if (
        record.recovery_key != intent.recovery_key
        or record.source_run_id != intent.source_run_id
        or record.successor_run_id != run_id
        or record.failed_attempt_id != intent.failed_attempt_id
        or record.turn_kind != intent.turn_kind
        or record.iteration != intent.iteration
        or record.conversation_owner_run_id != intent.conversation_owner_run_id
        or record.agent_id != intent.agent_id
        or record.conversation_key != intent.conversation_key
        or record.base_prompt_path != SDK_SUCCESSOR_BASE_PROMPT_REL
        or record.effective_prompt_path != SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL
        or record.effective_prompt_sha256 != intent.effective_prompt_sha256
        or record.reconciliation_artifact_path != intent.reconciliation_artifact_path
        or record.reconciliation_artifact_sha256 != intent.reconciliation_artifact_sha256
        or effective != base + b"\n\n" + RECOVERY_NOTE.encode("utf-8") + b"\n"
        or hashlib.sha256(base).hexdigest() != record.base_prompt_sha256
        or hashlib.sha256(effective).hexdigest() != record.effective_prompt_sha256
        or record.reviews_completed != intent.reviews_completed
        or record.submitted_max_review_iterations != intent.submitted_max_review_iterations
        or record.effective_review_ceiling != intent.effective_review_ceiling
    ):
        raise _corrupt("Cursor SDK successor record disagrees with frozen authority")
    from ai_dev_loop.scheduler.application.recovery_ancestry import (
        RecoveryAncestryError,
        recovery_ancestor_edges,
    )

    try:
        recovery_ancestor_edges(store, conn, run_id)
    except RecoveryAncestryError as exc:
        raise _corrupt(str(exc)) from exc
    parent = store.get_cursor_initial_recovery_by_successor(
        conn,
        successor_run_id=intent.source_run_id,
    )
    if parent is None:
        expected_base_sha = intent.base_prompt_sha256
    else:
        parent_record = authenticate_sdk_successor_authority(
            store,
            artifacts,
            conn,
            intent.source_run_id,
        )
        if parent_record is None:
            raise _corrupt("Cursor SDK parent successor authority is missing")
        expected_base_sha = (
            parent_record.base_prompt_sha256
            if parent_record.turn_kind == intent.turn_kind
            and parent_record.iteration == intent.iteration
            and parent_record.effective_prompt_sha256 == intent.base_prompt_sha256
            else intent.base_prompt_sha256
        )
    if record.base_prompt_sha256 != expected_base_sha:
        raise _corrupt("Cursor SDK successor base prompt changed across recovery lineage")
    relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
        conn,
        source_attempt_id=intent.failed_attempt_id,
    )
    if relation is None:
        raise _corrupt("Cursor SDK successor reconciliation authority is missing")
    from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
        authenticate_ready_sdk_reconciliation,
    )

    rec_intent, reconciliation = authenticate_ready_sdk_reconciliation(
        store,
        artifacts,
        conn,
        run_id=intent.source_run_id,
        row=relation,
    )
    if (
        reconciliation.outcome != "continuation_ready"
        or rec_intent.source_attempt_id != intent.failed_attempt_id
        or rec_intent.source_dispatch_id != intent.dispatch_id
        or rec_intent.turn_kind != intent.turn_kind
        or rec_intent.iteration != intent.iteration
        or rec_intent.conversation_owner_run_id != intent.conversation_owner_run_id
        or rec_intent.conversation_key != intent.conversation_key
        or rec_intent.agent_id != intent.agent_id
        or str(relation["result_artifact_path"] or "") != intent.reconciliation_artifact_path
        or str(relation["result_sha256"] or "") != intent.reconciliation_artifact_sha256
        or rec_intent.reviews_completed != intent.reviews_completed
        or rec_intent.submitted_max_review_iterations != intent.submitted_max_review_iterations
        or rec_intent.effective_review_ceiling != intent.effective_review_ceiling
    ):
        raise _corrupt("Cursor SDK successor disagrees with reconciled source authority")
    try:
        source_turn = (
            WaitingForCursorFixState.model_validate(rec_intent.source_state)
            if rec_intent.turn_kind == "correction"
            else CursorReadyState.model_validate(rec_intent.source_state)
        )
    except ModelValidationError as exc:
        raise _corrupt("Cursor SDK successor source checkpoint is invalid") from exc
    current, _, _ = store.load_validated_snapshot(conn, run_id)
    current_cursor = getattr(current, "cursor", None)
    if (
        source_turn.run_id != intent.source_run_id
        or intent.identity_owner_run_id != intent.source_run_id
        or intent.identity_artifact_path != rec_intent.identity_artifact_path
        or intent.identity_artifact_sha256 != rec_intent.identity_artifact_sha256
        or intent.reconciliation_owner_run_id != intent.source_run_id
        or intent.base_prompt_owner_run_id != intent.source_run_id
        or intent.base_prompt_path != rec_intent.prompt_path
        or intent.base_prompt_sha256 != rec_intent.prompt_sha256
        or intent.admitted_owner_run_id != intent.source_run_id
        or intent.admitted_artifact_path != source_turn.checkpoint.admission_status_artifact_path
        or intent.admitted_artifact_sha256 != source_turn.checkpoint.admission_status_sha256
        or current.context != source_turn.context
        or (
            current_cursor is not None
            and (
                current_cursor.sdk is None
                or current_cursor.sdk.agent_id != intent.agent_id
                or current_cursor.sdk.conversation_owner_run_id != intent.conversation_owner_run_id
                or current_cursor.sdk.conversation_key != intent.conversation_key
            )
        )
    ):
        raise _corrupt("Cursor SDK successor changed frozen settings or identity")
    if intent.turn_kind == "correction" and (
        intent.reviewer_session_id != source_turn.codex.reviewer_session_id
        or intent.reviewer_binding_owner_run_id != intent.source_run_id
        or intent.reviewer_binding_artifact_path != source_turn.codex.binding_artifact_path
        or intent.reviewer_binding_artifact_sha256 != source_turn.codex.binding_artifact_sha256
        or intent.fix_owner_run_id != intent.source_run_id
        or intent.fix_prompt_path != source_turn.codex.latest_fix_prompt_path
        or intent.fix_prompt_sha256 != source_turn.codex.latest_fix_prompt_sha256
        or intent.staged_owner_run_id != intent.source_run_id
        or intent.staged_patch_path != source_turn.cursor.staged_patch_path
        or intent.staged_patch_sha256 != source_turn.cursor.staged_patch_sha256
        or intent.review_owner_run_id != intent.source_run_id
        or intent.review_result_path != source_turn.codex.latest_review_result_path
        or intent.review_result_sha256 != source_turn.codex.latest_review_result_sha256
    ):
        raise _corrupt("Cursor SDK correction successor changed reviewer or staged evidence")
    copied_artifacts = [
        (
            source_turn.context.plan_prompt.plan_artifact_path,
            source_turn.context.plan_prompt.plan_sha256,
        ),
        (
            source_turn.context.plan_prompt.prompt_artifact_path,
            source_turn.context.plan_prompt.prompt_sha256,
        ),
        (
            source_turn.context.effective_config.effective_config_artifact_path,
            source_turn.context.effective_config.effective_config_sha256,
        ),
        (
            source_turn.context.effective_config.source_config_artifact_path,
            source_turn.context.effective_config.source_config_sha256,
        ),
        (intent.identity_artifact_path, intent.identity_artifact_sha256),
        (intent.reconciliation_artifact_path, intent.reconciliation_artifact_sha256),
        (intent.admitted_artifact_path, intent.admitted_artifact_sha256),
    ]
    if (
        source_turn.context.baseline_status_artifact_path
        and source_turn.context.baseline_status_sha256
    ):
        copied_artifacts.append(
            (
                source_turn.context.baseline_status_artifact_path,
                source_turn.context.baseline_status_sha256,
            )
        )
    context_codex = source_turn.context.codex
    if hasattr(context_codex, "binding_artifact_path") and hasattr(context_codex, "binding_sha256"):
        copied_artifacts.append(
            (
                context_codex.binding_artifact_path,
                context_codex.binding_sha256,
            )
        )
    for optional_path, optional_sha in (
        (intent.reviewer_binding_artifact_path, intent.reviewer_binding_artifact_sha256),
        (intent.fix_prompt_path, intent.fix_prompt_sha256),
        (intent.staged_patch_path, intent.staged_patch_sha256),
        (intent.review_result_path, intent.review_result_sha256),
    ):
        if optional_path is not None and optional_sha is not None:
            copied_artifacts.append((optional_path, optional_sha))
    try:
        for copied_path, copied_sha in copied_artifacts:
            artifacts.read_verified_bytes(
                run_id,
                copied_path,
                expected_sha256=copied_sha,
            )
    except (ProtectedArtifactError, OSError) as exc:
        raise _corrupt("Cursor SDK successor copied evidence failed authentication") from exc
    pending = intent.model_copy(
        update={
            "status": "pending",
            "effective_prompt_path": None,
            "effective_prompt_sha256": None,
            "record_sha256": None,
        }
    )
    if hashlib.sha256(pending.canonical_bytes()).hexdigest() != record.pending_intent_sha256:
        raise _corrupt("Cursor SDK successor pending authority digest is invalid")
    return record


def sdk_successor_prompt_applies(
    state: SchedulerState,
    record: CursorSdkSuccessorRecordV1,
) -> bool:
    """Limit the recovery prompt override to the authorized logical turn."""

    cursor = getattr(state, "cursor", None)
    if cursor is None or cursor.iteration != record.iteration:
        return False
    if record.turn_kind == "initial":
        return isinstance(state, CursorReadyState)
    return isinstance(state, WaitingForCursorFixState)


def authenticated_sdk_successor_launch(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection | None,
    run_id: str,
) -> CursorSdkSuccessorRecordV1 | None:
    """Return authenticated successor authority only while its turn is dispatchable."""

    if conn is None:
        with store.begin_read() as opened:
            return authenticated_sdk_successor_launch(store, artifacts, opened, run_id)
    record = authenticate_sdk_successor_authority(store, artifacts, conn, run_id)
    if record is None:
        return None
    state, _, _ = store.load_validated_snapshot(conn, run_id)
    return record if sdk_successor_prompt_applies(state, record) else None


def authenticate_sdk_successor_for_adoption(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
) -> str | None:
    intent = _row_intent(row)
    record = authenticate_sdk_successor_authority(store, artifacts, conn, intent.successor_run_id)
    return None if record is None else hashlib.sha256(record.canonical_bytes()).hexdigest()
