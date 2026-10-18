"""Durable Cursor SDK native-run reconciliation after a terminated worker."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError as ModelValidationError

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    frozen_repository_identity,
    verify_cursor_invocation_evidence,
    verify_recorded_cursor_fingerprint,
)
from ai_dev_loop.scheduler.application.cursor_sdk_journal import (
    authenticate_sdk_recovery_journal,
    seal_interrupted_sdk_send_journal,
)
from ai_dev_loop.scheduler.application.run_state_bridge import run_state_from_scheduler_context
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import (
    NORMALIZE_STAGING_EFFECT_ID,
    NORMALIZE_STAGING_EFFECT_KIND,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_ID,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_reconciliation_record_rel,
)
from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
    CursorSdkReconciliationIntentV1,
    CursorSdkReconciliationRecordV1,
)
from ai_dev_loop.scheduler.domain.events import (
    CURSOR_SDK_RECONCILED_EVENT_KIND,
    CURSOR_TURN_BLOCKED_EVENT_KIND,
    CursorSdkReconciledEvent,
    CursorSdkReconciliationRequestedEvent,
    CursorSdkTurnCompletedEvent,
    CursorTurnBlockedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import apply_cursor_turn_blocked
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    BlockedState,
    CursorReadyState,
    CursorSdkBinding,
    CursorSdkRecoveryPending,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.paths import resolve_run_relative_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_file, utc_now

SDK_ACTIVE_STATES = (CursorReadyState, WaitingForCursorFixState, WaitingUsageLimitState)
CAPACITY_FALLBACK = timedelta(hours=5)
TERMINAL_NATIVE_STATUSES = frozenset({"finished", "failed", "cancelled", "canceled", "error"})


class _ArtifactReader(Protocol):
    def run_root(self, run_id: str) -> Path: ...

    def read_verified_bytes(
        self,
        run_id: str,
        relative_path: str,
        *,
        expected_sha256: str,
    ) -> bytes: ...


def _invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def sdk_reconciliation_key(run_id: str, source_attempt_id: str) -> str:
    return canonical_json_sha256(
        {
            "schema": "cursor-sdk-reconciliation-key-v1",
            "run_id": run_id,
            "source_attempt_id": source_attempt_id,
        }
    )


def _intent_from_row(row: sqlite3.Row) -> CursorSdkReconciliationIntentV1:
    payload = str(row["intent_payload"])
    if hashlib.sha256(payload.encode("utf-8")).hexdigest() != str(row["intent_payload_sha256"]):
        raise _invalid("Cursor SDK reconciliation intent digest mismatch")
    try:
        intent = CursorSdkReconciliationIntentV1.model_validate_json(payload)
    except ModelValidationError as exc:
        raise _invalid("Cursor SDK reconciliation intent is invalid") from exc
    if (
        intent.run_id != str(row["run_id"])
        or intent.source_attempt_id != str(row["source_attempt_id"])
        or intent.source_dispatch_id != str(row["source_dispatch_id"])
    ):
        raise _invalid("Cursor SDK reconciliation intent disagrees with ledger")
    return intent


def _authenticate_reconciliation_record(
    artifacts: _ArtifactReader,
    *,
    run_id: str,
    intent: CursorSdkReconciliationIntentV1,
    record_path: str,
    record_sha256: str,
) -> CursorSdkReconciliationRecordV1:
    if not record_path or not record_sha256:
        raise _invalid("Cursor SDK reconciliation record binding is invalid")
    try:
        payload = artifacts.read_verified_bytes(
            run_id,
            record_path,
            expected_sha256=record_sha256,
        )
        record = CursorSdkReconciliationRecordV1.model_validate_json(payload)
        journal = authenticate_sdk_recovery_journal(
            resolve_run_relative_path(
                artifacts.run_root(run_id),
                intent.source_journal_path,
            ),
            expected_sha256=intent.source_journal_sha256,
            expected_binding={
                "attempt_id": intent.source_attempt_id,
                "dispatch_id": intent.source_dispatch_id,
                "conversation_owner_run_id": intent.conversation_owner_run_id,
                "conversation_key": intent.conversation_key,
                "agent_id": intent.agent_id,
                "prompt_sha256": intent.prompt_sha256,
            },
        )
    except (ProtectedArtifactError, OSError, ModelValidationError, ValueError) as exc:
        raise _invalid("Cursor SDK reconciliation record failed authentication") from exc
    expected_path = cursor_sdk_reconciliation_record_rel(
        intent.source_attempt_id,
        record.reconciliation_attempt_id,
    )
    legacy_path = cursor_sdk_reconciliation_record_rel(intent.source_attempt_id)
    if record_path not in {expected_path, legacy_path}:
        raise _invalid("Cursor SDK reconciliation record path changed worker identity")
    raw_baseline = journal[0].get("run_ids")
    if not isinstance(raw_baseline, list):
        raise _invalid("Cursor SDK reconciliation inventory baseline is invalid")
    baseline = tuple(sorted(str(item) for item in raw_baseline))
    candidates = tuple(record.candidate_run_ids)
    if (
        record.run_id != intent.run_id
        or record.run_id != run_id
        or record.source_attempt_id != intent.source_attempt_id
        or record.source_dispatch_id != intent.source_dispatch_id
        or record.conversation_owner_run_id != intent.conversation_owner_run_id
        or record.conversation_key != intent.conversation_key
        or record.agent_id != intent.agent_id
        or record.prompt_sha256 != intent.prompt_sha256
        or record.source_journal_path != intent.source_journal_path
        or record.source_journal_sha256 != intent.source_journal_sha256
        or record.baseline_run_ids != baseline
        or set(record.baseline_run_ids).intersection(candidates)
    ):
        raise _invalid("Cursor SDK reconciliation record disagrees with frozen authority")
    if intent.native_run_id is not None:
        if record.native_run_id != intent.native_run_id or candidates != (intent.native_run_id,):
            raise _invalid("Cursor SDK reconciliation changed the known native run")
    elif record.native_run_id is not None and candidates != (record.native_run_id,):
        raise _invalid("Cursor SDK reconciliation did not prove a unique native run")
    if record.outcome == "ambiguous_missing" and candidates:
        raise _invalid("missing-run reconciliation cannot carry candidates")
    if record.outcome == "ambiguous_multiple" and len(candidates) < 2:
        raise _invalid("multiple-run reconciliation requires conflicting candidates")
    if record.outcome in {"continuation_ready", "terminal_recovered"} and (
        record.native_run_id is None
        or record.initial_native_status is None
        or record.final_native_status is None
        or record.final_native_status.lower() not in TERMINAL_NATIVE_STATUSES
        or record.candidate_final_statuses.get(record.native_run_id)
        != record.final_native_status.lower()
    ):
        raise _invalid("Cursor SDK continuation lacks confirmed terminal native evidence")
    if (
        record.outcome == "terminal_recovered"
        and (record.final_native_status or "").lower() != "finished"
    ):
        raise _invalid("Cursor SDK terminal replay did not finish the exact native run")
    if record.outcome == "abort_reconciled" and any(
        status.lower() not in TERMINAL_NATIVE_STATUSES
        for status in record.candidate_final_statuses.values()
    ):
        raise _invalid("Cursor SDK abort reconciliation retained a live candidate")
    return record


def authenticate_ready_sdk_reconciliation(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    row: sqlite3.Row,
) -> tuple[CursorSdkReconciliationIntentV1, CursorSdkReconciliationRecordV1]:
    """Authenticate one ready ledger relation and all native evidence it names."""

    if str(row["run_id"]) != run_id or str(row["status"]) != "ready":
        raise _invalid("Cursor SDK reconciliation relation is not ready")
    intent = _intent_from_row(row)
    record = _authenticate_reconciliation_record(
        artifacts,
        run_id=run_id,
        intent=intent,
        record_path=str(row["result_artifact_path"] or ""),
        record_sha256=str(row["result_sha256"] or ""),
    )
    source_attempt = store.get_attempt_by_id(conn, intent.source_attempt_id)
    if (
        source_attempt is None
        or str(source_attempt["run_id"]) != run_id
        or str(source_attempt["dispatch_id"]) != intent.source_dispatch_id
        or str(source_attempt["component"]) != "cursor"
        or str(source_attempt["status"]) not in {"completed", "failed"}
        or int(source_attempt["ingested"]) != 1
    ):
        raise _invalid("Cursor SDK reconciliation source attempt changed owners")
    return intent, record


def authenticated_published_reconciliation_result(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    recovery_key: str,
    reconciliation_attempt_id: str,
    reconciliation_dispatch_id: str,
) -> tuple[str, str] | None:
    """Authenticate a record published before its worker lost the result envelope."""

    row = store.get_cursor_sdk_reconciliation_by_key(conn, recovery_key=recovery_key)
    if row is None or str(row["run_id"]) != run_id or str(row["status"]) != "pending":
        raise _invalid("interrupted Cursor SDK reconciliation authority is not pending")
    intent = _intent_from_row(row)
    record_path = cursor_sdk_reconciliation_record_rel(
        intent.source_attempt_id,
        reconciliation_attempt_id,
    )
    absolute = resolve_run_relative_path(artifacts.run_root(run_id), record_path)
    if not absolute.is_file():
        historical_path = str(row["result_artifact_path"] or "")
        historical_sha = str(row["result_sha256"] or "")
        if not historical_path and not historical_sha:
            return None
        if not historical_path or not historical_sha:
            raise _invalid("historical Cursor SDK reconciliation binding is incomplete")
        historical = _authenticate_reconciliation_record(
            artifacts,
            run_id=run_id,
            intent=intent,
            record_path=historical_path,
            record_sha256=historical_sha,
        )
        if (
            historical.reconciliation_attempt_id == reconciliation_attempt_id
            or historical.reconciliation_dispatch_id == reconciliation_dispatch_id
        ):
            raise _invalid("current reconciliation result moved to a historical path")
        historical_attempt = store.get_attempt_by_id(
            conn,
            historical.reconciliation_attempt_id,
        )
        historical_dispatch = store.get_effect_by_dispatch_id(
            conn,
            historical.reconciliation_dispatch_id,
        )
        if (
            historical_attempt is None
            or historical_dispatch is None
            or str(historical_attempt["run_id"]) != run_id
            or str(historical_attempt["dispatch_id"]) != historical.reconciliation_dispatch_id
            or str(historical_attempt["component"]) != "cursor"
            or str(historical_attempt["status"]) not in {"completed", "failed"}
            or int(historical_attempt["ingested"]) != 1
            or str(historical_dispatch["run_id"]) != run_id
            or str(historical_dispatch["effect_kind"]) != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
        ):
            raise _invalid("historical Cursor SDK reconciliation worker is unauthenticated")
        try:
            historical_effect = json.loads(str(historical_dispatch["effect_payload"]))
        except (TypeError, ValueError) as exc:
            raise _invalid("historical reconciliation effect payload is invalid") from exc
        if (
            not isinstance(historical_effect, dict)
            or str(historical_effect.get("recovery_key", "")) != recovery_key
        ):
            raise _invalid("historical reconciliation changed native obligation")
        return None
    record_sha = sha256_file(absolute)
    record = _authenticate_reconciliation_record(
        artifacts,
        run_id=run_id,
        intent=intent,
        record_path=record_path,
        record_sha256=record_sha,
    )
    if (
        record.reconciliation_attempt_id != reconciliation_attempt_id
        or record.reconciliation_dispatch_id != reconciliation_dispatch_id
    ):
        raise _invalid("published Cursor SDK reconciliation record changed worker identity")
    return record_path, record_sha


def decisive_ready_sdk_reconciliation(
    store: SqliteSchedulerStore,
    artifacts: _ArtifactReader,
    conn: sqlite3.Connection,
    *,
    state: BlockedState,
) -> tuple[sqlite3.Row, CursorSdkReconciliationIntentV1, CursorSdkReconciliationRecordV1]:
    """Resolve the exact ready reconciliation carried by the decisive failed turn."""

    block_row = conn.execute(
        """
        SELECT * FROM scheduler_events
        WHERE run_id = ? AND event_kind = ?
        ORDER BY sequence DESC LIMIT 1
        """,
        (state.run_id, CURSOR_TURN_BLOCKED_EVENT_KIND),
    ).fetchone()
    if block_row is None:
        raise _invalid("blocked Cursor SDK run lacks a causal failure event")
    block_payload = str(block_row["event_payload"])
    if hashlib.sha256(block_payload.encode("utf-8")).hexdigest() != str(
        block_row["event_payload_sha256"]
    ):
        raise _invalid("decisive Cursor SDK failure event digest mismatch")
    try:
        block = CursorTurnBlockedEvent.model_validate_json(block_payload)
    except ModelValidationError as exc:
        raise _invalid("decisive Cursor SDK failure event is invalid") from exc
    if block.run_id != state.run_id or block.block_reason_kind != state.block_reason_kind:
        raise _invalid("decisive Cursor SDK failure event disagrees with blocked state")
    reconciled_row = conn.execute(
        """
        SELECT * FROM scheduler_events
        WHERE run_id = ? AND sequence < ?
        ORDER BY sequence DESC LIMIT 1
        """,
        (state.run_id, int(block_row["sequence"])),
    ).fetchone()
    if reconciled_row is None or str(reconciled_row["event_kind"]) != (
        CURSOR_SDK_RECONCILED_EVENT_KIND
    ):
        raise _invalid("blocked Cursor SDK run lacks adjacent reconciliation authority")
    reconciled_payload = str(reconciled_row["event_payload"])
    if hashlib.sha256(reconciled_payload.encode("utf-8")).hexdigest() != str(
        reconciled_row["event_payload_sha256"]
    ):
        raise _invalid("decisive Cursor SDK reconciliation event digest mismatch")
    try:
        reconciled = CursorSdkReconciledEvent.model_validate_json(reconciled_payload)
    except ModelValidationError as exc:
        raise _invalid("decisive Cursor SDK reconciliation event is invalid") from exc
    row = store.get_cursor_sdk_reconciliation_by_key(
        conn,
        recovery_key=reconciled.recovery_key,
    )
    if (
        row is None
        or str(row["run_id"]) != state.run_id
        or str(row["source_attempt_id"]) != reconciled.source_attempt_id
        or str(row["result_artifact_path"] or "") != reconciled.record_path
        or str(row["result_sha256"] or "") != reconciled.record_sha256
    ):
        raise _invalid("decisive Cursor SDK reconciliation ledger authority is conflicting")
    intent, record = authenticate_ready_sdk_reconciliation(
        store,
        artifacts,
        conn,
        run_id=state.run_id,
        row=row,
    )
    if record.outcome != "continuation_ready":
        raise _invalid("decisive Cursor SDK reconciliation is not recoverable")
    return row, intent, record


class CursorSdkReconciliationService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory
        self.event_id = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self.dispatch_id = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def request(
        self,
        run_id: str,
        *,
        source_attempt_id: str,
        source_dispatch_id: str,
        outcome: dict[str, object],
        failure_class: str,
        retry_after_seconds: int | None = None,
    ) -> TickRunReceipt:
        now = self.now()
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
                authenticated_sdk_successor_launch,
            )
            from ai_dev_loop.scheduler.application.review_budget import (
                effective_review_ceiling_for_run,
            )

            effective_ceiling = effective_review_ceiling_for_run(
                self.store, conn, state, artifacts=self.artifacts
            )
            sdk_successor = authenticated_sdk_successor_launch(
                self.store,
                self.artifacts,
                conn,
                run_id,
            )
        if not isinstance(state, (CursorReadyState, WaitingForCursorFixState)):
            return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
        if not isinstance(state.context.cursor, CursorSdkBinding) or state.cursor.sdk is None:
            raise _invalid("Cursor SDK reconciliation requires an exact agent binding")
        journal_rel = str(outcome.get("journal_path", ""))
        journal_sha = str(outcome.get("journal_sha256", ""))
        if not journal_rel or not journal_sha:
            raise _invalid("Cursor SDK reconciliation requires a protected send journal")
        prompt_path = (
            state.codex.latest_correction_envelope_path
            if isinstance(state, WaitingForCursorFixState)
            else state.context.plan_prompt.prompt_artifact_path
        )
        prompt_sha = (
            state.codex.latest_correction_envelope_sha256
            if isinstance(state, WaitingForCursorFixState)
            else state.context.plan_prompt.prompt_sha256
        )
        if sdk_successor is not None:
            if sdk_successor.iteration != state.cursor.iteration:
                raise _invalid("Cursor SDK successor prompt iteration changed")
            prompt_path = sdk_successor.effective_prompt_path
            prompt_sha = sdk_successor.effective_prompt_sha256
        if not prompt_path or not prompt_sha:
            raise _invalid("Cursor SDK reconciliation prompt binding is missing")
        sdk = state.cursor.sdk
        expected = {
            "attempt_id": source_attempt_id,
            "dispatch_id": source_dispatch_id,
            "conversation_owner_run_id": sdk.conversation_owner_run_id,
            "conversation_key": sdk.conversation_key,
            "agent_id": sdk.agent_id,
            "prompt_sha256": prompt_sha,
        }
        records = authenticate_sdk_recovery_journal(
            resolve_run_relative_path(self.artifacts.run_root(run_id), journal_rel),
            expected_sha256=journal_sha,
            expected_binding=expected,
        )
        native_run_id = next(
            (
                str(record["native_run_id"])
                for record in records
                if record.get("kind") == "native_run_bound" and record.get("native_run_id")
            ),
            None,
        )
        key = sdk_reconciliation_key(run_id, source_attempt_id)
        intent = CursorSdkReconciliationIntentV1(
            run_id=run_id,
            source_attempt_id=source_attempt_id,
            source_dispatch_id=source_dispatch_id,
            turn_kind="correction" if isinstance(state, WaitingForCursorFixState) else "initial",
            iteration=state.cursor.iteration,
            conversation_owner_run_id=sdk.conversation_owner_run_id,
            conversation_key=sdk.conversation_key,
            agent_id=sdk.agent_id,
            prompt_path=prompt_path,
            prompt_sha256=prompt_sha,
            source_journal_path=journal_rel,
            source_journal_sha256=journal_sha,
            identity_artifact_path=sdk.identity_artifact_path,
            identity_artifact_sha256=sdk.identity_artifact_sha256,
            source_state=state.model_dump(mode="json"),
            reviews_completed=state.codex.reviews_completed,
            submitted_max_review_iterations=state.context.workflow.max_review_iterations,
            effective_review_ceiling=effective_ceiling,
            native_run_id=native_run_id,
            failure_class=failure_class,  # type: ignore[arg-type]
            retry_after_seconds=retry_after_seconds,
        )
        payload = intent.canonical_bytes()
        pending = CursorSdkRecoveryPending(
            recovery_key=key,
            source_attempt_id=source_attempt_id,
            source_dispatch_id=source_dispatch_id,
            turn_kind=intent.turn_kind,
            prompt_path=prompt_path,
            prompt_sha256=prompt_sha,
            source_journal_path=journal_rel,
            source_journal_sha256=journal_sha,
            native_run_id=native_run_id,
            failure_class=intent.failure_class,
            retry_after_seconds=retry_after_seconds,
        )
        with self.store.begin_immediate() as conn:
            current, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(current, (CursorReadyState, WaitingForCursorFixState)):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            source_attempt = self.store.get_attempt_by_id(conn, source_attempt_id)
            source_effect = self.store.get_effect_by_dispatch_id(conn, source_dispatch_id)
            if (
                source_attempt is None
                or source_effect is None
                or str(source_attempt["run_id"]) != run_id
                or str(source_attempt["dispatch_id"]) != source_dispatch_id
                or str(source_attempt["component"]) != "cursor"
                or str(source_attempt["status"]) not in {"completed", "failed"}
                or str(source_effect["run_id"]) != run_id
                or str(source_effect["effect_kind"]) != RUN_CURSOR_TURN_EFFECT_KIND
            ):
                raise _invalid("Cursor SDK reconciliation source attempt is not authenticated")
            existing = self.store.get_cursor_sdk_reconciliation_by_source_attempt(
                conn, source_attempt_id=source_attempt_id
            )
            if existing is not None:
                _intent_from_row(existing)
                self.store.mark_attempt_ingested(conn, attempt_id=source_attempt_id, now=now)
                return TickRunReceipt(run_id=run_id, action="cursor_sdk_reconciliation_pending")
            if current.cursor.sdk != sdk or current.cursor.iteration != state.cursor.iteration:
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            inserted = self.store.insert_cursor_sdk_reconciliation(
                conn,
                recovery_key=key,
                run_id=run_id,
                source_attempt_id=source_attempt_id,
                source_dispatch_id=source_dispatch_id,
                intent_payload=payload.decode("utf-8"),
                intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
                now=now,
            )
            if not inserted:
                raise _invalid("Cursor SDK reconciliation relation conflicted")
            event = CursorSdkReconciliationRequestedEvent(
                run_id=run_id,
                recovery_key=key,
                source_attempt_id=source_attempt_id,
                source_dispatch_id=source_dispatch_id,
            )
            event_id = self.event_id()
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=self.store.next_event_sequence(conn, run_id),
                event=event,
                now=now,
            )
            updated = current.model_copy(
                update={
                    "version": version + 1,
                    "updated_at": encode_utc_instant(now),
                    "cursor": current.cursor.model_copy(
                        update={"sdk_recovery_pending": pending, "wait_until": None}
                    ),
                }
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=updated,
                now=now,
            ):
                raise _invalid("Cursor SDK reconciliation lost state update")
            self.store.mark_attempt_ingested(conn, attempt_id=source_attempt_id, now=now)
            self.store.insert_effect(
                conn,
                dispatch_id=self.dispatch_id(),
                source_event_id=event_id,
                run_id=run_id,
                effect_id=RECONCILE_CURSOR_SDK_RUN_EFFECT_ID,
                effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
                effect_payload={"recovery_key": key},
                available_at=now,
                claimed_run_version=updated.version,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="cursor_sdk_reconciliation_requested")

    def binding(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        recovery_key: str,
    ) -> dict[str, object]:
        row = self.store.get_cursor_sdk_reconciliation_by_key(conn, recovery_key=recovery_key)
        if row is None or str(row["run_id"]) != run_id or str(row["status"]) != "pending":
            raise _invalid("Cursor SDK reconciliation authority is not pending")
        intent = _intent_from_row(row)
        state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, (*SDK_ACTIVE_STATES, BlockedState, AbortedState)):
            raise _invalid("Cursor SDK reconciliation state is not eligible")
        cursor = getattr(state, "cursor", None)
        if (
            cursor is not None
            and cursor.sdk is not None
            and (
                cursor.sdk.agent_id != intent.agent_id
                or cursor.sdk.conversation_owner_run_id != intent.conversation_owner_run_id
                or cursor.sdk.conversation_key != intent.conversation_key
            )
        ):
            raise _invalid("Cursor SDK reconciliation identity changed")
        checkpoint = checkpoint_from_state(state)
        if checkpoint is None:
            source_turn = (
                WaitingForCursorFixState.model_validate(intent.source_state)
                if intent.turn_kind == "correction"
                else CursorReadyState.model_validate(intent.source_state)
            )
            if source_turn.run_id != run_id or source_turn.context != state.context:
                raise _invalid("Cursor SDK reconciliation source authority changed")
            checkpoint = source_turn.checkpoint
        identity = frozen_repository_identity(
            state.context,
            run_id=run_id,
            artifacts=self.artifacts,
            checkpoint=checkpoint,
        )
        sdk = state.context.cursor
        if not isinstance(sdk, CursorSdkBinding):
            raise _invalid("Cursor SDK reconciliation runtime changed")
        from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
            authenticate_sdk_successor_authority,
        )

        successor = authenticate_sdk_successor_authority(
            self.store,
            self.artifacts,
            conn,
            run_id,
        )
        binding: dict[str, object] = {
            "schema_version": 2,
            "runtime": "sdk-local",
            "effect_kind": RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
            "repository_root": identity.root,
            "repository_git_common_dir": identity.git_common_dir,
            "repository_git_dir": identity.git_dir,
            "repository_branch": identity.branch,
            "repository_initial_head": identity.initial_head,
            "cursor_model": sdk.model,
            "cursor_sandbox": sdk.sandbox,
            "sdk_version": sdk.sdk_version,
            "sdk_parameters": sdk.parameters,
            "sdk_credential_ref": sdk.credential_ref,
            "sdk_setting_sources": list(sdk.setting_sources),
            "sdk_tool_policy": sdk.tool_policy,
            "conversation_owner_run_id": intent.conversation_owner_run_id,
            "conversation_key": intent.conversation_key,
            "agent_id": intent.agent_id,
            "iteration": intent.iteration,
            "prompt_path": intent.prompt_path,
            "prompt_sha256": intent.prompt_sha256,
            "source_attempt_id": intent.source_attempt_id,
            "source_dispatch_id": intent.source_dispatch_id,
            "source_journal_path": intent.source_journal_path,
            "source_journal_sha256": intent.source_journal_sha256,
            "native_run_id": intent.native_run_id,
            "recovery_key": recovery_key,
            "abort_requested": row["abort_requested_at"] is not None,
            "timeout_seconds": state.context.workflow.cursor_timeout_minutes * 60,
        }
        if successor is not None:
            binding["sdk_successor_recovery_key"] = successor.recovery_key
        return binding

    def ensure_pending_effects(self) -> list[TickRunReceipt]:
        now = self.now()
        with self.store.begin_read() as conn:
            rows = list(self.store.list_cursor_sdk_reconciliations(conn, statuses=("pending",)))
        receipts: list[TickRunReceipt] = []
        for row in rows:
            run_id = str(row["run_id"])
            key = str(row["recovery_key"])
            with self.store.begin_immediate() as conn:
                current = self.store.get_cursor_sdk_reconciliation_by_key(conn, recovery_key=key)
                if current is None or str(current["status"]) != "pending":
                    continue
                active = conn.execute(
                    """
                    SELECT 1 FROM scheduler_effects
                    WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed')
                    LIMIT 1
                    """,
                    (run_id, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND),
                ).fetchone()
                attempt = self.store.get_nonterminal_attempt_for_run(conn, run_id)
                if active is not None or attempt is not None:
                    continue
                state, version, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(state, (*SDK_ACTIVE_STATES, AbortedState)):
                    continue
                event = CursorSdkReconciliationRequestedEvent(
                    run_id=run_id,
                    recovery_key=key,
                    source_attempt_id=str(current["source_attempt_id"]),
                    source_dispatch_id=str(current["source_dispatch_id"]),
                )
                event_id = self.event_id()
                self.store.append_event(
                    conn,
                    event_id=event_id,
                    run_id=run_id,
                    sequence=self.store.next_event_sequence(conn, run_id),
                    event=event,
                    now=now,
                )
                self.store.insert_effect(
                    conn,
                    dispatch_id=self.dispatch_id(),
                    source_event_id=event_id,
                    run_id=run_id,
                    effect_id=RECONCILE_CURSOR_SDK_RUN_EFFECT_ID,
                    effect_kind=RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
                    effect_payload={"recovery_key": key},
                    available_at=now,
                    claimed_run_version=version,
                    now=now,
                )
                receipts.append(
                    TickRunReceipt(run_id=run_id, action="cursor_sdk_reconciliation_rescheduled")
                )
        return receipts

    def ingest(
        self,
        run_id: str,
        *,
        attempt: sqlite3.Row,
        outcome: dict[str, object],
    ) -> TickRunReceipt:
        attempt_id = str(attempt["attempt_id"])
        if outcome.get("failure_kind") == "cursor_sdk_reconciliation_worker_lost":
            return self._resume_interrupted_reconciliation(run_id, attempt)
        record_path = str(outcome.get("reconciliation_record_path", ""))
        record_sha = str(outcome.get("reconciliation_record_sha256", ""))
        if not record_path or not record_sha:
            return self._block_failed_reconciliation(run_id, attempt_id)
        try:
            payload = self.artifacts.read_verified_bytes(
                run_id, record_path, expected_sha256=record_sha
            )
            record = CursorSdkReconciliationRecordV1.model_validate_json(payload)
        except (ProtectedArtifactError, OSError, ModelValidationError) as exc:
            raise _invalid("Cursor SDK reconciliation record failed authentication") from exc
        with self.store.begin_immediate() as conn:
            persisted_attempt = self.store.get_attempt_by_id(conn, attempt_id)
            if (
                persisted_attempt is None
                or str(persisted_attempt["run_id"]) != run_id
                or str(persisted_attempt["dispatch_id"]) != str(attempt["dispatch_id"])
                or str(persisted_attempt["component"]) != "cursor"
                or str(persisted_attempt["status"]) not in {"completed", "failed"}
            ):
                raise _invalid("Cursor SDK reconciliation attempt is not authenticated")
            row = self.store.get_cursor_sdk_reconciliation_by_source_attempt(
                conn, source_attempt_id=record.source_attempt_id
            )
            if row is None or str(row["run_id"]) != run_id:
                raise _invalid("Cursor SDK reconciliation relation is missing")
            intent = _intent_from_row(row)
            record = _authenticate_reconciliation_record(
                self.artifacts,
                run_id=run_id,
                intent=intent,
                record_path=record_path,
                record_sha256=record_sha,
            )
            if (
                record.reconciliation_attempt_id != attempt_id
                or record.reconciliation_dispatch_id != str(attempt["dispatch_id"])
            ):
                raise _invalid("Cursor SDK reconciliation record disagrees with intent")
            now = self.now()
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            status = (
                "ready"
                if record.outcome
                in {
                    "terminal_recovered",
                    "continuation_ready",
                    "abort_reconciled",
                }
                else "blocked"
            )
            if not self.store.update_cursor_sdk_reconciliation_status(
                conn,
                recovery_key=str(row["recovery_key"]),
                expected_statuses=("pending",),
                status=status,
                result_artifact_path=record_path,
                result_sha256=record_sha,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            event = CursorSdkReconciledEvent(
                run_id=run_id,
                recovery_key=str(row["recovery_key"]),
                source_attempt_id=record.source_attempt_id,
                reconciliation_attempt_id=attempt_id,
                outcome=record.outcome,
                record_path=record_path,
                record_sha256=record_sha,
            )
            event_id = self.event_id()
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=self.store.next_event_sequence(conn, run_id),
                event=event,
                now=now,
            )
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
            if isinstance(state, AbortedState):
                return TickRunReceipt(run_id=run_id, action="cursor_sdk_abort_reconciled")
            if not isinstance(state, (CursorReadyState, WaitingForCursorFixState)):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            common_cursor = state.cursor.model_copy(
                update={
                    "sdk_recovery_pending": None,
                    "sdk_reconciliation_path": record_path,
                    "sdk_reconciliation_sha256": record_sha,
                    "wait_until": None,
                    "sdk": state.cursor.sdk.model_copy(
                        update={"native_run_id": record.native_run_id}
                    )
                    if state.cursor.sdk is not None
                    else None,
                }
            )
            if record.outcome == "terminal_recovered":
                run_state = run_state_from_scheduler_context(
                    run_id=run_id,
                    context=state.context,
                    repo_root=Path(state.context.repository.root),
                    checkpoint=checkpoint_from_state(state),
                    artifacts=self.artifacts,
                )
                verify_recorded_cursor_fingerprint(
                    run_state,
                    self.artifacts.run_root(run_id),
                    iteration_number=intent.iteration,
                    recorded_path=record.cursor_output_fingerprint_path or "",
                    recorded_sha256=record.cursor_output_fingerprint_sha256 or "",
                )
                turn_event = CursorSdkTurnCompletedEvent(
                    run_id=run_id,
                    iteration=intent.iteration,
                    native_run_id=record.native_run_id or "",
                    journal_path=intent.source_journal_path,
                    journal_sha256=intent.source_journal_sha256,
                    cursor_output_fingerprint_path=record.cursor_output_fingerprint_path or "",
                    cursor_output_fingerprint_sha256=(
                        record.cursor_output_fingerprint_sha256 or ""
                    ),
                )
                self.store.append_event(
                    conn,
                    event_id=self.event_id(),
                    run_id=run_id,
                    sequence=self.store.next_event_sequence(conn, run_id),
                    event=turn_event,
                    now=now,
                )
                terminal_updated = CursorReadyState(
                    run_id=state.run_id,
                    version=version + 1,
                    submitted_at=state.submitted_at,
                    updated_at=encode_utc_instant(now),
                    idempotency_key=state.idempotency_key,
                    context=state.context,
                    checkpoint=state.checkpoint,
                    cursor=common_cursor.model_copy(
                        update={
                            "cursor_output_fingerprint_path": (
                                record.cursor_output_fingerprint_path
                            ),
                            "cursor_output_fingerprint_sha256": (
                                record.cursor_output_fingerprint_sha256
                            ),
                        }
                    ),
                    codex=state.codex,
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=terminal_updated,
                    now=now,
                ):
                    raise _invalid("Cursor SDK reconciliation lost state update")
                self.store.insert_effect(
                    conn,
                    dispatch_id=self.dispatch_id(),
                    source_event_id=event_id,
                    run_id=run_id,
                    effect_id=NORMALIZE_STAGING_EFFECT_ID,
                    effect_kind=NORMALIZE_STAGING_EFFECT_KIND,
                    effect_payload={"iteration": intent.iteration},
                    available_at=now,
                    claimed_run_version=terminal_updated.version,
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="cursor_sdk_terminal_recovered")
            if record.outcome == "continuation_ready" and intent.failure_class == "capacity":
                capacity_due = now + timedelta(
                    seconds=intent.retry_after_seconds or int(CAPACITY_FALLBACK.total_seconds())
                )
                capacity_updated = WaitingUsageLimitState(
                    run_id=state.run_id,
                    version=version + 1,
                    submitted_at=state.submitted_at,
                    updated_at=encode_utc_instant(now),
                    idempotency_key=state.idempotency_key,
                    context=state.context,
                    checkpoint=state.checkpoint,
                    cursor=common_cursor.model_copy(
                        update={
                            "wait_until": encode_utc_instant(capacity_due),
                            "original_prompt_path": intent.prompt_path,
                            "original_prompt_sha256": intent.prompt_sha256,
                        }
                    ),
                    codex=state.codex,
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=capacity_updated,
                    now=now,
                ):
                    raise _invalid("Cursor SDK capacity reconciliation lost state update")
                self.store.insert_retry_timer(
                    conn,
                    timer_id=f"tmr-{secrets.token_hex(16)}",
                    source_event_id=event_id,
                    run_id=run_id,
                    due_at=capacity_due,
                    target_effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                    expected_run_version=capacity_updated.version,
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="waiting_usage_limit")
            updated: CursorReadyState | WaitingForCursorFixState = state.model_copy(
                update={
                    "version": version + 1,
                    "updated_at": encode_utc_instant(now),
                    "cursor": common_cursor,
                }
            )
            if record.outcome == "continuation_ready" and intent.failure_class == "timeout":
                from ai_dev_loop.scheduler.application.cursor_timeout_retry import (
                    MAX_AUTOMATIC_TIMEOUT_RETRIES,
                    TIMEOUT_RETRY_DELAY,
                )
                from ai_dev_loop.scheduler.domain.events import CursorTimeoutRetryEvent

                retries = state.cursor.timeout_automatic_retries
                automatic = retries < MAX_AUTOMATIC_TIMEOUT_RETRIES
                retry_due = now + TIMEOUT_RETRY_DELAY if automatic else None
                timeout_event = CursorTimeoutRetryEvent(
                    run_id=run_id,
                    attempt_id=record.source_attempt_id,
                    iteration=intent.iteration,
                    automatic_retries=retries + int(automatic),
                    action="automatic" if automatic else "exhausted",
                    available_at=(encode_utc_instant(retry_due) if retry_due is not None else None),
                )
                timeout_event_id = self.event_id()
                self.store.append_event(
                    conn,
                    event_id=timeout_event_id,
                    run_id=run_id,
                    sequence=self.store.next_event_sequence(conn, run_id),
                    event=timeout_event,
                    now=now,
                )
                updated = updated.model_copy(
                    update={
                        "cursor": common_cursor.model_copy(
                            update={
                                "timeout_attempt_id": record.source_attempt_id,
                                "timeout_automatic_retries": timeout_event.automatic_retries,
                                "wait_until": timeout_event.available_at,
                            }
                        )
                    }
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=updated,
                    now=now,
                ):
                    raise _invalid("Cursor SDK timeout reconciliation lost state update")
                if automatic:
                    assert retry_due is not None
                    self.store.insert_effect(
                        conn,
                        dispatch_id=self.dispatch_id(),
                        source_event_id=timeout_event_id,
                        run_id=run_id,
                        effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                        effect_payload={"iteration": intent.iteration},
                        available_at=retry_due,
                        claimed_run_version=updated.version,
                        now=now,
                    )
                return TickRunReceipt(
                    run_id=run_id,
                    action=(
                        "cursor_timeout_retry_scheduled"
                        if automatic
                        else "cursor_timeout_retry_exhausted"
                    ),
                )
            else:
                reason = (
                    "cursor_failure"
                    if record.outcome == "continuation_ready"
                    else f"cursor_sdk_{record.outcome}"
                )
                blocked_event = CursorTurnBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=reason,
                    block_reason_summary=(
                        "Cursor SDK native reconciliation did not establish one safe continuation."
                    ),
                )
                reconciled_state = state.model_copy(update={"cursor": common_cursor})
                blocked = apply_cursor_turn_blocked(
                    reconciled_state,
                    blocked_event,
                    now_text=encode_utc_instant(now),
                )
                self.store.append_event(
                    conn,
                    event_id=self.event_id(),
                    run_id=run_id,
                    sequence=self.store.next_event_sequence(conn, run_id),
                    event=blocked_event,
                    now=now,
                )
                if not self.store.compare_and_swap_state(
                    conn,
                    run_id=run_id,
                    expected_version=version,
                    new_state=blocked,
                    now=now,
                ):
                    raise _invalid("Cursor SDK reconciliation block lost state update")
                return TickRunReceipt(run_id=run_id, action="blocked", detail=reason)
        raise AssertionError("Cursor SDK reconciliation did not return an outcome")

    def _resume_interrupted_reconciliation(
        self,
        run_id: str,
        attempt: sqlite3.Row,
    ) -> TickRunReceipt:
        """Retire a proved-dead worker while retaining its pending native obligation."""

        attempt_id = str(attempt["attempt_id"])
        dispatch_id = str(attempt["dispatch_id"])
        now = self.now()
        with self.store.begin_immediate() as conn:
            persisted = self.store.get_attempt_by_id(conn, attempt_id)
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            if (
                persisted is None
                or dispatch is None
                or str(persisted["run_id"]) != run_id
                or str(persisted["dispatch_id"]) != dispatch_id
                or str(persisted["component"]) != "cursor"
                or str(persisted["status"]) not in {"completed", "failed"}
                or str(dispatch["run_id"]) != run_id
                or str(dispatch["effect_kind"]) != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            ):
                raise _invalid("interrupted Cursor SDK reconciliation attempt changed owners")
            try:
                effect_payload = json.loads(str(dispatch["effect_payload"]))
            except (TypeError, ValueError) as exc:
                raise _invalid("interrupted reconciliation effect payload is invalid") from exc
            recovery_key = (
                str(effect_payload.get("recovery_key", ""))
                if isinstance(effect_payload, dict)
                else ""
            )
            row = self.store.get_cursor_sdk_reconciliation_by_key(
                conn,
                recovery_key=recovery_key,
            )
            if (
                not recovery_key
                or row is None
                or str(row["run_id"]) != run_id
                or str(row["status"]) != "pending"
            ):
                raise _invalid("interrupted reconciliation lost its pending authority")
            _intent_from_row(row)
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(
            run_id=run_id,
            action="cursor_sdk_reconciliation_interrupted",
            detail=attempt_id,
        )

    def _block_failed_reconciliation(self, run_id: str, attempt_id: str) -> TickRunReceipt:
        now = self.now()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
            if isinstance(state, AbortedState):
                return TickRunReceipt(run_id=run_id, action="cursor_sdk_abort_reconcile_failed")
            if not isinstance(state, (CursorReadyState, WaitingForCursorFixState)):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            pending = state.cursor.sdk_recovery_pending
            if pending is not None:
                self.store.update_cursor_sdk_reconciliation_status(
                    conn,
                    recovery_key=pending.recovery_key,
                    expected_statuses=("pending",),
                    status="blocked",
                    result_artifact_path=None,
                    result_sha256=None,
                    now=now,
                )
            event = CursorTurnBlockedEvent(
                run_id=run_id,
                block_reason_kind="cursor_sdk_reconciliation_failed",
                block_reason_summary="Cursor SDK native reconciliation failed without safe evidence.",
            )
            blocked = apply_cursor_turn_blocked(state, event, now_text=encode_utc_instant(now))
            self.store.append_event(
                conn,
                event_id=self.event_id(),
                run_id=run_id,
                sequence=self.store.next_event_sequence(conn, run_id),
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=blocked,
                now=now,
            ):
                raise _invalid("Cursor SDK reconciliation failure lost state update")
        return TickRunReceipt(
            run_id=run_id, action="blocked", detail="cursor_sdk_reconciliation_failed"
        )


def ensure_aborted_sdk_turn_reconciliation(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    attempt: sqlite3.Row,
    now: datetime,
    event_id_factory: Callable[[], str],
) -> bool:
    """Persist native abort work for a stopped SDK turn before releasing ownership."""

    state, _, _ = store.load_validated_snapshot(conn, run_id)
    if not isinstance(state, AbortedState) or not isinstance(
        state.context.cursor, CursorSdkBinding
    ):
        return True
    dispatch_id = str(attempt["dispatch_id"])
    dispatch = store.get_effect_by_dispatch_id(conn, dispatch_id)
    if dispatch is None or str(dispatch["effect_kind"]) != RUN_CURSOR_TURN_EFFECT_KIND:
        return True
    if str(attempt["run_id"]) != run_id or str(attempt["component"]) != "cursor":
        raise _invalid("aborted Cursor SDK attempt changed owners")
    existing = store.get_cursor_sdk_reconciliation_by_source_attempt(
        conn,
        source_attempt_id=str(attempt["attempt_id"]),
    )
    if existing is not None:
        _intent_from_row(existing)
        store.request_cursor_sdk_reconciliation_abort(conn, run_id=run_id, now=now)
        return True
    if state.cursor is None or state.cursor.sdk is None or state.codex is None:
        raise _invalid("aborted Cursor SDK turn lost its frozen checkpoint")
    if state.checkpoint is None:
        raise _invalid("aborted Cursor SDK turn lost its admitted checkpoint")
    attempt_id = str(attempt["attempt_id"])
    unit_identity = str(attempt["unit_identity"] or "")
    launch_nonce = str(attempt["launch_nonce"] or "")
    launch_intent_sha = str(attempt["launch_intent_sha256"] or "")
    if not unit_identity or not launch_nonce or not launch_intent_sha:
        raise _invalid("aborted Cursor SDK turn lacks launch authority")
    run_root = artifacts.run_root(run_id)
    evidence = verify_cursor_invocation_evidence(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=dispatch_id,
        unit_identity=unit_identity,
        launch_nonce=launch_nonce,
        launch_intent_sha256=launch_intent_sha,
        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    sdk = state.cursor.sdk
    iteration = evidence.get("iteration")
    if isinstance(iteration, bool) or not isinstance(iteration, int):
        raise _invalid("aborted Cursor SDK turn iteration is invalid")
    prompt_path = str(evidence.get("prompt_path", ""))
    prompt_sha = str(evidence.get("prompt_sha256", ""))
    if (
        iteration != state.cursor.iteration
        or str(evidence.get("conversation_owner_run_id", "")) != sdk.conversation_owner_run_id
        or str(evidence.get("conversation_key", "")) != sdk.conversation_key
        or str(evidence.get("agent_id", "")) != sdk.agent_id
        or not prompt_path
        or not prompt_sha
    ):
        raise _invalid("aborted Cursor SDK invocation disagrees with frozen identity")
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
        authenticate_sdk_successor_authority,
    )

    source_turn: CursorReadyState | WaitingForCursorFixState
    turn_kind: Literal["initial", "correction"]
    if state.prior_state_kind == "waiting_for_cursor_fix":
        turn_kind = "correction"
        source_turn = WaitingForCursorFixState(
            run_id=state.run_id,
            version=state.version,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
            idempotency_key=state.idempotency_key,
            context=state.context,
            checkpoint=state.checkpoint,
            cursor=state.cursor,
            codex=state.codex,
        )
    elif state.prior_state_kind == "cursor_ready":
        turn_kind = "initial"
        source_turn = CursorReadyState(
            run_id=state.run_id,
            version=state.version,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
            idempotency_key=state.idempotency_key,
            context=state.context,
            checkpoint=state.checkpoint,
            cursor=state.cursor,
            codex=state.codex,
        )
    else:
        raise _invalid("aborted Cursor SDK active turn has unsupported source state")
    successor = authenticate_sdk_successor_authority(store, artifacts, conn, run_id)
    expected_prompt_path = (
        state.codex.latest_correction_envelope_path
        if turn_kind == "correction"
        else state.context.plan_prompt.prompt_artifact_path
    )
    expected_prompt_sha = (
        state.codex.latest_correction_envelope_sha256
        if turn_kind == "correction"
        else state.context.plan_prompt.prompt_sha256
    )
    if successor is not None:
        if str(evidence.get("sdk_successor_recovery_key", "")) != successor.recovery_key:
            raise _invalid("aborted Cursor SDK successor ancestry changed")
        successor_turn_active = (
            successor.turn_kind == turn_kind and successor.iteration == iteration
        )
        if successor_turn_active:
            expected_prompt_path = successor.effective_prompt_path
            expected_prompt_sha = successor.effective_prompt_sha256
    if (
        not expected_prompt_path
        or not expected_prompt_sha
        or prompt_path != expected_prompt_path
        or prompt_sha != expected_prompt_sha
    ):
        raise _invalid("aborted Cursor SDK invocation changed current-turn prompt authority")
    journal_rel = str(evidence.get("journal_path") or "") or cursor_sdk_attempt_journal_rel(
        attempt_id
    )
    expected_journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
    if journal_rel != expected_journal_rel:
        raise _invalid("aborted Cursor SDK journal path changed")
    journal_path = resolve_run_relative_path(run_root, journal_rel)
    if not seal_interrupted_sdk_send_journal(journal_path):
        return False
    journal_sha = sha256_file(journal_path)
    records = authenticate_sdk_recovery_journal(
        journal_path,
        expected_sha256=journal_sha,
        expected_binding={
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "conversation_owner_run_id": sdk.conversation_owner_run_id,
            "conversation_key": sdk.conversation_key,
            "agent_id": sdk.agent_id,
            "prompt_sha256": prompt_sha,
        },
    )
    artifacts.read_verified_bytes(
        run_id,
        sdk.identity_artifact_path,
        expected_sha256=sdk.identity_artifact_sha256,
    )
    native_run_id = next(
        (
            str(record["native_run_id"])
            for record in records
            if record.get("kind") == "native_run_bound" and record.get("native_run_id")
        ),
        None,
    )
    from ai_dev_loop.scheduler.application.review_budget import (
        effective_review_ceiling_for_run,
    )

    effective_ceiling = effective_review_ceiling_for_run(
        store,
        conn,
        state,
        artifacts=artifacts,
    )
    key = sdk_reconciliation_key(run_id, attempt_id)
    intent = CursorSdkReconciliationIntentV1(
        run_id=run_id,
        source_attempt_id=attempt_id,
        source_dispatch_id=dispatch_id,
        turn_kind=turn_kind,
        iteration=iteration,
        conversation_owner_run_id=sdk.conversation_owner_run_id,
        conversation_key=sdk.conversation_key,
        agent_id=sdk.agent_id,
        prompt_path=prompt_path,
        prompt_sha256=prompt_sha,
        source_journal_path=journal_rel,
        source_journal_sha256=journal_sha,
        identity_artifact_path=sdk.identity_artifact_path,
        identity_artifact_sha256=sdk.identity_artifact_sha256,
        source_state=source_turn.model_dump(mode="json"),
        reviews_completed=state.codex.reviews_completed,
        submitted_max_review_iterations=state.context.workflow.max_review_iterations,
        effective_review_ceiling=effective_ceiling,
        native_run_id=native_run_id,
        failure_class="ambiguous",
    )
    payload = intent.canonical_bytes()
    if not store.insert_cursor_sdk_reconciliation(
        conn,
        recovery_key=key,
        run_id=run_id,
        source_attempt_id=attempt_id,
        source_dispatch_id=dispatch_id,
        intent_payload=payload.decode("utf-8"),
        intent_payload_sha256=hashlib.sha256(payload).hexdigest(),
        now=now,
    ):
        raise _invalid("aborted Cursor SDK reconciliation obligation conflicted")
    event = CursorSdkReconciliationRequestedEvent(
        run_id=run_id,
        recovery_key=key,
        source_attempt_id=attempt_id,
        source_dispatch_id=dispatch_id,
    )
    store.append_event(
        conn,
        event_id=event_id_factory(),
        run_id=run_id,
        sequence=store.next_event_sequence(conn, run_id),
        event=event,
        now=now,
    )
    store.request_cursor_sdk_reconciliation_abort(conn, run_id=run_id, now=now)
    return True


def request_sdk_reconciliation_abort(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    run_id: str,
    now: datetime,
) -> int:
    return store.request_cursor_sdk_reconciliation_abort(conn, run_id=run_id, now=now)
