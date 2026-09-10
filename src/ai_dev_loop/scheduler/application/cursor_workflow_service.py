"""Scheduler cursor workflow effect processing for Phase 17.4."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import build_usage_limit_continuation_envelope
from ai_dev_loop.runners.git import validate_staged_patch_matches_artifact
from ai_dev_loop.runners.staging import run_git_staging
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    frozen_repository_identity,
    load_authenticated_cursor_outcome,
    usage_limit_continuation_path_for_attempt,
    validate_cursor_turn_outcome_semantics,
    validate_frozen_repository_identity,
    verify_recorded_cursor_fingerprint,
    verify_recorded_usage_limit_fingerprint,
)
from ai_dev_loop.scheduler.application.cursor_retry_after import resolve_usage_limit_retry_seconds
from ai_dev_loop.scheduler.application.run_state_bridge import run_state_from_scheduler_context
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.application.scheduler_preflight import (
    DefaultSchedulerPreflightPort,
    SchedulerPreflightPort,
    SchedulerPreflightResult,
)
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_ID,
    CREATE_CHAT_EFFECT_KIND,
    CURSOR_CHAT_ARTIFACT,
    INITIAL_CURSOR_ITERATION,
    NORMALIZE_STAGING_EFFECT_ID,
    NORMALIZE_STAGING_EFFECT_KIND,
    PREFLIGHT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import (
    AwaitingCodexReviewEnteredEvent,
    CursorChatBlockedEvent,
    CursorChatCreatedEvent,
    CursorTurnBlockedEvent,
    CursorTurnCompletedEvent,
    CursorUsageLimitDetectedEvent,
    PreflightBlockedEvent,
    PreflightCompletedEvent,
    StagingBlockedEvent,
    StagingCompletedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_awaiting_codex_review_entered,
    apply_cursor_chat_blocked,
    apply_cursor_chat_created,
    apply_cursor_turn_blocked,
    apply_cursor_turn_completed,
    apply_cursor_usage_limit_detected,
    apply_preflight_blocked,
    apply_preflight_completed,
    apply_staging_blocked,
    apply_staging_completed,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedState,
    CursorReadyState,
    PreflightCompleteState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    SqliteSchedulerStore,
)
from ai_dev_loop.state import sha256_bytes, sha256_file, utc_now


def _outcome_int(outcome: dict[str, object], key: str, default: int) -> int:
    value = outcome.get(key, default)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    return default


class CursorWorkflowService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
        timer_id_factory: Callable[[], str] | None = None,
        preflight_port: SchedulerPreflightPort | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")
        self._timer_id_factory = timer_id_factory or (lambda: f"tmr-{secrets.token_hex(16)}")
        self._preflight_port = preflight_port or DefaultSchedulerPreflightPort()

    def process_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> list[TickRunReceipt]:
        receipts: list[TickRunReceipt] = []
        usage_limit = self._maybe_schedule_usage_limit_continuation(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if usage_limit is not None:
            receipts.append(usage_limit)
        local = self._process_local_effect(tick_owner_id, tick_lease_generation, run_id)
        if local is not None:
            receipts.append(local)
        ingest = self._maybe_ingest_completed_attempt(tick_owner_id, tick_lease_generation, run_id)
        if ingest is not None:
            receipts.append(ingest)
        return receipts

    def _maybe_schedule_usage_limit_continuation(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, WaitingUsageLimitState):
                return None
            if not state.cursor.wait_until:
                return None
            wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
            if now < wait_until:
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return TickRunReceipt(run_id=run_id, action="cursor_busy")
            if (
                state.cursor.usage_limit_fingerprint_path
                and state.cursor.usage_limit_fingerprint_sha256
                and state.cursor.continuation_envelope_path
            ):
                run_root = self.artifacts.run_root(run_id)
                checkpoint = checkpoint_from_state(state)
                repo_root = Path(state.context.repository.root)
                identity = frozen_repository_identity(
                    state.context,
                    run_id=run_id,
                    artifacts=self.artifacts,
                    checkpoint=checkpoint,
                )
                try:
                    validate_frozen_repository_identity(
                        repo_root,
                        identity,
                        context="before usage-limit continuation",
                    )
                    run_state = run_state_from_scheduler_context(
                        run_id=run_id,
                        context=state.context,
                        repo_root=repo_root,
                        chat_id=state.cursor.chat_id,
                        checkpoint=checkpoint,
                        artifacts=self.artifacts,
                    )
                    verify_recorded_usage_limit_fingerprint(
                        run_state,
                        run_root,
                        iteration_number=state.cursor.iteration,
                        recorded_path=state.cursor.usage_limit_fingerprint_path,
                        recorded_sha256=state.cursor.usage_limit_fingerprint_sha256,
                    )
                except (CursorEvidenceError, ValidationError, ProtectedArtifactError) as exc:
                    return self._block_waiting_usage_limit(
                        run_id,
                        reason_kind="usage_limit_continuation_guard_failed",
                        summary=str(exc)[:240],
                    )
            pending = [
                row
                for row in self.store.list_eligible_effects(conn, run_id=run_id, now=now)
                if str(row["effect_kind"]) == RUN_CURSOR_TURN_EFFECT_KIND
            ]
            if pending:
                return None

        with self.store.begin_immediate() as conn:
            commit_now = self._now_factory()
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=commit_now,
            ):
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, WaitingUsageLimitState):
                return None
            fired_timer = self.store.get_fired_retry_timer_for_run(conn, run_id)
            if fired_timer is None:
                return None
            fired_event_id = fired_timer["fired_event_id"]
            if fired_event_id is None:
                return None
            dispatch_id = self._dispatch_id_factory()
            self.store.insert_effect(
                conn,
                dispatch_id=dispatch_id,
                source_event_id=str(fired_event_id),
                run_id=run_id,
                effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                effect_payload={"iteration": state.cursor.iteration},
                available_at=commit_now,
                claimed_run_version=version,
                now=commit_now,
            )
            new_state = CursorReadyState(
                run_id=state.run_id,
                version=state.version + 1,
                submitted_at=state.submitted_at,
                updated_at=commit_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                idempotency_key=state.idempotency_key,
                context=state.context,
                checkpoint=state.checkpoint,
                cursor=state.cursor.model_copy(update={"wait_until": None}),
                codex=state.codex,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=new_state,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="usage_limit_schedule_lost")
        return TickRunReceipt(run_id=run_id, action="usage_limit_continuation_scheduled")

    def _process_local_effect(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return None
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            local = [
                row
                for row in effects
                if str(row["effect_kind"])
                in {
                    PREFLIGHT_EFFECT_KIND,
                    NORMALIZE_STAGING_EFFECT_KIND,
                }
            ]
            if not local:
                return None
            dispatch_row = local[0]
            effect_kind = str(dispatch_row["effect_kind"])

        if effect_kind == PREFLIGHT_EFFECT_KIND:
            return self._run_preflight(tick_owner_id, tick_lease_generation, run_id, dispatch_row)
        return self._run_staging(tick_owner_id, tick_lease_generation, run_id, dispatch_row)

    def _run_preflight(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        dispatch_row: object,
    ) -> TickRunReceipt:
        dispatch_id = str(dispatch_row["dispatch_id"])  # type: ignore[index]
        lease_now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=lease_now,
            ):
                return TickRunReceipt(run_id=run_id, action="preflight_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return TickRunReceipt(run_id=run_id, action="preflight_state_changed")
            context = state.context
        try:
            result = self._preflight_port.run(
                run_id=run_id,
                context=context,
                artifacts=self.artifacts,
                state=state,
            )
        except (ValidationError, ProtectedArtifactError, CursorEvidenceError) as exc:
            result = SchedulerPreflightResult(
                ok=False,
                failure_kind="preflight_validation_failed",
                failure_summary=str(exc)[:240],
            )
        commit_now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="preflight_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return TickRunReceipt(run_id=run_id, action="preflight_state_changed")
            if not result.ok:
                blocked_event = PreflightBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=result.failure_kind or "preflight_failed",
                    block_reason_summary=result.failure_summary or "preflight failed",
                )
                new_state = apply_preflight_blocked(
                    state,
                    blocked_event,
                    now_text=commit_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=blocked_event,
                    new_state=new_state,
                    version=version,
                    now=commit_now,
                )
                self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=commit_now)
                return TickRunReceipt(
                    run_id=run_id,
                    action="blocked",
                    detail=result.failure_kind,
                )
            completed_event = PreflightCompletedEvent(run_id=run_id)
            now_text = commit_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            completed_state = apply_preflight_completed(state, completed_event, now_text=now_text)
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=completed_event,
                now=commit_now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=completed_state,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="preflight_cas_lost")
            self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=commit_now)
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="preflight_stale")
            next_dispatch = self._dispatch_id_factory()
            self.store.insert_effect(
                conn,
                dispatch_id=next_dispatch,
                source_event_id=event_id,
                run_id=run_id,
                effect_id=CREATE_CHAT_EFFECT_ID,
                effect_kind=CREATE_CHAT_EFFECT_KIND,
                effect_payload={},
                available_at=commit_now,
                claimed_run_version=completed_state.version,
                now=commit_now,
            )
        return TickRunReceipt(run_id=run_id, action="preflight_completed")

    def _run_staging(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        dispatch_row: object,
    ) -> TickRunReceipt:
        dispatch_id = str(dispatch_row["dispatch_id"])  # type: ignore[index]
        now = self._now_factory()
        repo_root = Path()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="staging_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, CursorReadyState):
                return TickRunReceipt(run_id=run_id, action="staging_state_changed")
            repo_root = Path(state.context.repository.root)
            iteration = state.cursor.iteration
            checkpoint = checkpoint_from_state(state)
            run_directory = self.artifacts.run_root(run_id)
            run_state = run_state_from_scheduler_context(
                run_id=run_id,
                context=state.context,
                repo_root=repo_root,
                chat_id=state.cursor.chat_id,
                checkpoint=checkpoint,
                artifacts=self.artifacts,
            )
            identity = frozen_repository_identity(
                state.context,
                run_id=run_id,
                artifacts=self.artifacts,
                checkpoint=checkpoint,
            )
            try:
                validate_frozen_repository_identity(
                    repo_root,
                    identity,
                    context="before staging",
                )
                if (
                    state.cursor.cursor_output_fingerprint_path
                    and state.cursor.cursor_output_fingerprint_sha256
                ):
                    verify_recorded_cursor_fingerprint(
                        run_state,
                        run_directory,
                        iteration_number=iteration,
                        recorded_path=state.cursor.cursor_output_fingerprint_path,
                        recorded_sha256=state.cursor.cursor_output_fingerprint_sha256,
                    )
            except (CursorEvidenceError, Exception) as exc:
                event = StagingBlockedEvent(
                    run_id=run_id,
                    block_reason_kind="pre_staging_validation_failed",
                    block_reason_summary=str(exc)[:240],
                )
                new_state = apply_staging_blocked(
                    state,
                    event,
                    now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=event,
                    new_state=new_state,
                    version=version,
                    now=now,
                )
                self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=now)
                return TickRunReceipt(
                    run_id=run_id,
                    action="blocked",
                    detail="pre_staging_validation_failed",
                )
            try:
                staging = run_git_staging(
                    run_state,
                    run_directory,
                    iteration=f"{iteration:02d}",
                    iteration_number=iteration,
                    cursor_started_at=now,
                    cursor_exit_code=0,
                    prompt_path=(
                        state.cursor.continuation_envelope_path
                        or state.context.plan_prompt.prompt_artifact_path
                    ),
                )
            except Exception as exc:
                event = StagingBlockedEvent(
                    run_id=run_id,
                    block_reason_kind="staging_failed",
                    block_reason_summary=str(exc)[:240],
                )
                new_state = apply_staging_blocked(
                    state,
                    event,
                    now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=event,
                    new_state=new_state,
                    version=version,
                    now=now,
                )
                self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=now)
                return TickRunReceipt(run_id=run_id, action="blocked", detail="staging_failed")

            patch_path = staging.artifacts.patch_path
            patch_sha = sha256_file(run_directory / patch_path)
            try:
                validate_staged_patch_matches_artifact(repo_root, run_directory / patch_path)
            except Exception as exc:
                event = StagingBlockedEvent(
                    run_id=run_id,
                    block_reason_kind="staged_patch_drift",
                    block_reason_summary=str(exc)[:240],
                )
                new_state = apply_staging_blocked(
                    state,
                    event,
                    now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=event,
                    new_state=new_state,
                    version=version,
                    now=now,
                )
                self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=now)
                return TickRunReceipt(run_id=run_id, action="blocked", detail="staged_patch_drift")

            staging_event = StagingCompletedEvent(
                run_id=run_id,
                iteration=iteration,
                staged_patch_path=patch_path,
                staged_patch_sha256=patch_sha,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            review_state = apply_staging_completed(state, staging_event, now_text=now_text)
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=staging_event,
                now=now,
            )
            entered = AwaitingCodexReviewEnteredEvent(run_id=run_id)
            apply_awaiting_codex_review_entered(review_state, entered)
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=self._event_id_factory(),
                run_id=run_id,
                sequence=sequence,
                event=entered,
                now=now,
            )
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="staging_stale")
            commit_now = self._now_factory()
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=review_state,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="staging_cas_lost")
            self.store.complete_effect_by_id(conn, dispatch_id=dispatch_id, now=commit_now)
        return TickRunReceipt(run_id=run_id, action="awaiting_codex_review")

    def _maybe_ingest_completed_attempt(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return None
            attempt = self.store.get_latest_completed_cursor_attempt(conn, run_id)
            if attempt is None:
                return None
            if int(attempt["ingested"]) == 1:
                return None
            dispatch = self.store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
            if dispatch is None:
                return None
            effect_kind = str(dispatch["effect_kind"])

        if effect_kind == CREATE_CHAT_EFFECT_KIND:
            return self._ingest_create_chat(run_id, attempt)
        if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
            return self._ingest_cursor_turn(run_id, attempt)
        return None

    def _authenticated_outcome(self, run_id: str, attempt: object) -> dict[str, object]:
        attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
        unit_identity = str(attempt["unit_identity"])  # type: ignore[index]
        result_rel = str(attempt["result_artifact_path"])  # type: ignore[index]
        stdout_rel = str(attempt["stdout_artifact_path"])  # type: ignore[index]
        stderr_rel = str(attempt["stderr_artifact_path"])  # type: ignore[index]
        exit_code = attempt["exit_code"]  # type: ignore[index]
        observed_exit = int(exit_code) if exit_code is not None else None
        dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
        envelope_sha = attempt["completion_envelope_sha256"]  # type: ignore[index]
        with self.store.begin_read() as conn:
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
        expected_effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else None
        return load_authenticated_cursor_outcome(
            self.artifacts.run_root(run_id),
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=observed_exit,
            expected_envelope_sha256=str(envelope_sha) if envelope_sha else None,
            expected_dispatch_id=dispatch_id,
            expected_effect_kind=expected_effect_kind,
        )

    def _ingest_create_chat(self, run_id: str, attempt: object) -> TickRunReceipt:
        attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
        dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
        now = self._now_factory()
        run_root = self.artifacts.run_root(run_id)
        try:
            outcome = self._authenticated_outcome(run_id, attempt)
        except (CursorEvidenceError, ValueError, TypeError, KeyError, OSError):
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_chat_blocked",
                reason_kind="outcome_evidence_invalid",
                summary="authenticated cursor chat outcome failed validation",
            )
        failure_kind = str(outcome.get("failure_kind", ""))
        if failure_kind == "cursor_chat_create_failed":
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_chat_blocked",
                reason_kind="cursor_chat_create_failed",
                summary="Cursor chat creation failed before a chat ID was received",
            )
        if failure_kind:
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_chat_blocked",
                reason_kind="cursor_chat_attempt_failed",
                summary="Cursor chat attempt failed before a chat ID was received",
            )
        chat_id = str(outcome.get("chat_id", ""))
        if not chat_id:
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_chat_blocked",
                reason_kind="invalid_chat_id",
                summary="Cursor chat creation returned an invalid chat ID",
            )
        existing_path = run_root / CURSOR_CHAT_ARTIFACT
        if existing_path.is_file():
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return self._block_from_ingest(
                    run_id,
                    dispatch_id=dispatch_id,
                    attempt_id=attempt_id,
                    kind="cursor_chat_blocked",
                    reason_kind="chat_artifact_conflict",
                    summary="existing chat artifact is unreadable",
                )
            if str(existing.get("chat_id", "")) != chat_id:
                return self._block_from_ingest(
                    run_id,
                    dispatch_id=dispatch_id,
                    attempt_id=attempt_id,
                    kind="cursor_chat_blocked",
                    reason_kind="chat_artifact_conflict",
                    summary="existing chat artifact conflicts with authenticated outcome",
                )
            stored_path = CURSOR_CHAT_ARTIFACT
            stored_sha = sha256_file(existing_path)
        else:
            chat_payload = {
                "chat_id": chat_id,
                "created_at": now.astimezone(UTC).isoformat(),
                "command": "",
                "attempt_id": attempt_id,
            }
            stored = self.artifacts.write_text(
                run_id,
                CURSOR_CHAT_ARTIFACT,
                json.dumps(chat_payload, indent=2, sort_keys=True) + "\n",
                max_bytes=16_384,
            )
            stored_path = stored.relative_path
            stored_sha = stored.sha256
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if isinstance(state, CursorReadyState):
                if state.cursor.chat_id != chat_id:
                    return self._block_from_ingest(
                        run_id,
                        dispatch_id=dispatch_id,
                        attempt_id=attempt_id,
                        kind="cursor_chat_blocked",
                        reason_kind="chat_artifact_conflict",
                        summary="run already has a different Cursor chat ID",
                    )
                self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
                return TickRunReceipt(run_id=run_id, action="cursor_chat_already_ready")
            if not isinstance(state, PreflightCompleteState):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            event = CursorChatCreatedEvent(
                run_id=run_id,
                chat_id=chat_id,
                chat_artifact_path=stored_path,
                chat_artifact_sha256=stored_sha,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_cursor_chat_created(state, event, now_text=now_text)
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=new_state,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_cas_lost")
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
            next_dispatch = self._dispatch_id_factory()
            self.store.insert_effect(
                conn,
                dispatch_id=next_dispatch,
                source_event_id=event_id,
                run_id=run_id,
                effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                effect_payload={"iteration": INITIAL_CURSOR_ITERATION},
                available_at=now,
                claimed_run_version=new_state.version,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="cursor_chat_created")

    def _ingest_cursor_turn(self, run_id: str, attempt: object) -> TickRunReceipt:
        attempt_id = str(attempt["attempt_id"])  # type: ignore[index]
        dispatch_id = str(attempt["dispatch_id"])  # type: ignore[index]
        now = self._now_factory()
        run_root = self.artifacts.run_root(run_id)
        try:
            outcome = self._authenticated_outcome(run_id, attempt)
        except (CursorEvidenceError, ValueError, TypeError, KeyError, OSError):
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="outcome_evidence_invalid",
                summary="authenticated cursor outcome failed validation",
            )
        iteration = _outcome_int(outcome, "iteration", INITIAL_CURSOR_ITERATION)
        timed_out = bool(outcome.get("timed_out"))
        failure_code = outcome.get("failure_code")
        returncode = _outcome_int(outcome, "returncode", 0)
        structured_errors = outcome.get("structured_errors", [])
        if not isinstance(structured_errors, list):
            structured_errors = []

        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(
                state, (CursorReadyState, WaitingUsageLimitState, WaitingForCursorFixState)
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            repo_root = Path(state.context.repository.root)
            checkpoint = checkpoint_from_state(state)

        if timed_out:
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="cursor_timeout",
                summary="Cursor execution timed out",
                state_kind=type(state).__name__,
            )

        if returncode != 0:
            if failure_code == "cursor_usage_limit":
                return self._handle_usage_limit(
                    run_id,
                    attempt_id=attempt_id,
                    iteration=iteration,
                    structured_errors=structured_errors,
                    after_status_path=str(outcome.get("after_status_path", "")),
                    usage_limit_fingerprint_path=str(
                        outcome.get("usage_limit_fingerprint_path", "")
                    ),
                    usage_limit_fingerprint_sha256=str(
                        outcome.get("usage_limit_fingerprint_sha256", "")
                    ),
                )
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="cursor_failure",
                summary="Cursor turn failed with an unclassified error",
            )

        try:
            validate_cursor_turn_outcome_semantics(outcome)
            run_state = run_state_from_scheduler_context(
                run_id=run_id,
                context=state.context,
                repo_root=repo_root,
                chat_id=state.cursor.chat_id,
                checkpoint=checkpoint,
                artifacts=self.artifacts,
            )
            fingerprint_path = str(outcome.get("cursor_output_fingerprint_path", ""))
            fingerprint_sha = str(outcome.get("cursor_output_fingerprint_sha256", ""))
            if not fingerprint_path or not fingerprint_sha:
                raise CursorEvidenceError("authenticated outcome missing post-cursor fingerprint")
            verify_recorded_cursor_fingerprint(
                run_state,
                run_root,
                iteration_number=iteration,
                recorded_path=fingerprint_path,
                recorded_sha256=fingerprint_sha,
            )
        except (CursorEvidenceError, Exception) as exc:
            return self._block_from_ingest(
                run_id,
                dispatch_id=dispatch_id,
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="post_cursor_validation_failed",
                summary=str(exc)[:240],
            )

        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(
                state, (CursorReadyState, WaitingUsageLimitState, WaitingForCursorFixState)
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            event = CursorTurnCompletedEvent(
                run_id=run_id,
                iteration=iteration,
                cursor_output_fingerprint_path=fingerprint_path,
                cursor_output_fingerprint_sha256=fingerprint_sha,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_cursor_turn_completed(state, event, now_text=now_text)
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=new_state,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_cas_lost")
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
            next_dispatch = self._dispatch_id_factory()
            self.store.insert_effect(
                conn,
                dispatch_id=next_dispatch,
                source_event_id=event_id,
                run_id=run_id,
                effect_id=NORMALIZE_STAGING_EFFECT_ID,
                effect_kind=NORMALIZE_STAGING_EFFECT_KIND,
                effect_payload={"iteration": iteration},
                available_at=now,
                claimed_run_version=new_state.version,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="cursor_turn_completed")

    def _handle_usage_limit(
        self,
        run_id: str,
        *,
        attempt_id: str,
        iteration: int,
        structured_errors: list[object],
        after_status_path: str,
        usage_limit_fingerprint_path: str,
        usage_limit_fingerprint_sha256: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        run_root = self.artifacts.run_root(run_id)
        with self.store.begin_read() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(
                state, (CursorReadyState, WaitingUsageLimitState, WaitingForCursorFixState)
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            if isinstance(state, WaitingUsageLimitState):
                return self._block_from_ingest(
                    run_id,
                    dispatch_id="",
                    attempt_id=attempt_id,
                    kind="cursor_turn_blocked",
                    reason_kind="usage_limit_state_invalid",
                    summary="usage-limit detection requires cursor_ready or waiting_for_cursor_fix",
                )
        records = tuple(item for item in structured_errors if isinstance(item, dict))
        checkpoint = checkpoint_from_state(state)
        retry_seconds = resolve_usage_limit_retry_seconds(records)
        wait_until = now + timedelta(seconds=retry_seconds)
        wait_until_text = wait_until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if isinstance(state, WaitingForCursorFixState):
            source_prompt_path = state.codex.latest_fix_prompt_path
            source_prompt_sha = state.codex.latest_fix_prompt_sha256
            if not source_prompt_path or not source_prompt_sha:
                return self._block_from_ingest(
                    run_id,
                    dispatch_id="",
                    attempt_id=attempt_id,
                    kind="cursor_turn_blocked",
                    reason_kind="fix_prompt_missing",
                    summary="correction usage-limit requires persisted fix prompt",
                )
            try:
                from ai_dev_loop.iterations import build_correction_execution_envelope

                fix_prompt = self.artifacts.read_verified_bytes(
                    run_id,
                    source_prompt_path,
                    expected_sha256=source_prompt_sha,
                )
            except (ProtectedArtifactError, ValidationError, CursorEvidenceError, OSError) as exc:
                return self._block_from_ingest(
                    run_id,
                    dispatch_id="",
                    attempt_id=attempt_id,
                    kind="cursor_turn_blocked",
                    reason_kind="prompt_binding_drift",
                    summary=str(exc)[:240],
                )
            envelope = build_correction_execution_envelope(fix_prompt.decode("utf-8"))
            envelope_rel = state.codex.latest_correction_envelope_path or (
                usage_limit_continuation_path_for_attempt(iteration, attempt_id)
            )
        else:
            original_prompt_path = (
                state.cursor.original_prompt_path or state.context.plan_prompt.prompt_artifact_path
            )
            original_prompt_sha = (
                state.cursor.original_prompt_sha256 or state.context.plan_prompt.prompt_sha256
            )
            try:
                original_prompt = self.artifacts.read_verified_bytes(
                    run_id,
                    original_prompt_path,
                    expected_sha256=original_prompt_sha,
                )
            except (ProtectedArtifactError, ValidationError, CursorEvidenceError, OSError) as exc:
                return self._block_from_ingest(
                    run_id,
                    dispatch_id="",
                    attempt_id=attempt_id,
                    kind="cursor_turn_blocked",
                    reason_kind="prompt_binding_drift",
                    summary=str(exc)[:240],
                )
            envelope = build_usage_limit_continuation_envelope(original_prompt.decode("utf-8"))
            envelope_rel = usage_limit_continuation_path_for_attempt(iteration, attempt_id)
        try:
            stored_path, stored_sha = self._store_or_verify_continuation_envelope(
                run_id,
                envelope_rel,
                envelope,
            )
        except CursorEvidenceError as exc:
            return self._block_from_ingest(
                run_id,
                dispatch_id="",
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="continuation_envelope_conflict",
                summary=str(exc)[:240],
            )
        if isinstance(state, WaitingForCursorFixState):
            if fix_prompt.decode("utf-8") not in envelope:
                return self._block_from_ingest(
                    run_id,
                    dispatch_id="",
                    attempt_id=attempt_id,
                    kind="cursor_turn_blocked",
                    reason_kind="continuation_envelope_invalid",
                    summary="correction envelope must embed exact fix prompt bytes",
                )
        elif original_prompt.decode("utf-8") not in envelope:
            return self._block_from_ingest(
                run_id,
                dispatch_id="",
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="continuation_envelope_invalid",
                summary="continuation envelope must embed exact original prompt bytes",
            )

        if not usage_limit_fingerprint_path or not usage_limit_fingerprint_sha256:
            return self._block_from_ingest(
                run_id,
                dispatch_id="",
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="usage_limit_fingerprint_missing",
                summary="authenticated outcome missing usage-limit fingerprint",
            )
        repo_root = Path(state.context.repository.root)
        identity = frozen_repository_identity(
            state.context,
            run_id=run_id,
            artifacts=self.artifacts,
            checkpoint=checkpoint,
        )
        try:
            validate_frozen_repository_identity(
                repo_root,
                identity,
                context="during usage-limit ingestion",
            )
            run_state = run_state_from_scheduler_context(
                run_id=run_id,
                context=state.context,
                repo_root=repo_root,
                chat_id=state.cursor.chat_id,
                checkpoint=checkpoint,
                artifacts=self.artifacts,
            )
            verify_recorded_usage_limit_fingerprint(
                run_state,
                run_root,
                iteration_number=iteration,
                recorded_path=usage_limit_fingerprint_path,
                recorded_sha256=usage_limit_fingerprint_sha256,
            )
        except (CursorEvidenceError, ValidationError, ProtectedArtifactError) as exc:
            return self._block_from_ingest(
                run_id,
                dispatch_id="",
                attempt_id=attempt_id,
                kind="cursor_turn_blocked",
                reason_kind="usage_limit_ingest_guard_failed",
                summary=str(exc)[:240],
            )

        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, (CursorReadyState, WaitingForCursorFixState)):
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            event = CursorUsageLimitDetectedEvent(
                run_id=run_id,
                iteration=iteration,
                wait_until=wait_until_text,
                usage_limit_fingerprint_path=usage_limit_fingerprint_path,
                usage_limit_fingerprint_sha256=usage_limit_fingerprint_sha256,
                continuation_envelope_path=stored_path,
                continuation_envelope_sha256=stored_sha,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_cursor_usage_limit_detected(state, event, now_text=now_text)
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=version,
                new_state=new_state,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="ingest_cas_lost")
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
            timer_id = self._timer_id_factory()
            self.store.insert_retry_timer(
                conn,
                timer_id=timer_id,
                source_event_id=event_id,
                run_id=run_id,
                due_at=wait_until,
                target_effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                expected_run_version=new_state.version,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="waiting_usage_limit")

    def _block_from_ingest(
        self,
        run_id: str,
        *,
        dispatch_id: str,
        attempt_id: str,
        kind: str,
        reason_kind: str,
        summary: str,
        state_kind: str | None = None,
    ) -> TickRunReceipt:
        del dispatch_id, state_kind
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if isinstance(state, PreflightCompleteState) and kind == "cursor_chat_blocked":
                blocked_event = CursorChatBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=reason_kind,
                    block_reason_summary=summary,
                )
                new_state = apply_cursor_chat_blocked(
                    state,
                    blocked_event,
                    now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=blocked_event,
                    new_state=new_state,
                    version=version,
                    now=now,
                )
            elif isinstance(
                state, (CursorReadyState, WaitingUsageLimitState, WaitingForCursorFixState)
            ):
                turn_blocked_event = CursorTurnBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=reason_kind,
                    block_reason_summary=summary,
                )
                new_state = apply_cursor_turn_blocked(
                    state,
                    turn_blocked_event,
                    now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=turn_blocked_event,
                    new_state=new_state,
                    version=version,
                    now=now,
                )
            else:
                return TickRunReceipt(run_id=run_id, action="ingest_block_state_changed")
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(run_id=run_id, action="blocked", detail=reason_kind)

    def block_launch_guard_failure(
        self,
        run_id: str,
        *,
        attempt_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        reason_kind: str,
        summary: str,
    ) -> TickRunReceipt:
        commit_now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="launch_block_stale")
            attempt = self.store.get_attempt_by_id(conn, attempt_id)
            if attempt is None or str(attempt["run_id"]) != run_id:
                return TickRunReceipt(run_id=run_id, action="launch_block_stale")
            dispatch_id = str(attempt["dispatch_id"])
            capacity_tick_generation = int(attempt["capacity_tick_generation"])
            if not self.store.settle_prelaunch_guard_failure(
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                capacity_claim_owner_id=tick_owner_id,
                capacity_tick_generation=capacity_tick_generation,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="launch_block_settlement_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if isinstance(state, PreflightCompleteState):
                blocked_event = CursorChatBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=reason_kind,
                    block_reason_summary=summary,
                )
                new_state = apply_cursor_chat_blocked(
                    state,
                    blocked_event,
                    now_text=commit_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=blocked_event,
                    new_state=new_state,
                    version=version,
                    now=commit_now,
                )
            elif isinstance(
                state, (CursorReadyState, WaitingUsageLimitState, WaitingForCursorFixState)
            ):
                turn_blocked_event = CursorTurnBlockedEvent(
                    run_id=run_id,
                    block_reason_kind=reason_kind,
                    block_reason_summary=summary,
                )
                new_state = apply_cursor_turn_blocked(
                    state,
                    turn_blocked_event,
                    now_text=commit_now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                )
                self._append_event_and_block(
                    conn,
                    run_id=run_id,
                    event=turn_blocked_event,
                    new_state=new_state,
                    version=version,
                    now=commit_now,
                )
            else:
                return TickRunReceipt(run_id=run_id, action="launch_block_state_changed")
        return TickRunReceipt(run_id=run_id, action="blocked", detail=reason_kind)

    def _block_waiting_usage_limit(
        self,
        run_id: str,
        *,
        reason_kind: str,
        summary: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, WaitingUsageLimitState):
                return TickRunReceipt(run_id=run_id, action="block_state_changed")
            event = CursorTurnBlockedEvent(
                run_id=run_id,
                block_reason_kind=reason_kind,
                block_reason_summary=summary,
            )
            new_state = apply_cursor_turn_blocked(
                state,
                event,
                now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            self._append_event_and_block(
                conn,
                run_id=run_id,
                event=event,
                new_state=new_state,
                version=version,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="blocked", detail=reason_kind)

    def _store_or_verify_continuation_envelope(
        self,
        run_id: str,
        envelope_rel: str,
        envelope_text: str,
    ) -> tuple[str, str]:
        run_root = self.artifacts.run_root(run_id)
        path = run_root / envelope_rel
        expected_sha = sha256_bytes(envelope_text.encode("utf-8"))
        if path.is_file():
            existing_sha = sha256_file(path)
            if existing_sha != expected_sha:
                raise CursorEvidenceError(
                    "continuation envelope conflicts with earlier attempt artifact"
                )
            return envelope_rel, existing_sha
        stored = self.artifacts.write_text(
            run_id,
            envelope_rel,
            envelope_text,
            max_bytes=2_000_000,
        )
        return stored.relative_path, stored.sha256

    def _append_event_and_block(
        self,
        conn: object,
        *,
        run_id: str,
        event: object,
        new_state: object,
        version: int,
        now: datetime,
    ) -> None:
        event_id = self._event_id_factory()
        sequence = self.store.next_event_sequence(conn, run_id)  # type: ignore[arg-type]
        self.store.append_event(
            conn,  # type: ignore[arg-type]
            event_id=event_id,
            run_id=run_id,
            sequence=sequence,
            event=event,  # type: ignore[arg-type]
            now=now,
        )
        self.store.compare_and_swap_state(
            conn,  # type: ignore[arg-type]
            run_id=run_id,
            expected_version=version,
            new_state=new_state,  # type: ignore[arg-type]
            now=now,
        )
        worktree_key = new_state.context.repository.worktree_key  # type: ignore[attr-defined]
        self.store.release_reservation(
            conn,  # type: ignore[arg-type]
            worktree_key=worktree_key,
            now=now,
        )
