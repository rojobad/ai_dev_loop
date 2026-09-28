"""Bounded operational retries of a terminated Cursor turn, in the same run."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    AppModel,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    load_authenticated_cursor_outcome,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_ID,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.events import CursorTimeoutRetryEvent
from ai_dev_loop.scheduler.domain.state import (
    CursorReadyState,
    WaitingForCursorFixState,
    WaitingUsageLimitState,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now

TIMEOUT_RETRY_DELAY = timedelta(minutes=30)
MAX_AUTOMATIC_TIMEOUT_RETRIES = 3
CursorTurnState = CursorReadyState | WaitingForCursorFixState | WaitingUsageLimitState
CURSOR_TURN_STATES = (CursorReadyState, WaitingForCursorFixState, WaitingUsageLimitState)


class CursorRetryResult(AppModel):
    run_id: str
    changed: bool
    available_at: str | None
    automatic_retries: int


def invalid(message: str) -> SchedulerEngineError:
    return SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, message)


def timeout_binding(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
) -> dict[str, object]:
    """Authenticate both the completed timeout and its exact original invocation."""
    try:
        return _timeout_binding(store, artifacts, conn, run_id, attempt_id)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise invalid("Cursor timeout evidence is missing or invalid") from exc


def _timeout_binding(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    run_id: str,
    attempt_id: str,
) -> dict[str, object]:
    attempt = store.get_attempt_by_id(conn, attempt_id)
    if (
        attempt is None
        or attempt["run_id"] != run_id
        or attempt["status"] not in {"completed", "failed"}
        or not attempt["completion_envelope_sha256"]
    ):
        raise invalid("Cursor retry requires a confirmed terminal attempt")
    dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
    if dispatch is None or dispatch["effect_kind"] != RUN_CURSOR_TURN_EFFECT_KIND:
        raise invalid("Only Cursor turn timeouts can be retried")
    root = artifacts.run_root(run_id)
    binding = verify_cursor_invocation_evidence(
        root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=str(attempt["dispatch_id"]),
        unit_identity=str(attempt["unit_identity"]),
        launch_nonce=str(attempt["launch_nonce"]),
        launch_intent_sha256=str(attempt["launch_intent_sha256"]),
        effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    outcome = load_authenticated_cursor_outcome(
        root,
        attempt_id=attempt_id,
        unit_identity=str(attempt["unit_identity"]),
        result_rel=str(attempt["result_artifact_path"]),
        stdout_rel=str(attempt["stdout_artifact_path"]),
        stderr_rel=str(attempt["stderr_artifact_path"]),
        observed_exit_code=int(attempt["exit_code"]),
        expected_envelope_sha256=str(attempt["completion_envelope_sha256"]),
        expected_dispatch_id=str(attempt["dispatch_id"]),
        expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
    )
    if outcome.get("timed_out") is not True or attempt["exit_code"] != 124:
        raise invalid("Cursor retry requires an authenticated timeout")
    if outcome.get("iteration") != binding.get("iteration"):
        raise invalid("Timeout iteration disagrees with invocation")
    return binding


class CursorTimeoutRetryService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.now = now_factory

    def record_timeout(self, run_id: str, attempt_id: str) -> TickRunReceipt:
        now = self.now()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            attempt = self.store.get_attempt_by_id(conn, attempt_id)
            if not isinstance(state, CURSOR_TURN_STATES) or attempt is None or attempt["ingested"]:
                return TickRunReceipt(run_id=run_id, action="ingest_state_changed")
            binding = timeout_binding(self.store, self.artifacts, conn, run_id, attempt_id)
            self._check_identity(state, binding)
            self._require_idle(conn, run_id)
            count = state.cursor.timeout_automatic_retries
            automatic = count < MAX_AUTOMATIC_TIMEOUT_RETRIES
            due = now + TIMEOUT_RETRY_DELAY if automatic else None
            event = CursorTimeoutRetryEvent(
                run_id=run_id,
                attempt_id=attempt_id,
                iteration=state.cursor.iteration,
                automatic_retries=count + int(automatic),
                action="automatic" if automatic else "exhausted",
                available_at=encode_utc_instant(due) if due else None,
            )
            self._persist(conn, state, version, event, now, schedule=automatic)
            self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return TickRunReceipt(
            run_id=run_id,
            action="cursor_timeout_retry_scheduled"
            if automatic
            else "cursor_timeout_retry_exhausted",
        )

    def retry(self, run_id: str) -> CursorRetryResult:
        now = self.now()
        with self.store.begin_immediate() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            self._require_idle(conn, run_id)
            if not isinstance(state, CURSOR_TURN_STATES) or not state.cursor.timeout_attempt_id:
                raise invalid("Run has no retryable Cursor timeout")
            binding = timeout_binding(
                self.store, self.artifacts, conn, run_id, state.cursor.timeout_attempt_id
            )
            self._check_identity(state, binding)
            pending = conn.execute(
                "SELECT * FROM scheduler_effects WHERE run_id=? AND status='pending' "
                "AND effect_kind=?",
                (run_id, RUN_CURSOR_TURN_EFFECT_KIND),
            ).fetchall()
            if len(pending) > 1:
                raise invalid("Multiple pending Cursor turns")
            if pending and str(pending[0]["available_at"]) <= encode_utc_instant(now):
                return CursorRetryResult(
                    run_id=run_id,
                    changed=False,
                    available_at=str(pending[0]["available_at"]),
                    automatic_retries=state.cursor.timeout_automatic_retries,
                )
            event = CursorTimeoutRetryEvent(
                run_id=run_id,
                attempt_id=state.cursor.timeout_attempt_id,
                iteration=state.cursor.iteration,
                automatic_retries=state.cursor.timeout_automatic_retries - int(bool(pending)),
                action="manual",
                available_at=encode_utc_instant(now),
            )
            self._persist(conn, state, version, event, now, schedule=not pending)
            if pending:
                conn.execute(
                    "UPDATE scheduler_effects SET available_at=? WHERE dispatch_id=? "
                    "AND status='pending'",
                    (encode_utc_instant(now), pending[0]["dispatch_id"]),
                )
        return CursorRetryResult(
            run_id=run_id,
            changed=True,
            available_at=event.available_at,
            automatic_retries=event.automatic_retries,
        )

    def _require_idle(self, conn: sqlite3.Connection, run_id: str) -> None:
        if self.store.get_reservation_for_run(conn, run_id) is None:
            raise invalid("Cursor retry requires the run's repository reservation")
        if (
            self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None
            or self.store.has_abort_requested_for_run(conn, run_id)
            or self.store.has_unresolved_abort_hold(conn, run_id)
            or self.store.has_checkpoint_reconciliation_hold(conn, run_id)
        ):
            raise invalid("Cursor retry requires confirmed termination and no pending abort")

    @staticmethod
    def _check_identity(state: CursorTurnState, binding: dict[str, object]) -> None:
        if (
            binding.get("iteration") != state.cursor.iteration
            or binding.get("chat_id") != state.cursor.chat_id
        ):
            raise invalid("Timeout does not belong to the current Cursor turn")

    def _persist(
        self,
        conn: sqlite3.Connection,
        state: CursorTurnState,
        version: int,
        event: CursorTimeoutRetryEvent,
        now: datetime,
        *,
        schedule: bool,
    ) -> None:
        # Same conversational checkpoint; only operational retry metadata changes.
        cursor = state.cursor.model_copy(
            update={
                "timeout_attempt_id": event.attempt_id,
                "timeout_automatic_retries": event.automatic_retries,
                "wait_until": event.available_at,
            }
        )
        if isinstance(state, WaitingUsageLimitState):
            # A provider continuation that timed out is now a timeout continuation.
            state = CursorReadyState.model_validate(state.model_dump(exclude={"kind"}))
        updated = state.model_copy(
            update={"cursor": cursor, "version": version + 1, "updated_at": encode_utc_instant(now)}
        )
        event_id = f"evt-{secrets.token_hex(16)}"
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=state.run_id,
            sequence=self.store.next_event_sequence(conn, state.run_id),
            event=event,
            now=now,
        )
        if not self.store.compare_and_swap_state(
            conn, run_id=state.run_id, expected_version=version, new_state=updated, now=now
        ):
            raise invalid("Cursor retry lost concurrent state update")
        if schedule:
            assert event.available_at is not None
            self.store.insert_effect(
                conn,
                dispatch_id=f"fx-{secrets.token_hex(16)}",
                source_event_id=event_id,
                run_id=state.run_id,
                effect_id=RUN_CURSOR_TURN_EFFECT_ID,
                effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
                effect_payload={"iteration": state.cursor.iteration},
                available_at=datetime.fromisoformat(event.available_at.replace("Z", "+00:00")),
                claimed_run_version=updated.version,
                now=now,
            )


def scheduler_cursor_retry(run_id: str, *, db_path: Path | None = None) -> CursorRetryResult:
    return CursorTimeoutRetryService(
        SqliteSchedulerStore(db_path or default_engine_db_path()),
        ProtectedArtifactStore(default_artifact_root()),
    ).retry(run_id)
