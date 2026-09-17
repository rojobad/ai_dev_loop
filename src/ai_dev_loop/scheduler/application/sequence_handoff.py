"""Sequence reviewed-checkpoint reconciliation and same-sequence handoff."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    CheckpointGitDeadline,
    GitIdentity,
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_rev_parse,
    checkpoint_git_symbolic_ref,
    checkpoint_git_write_tree,
    checkpoint_resolve_git_identity,
    checkpoint_validate_checked_out_branch,
    checkpoint_validate_repository_layout,
    checkpoint_validate_staged_patch_matches_artifact,
    checkpoint_verify_commit_identity,
)
from ai_dev_loop.scheduler.application.abort import complete_recorded_checkpoint_abort_if_ready
from ai_dev_loop.scheduler.application.checkpoint_abort_coordination import (
    CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
    CheckpointRefAdvancementInspection,
    CheckpointRefInspectionOutcome,
    abort_blocks_cas_authorization,
    abort_blocks_checkpoint_mutation,
    authorized_cas_abort_resolution_may_proceed,
    checkpoint_cas_authorization_pending,
    checkpoint_cas_uncertainty_active,
    checkpoint_commit_evidence_exists,
    checkpoint_ref_may_have_advanced_for_commit,
    inspect_checkpoint_ref_advancement,
)
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.cursor_evidence import (
    FrozenRepositoryIdentity,
    frozen_repository_identity,
)
from ai_dev_loop.scheduler.application.git_checkpoint import (
    CheckpointFenceError,
    CheckpointMutationBoundary,
    GitCheckpointCommit,
    GitCheckpointPort,
    ProductionGitCheckpointPort,
    checkpoint_result_from_commit,
)
from ai_dev_loop.scheduler.application.sequence_checkpoint_evidence import (
    SequenceCheckpointEvidenceError,
    authenticate_checkpoint_result_bindings,
    verify_checkpoint_commit_in_repository,
)
from ai_dev_loop.scheduler.application.sequence_materializer import (
    SequenceRunMaterializer,
    sequence_run_idempotency_key,
)
from ai_dev_loop.scheduler.application.tick_fencing import (
    parse_utc_instant,
    tick_lease_is_active,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
    SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
    SEQUENCE_CHECKPOINT_RESULT_ARTIFACT,
    SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
    GitIdentitySnapshot,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointResult,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.events import (
    RunAuthorizedEvent,
    RunCompletedEvent,
    RunCompletedWithResidualRiskEvent,
    RunSubmittedEvent,
    SchedulerEvent,
    SequenceCheckpointCommittedEvent,
    SequenceCheckpointRequestedEvent,
    SequenceFinalizedEvent,
    SequenceHandoffCompletedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_run_authorized,
    apply_run_completed,
    apply_run_completed_with_residual_risk,
    apply_sequence_checkpoint_requested,
)
from ai_dev_loop.scheduler.domain.sequence import (
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    FrozenSequenceEntry,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    AwaitingCodexReviewState,
    CheckpointPendingState,
    CompletedState,
    CompletedWithResidualRiskState,
    SubmittedState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_bytes, utc_now


class SequenceHandoffService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        git_checkpoint: GitCheckpointPort | None = None,
        materializer: SequenceRunMaterializer | None = None,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        handoff_step_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._git_checkpoint = git_checkpoint or ProductionGitCheckpointPort()
        self._materializer = materializer or SequenceRunMaterializer(artifacts)
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._handoff_step_hook = handoff_step_hook

    def _complete_recorded_checkpoint_abort(self, run_id: str) -> None:
        now = self._now_factory()
        fence_id = f"fnc-{secrets.token_hex(16)}"
        with self.store.begin_immediate() as conn:
            complete_recorded_checkpoint_abort_if_ready(
                self.store,
                conn,
                run_id=run_id,
                now=now,
                event_id_factory=self._event_id_factory,
                fence_id=fence_id,
            )

    def _finalize_checkpoint_aborted_receipt(self, run_id: str) -> TickRunReceipt:
        self._complete_recorded_checkpoint_abort(run_id)
        return TickRunReceipt(run_id=run_id, action="checkpoint_aborted")

    def _step(self, name: str) -> None:
        if self._handoff_step_hook is not None:
            self._handoff_step_hook(name)

    def _lease_expires_at(self, conn: sqlite3.Connection) -> datetime | None:
        row = self.store.get_tick_lease_row(conn)
        if row["expires_at"] is None:
            return None
        return parse_utc_instant(str(row["expires_at"]))

    def _git_deadline(self, conn: sqlite3.Connection) -> CheckpointGitDeadline:
        return CheckpointGitDeadline.from_lease(self._lease_expires_at(conn))

    def _reconciliation_may_advance_after_abort(
        self,
        conn: sqlite3.Connection,
        run_id: str,
    ) -> bool:
        hold = self.store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        return hold is not None and bool(int(hold["ref_may_have_advanced"]))

    def _emit_reconciliation_hold(
        self,
        *,
        run_id: str,
        intent_sha256: str,
        ref_may_have_advanced: bool,
        hold_reason: str = "checkpoint_reconciliation",
    ) -> TickRunReceipt:
        now = self._now_factory()
        with self.store.begin_immediate() as conn:
            existing = self.store.get_checkpoint_reconciliation_hold_row(conn, run_id)
            if (
                existing is not None
                and str(existing["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
                and not ref_may_have_advanced
            ):
                hold_reason = CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
            self.store.acquire_checkpoint_reconciliation_hold(
                conn,
                run_id=run_id,
                intent_sha256=intent_sha256,
                hold_reason=hold_reason,
                ref_may_have_advanced=ref_may_have_advanced,
                now=now,
            )
        return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_hold")

    def _verify_authorized_cas_non_applied_repository_state(
        self,
        intent: SequenceCheckpointIntent,
        trusted_tree: SequenceCheckpointTrustedTree,
        patch_path: Path,
        *,
        git_deadline: CheckpointGitDeadline,
    ) -> bool:
        repo_root = Path(intent.repository_root)
        now = self._now_factory()
        try:
            timeout = git_deadline.remaining_timeout(now)
            checkpoint_validate_repository_layout(
                repo_root,
                expected_root=intent.repository_root,
                expected_git_common_dir=intent.git_common_dir,
                expected_git_dir=intent.git_dir,
                expected_branch=intent.branch_ref.removeprefix("refs/heads/"),
                context="authorized CAS abort resolution repository identity",
                timeout=timeout,
            )
            timeout = git_deadline.remaining_timeout(now)
            checkpoint_validate_checked_out_branch(
                repo_root,
                expected_branch=intent.branch_ref.removeprefix("refs/heads/"),
                timeout=timeout,
            )
            timeout = git_deadline.remaining_timeout(now)
            if checkpoint_git_symbolic_ref(repo_root, "HEAD", timeout=timeout) != intent.branch_ref:
                return False
            timeout = git_deadline.remaining_timeout(now)
            if checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout) != intent.parent_head:
                return False
            timeout = git_deadline.remaining_timeout(now)
            checkpoint_validate_staged_patch_matches_artifact(
                repo_root,
                patch_path,
                timeout=timeout,
            )
            timeout = git_deadline.remaining_timeout(now)
            live_patch_sha = hashlib.sha256(
                checkpoint_git_diff_cached_patch_bytes(repo_root, timeout=timeout)
            ).hexdigest()
            if live_patch_sha != intent.reviewed_patch_sha256:
                return False
            if live_patch_sha != trusted_tree.reviewed_patch_sha256:
                return False
            timeout = git_deadline.remaining_timeout(now)
            live_tree_sha = checkpoint_git_write_tree(repo_root, timeout=timeout)
            if live_tree_sha != trusted_tree.reviewed_tree_sha256:
                return False
        except (ValidationError, OSError):
            return False
        return True

    def _try_resolve_authorized_cas_abort_hold(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        intent: SequenceCheckpointIntent,
        intent_sha256: str,
        trusted_tree: SequenceCheckpointTrustedTree,
        patch_path: Path,
        inspection: CheckpointRefAdvancementInspection,
        tick_owner_id: str,
        tick_lease_generation: int,
        git_deadline: CheckpointGitDeadline,
    ) -> bool:
        now = self._now_factory()
        if not authorized_cas_abort_resolution_may_proceed(
            self.store,
            conn,
            run_id=run_id,
            inspection=inspection,
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            now=now,
        ):
            return False
        if not self._verify_authorized_cas_non_applied_repository_state(
            intent,
            trusted_tree,
            patch_path,
            git_deadline=git_deadline,
        ):
            return False
        self.store.release_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256=intent_sha256,
        )
        return True

    def _reconcile_checkpoint_hold_from_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        intent: SequenceCheckpointIntent,
        intent_sha256: str,
        trusted_tree: SequenceCheckpointTrustedTree,
        patch_path: Path,
        tick_owner_id: str,
        tick_lease_generation: int,
        git_deadline: CheckpointGitDeadline,
        hold_reason: str = "checkpoint_commit_evidence_recovered",
    ) -> bool:
        if not checkpoint_commit_evidence_exists(self.artifacts, run_id):
            return False
        recovered_evidence = self._load_checkpoint_evidence(run_id)
        commit_sha256 = recovered_evidence.commit_sha256 if recovered_evidence is not None else None
        inspection = inspect_checkpoint_ref_advancement(intent, commit_sha256)
        existing = self.store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        existing_cas_authorized = (
            existing is not None
            and str(existing["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        )
        if inspection.outcome == CheckpointRefInspectionOutcome.APPLIED:
            self.store.acquire_checkpoint_reconciliation_hold(
                conn,
                run_id=run_id,
                intent_sha256=intent_sha256,
                hold_reason="checkpoint_ref_advanced",
                ref_may_have_advanced=True,
                now=self._now_factory(),
            )
            return False
        if existing_cas_authorized:
            return self._try_resolve_authorized_cas_abort_hold(
                conn,
                run_id=run_id,
                intent=intent,
                intent_sha256=intent_sha256,
                trusted_tree=trusted_tree,
                patch_path=patch_path,
                inspection=inspection,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                git_deadline=git_deadline,
            )
        if inspection.outcome == CheckpointRefInspectionOutcome.NOT_APPLIED_AT_PARENT:
            self.store.acquire_checkpoint_reconciliation_hold(
                conn,
                run_id=run_id,
                intent_sha256=intent_sha256,
                hold_reason=hold_reason,
                ref_may_have_advanced=False,
                now=self._now_factory(),
            )
            return False
        self.store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256=intent_sha256,
            hold_reason=CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
            ref_may_have_advanced=False,
            now=self._now_factory(),
        )
        return False

    def _try_record_applied_checkpoint_reconciliation_only(
        self,
        run_id: str,
        *,
        intent: SequenceCheckpointIntent,
        intent_sha256: str,
        trusted_tree: SequenceCheckpointTrustedTree,
        trusted_tree_sha256: str,
        tick_owner_id: str,
        tick_lease_generation: int,
    ) -> TickRunReceipt | None:
        evidence = self._load_checkpoint_evidence(run_id)
        commit_sha256 = evidence.commit_sha256 if evidence is not None else None
        inspection = inspect_checkpoint_ref_advancement(intent, commit_sha256)
        if inspection.outcome != CheckpointRefInspectionOutcome.APPLIED:
            return None
        if commit_sha256 is None:
            return None
        try:
            verify_checkpoint_commit_in_repository(
                intent=intent,
                trusted_tree=trusted_tree,
                commit_sha256=commit_sha256,
                require_head_match=True,
            )
        except SequenceCheckpointEvidenceError:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        result_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
        if result_path.is_file():
            result = SequenceCheckpointResult.model_validate_json(result_path.read_bytes())
            if result.commit_sha256 != commit_sha256:
                return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
            if not self._verify_checkpoint_result(
                intent,
                result,
                intent_sha256,
                trusted_tree_sha256=trusted_tree_sha256,
            ):
                return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
            authenticate_checkpoint_result_bindings(
                result=result,
                intent=intent,
                trusted_tree=trusted_tree,
                intent_sha256=intent_sha256,
                trusted_tree_file_sha256=trusted_tree_sha256,
                run_id=run_id,
                materialized=MaterializedSequenceEntry(
                    ordinal=intent.predecessor_ordinal,
                    run_id=run_id,
                    entry_hash="0" * 64,
                    materialized_at="1970-01-01T00:00:00.000000Z",
                ),
            )
        else:
            result = checkpoint_result_from_commit(
                intent,
                commit=GitCheckpointCommit(
                    commit_sha=commit_sha256,
                    tree_sha=trusted_tree.reviewed_tree_sha256,
                    parent_head=intent.parent_head,
                    already_applied=True,
                    ref_updated=True,
                ),
                intent_sha256=intent_sha256,
                trusted_tree_sha256=trusted_tree_sha256,
            )
            if not self._verify_checkpoint_result(
                intent,
                result,
                intent_sha256,
                trusted_tree_sha256=trusted_tree_sha256,
            ):
                return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
            authenticate_checkpoint_result_bindings(
                result=result,
                intent=intent,
                trusted_tree=trusted_tree,
                intent_sha256=intent_sha256,
                trusted_tree_file_sha256=trusted_tree_sha256,
                run_id=run_id,
                materialized=MaterializedSequenceEntry(
                    ordinal=intent.predecessor_ordinal,
                    run_id=run_id,
                    entry_hash="0" * 64,
                    materialized_at="1970-01-01T00:00:00.000000Z",
                ),
            )
            result_text = (
                json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            )
            self.artifacts.write_text_or_verify(
                run_id,
                SEQUENCE_CHECKPOINT_RESULT_ARTIFACT,
                result_text,
                max_bytes=16_384,
            )
        now = self._now_factory()
        fence_id = f"fnc-{secrets.token_hex(16)}"
        with self.store.begin_immediate() as conn:
            if not tick_lease_is_active(
                self.store,
                conn,
                owner_id=tick_owner_id,
                generation=tick_lease_generation,
                now=now,
            ):
                return TickRunReceipt(run_id=run_id, action="checkpoint_lease_lost")
            hold = self.store.get_checkpoint_reconciliation_hold_row(conn, run_id)
            if hold is not None:
                self.store.release_checkpoint_reconciliation_hold(
                    conn,
                    run_id=run_id,
                    intent_sha256=str(hold["intent_sha256"]),
                )
            if self.store.has_abort_requested_for_run(conn, run_id):
                complete_recorded_checkpoint_abort_if_ready(
                    self.store,
                    conn,
                    run_id=run_id,
                    now=now,
                    event_id_factory=self._event_id_factory,
                    fence_id=fence_id,
                )
        return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_completed")

    def _ref_may_have_advanced_after_git_error(
        self,
        intent: SequenceCheckpointIntent,
        commit_sha256: str | None,
    ) -> bool:
        return checkpoint_ref_may_have_advanced_for_commit(intent, commit_sha256)

    def _reconciliation_uncertainty_after_git_error(
        self,
        run_id: str,
        intent: SequenceCheckpointIntent,
        commit_sha256: str | None,
    ) -> bool:
        inspection = inspect_checkpoint_ref_advancement(intent, commit_sha256)
        if inspection.outcome == CheckpointRefInspectionOutcome.APPLIED:
            return True
        with self.store.begin_read() as conn:
            if checkpoint_cas_authorization_pending(self.store, conn, run_id=run_id):
                return True
        return inspection.outcome in {
            CheckpointRefInspectionOutcome.AMBIGUOUS,
            CheckpointRefInspectionOutcome.NOT_APPLIED_ELSEWHERE,
        }

    def _reconciliation_hold_after_git_error(
        self,
        run_id: str,
        intent: SequenceCheckpointIntent,
        commit_sha256: str | None,
    ) -> tuple[bool, str]:
        inspection = inspect_checkpoint_ref_advancement(intent, commit_sha256)
        if inspection.outcome == CheckpointRefInspectionOutcome.APPLIED:
            return True, "checkpoint_ref_advanced"
        with self.store.begin_read() as conn:
            if checkpoint_cas_authorization_pending(self.store, conn, run_id=run_id):
                return False, CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        if inspection.outcome in {
            CheckpointRefInspectionOutcome.AMBIGUOUS,
            CheckpointRefInspectionOutcome.NOT_APPLIED_ELSEWHERE,
        }:
            return False, CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        return False, "checkpoint_reconciliation"

    def _load_checkpoint_evidence(self, run_id: str) -> SequenceCheckpointEvidence | None:
        path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT
        if not path.is_file():
            return None
        return SequenceCheckpointEvidence.model_validate_json(path.read_bytes())

    def _persist_checkpoint_evidence(
        self,
        run_id: str,
        evidence: SequenceCheckpointEvidence,
        *,
        replace: bool,
    ) -> None:
        text = json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        if replace:
            self.artifacts.replace_text(
                run_id,
                SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
                text,
                max_bytes=8_192,
            )
        else:
            self.artifacts.write_text(
                run_id,
                SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
                text,
                max_bytes=8_192,
            )

    def _load_trusted_tree(
        self,
        run_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> SequenceCheckpointTrustedTree | None:
        path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
        if not path.is_file():
            return None
        if expected_sha256 is not None:
            bytes_payload = self.artifacts.read_verified_bytes(
                run_id,
                SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
                expected_sha256=expected_sha256,
            )
            return SequenceCheckpointTrustedTree.model_validate_json(bytes_payload)
        return SequenceCheckpointTrustedTree.model_validate_json(path.read_bytes())

    def _persist_trusted_tree(
        self,
        run_id: str,
        trusted_tree: SequenceCheckpointTrustedTree,
    ) -> str:
        text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        stored = self.artifacts.write_text(
            run_id,
            SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
            text,
            max_bytes=8_192,
        )
        return stored.sha256

    def _authenticate_trusted_tree(
        self,
        run_id: str,
        intent: SequenceCheckpointIntent,
        *,
        intent_sha256: str,
        trusted_tree: SequenceCheckpointTrustedTree,
        trusted_tree_sha256: str,
    ) -> bool:
        if trusted_tree.intent_sha256 != intent_sha256:
            return False
        if trusted_tree.parent_head != intent.parent_head:
            return False
        if trusted_tree.reviewed_patch_sha256 != intent.reviewed_patch_sha256:
            return False
        path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
        if not path.is_file():
            return False
        actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        return actual_sha == trusted_tree_sha256

    def _reconstruct_missing_checkpoint_artifacts(
        self,
        run_id: str,
        intent: SequenceCheckpointIntent,
        *,
        intent_sha256: str,
        recorded_at: str,
        git_deadline: CheckpointGitDeadline,
    ) -> tuple[str, SequenceCheckpointTrustedTree]:
        repo_root = Path(intent.repository_root)
        timeout = git_deadline.remaining_timeout(self._now_factory())
        checkpoint_validate_repository_layout(
            repo_root,
            expected_root=intent.repository_root,
            expected_git_common_dir=intent.git_common_dir,
            expected_git_dir=intent.git_dir,
            expected_branch=intent.branch_ref.removeprefix("refs/heads/"),
            context="orphan checkpoint repository identity",
            timeout=timeout,
        )
        timeout = git_deadline.remaining_timeout(self._now_factory())
        parent_head = checkpoint_git_rev_parse(repo_root, "HEAD", timeout=timeout)
        if parent_head != intent.parent_head:
            raise ValidationError("orphan checkpoint parent HEAD drift blocks reconstruction")
        patch_abs = self.artifacts.run_root(run_id) / intent.staged_patch_artifact_path
        timeout = git_deadline.remaining_timeout(self._now_factory())
        checkpoint_validate_staged_patch_matches_artifact(
            repo_root,
            patch_abs,
            timeout=timeout,
        )
        timeout = git_deadline.remaining_timeout(self._now_factory())
        live_patch_sha = hashlib.sha256(
            checkpoint_git_diff_cached_patch_bytes(repo_root, timeout=timeout)
        ).hexdigest()
        if live_patch_sha != intent.reviewed_patch_sha256:
            raise ValidationError("orphan checkpoint staged patch drift blocks reconstruction")
        timeout = git_deadline.remaining_timeout(self._now_factory())
        reviewed_tree_sha = checkpoint_git_write_tree(repo_root, timeout=timeout)
        trusted_tree = SequenceCheckpointTrustedTree(
            intent_sha256=intent_sha256,
            reviewed_tree_sha256=reviewed_tree_sha,
            reviewed_patch_sha256=intent.reviewed_patch_sha256,
            parent_head=intent.parent_head,
            recorded_at=recorded_at,
        )
        trusted_tree_sha256 = self._persist_trusted_tree(run_id, trusted_tree)
        evidence = self._load_checkpoint_evidence(run_id)
        if evidence is None:
            self._persist_checkpoint_evidence(
                run_id,
                SequenceCheckpointEvidence(tree_sha256=reviewed_tree_sha),
                replace=False,
            )
        elif evidence.tree_sha256 != reviewed_tree_sha or evidence.commit_sha256 is not None:
            raise ValidationError("orphan checkpoint evidence conflicts with trusted tree")
        return trusted_tree_sha256, trusted_tree

    def _expected_branch_ref(self, identity: FrozenRepositoryIdentity) -> str:
        branch = identity.branch
        if branch.startswith("refs/"):
            return branch
        return f"refs/heads/{branch}"

    def _authenticate_predecessor_checkpoint_result(
        self,
        conn: sqlite3.Connection,
        *,
        prev_run_id: str,
        sequence_state: ActiveSequenceState,
        current_ordinal: int,
    ) -> str:
        if len(sequence_state.materialized_entries) < current_ordinal - 1:
            raise ValidationError("previous phase materialization missing for checkpoint parent")
        if sequence_state.materialized_entries[current_ordinal - 2].run_id != prev_run_id:
            raise ValidationError("previous phase run_id mismatch for checkpoint parent")
        result_path = self.artifacts.run_root(prev_run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
        if not result_path.is_file():
            raise ValidationError("previous checkpoint result artifact missing")
        result_bytes = result_path.read_bytes()
        result = SequenceCheckpointResult.model_validate_json(result_bytes)
        if result.sequence_id != sequence_state.sequence_id:
            raise ValidationError("previous checkpoint result sequence_id mismatch")
        if result.predecessor_run_id != prev_run_id:
            raise ValidationError("previous checkpoint result predecessor_run_id mismatch")
        expected_prev_ordinal = current_ordinal - 1
        if result.predecessor_ordinal != expected_prev_ordinal:
            raise ValidationError("previous checkpoint result predecessor_ordinal mismatch")
        if result.accepted_outcome not in {"completed", "completed_with_residual_risk"}:
            raise ValidationError("previous checkpoint result accepted_outcome invalid")
        intent_path = self.artifacts.run_root(prev_run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
        if not intent_path.is_file():
            raise ValidationError("previous checkpoint intent artifact missing")
        prev_intent_bytes = intent_path.read_bytes()
        try:
            prev_intent = SequenceCheckpointIntent.model_validate_json(prev_intent_bytes)
        except PydanticValidationError as exc:
            raise ValidationError("previous checkpoint intent payload is invalid") from exc
        if result.successor_run_id != prev_intent.successor_run_id:
            raise ValidationError("previous checkpoint result successor_run_id mismatch")
        prev_intent_sha = hashlib.sha256(prev_intent_bytes).hexdigest()
        if result.intent_sha256 != prev_intent_sha:
            raise ValidationError("previous checkpoint result intent binding mismatch")
        successor_materialized = next(
            (
                entry.run_id
                for entry in sequence_state.materialized_entries
                if entry.ordinal == current_ordinal
            ),
            None,
        )
        if successor_materialized is None:
            raise ValidationError("checkpoint parent successor materialization missing")
        from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
            authenticate_checkpoint_successor_replacement_chain,
        )

        authenticate_checkpoint_successor_replacement_chain(
            conn,
            sequence_id=sequence_state.sequence_id,
            ordinal=current_ordinal,
            recorded_successor_run_id=prev_intent.successor_run_id,
            current_leaf_run_id=successor_materialized,
        )
        trusted_tree = self._load_trusted_tree(
            prev_run_id,
            expected_sha256=result.trusted_tree_sha256,
        )
        if trusted_tree is None:
            raise ValidationError("previous checkpoint trusted tree missing")
        if trusted_tree.reviewed_tree_sha256 != result.tree_sha256:
            raise ValidationError("previous checkpoint trusted tree mismatch")
        if trusted_tree.intent_sha256 != prev_intent_sha:
            raise ValidationError("previous checkpoint trusted tree intent mismatch")
        if result.parent_head != prev_intent.parent_head:
            raise ValidationError("previous checkpoint result parent_head mismatch")
        if result.parent_head != trusted_tree.parent_head:
            raise ValidationError("previous checkpoint trusted tree parent mismatch")
        if result.branch_ref != prev_intent.branch_ref:
            raise ValidationError("previous checkpoint result branch_ref mismatch")
        if result.reviewed_patch_sha256 != prev_intent.reviewed_patch_sha256:
            raise ValidationError("previous checkpoint result reviewed patch mismatch")
        predecessor_entry = sequence_state.definition.entries[expected_prev_ordinal - 1]
        if (
            predecessor_entry.commit_message is not None
            and prev_intent.commit_message != predecessor_entry.commit_message
        ):
            raise ValidationError("previous checkpoint intent commit_message mismatch")
        repo_root = Path(prev_intent.repository_root)
        identity = GitIdentity(
            author_name=prev_intent.git_identity.author_name,
            author_email=prev_intent.git_identity.author_email,
            author_date=prev_intent.git_identity.author_date,
            committer_name=prev_intent.git_identity.committer_name,
            committer_email=prev_intent.git_identity.committer_email,
            committer_date=prev_intent.git_identity.committer_date,
        )
        checkpoint_verify_commit_identity(
            repo_root,
            commit_sha=result.commit_sha256,
            tree_sha=result.tree_sha256,
            parent_sha=result.parent_head,
            message=prev_intent.commit_message,
            identity=identity,
        )
        return result.commit_sha256

    def _expected_checkpoint_parent_head(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        ordinal: int,
        identity: FrozenRepositoryIdentity,
    ) -> str:
        if ordinal == 1:
            return identity.initial_head
        prev_run_id = sequence_state.materialized_entries[ordinal - 2].run_id
        return self._authenticate_predecessor_checkpoint_result(
            conn,
            prev_run_id=prev_run_id,
            sequence_state=sequence_state,
            current_ordinal=ordinal,
        )

    def _validate_orphan_intent_bindings(
        self,
        intent: SequenceCheckpointIntent,
        *,
        intent_sha256: str,
        state: AwaitingCodexReviewState,
        version: int,
        sequence_state: ActiveSequenceState,
        sequence_binding_ordinal: int,
        accepted_outcome: Literal["completed", "completed_with_residual_risk"],
        result_path: str,
        result_sha: str,
        identity: FrozenRepositoryIdentity,
        expected_parent: str,
        predecessor_entry: FrozenSequenceEntry,
        successor_entry: FrozenSequenceEntry,
        expected_branch_ref: str,
    ) -> None:
        if intent.predecessor_run_id != state.run_id:
            raise ValidationError("orphan intent predecessor_run_id mismatch")
        if intent.predecessor_run_version != version:
            raise ValidationError("orphan intent predecessor_run_version mismatch")
        if intent.predecessor_ordinal != sequence_binding_ordinal:
            raise ValidationError("orphan intent predecessor_ordinal mismatch")
        if intent.sequence_id != sequence_state.sequence_id:
            raise ValidationError("orphan intent sequence_id mismatch")
        if intent.sequence_version != sequence_state.version:
            raise ValidationError("orphan intent sequence_version mismatch")
        if intent.accepted_outcome != accepted_outcome:
            raise ValidationError("orphan intent accepted_outcome mismatch")
        if intent.review_result_artifact_path != result_path:
            raise ValidationError("orphan intent review_result_artifact_path mismatch")
        if intent.review_result_sha256 != result_sha:
            raise ValidationError("orphan intent review_result_sha256 mismatch")
        if intent.parent_head != expected_parent:
            raise ValidationError("orphan intent parent_head mismatch")
        if predecessor_entry.commit_message is None:
            raise ValidationError("orphan intent predecessor entry missing commit_message")
        if intent.commit_message != predecessor_entry.commit_message:
            raise ValidationError("orphan intent commit_message mismatch")
        if intent.successor_run_id != successor_entry.planned_run_id:
            raise ValidationError("orphan intent successor_run_id mismatch")
        if intent.successor_ordinal != successor_entry.ordinal:
            raise ValidationError("orphan intent successor_ordinal mismatch")
        if intent.branch_ref != expected_branch_ref:
            raise ValidationError("orphan intent branch_ref mismatch")
        if intent.repository_root != identity.root:
            raise ValidationError("orphan intent repository_root mismatch")
        if intent.git_common_dir != identity.git_common_dir:
            raise ValidationError("orphan intent git_common_dir mismatch")
        if intent.git_dir != identity.git_dir:
            raise ValidationError("orphan intent git_dir mismatch")
        patch_sha256 = state.cursor.staged_patch_sha256
        if patch_sha256 is None or intent.reviewed_patch_sha256 != patch_sha256:
            raise ValidationError("orphan intent reviewed_patch_sha256 mismatch")

    def _checkpoint_fencing(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        pending_version: int,
        intent_sha256: str,
        intent: SequenceCheckpointIntent,
        tick_owner_id: str,
        tick_lease_generation: int,
        now: datetime,
        phase: Literal["pre_git", "pre_materialize", "pre_ledger"],
        allow_post_cas_reconciliation: bool = False,
        acquire_hold_on_reconciliation: bool = False,
        ref_may_have_advanced: bool = False,
    ) -> TickRunReceipt | None:
        if not tick_lease_is_active(
            self.store,
            conn,
            owner_id=tick_owner_id,
            generation=tick_lease_generation,
            now=now,
        ):
            if allow_post_cas_reconciliation:
                if acquire_hold_on_reconciliation:
                    self.store.acquire_checkpoint_reconciliation_hold(
                        conn,
                        run_id=run_id,
                        intent_sha256=intent_sha256,
                        hold_reason="checkpoint_reconciliation",
                        ref_may_have_advanced=ref_may_have_advanced,
                        now=now,
                    )
                return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_hold")
            return TickRunReceipt(run_id=run_id, action="checkpoint_lease_lost")
        state, version, _ = self.store.load_validated_snapshot(conn, run_id)
        if abort_blocks_checkpoint_mutation(
            self.store,
            conn,
            run_id=run_id,
            state=state,
            allow_post_cas_reconciliation=allow_post_cas_reconciliation,
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_aborted")
        if isinstance(state, AbortedState):
            if allow_post_cas_reconciliation:
                if acquire_hold_on_reconciliation:
                    self.store.acquire_checkpoint_reconciliation_hold(
                        conn,
                        run_id=run_id,
                        intent_sha256=intent_sha256,
                        hold_reason="checkpoint_reconciliation",
                        ref_may_have_advanced=ref_may_have_advanced,
                        now=now,
                    )
                return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_hold")
            return TickRunReceipt(run_id=run_id, action="checkpoint_aborted")
        if not isinstance(state, CheckpointPendingState) or version != pending_version:
            return TickRunReceipt(run_id=run_id, action="checkpoint_state_changed")
        if state.checkpoint_intent_sha256 != intent_sha256:
            return TickRunReceipt(run_id=run_id, action="checkpoint_intent_hash_mismatch")
        if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_active_work")
        if self.store.has_pending_effects_for_run(conn, run_id):
            return TickRunReceipt(run_id=run_id, action="checkpoint_pending_effects")
        sequence_state = self.store.load_validated_sequence_state(conn, intent.sequence_id)
        if isinstance(sequence_state, AbortPendingSequenceState):
            if allow_post_cas_reconciliation:
                if acquire_hold_on_reconciliation:
                    self.store.acquire_checkpoint_reconciliation_hold(
                        conn,
                        run_id=run_id,
                        intent_sha256=intent_sha256,
                        hold_reason="checkpoint_reconciliation",
                        ref_may_have_advanced=ref_may_have_advanced,
                        now=now,
                    )
                return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_hold")
            return TickRunReceipt(run_id=run_id, action="checkpoint_aborted")
        if not isinstance(sequence_state, ActiveSequenceState):
            return TickRunReceipt(run_id=run_id, action="sequence_not_active")
        if sequence_state.version != intent.sequence_version:
            return TickRunReceipt(run_id=run_id, action="sequence_version_drift")
        if sequence_state.current_run_id != intent.predecessor_run_id:
            return TickRunReceipt(run_id=run_id, action="sequence_run_mismatch")
        reservation = self.store.get_reservation_for_run(conn, intent.predecessor_run_id)
        if reservation is None:
            if allow_post_cas_reconciliation:
                if acquire_hold_on_reconciliation:
                    self.store.acquire_checkpoint_reconciliation_hold(
                        conn,
                        run_id=run_id,
                        intent_sha256=intent_sha256,
                        hold_reason="checkpoint_reconciliation",
                        ref_may_have_advanced=ref_may_have_advanced,
                        now=now,
                    )
                return TickRunReceipt(run_id=run_id, action="checkpoint_reconciliation_hold")
            return TickRunReceipt(run_id=run_id, action="checkpoint_reservation_lost")
        if phase in {"pre_materialize", "pre_ledger"}:
            existing_successor = conn.execute(
                "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
                (intent.successor_run_id,),
            ).fetchone()
            if existing_successor is not None and phase == "pre_materialize":
                current, _, _ = self.store.load_validated_snapshot(conn, intent.successor_run_id)
                if current.kind != "authorized":
                    return TickRunReceipt(run_id=run_id, action="successor_state_conflict")
        return None

    def process_run(
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
                return None
            if self.store.get_nonterminal_attempt_for_run(conn, run_id) is not None:
                return None
            if self.store.has_pending_effects_for_run(conn, run_id):
                return None
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, CheckpointPendingState):
                return None
        return self._reconcile_checkpoint(
            run_id,
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
        )

    def enter_checkpoint_pending(
        self,
        conn: sqlite3.Connection,
        *,
        state: AwaitingCodexReviewState,
        version: int,
        accepted_outcome: Literal["completed", "completed_with_residual_risk"],
        review_iteration: int,
        result_path: str,
        result_sha: str,
        now: datetime,
        attempt_id: str,
    ) -> CheckpointPendingState:
        sequence_binding = state.context.sequence
        if sequence_binding is None:
            raise ValidationError("sequence binding required for checkpoint pending")
        sequence_state = self.store.load_validated_sequence_state(
            conn, sequence_binding.sequence_id
        )
        if isinstance(sequence_state, AbortPendingSequenceState):
            raise ValidationError("sequence abort pending blocks checkpoint")
        if not isinstance(sequence_state, ActiveSequenceState):
            raise ValidationError("sequence must be active for checkpoint pending")
        if sequence_state.current_run_id != state.run_id:
            raise ValidationError("sequence current_run_id mismatch")
        predecessor_entry = sequence_state.definition.entries[sequence_binding.ordinal - 1]
        if predecessor_entry.commit_message is None:
            raise ValidationError("non-final sequence entry requires commit_message")
        successor_entry = sequence_state.definition.entries[sequence_binding.ordinal]
        identity = frozen_repository_identity(
            state.context,
            run_id=state.run_id,
            artifacts=self.artifacts,
            checkpoint=state.checkpoint,
        )
        repo_root = Path(identity.root)
        git_deadline = self._git_deadline(conn)
        expected_branch_ref = self._expected_branch_ref(identity)
        expected_parent = self._expected_checkpoint_parent_head(
            conn,
            sequence_state=sequence_state,
            ordinal=sequence_binding.ordinal,
            identity=identity,
        )
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        intent_path = self.artifacts.run_root(state.run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
        if intent_path.is_file():
            intent_bytes = intent_path.read_bytes()
            intent_sha = sha256_bytes(intent_bytes)
            intent = SequenceCheckpointIntent.model_validate_json(intent_bytes)
            self._validate_orphan_intent_bindings(
                intent,
                intent_sha256=intent_sha,
                state=state,
                version=version,
                sequence_state=sequence_state,
                sequence_binding_ordinal=sequence_binding.ordinal,
                accepted_outcome=accepted_outcome,
                result_path=result_path,
                result_sha=result_sha,
                identity=identity,
                expected_parent=expected_parent,
                predecessor_entry=predecessor_entry,
                successor_entry=successor_entry,
                expected_branch_ref=expected_branch_ref,
            )
            trusted_tree = self._load_trusted_tree(state.run_id)
            if trusted_tree is None:
                trusted_tree_sha, trusted_tree = self._reconstruct_missing_checkpoint_artifacts(
                    state.run_id,
                    intent,
                    intent_sha256=intent_sha,
                    recorded_at=now_text,
                    git_deadline=git_deadline,
                )
            else:
                trusted_tree_sha = hashlib.sha256(
                    (
                        self.artifacts.run_root(state.run_id)
                        / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
                    ).read_bytes()
                ).hexdigest()
                if not self._authenticate_trusted_tree(
                    state.run_id,
                    intent,
                    intent_sha256=intent_sha,
                    trusted_tree=trusted_tree,
                    trusted_tree_sha256=trusted_tree_sha,
                ):
                    raise ValidationError("orphan trusted reviewed tree authentication failed")
                evidence = self._load_checkpoint_evidence(state.run_id)
                if evidence is None:
                    self._persist_checkpoint_evidence(
                        state.run_id,
                        SequenceCheckpointEvidence(
                            tree_sha256=trusted_tree.reviewed_tree_sha256,
                        ),
                        replace=False,
                    )
        else:
            checkpoint_validate_repository_layout(
                repo_root,
                expected_root=identity.root,
                expected_git_common_dir=identity.git_common_dir,
                expected_git_dir=identity.git_dir,
                expected_branch=identity.branch,
                context="checkpoint intent repository identity",
                timeout=git_deadline.remaining_timeout(now),
            )
            checkpoint_validate_checked_out_branch(
                repo_root,
                expected_branch=identity.branch,
                timeout=git_deadline.remaining_timeout(now),
            )
            branch_ref = checkpoint_git_symbolic_ref(
                repo_root, "HEAD", timeout=git_deadline.remaining_timeout(now)
            )
            parent_head = checkpoint_git_rev_parse(
                repo_root, "HEAD", timeout=git_deadline.remaining_timeout(now)
            )
            if parent_head != expected_parent:
                raise ValidationError("parent HEAD drift from recorded checkpoint baseline")
            patch_rel = str(state.cursor.staged_patch_path)
            patch_sha256 = state.cursor.staged_patch_sha256
            if patch_sha256 is None:
                raise ValidationError("staged patch hash required for checkpoint intent")
            patch_abs = self.artifacts.run_root(state.run_id) / patch_rel
            self.artifacts.read_verified_bytes(
                state.run_id,
                patch_rel,
                expected_sha256=patch_sha256,
            )
            checkpoint_validate_staged_patch_matches_artifact(
                repo_root,
                patch_abs,
                timeout=git_deadline.remaining_timeout(now),
            )
            live_patch_sha = hashlib.sha256(
                checkpoint_git_diff_cached_patch_bytes(
                    repo_root, timeout=git_deadline.remaining_timeout(now)
                )
            ).hexdigest()
            if live_patch_sha != patch_sha256:
                raise ValidationError("live staged patch drift from recorded artifact hash")
            reviewed_tree_sha = checkpoint_git_write_tree(
                repo_root, timeout=git_deadline.remaining_timeout(now)
            )
            git_identity = checkpoint_resolve_git_identity(
                repo_root, timeout=git_deadline.remaining_timeout(now)
            )
            intent = SequenceCheckpointIntent(
                sequence_id=sequence_binding.sequence_id,
                sequence_version=sequence_state.version,
                predecessor_run_id=state.run_id,
                predecessor_run_version=version,
                predecessor_ordinal=sequence_binding.ordinal,
                successor_run_id=successor_entry.planned_run_id,
                successor_ordinal=successor_entry.ordinal,
                accepted_outcome=accepted_outcome,
                branch_ref=branch_ref,
                parent_head=parent_head,
                reviewed_patch_sha256=patch_sha256,
                commit_message=predecessor_entry.commit_message,
                git_identity=GitIdentitySnapshot(
                    author_name=git_identity.author_name,
                    author_email=git_identity.author_email,
                    author_date=git_identity.author_date,
                    committer_name=git_identity.committer_name,
                    committer_email=git_identity.committer_email,
                    committer_date=git_identity.committer_date,
                ),
                repository_root=identity.root,
                git_common_dir=identity.git_common_dir,
                git_dir=identity.git_dir,
                staged_patch_artifact_path=patch_rel,
                review_result_artifact_path=result_path,
                review_result_sha256=result_sha,
            )
            intent_text = (
                json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
            )
            self._step("before_intent_write")
            self.artifacts.write_text_or_verify(
                state.run_id,
                SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
                intent_text,
                max_bytes=32_768,
            )
            intent_bytes = (
                self.artifacts.run_root(state.run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
            ).read_bytes()
            intent_sha = sha256_bytes(intent_bytes)
            trusted_tree = SequenceCheckpointTrustedTree(
                intent_sha256=intent_sha,
                reviewed_tree_sha256=reviewed_tree_sha,
                reviewed_patch_sha256=patch_sha256,
                parent_head=parent_head,
                recorded_at=now_text,
            )
            trusted_tree_sha = self._persist_trusted_tree(state.run_id, trusted_tree)
            self._step("before_evidence_write")
            evidence = SequenceCheckpointEvidence(tree_sha256=reviewed_tree_sha)
            self._persist_checkpoint_evidence(state.run_id, evidence, replace=False)
            self._step("after_intent_write")
        state_with_review = state.model_copy(
            update={
                "codex": state.codex.model_copy(
                    update={
                        "latest_review_result_path": result_path,
                        "latest_review_result_sha256": result_sha,
                    }
                )
            }
        )
        event = SequenceCheckpointRequestedEvent(
            run_id=state.run_id,
            sequence_id=sequence_binding.sequence_id,
            accepted_outcome=accepted_outcome,
            review_iteration=review_iteration,
            checkpoint_intent_artifact_path=SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
            checkpoint_intent_sha256=intent_sha,
            checkpoint_trusted_tree_sha256=trusted_tree_sha,
        )
        pending_state = apply_sequence_checkpoint_requested(
            state_with_review, event, now_text=now_text
        )
        event_id = self._event_id_factory()
        sequence_num = self.store.next_event_sequence(conn, state.run_id)
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=state.run_id,
            sequence=sequence_num,
            event=event,
            now=now,
        )
        if not self.store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=version,
            new_state=pending_state,
            now=now,
        ):
            raise ValidationError("checkpoint pending CAS lost")
        self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        return pending_state

    def _reconcile_checkpoint(
        self,
        run_id: str,
        *,
        tick_owner_id: str,
        tick_lease_generation: int,
    ) -> TickRunReceipt:
        with self.store.begin_read() as conn:
            state, version, _ = self.store.load_validated_snapshot(conn, run_id)
            if not isinstance(state, CheckpointPendingState):
                return TickRunReceipt(run_id=run_id, action="checkpoint_state_changed")
            intent_path = self.artifacts.run_root(run_id) / state.checkpoint_intent_artifact_path
            if not intent_path.is_file():
                return TickRunReceipt(run_id=run_id, action="checkpoint_intent_missing")
            intent_bytes = intent_path.read_bytes()
            intent_sha256 = hashlib.sha256(intent_bytes).hexdigest()
            if intent_sha256 != state.checkpoint_intent_sha256:
                return TickRunReceipt(run_id=run_id, action="checkpoint_intent_hash_mismatch")
            trusted_tree_sha256 = state.checkpoint_trusted_tree_sha256
            git_deadline = self._git_deadline(conn)
            allow_post_cas = self._reconciliation_may_advance_after_abort(conn, run_id)
        intent = SequenceCheckpointIntent.model_validate_json(intent_bytes)
        trusted_tree = self._load_trusted_tree(
            run_id,
            expected_sha256=trusted_tree_sha256,
        )
        if trusted_tree is None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_trusted_tree_missing")
        if not self._authenticate_trusted_tree(
            run_id,
            intent,
            intent_sha256=intent_sha256,
            trusted_tree=trusted_tree,
            trusted_tree_sha256=trusted_tree_sha256,
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_trusted_tree_invalid")
        with self.store.begin_read() as conn:
            sequence_state = self.store.load_validated_sequence_state(conn, intent.sequence_id)
            abort_reconcile_only = isinstance(sequence_state, AbortPendingSequenceState)
            abort_reconcile_only = abort_reconcile_only or self.store.has_abort_requested_for_run(
                conn, run_id
            )
        if allow_post_cas or abort_reconcile_only:
            reconciled = self._try_record_applied_checkpoint_reconciliation_only(
                run_id,
                intent=intent,
                intent_sha256=intent_sha256,
                trusted_tree=trusted_tree,
                trusted_tree_sha256=trusted_tree_sha256,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
            )
            if reconciled is not None:
                return reconciled
        patch_path = self.artifacts.run_root(run_id) / intent.staged_patch_artifact_path
        evidence = self._load_checkpoint_evidence(run_id)
        if (
            evidence is not None
            and evidence.tree_sha256 is not None
            and evidence.tree_sha256 != trusted_tree.reviewed_tree_sha256
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_evidence_tree_mismatch")
        with self.store.begin_immediate() as conn:
            hold_resolved = self._reconcile_checkpoint_hold_from_evidence(
                conn,
                run_id=run_id,
                intent=intent,
                intent_sha256=intent_sha256,
                trusted_tree=trusted_tree,
                patch_path=patch_path,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                git_deadline=git_deadline,
            )
        if hold_resolved:
            with self.store.begin_read() as conn:
                if self.store.has_abort_requested_for_run(
                    conn, run_id
                ) and not checkpoint_cas_uncertainty_active(self.store, conn, run_id=run_id):
                    return self._finalize_checkpoint_aborted_receipt(run_id)
        evidence_state: dict[str, str | None] = {
            "tree_sha256": evidence.tree_sha256 if evidence is not None else None,
            "commit_sha256": evidence.commit_sha256 if evidence is not None else None,
        }

        def _current_evidence() -> SequenceCheckpointEvidence:
            return SequenceCheckpointEvidence(
                tree_sha256=evidence_state["tree_sha256"],
                commit_sha256=evidence_state["commit_sha256"],
            )

        def _evidence_file_exists() -> bool:
            return (
                self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT
            ).is_file()

        def persist_tree_sha(tree_sha: str) -> None:
            if tree_sha != trusted_tree.reviewed_tree_sha256:
                raise ValidationError("checkpoint tree evidence disagrees with trusted tree")
            evidence_state["tree_sha256"] = tree_sha
            self._persist_checkpoint_evidence(
                run_id,
                _current_evidence(),
                replace=_evidence_file_exists(),
            )

        def persist_commit_sha(commit_sha: str) -> None:
            evidence_state["commit_sha256"] = commit_sha
            self._persist_checkpoint_evidence(
                run_id,
                _current_evidence(),
                replace=_evidence_file_exists(),
            )
            self._emit_reconciliation_hold(
                run_id=run_id,
                intent_sha256=intent_sha256,
                ref_may_have_advanced=False,
                hold_reason="checkpoint_commit_persisted",
            )

        def mutation_fence(boundary: CheckpointMutationBoundary) -> None:
            with self.store.begin_immediate() as conn:
                if boundary == "pre_commit_tree":
                    fence = self._checkpoint_fencing(
                        conn,
                        run_id=run_id,
                        pending_version=version,
                        intent_sha256=intent_sha256,
                        intent=intent,
                        tick_owner_id=tick_owner_id,
                        tick_lease_generation=tick_lease_generation,
                        now=self._now_factory(),
                        phase="pre_git",
                        allow_post_cas_reconciliation=False,
                    )
                    if fence is not None:
                        raise CheckpointFenceError(fence.action)
                    return
                if boundary == "pre_update_ref" and abort_blocks_cas_authorization(
                    self.store,
                    conn,
                    run_id=run_id,
                    allow_post_cas_reconciliation=False,
                ):
                    raise CheckpointFenceError("checkpoint_aborted")

        def authorize_ref_update() -> None:
            now = self._now_factory()
            with self.store.begin_immediate() as conn:
                fence = self._checkpoint_fencing(
                    conn,
                    run_id=run_id,
                    pending_version=version,
                    intent_sha256=intent_sha256,
                    intent=intent,
                    tick_owner_id=tick_owner_id,
                    tick_lease_generation=tick_lease_generation,
                    now=now,
                    phase="pre_git",
                    allow_post_cas_reconciliation=False,
                )
                if fence is not None:
                    raise CheckpointFenceError(fence.action)
                if abort_blocks_cas_authorization(
                    self.store,
                    conn,
                    run_id=run_id,
                    allow_post_cas_reconciliation=False,
                ):
                    raise CheckpointFenceError("checkpoint_aborted")
                self.store.acquire_checkpoint_reconciliation_hold(
                    conn,
                    run_id=run_id,
                    intent_sha256=intent_sha256,
                    hold_reason=CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
                    ref_may_have_advanced=False,
                    now=now,
                )

        def on_ref_advanced() -> None:
            self._emit_reconciliation_hold(
                run_id=run_id,
                intent_sha256=intent_sha256,
                ref_may_have_advanced=True,
                hold_reason="checkpoint_ref_advanced",
            )

        with self.store.begin_read() as conn:
            fence = self._checkpoint_fencing(
                conn,
                run_id=run_id,
                pending_version=version,
                intent_sha256=intent_sha256,
                intent=intent,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
                phase="pre_git",
                allow_post_cas_reconciliation=allow_post_cas,
            )
            if fence is not None:
                if fence.action == "checkpoint_reconciliation_hold" and (
                    allow_post_cas or abort_reconcile_only
                ):
                    reconciled = self._try_record_applied_checkpoint_reconciliation_only(
                        run_id,
                        intent=intent,
                        intent_sha256=intent_sha256,
                        trusted_tree=trusted_tree,
                        trusted_tree_sha256=trusted_tree_sha256,
                        tick_owner_id=tick_owner_id,
                        tick_lease_generation=tick_lease_generation,
                    )
                    if reconciled is not None:
                        return reconciled
                if fence.action == "checkpoint_aborted":
                    return self._finalize_checkpoint_aborted_receipt(run_id)
                return fence

        self._step("before_git_checkpoint")
        try:
            commit = self._git_checkpoint.execute_checkpoint(
                intent,
                patch_path=patch_path,
                trusted_tree=trusted_tree,
                evidence=_current_evidence()
                if evidence_state["tree_sha256"] or evidence_state["commit_sha256"]
                else None,
                persist_tree_sha=persist_tree_sha,
                persist_commit_sha=persist_commit_sha,
                mutation_fence=mutation_fence,
                authorize_ref_update=authorize_ref_update,
                on_ref_advanced=on_ref_advanced,
                deadline=git_deadline,
                now_factory=self._now_factory,
            )
        except CheckpointFenceError as exc:
            if evidence_state["commit_sha256"] or self._reconciliation_uncertainty_after_git_error(
                run_id,
                intent,
                evidence_state["commit_sha256"],
            ):
                ref_advanced, hold_reason = self._reconciliation_hold_after_git_error(
                    run_id,
                    intent,
                    evidence_state["commit_sha256"],
                )
                return self._emit_reconciliation_hold(
                    run_id=run_id,
                    intent_sha256=intent_sha256,
                    ref_may_have_advanced=ref_advanced,
                    hold_reason=hold_reason,
                )
            if str(exc) == "checkpoint_aborted":
                return self._finalize_checkpoint_aborted_receipt(run_id)
            return TickRunReceipt(run_id=run_id, action=str(exc))
        except ValidationError:
            if evidence_state["commit_sha256"] or self._reconciliation_uncertainty_after_git_error(
                run_id,
                intent,
                evidence_state["commit_sha256"],
            ):
                ref_advanced, hold_reason = self._reconciliation_hold_after_git_error(
                    run_id,
                    intent,
                    evidence_state["commit_sha256"],
                )
                return self._emit_reconciliation_hold(
                    run_id=run_id,
                    intent_sha256=intent_sha256,
                    ref_may_have_advanced=ref_advanced,
                    hold_reason=hold_reason,
                )
            raise
        result = checkpoint_result_from_commit(
            intent,
            commit=commit,
            intent_sha256=intent_sha256,
            trusted_tree_sha256=trusted_tree_sha256,
        )
        if not self._verify_checkpoint_result(
            intent,
            result,
            intent_sha256,
            trusted_tree_sha256=trusted_tree_sha256,
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        result_text = json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        self.artifacts.write_text_or_verify(
            run_id,
            SEQUENCE_CHECKPOINT_RESULT_ARTIFACT,
            result_text,
            max_bytes=16_384,
        )
        self._step("after_git_checkpoint")
        return self._complete_handoff(
            run_id,
            intent=intent,
            result=result,
            pending_state=state,
            pending_version=version,
            intent_sha256=intent_sha256,
            tick_owner_id=tick_owner_id,
            tick_lease_generation=tick_lease_generation,
            ref_updated=commit.ref_updated,
        )

    def _verify_checkpoint_result(
        self,
        intent: SequenceCheckpointIntent,
        result: SequenceCheckpointResult,
        intent_sha256: str,
        *,
        trusted_tree_sha256: str,
    ) -> bool:
        return (
            result.sequence_id == intent.sequence_id
            and result.predecessor_run_id == intent.predecessor_run_id
            and result.predecessor_ordinal == intent.predecessor_ordinal
            and result.successor_run_id == intent.successor_run_id
            and result.accepted_outcome == intent.accepted_outcome
            and result.parent_head == intent.parent_head
            and result.branch_ref == intent.branch_ref
            and result.reviewed_patch_sha256 == intent.reviewed_patch_sha256
            and result.intent_sha256 == intent_sha256
            and result.trusted_tree_sha256 == trusted_tree_sha256
        )

    def _complete_handoff(
        self,
        run_id: str,
        *,
        intent: SequenceCheckpointIntent,
        result: SequenceCheckpointResult,
        pending_state: CheckpointPendingState,
        pending_version: int,
        intent_sha256: str,
        tick_owner_id: str,
        tick_lease_generation: int,
        ref_updated: bool = False,
        now: datetime | None = None,
    ) -> TickRunReceipt:
        now = now or self._now_factory()
        sequence_id = intent.sequence_id
        with self.store.begin_read() as conn:
            fence = self._checkpoint_fencing(
                conn,
                run_id=run_id,
                pending_version=pending_version,
                intent_sha256=intent_sha256,
                intent=intent,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
                phase="pre_materialize",
                allow_post_cas_reconciliation=ref_updated,
                acquire_hold_on_reconciliation=ref_updated,
                ref_may_have_advanced=ref_updated,
            )
            if fence is not None:
                if fence.action == "checkpoint_aborted":
                    return self._finalize_checkpoint_aborted_receipt(run_id)
                return fence
            sequence_state = self.store.load_validated_sequence_state(conn, sequence_id)
            if not isinstance(sequence_state, ActiveSequenceState):
                return TickRunReceipt(run_id=run_id, action="sequence_not_active")
        successor_entry = sequence_state.definition.entries[intent.successor_ordinal - 1]
        self._step("before_materialize_successor")
        context, entry_hash = self._materializer.materialize_next_entry(
            sequence_id=sequence_id,
            definition=sequence_state.definition,
            entry=successor_entry,
        )
        self._step("after_materialize_successor")
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        successor_run_id = intent.successor_run_id
        idempotency_key = sequence_run_idempotency_key(context)
        submitted_state = SubmittedState(
            run_id=successor_run_id,
            version=1,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=idempotency_key,
            context=context,
        )
        submitted_event = RunSubmittedEvent(
            run_id=successor_run_id,
            idempotency_key=idempotency_key,
            worktree_key=context.repository.worktree_key,
            reused_existing=False,
        )
        authorized_event = RunAuthorizedEvent(
            run_id=successor_run_id,
            controller_session_id=context.controller.controller_session_id,
            idempotent_replay=False,
        )
        authorized_state = apply_run_authorized(
            submitted_state, authorized_event, now_text=now_text
        )
        if intent.accepted_outcome == "completed":
            terminal_event = RunCompletedEvent(
                run_id=run_id,
                review_iteration=pending_state.codex.review_iteration,
            )
            terminal_state: CompletedState | CompletedWithResidualRiskState = apply_run_completed(
                pending_state, terminal_event, now_text=now_text
            )
        else:
            residual_event = RunCompletedWithResidualRiskEvent(
                run_id=run_id,
                review_iteration=pending_state.codex.review_iteration,
            )
            terminal_state = apply_run_completed_with_residual_risk(
                pending_state, residual_event, now_text=now_text
            )
        residual_ordinals = tuple(sequence_state.residual_risk_ordinals)
        if intent.accepted_outcome == "completed_with_residual_risk":
            residual_ordinals = tuple(
                sorted(set(residual_ordinals + (intent.predecessor_ordinal,)))
            )
        updated_sequence = ActiveSequenceState(
            schema_version=sequence_state.schema_version,
            sequence_id=sequence_state.sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            current_ordinal=intent.successor_ordinal,
            current_run_id=successor_run_id,
            materialized_entries=(
                *sequence_state.materialized_entries,
                MaterializedSequenceEntry(
                    ordinal=intent.successor_ordinal,
                    run_id=successor_run_id,
                    entry_hash=entry_hash,
                    materialized_at=now_text,
                ),
            ),
            residual_risk_ordinals=residual_ordinals,
        )
        committed_event = SequenceCheckpointCommittedEvent(
            run_id=run_id,
            sequence_id=sequence_id,
            commit_sha256_prefix=result.commit_sha256[:12],
            tree_sha256_prefix=result.tree_sha256[:12],
        )
        handoff_event = SequenceHandoffCompletedEvent(
            predecessor_run_id=run_id,
            successor_run_id=successor_run_id,
            sequence_id=sequence_id,
            predecessor_ordinal=intent.predecessor_ordinal,
            successor_ordinal=intent.successor_ordinal,
        )
        self._step("before_ledger_handoff")
        checkpoint_aborted = False
        with self.store.begin_immediate() as conn:
            fence = self._checkpoint_fencing(
                conn,
                run_id=run_id,
                pending_version=pending_version,
                intent_sha256=intent_sha256,
                intent=intent,
                tick_owner_id=tick_owner_id,
                tick_lease_generation=tick_lease_generation,
                now=self._now_factory(),
                phase="pre_ledger",
                allow_post_cas_reconciliation=ref_updated,
                acquire_hold_on_reconciliation=ref_updated,
                ref_may_have_advanced=ref_updated,
            )
            if fence is not None:
                if fence.action == "checkpoint_aborted":
                    checkpoint_aborted = True
                else:
                    return fence
            if not checkpoint_aborted:
                self.store.complete_sequence_checkpoint_handoff(
                    conn,
                    intent=intent,
                    result=result,
                    predecessor_state=terminal_state,
                    predecessor_version=pending_version,
                    successor_submitted=submitted_state,
                    successor_authorized=authorized_state,
                    submitted_event=submitted_event,
                    authorized_event=authorized_event,
                    committed_event=committed_event,
                    handoff_event=handoff_event,
                    updated_sequence=updated_sequence,
                    event_id_factory=self._event_id_factory,
                    now=now,
                )
        if checkpoint_aborted:
            return self._finalize_checkpoint_aborted_receipt(run_id)
        self._step("after_ledger_handoff")
        return TickRunReceipt(run_id=run_id, action="sequence_handoff_completed")

    def finalize_sequence(
        self,
        conn: sqlite3.Connection,
        *,
        state: AwaitingCodexReviewState,
        version: int,
        accepted_outcome: Literal["completed", "completed_with_residual_risk"],
        review_iteration: int,
        result_path: str,
        result_sha: str,
        now: datetime,
        attempt_id: str,
    ) -> CompletedState | CompletedWithResidualRiskState:
        sequence_binding = state.context.sequence
        if sequence_binding is None:
            raise ValidationError("sequence binding required for finalization")
        sequence_state = self.store.load_validated_sequence_state(
            conn, sequence_binding.sequence_id
        )
        if not isinstance(sequence_state, ActiveSequenceState):
            raise ValidationError("sequence must be active for finalization")
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if accepted_outcome == "completed":
            completed_event = RunCompletedEvent(
                run_id=state.run_id, review_iteration=review_iteration
            )
            terminal_state: CompletedState | CompletedWithResidualRiskState = apply_run_completed(
                state, completed_event, now_text=now_text
            )
            terminal_event: SchedulerEvent = completed_event
        else:
            residual_event = RunCompletedWithResidualRiskEvent(
                run_id=state.run_id,
                review_iteration=review_iteration,
            )
            terminal_state = apply_run_completed_with_residual_risk(
                state, residual_event, now_text=now_text
            )
            terminal_event = residual_event
        terminal_state = terminal_state.model_copy(
            update={
                "codex": terminal_state.codex.model_copy(
                    update={
                        "latest_review_result_path": result_path,
                        "latest_review_result_sha256": result_sha,
                    }
                )
            }
        )
        residual_ordinals = tuple(sequence_state.residual_risk_ordinals)
        if accepted_outcome == "completed_with_residual_risk":
            residual_ordinals = tuple(sorted(set(residual_ordinals + (sequence_binding.ordinal,))))
        finalized_sequence = AwaitingFinalizationSequenceState(
            schema_version=1,
            sequence_id=sequence_state.sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            finalized_at=now_text,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            final_run_id=state.run_id,
            final_outcome=accepted_outcome,
            materialized_entries=sequence_state.materialized_entries,
            residual_risk_ordinals=residual_ordinals,
        )
        finalized_event = SequenceFinalizedEvent(
            sequence_id=sequence_binding.sequence_id,
            final_run_id=state.run_id,
            final_outcome=accepted_outcome,
        )
        event_id = self._event_id_factory()
        sequence_num = self.store.next_event_sequence(conn, state.run_id)
        self.store.append_event(
            conn,
            event_id=event_id,
            run_id=state.run_id,
            sequence=sequence_num,
            event=terminal_event,
            now=now,
        )
        if not self.store.compare_and_swap_state(
            conn,
            run_id=state.run_id,
            expected_version=version,
            new_state=terminal_state,
            now=now,
        ):
            raise ValidationError("finalization CAS lost")
        self.store.mark_attempt_ingested(conn, attempt_id=attempt_id, now=now)
        self.store.finalize_sequence_state(
            conn,
            sequence_state=sequence_state,
            finalized_state=finalized_sequence,
            finalized_event=finalized_event,
            event_id_factory=self._event_id_factory,
            now=now,
        )
        self.store.release_reservation(
            conn,
            worktree_key=terminal_state.context.repository.worktree_key,
            now=now,
        )
        return terminal_state

    def reconcile_interrupted_sequence_advancement(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        run_state: CompletedState | CompletedWithResidualRiskState,
        now: datetime,
    ) -> TickRunReceipt:
        run_id = run_state.run_id
        total = len(sequence_state.definition.entries)
        accepted_outcome: Literal["completed", "completed_with_residual_risk"] = (
            "completed_with_residual_risk"
            if isinstance(run_state, CompletedWithResidualRiskState)
            else "completed"
        )
        if sequence_state.current_ordinal == total:
            refreshed = self.store.load_validated_sequence_state(conn, sequence_state.sequence_id)
            if isinstance(refreshed, AwaitingFinalizationSequenceState):
                return TickRunReceipt(run_id=run_id, action="sequence_finalization_complete")
            if not isinstance(refreshed, ActiveSequenceState):
                return TickRunReceipt(run_id=run_id, action="sequence_finalization_state_changed")
            sequence_state = refreshed
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            residual_ordinals = tuple(sequence_state.residual_risk_ordinals)
            if accepted_outcome == "completed_with_residual_risk":
                residual_ordinals = tuple(
                    sorted(set(residual_ordinals + (sequence_state.current_ordinal,)))
                )
            finalized_sequence = AwaitingFinalizationSequenceState(
                schema_version=sequence_state.schema_version,
                sequence_id=sequence_state.sequence_id,
                version=sequence_state.version + 1,
                prepared_at=sequence_state.prepared_at,
                updated_at=now_text,
                started_at=sequence_state.started_at,
                finalized_at=now_text,
                idempotency_key=sequence_state.idempotency_key,
                definition=sequence_state.definition,
                final_run_id=run_id,
                final_outcome=accepted_outcome,
                materialized_entries=sequence_state.materialized_entries,
                residual_risk_ordinals=residual_ordinals,
            )
            finalized_event = SequenceFinalizedEvent(
                sequence_id=sequence_state.sequence_id,
                final_run_id=run_id,
                final_outcome=accepted_outcome,
            )
            return self._reconcile_interrupted_finalization_on_savepoint(
                conn,
                sequence_state=sequence_state,
                run_state=run_state,
                finalized_sequence=finalized_sequence,
                finalized_event=finalized_event,
                now=now,
            )
        hold_receipt = self._reconcile_terminal_checkpoint_hold_for_restart(
            conn,
            run_id=run_id,
            now=now,
        )
        if hold_receipt is not None:
            return hold_receipt
        intent_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
        result_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
        if not intent_path.is_file() or not result_path.is_file():
            return TickRunReceipt(run_id=run_id, action="sequence_handoff_artifacts_missing")
        try:
            intent = SequenceCheckpointIntent.model_validate_json(intent_path.read_bytes())
            result = SequenceCheckpointResult.model_validate_json(result_path.read_bytes())
        except (PydanticValidationError, OSError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.predecessor_run_id != run_id:
            return TickRunReceipt(run_id=run_id, action="sequence_handoff_intent_mismatch")
        auth_failure = self._authenticate_terminal_checkpoint_for_replay(
            conn,
            run_id=run_id,
            sequence_state=sequence_state,
            run_state=run_state,
            intent=intent,
            result=result,
        )
        if auth_failure is not None:
            return auth_failure
        return self._replay_handoff_from_terminal_predecessor(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            intent=intent,
            result=result,
            now=now,
        )

    def _reconcile_interrupted_finalization_on_savepoint(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        run_state: CompletedState | CompletedWithResidualRiskState,
        finalized_sequence: AwaitingFinalizationSequenceState,
        finalized_event: SequenceFinalizedEvent,
        now: datetime,
    ) -> TickRunReceipt:
        from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

        run_id = run_state.run_id
        conn.execute("SAVEPOINT sequence_finalization_replay")
        try:
            self.store.finalize_sequence_state(
                conn,
                sequence_state=sequence_state,
                finalized_state=finalized_sequence,
                finalized_event=finalized_event,
                event_id_factory=self._event_id_factory,
                now=now,
            )
            self.store.release_reservation(
                conn,
                worktree_key=run_state.context.repository.worktree_key,
                now=now,
            )
            conn.execute("RELEASE sequence_finalization_replay")
            return TickRunReceipt(run_id=run_id, action="sequence_finalization_reconciled")
        except SchedulerEngineError:
            conn.execute("ROLLBACK TO sequence_finalization_replay")
            conn.execute("RELEASE sequence_finalization_replay")
            again = self.store.load_validated_sequence_state(conn, sequence_state.sequence_id)
            if isinstance(again, AwaitingFinalizationSequenceState):
                return TickRunReceipt(run_id=run_id, action="sequence_finalization_complete")
            return TickRunReceipt(run_id=run_id, action="sequence_finalization_cas_lost")

    def _reconcile_terminal_checkpoint_hold_for_restart(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        now: datetime,
    ) -> TickRunReceipt | None:
        if not self.store.has_checkpoint_reconciliation_hold(conn, run_id):
            return None
        intent_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
        result_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
        if not intent_path.is_file() or not result_path.is_file():
            return TickRunReceipt(run_id=run_id, action="sequence_handoff_hold_pending")
        from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
            ProtectedArtifactError,
        )

        try:
            trusted = self._load_trusted_tree(run_id, expected_sha256=None)
        except (PydanticValidationError, OSError, ProtectedArtifactError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if trusted is None:
            return TickRunReceipt(run_id=run_id, action="sequence_handoff_hold_pending")
        return None

    def _authenticate_terminal_checkpoint_for_replay(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        sequence_state: ActiveSequenceState,
        run_state: CompletedState | CompletedWithResidualRiskState,
        intent: SequenceCheckpointIntent,
        result: SequenceCheckpointResult,
    ) -> TickRunReceipt | None:
        if sequence_state.current_run_id != run_id:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        authorization = self.store.load_authoritative_sequence_checkpoint_requested_event(
            conn,
            run_id=run_id,
        )
        if authorization is None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if authorization.sequence_id != sequence_state.sequence_id:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if authorization.review_iteration != run_state.codex.review_iteration:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if authorization.accepted_outcome != intent.accepted_outcome:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.sequence_id != sequence_state.sequence_id:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.predecessor_ordinal != sequence_state.current_ordinal:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.sequence_version != sequence_state.version:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.successor_ordinal != sequence_state.current_ordinal + 1:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.accepted_outcome == "completed" and not isinstance(run_state, CompletedState):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if intent.accepted_outcome == "completed_with_residual_risk" and not isinstance(
            run_state, CompletedWithResidualRiskState
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        materialized = next(
            (
                entry
                for entry in sequence_state.materialized_entries
                if entry.ordinal == intent.predecessor_ordinal
            ),
            None,
        )
        if materialized is None or materialized.run_id != run_id:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        successor_planned = sequence_state.definition.entries[
            intent.successor_ordinal - 1
        ].planned_run_id
        if intent.successor_run_id != successor_planned:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        try:
            from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
                ProtectedArtifactError,
            )

            identity = frozen_repository_identity(
                run_state.context,
                run_id=run_id,
                artifacts=self.artifacts,
                checkpoint=run_state.checkpoint,
            )
        except (ValidationError, OSError, ProtectedArtifactError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if (
            intent.repository_root != identity.root
            or intent.git_common_dir != identity.git_common_dir
            or intent.git_dir != identity.git_dir
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        patch_rel = run_state.cursor.staged_patch_path
        patch_sha256 = run_state.cursor.staged_patch_sha256
        if patch_rel is None or patch_sha256 is None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if (
            intent.reviewed_patch_sha256 != patch_sha256
            or intent.staged_patch_artifact_path != patch_rel
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        review_path = run_state.codex.latest_review_result_path
        review_sha = run_state.codex.latest_review_result_sha256
        if review_path is None or review_sha is None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if (
            intent.review_result_artifact_path != review_path
            or intent.review_result_sha256 != review_sha
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        try:
            self.artifacts.read_verified_bytes(
                run_id,
                patch_rel,
                expected_sha256=patch_sha256,
            )
            self.artifacts.read_verified_bytes(
                run_id,
                review_path,
                expected_sha256=review_sha,
            )
        except (PydanticValidationError, OSError, ProtectedArtifactError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        intent_sha256 = authorization.checkpoint_intent_sha256
        trusted_tree_file_sha256 = authorization.checkpoint_trusted_tree_sha256
        try:
            self.artifacts.read_verified_bytes(
                run_id,
                authorization.checkpoint_intent_artifact_path,
                expected_sha256=intent_sha256,
            )
        except (PydanticValidationError, OSError, ProtectedArtifactError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if result.trusted_tree_sha256 != trusted_tree_file_sha256:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        try:
            trusted_tree = self._load_trusted_tree(
                run_id,
                expected_sha256=trusted_tree_file_sha256,
            )
        except (PydanticValidationError, OSError, ProtectedArtifactError):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if trusted_tree is None:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        if not self._verify_checkpoint_result(
            intent,
            result,
            intent_sha256,
            trusted_tree_sha256=trusted_tree_file_sha256,
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        try:
            authenticate_checkpoint_result_bindings(
                result=result,
                intent=intent,
                trusted_tree=trusted_tree,
                intent_sha256=intent_sha256,
                trusted_tree_file_sha256=trusted_tree_file_sha256,
                run_id=run_id,
                materialized=materialized,
            )
            verify_checkpoint_commit_in_repository(
                intent=intent,
                trusted_tree=trusted_tree,
                commit_sha256=result.commit_sha256,
                require_head_match=True,
            )
        except SequenceCheckpointEvidenceError:
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        predecessor_entry = sequence_state.definition.entries[intent.predecessor_ordinal - 1]
        if (
            predecessor_entry.commit_message is not None
            and intent.commit_message != predecessor_entry.commit_message
        ):
            return TickRunReceipt(run_id=run_id, action="checkpoint_result_invalid")
        return None

    def _replay_handoff_from_terminal_predecessor(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        run_state: CompletedState | CompletedWithResidualRiskState,
        intent: SequenceCheckpointIntent,
        result: SequenceCheckpointResult,
        now: datetime,
    ) -> TickRunReceipt:
        from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

        run_id = run_state.run_id
        conn.execute("SAVEPOINT sequence_checkpoint_replay")
        try:
            return self._replay_handoff_from_terminal_predecessor_on_savepoint(
                conn,
                sequence_state=sequence_state,
                run_state=run_state,
                intent=intent,
                result=result,
                now=now,
            )
        except SchedulerEngineError:
            conn.execute("ROLLBACK TO sequence_checkpoint_replay")
            conn.execute("RELEASE sequence_checkpoint_replay")
            sequence_id = intent.sequence_id
            again = self.store.load_validated_sequence_state(conn, sequence_id)
            successor_run_id = intent.successor_run_id
            if isinstance(again, ActiveSequenceState) and again.current_run_id == successor_run_id:
                return TickRunReceipt(run_id=run_id, action="sequence_handoff_complete")
            return TickRunReceipt(run_id=run_id, action="sequence_handoff_reconcile_failed")

    def _replay_handoff_from_terminal_predecessor_on_savepoint(
        self,
        conn: sqlite3.Connection,
        *,
        sequence_state: ActiveSequenceState,
        run_state: CompletedState | CompletedWithResidualRiskState,
        intent: SequenceCheckpointIntent,
        result: SequenceCheckpointResult,
        now: datetime,
    ) -> TickRunReceipt:
        run_id = run_state.run_id
        _, predecessor_version, _ = self.store.load_validated_snapshot(conn, run_id)
        sequence_id = intent.sequence_id
        refreshed = self.store.load_validated_sequence_state(conn, sequence_id)
        if not isinstance(refreshed, ActiveSequenceState):
            conn.execute("RELEASE sequence_checkpoint_replay")
            return TickRunReceipt(run_id=run_id, action="sequence_not_active")
        sequence_state = refreshed
        successor_entry = sequence_state.definition.entries[intent.successor_ordinal - 1]
        context, entry_hash = self._materializer.materialize_next_entry(
            sequence_id=sequence_id,
            definition=sequence_state.definition,
            entry=successor_entry,
        )
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        successor_run_id = intent.successor_run_id
        idempotency_key = sequence_run_idempotency_key(context)
        submitted_state = SubmittedState(
            run_id=successor_run_id,
            version=1,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=idempotency_key,
            context=context,
        )
        submitted_event = RunSubmittedEvent(
            run_id=successor_run_id,
            idempotency_key=idempotency_key,
            worktree_key=context.repository.worktree_key,
            reused_existing=False,
        )
        authorized_event = RunAuthorizedEvent(
            run_id=successor_run_id,
            controller_session_id=context.controller.controller_session_id,
            idempotent_replay=False,
        )
        authorized_state = apply_run_authorized(
            submitted_state, authorized_event, now_text=now_text
        )
        residual_ordinals = tuple(sequence_state.residual_risk_ordinals)
        if intent.accepted_outcome == "completed_with_residual_risk":
            residual_ordinals = tuple(
                sorted(set(residual_ordinals + (intent.predecessor_ordinal,)))
            )
        updated_sequence = ActiveSequenceState(
            schema_version=sequence_state.schema_version,
            sequence_id=sequence_state.sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            current_ordinal=intent.successor_ordinal,
            current_run_id=successor_run_id,
            materialized_entries=(
                *sequence_state.materialized_entries,
                MaterializedSequenceEntry(
                    ordinal=intent.successor_ordinal,
                    run_id=successor_run_id,
                    entry_hash=entry_hash,
                    materialized_at=now_text,
                ),
            ),
            residual_risk_ordinals=residual_ordinals,
        )
        committed_event = SequenceCheckpointCommittedEvent(
            run_id=run_id,
            sequence_id=sequence_id,
            commit_sha256_prefix=result.commit_sha256[:12],
            tree_sha256_prefix=result.tree_sha256[:12],
        )
        handoff_event = SequenceHandoffCompletedEvent(
            predecessor_run_id=run_id,
            successor_run_id=successor_run_id,
            sequence_id=sequence_id,
            predecessor_ordinal=intent.predecessor_ordinal,
            successor_ordinal=intent.successor_ordinal,
        )
        self.store.complete_sequence_checkpoint_handoff(
            conn,
            intent=intent,
            result=result,
            predecessor_state=run_state,
            predecessor_version=predecessor_version,
            successor_submitted=submitted_state,
            successor_authorized=authorized_state,
            submitted_event=submitted_event,
            authorized_event=authorized_event,
            committed_event=committed_event,
            handoff_event=handoff_event,
            updated_sequence=updated_sequence,
            event_id_factory=self._event_id_factory,
            now=now,
        )
        intent_path = self.artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT
        intent_sha256 = hashlib.sha256(intent_path.read_bytes()).hexdigest()
        if self.store.has_checkpoint_reconciliation_hold(conn, run_id):
            self.store.release_checkpoint_reconciliation_hold(
                conn,
                run_id=run_id,
                intent_sha256=intent_sha256,
            )
        conn.execute("RELEASE sequence_checkpoint_replay")
        return TickRunReceipt(run_id=run_id, action="sequence_handoff_reconciled")
