"""Scheduler attempt launch, observe, and reconcile service."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

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
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.effects import FAKE_AGENT_SELF_TEST_EFFECT_KIND
from ai_dev_loop.scheduler.domain.events import (
    AttemptCompletedEvent,
    AttemptLaunchRequestedEvent,
    AttemptUncertainEvent,
)
from ai_dev_loop.scheduler.domain.state import AdmittedState
from ai_dev_loop.scheduler.infrastructure.paths import scheduler_state_dir
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    ATTEMPT_STATUS_ACTIVE,
    ATTEMPT_STATUS_COMPLETED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_LAUNCHING,
    ATTEMPT_STATUS_UNCERTAIN,
    SqliteSchedulerStore,
)


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
        return self._maybe_launch_fake_agent_attempt(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )

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
            if not isinstance(state, AdmittedState):
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

        if observation.lifecycle_state == UnitLifecycleState.MISSING:
            if attempt_status == ATTEMPT_STATUS_LAUNCHING and observation.absence_proven:
                return self._launch_recorded_attempt(
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    run_id=run_id,
                    attempt_id=attempt_id,
                    unit_identity=unit_identity,
                    claim_id=claim_id,
                    adopt_only=True,
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

        run_root = self.artifacts.run_root(run_id)
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
    ) -> TickRunReceipt:
        run_root = self.artifacts.run_root(run_id)
        stdout_path, stderr_path, result_path, stdout_rel, stderr_rel, result_rel = (
            prepare_attempt_output_paths(run_root, attempt_id)
        )
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return TickRunReceipt(run_id=run_id, action="attempt_state_changed")
            repository_root = Path(str(state.context.repository.root))
            worktree_key = state.context.repository.worktree_key
        lock_path = worktree_lock_path(scheduler_state_dir(), worktree_key)
        agent_argv = self._agent_argv(run_id, attempt_id, unit_identity)
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

    def _agent_argv(self, run_id: str, attempt_id: str, unit_identity: str) -> list[str]:
        if self._build_agent_argv is not None:
            return self._build_agent_argv(
                run_id,
                attempt_id,
                unit_identity,
                self.artifacts.artifact_root,
            )
        import sys

        return [
            sys.executable,
            "-m",
            "ai_dev_loop.scheduler.fake_agent_runner",
            "--run-id",
            run_id,
            "--attempt-id",
            attempt_id,
            "--unit-identity",
            unit_identity,
            "--artifact-root",
            str(self.artifacts.artifact_root),
        ]

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
            if not isinstance(state, AdmittedState):
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
