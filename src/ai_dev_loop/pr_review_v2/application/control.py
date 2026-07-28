"""Control-plane operations for ``pr-review-v2`` (start/status/history/resume/abort)."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from ai_dev_loop.pr_review_v2.application.contracts import (
    EventDisposition,
    EventSubmission,
    NextActionCategory,
)
from ai_dev_loop.pr_review_v2.application.control_contracts import (
    HARD_HISTORY_MAX,
    AbortProcessAction,
    AbortResult,
    ControlError,
    ControlErrorKind,
    ControlStatus,
    HistoryEntry,
    HistoryResult,
    OperatorContinuationArtifact,
    OriginKind,
    ResumeResult,
    SafeNextAction,
    StartResult,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.execution_context import ExecutionContextArtifact
from ai_dev_loop.pr_review_v2.domain.common import ArtifactRef, UserContinuationEvidence
from ai_dev_loop.pr_review_v2.domain.events import (
    ResumeRequested,
    StartRequested,
    UserContinuationRequested,
)
from ai_dev_loop.pr_review_v2.domain.state import WaitingForUserState
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import parse_utc_instant
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    SupervisorLauncherStore,
    signal_owned_supervisor,
)

SupervisorSpawner = Callable[[str], str]  # run_id -> action: spawned|reused|repaired|spawn_failed


def map_engine_next_action(category: NextActionCategory, *, state_kind: str) -> SafeNextAction:
    if category is NextActionCategory.START or state_kind == "prepared":
        return SafeNextAction.START
    if category is NextActionCategory.WAIT_FOR_USER:
        return SafeNextAction.RESUME_CONFIRM_USER_CONTINUATION
    if category is NextActionCategory.RESUME:
        return SafeNextAction.RESUME
    if category in {
        NextActionCategory.WAIT_FOR_RETRY,
        NextActionCategory.WAIT_FOR_ELIGIBILITY,
    }:
        return SafeNextAction.WAIT_UNTIL
    if category is NextActionCategory.INSPECT:
        return SafeNextAction.INSPECT_PROTECTED_FAILURE
    if category in {
        NextActionCategory.TERMINAL_COMPLETED,
        NextActionCategory.TERMINAL_FAILED,
        NextActionCategory.TERMINAL_ABORTED,
        NextActionCategory.NONE,
    }:
        return SafeNextAction.NONE
    if state_kind in {"paused"}:
        return SafeNextAction.RESUME
    return SafeNextAction.NONE


class ControlPlaneService:
    def __init__(
        self,
        engine: PrReviewEngine,
        *,
        artifact_store: ProtectedResultStore,
        launcher_store: SupervisorLauncherStore,
        spawner: SupervisorSpawner | None = None,
    ) -> None:
        self._engine = engine
        self._artifacts = artifact_store
        self._launchers = launcher_store
        self._spawner = spawner

    def start(self, run_id: str) -> StartResult:
        status = self._engine.get_status(run_id)
        if status.state_kind != "prepared":
            # Idempotent repair: active without live supervisor
            if status.state_kind not in {
                "completed",
                "failed",
                "aborted",
                "paused",
                "waiting_for_user",
            }:
                live = self._supervisor_live(run_id)
                if live:
                    return StartResult(
                        run_id=run_id,
                        state_kind=status.state_kind,
                        transition_applied=False,
                        supervisor_action="reused",
                        next_action=map_engine_next_action(
                            status.next_action, state_kind=status.state_kind
                        ),
                    )
                action = self._spawn(run_id)
                return StartResult(
                    run_id=run_id,
                    state_kind=self._engine.get_status(run_id).state_kind,
                    transition_applied=False,
                    supervisor_action="repaired" if action != "spawn_failed" else "spawn_failed",
                    next_action=SafeNextAction.NONE
                    if action != "spawn_failed"
                    else SafeNextAction.START,
                    safe_detail="supervisor repaired without duplicating start event"
                    if action != "spawn_failed"
                    else "supervisor spawn failed; pending effect preserved",
                )
            raise ControlError(
                ControlErrorKind.NOT_PREPARED,
                "start requires PreparedState",
                next_action=map_engine_next_action(
                    status.next_action, state_kind=status.state_kind
                ).value,
            )

        receipt = self._engine.apply_event(
            EventSubmission(
                submission_id=f"start:{run_id}:{status.run_version}",
                run_id=run_id,
                expected_version=status.run_version,
                event=StartRequested(occurred_at=self._engine.clock.now()),
            )
        )
        if receipt.disposition is not EventDisposition.ACCEPTED:
            raise ControlError(
                ControlErrorKind.VALIDATION,
                receipt.safe_detail or "start transition rejected",
            )
        action = self._spawn(run_id)
        after = self._engine.get_status(run_id)
        return StartResult(
            run_id=run_id,
            state_kind=after.state_kind,
            transition_applied=True,
            supervisor_action=action,  # type: ignore[arg-type]
            next_action=SafeNextAction.NONE if action != "spawn_failed" else SafeNextAction.START,
            safe_detail=None
            if action != "spawn_failed"
            else "start persisted; supervisor spawn failed — retry start to repair",
        )

    def resume(
        self,
        run_id: str,
        *,
        confirm_user_continuation: bool = False,
    ) -> ResumeResult:
        status = self._engine.get_status(run_id)
        if status.state_kind == "prepared":
            raise ControlError(
                ControlErrorKind.NOT_RESUMABLE,
                "prepared runs require start",
                next_action=SafeNextAction.START.value,
            )
        if status.state_kind in {"completed", "failed", "aborted"}:
            raise ControlError(
                ControlErrorKind.NOT_RESUMABLE,
                "run is terminal",
                next_action=SafeNextAction.NONE.value,
            )
        if status.state_kind == "waiting_for_user":
            if not confirm_user_continuation:
                raise ControlError(
                    ControlErrorKind.REQUIRES_USER_CONFIRMATION,
                    "waiting_for_user requires --confirm-user-continuation",
                    next_action=SafeNextAction.RESUME_CONFIRM_USER_CONTINUATION.value,
                )
            return self._resume_user_continuation(run_id, status.run_version)

        if status.state_kind == "paused":
            receipt = self._engine.apply_event(
                EventSubmission(
                    submission_id=f"resume:{run_id}:{status.run_version}",
                    run_id=run_id,
                    expected_version=status.run_version,
                    event=ResumeRequested(occurred_at=self._engine.clock.now()),
                )
            )
            if receipt.disposition is not EventDisposition.ACCEPTED:
                raise ControlError(
                    ControlErrorKind.NOT_RESUMABLE,
                    receipt.safe_detail or "resume rejected",
                )
            action = self._spawn(run_id)
            after = self._engine.get_status(run_id)
            return ResumeResult(
                run_id=run_id,
                state_kind=after.state_kind,
                transition_applied=True,
                supervisor_action=action,  # type: ignore[arg-type]
                next_action=SafeNextAction.NONE,
            )

        # Active state: repair supervisor only; never fire timers early.
        if self._supervisor_live(run_id):
            return ResumeResult(
                run_id=run_id,
                state_kind=status.state_kind,
                transition_applied=False,
                supervisor_action="reused",
                next_action=map_engine_next_action(
                    status.next_action, state_kind=status.state_kind
                ),
            )
        action = self._spawn(run_id)
        return ResumeResult(
            run_id=run_id,
            state_kind=self._engine.get_status(run_id).state_kind,
            transition_applied=False,
            supervisor_action="repaired" if action != "spawn_failed" else "spawn_failed",
            next_action=SafeNextAction.NONE,
            safe_detail="supervisor repaired" if action != "spawn_failed" else "spawn failed",
        )

    def abort(self, run_id: str) -> AbortResult:
        # Capture carrier identity before AbortedState drops the active effect.
        carrier_id = self._capture_local_fix_carrier_id(run_id)
        receipt = self._engine.abort_run(
            submission_id=f"abort:{run_id}:{secrets.token_hex(8)}",
            run_id=run_id,
        )
        process_action = self._terminate_owned_processes(run_id, carrier_run_id=carrier_id)
        after = self._engine.get_status(run_id)
        return AbortResult(
            run_id=run_id,
            state_kind=after.state_kind,
            abort_persisted=receipt.disposition
            in {EventDisposition.ACCEPTED, EventDisposition.DUPLICATE},
            process_action=process_action,
            next_action=SafeNextAction.NONE,
        )

    def _capture_local_fix_carrier_id(self, run_id: str) -> str | None:
        """Read validated active RunLocalFixEffect and derive deterministic carrier id."""

        from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
        from ai_dev_loop.pr_review_v2.domain.state import active_effect
        from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import carrier_run_id

        try:
            with self._engine.store.begin_read() as conn:
                state, _version, _updated = self._engine.store.load_validated_snapshot(conn, run_id)
        except Exception:  # noqa: BLE001
            return None
        effect = active_effect(state)
        if not isinstance(effect, RunLocalFixEffect):
            return None
        if effect.run_id != run_id:
            return None
        return carrier_run_id(run_id, effect.cycle_number, effect.effect_id)

    def _terminate_owned_processes(
        self, run_id: str, *, carrier_run_id: str | None = None
    ) -> AbortProcessAction:
        """Carrier Cursor → owned Codex children → supervisor (durable-abort-first)."""

        import contextlib

        from ai_dev_loop.abort_control import (
            ProcessSignalOutcome,
            signal_active_process_group,
            write_abort_request,
        )
        from ai_dev_loop.pr_review_v2.workers.owned_children import (
            OwnedChildStore,
            signal_owned_child,
        )
        from ai_dev_loop.run_discovery import find_run_directory

        actions: list[str] = []

        # 1) Subordinate carrier Cursor: write abort request, then signal its process group.
        if carrier_run_id is not None:
            try:
                carrier_dir = find_run_directory(carrier_run_id)
            except Exception:  # noqa: BLE001
                carrier_dir = None
            if carrier_dir is not None:
                with contextlib.suppress(Exception):
                    write_abort_request(carrier_dir, run_id=carrier_run_id)
                signal_result = signal_active_process_group(carrier_dir, run_id=carrier_run_id)
                if signal_result.outcome is ProcessSignalOutcome.SIGNALED:
                    actions.append("terminated")
                elif signal_result.outcome is ProcessSignalOutcome.STALE:
                    actions.append("refused")

        # 2) Owned v2 Codex (and any other registered) children for this run.
        children = OwnedChildStore(self._artifacts.root)
        for child in children.list_for_run(run_id):
            actions.append(signal_owned_child(child, run_id=run_id))
            if actions[-1] == "terminated":
                children.clear(run_id, child.component)

        # 3) Supervisor last.
        meta = self._launchers.read(run_id)
        if meta is not None:
            result = signal_owned_supervisor(meta, run_id=run_id)
            actions.append(result)
            if result == "terminated":
                self._launchers.clear(run_id)

        if not actions:
            return AbortProcessAction.UNNECESSARY
        if any(item == "terminated" for item in actions):
            return AbortProcessAction.TERMINATED
        if all(item == "unnecessary" for item in actions):
            return AbortProcessAction.UNNECESSARY
        if any(item == "refused" for item in actions):
            return AbortProcessAction.REFUSED
        return AbortProcessAction.UNNECESSARY

    def status(self, run_id: str, *, recent_limit: int = 5) -> ControlStatus:
        engine_status = self._engine.get_status(run_id)
        with self._engine.store.begin_read() as conn:
            state, _version, _updated = self._engine.store.load_validated_snapshot(conn, run_id)
        origin_kind = None
        max_ext = None
        max_local = None
        if hasattr(state, "origin"):
            origin_kind = OriginKind(state.origin.kind)
            max_ext = state.limits.max_external_cycles
            max_local = state.limits.max_local_iterations
        history = self.history(run_id, limit=recent_limit, order="newest")
        supervisor_live = self._supervisor_live(run_id)
        resumable, next_action = self._status_recovery_guidance(
            state_kind=engine_status.state_kind,
            engine_next_action=engine_status.next_action,
            supervisor_live=supervisor_live,
            lease_active=engine_status.lease_active,
        )
        return ControlStatus(
            run_id=run_id,
            origin_kind=origin_kind,
            state_kind=engine_status.state_kind,
            run_version=engine_status.run_version,
            cycle_number=engine_status.cycle_number,
            max_external_cycles=max_ext,
            max_local_iterations=max_local,
            repository=engine_status.repository,
            pr_number=engine_status.pr_number,
            head_sha_short=engine_status.head_sha_short,
            active_effect_kind=engine_status.active_effect_kind,
            effect_status=engine_status.effect_status.value
            if engine_status.effect_status is not None
            else None,
            effect_attempt=engine_status.effect_attempt,
            effect_max_attempts=engine_status.effect_max_attempts,
            pending_or_claimed=engine_status.effect_status is not None
            and engine_status.effect_status.value in {"pending", "claimed"},
            retry_waiting=engine_status.state_kind == "waiting_retry",
            next_eligible_at=engine_status.next_eligible_at,
            last_durable_transition_at=engine_status.updated_at,
            last_error_kind=engine_status.last_error_kind,
            last_error_summary=engine_status.last_error_summary,
            resumable=resumable,
            supervisor_live=supervisor_live,
            lease_active=engine_status.lease_active,
            safe_action_kind=engine_status.safe_action_kind,
            safe_action_condition=engine_status.safe_action_condition,
            engine_next_action=engine_status.next_action,
            next_action=next_action,
            recent_history=history.entries,
        )

    def _status_recovery_guidance(
        self,
        *,
        state_kind: str,
        engine_next_action: NextActionCategory,
        supervisor_live: bool,
        lease_active: bool,
    ) -> tuple[bool, SafeNextAction]:
        """Map status to a safe operator action without racing a live lease."""

        mapped = map_engine_next_action(engine_next_action, state_kind=state_kind)
        if state_kind in {"prepared", "completed", "failed", "aborted"}:
            return False, mapped
        if state_kind == "waiting_for_user":
            return True, mapped
        if state_kind == "paused":
            return True, mapped
        if supervisor_live:
            return False, mapped
        # Nonterminal active run with no live supervisor.
        if lease_active:
            # Do not advise a competing resume while old ownership may still be live.
            return False, SafeNextAction.WAIT_UNTIL
        return True, SafeNextAction.RESUME

    def history(
        self,
        run_id: str,
        *,
        limit: int = 50,
        order: str = "oldest",
    ) -> HistoryResult:
        if limit < 1:
            raise ControlError(ControlErrorKind.VALIDATION, "history limit must be positive")
        capped = min(limit, HARD_HISTORY_MAX)
        newest_first = order == "newest"
        with self._engine.store.begin_read() as conn:
            # Fetch one extra to detect truncation.
            rows = self._engine.store.list_events_for_run(
                conn, run_id, limit=capped + 1, newest_first=newest_first
            )
        truncated = len(rows) > capped
        rows = rows[:capped]
        entries: list[HistoryEntry] = []
        for row in rows:
            entries.append(
                HistoryEntry(
                    sequence=int(row["sequence"]),
                    event_kind=str(row["event_kind"]),
                    disposition=str(row["disposition"]),
                    created_at=parse_utc_instant(row["created_at"]),
                    rejection_code=row["rejection_code"],
                    safe_detail=row["safe_detail"],
                )
            )
        return HistoryResult(
            run_id=run_id,
            order="newest" if newest_first else "oldest",
            limit=capped,
            truncated=truncated,
            entries=tuple(entries),
        )

    def _resume_user_continuation(self, run_id: str, expected_version: int) -> ResumeResult:
        with self._engine.store.begin_read() as conn:
            state, version, _ = self._engine.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, WaitingForUserState):
            raise ControlError(ControlErrorKind.NOT_RESUMABLE, "not waiting_for_user")
        if version != expected_version:
            raise ControlError(ControlErrorKind.CONFLICT, "run version changed")
        now = self._engine.clock.now()
        artifact = OperatorContinuationArtifact(
            run_id=run_id,
            repository=state.binding.repository.name_with_owner,
            pr_number=state.binding.pr_number,
            cycle_number=state.cycle_number,
            head_sha=state.binding.head_sha,
            confirmed_at=now,
        )
        evidence_ref = self._artifacts.persist_operator_continuation(
            run_id=run_id, artifact=artifact
        )
        evidence = UserContinuationEvidence(
            repository=state.binding.repository,
            pr_number=state.binding.pr_number,
            cycle_number=state.cycle_number,
            head_sha=state.binding.head_sha,
            evidence_ref=evidence_ref,
        )
        receipt = self._engine.apply_event(
            EventSubmission(
                submission_id=f"user-continue:{run_id}:{version}",
                run_id=run_id,
                expected_version=version,
                event=UserContinuationRequested(occurred_at=now, evidence=evidence),
            )
        )
        if receipt.disposition is not EventDisposition.ACCEPTED:
            raise ControlError(
                ControlErrorKind.VALIDATION,
                receipt.safe_detail or "user continuation rejected",
            )
        action = self._spawn(run_id)
        after = self._engine.get_status(run_id)
        return ResumeResult(
            run_id=run_id,
            state_kind=after.state_kind,
            transition_applied=True,
            supervisor_action=action,  # type: ignore[arg-type]
            next_action=SafeNextAction.NONE,
        )

    def _spawn(self, run_id: str) -> str:
        if self._spawner is None:
            return "spawn_failed"
        try:
            result = self._spawner(run_id)
        except Exception:  # noqa: BLE001 - never claim spawned without owned process
            return "spawn_failed"
        if result not in {"spawned", "reused", "repaired", "spawn_failed", "none"}:
            return "spawn_failed"
        return result

    def _supervisor_live(self, run_id: str) -> bool:
        meta = self._launchers.read(run_id)
        if meta is None:
            return False
        from ai_dev_loop.pr_review_v2.workers.supervisor import (
            validate_launcher_ownership_against_os,
        )

        return validate_launcher_ownership_against_os(meta, run_id=run_id)


class EngineOriginContextResolver:
    """Resolve frozen execution context from durable origin.execution_context_ref."""

    def __init__(self, engine: PrReviewEngine, store: ProtectedResultStore) -> None:
        self._engine = engine
        self._store = store

    def resolve(self, run_id: str) -> tuple[ExecutionContextArtifact, ArtifactRef]:
        with self._engine.store.begin_read() as conn:
            state, _, _ = self._engine.store.load_validated_snapshot(conn, run_id)
        ref = state.origin.execution_context_ref
        return self._store.read_execution_context(run_id=run_id, ref=ref), ref


__all__ = [
    "ControlPlaneService",
    "EngineOriginContextResolver",
    "SupervisorSpawner",
    "map_engine_next_action",
]
