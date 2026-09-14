"""Scheduler sequence start authorization and first-run materialization."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextAction,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    SequenceStartResult,
    active_sequence_safe_next_action,
    authorized_safe_next_action,
)
from ai_dev_loop.scheduler.application.sequence_materializer import (
    SequenceRunMaterializer,
    sequence_run_idempotency_key,
)
from ai_dev_loop.scheduler.application.submission import (
    _ensure_worktree_available_for_fresh_submission,
)
from ai_dev_loop.scheduler.domain.events import RunAuthorizedEvent, RunSubmittedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_authorized
from ai_dev_loop.scheduler.domain.sequence import (
    ACTIVE_SEQUENCE_STATE_KIND,
    ActiveSequenceState,
    MaterializedSequenceEntry,
    PreparedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SchedulerState,
    SubmittedState,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


@dataclass(frozen=True)
class SequenceStartOptions:
    sequence_id: str
    db_path: Path | None = None
    artifact_root: Path | None = None


class SequenceStartService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        materializer: SequenceRunMaterializer | None = None,
        start_step_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._materializer = materializer or SequenceRunMaterializer(artifacts)
        self._start_step_hook = start_step_hook

    def _step(self, name: str) -> None:
        if self._start_step_hook is not None:
            self._start_step_hook(name)

    def start(self, sequence_id: str) -> SequenceStartResult:
        with self.store.begin_immediate() as conn:
            self.store.require_sequence_schema(conn)
            state = self._load_sequence(conn, sequence_id)
            if isinstance(state, ActiveSequenceState):
                return self._result_from_active(conn, state, idempotent_replay=True, changed=False)
            if not isinstance(state, PreparedSequenceState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CORRUPTION,
                    "sequence state has unexpected type",
                )
            entry = state.definition.entries[0]
            run_id = entry.planned_run_id
            existing_run = conn.execute(
                "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing_run is not None:
                active = self._try_recover_active_from_existing_run(conn, state, run_id)
                if active is not None:
                    return self._result_from_active(
                        conn,
                        active,
                        idempotent_replay=True,
                        changed=False,
                    )
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "planned first-run ID already exists outside this sequence start",
                )
            _ensure_worktree_available_for_fresh_submission(
                self.store,
                conn,
                state.definition.repository.worktree_key,
            )
            prepared_version = state.version

        try:
            self.artifacts.verify_prepared_sequence_artifacts(
                sequence_id,
                state.definition,
            )
        except ProtectedArtifactError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "prepared sequence artifacts failed integrity verification",
            ) from exc

        self._step("before_materialize")
        try:
            with self.artifacts.materialization_section(run_id, blocking=True):
                with self.store.begin_immediate() as conn:
                    replay = self._recheck_locked_materialization_start(
                        conn,
                        sequence_id=sequence_id,
                        run_id=run_id,
                    )
                    if replay is not None:
                        return replay
                context, entry_hash = self._materializer.materialize_first_entry(
                    sequence_id=sequence_id,
                    definition=state.definition,
                    entry=entry,
                )
                self._step("artifacts_materialized")

                now = self._now_factory()
                now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                idempotency_key = sequence_run_idempotency_key(context)
                submitted_state = SubmittedState(
                    run_id=run_id,
                    version=1,
                    submitted_at=now_text,
                    updated_at=now_text,
                    idempotency_key=idempotency_key,
                    context=context,
                )
                submitted_event = RunSubmittedEvent(
                    run_id=run_id,
                    idempotency_key=idempotency_key,
                    worktree_key=context.repository.worktree_key,
                    reused_existing=False,
                )
                authorized_event = RunAuthorizedEvent(
                    run_id=run_id,
                    controller_session_id=context.controller.controller_session_id,
                    idempotent_replay=False,
                )
                authorized_state = apply_run_authorized(
                    submitted_state, authorized_event, now_text=now_text
                )
                active_state = ActiveSequenceState(
                    schema_version=1,
                    sequence_id=sequence_id,
                    version=prepared_version + 1,
                    prepared_at=state.prepared_at,
                    updated_at=now_text,
                    started_at=now_text,
                    idempotency_key=state.idempotency_key,
                    definition=state.definition,
                    current_ordinal=1,
                    current_run_id=run_id,
                    materialized_entries=(
                        MaterializedSequenceEntry(
                            ordinal=1,
                            run_id=run_id,
                            entry_hash=entry_hash,
                            materialized_at=now_text,
                        ),
                    ),
                )

                with self.store.begin_immediate() as conn:
                    self.store.require_sequence_schema(conn)
                    replay = self._recheck_locked_materialization_start(
                        conn,
                        sequence_id=sequence_id,
                        run_id=run_id,
                    )
                    if replay is not None:
                        return replay
                    current = self._load_sequence(conn, sequence_id)
                    if not isinstance(current, PreparedSequenceState):
                        raise SchedulerEngineError(
                            SchedulerEngineErrorKind.CORRUPTION,
                            "sequence state changed unexpectedly during materialization",
                        )
                    if current.version != prepared_version:
                        raise SchedulerEngineError(
                            SchedulerEngineErrorKind.CONFLICT,
                            "sequence start lost a concurrent sequence update",
                        )
                    _ensure_worktree_available_for_fresh_submission(
                        self.store,
                        conn,
                        current.definition.repository.worktree_key,
                    )
                    self._step("before_db_insert")
                    self.store.insert_materialized_sequence_run(
                        conn,
                        run_id=run_id,
                        submitted_state=submitted_state,
                        authorized_state=authorized_state,
                        submitted_event_id=self._event_id_factory(),
                        submitted_event=submitted_event,
                        authorized_event_id=self._event_id_factory(),
                        authorized_event=authorized_event,
                        now=now,
                    )
                    if not self.store.compare_and_swap_sequence_state(
                        conn,
                        sequence_id=sequence_id,
                        expected_version=prepared_version,
                        new_state=active_state,
                        now=now,
                    ):
                        raise SchedulerEngineError(
                            SchedulerEngineErrorKind.CONFLICT,
                            "sequence start lost a concurrent sequence update",
                        )
                    self._step("after_db_insert")
                    return self._result_from_active(
                        conn, active_state, idempotent_replay=False, changed=True
                    )
        except ProtectedArtifactError as exc:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence run artifact materialization failed",
            ) from exc

    def _recheck_locked_materialization_start(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_id: str,
        run_id: str,
    ) -> SequenceStartResult | None:
        current = self._load_sequence(conn, sequence_id)
        if isinstance(current, ActiveSequenceState):
            return self._result_from_active(
                conn,
                current,
                idempotent_replay=True,
                changed=False,
            )
        existing_run = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if existing_run is None:
            return None
        if not isinstance(current, PreparedSequenceState):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CORRUPTION,
                "sequence state changed unexpectedly during materialization",
            )
        active = self._try_recover_active_from_existing_run(conn, current, run_id)
        if active is not None:
            return self._result_from_active(
                conn,
                active,
                idempotent_replay=True,
                changed=False,
            )
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "planned first-run ID already exists outside this sequence start",
        )

    def _load_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_id: str,
    ) -> PreparedSequenceState | ActiveSequenceState:
        try:
            state = self.store.load_validated_sequence_state(conn, sequence_id)
        except SchedulerEngineError as exc:
            if exc.kind is SchedulerEngineErrorKind.NOT_FOUND:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.NOT_FOUND,
                    f"prepared sequence not found: {sequence_id}",
                ) from exc
            raise
        if isinstance(state, (PreparedSequenceState, ActiveSequenceState)):
            return state
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CORRUPTION,
            "sequence state has unexpected type",
        )

    def _try_recover_active_from_existing_run(
        self,
        conn: sqlite3.Connection,
        prepared_state: PreparedSequenceState,
        run_id: str,
    ) -> ActiveSequenceState | None:
        try:
            run_state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        except SchedulerEngineError:
            return None
        binding = run_state.context.sequence
        if binding is None or binding.sequence_id != prepared_state.sequence_id:
            return None
        if binding.ordinal != 1:
            return None
        entry = prepared_state.definition.entries[0]
        if binding.entry_hash != self.store.dump_sequence_entry(entry)[1]:
            return None
        now_text = run_state.updated_at
        active_state = ActiveSequenceState(
            schema_version=1,
            sequence_id=prepared_state.sequence_id,
            version=prepared_state.version + 1,
            prepared_at=prepared_state.prepared_at,
            updated_at=now_text,
            started_at=now_text,
            idempotency_key=prepared_state.idempotency_key,
            definition=prepared_state.definition,
            current_ordinal=1,
            current_run_id=run_id,
            materialized_entries=(
                MaterializedSequenceEntry(
                    ordinal=1,
                    run_id=run_id,
                    entry_hash=binding.entry_hash,
                    materialized_at=now_text,
                ),
            ),
        )
        now = self._now_factory()
        if not self.store.compare_and_swap_sequence_state(
            conn,
            sequence_id=prepared_state.sequence_id,
            expected_version=prepared_state.version,
            new_state=active_state,
            now=now,
        ):
            reloaded = self._load_sequence(conn, prepared_state.sequence_id)
            if isinstance(reloaded, ActiveSequenceState):
                return reloaded
            return None
        return active_state

    def _result_from_active(
        self,
        conn: sqlite3.Connection,
        state: ActiveSequenceState,
        *,
        idempotent_replay: bool,
        changed: bool,
    ) -> SequenceStartResult:
        run_state, _, _ = self.store.load_validated_snapshot(conn, state.current_run_id)
        safe_action = self._safe_action_for_active_sequence(conn, state, run_state)
        current_phase = state.definition.entries[state.current_ordinal - 1].phase_name
        return SequenceStartResult(
            sequence_id=state.sequence_id,
            run_id=state.current_run_id,
            sequence_state_kind=ACTIVE_SEQUENCE_STATE_KIND,
            run_state_kind=run_state.kind,
            current_ordinal=state.current_ordinal,
            entry_count=len(state.definition.entries),
            current_phase_name=current_phase,
            changed=changed,
            idempotent_replay=idempotent_replay,
            safe_next_action=safe_action,
        )

    def _safe_action_for_active_sequence(
        self,
        conn: sqlite3.Connection,
        sequence_state: ActiveSequenceState,
        run_state: SchedulerState,
    ) -> SafeNextAction:
        from ai_dev_loop.scheduler.application.safe_actions import (
            safe_next_action_for_scheduler_state,
        )

        if run_state.kind == "authorized":
            return authorized_safe_next_action()
        return active_sequence_safe_next_action(
            sequence_id=sequence_state.sequence_id,
            run_safe_action=safe_next_action_for_scheduler_state(self.store, conn, run_state),
        )


def default_sequence_start_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> SequenceStartService:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return SequenceStartService(store, artifacts)


def start_sequence(sequence_id: str, *, db_path: Path | None = None) -> SequenceStartResult:
    return default_sequence_start_service(db_path=db_path).start(sequence_id)
