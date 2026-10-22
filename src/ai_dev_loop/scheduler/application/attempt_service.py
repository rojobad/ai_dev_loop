"""Scheduler attempt launch, observe, and reconcile service."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError as ModelValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.abort_reconcile import (
    append_attempt_result_stale_event,
    attempt_stop_action,
    finalize_aborted_run_cleanup,
    finalize_resolved_abort_attempt,
    unit_observation_stopped,
)
from ai_dev_loop.scheduler.application.attempt_backend import (
    AgentProcessBackend,
    LaunchRequest,
    TerminationClass,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    sha256_file,
    validate_completion_evidence,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    unit_identity_from_attempt_id,
    worktree_lock_path,
)
from ai_dev_loop.scheduler.application.attempt_paths import prepare_attempt_output_paths
from ai_dev_loop.scheduler.application.codex_argv import scheduler_codex_review_sandbox
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    verify_codex_implementer_invocation_evidence,
    verify_codex_invocation_evidence,
    verify_pre_execution_codex_guards,
    verify_pre_execution_implementer_guards,
)
from ai_dev_loop.scheduler.application.codex_workflow_service import CodexWorkflowService
from ai_dev_loop.scheduler.application.contracts import (
    AbortProcessAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    frozen_repository_identity,
    invocation_evidence_sha256,
    verify_cursor_invocation_evidence,
    verify_pre_execution_cursor_guards,
)
from ai_dev_loop.scheduler.application.cursor_runtime_policy import (
    context_uses_cursor_cli,
    unsupported_sequence_cursor_cli_ordinals,
)
from ai_dev_loop.scheduler.application.cursor_sdk_journal import (
    JOURNAL_SCHEMA_VERSION,
    authenticate_sdk_journal,
    authenticate_sdk_recovery_journal,
    seal_interrupted_sdk_send_journal,
)
from ai_dev_loop.scheduler.application.cursor_sdk_progress import (
    seal_interrupted_sdk_wait_progress,
)
from ai_dev_loop.scheduler.application.cursor_workflow_service import CursorWorkflowService
from ai_dev_loop.scheduler.application.review_budget import effective_review_ceiling_for_run
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    IMPLEMENTER_ATTEMPT_EFFECT_KINDS,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_sdk_attempt_journal_rel,
    cursor_sdk_wait_progress_rel,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.domain.effects import (
    CODEX_ATTEMPT_EFFECT_KINDS,
    CURSOR_ATTEMPT_EFFECT_KINDS,
    FAKE_AGENT_SELF_TEST_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import (
    AttemptCompletedEvent,
    AttemptLaunchRequestedEvent,
    AttemptUncertainEvent,
)
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    AdmittedState,
    AwaitingCodexReviewState,
    BlockedState,
    CursorReadyState,
    CursorSdkBinding,
    PreflightCompleteState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
    resolved_implementer_provider,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    resolve_run_relative_path,
    scheduler_state_dir,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_ACTIVE,
    ATTEMPT_STATUS_CANCELLED,
    ATTEMPT_STATUS_COMPLETED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_LAUNCHING,
    ATTEMPT_STATUS_UNCERTAIN,
    SqliteSchedulerStore,
)
from ai_dev_loop.state import atomic_write_bytes, atomic_write_text

_CURSOR_ATTEMPT_STATES = (
    PreflightCompleteState,
    CursorReadyState,
    WaitingUsageLimitState,
    WaitingForCursorFixState,
)
_CODEX_ATTEMPT_STATES = (AwaitingCodexReviewState,)
_CODEX_IMPLEMENTER_ATTEMPT_STATES = (PreflightCompleteState, WaitingForCursorFixState)
_ATTEMPT_RECONCILE_STATES = (AdmittedState, *_CURSOR_ATTEMPT_STATES, *_CODEX_ATTEMPT_STATES)


def _effect_is_no_progress_reobserve(dispatch: sqlite3.Row | None) -> bool:
    if dispatch is None:
        return False
    if str(dispatch["effect_kind"]) != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
        return False
    try:
        payload = json.loads(str(dispatch["effect_payload"]))
    except (TypeError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("no_progress_reobserve") is True


def _cursor_reobserve_attempt(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    attempt_id: str,
) -> bool:
    """The owned fresh read is the only cursor attempt a blocked run may run."""

    attempt_row = store.get_attempt_by_id(conn, attempt_id)
    if attempt_row is None:
        return False
    dispatch = store.get_effect_by_dispatch_id(conn, str(attempt_row["dispatch_id"]))
    return _effect_is_no_progress_reobserve(dispatch)


def _blocked_no_progress_reobserve(state: object, dispatch: sqlite3.Row | None) -> bool:
    return isinstance(state, BlockedState) and _effect_is_no_progress_reobserve(dispatch)


def _initial_recovery_prompt_applies(
    state: PreflightCompleteState | CursorReadyState | WaitingUsageLimitState,
    recovery: object,
    *,
    iteration: int,
) -> bool:
    """Bind the recovery prompt only for its authorized initial retry."""

    from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
        InitialRecoveryAuthority,
    )
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
        CursorInitialRecoveryRecordV1,
    )

    if not isinstance(recovery, (CursorInitialRecoveryRecordV1, InitialRecoveryAuthority)):
        raise ValidationError("initial recovery launch record is not an initial recovery record")
    if recovery.iteration != iteration:
        return False
    continuation = state.cursor.continuation_envelope_path
    if continuation and continuation != recovery.effective_prompt_path:
        return False
    if recovery.chat_id != state.cursor.chat_id:
        raise ValidationError("initial recovery record disagrees with the cursor checkpoint")
    return True


def _correction_recovery_prompt_applies(
    state: PreflightCompleteState | CursorReadyState | WaitingUsageLimitState,
    recovery: object,
    *,
    iteration: int,
) -> bool:
    """Bind the recovered correction envelope only for its authorized retry."""

    from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import CursorRecoveryRecordV2

    if not isinstance(recovery, CursorRecoveryRecordV2):
        raise ValidationError("correction recovery launch record is not the v2 record")
    if recovery.turn_kind != "correction" or recovery.iteration != iteration:
        return False
    if recovery.chat_id != state.cursor.chat_id:
        raise ValidationError("correction recovery record disagrees with the cursor checkpoint")
    if recovery.raw_fix is None or recovery.staged_patch is None:
        raise ValidationError("correction recovery record is missing fix or staged evidence")
    return True


class AttemptService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        backend: AgentProcessBackend,
        *,
        now_factory: Callable[[], datetime],
        event_id_factory: Callable[[], str],
        claim_id_factory: Callable[[], str],
        attempt_id_factory: Callable[[], str],
        fence_id_factory: Callable[[], str],
        launch_nonce_factory: Callable[[], str],
        cursor_workflow: CursorWorkflowService | None = None,
        codex_workflow: CodexWorkflowService | None = None,
        before_effect_claim: Callable[[], None] | None = None,
        build_agent_argv: Callable[[str, str, str, Path], list[str]] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.backend = backend
        self._now_factory = now_factory
        self._event_id_factory = event_id_factory
        self._claim_id_factory = claim_id_factory
        self._attempt_id_factory = attempt_id_factory
        self._fence_id_factory = fence_id_factory
        self._launch_nonce_factory = launch_nonce_factory
        self._cursor_workflow = cursor_workflow
        self._codex_workflow = codex_workflow
        self._before_effect_claim = before_effect_claim
        self._build_agent_argv = build_agent_argv

    def process_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        cleanup = self._maybe_finalize_aborted_resources(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if cleanup is not None:
            return cleanup

        # Dispatch fences intentionally reject recovered sequence leaves once their
        # run or sequence has been aborted.  An already-aborted run can still own a
        # cancelled process, though, and that process must remain observable until
        # its exact unit is authenticated as stopped.  Give only terminal abort
        # reconciliation priority here; every live launch/relaunch path remains
        # behind the recovery authority checks below.
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if isinstance(state, AbortedState):
            reconcile = self._reconcile_existing_attempt(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
            if reconcile is not None:
                return reconcile

        from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
            cursor_initial_recovery_blocks_dispatch,
        )

        with self.store.begin_read() as conn:
            if cursor_initial_recovery_blocks_dispatch(self.store, conn, run_id):
                return TickRunReceipt(
                    run_id=run_id,
                    action="cursor_initial_recovery_publication_pending",
                )
            from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                implementer_recovery_blocks_dispatch,
            )

            if implementer_recovery_blocks_dispatch(self.store, conn, run_id):
                return TickRunReceipt(
                    run_id=run_id,
                    action="codex_implementer_recovery_publication_pending",
                )
        reconcile = self._reconcile_existing_attempt(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if reconcile is not None:
            return reconcile
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if context_uses_cursor_cli(state.context):
                return TickRunReceipt(run_id=run_id, action="cursor_cli_runtime_retired")
            if state.context.sequence is not None:
                sequence = self.store.load_validated_sequence_state(
                    conn, state.context.sequence.sequence_id
                )
                if unsupported_sequence_cursor_cli_ordinals(sequence.definition):
                    return TickRunReceipt(
                        run_id=run_id, action="sequence_cursor_cli_runtime_retired"
                    )
        return (
            self._maybe_launch_codex_implementer_attempt(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
            or self._maybe_launch_codex_attempt(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
            or self._maybe_launch_cursor_attempt(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
            or self._maybe_launch_fake_agent_attempt(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
        )

    def _maybe_launch_codex_implementer_attempt(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, _CODEX_IMPLEMENTER_ATTEMPT_STATES):
                return None
            if resolved_implementer_provider(state.context) != "codex":
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return TickRunReceipt(run_id=run_id, action="attempt_busy")
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            implementer_effects = [
                row
                for row in effects
                if str(row["effect_kind"]) in IMPLEMENTER_ATTEMPT_EFFECT_KINDS
            ]
            if not implementer_effects:
                return None
            dispatch_row = implementer_effects[0]
            dispatch_id = str(dispatch_row["dispatch_id"])
            effect_kind = str(dispatch_row["effect_kind"])
            effect_payload_text = str(dispatch_row["effect_payload"])
            scheduled_version = int(dispatch_row["claimed_run_version"])
            implementer_state = state

        claim_id = self._claim_id_factory()
        attempt_id = self._attempt_id_factory()
        unit_identity = unit_identity_from_attempt_id(attempt_id)
        launch_nonce = self._launch_nonce_factory()
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        result_rel = attempt_result_rel(attempt_id)
        try:
            parsed_effect_payload = json.loads(effect_payload_text)
            if not isinstance(parsed_effect_payload, dict):
                raise ValueError("Codex implementer effect payload must be an object")
            evidence = self._codex_implementer_binding(
                implementer_state,
                effect_kind=effect_kind,
                run_id=run_id,
                effect_payload=parsed_effect_payload,
            )
        except SchedulerEngineError as exc:
            if exc.kind is not SchedulerEngineErrorKind.VALIDATION:
                raise
            if self._codex_workflow is not None:
                return self._codex_workflow.block_implementer_launch_guard_failure(
                    run_id,
                    attempt_id="",
                    reason_kind="codex_implementer_launch_guard_failed",
                    summary=str(exc)[:240],
                )
            raise
        except (ValidationError, ValueError, OSError) as exc:
            if self._codex_workflow is not None:
                return self._codex_workflow.block_implementer_launch_guard_failure(
                    run_id,
                    attempt_id="",
                    reason_kind="codex_implementer_launch_guard_failed",
                    summary=str(exc)[:240],
                )
            raise
        evidence.update(
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "dispatch_id": dispatch_id,
            }
        )
        launch_intent = json.dumps(
            {
                "attempt_id": attempt_id,
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "unit_identity": unit_identity,
                "launch_nonce": launch_nonce,
                "effect_kind": effect_kind,
                "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        launch_intent_sha256 = payload_sha256(launch_intent)

        if self._before_effect_claim is not None:
            self._before_effect_claim()
        with self.store.begin_immediate() as conn:
            commit_now = self._now_factory()
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, _CODEX_IMPLEMENTER_ATTEMPT_STATES):
                return None
            if version < scheduled_version:
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                implementer_recovery_blocks_dispatch,
            )

            if implementer_recovery_blocks_dispatch(self.store, conn, run_id):
                return TickRunReceipt(
                    run_id=run_id,
                    action="codex_implementer_recovery_dispatch_fenced",
                )
            if not self.store.try_acquire_capacity(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=commit_now,
            ):
                return TickRunReceipt(run_id=run_id, action="capacity_busy")
            if not self.store.claim_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=version,
                now=commit_now,
            ):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=commit_now,
                )
                return TickRunReceipt(run_id=run_id, action="attempt_claim_lost")
            self.store.insert_launch_requested_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                launch_nonce=launch_nonce,
                unit_identity=unit_identity,
                launch_intent_sha256=launch_intent_sha256,
                capacity_claim_id=claim_id,
                capacity_tick_generation=tick_lease_generation,
                stdout_artifact_path=stdout_rel,
                stderr_artifact_path=stderr_rel,
                result_artifact_path=result_rel,
                component="codex_implementer",
                iteration=state.cursor.iteration,
                now=commit_now,
            )
            event = AttemptLaunchRequestedEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
            )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=commit_now,
            )

        self.artifacts.write_text(
            run_id,
            invocation_evidence_rel(attempt_id),
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
            max_bytes=1_000_000,
        )
        return self._launch_recorded_attempt(
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            run_id=run_id,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            claim_id=claim_id,
            adopt_only=False,
            codex_implementer_attempt=True,
        )

    def _maybe_launch_codex_attempt(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return TickRunReceipt(run_id=run_id, action="attempt_busy")
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            codex_effects = [
                row for row in effects if str(row["effect_kind"]) in CODEX_ATTEMPT_EFFECT_KINDS
            ]
            if not codex_effects:
                return None
            dispatch_row = codex_effects[0]
            dispatch_id = str(dispatch_row["dispatch_id"])
            effect_kind = str(dispatch_row["effect_kind"])
            scheduled_version = int(dispatch_row["claimed_run_version"])
            codex_state = state

        claim_id = self._claim_id_factory()
        attempt_id = self._attempt_id_factory()
        unit_identity = unit_identity_from_attempt_id(attempt_id)
        launch_nonce = self._launch_nonce_factory()
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        result_rel = attempt_result_rel(attempt_id)
        evidence = self._codex_binding(codex_state, effect_kind=effect_kind, run_id=run_id)
        evidence.update(
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "dispatch_id": dispatch_id,
            }
        )
        launch_intent = json.dumps(
            {
                "attempt_id": attempt_id,
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "unit_identity": unit_identity,
                "launch_nonce": launch_nonce,
                "effect_kind": effect_kind,
                "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        launch_intent_sha256 = payload_sha256(launch_intent)

        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AwaitingCodexReviewState):
                return None
            if version < scheduled_version:
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            if not self.store.try_acquire_capacity(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="capacity_busy")
            if not self.store.claim_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=version,
                now=self._now_factory(),
            ):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=self._now_factory(),
                )
                return TickRunReceipt(run_id=run_id, action="attempt_claim_lost")
            self.store.insert_launch_requested_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                launch_nonce=launch_nonce,
                unit_identity=unit_identity,
                launch_intent_sha256=launch_intent_sha256,
                capacity_claim_id=claim_id,
                capacity_tick_generation=tick_lease_generation,
                stdout_artifact_path=stdout_rel,
                stderr_artifact_path=stderr_rel,
                result_artifact_path=result_rel,
                component="codex",
                iteration=codex_state.cursor.iteration,
                now=self._now_factory(),
            )
            event = AttemptLaunchRequestedEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
            )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=self._now_factory(),
            )

        self.artifacts.write_text(
            run_id,
            invocation_evidence_rel(attempt_id),
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
            max_bytes=1_000_000,
        )

        return self._launch_recorded_attempt(
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            run_id=run_id,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            claim_id=claim_id,
            adopt_only=False,
            cursor_attempt=False,
            codex_attempt=True,
        )

    def _initial_recovery_evidence_receipt(
        self,
        run_id: str,
        exc: BaseException,
    ) -> TickRunReceipt | None:
        with self.store.begin_read() as conn:
            row = self.store.get_cursor_initial_recovery_by_successor(
                conn,
                successor_run_id=run_id,
            )
        if row is None:
            return None
        return TickRunReceipt(
            run_id=run_id,
            action="cursor_initial_recovery_evidence_invalid",
            detail=type(exc).__name__,
        )

    def _reauthenticate_initial_recovery_launch(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> TickRunReceipt | None:
        from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
            authenticated_initial_recovery_launch,
        )

        try:
            from ai_dev_loop.scheduler.application.cursor_correction_recovery import (
                authenticated_correction_launch,
            )

            if (
                authenticated_initial_recovery_launch(self.store, self.artifacts, conn, run_id)
                is None
                and authenticated_correction_launch(self.store, self.artifacts, conn, run_id)
                is None
            ):
                from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
                    authenticated_sdk_successor_launch,
                )

                authenticated_sdk_successor_launch(self.store, self.artifacts, conn, run_id)
        except (SchedulerEngineError, ModelValidationError) as exc:
            if (
                self.store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=run_id)
                is None
            ):
                raise
            return TickRunReceipt(
                run_id=run_id,
                action="cursor_initial_recovery_evidence_invalid",
                detail=type(exc).__name__,
            )
        return None

    def _maybe_launch_cursor_attempt(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(
                state,
                (
                    PreflightCompleteState,
                    CursorReadyState,
                    WaitingUsageLimitState,
                    WaitingForCursorFixState,
                    AbortedState,
                    BlockedState,
                ),
            ):
                return None
            if resolved_implementer_provider(state.context) == "codex":
                return None
            if context_uses_cursor_cli(state.context):
                return TickRunReceipt(run_id=run_id, action="cursor_cli_runtime_retired")
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return TickRunReceipt(run_id=run_id, action="attempt_busy")
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            cursor_effects = [
                row for row in effects if str(row["effect_kind"]) in CURSOR_ATTEMPT_EFFECT_KINDS
            ]
            if not cursor_effects:
                return None
            dispatch_row = cursor_effects[0]
            dispatch_id = str(dispatch_row["dispatch_id"])
            effect_kind = str(dispatch_row["effect_kind"])
            effect_payload: dict[str, object] = {}
            if effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
                parsed_payload = json.loads(str(dispatch_row["effect_payload"]))
                if isinstance(parsed_payload, dict):
                    effect_payload = parsed_payload
            reobserve = effect_payload.get("no_progress_reobserve") is True
            if isinstance(state, BlockedState) and not reobserve:
                return None
            if (
                isinstance(state, AbortedState)
                and effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            ):
                return None
            scheduled_version = int(dispatch_row["claimed_run_version"])
            cursor_state = state

        claim_id = self._claim_id_factory()
        attempt_id = self._attempt_id_factory()
        unit_identity = unit_identity_from_attempt_id(attempt_id)
        launch_nonce = self._launch_nonce_factory()
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        result_rel = attempt_result_rel(attempt_id)
        cursor_checkpoint = getattr(cursor_state, "cursor", None)
        if (
            cursor_checkpoint is not None
            and cursor_checkpoint.timeout_attempt_id
            and effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        ):
            if isinstance(cursor_state.context.cursor, CursorSdkBinding):
                if isinstance(cursor_state, AbortedState):
                    raise ValidationError("aborted SDK run cannot dispatch a timeout retry")
                if isinstance(cursor_state, BlockedState):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
                evidence = self._cursor_binding(
                    cursor_state,
                    effect_kind=effect_kind,
                    run_id=run_id,
                )
                evidence["timeout_retry_of"] = cursor_checkpoint.timeout_attempt_id
                evidence["sdk_reconciliation_path"] = cursor_checkpoint.sdk_reconciliation_path
                evidence["sdk_reconciliation_sha256"] = cursor_checkpoint.sdk_reconciliation_sha256
            else:
                from ai_dev_loop.scheduler.application.cursor_timeout_retry import timeout_binding

                with self.store.begin_read() as conn:
                    evidence = timeout_binding(
                        self.store,
                        self.artifacts,
                        conn,
                        run_id,
                        cursor_checkpoint.timeout_attempt_id,
                    )
                evidence["timeout_retry_of"] = cursor_checkpoint.timeout_attempt_id
        elif effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
            payload = json.loads(str(dispatch_row["effect_payload"]))
            if not isinstance(payload, dict) or not isinstance(payload.get("recovery_key"), str):
                raise ValidationError("Cursor SDK reconciliation effect payload is invalid")
            from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
                CursorSdkReconciliationService,
            )

            with self.store.begin_read() as conn:
                evidence = CursorSdkReconciliationService(
                    self.store,
                    self.artifacts,
                    now_factory=self._now_factory,
                ).binding(
                    conn,
                    run_id=run_id,
                    recovery_key=str(payload["recovery_key"]),
                    allow_ready=reobserve,
                )
            if reobserve:
                evidence["no_progress_reobserve"] = True
        else:
            try:
                if isinstance(cursor_state, AbortedState):
                    raise ValidationError("aborted SDK run can only reconcile native state")
                if isinstance(cursor_state, BlockedState):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
                evidence = self._cursor_binding(
                    cursor_state, effect_kind=effect_kind, run_id=run_id
                )
            except (SchedulerEngineError, ModelValidationError) as exc:
                blocked = self._initial_recovery_evidence_receipt(run_id, exc)
                if blocked is not None:
                    return blocked
                raise
        evidence.update(
            {
                "attempt_id": attempt_id,
                "run_id": run_id,
                "dispatch_id": dispatch_id,
            }
        )
        launch_intent = json.dumps(
            {
                "attempt_id": attempt_id,
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "unit_identity": unit_identity,
                "launch_nonce": launch_nonce,
                "effect_kind": effect_kind,
                "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        launch_intent_sha256 = payload_sha256(launch_intent)

        if self._before_effect_claim is not None:
            self._before_effect_claim()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(
                state,
                (
                    PreflightCompleteState,
                    CursorReadyState,
                    WaitingUsageLimitState,
                    WaitingForCursorFixState,
                    AbortedState,
                    BlockedState,
                ),
            ):
                return None
            if isinstance(state, BlockedState) and not reobserve:
                return None
            if (
                isinstance(state, AbortedState)
                and effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            ):
                return None
            if version < scheduled_version:
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            if effect_kind != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND:
                blocked = self._reauthenticate_initial_recovery_launch(conn, run_id)
                if blocked is not None:
                    return blocked
            from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
                cursor_initial_recovery_blocks_dispatch,
            )

            if cursor_initial_recovery_blocks_dispatch(self.store, conn, run_id):
                return TickRunReceipt(
                    run_id=run_id,
                    action="cursor_initial_recovery_publication_pending",
                )
            if not self.store.try_acquire_capacity(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="capacity_busy")
            if not self.store.claim_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=version,
                now=self._now_factory(),
            ):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=self._now_factory(),
                )
                return TickRunReceipt(run_id=run_id, action="attempt_claim_lost")
            self.store.insert_launch_requested_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                launch_nonce=launch_nonce,
                unit_identity=unit_identity,
                launch_intent_sha256=launch_intent_sha256,
                capacity_claim_id=claim_id,
                capacity_tick_generation=tick_lease_generation,
                stdout_artifact_path=stdout_rel,
                stderr_artifact_path=stderr_rel,
                result_artifact_path=result_rel,
                iteration=cursor_checkpoint.iteration if cursor_checkpoint is not None else 1,
                now=self._now_factory(),
            )
            event = AttemptLaunchRequestedEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
            )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=self._now_factory(),
            )

        self.artifacts.write_text(
            run_id,
            invocation_evidence_rel(attempt_id),
            json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n",
            max_bytes=1_000_000,
        )

        return self._launch_recorded_attempt(
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            run_id=run_id,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            claim_id=claim_id,
            adopt_only=False,
            cursor_attempt=True,
        )

    def _codex_binding(
        self,
        state: AwaitingCodexReviewState,
        *,
        effect_kind: str,
        run_id: str,
    ) -> dict[str, object]:
        context = state.context
        checkpoint = checkpoint_from_state(state)
        identity = frozen_repository_identity(
            context,
            run_id=run_id,
            artifacts=self.artifacts,
            checkpoint=checkpoint,
        )
        codex = context.codex
        with self.store.begin_read() as conn:
            max_reviews = effective_review_ceiling_for_run(
                self.store,
                conn,
                state,
                artifacts=self.artifacts,
            )
        binding: dict[str, object] = {
            "effect_kind": effect_kind,
            "repository_root": identity.root,
            "repository_git_common_dir": identity.git_common_dir,
            "repository_git_dir": identity.git_dir,
            "repository_branch": identity.branch,
            "repository_initial_head": identity.initial_head,
            "review_model": codex.review_model,
            "review_reasoning_effort": codex.review_reasoning_effort,
            "codex_command": codex.command,
            "codex_sandbox": scheduler_codex_review_sandbox(codex.sandbox),
            "review_skill": codex.review_skill,
            "implementer_provider": resolved_implementer_provider(context),
            "plan_repository_path": context.plan_prompt.plan_repository_path,
            "plan_artifact_path": context.plan_prompt.plan_artifact_path,
            "plan_sha256": context.plan_prompt.plan_sha256,
            "prompt_source_repository_path": context.plan_prompt.prompt_source_repository_path,
            "prompt_artifact_path": context.plan_prompt.prompt_artifact_path,
            "prompt_sha256": context.plan_prompt.prompt_sha256,
            "review_iteration": state.cursor.iteration,
            "cursor_chat_id": state.cursor.chat_id,
            "max_review_iterations": max_reviews,
            "codex_timeout_minutes": context.workflow.codex_timeout_minutes,
        }
        if state.codex.reviewer_session_id:
            binding["reviewer_session_id"] = state.codex.reviewer_session_id
            binding_path = state.codex.binding_artifact_path
            if binding_path:
                binding_abs = self.artifacts.run_root(run_id) / binding_path
                if binding_abs.is_file():
                    try:
                        binding_payload = json.loads(binding_abs.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        binding_payload = None
                    if isinstance(binding_payload, dict):
                        events_sha = str(binding_payload.get("bootstrap_events_sha256", "")).strip()
                        bound_at = str(binding_payload.get("bootstrap_bound_at", "")).strip()
                        if events_sha:
                            binding["bootstrap_events_sha256"] = events_sha
                        if bound_at:
                            binding["bootstrap_bound_at"] = bound_at
        if effect_kind == RESUME_CODEX_REVIEW_EFFECT_KIND and not state.codex.reviewer_session_id:
            raise ValidationError("resume codex review requires bound reviewer identity")
        if effect_kind == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND and state.codex.reviewer_session_id:
            raise ValidationError("bootstrap codex review cannot run with bound reviewer identity")
        generation = state.codex.review_retry_generation
        scheduled = state.codex.review_retry_scheduled_generation
        if generation > 0 and scheduled == generation:
            binding["operational_review_retry"] = True
        return binding

    def _codex_implementer_binding(
        self,
        state: PreflightCompleteState | WaitingForCursorFixState,
        *,
        effect_kind: str,
        run_id: str,
        effect_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        context = state.context
        implementer = context.implementer
        if implementer is None or implementer.provider != "codex":
            raise ValidationError("Codex implementer dispatch requires frozen Codex settings")
        checkpoint = checkpoint_from_state(state)
        identity = frozen_repository_identity(
            context,
            run_id=run_id,
            artifacts=self.artifacts,
            checkpoint=checkpoint,
        )
        codex_checkpoint = state.codex
        bound_session = (
            codex_checkpoint.implementer_session_id if codex_checkpoint is not None else None
        )
        continuation = (
            codex_checkpoint.capacity_continuation if codex_checkpoint is not None else None
        )
        timeout_continuation = (
            codex_checkpoint.implementer_timeout_continuation
            if codex_checkpoint is not None
            else None
        )
        recovery = None
        with self.store.begin_read() as conn:
            current, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(current, _CODEX_IMPLEMENTER_ATTEMPT_STATES):
                raise ValidationError("Codex implementer state changed before dispatch")
            from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                authenticate_implementer_recovery_dispatch,
            )

            recovery = authenticate_implementer_recovery_dispatch(
                self.store,
                self.artifacts,
                conn,
                current,
                effect_payload=effect_payload or {},
            )
        if timeout_continuation is not None:
            from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
                CodexImplementerTimeoutRetryService,
            )

            with self.store.begin_read() as conn:
                current, _, _ = self.store.load_validated_snapshot(conn, run_id)
                if not isinstance(current, _CODEX_IMPLEMENTER_ATTEMPT_STATES):
                    raise ValidationError("Codex timeout run state changed before dispatch")
                CodexImplementerTimeoutRetryService(
                    self.store,
                    self.artifacts,
                    now_factory=self._now_factory,
                ).authenticate_dispatch(
                    conn,
                    current,
                    effect_payload=effect_payload or {},
                )
        turn = "correction" if isinstance(state, WaitingForCursorFixState) else "initial"
        prompt_path: str | None
        prompt_sha: str | None
        if recovery is not None:
            if effect_kind != RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
                raise ValidationError("implementer recovery must resume its exact session")
            prompt_path = recovery.exact_prompt.relative_path
            prompt_sha = recovery.exact_prompt.sha256
        elif continuation is not None and continuation.role == "implementer":
            if continuation.turn != turn:
                raise ValidationError("Codex implementer continuation turn mismatch")
            prompt_path = continuation.prompt_artifact_path
            prompt_sha = continuation.prompt_sha256
        elif timeout_continuation is not None:
            if timeout_continuation.turn != turn:
                raise ValidationError("Codex implementer timeout continuation turn mismatch")
            if effect_kind != RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
                raise ValidationError("Codex timeout continuation must resume its exact session")
            if effect_payload is not None and (
                effect_payload.get("timeout_retry_generation")
                != timeout_continuation.retry_generation
                or effect_payload.get("timeout_retry_of") != timeout_continuation.source_attempt_id
                or effect_payload.get("prompt_path") != timeout_continuation.prompt_artifact_path
            ):
                raise ValidationError("Codex timeout effect does not match durable continuation")
            prompt_path = timeout_continuation.prompt_artifact_path
            prompt_sha = timeout_continuation.prompt_sha256
        elif turn == "correction":
            assert isinstance(state, WaitingForCursorFixState)
            prompt_path = state.codex.latest_correction_envelope_path
            prompt_sha = state.codex.latest_correction_envelope_sha256
        else:
            prompt_path = context.plan_prompt.prompt_artifact_path
            prompt_sha = context.plan_prompt.prompt_sha256
        if not prompt_path or not prompt_sha:
            raise ValidationError("Codex implementer dispatch requires an exact prompt binding")
        if effect_kind == RESUME_CODEX_IMPLEMENTER_EFFECT_KIND and not bound_session:
            raise ValidationError("Codex implementer resume requires its bound session")
        if effect_kind == BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND and bound_session:
            raise ValidationError("Codex implementer bootstrap cannot replace its bound session")
        if turn == "correction" and effect_kind != RESUME_CODEX_IMPLEMENTER_EFFECT_KIND:
            raise ValidationError("Codex correction must resume the exact implementer session")
        reviewer_session = codex_checkpoint.reviewer_session_id if codex_checkpoint else None
        if bound_session and reviewer_session and bound_session == reviewer_session:
            raise ValidationError("implementer and reviewer identities must stay distinct")
        binding: dict[str, object] = {
            "effect_kind": effect_kind,
            "role": "implementer",
            "turn": turn,
            "iteration": state.cursor.iteration,
            "repository_root": identity.root,
            "repository_git_common_dir": identity.git_common_dir,
            "repository_git_dir": identity.git_dir,
            "repository_branch": identity.branch,
            "repository_initial_head": identity.initial_head,
            "codex_command": implementer.command,
            "model": implementer.model,
            "reasoning_effort": implementer.reasoning_effort,
            "codex_sandbox": implementer.sandbox,
            "approval_policy": implementer.approval_policy,
            "timeout_seconds": implementer.timeout_minutes * 60,
            "prompt_path": prompt_path,
            "prompt_sha256": prompt_sha,
            "session_id": bound_session or "",
        }
        if timeout_continuation is not None:
            binding.update(
                {
                    "timeout_retry_of": timeout_continuation.source_attempt_id,
                    "timeout_retry_generation": timeout_continuation.retry_generation,
                    "timeout_retry_intent_path": timeout_continuation.intent_path,
                    "timeout_retry_intent_sha256": timeout_continuation.intent_sha256,
                }
            )
        if recovery is not None:
            binding.update(
                {
                    "implementer_recovery_key": recovery.recovery_key,
                    "implementer_recovery_source": recovery.source_run_id,
                    "implementer_recovery_attempt": recovery.failed_attempt_id,
                }
            )
        return binding

    def _prior_authorized_prompt_evidence(
        self,
        state: PreflightCompleteState
        | CursorReadyState
        | WaitingUsageLimitState
        | WaitingForCursorFixState,
        *,
        run_id: str,
    ) -> dict[str, str] | None:
        """Pin the last authorized prompt journal from ledger authority.

        ``latest_journal_path`` and ``latest_journal_sha256`` are the durable
        reference. Attempt, dispatch and prompt bindings come from that
        attempt's authenticated invocation, not from a fresh hash of the file.
        """

        sdk_runtime = state.cursor.sdk
        if sdk_runtime is None:
            return None
        relative = sdk_runtime.latest_journal_path
        expected_sha = sdk_runtime.latest_journal_sha256
        marker = "cursor/sdk-attempts/"
        suffix = "/dispatch-journal.jsonl"
        if not relative.startswith(marker) or not relative.endswith(suffix):
            raise ValidationError("Cursor SDK journal path is not an attempt journal")
        source_attempt_id = relative[len(marker) : -len(suffix)]
        if cursor_sdk_attempt_journal_rel(source_attempt_id) != relative:
            raise ValidationError("Cursor SDK journal path is not an attempt journal")
        run_root = self.artifacts.run_root(run_id)
        journal_path = resolve_run_relative_path(run_root, relative)
        if journal_path.is_symlink() or not journal_path.is_file():
            return None
        if sha256_file(journal_path) != expected_sha:
            raise ValidationError(
                "Cursor SDK prior journal digest does not match durable authority"
            )
        with self.store.begin_read() as conn:
            attempt = self.store.get_attempt_by_id(conn, source_attempt_id)
            if (
                attempt is None
                or str(attempt["run_id"]) != run_id
                or attempt["launch_intent_sha256"] is None
                or attempt["launch_nonce"] is None
                or attempt["unit_identity"] is None
            ):
                return None
            dispatch_id = str(attempt["dispatch_id"])
            effect = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            if effect is None or str(effect["run_id"]) != run_id:
                raise ValidationError("Cursor SDK prior journal attempt has no dispatch")
            effect_kind = str(effect["effect_kind"])
            launch_nonce = str(attempt["launch_nonce"])
            launch_intent_sha = str(attempt["launch_intent_sha256"])
            unit_identity = str(attempt["unit_identity"])
        if effect_kind != RUN_CURSOR_TURN_EFFECT_KIND:
            return None
        try:
            invocation = verify_cursor_invocation_evidence(
                run_root,
                attempt_id=source_attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha,
                effect_kind=effect_kind,
            )
        except CursorEvidenceError as exc:
            raise ValidationError(
                "Cursor SDK prior journal invocation is not authenticated"
            ) from exc
        prompt_sha = str(invocation.get("prompt_sha256", ""))
        if not prompt_sha or invocation.get("agent_id") != sdk_runtime.agent_id:
            raise ValidationError("Cursor SDK prior journal agent does not match the bound agent")
        expected_binding = {
            "attempt_id": source_attempt_id,
            "dispatch_id": dispatch_id,
            "conversation_owner_run_id": sdk_runtime.conversation_owner_run_id,
            "conversation_key": sdk_runtime.conversation_key,
            "agent_id": sdk_runtime.agent_id,
            "prompt_sha256": prompt_sha,
        }
        try:
            records = authenticate_sdk_recovery_journal(
                journal_path,
                expected_sha256=expected_sha,
                expected_binding=expected_binding,
            )
        except ValueError:
            try:
                records = authenticate_sdk_journal(
                    journal_path,
                    expected_sha256=expected_sha,
                    operation="send",
                    expected_binding=expected_binding,
                )
            except ValueError:
                return None
        if not any(record.get("kind") == "send_intent" for record in records):
            return None
        kinds = {str(record.get("kind", "")) for record in records}
        version = records[0].get("schema_version")
        if version != JOURNAL_SCHEMA_VERSION and not kinds.intersection(
            {"readiness_clear", "creation_blocker_cancel_confirmed"}
        ):
            return None
        return {
            "prior_authorized_journal_path": relative,
            "prior_authorized_journal_sha256": expected_sha,
            "prior_authorized_attempt_id": source_attempt_id,
            "prior_authorized_dispatch_id": dispatch_id,
            "prior_authorized_prompt_sha256": prompt_sha,
        }

    def _cursor_binding(
        self,
        state: PreflightCompleteState
        | CursorReadyState
        | WaitingUsageLimitState
        | WaitingForCursorFixState,
        *,
        effect_kind: str,
        run_id: str,
    ) -> dict[str, object]:
        context = state.context
        checkpoint = checkpoint_from_state(state)
        identity = frozen_repository_identity(
            context,
            run_id=run_id,
            artifacts=self.artifacts,
            checkpoint=checkpoint,
        )
        if isinstance(context.cursor, CursorSdkBinding):
            from ai_dev_loop.paths import cursor_conversation_key

            sdk = context.cursor
            binding: dict[str, object] = {
                "schema_version": 3 if sdk.schema_version == 2 else 2,
                "runtime": "sdk-local",
                "effect_kind": effect_kind,
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
                "conversation_owner_run_id": (
                    state.cursor.sdk.conversation_owner_run_id
                    if state.cursor.sdk is not None
                    else run_id
                ),
                "conversation_key": (
                    state.cursor.sdk.conversation_key
                    if state.cursor.sdk is not None
                    else cursor_conversation_key(run_id)
                ),
                "timeout_seconds": context.workflow.cursor_timeout_minutes * 60,
            }
            if sdk.schema_version == 2:
                binding.update(
                    {
                        "store_backend": sdk.store_backend,
                        "native_store_layout_version": sdk.native_store_layout_version,
                    }
                )
            if effect_kind == CREATE_CHAT_EFFECT_KIND:
                return binding
            if state.cursor.sdk is None:
                raise ValidationError("Cursor SDK turn requires a persisted agent identity")
            if state.cursor.sdk.conversation_key != cursor_conversation_key(
                state.cursor.sdk.conversation_owner_run_id
            ):
                raise ValidationError("Cursor SDK identity owner disagrees with conversation key")
            if (
                state.cursor.sdk.schema_version != sdk.schema_version
                or state.cursor.sdk.store_backend != sdk.store_backend
                or state.cursor.sdk.native_store_layout_version != sdk.native_store_layout_version
            ):
                raise ValidationError("Cursor SDK runtime storage binding changed")
            if isinstance(state, WaitingForCursorFixState):
                prompt_path = state.codex.latest_correction_envelope_path
                prompt_sha = state.codex.latest_correction_envelope_sha256
                if not prompt_path or not prompt_sha:
                    raise ValidationError("SDK correction requires persisted execution envelope")
                binding.update(
                    {
                        "fix_prompt_path": state.codex.latest_fix_prompt_path,
                        "fix_prompt_sha256": state.codex.latest_fix_prompt_sha256,
                        "staged_patch_path": state.cursor.staged_patch_path,
                        "staged_patch_sha256": state.cursor.staged_patch_sha256,
                    }
                )
            elif isinstance(state, WaitingUsageLimitState):
                prompt_path = state.cursor.original_prompt_path
                prompt_sha = state.cursor.original_prompt_sha256
                if not prompt_path or not prompt_sha:
                    raise ValidationError("SDK usage continuation requires persisted prompt")
            else:
                prompt_path = context.plan_prompt.prompt_artifact_path
                prompt_sha = context.plan_prompt.prompt_sha256
            binding.update(
                {
                    "iteration": state.cursor.iteration,
                    "agent_id": state.cursor.sdk.agent_id,
                    "prompt_path": prompt_path,
                    "prompt_sha256": prompt_sha,
                    "identity_artifact_path": state.cursor.sdk.identity_artifact_path,
                    "identity_artifact_sha256": state.cursor.sdk.identity_artifact_sha256,
                }
            )
            from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
                authenticate_sdk_successor_authority,
                sdk_successor_prompt_applies,
            )

            dispatch_authority: dict[str, object] | None = None
            with self.store.begin_read() as conn:
                sdk_authority = authenticate_sdk_successor_authority(
                    self.store, self.artifacts, conn, run_id
                )
                review_source = self.store.get_review_recovery_source_for_successor(
                    conn, successor_run_id=run_id
                )
                if review_source is not None:
                    source_state, _, _ = self.store.load_validated_snapshot(
                        conn, str(review_source["source_run_id"])
                    )
                    if source_state.context != state.context:
                        raise ValidationError("SDK review successor changed frozen inputs")
                if sdk_authority is not None:
                    from ai_dev_loop.scheduler.application.cursor_sdk_no_progress import (
                        no_progress_dispatch_authority,
                    )

                    dispatch_authority = no_progress_dispatch_authority(
                        self.store,
                        self.artifacts,
                        conn,
                        source_run_id=sdk_authority.source_run_id,
                        failed_attempt_id=sdk_authority.failed_attempt_id,
                    )
            if review_source is not None:
                binding["sdk_review_recovery_source_run_id"] = str(review_source["source_run_id"])
            if sdk_authority is not None:
                binding["sdk_successor_recovery_key"] = sdk_authority.recovery_key
            anchor = self._prior_authorized_prompt_evidence(state, run_id=run_id)
            if anchor is not None:
                binding.update(anchor)
            if sdk_authority is not None and sdk_successor_prompt_applies(state, sdk_authority):
                if dispatch_authority is not None:
                    binding.update(dispatch_authority)
                # The SDK successor record authenticates the complete correction
                # ancestry.  The resumed public SDK send consumes the copy-once
                # effective prompt rather than reinterpreting that prompt as a
                # legacy correction-recovery envelope.
                binding.pop("fix_prompt_path", None)
                binding.pop("fix_prompt_sha256", None)
                binding.pop("staged_patch_path", None)
                binding.pop("staged_patch_sha256", None)
                binding.update(
                    {
                        "prompt_path": sdk_authority.effective_prompt_path,
                        "prompt_sha256": sdk_authority.effective_prompt_sha256,
                        "iteration": sdk_authority.iteration,
                    }
                )
            return binding
        legacy_binding: dict[str, object] = {
            "effect_kind": effect_kind,
            "repository_root": identity.root,
            "repository_git_common_dir": identity.git_common_dir,
            "repository_git_dir": identity.git_dir,
            "repository_branch": identity.branch,
            "repository_initial_head": identity.initial_head,
            "cursor_command": context.cursor.command,
            "cursor_model": context.cursor.model,
            "cursor_output_format": context.cursor.output_format,
            "cursor_force": context.cursor.force,
            "cursor_trust_workspace": context.cursor.trust_workspace,
            "cursor_sandbox": context.cursor.sandbox,
            "timeout_seconds": context.workflow.cursor_timeout_minutes * 60,
        }
        if effect_kind == CREATE_CHAT_EFFECT_KIND:
            return legacy_binding
        assert effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        iteration = state.cursor.iteration
        if isinstance(state, WaitingForCursorFixState):
            envelope_path = state.codex.latest_correction_envelope_path
            envelope_sha = state.codex.latest_correction_envelope_sha256
            if not envelope_path or not envelope_sha:
                raise ValidationError(
                    "correction cursor turn requires persisted execution envelope binding"
                )
            legacy_binding.update(
                {
                    "iteration": iteration,
                    "chat_id": state.cursor.chat_id,
                    "prompt_path": envelope_path,
                    "prompt_sha256": envelope_sha,
                    "fix_prompt_path": state.codex.latest_fix_prompt_path,
                    "fix_prompt_sha256": state.codex.latest_fix_prompt_sha256,
                    "staged_patch_path": state.cursor.staged_patch_path,
                    "staged_patch_sha256": state.cursor.staged_patch_sha256,
                }
            )
        else:
            prompt_path = (
                state.cursor.continuation_envelope_path
                if state.cursor.continuation_envelope_path
                else context.plan_prompt.prompt_artifact_path
            )
            legacy_binding.update(
                {
                    "iteration": iteration,
                    "chat_id": state.cursor.chat_id,
                    "prompt_path": prompt_path,
                    "prompt_sha256": (
                        state.cursor.continuation_envelope_sha256
                        if state.cursor.continuation_envelope_path
                        else context.plan_prompt.prompt_sha256
                    ),
                }
            )
            from ai_dev_loop.scheduler.application.cursor_initial_recovery import (
                authenticated_initial_recovery_launch,
            )

            with self.store.begin_read() as conn:
                recovery = authenticated_initial_recovery_launch(
                    self.store,
                    self.artifacts,
                    conn,
                    run_id,
                )
            if recovery is not None and _initial_recovery_prompt_applies(
                state,
                recovery,
                iteration=iteration,
            ):
                legacy_binding["prompt_path"] = recovery.effective_prompt_path
                legacy_binding["prompt_sha256"] = recovery.effective_prompt_sha256
                legacy_binding["chat_id"] = recovery.chat_id
                legacy_binding["iteration"] = recovery.iteration
            else:
                from ai_dev_loop.scheduler.application.cursor_correction_recovery import (
                    authenticated_correction_launch,
                )

                with self.store.begin_read() as conn:
                    correction = authenticated_correction_launch(
                        self.store,
                        self.artifacts,
                        conn,
                        run_id,
                    )
                if correction is not None and _correction_recovery_prompt_applies(
                    state,
                    correction,
                    iteration=iteration,
                ):
                    assert correction.raw_fix is not None
                    assert correction.staged_patch is not None
                    prompt_path = correction.effective_prompt_path
                    prompt_sha = correction.effective_prompt_sha256
                    continuation = state.cursor.continuation_envelope_path
                    if continuation and continuation != correction.effective_prompt_path:
                        if not state.cursor.continuation_envelope_sha256:
                            raise ValidationError("usage-limit continuation is missing its digest")
                        wrapped = self.artifacts.read_verified_bytes(
                            run_id,
                            continuation,
                            expected_sha256=state.cursor.continuation_envelope_sha256,
                        )
                        recovered = self.artifacts.read_verified_bytes(
                            run_id,
                            correction.effective_prompt_path,
                            expected_sha256=correction.effective_prompt_sha256,
                        )
                        if recovered not in wrapped:
                            raise ValidationError(
                                "usage-limit continuation dropped the recovered correction envelope"
                            )
                        prompt_path = continuation
                        prompt_sha = state.cursor.continuation_envelope_sha256
                    legacy_binding.update(
                        {
                            "prompt_path": prompt_path,
                            "prompt_sha256": prompt_sha,
                            "chat_id": correction.chat_id,
                            "iteration": correction.iteration,
                            "fix_prompt_path": correction.raw_fix.relative_path,
                            "fix_prompt_sha256": correction.raw_fix.sha256,
                            "staged_patch_path": correction.staged_patch.relative_path,
                            "staged_patch_sha256": correction.staged_patch.sha256,
                            "cursor_recovery_base_prompt_path": correction.base_prompt.relative_path,
                            "cursor_recovery_base_prompt_sha256": correction.base_prompt.sha256,
                            "cursor_recovery_record_sha256": correction.canonical_sha256(),
                        }
                    )
        if (
            state.cursor.usage_limit_fingerprint_path
            and state.cursor.usage_limit_fingerprint_sha256
        ):
            legacy_binding.update(
                {
                    "usage_limit_fingerprint_path": state.cursor.usage_limit_fingerprint_path,
                    "usage_limit_fingerprint_sha256": state.cursor.usage_limit_fingerprint_sha256,
                }
            )
        return legacy_binding

    def _reconcile_existing_attempt(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            attempt = self.store.get_nonterminal_attempt_for_run(conn, run_id)
            if attempt is None and isinstance(state, AbortedState):
                attempt = self.store.get_cancelled_reconcile_attempt_for_run(conn, run_id)
            if attempt is None:
                return None
            dispatch_id = str(attempt["dispatch_id"])
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else ""
            aborted_sdk_reconciliation = (
                isinstance(state, AbortedState)
                and effect_kind == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
                and str(attempt["status"]) != ATTEMPT_STATUS_CANCELLED
            )
            if isinstance(state, AbortedState) and not aborted_sdk_reconciliation:
                return self._reconcile_aborted_attempt(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt=attempt,
                )
            if (
                not isinstance(state, _ATTEMPT_RECONCILE_STATES)
                and not aborted_sdk_reconciliation
                and not _blocked_no_progress_reobserve(state, dispatch)
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            attempt_id = str(attempt["attempt_id"])
            unit_identity = str(attempt["unit_identity"])
            if str(attempt["status"]) == ATTEMPT_STATUS_CANCELLED:
                return self._reconcile_cancelled_attempt(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                )
            claim_id = str(attempt["capacity_claim_id"])
            result_rel = str(attempt["result_artifact_path"])
            attempt_status = str(attempt["status"])
            capacity_tick_generation = int(attempt["capacity_tick_generation"])
            stdout_rel = str(attempt["stdout_artifact_path"])
            stderr_rel = str(attempt["stderr_artifact_path"])
            cursor_attempt = effect_kind in CURSOR_ATTEMPT_EFFECT_KINDS
            codex_attempt = effect_kind in CODEX_ATTEMPT_EFFECT_KINDS
            codex_implementer_attempt = effect_kind in IMPLEMENTER_ATTEMPT_EFFECT_KINDS
            agent_attempt = cursor_attempt or codex_attempt or codex_implementer_attempt
            sdk_attempt = cursor_attempt and isinstance(state.context.cursor, CursorSdkBinding)

        observation = self.backend.observe(unit_identity=unit_identity, attempt_id=attempt_id)
        if observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE:
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_observation_unavailable",
                detail=attempt_id,
            )
        if not observation.owned and not observation.absence_proven:
            return self._persist_attempt_uncertain(
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                capacity_tick_generation=capacity_tick_generation,
                safe_summary="backend reported unit is not owned by this attempt",
            )

        if observation.lifecycle_state == UnitLifecycleState.ACTIVE:
            if attempt_status == ATTEMPT_STATUS_ACTIVE:
                return TickRunReceipt(run_id=run_id, action="attempt_active", detail=attempt_id)
            with self.store.begin_immediate() as conn:
                if not self.store.mark_attempt_active(
                    conn,
                    attempt_id=attempt_id,
                    reconciliation_owner_id=tick_owner_id,
                    reconciliation_lease_generation=tick_lease_generation,
                    now=self._now_factory(),
                ):
                    return TickRunReceipt(run_id=run_id, action="attempt_active_stale")
            return TickRunReceipt(run_id=run_id, action="attempt_active", detail=attempt_id)

        run_root = self.artifacts.run_root(run_id)

        lost_sdk_envelope = (
            sdk_attempt
            and effect_kind in {RUN_CURSOR_TURN_EFFECT_KIND, RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND}
            and observation.lifecycle_state
            in {
                UnitLifecycleState.MISSING,
                UnitLifecycleState.INACTIVE,
                UnitLifecycleState.FAILED,
            }
            and not (run_root / result_rel).is_file()
        )
        if lost_sdk_envelope:
            if observation.lifecycle_state == UnitLifecycleState.MISSING:
                stopped = observation.owned and observation.absence_proven
            else:
                stop_action, stopped_observation = attempt_stop_action(
                    self.backend,
                    unit_identity=unit_identity,
                    attempt_id=attempt_id,
                    require_explicit_stop=True,
                )
                stopped = stop_action == AbortProcessAction.TERMINATED and unit_observation_stopped(
                    stopped_observation
                )
            if not stopped:
                return self._persist_attempt_uncertain(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_id=dispatch_id,
                    claim_id=claim_id,
                    capacity_tick_generation=capacity_tick_generation,
                    safe_summary=(
                        "SDK worker result is missing and owned process tree termination is unproved"
                    ),
                )
            try:
                invocation = verify_cursor_invocation_evidence(
                    run_root,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    dispatch_id=dispatch_id,
                    unit_identity=unit_identity,
                    launch_nonce=str(attempt["launch_nonce"]),
                    launch_intent_sha256=str(attempt["launch_intent_sha256"]),
                    effect_kind=effect_kind,
                )
                verify_pre_execution_cursor_guards(run_root, invocation, run_id=run_id)
                stdout_path = run_root / stdout_rel
                stderr_path = run_root / stderr_rel
                result_path = run_root / result_rel
                payload: dict[str, object] = {
                    "runtime": "sdk-local",
                    "effect_kind": effect_kind,
                    "attempt_id": attempt_id,
                    "run_id": run_id,
                    "dispatch_id": dispatch_id,
                    "iteration": invocation["iteration"],
                    "agent_id": invocation["agent_id"],
                    "returncode": 137,
                    "timed_out": False,
                    "parse_ok": False,
                    "has_completion_signal": False,
                }
                if effect_kind == RUN_CURSOR_TURN_EFFECT_KIND:
                    journal_rel = cursor_sdk_attempt_journal_rel(attempt_id)
                    journal_path = run_root / journal_rel
                    if not seal_interrupted_sdk_send_journal(journal_path):
                        raise ValueError("interrupted SDK send journal cannot be sealed")
                    seal_interrupted_sdk_wait_progress(
                        run_root / cursor_sdk_wait_progress_rel(attempt_id)
                    )
                    payload.update(
                        {
                            "failure_kind": "cursor_sdk_worker_lost",
                            "journal_path": journal_rel,
                            "journal_sha256": sha256_file(journal_path),
                        }
                    )
                else:
                    recovery_key = str(invocation.get("recovery_key", ""))
                    if not recovery_key:
                        raise ValueError("reconciliation invocation lacks recovery authority")
                    from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
                        authenticated_published_reconciliation_result,
                    )

                    with self.store.begin_read() as conn:
                        published = authenticated_published_reconciliation_result(
                            self.store,
                            self.artifacts,
                            conn,
                            run_id=run_id,
                            recovery_key=recovery_key,
                            reconciliation_attempt_id=attempt_id,
                            reconciliation_dispatch_id=dispatch_id,
                        )
                    if published is None:
                        payload["failure_kind"] = "cursor_sdk_reconciliation_worker_lost"
                    else:
                        record_path, record_sha = published
                        payload.update(
                            {
                                "parse_ok": True,
                                "has_completion_signal": True,
                                "reconciliation_record_path": record_path,
                                "reconciliation_record_sha256": record_sha,
                            }
                        )
                atomic_write_text(
                    stdout_path,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    sensitive=True,
                )
                atomic_write_text(
                    stderr_path,
                    "Cursor SDK worker stopped without publishing its result envelope.\n",
                    sensitive=True,
                )
                envelope = build_result_envelope(
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                    exit_code=137,
                    termination_class=TerminationClass.KILLED,
                    stdout_artifact_path=stdout_rel,
                    stdout_sha256=sha256_file(stdout_path),
                    stderr_artifact_path=stderr_rel,
                    stderr_sha256=sha256_file(stderr_path),
                )
                atomic_write_bytes(result_path, envelope, sensitive=True)
            except (
                CursorEvidenceError,
                ProtectedArtifactError,
                SchedulerEngineError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
                json.JSONDecodeError,
            ):
                return self._persist_attempt_uncertain(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_id=dispatch_id,
                    claim_id=claim_id,
                    capacity_tick_generation=capacity_tick_generation,
                    safe_summary="SDK worker stop was proved but recovery evidence is invalid",
                )
            return self._persist_attempt_completion(
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                unit_identity=unit_identity,
                capacity_tick_generation=capacity_tick_generation,
                exit_code=137,
                termination_class=TerminationClass.KILLED,
                envelope_sha256=envelope_sha256(envelope),
                cleanup_unit=False,
            )

        if observation.lifecycle_state == UnitLifecycleState.MISSING:
            if attempt_status == ATTEMPT_STATUS_LAUNCHING and observation.absence_proven:
                binding_path = run_root / invocation_evidence_rel(attempt_id)
                if agent_attempt and not binding_path.is_file():
                    return self._persist_attempt_uncertain(
                        tick_owner_id=tick_owner_id,
                        tick_lease_generation=tick_lease_generation,
                        run_id=run_id,
                        attempt_id=attempt_id,
                        dispatch_id=dispatch_id,
                        claim_id=claim_id,
                        capacity_tick_generation=capacity_tick_generation,
                        safe_summary="cursor invocation binding missing before relaunch",
                    )
                return self._launch_recorded_attempt(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                    claim_id=claim_id,
                    adopt_only=True,
                    cursor_attempt=cursor_attempt,
                    codex_attempt=codex_attempt,
                    codex_implementer_attempt=codex_implementer_attempt,
                )
            if attempt_status == ATTEMPT_STATUS_UNCERTAIN:
                return self._persist_attempt_uncertain(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_id=dispatch_id,
                    claim_id=claim_id,
                    capacity_tick_generation=capacity_tick_generation,
                    safe_summary="attempt unit absent while evidence remains uncertain",
                )
            return self._persist_attempt_uncertain(
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                capacity_tick_generation=capacity_tick_generation,
                safe_summary="attempt unit disappeared before a verified result",
            )

        try:
            validated = validate_completion_evidence(
                run_root=run_root,
                attempt_id=attempt_id,
                unit_identity=unit_identity,
                result_rel=result_rel,
                stdout_rel=stdout_rel,
                stderr_rel=stderr_rel,
                observed_exit_code=observation.exit_code,
                observed_termination=observation.termination_class,
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return self._persist_attempt_uncertain(
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                capacity_tick_generation=capacity_tick_generation,
                safe_summary="result envelope failed validation",
            )

        if sdk_attempt:
            try:
                self.backend.terminate(unit_identity=unit_identity, attempt_id=attempt_id)
                terminated = self.backend.observe(
                    unit_identity=unit_identity,
                    attempt_id=attempt_id,
                )
                if not unit_observation_stopped(terminated):
                    raise RuntimeError("owned SDK process tree remains active after stop")
            except Exception:
                return self._persist_attempt_uncertain(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    dispatch_id=dispatch_id,
                    claim_id=claim_id,
                    capacity_tick_generation=capacity_tick_generation,
                    safe_summary=(
                        "SDK attempt completed but owned process tree termination is unproved"
                    ),
                )

        return self._persist_attempt_completion(
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            run_id=run_id,
            attempt_id=attempt_id,
            dispatch_id=dispatch_id,
            claim_id=claim_id,
            unit_identity=unit_identity,
            capacity_tick_generation=capacity_tick_generation,
            exit_code=validated.exit_code,
            termination_class=validated.termination_class,
            envelope_sha256=validated.envelope_sha256,
            cleanup_unit=not sdk_attempt,
        )

    def _maybe_launch_fake_agent_attempt(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return TickRunReceipt(run_id=run_id, action="attempt_busy")
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            fake_effects = [
                row
                for row in effects
                if str(row["effect_kind"]) == FAKE_AGENT_SELF_TEST_EFFECT_KIND
            ]
            if not fake_effects:
                return None
            dispatch_row = fake_effects[0]
            dispatch_id = str(dispatch_row["dispatch_id"])
            scheduled_version = int(dispatch_row["claimed_run_version"])

        claim_id = self._claim_id_factory()
        attempt_id = self._attempt_id_factory()
        unit_identity = unit_identity_from_attempt_id(attempt_id)
        launch_nonce = self._launch_nonce_factory()
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        result_rel = attempt_result_rel(attempt_id)
        launch_intent = json.dumps(
            {
                "attempt_id": attempt_id,
                "dispatch_id": dispatch_id,
                "run_id": run_id,
                "unit_identity": unit_identity,
                "launch_nonce": launch_nonce,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        launch_intent_sha256 = payload_sha256(launch_intent)

        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return None
            if version < scheduled_version:
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            if not self.store.try_acquire_capacity(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="capacity_busy")
            if not self.store.claim_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=version,
                now=self._now_factory(),
            ):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=self._now_factory(),
                )
                return TickRunReceipt(run_id=run_id, action="attempt_claim_lost")
            self.store.insert_launch_requested_attempt(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                launch_nonce=launch_nonce,
                unit_identity=unit_identity,
                launch_intent_sha256=launch_intent_sha256,
                capacity_claim_id=claim_id,
                capacity_tick_generation=tick_lease_generation,
                stdout_artifact_path=stdout_rel,
                stderr_artifact_path=stderr_rel,
                result_artifact_path=result_rel,
                now=self._now_factory(),
            )
            event = AttemptLaunchRequestedEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                unit_identity=unit_identity,
                launch_nonce=launch_nonce,
                launch_intent_sha256=launch_intent_sha256,
            )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=run_id,
                sequence=sequence,
                event=event,
                now=self._now_factory(),
            )

        return self._launch_recorded_attempt(
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            run_id=run_id,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            claim_id=claim_id,
            adopt_only=False,
        )

    def _launch_recorded_attempt(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt_id: str,
        unit_identity: str,
        claim_id: str,
        adopt_only: bool,
        cursor_attempt: bool = False,
        codex_attempt: bool = False,
        codex_implementer_attempt: bool = False,
    ) -> TickRunReceipt:
        run_root = self.artifacts.run_root(run_id)
        stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, result_rel = (
            prepare_attempt_output_paths(run_root, attempt_id)
        )
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if codex_implementer_attempt:
                if not isinstance(state, _CODEX_IMPLEMENTER_ATTEMPT_STATES):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
                from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                    implementer_recovery_blocks_dispatch,
                )

                if implementer_recovery_blocks_dispatch(self.store, conn, run_id):
                    return TickRunReceipt(
                        run_id=run_id,
                        action="codex_implementer_recovery_dispatch_fenced",
                        detail=attempt_id,
                    )
            elif codex_attempt:
                if not isinstance(state, _CODEX_ATTEMPT_STATES):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            elif cursor_attempt:
                reobserve = _cursor_reobserve_attempt(self.store, conn, attempt_id)
                if not isinstance(state, (*_CURSOR_ATTEMPT_STATES, AbortedState)) and not (
                    isinstance(state, BlockedState) and reobserve
                ):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            elif not isinstance(state, AdmittedState):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            repository_root = Path(str(state.context.repository.root))
            worktree_key = state.context.repository.worktree_key
            if codex_implementer_attempt:
                implementer = state.context.implementer
                if implementer is None or implementer.provider != "codex":
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
                execution_timeout_seconds = implementer.timeout_minutes * 60
            elif codex_attempt:
                execution_timeout_seconds = state.context.workflow.codex_timeout_minutes * 60
            else:
                execution_timeout_seconds = state.context.workflow.cursor_timeout_minutes * 60
            if execution_timeout_seconds <= 0:
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_launch_blocked",
                    detail=attempt_id,
                )
        lock_path = worktree_lock_path(scheduler_state_dir(), worktree_key)
        launch_intent_sha256: str | None = None
        launch_nonce: str | None = None
        dispatch_id_value: str | None = None
        effect_kind_value: str | None = None
        evidence_digest: str | None = None
        if cursor_attempt or codex_attempt or codex_implementer_attempt:
            with self.store.begin_read() as conn:
                attempt_row = self.store.get_attempt_by_id(conn, attempt_id)
                dispatch = (
                    self.store.get_effect_by_dispatch_id(conn, str(attempt_row["dispatch_id"]))
                    if attempt_row is not None
                    else None
                )
            if attempt_row is None or dispatch is None:
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_launch_blocked",
                    detail=attempt_id,
                )
            if (
                isinstance(state, AbortedState)
                and str(dispatch["effect_kind"]) != RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            launch_intent_sha256 = str(attempt_row["launch_intent_sha256"])
            launch_nonce = str(attempt_row["launch_nonce"])
            dispatch_id_value = str(attempt_row["dispatch_id"])
            effect_kind_value = str(dispatch["effect_kind"])
            evidence_path = run_root / invocation_evidence_rel(attempt_id)
            if not evidence_path.is_file():
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_launch_blocked",
                    detail=attempt_id,
                )
            try:
                evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
                if not isinstance(evidence_payload, dict):
                    raise CursorEvidenceError("invocation evidence must be a JSON object")
                evidence_digest = invocation_evidence_sha256(evidence_payload)
                if codex_implementer_attempt:
                    verified_implementer = verify_codex_implementer_invocation_evidence(
                        run_root,
                        attempt_id=attempt_id,
                        run_id=run_id,
                        dispatch_id=dispatch_id_value,
                        unit_identity=unit_identity,
                        launch_nonce=launch_nonce,
                        launch_intent_sha256=launch_intent_sha256,
                        effect_kind=effect_kind_value,
                    )
                    verify_pre_execution_implementer_guards(
                        run_root,
                        verified_implementer,
                        run_id=run_id,
                    )
                elif codex_attempt:
                    verified_codex = verify_codex_invocation_evidence(
                        run_root,
                        attempt_id=attempt_id,
                        run_id=run_id,
                        dispatch_id=dispatch_id_value,
                        unit_identity=unit_identity,
                        launch_nonce=launch_nonce,
                        launch_intent_sha256=launch_intent_sha256,
                        effect_kind=effect_kind_value,
                    )
                    verify_pre_execution_codex_guards(run_root, verified_codex, run_id=run_id)
                else:
                    verified = verify_cursor_invocation_evidence(
                        run_root,
                        attempt_id=attempt_id,
                        run_id=run_id,
                        dispatch_id=dispatch_id_value,
                        unit_identity=unit_identity,
                        launch_nonce=launch_nonce,
                        launch_intent_sha256=launch_intent_sha256,
                        effect_kind=effect_kind_value,
                    )
                    verify_pre_execution_cursor_guards(run_root, verified, run_id=run_id)
            except (
                CursorEvidenceError,
                CodexEvidenceError,
                ValidationError,
                ProtectedArtifactError,
                ValueError,
                TypeError,
                KeyError,
                OSError,
            ) as exc:
                if codex_implementer_attempt and self._codex_workflow is not None:
                    return self._codex_workflow.block_implementer_launch_guard_failure(
                        run_id,
                        attempt_id=attempt_id,
                        reason_kind="codex_implementer_launch_guard_failed",
                        summary=str(exc)[:240],
                    )
                if codex_attempt and self._codex_workflow is not None:
                    return self._codex_workflow.block_launch_guard_failure(
                        run_id,
                        attempt_id=attempt_id,
                        reason_kind="launch_guard_failed",
                        summary=str(exc)[:240],
                    )
                if self._cursor_workflow is not None:
                    return self._cursor_workflow.block_launch_guard_failure(
                        run_id,
                        attempt_id=attempt_id,
                        claim_id=claim_id,
                        tick_owner_id=tick_owner_id,
                        tick_lease_generation=tick_lease_generation,
                        reason_kind="launch_guard_failed",
                        summary=str(exc)[:240],
                    )
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_launch_blocked",
                    detail=attempt_id,
                )
        agent_argv = self._agent_argv(
            run_id,
            attempt_id,
            unit_identity,
            cursor_attempt=cursor_attempt,
            codex_attempt=codex_attempt,
            codex_implementer_attempt=codex_implementer_attempt,
            invocation_evidence_sha=evidence_digest,
            launch_intent_sha256=launch_intent_sha256,
            launch_nonce=launch_nonce,
            dispatch_id=dispatch_id_value,
            effect_kind=effect_kind_value,
        )
        request = LaunchRequest(
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            working_directory=repository_root,
            agent_argv=agent_argv,
            lock_path=lock_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            result_envelope_path=result_path,
            execution_timeout_seconds=execution_timeout_seconds,
        )
        observation = self.backend.observe(unit_identity=unit_identity, attempt_id=attempt_id)
        if observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE:
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_observation_unavailable",
                detail=attempt_id,
            )
        if (
            observation.lifecycle_state == UnitLifecycleState.MISSING
            and not observation.absence_proven
        ):
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_launch_blocked",
                detail=attempt_id,
            )
        if not observation.owned:
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_launch_blocked",
                detail=attempt_id,
            )
        if observation.lifecycle_state == UnitLifecycleState.ACTIVE:
            with self.store.begin_immediate() as conn:
                if self.store.mark_attempt_active(
                    conn,
                    attempt_id=attempt_id,
                    reconciliation_owner_id=tick_owner_id,
                    reconciliation_lease_generation=tick_lease_generation,
                    now=self._now_factory(),
                ):
                    return TickRunReceipt(
                        run_id=run_id, action="attempt_adopted", detail=attempt_id
                    )
            return TickRunReceipt(run_id=run_id, action="attempt_reconcile", detail=attempt_id)
        if adopt_only and observation.lifecycle_state != UnitLifecycleState.MISSING:
            return TickRunReceipt(run_id=run_id, action="attempt_reconcile", detail=attempt_id)
        if observation.lifecycle_state != UnitLifecycleState.MISSING:
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_launch_blocked",
                detail=attempt_id,
            )
        if cursor_attempt or codex_attempt or codex_implementer_attempt:
            evidence_path = run_root / invocation_evidence_rel(attempt_id)
            if not evidence_path.is_file():
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_launch_blocked",
                    detail=attempt_id,
                )
        if not self._verify_launch_authority(
            attempt_id=attempt_id,
            run_id=run_id,
            claim_id=claim_id,
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            codex_implementer_attempt=codex_implementer_attempt,
        ):
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_launch_stale",
                detail=attempt_id,
            )
        try:
            self.backend.launch(request)
        except Exception:
            return TickRunReceipt(run_id=run_id, action="attempt_launch_failed", detail=attempt_id)
        with self.store.begin_immediate() as conn:
            if not self.store.mark_attempt_active(
                conn,
                attempt_id=attempt_id,
                reconciliation_owner_id=tick_owner_id,
                reconciliation_lease_generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(
                    run_id=run_id, action="attempt_launch_stale", detail=attempt_id
                )
        return TickRunReceipt(run_id=run_id, action="attempt_launched", detail=attempt_id)

    def _verify_launch_authority(
        self,
        *,
        attempt_id: str,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        codex_implementer_attempt: bool = False,
    ) -> bool:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            if codex_implementer_attempt:
                from ai_dev_loop.scheduler.application.implementer_failure_recovery import (
                    implementer_recovery_blocks_dispatch,
                )

                if implementer_recovery_blocks_dispatch(self.store, conn, run_id):
                    return False
            return self.store.verify_launch_authority(
                conn,
                attempt_id=attempt_id,
                run_id=run_id,
                claim_id=claim_id,
                reconciliation_owner_id=tick_owner_id,
                reconciliation_lease_generation=tick_lease_generation,
                now=now,
            )

    def _cleanup_completed_unit(self, *, unit_identity: str, attempt_id: str) -> None:
        try:
            self.backend.terminate(unit_identity=unit_identity, attempt_id=attempt_id)
        except Exception:
            return

    def _agent_argv(
        self,
        run_id: str,
        attempt_id: str,
        unit_identity: str,
        *,
        cursor_attempt: bool = False,
        codex_attempt: bool = False,
        codex_implementer_attempt: bool = False,
        invocation_evidence_sha: str | None = None,
        launch_intent_sha256: str | None = None,
        launch_nonce: str | None = None,
        dispatch_id: str | None = None,
        effect_kind: str | None = None,
    ) -> list[str]:
        if self._build_agent_argv is not None:
            return self._build_agent_argv(
                run_id,
                attempt_id,
                unit_identity,
                self.artifacts.artifact_root,
            )
        import sys

        if codex_implementer_attempt:
            module = "ai_dev_loop.scheduler.codex_implementer_attempt_runner"
        elif codex_attempt:
            module = "ai_dev_loop.scheduler.codex_attempt_runner"
        elif cursor_attempt:
            module = "ai_dev_loop.scheduler.cursor_attempt_runner"
        else:
            module = "ai_dev_loop.scheduler.fake_agent_runner"
        argv = [
            sys.executable,
            "-m",
            module,
            "--run-id",
            run_id,
            "--attempt-id",
            attempt_id,
            "--unit-identity",
            unit_identity,
            "--artifact-root",
            str(self.artifacts.artifact_root),
        ]
        if cursor_attempt or codex_attempt or codex_implementer_attempt:
            if not (
                invocation_evidence_sha
                and launch_intent_sha256
                and launch_nonce
                and dispatch_id
                and effect_kind
            ):
                raise ValueError("agent attempt launch requires pinned invocation bindings")
            argv.extend(
                [
                    "--invocation-evidence-sha256",
                    invocation_evidence_sha,
                    "--launch-intent-sha256",
                    launch_intent_sha256,
                    "--launch-nonce",
                    launch_nonce,
                    "--dispatch-id",
                    dispatch_id,
                    "--effect-kind",
                    effect_kind,
                ]
            )
        return argv

    def _persist_attempt_completion(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt_id: str,
        dispatch_id: str,
        claim_id: str,
        unit_identity: str,
        capacity_tick_generation: int,
        exit_code: int,
        termination_class: TerminationClass,
        envelope_sha256: str,
        cleanup_unit: bool = True,
    ) -> TickRunReceipt:
        now = self._now_factory()
        terminal_status = (
            ATTEMPT_STATUS_COMPLETED
            if termination_class == TerminationClass.SUCCESS
            else ATTEMPT_STATUS_FAILED
        )
        fence_id = self._fence_id_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            dispatch_row = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            aborted_sdk_reconciliation = (
                isinstance(state, AbortedState)
                and dispatch_row is not None
                and str(dispatch_row["effect_kind"]) == RECONCILE_CURSOR_SDK_RUN_EFFECT_KIND
            )
            if isinstance(state, AbortedState) and not aborted_sdk_reconciliation:
                self._reject_stale_attempt_result(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    rejection_kind="run_aborted",
                    safe_summary="attempt completion blocked by scheduler abort",
                    conn=conn,
                )
                self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
                finalize_aborted_run_cleanup(
                    self.store,
                    conn,
                    run_id=run_id,
                    now=now,
                )
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_result_stale",
                    detail="run_aborted",
                )
            if (
                not isinstance(state, _ATTEMPT_RECONCILE_STATES)
                and not aborted_sdk_reconciliation
                and not _blocked_no_progress_reobserve(state, dispatch_row)
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            attempt_row = self.store.get_attempt_by_id(conn, attempt_id)
            if attempt_row is not None and str(attempt_row["status"]) == ATTEMPT_STATUS_CANCELLED:
                self._reject_stale_attempt_result(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    rejection_kind="attempt_cancelled",
                    safe_summary="attempt completion blocked by cancellation fence",
                    conn=conn,
                )
                self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
                if isinstance(state, AbortedState):
                    finalize_aborted_run_cleanup(
                        self.store,
                        conn,
                        run_id=run_id,
                        now=now,
                    )
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_result_stale",
                    detail="attempt_cancelled",
                )
            capacity_claim_owner_id = self.store.get_claim_owner_id(conn, claim_id)
            if capacity_claim_owner_id is None:
                return TickRunReceipt(run_id=run_id, action="attempt_complete_stale")
            if not self.store.complete_attempt_fenced(
                conn,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                capacity_claim_owner_id=capacity_claim_owner_id,
                capacity_tick_generation=capacity_tick_generation,
                expected_run_version=version,
                completion_fence_id=fence_id,
                exit_code=exit_code,
                termination_class=termination_class.value,
                completion_envelope_sha256=envelope_sha256,
                terminal_status=terminal_status,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_complete_stale")
            if not self.store.complete_claimed_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=capacity_claim_owner_id,
                tick_lease_generation=capacity_tick_generation,
                expected_run_version=version,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_complete_stale")
            event = AttemptCompletedEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                completion_fence_id=fence_id,
                termination_class=termination_class.value,
                exit_code=exit_code,
            )
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
            self.store.release_capacity(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=capacity_claim_owner_id,
                tick_lease_generation=capacity_tick_generation,
                now=now,
            )
        if cleanup_unit:
            self._cleanup_completed_unit(unit_identity=unit_identity, attempt_id=attempt_id)
        return TickRunReceipt(run_id=run_id, action="attempt_completed", detail=attempt_id)

    def _persist_attempt_uncertain(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt_id: str,
        dispatch_id: str,
        claim_id: str,
        capacity_tick_generation: int,
        safe_summary: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            if not self.store.mark_attempt_uncertain(
                conn,
                attempt_id=attempt_id,
                claim_id=claim_id,
                tick_lease_generation=capacity_tick_generation,
                now=now,
            ):
                attempt = self.store.get_attempt_by_id(conn, attempt_id)
                if attempt is not None and str(attempt["status"]) == ATTEMPT_STATUS_UNCERTAIN:
                    return TickRunReceipt(
                        run_id=run_id, action="attempt_uncertain", detail=attempt_id
                    )
                return TickRunReceipt(run_id=run_id, action="attempt_uncertain_stale")
            event = AttemptUncertainEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                safe_summary=safe_summary,
            )
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
        return TickRunReceipt(run_id=run_id, action="attempt_uncertain", detail=attempt_id)

    def _maybe_finalize_aborted_resources(
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
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AbortedState):
                return None
            if self.store.has_unresolved_abort_hold(conn, run_id):
                return None
            if not self.store.has_unreleased_abort_resources(conn, run_id):
                return None
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AbortedState):
                return None
            if self.store.has_unresolved_abort_hold(conn, run_id):
                return None
            finalize_aborted_run_cleanup(
                self.store,
                conn,
                run_id=run_id,
                now=now,
            )
            if self.store.has_unreleased_abort_resources(conn, run_id):
                return None
        return TickRunReceipt(run_id=run_id, action="abort_resource_cleanup")

    def _reconcile_aborted_attempt(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt: sqlite3.Row,
    ) -> TickRunReceipt:
        attempt_id = str(attempt["attempt_id"])
        unit_identity = attempt["unit_identity"]
        if unit_identity is None:
            now = self._now_factory()
            with self.store.begin_immediate() as conn:
                if not tick_lease_is_active(
                    self.store,
                    conn,
                    owner_id=tick_owner_id,
                    generation=tick_lease_generation,
                    now=now,
                ):
                    return TickRunReceipt(run_id=run_id, action="attempt_stale")
                finalize_resolved_abort_attempt(
                    self.store,
                    conn,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    now=now,
                    event_id_factory=self._event_id_factory,
                    stale_rejection_kind="run_aborted",
                    stale_safe_summary="attempt result arrived after scheduler abort",
                )
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_result_stale",
                detail="run_aborted",
            )

        _, observation = attempt_stop_action(
            self.backend,
            unit_identity=str(unit_identity),
            attempt_id=attempt_id,
            require_explicit_stop=True,
        )
        if not unit_observation_stopped(observation):
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_abort_pending",
                detail=attempt_id,
            )
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            from ai_dev_loop.scheduler.application.cursor_sdk_reconciliation import (
                ensure_aborted_sdk_turn_reconciliation,
            )

            try:
                obligation_ready = ensure_aborted_sdk_turn_reconciliation(
                    self.store,
                    self.artifacts,
                    conn,
                    run_id=run_id,
                    attempt=attempt,
                    now=now,
                    event_id_factory=self._event_id_factory,
                )
            except (
                ProtectedArtifactError,
                SchedulerEngineError,
                OSError,
                ValueError,
                TypeError,
                KeyError,
            ):
                obligation_ready = False
            if not obligation_ready:
                return TickRunReceipt(
                    run_id=run_id,
                    action="attempt_abort_pending",
                    detail=attempt_id,
                )
            finalize_resolved_abort_attempt(
                self.store,
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                now=now,
                event_id_factory=self._event_id_factory,
                stale_rejection_kind="run_aborted",
                stale_safe_summary="attempt result arrived after scheduler abort",
            )
        return TickRunReceipt(
            run_id=run_id,
            action="attempt_result_stale",
            detail="run_aborted",
        )

    def _reconcile_cancelled_attempt(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt_id: str,
        unit_identity: str,
    ) -> TickRunReceipt:
        _, observation = attempt_stop_action(
            self.backend,
            unit_identity=unit_identity,
            attempt_id=attempt_id,
        )
        if not unit_observation_stopped(observation):
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_abort_pending",
                detail=attempt_id,
            )
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            finalize_resolved_abort_attempt(
                self.store,
                conn,
                run_id=run_id,
                attempt_id=attempt_id,
                now=now,
                event_id_factory=self._event_id_factory,
                stale_rejection_kind="attempt_cancelled",
                stale_safe_summary="attempt result arrived after cancellation fence",
            )
        return TickRunReceipt(
            run_id=run_id,
            action="attempt_result_stale",
            detail="attempt_cancelled",
        )

    def _reject_stale_attempt_result(
        self,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
        attempt_id: str,
        rejection_kind: str,
        safe_summary: str,
        conn: sqlite3.Connection | None = None,
    ) -> TickRunReceipt:
        now = self._now_factory()

        def _append(connection: sqlite3.Connection) -> TickRunReceipt | None:
            if not tick_lease_is_active(
                self.store,
                connection,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="attempt_stale")
            append_attempt_result_stale_event(
                self.store,
                connection,
                run_id=run_id,
                attempt_id=attempt_id,
                rejection_kind=rejection_kind,
                safe_summary=safe_summary,
                now=now,
                event_id_factory=self._event_id_factory,
            )
            return TickRunReceipt(
                run_id=run_id,
                action="attempt_result_stale",
                detail=rejection_kind,
            )

        if isinstance(conn, sqlite3.Connection):
            result = _append(conn)
            assert result is not None
            return result
        with self.store.begin_immediate() as new_conn:
            result = _append(new_conn)
            assert result is not None
            return result


def default_attempt_id_factory() -> Callable[[], str]:
    return lambda: f"att-{secrets.token_hex(16)}"


def default_launch_nonce_factory() -> Callable[[], str]:
    return lambda: secrets.token_hex(16)
