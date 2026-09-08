"""Scheduler explicit start authorization service."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.integrations.codex.session_runtime import require_codex_session_id
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    StartResult,
    authorized_safe_next_action,
    queued_safe_next_action,
)
from ai_dev_loop.scheduler.domain.events import RunAuthorizedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_authorized
from ai_dev_loop.scheduler.domain.state import AuthorizedState, SubmittedState
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


class StartService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.store = store
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")

    def start(self, run_id: str, controller_session_id: str) -> StartResult:
        controller_id = require_codex_session_id(controller_session_id)
        now = self._now_factory()

        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if state.context.controller.controller_session_id != controller_id:
                return StartResult(
                    run_id=run_id,
                    state_kind=state.kind,
                    changed=False,
                    idempotent_replay=False,
                    safe_next_action=queued_safe_next_action(run_id)
                    if state.kind == "queued"
                    else authorized_safe_next_action(),
                )
            reservation = self.store.get_reservation_for_run(conn, run_id)
            if reservation is None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "active repository reservation missing for run",
                )
            if str(reservation["run_id"]) != run_id:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "repository reservation owner mismatch",
                )
            if isinstance(state, AuthorizedState):
                return StartResult(
                    run_id=run_id,
                    state_kind=state.kind,
                    changed=False,
                    idempotent_replay=True,
                    safe_next_action=authorized_safe_next_action(),
                )
            if not isinstance(state, SubmittedState) or state.kind != "queued":
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    f"run is not queued for authorization (state={state.kind})",
                )

            event = RunAuthorizedEvent(
                run_id=run_id,
                controller_session_id=controller_id,
                idempotent_replay=False,
            )
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            new_state = apply_run_authorized(state, event, now_text=now_text)
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
                    "authorization lost a concurrent state update",
                )
            dispatch_id = self._dispatch_id_factory()
            self.store.insert_fake_agent_self_test_effect(
                conn,
                dispatch_id=dispatch_id,
                source_event_id=event_id,
                run_id=run_id,
                available_at=now,
                claimed_run_version=new_state.version,
                now=now,
            )
            return StartResult(
                run_id=run_id,
                state_kind=new_state.kind,
                changed=True,
                idempotent_replay=False,
                safe_next_action=authorized_safe_next_action(),
            )


def default_start_service(*, db_path: Path | None = None) -> StartService:
    return StartService(SqliteSchedulerStore(db_path or default_engine_db_path()))


def start_run(
    run_id: str, controller_session_id: str, *, db_path: Path | None = None
) -> StartResult:
    return default_start_service(db_path=db_path).start(run_id, controller_session_id)
