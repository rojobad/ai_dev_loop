"""Process-free scheduler Codex review retry authorization."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    AppModel,
    SafeNextAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    awaiting_codex_review_safe_next_action,
    waiting_codex_review_retry_safe_next_action,
)
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    ReviewCheckpointVerificationError,
    verify_retry_state_repository,
)
from ai_dev_loop.scheduler.application.review_recovery import (
    analyze_blocked_review_recovery,
    materialize_review_recovery_successor,
)
from ai_dev_loop.scheduler.domain.events import (
    CodexCapacityRetryAuthorizedEvent,
    CodexReviewRetryRequestedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_codex_capacity_retry_authorized,
    apply_codex_review_retry_requested,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    BlockedState,
    WaitingCodexCapacityState,
    WaitingCodexReviewRetryState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


class ReviewRetryResult(AppModel):
    run_id: str
    source_run_id: str | None = None
    state_kind: str
    changed: bool
    idempotent_replay: bool
    recovery_successor: bool
    safe_next_action: SafeNextAction


class ReviewRetryService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._run_id_factory = run_id_factory

    def retry(self, run_id: str) -> ReviewRetryResult:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            state, _, _ = self.store.load_validated_snapshot(conn, run_id)

        if isinstance(state, BlockedState):
            return self._recover_blocked_source(run_id, now=now)
        if isinstance(state, WaitingCodexReviewRetryState):
            return self._retry_waiting_state(state, now=now)
        if isinstance(state, WaitingCodexCapacityState):
            return self._retry_capacity_wait_state(state, now=now)
        if isinstance(state, AwaitingCodexReviewState):
            replay = self._replay_awaiting_codex_review(state)
            if replay is not None:
                return replay
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            f"run state {state.kind} is not eligible for scheduler review retry",
        )

    def _replay_awaiting_codex_review(
        self,
        state: AwaitingCodexReviewState,
    ) -> ReviewRetryResult | None:
        review_generation = state.codex.review_retry_generation
        review_scheduled = state.codex.review_retry_scheduled_generation
        if review_generation > 0 and review_scheduled == review_generation:
            return ReviewRetryResult(
                run_id=state.run_id,
                state_kind=state.kind,
                changed=False,
                idempotent_replay=True,
                recovery_successor=state.recovery is not None,
                safe_next_action=awaiting_codex_review_safe_next_action(),
            )
        capacity_generation = state.codex.capacity_wait_generation
        capacity_scheduled = state.codex.capacity_retry_scheduled_generation
        if capacity_generation > 0 and capacity_scheduled == capacity_generation:
            return ReviewRetryResult(
                run_id=state.run_id,
                state_kind=state.kind,
                changed=False,
                idempotent_replay=True,
                recovery_successor=state.recovery is not None,
                safe_next_action=awaiting_codex_review_safe_next_action(),
            )
        return None

    def _recover_blocked_source(self, source_run_id: str, *, now: datetime) -> ReviewRetryResult:
        with self.store.begin_read() as conn:
            existing_successor = self._find_existing_recovery_successor(conn, source_run_id)
        if existing_successor is not None:
            return self._recovery_replay_result(
                source_run_id=source_run_id,
                successor_run_id=existing_successor,
                changed=False,
            )
        evidence = analyze_blocked_review_recovery(self.store, self.artifacts, source_run_id)
        with self.store.begin_read() as conn:
            existing = self.store.get_review_recovery_successor(
                conn,
                source_run_id=source_run_id,
                recovery_key=evidence.recovery_key,
            )
        if existing is not None:
            successor_run_id = str(existing["successor_run_id"])
            return self._recovery_replay_result(
                source_run_id=source_run_id,
                successor_run_id=successor_run_id,
                changed=False,
            )
        successor_run_id = materialize_review_recovery_successor(
            self.store,
            self.artifacts,
            evidence,
            source_run_id=source_run_id,
            now=now,
            run_id_factory=self._run_id_factory,
            event_id_factory=self._event_id_factory,
        )
        return self._recovery_replay_result(
            source_run_id=source_run_id,
            successor_run_id=successor_run_id,
            changed=True,
        )

    def _find_existing_recovery_successor(
        self,
        conn: sqlite3.Connection,
        source_run_id: str,
    ) -> str | None:
        row = self.store.get_latest_review_recovery_successor(
            conn,
            source_run_id=source_run_id,
        )
        if row is None:
            return None
        return str(row["successor_run_id"])

    def _recovery_replay_result(
        self,
        *,
        source_run_id: str,
        successor_run_id: str,
        changed: bool,
    ) -> ReviewRetryResult:
        with self.store.begin_read() as conn:
            successor, _, _ = self.store.load_validated_snapshot(conn, successor_run_id)
        if isinstance(successor, AwaitingCodexReviewState):
            replay = self._replay_awaiting_codex_review(successor)
            if replay is not None:
                return replay.model_copy(
                    update={
                        "run_id": successor_run_id,
                        "source_run_id": source_run_id,
                        "changed": changed,
                        "idempotent_replay": not changed,
                        "recovery_successor": True,
                    }
                )
        return ReviewRetryResult(
            run_id=successor_run_id,
            source_run_id=source_run_id,
            state_kind=successor.kind,
            changed=changed,
            idempotent_replay=not changed,
            recovery_successor=True,
            safe_next_action=awaiting_codex_review_safe_next_action(),
        )

    def _retry_capacity_wait_state(
        self,
        state: WaitingCodexCapacityState,
        *,
        now: datetime,
    ) -> ReviewRetryResult:
        try:
            verify_retry_state_repository(state, artifacts=self.artifacts)
        except ReviewCheckpointVerificationError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                str(exc),
            ) from exc

        with self.store.begin_read() as conn:
            if self.store.get_nonterminal_attempt_for_run(conn, state.run_id) is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "cannot authorize review retry while a Codex attempt is active",
                )

        wait_generation = state.codex.capacity_wait_generation
        with self.store.begin_read() as conn:
            existing_row = self.store.get_capacity_retry_generation_row(
                conn,
                run_id=state.run_id,
                capacity_wait_generation=wait_generation,
            )
        if existing_row is not None:
            with self.store.begin_read() as conn:
                current, _, _ = self.store.load_validated_snapshot(conn, state.run_id)
            action = (
                awaiting_codex_review_safe_next_action()
                if current.kind == "awaiting_codex_review"
                else waiting_codex_review_retry_safe_next_action(state.run_id)
            )
            return ReviewRetryResult(
                run_id=state.run_id,
                state_kind=current.kind,
                changed=False,
                idempotent_replay=True,
                recovery_successor=False,
                safe_next_action=action,
            )

        event = CodexCapacityRetryAuthorizedEvent(
            run_id=state.run_id,
            review_iteration=state.codex.review_iteration,
            capacity_wait_generation=wait_generation,
            idempotent_replay=False,
        )
        with self.store.begin_immediate() as conn:
            current, version, _ = self.store.load_validated_snapshot(conn, state.run_id)
            if not isinstance(current, WaitingCodexCapacityState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "capacity wait state changed concurrently",
                )
            new_state = apply_codex_capacity_retry_authorized(
                current,
                event,
                now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            )
            inserted = self.store.insert_capacity_retry_generation(
                conn,
                run_id=state.run_id,
                capacity_wait_generation=wait_generation,
                now=now,
            )
            if not inserted:
                row = self.store.get_capacity_retry_generation_row(
                    conn,
                    run_id=state.run_id,
                    capacity_wait_generation=wait_generation,
                )
                if row is not None:
                    current, _, _ = self.store.load_validated_snapshot(conn, state.run_id)
                    return ReviewRetryResult(
                        run_id=state.run_id,
                        state_kind=current.kind,
                        changed=False,
                        idempotent_replay=True,
                        recovery_successor=False,
                        safe_next_action=awaiting_codex_review_safe_next_action(),
                    )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, state.run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=state.run_id,
                sequence=sequence,
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=state.run_id,
                expected_version=version,
                new_state=new_state,
                now=now,
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "capacity retry authorization lost a concurrent update",
                )
        return ReviewRetryResult(
            run_id=state.run_id,
            state_kind="awaiting_codex_review",
            changed=True,
            idempotent_replay=False,
            recovery_successor=False,
            safe_next_action=awaiting_codex_review_safe_next_action(),
        )

    def _retry_waiting_state(
        self,
        state: WaitingCodexReviewRetryState,
        *,
        now: datetime,
    ) -> ReviewRetryResult:
        try:
            verify_retry_state_repository(state, artifacts=self.artifacts)
        except ReviewCheckpointVerificationError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                str(exc),
            ) from exc

        generation = state.codex.review_retry_generation
        with self.store.begin_read() as conn:
            existing_row = self.store.get_review_retry_generation_row(
                conn,
                run_id=state.run_id,
                failure_generation=generation,
            )
        if existing_row is not None:
            with self.store.begin_read() as conn:
                current, _, _ = self.store.load_validated_snapshot(conn, state.run_id)
            current_kind = current.kind
            action = (
                awaiting_codex_review_safe_next_action()
                if current_kind == "awaiting_codex_review"
                else waiting_codex_review_retry_safe_next_action(state.run_id)
            )
            return ReviewRetryResult(
                run_id=state.run_id,
                state_kind=current_kind,
                changed=False,
                idempotent_replay=True,
                recovery_successor=False,
                safe_next_action=action,
            )

        with self.store.begin_read() as conn:
            if self.store.get_nonterminal_attempt_for_run(conn, state.run_id) is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "cannot authorize review retry while a Codex attempt is active",
                )

        event = CodexReviewRetryRequestedEvent(
            run_id=state.run_id,
            review_iteration=state.codex.review_iteration,
            retry_generation=generation,
            idempotent_replay=False,
        )
        with self.store.begin_immediate() as conn:
            current, version, _ = self.store.load_validated_snapshot(conn, state.run_id)
            if not isinstance(current, WaitingCodexReviewRetryState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "review retry state changed concurrently",
                )
            new_state = apply_codex_review_retry_requested(
                current, event, now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            )
            inserted = self.store.insert_review_retry_generation(
                conn,
                run_id=state.run_id,
                failure_generation=generation,
                now=now,
            )
            if not inserted:
                row = self.store.get_review_retry_generation_row(
                    conn,
                    run_id=state.run_id,
                    failure_generation=generation,
                )
                if row is not None:
                    current, _, _ = self.store.load_validated_snapshot(conn, state.run_id)
                    return ReviewRetryResult(
                        run_id=state.run_id,
                        state_kind=current.kind,
                        changed=False,
                        idempotent_replay=True,
                        recovery_successor=False,
                        safe_next_action=awaiting_codex_review_safe_next_action(),
                    )
            event_id = self._event_id_factory()
            sequence = self.store.next_event_sequence(conn, state.run_id)
            self.store.append_event(
                conn,
                event_id=event_id,
                run_id=state.run_id,
                sequence=sequence,
                event=event,
                now=now,
            )
            if not self.store.compare_and_swap_state(
                conn,
                run_id=state.run_id,
                expected_version=version,
                new_state=new_state,
                now=now,
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "review retry authorization lost a concurrent update",
                )
        return ReviewRetryResult(
            run_id=state.run_id,
            state_kind="awaiting_codex_review",
            changed=True,
            idempotent_replay=False,
            recovery_successor=False,
            safe_next_action=awaiting_codex_review_safe_next_action(),
        )


def default_review_retry_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> ReviewRetryService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return ReviewRetryService(store, artifacts)


def scheduler_review_retry(run_id: str, *, db_path: Path | None = None) -> ReviewRetryResult:
    service = default_review_retry_service(db_path=db_path)
    return service.retry(run_id)
