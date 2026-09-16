"""Fresh-review recovery abort for Phase 20.6."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from ai_dev_loop.runners.git import checkpoint_git_rev_parse
from ai_dev_loop.scheduler.application.contracts import (
    RecoveryAbortResult,
    SafeNextAction,
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    load_recovery_definition,
    load_recovery_integration_intent,
    load_recovery_integration_result,
    load_recovery_integration_trusted_tree,
)
from ai_dev_loop.scheduler.application.recovery_integration import (
    RecoveryIntegrationService,
    _verify_recovery_commit_against_intent,
)
from ai_dev_loop.scheduler.domain.recovery import (
    ABORT_PENDING_RECOVERY_STATE_KIND,
    ABORTED_RECOVERY_STATE_KIND,
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    INTEGRATED_RECOVERY_STATE_KIND,
    INTEGRATION_PENDING_RECOVERY_STATE_KIND,
    PREPARED_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_START_INTENT_ARTIFACT,
    RECOVERY_STATE_ADAPTERS,
    AbortedRecoveryState,
    AbortPendingRecoveryState,
    CleanupPendingRecoveryState,
    IntegrationPendingRecoveryState,
    PreparedRecoveryState,
    RecoveryBaseState,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


class IntegratedCommitAbortProofKind(StrEnum):
    PROVEN = "proven"
    AMBIGUOUS = "ambiguous"
    UNPROVEN = "unproven"


@dataclass(frozen=True)
class IntegratedCommitAbortProof:
    kind: IntegratedCommitAbortProofKind
    commit_sha: str | None = None


class RecoveryAbortService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore | None = None,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())

    def abort(self, recovery_id: str) -> RecoveryAbortResult:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found",
            )
        state_kind = str(row["state_kind"])
        if state_kind in {ABORTED_RECOVERY_STATE_KIND, INTEGRATED_RECOVERY_STATE_KIND}:
            return RecoveryAbortResult(
                recovery_id=recovery_id,
                state_kind=state_kind,
                changed=False,
                idempotent_replay=True,
                safe_next_action=SafeNextAction(
                    kind=SafeNextActionKind.NONE,
                    command=f"Recovery {recovery_id} is already terminal ({state_kind}).",
                ),
            )
        if state_kind == ABORT_PENDING_RECOVERY_STATE_KIND:
            return RecoveryAbortResult(
                recovery_id=recovery_id,
                state_kind=state_kind,
                changed=False,
                idempotent_replay=True,
                safe_next_action=SafeNextAction(
                    kind=SafeNextActionKind.SCHEDULER_TICK,
                    command=f"Recovery {recovery_id} abort is pending reconciliation.",
                ),
            )
        adapter = RECOVERY_STATE_ADAPTERS[state_kind]
        payload = str(row["state_payload"])
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND:
            return self._abort_cleanup_pending(recovery_id, payload=payload)
        if state_kind == INTEGRATION_PENDING_RECOVERY_STATE_KIND:
            return self._abort_integration_pending(recovery_id, payload=payload)
        current: RecoveryBaseState
        if state_kind == PREPARED_RECOVERY_STATE_KIND:
            current = PreparedRecoveryState.model_validate_json(payload)
            if self.artifacts is not None:
                start_intent_path = (
                    self.artifacts.run_root(recovery_id) / RECOVERY_START_INTENT_ARTIFACT
                )
                if start_intent_path.is_file():
                    intent_bytes = (
                        f"recovery_id={recovery_id}\n"
                        f"requested_at={now_text}\n"
                        f"partial_materialization=true\n"
                    ).encode()
                    self.artifacts.write_bytes(
                        recovery_id,
                        RECOVERY_ABORT_INTENT_ARTIFACT,
                        intent_bytes,
                        max_bytes=len(intent_bytes) + 1,
                    )
            aborted = AbortedRecoveryState(
                recovery_id=recovery_id,
                version=current.version + 1,
                updated_at=now_text,
                definition_sha256=current.definition_sha256,
                definition_artifact_sha256=current.definition_artifact_sha256,
                source_run_id=current.source_run_id,
                source_run_id_prefix=current.source_run_id_prefix,
                sequence=current.sequence,
                aborted_at=now_text,
            )
        else:
            current = cast(RecoveryBaseState, adapter.validate_json(payload))
            if self.artifacts is not None:
                intent_bytes = (
                    f"recovery_id={recovery_id}\n"
                    f"requested_at={now_text}\n"
                    f"recovery_run_id={getattr(current, 'recovery_run_id', '')}\n"
                ).encode()
                self.artifacts.write_bytes(
                    recovery_id,
                    RECOVERY_ABORT_INTENT_ARTIFACT,
                    intent_bytes,
                    max_bytes=len(intent_bytes) + 1,
                )
            abort_pending = AbortPendingRecoveryState(
                recovery_id=recovery_id,
                version=current.version + 1,
                updated_at=now_text,
                definition_sha256=current.definition_sha256,
                definition_artifact_sha256=current.definition_artifact_sha256,
                source_run_id=current.source_run_id,
                source_run_id_prefix=current.source_run_id_prefix,
                sequence=current.sequence,
                started_at=getattr(current, "started_at", now_text),
                abort_requested_at=now_text,
                recovery_run_id=getattr(current, "recovery_run_id", None),
            )
            with self.store.begin_immediate() as conn:
                self.store.update_fresh_review_recovery(
                    conn,
                    recovery_id=recovery_id,
                    state=abort_pending,
                    expected_version=current.version,
                    now=now,
                )
            return RecoveryAbortResult(
                recovery_id=recovery_id,
                state_kind=abort_pending.kind,
                changed=True,
                idempotent_replay=False,
                safe_next_action=SafeNextAction(
                    kind=SafeNextActionKind.SCHEDULER_TICK,
                    command=f"Recovery {recovery_id} abort pending. Run ai_dev_loop scheduler tick.",
                ),
            )
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=aborted,
                expected_version=current.version,
                now=now,
            )
        return RecoveryAbortResult(
            recovery_id=recovery_id,
            state_kind=aborted.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=SafeNextAction(
                kind=SafeNextActionKind.NONE,
                command=f"Recovery {recovery_id} aborted before start.",
            ),
        )


    def _classify_integrated_commit_for_abort(
        self,
        recovery_id: str,
        *,
        definition_sha256: str,
        definition_artifact_sha256: str,
        integration_intent_artifact_sha256: str | None = None,
        integration_trusted_tree_artifact_sha256: str | None = None,
    ) -> IntegratedCommitAbortProof:
        if self.artifacts is None:
            return IntegratedCommitAbortProof(IntegratedCommitAbortProofKind.UNPROVEN)
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=definition_sha256,
            definition_artifact_sha256=definition_artifact_sha256,
        )
        target_root = Path(definition.repository.root)
        commit_sha: str | None = None
        result_path = self.artifacts.run_root(recovery_id) / "recovery/integration-result.json"
        if result_path.is_file():
            result = load_recovery_integration_result(self.artifacts, recovery_id)
            raw_commit = result.get("commit_sha256", "")
            if isinstance(raw_commit, str) and raw_commit:
                commit_sha = raw_commit
        if commit_sha is None:
            evidence_path = (
                self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
            )
            if evidence_path.is_file():
                payload = json.loads(evidence_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    raw_evidence_commit = payload.get("commit_sha256")
                    if isinstance(raw_evidence_commit, str) and raw_evidence_commit:
                        commit_sha = raw_evidence_commit
        if commit_sha is None:
            return IntegratedCommitAbortProof(IntegratedCommitAbortProofKind.UNPROVEN)
        try:
            branch_head = checkpoint_git_rev_parse(target_root, definition.target_branch_ref)
        except Exception:
            return IntegratedCommitAbortProof(
                IntegratedCommitAbortProofKind.AMBIGUOUS,
                commit_sha=commit_sha,
            )
        if branch_head != commit_sha:
            return IntegratedCommitAbortProof(IntegratedCommitAbortProofKind.UNPROVEN)
        if branch_head == definition.parent_head:
            return IntegratedCommitAbortProof(IntegratedCommitAbortProofKind.UNPROVEN)
        if integration_intent_artifact_sha256 is None or integration_trusted_tree_artifact_sha256 is None:
            return IntegratedCommitAbortProof(
                IntegratedCommitAbortProofKind.AMBIGUOUS,
                commit_sha=commit_sha,
            )
        try:
            intent = load_recovery_integration_intent(
                self.artifacts,
                recovery_id,
                expected_sha256=integration_intent_artifact_sha256,
            )
            trusted, _ = load_recovery_integration_trusted_tree(
                self.artifacts,
                recovery_id,
                expected_sha256=integration_trusted_tree_artifact_sha256,
            )
        except SchedulerEngineError:
            return IntegratedCommitAbortProof(
                IntegratedCommitAbortProofKind.AMBIGUOUS,
                commit_sha=commit_sha,
            )
        if trusted.intent_sha256 != integration_intent_artifact_sha256:
            return IntegratedCommitAbortProof(
                IntegratedCommitAbortProofKind.AMBIGUOUS,
                commit_sha=commit_sha,
            )
        if _verify_recovery_commit_against_intent(
            target_root,
            intent=intent,
            trusted=trusted,
            commit_sha=commit_sha,
        ):
            return IntegratedCommitAbortProof(
                IntegratedCommitAbortProofKind.PROVEN,
                commit_sha=commit_sha,
            )
        return IntegratedCommitAbortProof(
            IntegratedCommitAbortProofKind.AMBIGUOUS,
            commit_sha=commit_sha,
        )

    def _persist_abort_intent(
        self,
        recovery_id: str,
        *,
        recovery_run_id: str,
        now_text: str,
    ) -> bool:
        if self.artifacts is None:
            return False
        abort_path = self.artifacts.run_root(recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
        if abort_path.is_file():
            return False
        intent_bytes = (
            f"recovery_id={recovery_id}\n"
            f"requested_at={now_text}\n"
            f"recovery_run_id={recovery_run_id}\n"
        ).encode()
        self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_ABORT_INTENT_ARTIFACT,
            intent_bytes,
            max_bytes=len(intent_bytes) + 1,
        )
        return True

    def _abort_integration_pending(self, recovery_id: str, *, payload: str) -> RecoveryAbortResult:
        pending = IntegrationPendingRecoveryState.model_validate_json(payload)
        proof = self._classify_integrated_commit_for_abort(
            recovery_id,
            definition_sha256=pending.definition_sha256,
            definition_artifact_sha256=pending.definition_artifact_sha256,
            integration_intent_artifact_sha256=pending.integration_intent_artifact_sha256,
            integration_trusted_tree_artifact_sha256=pending.integration_trusted_tree_artifact_sha256,
        )
        if (
            proof.kind == IntegratedCommitAbortProofKind.PROVEN
            and proof.commit_sha is not None
            and self.artifacts is not None
        ):
            RecoveryIntegrationService(self.store, self.artifacts).complete_proven_integration_reconciliation(
                recovery_id
            )
            with self.store.begin_read() as conn:
                row = self.store.get_fresh_review_recovery(conn, recovery_id)
            if row is not None and str(row["state_kind"]) == CLEANUP_PENDING_RECOVERY_STATE_KIND:
                return self._abort_cleanup_pending(recovery_id, payload=str(row["state_payload"]))
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        abort_intent_changed = self._persist_abort_intent(
            recovery_id,
            recovery_run_id=pending.recovery_run_id,
            now_text=now_text,
        )
        if proof.kind == IntegratedCommitAbortProofKind.AMBIGUOUS:
            return RecoveryAbortResult(
                recovery_id=recovery_id,
                state_kind=INTEGRATION_PENDING_RECOVERY_STATE_KIND,
                changed=abort_intent_changed,
                idempotent_replay=False,
                safe_next_action=SafeNextAction(
                    kind=SafeNextActionKind.SCHEDULER_TICK,
                    command=(
                        f"Recovery {recovery_id} target CAS evidence is ambiguous; "
                        "reconcile integration before abort finalization."
                    ),
                ),
            )
        with self.store.begin_read() as conn:
            if self.store.has_checkpoint_reconciliation_hold(conn, pending.source_run_id):
                return RecoveryAbortResult(
                    recovery_id=recovery_id,
                    state_kind=INTEGRATION_PENDING_RECOVERY_STATE_KIND,
                    changed=abort_intent_changed,
                    idempotent_replay=False,
                    safe_next_action=SafeNextAction(
                        kind=SafeNextActionKind.SCHEDULER_TICK,
                        command=(
                            f"Recovery {recovery_id} checkpoint CAS reconciliation is pending; "
                            "resolve integration before abort finalization."
                        ),
                    ),
                )
        abort_pending = AbortPendingRecoveryState(
            recovery_id=recovery_id,
            version=pending.version + 1,
            updated_at=now_text,
            definition_sha256=pending.definition_sha256,
            definition_artifact_sha256=pending.definition_artifact_sha256,
            source_run_id=pending.source_run_id,
            source_run_id_prefix=pending.source_run_id_prefix,
            sequence=pending.sequence,
            started_at=pending.started_at,
            abort_requested_at=now_text,
            recovery_run_id=pending.recovery_run_id,
        )
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=abort_pending,
                expected_version=pending.version,
                now=now,
            )
        return RecoveryAbortResult(
            recovery_id=recovery_id,
            state_kind=abort_pending.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=SafeNextAction(
                kind=SafeNextActionKind.SCHEDULER_TICK,
                command=f"Recovery {recovery_id} abort pending during integration reconciliation.",
            ),
        )

    def _abort_cleanup_pending(self, recovery_id: str, *, payload: str) -> RecoveryAbortResult:
        cleanup = CleanupPendingRecoveryState.model_validate_json(payload)
        if self.artifacts is not None:
            definition = load_recovery_definition(
                self.artifacts,
                recovery_id,
                definition_sha256=cleanup.definition_sha256,
                definition_artifact_sha256=cleanup.definition_artifact_sha256,
            )
            branch_head = checkpoint_git_rev_parse(
                Path(definition.repository.root),
                definition.target_branch_ref,
            )
            if branch_head == cleanup.integrated_commit_sha256:
                from ai_dev_loop.scheduler.application.recovery_reconcile import (
                    RecoveryReconcileService,
                )

                RecoveryReconcileService(self.store, self.artifacts).cleanup_deferred(recovery_id)
                with self.store.begin_read() as conn:
                    row = self.store.get_fresh_review_recovery(conn, recovery_id)
                if row is not None:
                    state_kind = str(row["state_kind"])
                    if state_kind == INTEGRATED_RECOVERY_STATE_KIND:
                        return RecoveryAbortResult(
                            recovery_id=recovery_id,
                            state_kind=INTEGRATED_RECOVERY_STATE_KIND,
                            changed=True,
                            idempotent_replay=False,
                            safe_next_action=SafeNextAction(
                                kind=SafeNextActionKind.NONE,
                                command=(
                                    f"Recovery {recovery_id} integration preserved after "
                                    "cleanup reconciliation."
                                ),
                            ),
                        )
                    if state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND:
                        return RecoveryAbortResult(
                            recovery_id=recovery_id,
                            state_kind=CLEANUP_PENDING_RECOVERY_STATE_KIND,
                            changed=False,
                            idempotent_replay=False,
                            safe_next_action=SafeNextAction(
                                kind=SafeNextActionKind.SCHEDULER_TICK,
                                command=(
                                    f"Recovery {recovery_id} integrated commit preserved; "
                                    "cleanup remains retryable."
                                ),
                            ),
                        )
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        abort_pending = AbortPendingRecoveryState(
            recovery_id=recovery_id,
            version=cleanup.version + 1,
            updated_at=now_text,
            definition_sha256=cleanup.definition_sha256,
            definition_artifact_sha256=cleanup.definition_artifact_sha256,
            source_run_id=cleanup.source_run_id,
            source_run_id_prefix=cleanup.source_run_id_prefix,
            sequence=cleanup.sequence,
            started_at=cleanup.started_at,
            abort_requested_at=now_text,
            recovery_run_id=cleanup.recovery_run_id,
        )
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=abort_pending,
                expected_version=cleanup.version,
                now=now,
            )
        return RecoveryAbortResult(
            recovery_id=recovery_id,
            state_kind=abort_pending.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=SafeNextAction(
                kind=SafeNextActionKind.SCHEDULER_TICK,
                command=f"Recovery {recovery_id} abort pending during cleanup reconciliation.",
            ),
        )


def abort_recovery(
    recovery_id: str,
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> RecoveryAbortResult:
    store = SqliteSchedulerStore(db_path or default_engine_db_path())
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return RecoveryAbortService(store, artifacts).abort(recovery_id)
