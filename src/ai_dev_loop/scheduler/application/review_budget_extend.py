"""Process-free scheduler review budget extension authorization."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.iterations import correction_execution_envelope_path
from ai_dev_loop.scheduler.application.contracts import (
    AppModel,
    SafeNextAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    max_iterations_reached_safe_next_action,
    waiting_for_cursor_fix_safe_next_action,
)
from ai_dev_loop.scheduler.application.review_budget import (
    effective_review_ceiling_for_state,
    find_extension_event_for_target,
    load_review_budget_extensions,
    validate_extension_target_for_exhausted_state,
)
from ai_dev_loop.scheduler.application.review_budget_artifacts import (
    load_exhausted_review_artifacts,
)
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    ReviewCheckpointVerificationError,
    verify_review_retry_repository_checkpoint,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import ReviewBudgetExtendedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_review_budget_extended
from ai_dev_loop.scheduler.domain.state import (
    MaxIterationsReachedState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


class ReviewBudgetExtendResult(AppModel):
    run_id: str
    state_kind: str
    changed: bool
    idempotent_replay: bool
    previous_effective_total: int
    new_effective_total: int
    safe_next_action: SafeNextAction


class ReviewBudgetExtendService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def extend(self, run_id: str, *, target_total: int) -> ReviewBudgetExtendResult:
        if target_total < 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "max-review-iterations must be a positive integer",
            )
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)
            extension_events = load_review_budget_extensions(self.store, conn, run_id)
        effective_total = effective_review_ceiling_for_state(state, extension_events)
        if target_total < effective_total:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "requested review ceiling is lower than the current effective total",
            )
        if target_total == effective_total:
            replay = self._replay_exact_target(
                run_id,
                state=state,
                extension_events=extension_events,
                target_total=target_total,
            )
            if replay is not None:
                return replay
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "requested review ceiling equals the current effective total",
            )
        if not isinstance(state, MaxIterationsReachedState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"run state {state.kind} is not eligible for review budget extension",
            )
        effective_total = validate_extension_target_for_exhausted_state(
            state,
            target_total=target_total,
            extension_events=extension_events,
        )

        exhausted = load_exhausted_review_artifacts(self.store, self.artifacts, state)
        try:
            verify_review_retry_repository_checkpoint(
                context=state.context,
                run_id=run_id,
                artifacts=self.artifacts,
                checkpoint=state.checkpoint,
                cursor=state.cursor,
                plan_sha256=state.context.plan_prompt.plan_sha256,
                prompt_sha256=state.context.plan_prompt.prompt_sha256,
            )
        except ReviewCheckpointVerificationError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                str(exc),
            ) from exc

        with self.store.begin_read() as conn:
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "cannot extend review budget while an attempt is active",
                )
            worktree_key = state.context.repository.worktree_key
            active = self.store.get_active_reservation(conn, worktree_key)
            if active is not None and str(active["run_id"]) != run_id:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree reservation is held by another run",
                )

        now = self._now_factory()
        event = ReviewBudgetExtendedEvent(
            run_id=run_id,
            review_iteration=exhausted.review_iteration,
            previous_effective_total=effective_total,
            new_effective_total=target_total,
            review_result_path=exhausted.review_result_path,
            review_result_sha256=exhausted.review_result_sha256,
            fix_prompt_path=exhausted.fix_prompt_path,
            fix_prompt_sha256=exhausted.fix_prompt_sha256,
            correction_envelope_path=exhausted.correction_envelope_path,
            correction_envelope_sha256=exhausted.correction_envelope_sha256,
        )
        with self.store.begin_immediate() as conn:
            current, version, _ = self.store.load_validated_snapshot(conn, run_id)
            current_extensions = load_review_budget_extensions(self.store, conn, run_id)
            current_effective = effective_review_ceiling_for_state(current, current_extensions)
            if target_total <= current_effective:
                replay = self._replay_exact_target(
                    run_id,
                    state=current,
                    extension_events=current_extensions,
                    target_total=target_total,
                )
                if replay is not None:
                    return replay
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "review budget extension lost a concurrent update",
                )
            if not isinstance(current, MaxIterationsReachedState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "review budget extension state changed concurrently",
                )
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "cannot extend review budget while an attempt is active",
                )
            if not self.store.reacquire_released_reservation_for_run(
                conn,
                run_id=run_id,
                worktree_key=current.context.repository.worktree_key,
                repository_root=current.context.repository.root,
                now=now,
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "repository worktree reservation could not be reacquired",
                )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_review_budget_extended(current, event, now_text=now_text)
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
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "review budget extension lost a concurrent state update",
                )
            self.store.insert_effect(
                conn,
                dispatch_id=self._dispatch_id_factory(),
                source_event_id=event_id,
                run_id=run_id,
                effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                effect_payload={
                    "iteration": exhausted.review_iteration + 1,
                    "prompt_path": correction_execution_envelope_path(exhausted.review_iteration),
                },
                available_at=now,
                claimed_run_version=new_state.version,
                now=now,
            )
        return ReviewBudgetExtendResult(
            run_id=run_id,
            state_kind="waiting_for_cursor_fix",
            changed=True,
            idempotent_replay=False,
            previous_effective_total=effective_total,
            new_effective_total=target_total,
            safe_next_action=waiting_for_cursor_fix_safe_next_action(),
        )

    def _replay_exact_target(
        self,
        run_id: str,
        *,
        state: object,
        extension_events: tuple[ReviewBudgetExtendedEvent, ...],
        target_total: int,
    ) -> ReviewBudgetExtendResult | None:
        existing = find_extension_event_for_target(extension_events, target_total)
        if existing is None:
            return None
        if isinstance(state, WaitingForCursorFixState):
            return ReviewBudgetExtendResult(
                run_id=run_id,
                state_kind=state.kind,
                changed=False,
                idempotent_replay=True,
                previous_effective_total=existing.previous_effective_total,
                new_effective_total=existing.new_effective_total,
                safe_next_action=waiting_for_cursor_fix_safe_next_action(),
            )
        if isinstance(state, MaxIterationsReachedState):
            return ReviewBudgetExtendResult(
                run_id=run_id,
                state_kind=state.kind,
                changed=False,
                idempotent_replay=True,
                previous_effective_total=existing.previous_effective_total,
                new_effective_total=existing.new_effective_total,
                safe_next_action=max_iterations_reached_safe_next_action(run_id),
            )
        return None


def default_review_budget_extend_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> ReviewBudgetExtendService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return ReviewBudgetExtendService(store, artifacts)


def scheduler_extend_review_budget(
    run_id: str,
    *,
    target_total: int,
    db_path: Path | None = None,
) -> ReviewBudgetExtendResult:
    service = default_review_budget_extend_service(db_path=db_path)
    return service.extend(run_id, target_total=target_total)
