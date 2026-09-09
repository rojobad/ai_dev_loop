"""Scheduler attempt launch, observe, and reconcile service."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ai_dev_loop.errors import ValidationError
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
    verify_codex_invocation_evidence,
    verify_pre_execution_codex_guards,
)
from ai_dev_loop.scheduler.application.codex_workflow_service import CodexWorkflowService
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    frozen_repository_identity,
    invocation_evidence_sha256,
    verify_cursor_invocation_evidence,
    verify_pre_execution_cursor_guards,
)
from ai_dev_loop.scheduler.application.cursor_workflow_service import CursorWorkflowService
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
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
    AdmittedState,
    AwaitingCodexReviewState,
    CursorReadyState,
    PreflightCompleteState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.paths import scheduler_state_dir
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_ACTIVE,
    ATTEMPT_STATUS_COMPLETED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_LAUNCHING,
    ATTEMPT_STATUS_UNCERTAIN,
    SqliteSchedulerStore,
)

_CURSOR_ATTEMPT_STATES = (
    PreflightCompleteState,
    CursorReadyState,
    WaitingUsageLimitState,
    WaitingForCursorFixState,
)
_CODEX_ATTEMPT_STATES = (AwaitingCodexReviewState,)
_ATTEMPT_RECONCILE_STATES = (AdmittedState, *_CURSOR_ATTEMPT_STATES, *_CODEX_ATTEMPT_STATES)


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
        self._build_agent_argv = build_agent_argv

    def process_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        reconcile = self._reconcile_existing_attempt(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if reconcile is not None:
            return reconcile
        return (
            self._maybe_launch_codex_attempt(
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
                ),
            ):
                return None
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
            scheduled_version = int(dispatch_row["claimed_run_version"])
            cursor_state = state

        claim_id = self._claim_id_factory()
        attempt_id = self._attempt_id_factory()
        unit_identity = unit_identity_from_attempt_id(attempt_id)
        launch_nonce = self._launch_nonce_factory()
        stdout_rel = attempt_stdout_rel(attempt_id)
        stderr_rel = attempt_stderr_rel(attempt_id)
        result_rel = attempt_result_rel(attempt_id)
        evidence = self._cursor_binding(cursor_state, effect_kind=effect_kind, run_id=run_id)
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
            if not isinstance(
                state,
                (
                    PreflightCompleteState,
                    CursorReadyState,
                    WaitingUsageLimitState,
                    WaitingForCursorFixState,
                ),
            ):
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
                iteration=state.cursor.iteration if hasattr(state, "cursor") else 1,
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
            "plan_repository_path": context.plan_prompt.plan_repository_path,
            "plan_artifact_path": context.plan_prompt.plan_artifact_path,
            "plan_sha256": context.plan_prompt.plan_sha256,
            "prompt_source_repository_path": context.plan_prompt.prompt_source_repository_path,
            "prompt_artifact_path": context.plan_prompt.prompt_artifact_path,
            "prompt_sha256": context.plan_prompt.prompt_sha256,
            "review_iteration": state.cursor.iteration,
            "cursor_chat_id": state.cursor.chat_id,
            "max_review_iterations": context.workflow.max_review_iterations,
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
        return binding

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
        binding: dict[str, object] = {
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
            return binding
        assert effect_kind == RUN_CURSOR_TURN_EFFECT_KIND
        iteration = state.cursor.iteration
        if isinstance(state, WaitingForCursorFixState):
            envelope_path = state.codex.latest_correction_envelope_path
            envelope_sha = state.codex.latest_correction_envelope_sha256
            if not envelope_path or not envelope_sha:
                raise ValidationError(
                    "correction cursor turn requires persisted execution envelope binding"
                )
            binding.update(
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
            binding.update(
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
        if (
            state.cursor.usage_limit_fingerprint_path
            and state.cursor.usage_limit_fingerprint_sha256
        ):
            binding.update(
                {
                    "usage_limit_fingerprint_path": state.cursor.usage_limit_fingerprint_path,
                    "usage_limit_fingerprint_sha256": state.cursor.usage_limit_fingerprint_sha256,
                }
            )
        return binding

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
            attempt = self.store.get_nonterminal_attempt_for_run(conn, run_id)
            if attempt is None:
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, _ATTEMPT_RECONCILE_STATES):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            attempt_id = str(attempt["attempt_id"])
            unit_identity = str(attempt["unit_identity"])
            dispatch_id = str(attempt["dispatch_id"])
            claim_id = str(attempt["capacity_claim_id"])
            result_rel = str(attempt["result_artifact_path"])
            attempt_status = str(attempt["status"])
            capacity_tick_generation = int(attempt["capacity_tick_generation"])
            stdout_rel = str(attempt["stdout_artifact_path"])
            stderr_rel = str(attempt["stderr_artifact_path"])
            dispatch = self.store.get_effect_by_dispatch_id(conn, dispatch_id)
            effect_kind = str(dispatch["effect_kind"]) if dispatch is not None else ""
            cursor_attempt = effect_kind in CURSOR_ATTEMPT_EFFECT_KINDS
            codex_attempt = effect_kind in CODEX_ATTEMPT_EFFECT_KINDS
            agent_attempt = cursor_attempt or codex_attempt

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
    ) -> TickRunReceipt:
        run_root = self.artifacts.run_root(run_id)
        stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, result_rel = (
            prepare_attempt_output_paths(run_root, attempt_id)
        )
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if codex_attempt:
                if not isinstance(state, _CODEX_ATTEMPT_STATES):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            elif cursor_attempt:
                if not isinstance(state, _CURSOR_ATTEMPT_STATES):
                    return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            elif not isinstance(state, AdmittedState):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            repository_root = Path(str(state.context.repository.root))
            worktree_key = state.context.repository.worktree_key
        lock_path = worktree_lock_path(scheduler_state_dir(), worktree_key)
        launch_intent_sha256: str | None = None
        launch_nonce: str | None = None
        dispatch_id_value: str | None = None
        effect_kind_value: str | None = None
        evidence_digest: str | None = None
        if cursor_attempt or codex_attempt:
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
                if codex_attempt:
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
        if cursor_attempt or codex_attempt:
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
    ) -> bool:
        now = self._now_factory()
        with self.store.begin_read() as conn:
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

        if codex_attempt:
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
        if cursor_attempt or codex_attempt:
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
            if not isinstance(state, _ATTEMPT_RECONCILE_STATES):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
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


def default_attempt_id_factory() -> Callable[[], str]:
    return lambda: f"att-{secrets.token_hex(16)}"


def default_launch_nonce_factory() -> Callable[[], str]:
    return lambda: secrets.token_hex(16)
