"""Bounded scheduler-tick reconciliation for Phase 20.6 recoveries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.git import (
    checkpoint_git_rev_parse,
    checkpoint_git_status_porcelain,
    recovery_git_private_ref_peek,
    recovery_git_worktree_contains,
)
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.attempt_backend import AgentProcessBackend
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TickRunReceipt,
)
from ai_dev_loop.scheduler.application.git_admission import discover_repository_bounded
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    load_recovery_cleanup_evidence,
    load_recovery_definition,
    load_recovery_seed_evidence,
)
from ai_dev_loop.scheduler.application.recovery_integration import RecoveryIntegrationService
from ai_dev_loop.scheduler.application.recovery_integration_fencing import (
    RecoveryIntegrationTickContext,
)
from ai_dev_loop.scheduler.application.recovery_worktree import ProductionRecoveryWorktreePort
from ai_dev_loop.scheduler.domain.recovery import (
    ABORT_PENDING_RECOVERY_STATE_KIND,
    ACTIVE_RECOVERY_STATE_KIND,
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    INTEGRATION_PENDING_RECOVERY_STATE_KIND,
    RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
    RECOVERY_SEED_EVIDENCE_ARTIFACT,
    AbortedRecoveryState,
    AbortPendingRecoveryState,
    ActiveRecoveryState,
    BlockedRecoveryState,
    CleanupPendingRecoveryState,
    IntegratedRecoveryState,
    IntegrationPendingRecoveryState,
)
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    BlockedState,
    CompletedState,
    CompletedWithResidualRiskState,
    MaxIterationsReachedState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now

_RECONCILE_RECOVERY_STATE_KINDS = frozenset(
    {
        ABORT_PENDING_RECOVERY_STATE_KIND,
        ACTIVE_RECOVERY_STATE_KIND,
        INTEGRATION_PENDING_RECOVERY_STATE_KIND,
        CLEANUP_PENDING_RECOVERY_STATE_KIND,
    }
)

SCHEDULER_TERMINAL_FOR_RECOVERY = frozenset(
    {
        "completed",
        "completed_with_residual_risk",
        "aborted",
        "failed",
        "max_iterations_reached",
        "blocked",
    }
)

_cleanup_publication_step_hook: Callable[[str], None] | None = None


def set_recovery_cleanup_publication_step_hook(
    hook: Callable[[str], None] | None,
) -> None:
    """Test hook for deterministic cleanup evidence publication interruption points."""
    global _cleanup_publication_step_hook
    _cleanup_publication_step_hook = hook


def _cleanup_publication_step(name: str) -> None:
    if _cleanup_publication_step_hook is not None:
        _cleanup_publication_step_hook(name)


def _verify_cleanup_resource_removal(
    target_root: Path,
    *,
    worktree_path: str,
    private_ref: str,
) -> None:
    wt_path = Path(worktree_path)
    if wt_path.is_dir() and recovery_git_worktree_contains(
        target_root,
        worktree_path=wt_path,
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cleanup evidence adoption requires managed worktree removal",
        )
    if recovery_git_private_ref_peek(target_root, ref=private_ref) is not None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "cleanup evidence adoption requires private ref removal",
        )


def _cleanup_evidence_bytes(
    *,
    integrated_commit_sha256: str,
    worktree_path: str,
    private_ref: str,
) -> bytes:
    return json.dumps(
        {
            "integrated_commit_sha256": integrated_commit_sha256,
            "private_ref": private_ref,
            "worktree_path": worktree_path,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


@dataclass(frozen=True)
class DeferredRecoveryAbort:
    recovery_id: str
    recovery_run_id: str
    expected_version: int


class RecoveryReconcileService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        abort_backend: AgentProcessBackend | None = None,
        worktree_port: ProductionRecoveryWorktreePort | None = None,
        now_factory: Callable[[], datetime] | None = None,
        tick_context: RecoveryIntegrationTickContext | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._abort_backend = abort_backend
        self._worktree = worktree_port or ProductionRecoveryWorktreePort()
        self._now_factory = now_factory or (lambda: utc_now())
        self._tick_context = tick_context
        self._integration = RecoveryIntegrationService(
            store,
            artifacts,
            now_factory=self._now_factory,
            tick_context=tick_context,
        )
        self._deferred_aborts: list[DeferredRecoveryAbort] = []
        self._deferred_integrations: list[str] = []
        self._deferred_cleanups: list[str] = []

    def set_integration_tick_context(
        self,
        tick_context: RecoveryIntegrationTickContext | None,
    ) -> None:
        self._tick_context = tick_context
        self._integration = RecoveryIntegrationService(
            self.store,
            self.artifacts,
            now_factory=self._now_factory,
            tick_context=tick_context,
        )

    def reconcile_pending_recoveries(self, conn: sqlite3.Connection) -> list[TickRunReceipt]:
        recovery_ids = self.store.list_fresh_review_recovery_ids_by_state_kinds(
            conn,
            _RECONCILE_RECOVERY_STATE_KINDS,
        )
        receipts: list[TickRunReceipt] = []
        for recovery_id in recovery_ids:
            receipt = self.reconcile_recovery(conn, recovery_id)
            if receipt is not None:
                receipts.append(receipt)
        return receipts

    def finalize_deferred_aborts(self) -> list[TickRunReceipt]:
        if self._abort_backend is None:
            self._deferred_aborts.clear()
            return []
        receipts: list[TickRunReceipt] = []
        abort_service = SchedulerAbortService(
            self.store,
            self._abort_backend,
            artifacts=self.artifacts,
            now_factory=self._now_factory,
        )
        pending = list(self._deferred_aborts)
        self._deferred_aborts.clear()
        for item in pending:
            with self.store.begin_read() as conn:
                row = self.store.get_fresh_review_recovery(conn, item.recovery_id)
                if row is None or str(row["state_kind"]) != ABORT_PENDING_RECOVERY_STATE_KIND:
                    continue
                run_state, _, _ = self.store.load_validated_snapshot(conn, item.recovery_run_id)
            if run_state.kind not in {
                "aborted",
                "failed",
                "completed",
                "completed_with_residual_risk",
                "max_iterations_reached",
                "blocked",
            }:
                try:
                    abort_service.abort_run(
                        item.recovery_run_id,
                        reason="recovery_abort_requested",
                    )
                except Exception:
                    receipts.append(
                        TickRunReceipt(
                            run_id=item.recovery_id,
                            action="recovery_abort_deferred_failed",
                        )
                    )
                    continue
            now = self._now_factory()
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            pending_state = AbortPendingRecoveryState.model_validate_json(str(row["state_payload"]))
            aborted = AbortedRecoveryState(
                recovery_id=item.recovery_id,
                version=pending_state.version + 1,
                updated_at=now_text,
                definition_sha256=pending_state.definition_sha256,
                definition_artifact_sha256=pending_state.definition_artifact_sha256,
                source_run_id=pending_state.source_run_id,
                source_run_id_prefix=pending_state.source_run_id_prefix,
                recovery_run_id=item.recovery_run_id,
                sequence=pending_state.sequence,
                started_at=pending_state.started_at,
                aborted_at=now_text,
            )
            with self.store.begin_immediate() as conn:
                self.store.update_fresh_review_recovery(
                    conn,
                    recovery_id=item.recovery_id,
                    state=aborted,
                    expected_version=pending_state.version,
                    now=now,
                )
            receipts.append(TickRunReceipt(run_id=item.recovery_id, action="recovery_aborted"))
        return receipts

    def reconcile_recovery(
        self, conn: sqlite3.Connection, recovery_id: str
    ) -> TickRunReceipt | None:
        row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            return None
        state_kind = str(row["state_kind"])
        if state_kind == ABORT_PENDING_RECOVERY_STATE_KIND:
            return self._collect_abort_pending(conn, recovery_id, row)
        if state_kind == ACTIVE_RECOVERY_STATE_KIND:
            return self._reconcile_active(conn, recovery_id, row)
        if state_kind == INTEGRATION_PENDING_RECOVERY_STATE_KIND:
            if self._integration.reconcile_pre_cas_abort_hold(recovery_id):
                return TickRunReceipt(
                    run_id=recovery_id,
                    action="recovery_pre_cas_abort_hold_reconciled",
                )
            self._deferred_integrations.append(recovery_id)
            return TickRunReceipt(run_id=recovery_id, action="recovery_integration_deferred")
        if state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND:
            self._deferred_cleanups.append(recovery_id)
            return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_deferred")
        return None

    def integrate_deferred(self, recovery_id: str) -> TickRunReceipt:
        try:
            self._integration.integrate_recovery_pending(recovery_id)
            return TickRunReceipt(run_id=recovery_id, action="recovery_integrated")
        except Exception:
            return TickRunReceipt(run_id=recovery_id, action="recovery_integration_failed")

    def cleanup_deferred(self, recovery_id: str) -> TickRunReceipt:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_missing")
        pending = CleanupPendingRecoveryState.model_validate_json(str(row["state_payload"]))
        seed_path = self.artifacts.run_root(recovery_id) / RECOVERY_SEED_EVIDENCE_ARTIFACT
        if not seed_path.is_file():
            return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_missing_seed")
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=pending.definition_sha256,
            definition_artifact_sha256=pending.definition_artifact_sha256,
        )
        if pending.seed_evidence_artifact_sha256 is None:
            return TickRunReceipt(
                run_id=recovery_id, action="recovery_cleanup_missing_seed_binding"
            )
        seed = load_recovery_seed_evidence(
            self.artifacts,
            recovery_id,
            definition=definition,
            expected_sha256=pending.seed_evidence_artifact_sha256,
        )
        worktree_path = Path(seed.worktree_path)
        target_root = Path(seed.target_repository_root)
        private_ref = seed.private_ref
        integrated_sha = pending.integrated_commit_sha256
        cleanup_evidence_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_CLEANUP_EVIDENCE_ARTIFACT
        )
        if cleanup_evidence_path.is_file():
            try:
                pending = self._publish_cleanup_evidence(
                    recovery_id,
                    pending,
                    target_root=target_root,
                    integrated_sha=integrated_sha,
                    worktree_path=str(worktree_path),
                    private_ref=private_ref,
                )
            except SchedulerEngineError:
                return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_failed")
            return self._finalize_integrated_after_cleanup(
                recovery_id, pending, now=self._now_factory()
            )
        try:
            if target_root.is_dir():
                branch_head = checkpoint_git_rev_parse(target_root, definition.target_branch_ref)
                if branch_head != integrated_sha:
                    raise ValueError("target branch does not match integrated commit")
                if worktree_path.is_dir():
                    if not recovery_git_worktree_contains(target_root, worktree_path=worktree_path):
                        raise ValueError("managed worktree is not registered for cleanup")
                    admission = discover_repository_bounded(worktree_path)
                    if admission.git_common_dir != seed.managed_git_common_dir:
                        raise ValueError("managed worktree git_common_dir drift before cleanup")
                    if admission.git_dir != seed.managed_git_dir:
                        raise ValueError("managed worktree git_dir drift before cleanup")
                    worktree_head = checkpoint_git_rev_parse(worktree_path, "HEAD")
                    if worktree_head != integrated_sha:
                        raise ValueError("managed worktree HEAD does not match integrated commit")
                    status = checkpoint_git_status_porcelain(worktree_path)
                    if status.strip():
                        raise ValueError("managed worktree is not clean before cleanup")
                    peek = recovery_git_private_ref_peek(target_root, ref=private_ref)
                    if peek is not None and peek != integrated_sha:
                        raise ValueError("private recovery ref drift before cleanup")
                    self._worktree.remove_worktree(target_root, worktree_path=worktree_path)
                if private_ref and integrated_sha:
                    peek = recovery_git_private_ref_peek(target_root, ref=private_ref)
                    if peek is not None:
                        self._worktree.delete_private_ref(
                            target_root,
                            private_ref=private_ref,
                            expected_sha=integrated_sha,
                        )
        except Exception:
            return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_failed")
        try:
            pending = self._publish_cleanup_evidence(
                recovery_id,
                pending,
                target_root=target_root,
                integrated_sha=integrated_sha,
                worktree_path=str(worktree_path),
                private_ref=private_ref,
            )
        except SchedulerEngineError:
            return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_failed")
        return self._finalize_integrated_after_cleanup(
            recovery_id,
            pending,
            now=self._now_factory(),
        )

    def _publish_cleanup_evidence(
        self,
        recovery_id: str,
        pending: CleanupPendingRecoveryState,
        *,
        target_root: Path,
        integrated_sha: str,
        worktree_path: str,
        private_ref: str,
    ) -> CleanupPendingRecoveryState:
        frozen_bytes = _cleanup_evidence_bytes(
            integrated_commit_sha256=integrated_sha,
            worktree_path=worktree_path,
            private_ref=private_ref,
        )
        frozen_sha = hashlib.sha256(frozen_bytes).hexdigest()
        cleanup_path = self.artifacts.run_root(recovery_id) / RECOVERY_CLEANUP_EVIDENCE_ARTIFACT
        if pending.cleanup_evidence_artifact_sha256 is not None:
            bound_sha = pending.cleanup_evidence_artifact_sha256
            if frozen_sha != bound_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "cleanup evidence reconstruction disagrees with durable binding",
                )
            load_recovery_cleanup_evidence(
                self.artifacts,
                recovery_id,
                expected_sha256=bound_sha,
                integrated_commit_sha256=integrated_sha,
                worktree_path=worktree_path,
                private_ref=private_ref,
            )
            return pending
        if cleanup_path.is_file():
            existing_bytes = cleanup_path.read_bytes()
            existing_sha = hashlib.sha256(existing_bytes).hexdigest()
            if existing_bytes != frozen_bytes:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "immutable cleanup evidence artifact conflict",
                )
            _verify_cleanup_resource_removal(
                target_root,
                worktree_path=worktree_path,
                private_ref=private_ref,
            )
            _cleanup_publication_step("after_cleanup_write")
            return self._persist_cleanup_evidence_binding(
                recovery_id,
                pending,
                cleanup_evidence_artifact_sha256=existing_sha,
            )
        stored_cleanup = self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_CLEANUP_EVIDENCE_ARTIFACT,
            frozen_bytes,
            max_bytes=len(frozen_bytes) + 1,
        )
        _cleanup_publication_step("after_cleanup_write")
        return self._persist_cleanup_evidence_binding(
            recovery_id,
            pending,
            cleanup_evidence_artifact_sha256=stored_cleanup.sha256,
        )

    def _persist_cleanup_evidence_binding(
        self,
        recovery_id: str,
        pending: CleanupPendingRecoveryState,
        *,
        cleanup_evidence_artifact_sha256: str,
    ) -> CleanupPendingRecoveryState:
        if pending.cleanup_evidence_artifact_sha256 == cleanup_evidence_artifact_sha256:
            return pending
        updated = pending.model_copy(
            update={"cleanup_evidence_artifact_sha256": cleanup_evidence_artifact_sha256},
        )
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=updated,
                expected_version=pending.version,
                now=now,
            )
        _cleanup_publication_step("after_cleanup_binding")
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found after cleanup evidence binding",
            )
        return CleanupPendingRecoveryState.model_validate_json(str(row["state_payload"]))

    def _collect_abort_pending(
        self,
        conn: sqlite3.Connection,
        recovery_id: str,
        row: sqlite3.Row,
    ) -> TickRunReceipt:
        pending = AbortPendingRecoveryState.model_validate_json(str(row["state_payload"]))
        recovery_run_id = pending.recovery_run_id
        if recovery_run_id:
            self._deferred_aborts.append(
                DeferredRecoveryAbort(
                    recovery_id=recovery_id,
                    recovery_run_id=recovery_run_id,
                    expected_version=pending.version,
                )
            )
        return TickRunReceipt(run_id=recovery_id, action="recovery_abort_deferred")

    def _reconcile_active(
        self,
        conn: sqlite3.Connection,
        recovery_id: str,
        row: sqlite3.Row,
    ) -> TickRunReceipt | None:
        active = ActiveRecoveryState.model_validate_json(str(row["state_payload"]))
        recovery_run_id = active.recovery_run_id
        state, _, _ = self.store.load_validated_snapshot(conn, recovery_run_id)
        if isinstance(state, (CompletedState, CompletedWithResidualRiskState)):
            now = self._now_factory()
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            pending = IntegrationPendingRecoveryState(
                recovery_id=recovery_id,
                version=active.version + 1,
                updated_at=now_text,
                definition_sha256=active.definition_sha256,
                definition_artifact_sha256=active.definition_artifact_sha256,
                source_run_id=active.source_run_id,
                source_run_id_prefix=active.source_run_id_prefix,
                recovery_run_id=recovery_run_id,
                sequence=active.sequence,
                started_at=active.started_at,
                accepted_outcome=state.kind,
                residual_risk=isinstance(state, CompletedWithResidualRiskState),
                seed_evidence_artifact_sha256=active.seed_evidence_artifact_sha256,
            )
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=pending,
                expected_version=active.version,
                now=now,
            )
            return TickRunReceipt(run_id=recovery_id, action="recovery_integration_pending")
        if isinstance(state, AbortedState):
            return self._mark_aborted(conn, recovery_id, active, recovery_run_id=recovery_run_id)
        if isinstance(state, BlockedState):
            return self._mark_blocked(conn, recovery_id, active, state, recovery_run_id)
        if isinstance(state, MaxIterationsReachedState):
            return self._mark_blocked_kind(
                conn,
                recovery_id,
                active,
                recovery_run_id,
                block_reason_kind="max_iterations_reached",
            )
        return None

    def _mark_aborted(
        self,
        conn: sqlite3.Connection,
        recovery_id: str,
        active: ActiveRecoveryState,
        *,
        recovery_run_id: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        aborted = AbortedRecoveryState(
            recovery_id=recovery_id,
            version=active.version + 1,
            updated_at=now_text,
            definition_sha256=active.definition_sha256,
            definition_artifact_sha256=active.definition_artifact_sha256,
            source_run_id=active.source_run_id,
            source_run_id_prefix=active.source_run_id_prefix,
            recovery_run_id=recovery_run_id,
            sequence=active.sequence,
            started_at=active.started_at,
            aborted_at=now_text,
        )
        self.store.update_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=aborted,
            expected_version=active.version,
            now=now,
        )
        return TickRunReceipt(run_id=recovery_id, action="recovery_aborted")

    def finalize_deferred_integrations(self) -> list[TickRunReceipt]:
        if not self._deferred_integrations:
            return []
        recovery_id = self._deferred_integrations.pop(0)
        self._deferred_integrations.clear()
        return [self.integrate_deferred(recovery_id)]

    def finalize_deferred_cleanups(self) -> list[TickRunReceipt]:
        if not self._deferred_cleanups:
            return []
        recovery_id = self._deferred_cleanups.pop(0)
        self._deferred_cleanups.clear()
        return [self.cleanup_deferred(recovery_id)]

    def _finalize_integrated_after_cleanup(
        self,
        recovery_id: str,
        pending: CleanupPendingRecoveryState,
        *,
        now: datetime,
    ) -> TickRunReceipt:
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if pending.cleanup_evidence_artifact_sha256 is None:
            raise ValueError("cleanup pending lacks durable cleanup evidence binding")
        integrated = IntegratedRecoveryState(
            recovery_id=recovery_id,
            version=pending.version + 1,
            updated_at=now_text,
            definition_sha256=pending.definition_sha256,
            definition_artifact_sha256=pending.definition_artifact_sha256,
            source_run_id=pending.source_run_id,
            source_run_id_prefix=pending.source_run_id_prefix,
            recovery_run_id=pending.recovery_run_id,
            sequence=pending.sequence,
            started_at=pending.started_at,
            integrated_at=now_text,
            accepted_outcome=pending.accepted_outcome,
            residual_risk=pending.residual_risk,
            integrated_commit_sha256=pending.integrated_commit_sha256,
            integrated_commit_sha256_prefix=pending.integrated_commit_sha256_prefix,
            seed_evidence_artifact_sha256=pending.seed_evidence_artifact_sha256,
            integration_intent_artifact_sha256=pending.integration_intent_artifact_sha256,
            integration_trusted_tree_artifact_sha256=pending.integration_trusted_tree_artifact_sha256,
            integration_result_artifact_sha256=pending.integration_result_artifact_sha256,
            cleanup_evidence_artifact_sha256=pending.cleanup_evidence_artifact_sha256,
        )
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=integrated,
                expected_version=pending.version,
                now=now,
            )
        return TickRunReceipt(run_id=recovery_id, action="recovery_cleanup_complete")

    def _mark_blocked(
        self,
        conn: sqlite3.Connection,
        recovery_id: str,
        active: ActiveRecoveryState,
        run_state: BlockedState,
        recovery_run_id: str,
    ) -> TickRunReceipt:
        return self._mark_blocked_kind(
            conn,
            recovery_id,
            active,
            recovery_run_id,
            block_reason_kind=run_state.block_reason_kind,
        )

    def _mark_blocked_kind(
        self,
        conn: sqlite3.Connection,
        recovery_id: str,
        active: ActiveRecoveryState,
        recovery_run_id: str,
        *,
        block_reason_kind: str,
    ) -> TickRunReceipt:
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        blocked = BlockedRecoveryState(
            recovery_id=recovery_id,
            version=active.version + 1,
            updated_at=now_text,
            definition_sha256=active.definition_sha256,
            definition_artifact_sha256=active.definition_artifact_sha256,
            source_run_id=active.source_run_id,
            source_run_id_prefix=active.source_run_id_prefix,
            recovery_run_id=recovery_run_id,
            sequence=active.sequence,
            started_at=active.started_at,
            blocked_at=now_text,
            block_reason_kind=block_reason_kind,
        )
        self.store.update_fresh_review_recovery(
            conn,
            recovery_id=recovery_id,
            state=blocked,
            expected_version=active.version,
            now=now,
        )
        return TickRunReceipt(run_id=recovery_id, action="recovery_blocked")
