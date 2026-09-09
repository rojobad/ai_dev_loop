"""Bounded one-shot scheduler tick service."""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.attempt_backend import AgentProcessBackend
from ai_dev_loop.scheduler.application.attempt_service import (
    AttemptService,
    default_attempt_id_factory,
    default_launch_nonce_factory,
)
from ai_dev_loop.scheduler.application.codex_workflow_service import CodexWorkflowService
from ai_dev_loop.scheduler.application.contracts import (
    TickReceipt,
    TickRunReceipt,
    admitted_safe_next_action,
    authorized_safe_next_action,
)
from ai_dev_loop.scheduler.application.cursor_workflow_service import CursorWorkflowService
from ai_dev_loop.scheduler.application.git_admission import GitAdmissionPort
from ai_dev_loop.scheduler.application.scheduler_preflight import SchedulerPreflightPort
from ai_dev_loop.scheduler.application.tick_fencing import (
    admission_claim_matches,
    tick_lease_is_active,
)
from ai_dev_loop.scheduler.domain.admission_contract import ADMISSION_STATUS_ARTIFACT
from ai_dev_loop.scheduler.domain.events import (
    SyntheticEffectCompletedEvent,
    TickStaleRejectedEvent,
    TimerFiredEvent,
    WorktreeAdmissionBlockedEvent,
    WorktreeAdmittedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_synthetic_effect_completed,
    apply_worktree_admission_blocked,
    apply_worktree_admitted,
)
from ai_dev_loop.scheduler.domain.state import AdmittedState, AuthorizedState
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    MAX_BASELINE_STATUS_BYTES,
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now

DEFAULT_TICK_LEASE_SECONDS = 30


class TickService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        git_admission: GitAdmissionPort,
        *,
        now_factory: Callable[[], datetime] | None = None,
        tick_owner_factory: Callable[[], str] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        claim_id_factory: Callable[[], str] | None = None,
        dispatch_id_factory: Callable[[], str] | None = None,
        attempt_id_factory: Callable[[], str] | None = None,
        fence_id_factory: Callable[[], str] | None = None,
        launch_nonce_factory: Callable[[], str] | None = None,
        attempt_backend: AgentProcessBackend | None = None,
        preflight_port: SchedulerPreflightPort | None = None,
        lease_ttl_seconds: int = DEFAULT_TICK_LEASE_SECONDS,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.git_admission = git_admission
        self._now_factory = now_factory or (lambda: utc_now())
        self._tick_owner_factory = tick_owner_factory or (lambda: f"tick-{secrets.token_hex(8)}")
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._claim_id_factory = claim_id_factory or (lambda: f"clm-{secrets.token_hex(16)}")
        self._dispatch_id_factory = dispatch_id_factory or (lambda: f"fx-{secrets.token_hex(16)}")
        self._attempt_id_factory = attempt_id_factory or default_attempt_id_factory()
        self._fence_id_factory = fence_id_factory or (lambda: f"fnc-{secrets.token_hex(16)}")
        self._launch_nonce_factory = launch_nonce_factory or default_launch_nonce_factory()
        self._lease_ttl_seconds = lease_ttl_seconds
        self._attempt_service: AttemptService | None = None
        self._cursor_workflow: CursorWorkflowService | None = None
        self._codex_workflow: CodexWorkflowService | None = None
        if attempt_backend is not None:
            self._cursor_workflow = CursorWorkflowService(
                store,
                artifacts,
                now_factory=self._now_factory,
                event_id_factory=self._event_id_factory,
                dispatch_id_factory=self._dispatch_id_factory,
                preflight_port=preflight_port,
            )
            self._codex_workflow = CodexWorkflowService(
                store,
                artifacts,
                now_factory=self._now_factory,
                event_id_factory=self._event_id_factory,
                dispatch_id_factory=self._dispatch_id_factory,
            )
            self._attempt_service = AttemptService(
                store,
                artifacts,
                attempt_backend,
                now_factory=self._now_factory,
                event_id_factory=self._event_id_factory,
                claim_id_factory=self._claim_id_factory,
                attempt_id_factory=self._attempt_id_factory,
                fence_id_factory=self._fence_id_factory,
                launch_nonce_factory=self._launch_nonce_factory,
                cursor_workflow=self._cursor_workflow,
                codex_workflow=self._codex_workflow,
            )

    def run_once(self) -> TickReceipt:
        from ai_dev_loop.scheduler.application.cutover_cleanup import (
            cutover_cleanup_blocks_scheduler_tick,
        )

        owner_id = self._tick_owner_factory()
        now = self._now_factory()
        generation = 0
        lease_acquired = False
        run_ids: list[str] = []
        receipts: list[TickRunReceipt] = []
        if cutover_cleanup_blocks_scheduler_tick(self.store.db_path.parent):
            return TickReceipt(
                tick_owner_id=owner_id,
                lease_generation=0,
                visited_runs=0,
                run_receipts=(),
                lease_acquired=False,
                safe_next_action=authorized_safe_next_action(),
            )
        try:
            with self.store.begin_immediate() as conn:
                lease = self.store.acquire_global_tick_lease(
                    conn,
                    owner_id=owner_id,
                    now=now,
                    ttl_seconds=self._lease_ttl_seconds,
                )
                if lease is None:
                    return TickReceipt(
                        tick_owner_id=owner_id,
                        lease_generation=0,
                        visited_runs=0,
                        run_receipts=(),
                        lease_acquired=False,
                        safe_next_action=authorized_safe_next_action(),
                    )
                generation, _expires = lease
                lease_acquired = True
                self.store.reconcile_stale_tick_resources(
                    conn,
                    current_generation=generation,
                    now=now,
                )
                run_ids = self.store.list_tick_eligible_run_ids(conn)

            for run_id in run_ids:
                receipts.extend(self._visit_run(owner_id, generation, run_id))
        finally:
            if lease_acquired:
                now = self._now_factory()
                with self.store.begin_immediate() as conn:
                    self.store.release_global_tick_lease(
                        conn,
                        owner_id=owner_id,
                        generation=generation,
                        now=now,
                    )

        return TickReceipt(
            tick_owner_id=owner_id,
            lease_generation=generation,
            visited_runs=len(run_ids),
            run_receipts=tuple(receipts),
            lease_acquired=lease_acquired,
            safe_next_action=admitted_safe_next_action(),
        )

    def _visit_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> list[TickRunReceipt]:
        receipts: list[TickRunReceipt] = []
        receipts.extend(self._fire_due_timers(tick_owner_id, tick_lease_generation, run_id))
        admission_receipt = self._maybe_admit_run(tick_owner_id, tick_lease_generation, run_id)
        if admission_receipt is not None:
            receipts.append(admission_receipt)
        if self._attempt_service is not None:
            attempt_receipt = self._attempt_service.process_run(
                tick_owner_id,
                tick_lease_generation,
                run_id,
            )
            if attempt_receipt is not None:
                receipts.append(attempt_receipt)
        if self._cursor_workflow is not None:
            receipts.extend(
                self._cursor_workflow.process_run(
                    tick_owner_id,
                    tick_lease_generation,
                    run_id,
                )
            )
        if self._codex_workflow is not None:
            receipts.extend(
                self._codex_workflow.process_run(
                    tick_owner_id,
                    tick_lease_generation,
                    run_id,
                )
            )
        effect_receipt = self._maybe_run_synthetic_effect(
            tick_owner_id,
            tick_lease_generation,
            run_id,
        )
        if effect_receipt is not None:
            receipts.append(effect_receipt)
        return receipts

    def _record_stale(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        rejection_kind: str,
        safe_summary: str,
        now: datetime,
    ) -> None:
        self._append_stale_rejection(
            conn,
            run_id=run_id,
            rejection_kind=rejection_kind,
            safe_summary=safe_summary,
            now=now,
        )

    def _fire_due_timers(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> list[TickRunReceipt]:
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                self._record_stale(
                    conn,
                    run_id=run_id,
                    rejection_kind="stale_tick_lease",
                    safe_summary="tick lease expired before timer processing",
                    now=now,
                )
                return [
                    TickRunReceipt(run_id=run_id, action="timer_stale", detail="stale_tick_lease")
                ]
            timers = [
                row
                for row in self.store.list_due_timers(conn, now=now)
                if str(row["run_id"]) == run_id
            ]
            if not timers:
                return []
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            receipts: list[TickRunReceipt] = []
            for timer in timers:
                expected_version = int(timer["expected_run_version"])
                if expected_version != version:
                    self._record_stale(
                        conn,
                        run_id=run_id,
                        rejection_kind="stale_timer_run_version",
                        safe_summary="timer expected run version disagrees with current snapshot",
                        now=now,
                    )
                    receipts.append(
                        TickRunReceipt(
                            run_id=run_id,
                            action="timer_stale",
                            detail=str(timer["timer_id"]),
                        )
                    )
                    continue
                event = TimerFiredEvent(
                    run_id=run_id,
                    timer_id=str(timer["timer_id"]),
                    target_effect_id=str(timer["target_effect_id"]),
                    expected_run_version=expected_version,
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
                if not self.store.fire_timer(
                    conn,
                    timer_id=str(timer["timer_id"]),
                    fired_event_id=event_id,
                    now=now,
                ):
                    receipts.append(
                        TickRunReceipt(
                            run_id=run_id,
                            action="timer_stale",
                            detail=str(timer["timer_id"]),
                        )
                    )
                    continue
                receipts.append(
                    TickRunReceipt(
                        run_id=run_id,
                        action="timer_fired",
                        detail=str(timer["timer_id"]),
                    )
                )
            return receipts

    def _redact_artifact_failure_summary(self, exc: BaseException) -> str:
        if isinstance(exc, ProtectedArtifactError):
            return str(exc)
        return "admission artifact write failed"

    def _redact_artifact_access_failure_summary(self, exc: BaseException) -> str:
        if isinstance(exc, ProtectedArtifactError):
            return str(exc)
        return "admission artifact boundary access failed"

    def _inspect_admission_artifact_boundary(self, run_id: str) -> tuple[str, str | None]:
        try:
            artifact_path = self.artifacts.run_root(run_id) / ADMISSION_STATUS_ARTIFACT
        except (OSError, PermissionError) as exc:
            return "inaccessible", self._redact_artifact_access_failure_summary(exc)
        try:
            if artifact_path.is_file():
                return "present", None
        except (OSError, PermissionError) as exc:
            return "inaccessible", self._redact_artifact_access_failure_summary(exc)
        return "absent", None

    def _maybe_admit_run(
        self,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id: str,
    ) -> TickRunReceipt | None:
        now = self._now_factory()
        claim_id = self._claim_id_factory()
        repository_root = ""
        require_clean = True
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="admission_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AuthorizedState):
                return None
            boundary_status, access_failure_summary = self._inspect_admission_artifact_boundary(
                run_id
            )
            if boundary_status == "present":
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=version,
                    failure_kind="admission_artifact_present",
                    failure_summary=(
                        "protected admission artifact already exists; refusing to rerun Git admission"
                    ),
                    now=now,
                    acquire_claim=False,
                )
            if boundary_status == "inaccessible":
                assert access_failure_summary is not None
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=version,
                    failure_kind="admission_artifact_access_failed",
                    failure_summary=access_failure_summary,
                    now=now,
                    acquire_claim=False,
                )
            if self.store.has_stale_admission_attempt(conn, run_id):
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=version,
                    failure_kind="admission_attempt_uncertain",
                    failure_summary=(
                        "prior admission attempt left uncertain evidence; refusing to rerun Git admission"
                    ),
                    now=now,
                    acquire_claim=False,
                )
            if not self.store.acquire_admission_tick_claim(
                conn,
                claim_id=claim_id,
                run_id=run_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=version,
                now=now,
            ):
                return TickRunReceipt(
                    run_id=run_id,
                    action="admission_claim_busy",
                )
            repository_root = str(state.context.repository.root)
            require_clean = state.context.workflow.require_clean_worktree

        try:
            admission = self.git_admission.admit(
                repository_root=repository_root,
                require_clean_worktree=require_clean,
            )
        except Exception:
            with self.store.begin_immediate() as conn:
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=None,
                    failure_kind="admission_port_failed",
                    failure_summary="Git admission port failed before a durable outcome",
                    now=self._now_factory(),
                    acquire_claim=True,
                )

        now = self._now_factory()
        if not admission.ok:
            with self.store.begin_immediate() as conn:
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=None,
                    failure_kind=admission.failure_kind or "admission_failed",
                    failure_summary=admission.failure_summary or "worktree admission failed",
                    now=now,
                    acquire_claim=True,
                )

        assert admission.artifact_text is not None
        assert admission.evidence is not None
        if str(admission.evidence.resolved_root) != repository_root:
            with self.store.begin_immediate() as conn:
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=None,
                    failure_kind="repository_root_mismatch",
                    failure_summary="resolved repository root does not match submitted target",
                    now=now,
                    acquire_claim=True,
                )

        try:
            stored = self.artifacts.write_text(
                run_id,
                ADMISSION_STATUS_ARTIFACT,
                admission.artifact_text,
                max_bytes=MAX_BASELINE_STATUS_BYTES,
            )
        except (ProtectedArtifactError, OSError, PermissionError) as exc:
            with self.store.begin_immediate() as conn:
                return self._persist_admission_block(
                    conn=conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    expected_version=None,
                    failure_kind="admission_artifact_write_failed",
                    failure_summary=self._redact_artifact_failure_summary(exc),
                    now=self._now_factory(),
                    acquire_claim=True,
                )

        with self.store.begin_immediate() as conn:
            return self._persist_admission_success(
                conn=conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                artifact_path=stored.relative_path,
                artifact_sha256=stored.sha256,
                resolved_root=admission.evidence.resolved_root,
                now=self._now_factory(),
            )

    def _release_owned_admission_claim(
        self,
        *,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        now: datetime,
        stale: bool,
    ) -> None:
        with self.store.begin_immediate() as conn:
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                stale=stale,
                owner_id=tick_owner_id,
                lease_generation=tick_lease_generation,
            )

    def _validate_admission_fence(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        now: datetime,
    ) -> tuple[AuthorizedState, int] | TickRunReceipt:
        if not tick_lease_is_active(
            self.store,
            conn,
            owner_id=tick_owner_id,
            generation=tick_lease_generation,
            now=now,
        ):
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                stale=True,
                owner_id=tick_owner_id,
                lease_generation=tick_lease_generation,
            )
            self._record_stale(
                conn,
                run_id=run_id,
                rejection_kind="stale_tick_lease",
                safe_summary="tick lease expired before admission outcome",
                now=now,
            )
            return TickRunReceipt(run_id=run_id, action="admission_stale")
        claim = self.store.get_active_admission_claim(conn, claim_id)
        if claim is None:
            self._record_stale(
                conn,
                run_id=run_id,
                rejection_kind="stale_admission_claim",
                safe_summary="admission claim was not active",
                now=now,
            )
            return TickRunReceipt(run_id=run_id, action="admission_stale")
        state, version, _ = self.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, AuthorizedState):
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                stale=True,
                owner_id=tick_owner_id,
                lease_generation=tick_lease_generation,
            )
            return TickRunReceipt(run_id=run_id, action="admission_state_changed")
        if not admission_claim_matches(
            claim,
            claim_id=claim_id,
            owner_id=tick_owner_id,
            generation=tick_lease_generation,
            expected_run_version=version,
        ):
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                stale=True,
                owner_id=str(claim["tick_owner_id"]),
                lease_generation=int(claim["tick_lease_generation"]),
            )
            self._record_stale(
                conn,
                run_id=run_id,
                rejection_kind="stale_admission_claim",
                safe_summary="admission claim fence rejected the outcome",
                now=now,
            )
            return TickRunReceipt(run_id=run_id, action="admission_stale")
        return state, version

    def _persist_admission_success(
        self,
        *,
        conn: sqlite3.Connection,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        artifact_path: str,
        artifact_sha256: str,
        resolved_root: str,
        now: datetime,
    ) -> TickRunReceipt:
        fenced = self._validate_admission_fence(
            conn,
            run_id=run_id,
            claim_id=claim_id,
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            now=now,
        )
        if isinstance(fenced, TickRunReceipt):
            return fenced
        state, version = fenced
        event = WorktreeAdmittedEvent(
            run_id=run_id,
            admission_status_artifact_path=artifact_path,
            admission_status_sha256=artifact_sha256,
            resolved_root=resolved_root,
        )
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        new_state = apply_worktree_admitted(state, event, now_text=now_text)
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
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                stale=True,
                owner_id=tick_owner_id,
                lease_generation=tick_lease_generation,
            )
            return TickRunReceipt(run_id=run_id, action="admission_cas_lost")
        dispatch_id = self._dispatch_id_factory()
        pending_effect = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND status IN ('pending', 'claimed')
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if pending_effect is None:
            self.store.insert_preflight_effect(
                conn,
                dispatch_id=dispatch_id,
                source_event_id=event_id,
                run_id=run_id,
                available_at=now,
                claimed_run_version=new_state.version,
                now=now,
            )
        self.store.release_admission_tick_claim(
            conn,
            claim_id=claim_id,
            now=now,
            owner_id=tick_owner_id,
            lease_generation=tick_lease_generation,
        )
        return TickRunReceipt(run_id=run_id, action="admitted")

    def _persist_admission_block(
        self,
        *,
        conn: sqlite3.Connection,
        run_id: str,
        claim_id: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        expected_version: int | None,
        failure_kind: str,
        failure_summary: str,
        now: datetime,
        acquire_claim: bool,
    ) -> TickRunReceipt:
        if acquire_claim:
            fenced = self._validate_admission_fence(
                conn,
                run_id=run_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=now,
            )
            if isinstance(fenced, TickRunReceipt):
                return fenced
            state, version = fenced
        else:
            loaded_state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(loaded_state, AuthorizedState):
                return TickRunReceipt(run_id=run_id, action="admission_state_changed")
            state = loaded_state
            if expected_version is not None and version != expected_version:
                self._record_stale(
                    conn,
                    run_id=run_id,
                    rejection_kind="stale_run_version",
                    safe_summary="run version changed before admission block",
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="admission_stale")
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="admission_stale")
        event = WorktreeAdmissionBlockedEvent(
            run_id=run_id,
            block_reason_kind=failure_kind,
            block_reason_summary=failure_summary,
        )
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        new_state = apply_worktree_admission_blocked(state, event, now_text=now_text)
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
            if acquire_claim:
                self.store.release_admission_tick_claim(
                    conn,
                    claim_id=claim_id,
                    now=now,
                    stale=True,
                    owner_id=tick_owner_id,
                    lease_generation=tick_lease_generation,
                )
            return TickRunReceipt(run_id=run_id, action="admission_cas_lost")
        self.store.release_reservation(
            conn,
            worktree_key=state.context.repository.worktree_key,
            now=now,
        )
        if acquire_claim:
            self.store.release_admission_tick_claim(
                conn,
                claim_id=claim_id,
                now=now,
                owner_id=tick_owner_id,
                lease_generation=tick_lease_generation,
            )
        return TickRunReceipt(run_id=run_id, action="blocked", detail=failure_kind)

    def _append_stale_rejection(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        rejection_kind: str,
        safe_summary: str,
        now: datetime,
    ) -> None:
        state, _, _ = self.store.load_validated_snapshot(conn, run_id)
        if not isinstance(state, (AuthorizedState, AdmittedState)):
            return
        event = TickStaleRejectedEvent(
            run_id=run_id,
            rejection_kind=rejection_kind,
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

    def _maybe_run_synthetic_effect(
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
                return TickRunReceipt(run_id=run_id, action="effect_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return None
            effects = self.store.list_eligible_effects(conn, run_id=run_id, now=now)
            if not effects:
                return None
            dispatch_row = effects[0]
            dispatch_id = str(dispatch_row["dispatch_id"])
            effect_id = str(dispatch_row["effect_id"])
            scheduled_version = int(dispatch_row["claimed_run_version"])

        claim_id = self._claim_id_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=self._now_factory(),
            ):
                return TickRunReceipt(run_id=run_id, action="effect_stale")
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                return None
            if version < scheduled_version:
                self._record_stale(
                    conn,
                    run_id=run_id,
                    rejection_kind="stale_effect_run_version",
                    safe_summary="effect dispatch run version regressed below schedule baseline",
                    now=self._now_factory(),
                )
                return TickRunReceipt(run_id=run_id, action="effect_stale")
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
                return TickRunReceipt(run_id=run_id, action="effect_claim_lost")

        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                self.store.mark_effect_stale(
                    conn, dispatch_id=dispatch_id, claim_id=claim_id, now=now
                )
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                )
                self._record_stale(
                    conn,
                    run_id=run_id,
                    rejection_kind="stale_tick_lease",
                    safe_summary="tick lease expired before effect completion",
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="effect_stale")

            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, AdmittedState):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="effect_state_changed")

            claimed_effect = self.store.get_claimed_effect_row(conn, dispatch_id)
            if claimed_effect is None:
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="effect_complete_stale")
            claimed_run_version = int(claimed_effect["claimed_run_version"])
            if version != claimed_run_version:
                self.store.mark_effect_stale(
                    conn, dispatch_id=dispatch_id, claim_id=claim_id, now=now
                )
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                )
                self._record_stale(
                    conn,
                    run_id=run_id,
                    rejection_kind="stale_effect_run_version",
                    safe_summary="effect claim run version disagrees with current snapshot",
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="effect_stale")

            if not self.store.complete_claimed_effect(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                expected_run_version=claimed_run_version,
                now=now,
            ):
                self.store.release_capacity(
                    conn,
                    run_id=run_id,
                    claim_id=claim_id,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                )
                return TickRunReceipt(run_id=run_id, action="effect_complete_stale")

            event = SyntheticEffectCompletedEvent(
                run_id=run_id,
                effect_id=effect_id,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
            )
            apply_synthetic_effect_completed(state, event)
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
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=now,
            )
            return TickRunReceipt(run_id=run_id, action="synthetic_effect_completed")


def default_tick_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    git_admission: GitAdmissionPort | None = None,
    attempt_backend: AgentProcessBackend | None = None,
    preflight_port: SchedulerPreflightPort | None = None,
) -> TickService:
    from ai_dev_loop.scheduler.application.git_admission import BoundedGitAdmissionPort
    from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend

    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    backend = attempt_backend
    if backend is None:
        backend = SystemdUserBackend()
    return TickService(
        store,
        artifacts,
        git_admission or BoundedGitAdmissionPort(),
        attempt_backend=backend,
        preflight_port=preflight_port,
    )


def run_scheduler_tick(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    git_admission: GitAdmissionPort | None = None,
    attempt_backend: AgentProcessBackend | None = None,
    preflight_port: SchedulerPreflightPort | None = None,
) -> TickReceipt:
    return default_tick_service(
        db_path=db_path,
        artifact_root=artifact_root,
        git_admission=git_admission,
        attempt_backend=attempt_backend,
        preflight_port=preflight_port,
    ).run_once()
