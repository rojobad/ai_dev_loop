"""Target integration for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import (
    CHECKPOINT_GIT_TIMEOUT_SECONDS,
    GitIdentity,
    checkpoint_git_commit_tree,
    checkpoint_git_diff_cached_patch_bytes,
    checkpoint_git_rev_parse,
    checkpoint_git_status_porcelain,
    checkpoint_git_update_ref_cas,
    checkpoint_git_write_tree,
    checkpoint_validate_repository_layout,
    checkpoint_validate_staged_patch_matches_artifact,
    checkpoint_verify_commit_identity,
    paths_with_unstaged_changes,
    paths_with_untracked,
    recovery_git_apply_staged_patch,
    recovery_git_apply_worktree_patch,
    recovery_git_checkout_detach,
    recovery_git_private_ref_peek,
    recovery_git_update_private_ref_cas,
)
from ai_dev_loop.scheduler.application.checkpoint_abort_coordination import (
    CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
)
from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.git_admission import discover_repository_bounded
from ai_dev_loop.scheduler.application.git_checkpoint import (
    GitCheckpointPort,
    ProductionGitCheckpointPort,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    load_recovery_definition,
    load_recovery_integration_intent,
    load_recovery_integration_result,
    load_recovery_integration_trusted_tree,
    load_recovery_seed_evidence,
)
from ai_dev_loop.scheduler.application.recovery_integration_fencing import (
    RecoveryIntegrationTickContext,
    assert_recovery_mutation_allowed,
    recovery_git_deadline,
)
from ai_dev_loop.scheduler.application.recovery_worktree import RecoverySeedEvidence
from ai_dev_loop.scheduler.application.sequence_materializer import (
    SequenceRunMaterializer,
    sequence_run_idempotency_key,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    GitIdentitySnapshot,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.events import RunAuthorizedEvent, RunSubmittedEvent
from ai_dev_loop.scheduler.domain.recovery import (
    CLEANUP_PENDING_RECOVERY_STATE_KIND,
    INTEGRATION_PENDING_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT,
    RECOVERY_INTEGRATION_RESULT_ARTIFACT,
    RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
    CleanupPendingRecoveryState,
    FreshReviewRecoveryDefinition,
    IntegrationPendingRecoveryState,
    RecoveryCheckpointTrustedTree,
    RecoveryIntegrationIntent,
    RecoveryIntegrationPublicationInputs,
    SequenceRecoveryResolution,
)
from ai_dev_loop.scheduler.domain.reducer import apply_run_authorized
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    RecoveryIntegratedFinalizationSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    CompletedState,
    CompletedWithResidualRiskState,
    SubmittedState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now

_integration_publication_step_hook: Callable[[str], None] | None = None


def set_recovery_integration_publication_step_hook(
    hook: Callable[[str], None] | None,
) -> None:
    """Test hook for deterministic integration artifact publication interruption points."""
    global _integration_publication_step_hook
    _integration_publication_step_hook = hook


def _integration_publication_step(name: str) -> None:
    if _integration_publication_step_hook is not None:
        _integration_publication_step_hook(name)


def _tree_delta_patch(
    source_tree: str,
    accepted_tree: str,
    repo_root: Path,
    *,
    timeout: float = CHECKPOINT_GIT_TIMEOUT_SECONDS,
) -> bytes | None:
    if source_tree == accepted_tree:
        return None
    from ai_dev_loop.process import run_process_bytes

    result = run_process_bytes(
        ["git", "diff", "--binary", source_tree, accepted_tree],
        cwd=str(repo_root),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "failed to compute source-to-accepted tree delta",
        )
    return result.stdout


def _integration_intent_bytes(intent: RecoveryIntegrationIntent) -> bytes:
    return json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True).encode("utf-8")


def _integration_intent_sha256_from_bytes(intent_bytes: bytes) -> str:
    return hashlib.sha256(intent_bytes).hexdigest()


def _intent_bound_git_identity(intent: RecoveryIntegrationIntent) -> GitIdentity:
    from ai_dev_loop.runners.git import git_timestamp_from_intent_date

    return GitIdentity(
        author_name=intent.git_identity.author_name,
        author_email=intent.git_identity.author_email,
        author_date=git_timestamp_from_intent_date(intent.git_identity.author_date),
        committer_name=intent.git_identity.committer_name,
        committer_email=intent.git_identity.committer_email,
        committer_date=git_timestamp_from_intent_date(intent.git_identity.committer_date),
    )


def _git_identity_from_commit_object(repo_root: Path, commit_sha: str) -> GitIdentity:
    from ai_dev_loop.runners.git import _checkpoint_git_success, _parse_git_ident_line

    raw_commit = _checkpoint_git_success(
        ["cat-file", "-p", commit_sha],
        cwd=repo_root,
        context=f"git cat-file -p {commit_sha}",
    )
    lines = raw_commit.splitlines()
    if len(lines) < 4:
        raise ValidationError("commit object payload is truncated")
    author_name, author_email, author_date = _parse_git_ident_line(lines[2].removeprefix("author "))
    committer_name, committer_email, committer_date = _parse_git_ident_line(
        lines[3].removeprefix("committer ")
    )
    return GitIdentity(
        author_name=author_name,
        author_email=author_email,
        author_date=author_date,
        committer_name=committer_name,
        committer_email=committer_email,
        committer_date=committer_date,
    )


def _verify_recovery_commit_against_intent(
    target_root: Path,
    *,
    intent: RecoveryIntegrationIntent,
    trusted: RecoveryCheckpointTrustedTree,
    commit_sha: str,
) -> bool:
    try:
        commit_identity = _git_identity_from_commit_object(target_root, commit_sha)
        intent_identity = _intent_bound_git_identity(intent)
    except ValidationError:
        return False
    if (
        commit_identity.author_name != intent_identity.author_name
        or commit_identity.author_email != intent_identity.author_email
        or commit_identity.committer_name != intent_identity.committer_name
        or commit_identity.committer_email != intent_identity.committer_email
        or commit_identity.author_date != intent_identity.author_date
        or commit_identity.committer_date != intent_identity.committer_date
    ):
        return False
    try:
        checkpoint_verify_commit_identity(
            target_root,
            commit_sha=commit_sha,
            tree_sha=trusted.reviewed_tree_sha256,
            parent_sha=intent.parent_head,
            message=intent.commit_message,
            identity=intent_identity,
        )
    except ValidationError:
        return False
    return True


@dataclass(frozen=True)
class IntegrationReplayClassification:
    target_commit_sha: str | None = None
    skip_frozen_source_checks: bool = False
    managed_side_complete: bool = False


@dataclass(frozen=True)
class AuthenticatedIntegrationBindings:
    intent: RecoveryIntegrationIntent
    trusted: RecoveryCheckpointTrustedTree
    intent_artifact_sha256: str
    trusted_artifact_sha256: str
    accepted_tree_sha256: str
    reviewed_patch_sha256: str


class RecoveryIntegrationService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        git_checkpoint: GitCheckpointPort | None = None,
        now_factory: Callable[[], datetime] | None = None,
        tick_context: RecoveryIntegrationTickContext | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._git_checkpoint = git_checkpoint or ProductionGitCheckpointPort()
        self._now_factory = now_factory or (lambda: utc_now())
        self._tick_context = tick_context

    def _classify_integration_replay(
        self,
        recovery_id: str,
        definition: object,
    ) -> IntegrationReplayClassification:
        from ai_dev_loop.scheduler.domain.recovery import FreshReviewRecoveryDefinition

        assert isinstance(definition, FreshReviewRecoveryDefinition)
        evidence = self._load_integration_evidence(recovery_id)
        target_root = Path(definition.repository.root)
        commit_sha = evidence.get("commit_sha256")
        if commit_sha:
            branch_head = checkpoint_git_rev_parse(target_root, definition.target_branch_ref)
            if branch_head == commit_sha:
                return IntegrationReplayClassification(
                    target_commit_sha=commit_sha,
                    skip_frozen_source_checks=True,
                    managed_side_complete=True,
                )
        managed_commit = evidence.get("managed_commit_sha256")
        if managed_commit:
            private_ref_sha = recovery_git_private_ref_peek(target_root, ref=definition.private_ref)
            branch_head = checkpoint_git_rev_parse(target_root, definition.target_branch_ref)
            managed_side_complete = private_ref_sha == managed_commit
            if branch_head == managed_commit and managed_side_complete:
                return IntegrationReplayClassification(
                    target_commit_sha=managed_commit,
                    skip_frozen_source_checks=True,
                    managed_side_complete=True,
                )
            if evidence.get("delta_applied") == "true" and branch_head != definition.parent_head:
                return IntegrationReplayClassification(
                    skip_frozen_source_checks=True,
                    managed_side_complete=managed_side_complete,
                )
            if managed_side_complete:
                return IntegrationReplayClassification(managed_side_complete=True)
        return IntegrationReplayClassification()

    def _load_authenticated_integration_bindings(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
    ) -> AuthenticatedIntegrationBindings:
        intent_artifact_sha256 = pending_state.integration_intent_artifact_sha256
        trusted_artifact_sha256 = pending_state.integration_trusted_tree_artifact_sha256
        if intent_artifact_sha256 is None or trusted_artifact_sha256 is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery integration pending lacks durable artifact bindings",
            )
        intent_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
        trusted_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT
        )
        if not intent_path.is_file() or not trusted_path.is_file():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery integration requires authenticated intent and trusted-tree artifacts",
            )
        self.artifacts.read_verified_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_INTENT_ARTIFACT,
            expected_sha256=intent_artifact_sha256,
        )
        intent = load_recovery_integration_intent(
            self.artifacts,
            recovery_id,
            expected_sha256=intent_artifact_sha256,
        )
        trusted, _ = load_recovery_integration_trusted_tree(
            self.artifacts,
            recovery_id,
            expected_sha256=trusted_artifact_sha256,
        )
        if trusted.intent_sha256 != intent_artifact_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery trusted-tree intent binding mismatch",
            )
        if pending_state.seed_evidence_artifact_sha256 is not None:
            load_recovery_seed_evidence(
                self.artifacts,
                recovery_id,
                definition=load_recovery_definition(
                    self.artifacts,
                    recovery_id,
                    definition_sha256=pending_state.definition_sha256,
                    definition_artifact_sha256=pending_state.definition_artifact_sha256,
                ),
                expected_sha256=pending_state.seed_evidence_artifact_sha256,
            )
        return AuthenticatedIntegrationBindings(
            intent=intent,
            trusted=trusted,
            intent_artifact_sha256=intent_artifact_sha256,
            trusted_artifact_sha256=trusted_artifact_sha256,
            accepted_tree_sha256=trusted.reviewed_tree_sha256,
            reviewed_patch_sha256=trusted.reviewed_patch_sha256,
        )

    def _persist_integration_artifact_bindings(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        *,
        intent_artifact_sha256: str | None = None,
        trusted_artifact_sha256: str | None = None,
        seed_evidence_artifact_sha256: str | None = None,
    ) -> IntegrationPendingRecoveryState:
        updates: dict[str, object] = {}
        if intent_artifact_sha256 is not None:
            updates["integration_intent_artifact_sha256"] = intent_artifact_sha256
        if trusted_artifact_sha256 is not None:
            updates["integration_trusted_tree_artifact_sha256"] = trusted_artifact_sha256
        if seed_evidence_artifact_sha256 is not None:
            updates["seed_evidence_artifact_sha256"] = seed_evidence_artifact_sha256
        if not updates:
            return pending_state
        next_intent = updates.get(
            "integration_intent_artifact_sha256",
            pending_state.integration_intent_artifact_sha256,
        )
        next_trusted = updates.get(
            "integration_trusted_tree_artifact_sha256",
            pending_state.integration_trusted_tree_artifact_sha256,
        )
        next_seed = updates.get(
            "seed_evidence_artifact_sha256",
            pending_state.seed_evidence_artifact_sha256,
        )
        if (
            pending_state.integration_intent_artifact_sha256 == next_intent
            and pending_state.integration_trusted_tree_artifact_sha256 == next_trusted
            and pending_state.seed_evidence_artifact_sha256 == next_seed
        ):
            return pending_state
        now = self._now_factory()
        updated = pending_state.model_copy(update=updates)
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=updated,
                expected_version=pending_state.version,
                now=now,
            )
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found after binding persistence",
            )
        return IntegrationPendingRecoveryState.model_validate_json(str(row["state_payload"]))

    def _resolve_integration_patch_paths(
        self,
        recovery_id: str,
        definition: FreshReviewRecoveryDefinition,
        intent: RecoveryIntegrationIntent,
        *,
        reviewed_patch_sha256: str,
    ) -> tuple[Path, Path]:
        self.artifacts.read_verified_bytes(
            recovery_id,
            definition.source_staged_patch_path,
            expected_sha256=definition.source_staged_patch_sha256,
        )
        source_patch_path = (
            self.artifacts.run_root(recovery_id) / definition.source_staged_patch_path
        )
        recovery_run_id = intent.recovery_run_id
        with self.store.begin_read() as conn:
            run_state, _, _ = self.store.load_validated_snapshot(conn, recovery_run_id)
        if not isinstance(run_state, (CompletedState, CompletedWithResidualRiskState)):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run is not in an accepted completion state",
            )
        reviewed_patch_rel = run_state.cursor.staged_patch_path
        run_reviewed_patch_sha = run_state.cursor.staged_patch_sha256
        if not reviewed_patch_rel or not run_reviewed_patch_sha:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run lacks staged patch binding",
            )
        if run_reviewed_patch_sha != reviewed_patch_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run patch binding disagrees with authenticated intent",
            )
        if reviewed_patch_sha256 != intent.reviewed_patch_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "authenticated reviewed patch disagrees with integration intent",
            )
        self.artifacts.read_verified_bytes(
            recovery_run_id,
            reviewed_patch_rel,
            expected_sha256=reviewed_patch_sha256,
        )
        accepted_patch_path = self.artifacts.run_root(recovery_run_id) / reviewed_patch_rel
        return accepted_patch_path, source_patch_path

    def _publish_integration_intent(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        intent: RecoveryIntegrationIntent,
    ) -> tuple[RecoveryIntegrationIntent, str, IntegrationPendingRecoveryState]:
        intent_bytes = _integration_intent_bytes(intent)
        frozen_sha = _integration_intent_sha256_from_bytes(intent_bytes)
        intent_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
        if pending_state.integration_intent_artifact_sha256 is not None:
            bound_sha = pending_state.integration_intent_artifact_sha256
            if frozen_sha != bound_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "integration intent reconstruction disagrees with durable binding",
                )
            if not intent_path.is_file():
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "integration intent artifact missing for bound digest",
                )
            self.artifacts.read_verified_bytes(
                recovery_id,
                RECOVERY_INTEGRATION_INTENT_ARTIFACT,
                expected_sha256=bound_sha,
            )
            loaded = load_recovery_integration_intent(
                self.artifacts,
                recovery_id,
                expected_sha256=bound_sha,
            )
            return loaded, bound_sha, pending_state
        if intent_path.is_file():
            existing_bytes = intent_path.read_bytes()
            existing_sha = _integration_intent_sha256_from_bytes(existing_bytes)
            if existing_sha != frozen_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "immutable integration intent artifact conflict",
                )
            _integration_publication_step("after_intent_write")
            pending_state = self._persist_integration_artifact_bindings(
                recovery_id,
                pending_state,
                intent_artifact_sha256=existing_sha,
            )
            _integration_publication_step("after_intent_binding")
            loaded = load_recovery_integration_intent(
                self.artifacts,
                recovery_id,
                expected_sha256=existing_sha,
            )
            return loaded, existing_sha, pending_state
        stored = self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_INTENT_ARTIFACT,
            intent_bytes,
            max_bytes=len(intent_bytes) + 1,
        )
        _integration_publication_step("after_intent_write")
        pending_state = self._persist_integration_artifact_bindings(
            recovery_id,
            pending_state,
            intent_artifact_sha256=stored.sha256,
        )
        _integration_publication_step("after_intent_binding")
        return intent, stored.sha256, pending_state

    def _publish_integration_trusted_tree(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        trusted: RecoveryCheckpointTrustedTree,
        *,
        intent_sha256: str,
    ) -> tuple[RecoveryCheckpointTrustedTree, str, IntegrationPendingRecoveryState]:
        trusted_bytes = json.dumps(
            trusted.model_dump(mode="json"), indent=2, sort_keys=True
        ).encode("utf-8")
        frozen_sha = hashlib.sha256(trusted_bytes).hexdigest()
        trusted_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT
        )
        if trusted.intent_sha256 != intent_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "integration trusted-tree intent binding mismatch before publication",
            )
        if pending_state.integration_trusted_tree_artifact_sha256 is not None:
            bound_sha = pending_state.integration_trusted_tree_artifact_sha256
            if frozen_sha != bound_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "integration trusted-tree reconstruction disagrees with durable binding",
                )
            if not trusted_path.is_file():
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "integration trusted-tree artifact missing for bound digest",
                )
            loaded, _ = load_recovery_integration_trusted_tree(
                self.artifacts,
                recovery_id,
                expected_sha256=bound_sha,
            )
            return loaded, bound_sha, pending_state
        if trusted_path.is_file():
            existing_bytes = trusted_path.read_bytes()
            existing_sha = hashlib.sha256(existing_bytes).hexdigest()
            if existing_sha != frozen_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "immutable integration trusted-tree artifact conflict",
                )
            _integration_publication_step("after_trusted_write")
            pending_state = self._persist_integration_artifact_bindings(
                recovery_id,
                pending_state,
                trusted_artifact_sha256=existing_sha,
            )
            _integration_publication_step("after_trusted_binding")
            loaded, _ = load_recovery_integration_trusted_tree(
                self.artifacts,
                recovery_id,
                expected_sha256=existing_sha,
            )
            return loaded, existing_sha, pending_state
        stored = self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT,
            trusted_bytes,
            max_bytes=len(trusted_bytes) + 1,
        )
        _integration_publication_step("after_trusted_write")
        pending_state = self._persist_integration_artifact_bindings(
            recovery_id,
            pending_state,
            trusted_artifact_sha256=stored.sha256,
        )
        _integration_publication_step("after_trusted_binding")
        return trusted, stored.sha256, pending_state

    def _complete_proven_target_cas_replay(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        definition: FreshReviewRecoveryDefinition,
        *,
        commit_sha: str,
    ) -> CleanupPendingRecoveryState:
        bindings = self._load_authenticated_integration_bindings(recovery_id, pending_state)
        target_root = Path(definition.repository.root)
        evidence = self._load_integration_evidence(recovery_id)
        evidence_state = dict(evidence)
        evidence_state["commit_sha256"] = commit_sha
        reconciled = self._reconcile_standalone_target_cas_if_proven(
            intent=bindings.intent,
            intent_sha256=bindings.intent_artifact_sha256,
            trusted=bindings.trusted,
            target_root=target_root,
            source_run_id=definition.source_run_id,
            evidence=evidence_state,
        )
        if reconciled is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "proven target CAS lacks matching commit/tree/worktree postconditions",
            )
        return self._finish_integration_from_commit(
            recovery_id=recovery_id,
            pending_state=pending_state,
            definition=definition,
            commit_sha=reconciled,
            accepted_tree=bindings.accepted_tree_sha256,
            reviewed_patch_sha=bindings.reviewed_patch_sha256,
            intent_sha256=bindings.intent_artifact_sha256,
            intent_artifact_sha256=bindings.intent_artifact_sha256,
            trusted_artifact_sha256=bindings.trusted_artifact_sha256,
        )

    def _resume_integration_from_persisted_intent(
        self,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        definition: object,
        *,
        skip_frozen_source_checks: bool,
    ) -> CleanupPendingRecoveryState:
        assert isinstance(definition, FreshReviewRecoveryDefinition)
        bindings = self._load_authenticated_integration_bindings(recovery_id, pending_state)
        intent = bindings.intent
        trusted = bindings.trusted
        patch_path, source_patch_path = self._resolve_integration_patch_paths(
            recovery_id,
            definition,
            intent,
            reviewed_patch_sha256=bindings.reviewed_patch_sha256,
        )
        seed = load_recovery_seed_evidence(
            self.artifacts,
            recovery_id,
            definition=definition,
            expected_sha256=pending_state.seed_evidence_artifact_sha256,
        )
        managed_root = Path(seed.worktree_path)
        if not managed_root.is_dir():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed recovery worktree is missing",
            )
        commit_sha = self._integrate_target(
            intent=intent,
            intent_sha256=bindings.intent_artifact_sha256,
            trusted=trusted,
            patch_path=patch_path,
            source_patch_path=source_patch_path,
            source_staged_patch_sha256=definition.source_staged_patch_sha256,
            managed_root=managed_root,
            definition_parent_head=definition.parent_head,
            source_run_id=definition.source_run_id,
            target_worktree_key=definition.repository.worktree_key,
            sequence_is_final=definition.sequence.is_final_phase if definition.sequence else True,
            recovery_version=pending_state.version,
            skip_frozen_source_checks=skip_frozen_source_checks,
        )
        return self._finish_integration_from_commit(
            recovery_id=recovery_id,
            pending_state=pending_state,
            definition=definition,
            commit_sha=commit_sha,
            accepted_tree=bindings.accepted_tree_sha256,
            reviewed_patch_sha=bindings.reviewed_patch_sha256,
            intent_sha256=bindings.intent_artifact_sha256,
            intent_artifact_sha256=bindings.intent_artifact_sha256,
            trusted_artifact_sha256=bindings.trusted_artifact_sha256,
        )

    def complete_proven_integration_reconciliation(
        self,
        recovery_id: str,
    ) -> CleanupPendingRecoveryState:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found",
            )
        state_kind = str(row["state_kind"])
        if state_kind == CLEANUP_PENDING_RECOVERY_STATE_KIND:
            return CleanupPendingRecoveryState.model_validate_json(str(row["state_payload"]))
        if state_kind != INTEGRATION_PENDING_RECOVERY_STATE_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "proven integration reconciliation requires integration_pending state",
            )
        pending_state = IntegrationPendingRecoveryState.model_validate_json(
            str(row["state_payload"])
        )
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=pending_state.definition_sha256,
            definition_artifact_sha256=pending_state.definition_artifact_sha256,
        )
        replay = self._classify_integration_replay(recovery_id, definition)
        result_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_RESULT_ARTIFACT
        if result_path.is_file():
            return self._resume_proven_integration(
                recovery_id=recovery_id,
                pending_state=pending_state,
                definition=definition,
            )
        if replay.target_commit_sha is not None:
            return self._complete_proven_target_cas_replay(
                recovery_id,
                pending_state,
                definition,
                commit_sha=replay.target_commit_sha,
            )
        if replay.managed_side_complete or replay.skip_frozen_source_checks:
            return self._resume_integration_from_persisted_intent(
                recovery_id,
                pending_state,
                definition,
                skip_frozen_source_checks=replay.skip_frozen_source_checks
                or replay.managed_side_complete,
            )
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "proven integration reconciliation lacks authenticated target commit",
        )

    def integrate_recovery_pending(self, recovery_id: str) -> CleanupPendingRecoveryState:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found",
            )
        pending_state = IntegrationPendingRecoveryState.model_validate_json(
            str(row["state_payload"])
        )
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=pending_state.definition_sha256,
            definition_artifact_sha256=pending_state.definition_artifact_sha256,
        )
        recovery_run_id = pending_state.recovery_run_id
        result_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_RESULT_ARTIFACT
        if result_path.is_file():
            return self._resume_proven_integration(
                recovery_id=recovery_id,
                pending_state=pending_state,
                definition=definition,
            )

        replay = self._classify_integration_replay(recovery_id, definition)
        if replay.target_commit_sha is not None:
            return self._complete_proven_target_cas_replay(
                recovery_id,
                pending_state,
                definition,
                commit_sha=replay.target_commit_sha,
            )
        if replay.managed_side_complete or replay.skip_frozen_source_checks:
            intent_path = (
                self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
            )
            trusted_path = (
                self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_TRUSTED_TREE_ARTIFACT
            )
            if intent_path.is_file() and trusted_path.is_file():
                return self._resume_integration_from_persisted_intent(
                    recovery_id,
                    pending_state,
                    definition,
                    skip_frozen_source_checks=replay.skip_frozen_source_checks
                    or replay.managed_side_complete,
                )

        with self.store.begin_read() as conn:
            run_state, _, _ = self.store.load_validated_snapshot(conn, recovery_run_id)
        if not isinstance(run_state, (CompletedState, CompletedWithResidualRiskState)):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run is not in an accepted completion state",
            )
        reviewed_patch_path = run_state.cursor.staged_patch_path
        reviewed_patch_sha = run_state.cursor.staged_patch_sha256
        review_result_path = run_state.codex.latest_review_result_path
        review_result_sha = run_state.codex.latest_review_result_sha256
        if not reviewed_patch_path or not reviewed_patch_sha:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run lacks staged patch binding",
            )
        if not review_result_path or not review_result_sha:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery run lacks review result binding",
            )
        self.artifacts.read_verified_bytes(
            recovery_run_id,
            review_result_path,
            expected_sha256=review_result_sha,
        )
        patch_path = self.artifacts.run_root(recovery_run_id) / reviewed_patch_path
        self.artifacts.read_verified_bytes(
            recovery_run_id,
            reviewed_patch_path,
            expected_sha256=reviewed_patch_sha,
        )

        seed = load_recovery_seed_evidence(
            self.artifacts,
            recovery_id,
            definition=definition,
            expected_sha256=pending_state.seed_evidence_artifact_sha256,
        )
        managed_root = Path(seed.worktree_path)
        if not managed_root.is_dir():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed recovery worktree is missing",
            )
        self._authenticate_managed_repository(seed, managed_root)
        accepted_tree = self._measure_managed_accepted_tree(
            managed_root,
            reviewed_patch_sha=reviewed_patch_sha,
            patch_path=patch_path,
            expected_tree_sha=definition.source_staged_tree_sha256,
        )
        accepted_outcome: Literal["completed", "completed_with_residual_risk"] = (
            pending_state.accepted_outcome
        )

        publication_inputs = self._load_or_freeze_integration_publication_inputs(recovery_id)
        now_text = publication_inputs.recorded_at
        git_identity = publication_inputs.git_identity
        draft_intent = RecoveryIntegrationIntent(
            recovery_id=recovery_id,
            source_run_id=definition.source_run_id,
            recovery_run_id=recovery_run_id,
            sequence_id=definition.sequence.sequence_id if definition.sequence else None,
            sequence_ordinal=definition.sequence.ordinal if definition.sequence else None,
            accepted_outcome=accepted_outcome,
            parent_head=definition.parent_head,
            source_tree_sha256=definition.source_staged_tree_sha256,
            accepted_tree_sha256=accepted_tree,
            reviewed_patch_sha256=reviewed_patch_sha,
            review_result_sha256=review_result_sha,
            commit_message=definition.integration.commit_message,
            target_branch_ref=definition.target_branch_ref,
            private_ref=definition.private_ref,
            target_repository_root=definition.repository.root,
            target_git_common_dir=definition.repository.git_common_dir,
            target_git_dir=definition.repository.git_dir,
            managed_repository_root=str(managed_root),
            managed_git_common_dir=seed.managed_git_common_dir,
            managed_git_dir=seed.managed_git_dir,
            git_identity=git_identity,
            recorded_at=now_text,
        )
        intent, intent_artifact_sha256, pending_state = self._publish_integration_intent(
            recovery_id,
            pending_state,
            draft_intent,
        )
        intent_sha256 = intent_artifact_sha256
        draft_trusted = RecoveryCheckpointTrustedTree(
            intent_sha256=intent_sha256,
            reviewed_tree_sha256=accepted_tree,
            reviewed_patch_sha256=reviewed_patch_sha,
            parent_head=definition.parent_head,
            recorded_at=now_text,
        )
        trusted, trusted_artifact_sha256, pending_state = self._publish_integration_trusted_tree(
            recovery_id,
            pending_state,
            draft_trusted,
            intent_sha256=intent_sha256,
        )

        source_patch_path = (
            self.artifacts.run_root(recovery_id) / definition.source_staged_patch_path
        )
        commit_sha = self._integrate_target(
            intent=intent,
            intent_sha256=intent_sha256,
            trusted=trusted,
            patch_path=patch_path,
            source_patch_path=source_patch_path,
            source_staged_patch_sha256=definition.source_staged_patch_sha256,
            managed_root=managed_root,
            definition_parent_head=definition.parent_head,
            source_run_id=definition.source_run_id,
            target_worktree_key=definition.repository.worktree_key,
            sequence_is_final=definition.sequence.is_final_phase if definition.sequence else True,
            recovery_version=pending_state.version,
            skip_frozen_source_checks=replay.skip_frozen_source_checks,
        )

        return self._finish_integration_from_commit(
            recovery_id=recovery_id,
            pending_state=pending_state,
            definition=definition,
            commit_sha=commit_sha,
            accepted_tree=accepted_tree,
            reviewed_patch_sha=reviewed_patch_sha,
            intent_sha256=intent_sha256,
            intent_artifact_sha256=intent_artifact_sha256,
            trusted_artifact_sha256=trusted_artifact_sha256,
        )

    def _finish_integration_from_commit(
        self,
        *,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        definition: object,
        commit_sha: str,
        accepted_tree: str | None = None,
        reviewed_patch_sha: str | None = None,
        intent_sha256: str | None = None,
        intent_artifact_sha256: str | None = None,
        trusted_artifact_sha256: str | None = None,
    ) -> CleanupPendingRecoveryState:
        from ai_dev_loop.scheduler.domain.recovery import FreshReviewRecoveryDefinition

        assert isinstance(definition, FreshReviewRecoveryDefinition)
        recovery_run_id = pending_state.recovery_run_id
        accepted_outcome = pending_state.accepted_outcome
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if accepted_tree is None or reviewed_patch_sha is None or intent_sha256 is None:
            bindings = self._load_authenticated_integration_bindings(recovery_id, pending_state)
            accepted_tree = bindings.accepted_tree_sha256
            reviewed_patch_sha = bindings.reviewed_patch_sha256
            intent_sha256 = bindings.intent_artifact_sha256
            if intent_artifact_sha256 is None:
                intent_artifact_sha256 = bindings.intent_artifact_sha256
            if trusted_artifact_sha256 is None:
                trusted_artifact_sha256 = bindings.trusted_artifact_sha256
        if definition.sequence is not None:
            resolution = SequenceRecoveryResolution(
                sequence_id=definition.sequence.sequence_id,
                ordinal=definition.sequence.ordinal,
                source_run_id=definition.source_run_id,
                recovery_id=recovery_id,
                recovery_run_id=recovery_run_id,
                accepted_outcome=accepted_outcome,
                residual_risk=pending_state.residual_risk,
                reviewed_patch_sha256=reviewed_patch_sha,
                reviewed_tree_sha256=accepted_tree,
                commit_sha256=commit_sha,
                recorded_at=now_text,
            )
            with self.store.begin_immediate() as conn:
                existing_resolution = self.store.get_sequence_recovery_resolution(
                    conn,
                    sequence_id=definition.sequence.sequence_id,
                    ordinal=definition.sequence.ordinal,
                )
                if existing_resolution is not None:
                    assert isinstance(existing_resolution, SequenceRecoveryResolution)
                    self._validate_matching_sequence_resolution(existing_resolution, resolution)
                else:
                    self.store.insert_sequence_recovery_resolution(conn, resolution, now=now)
                if not self._sequence_recovery_effects_complete(
                    conn,
                    definition=definition,
                    resolution=resolution,
                    commit_sha=commit_sha,
                ):
                    trusted = RecoveryCheckpointTrustedTree(
                        intent_sha256=intent_sha256 or "",
                        reviewed_tree_sha256=accepted_tree,
                        reviewed_patch_sha256=reviewed_patch_sha,
                        parent_head=definition.parent_head,
                        recorded_at=now_text,
                    )
                    self._complete_sequence_recovery(
                        conn,
                        definition=definition,
                        resolution=resolution,
                        commit_sha=commit_sha,
                        trusted=trusted,
                        now=now,
                    )
        result_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_RESULT_ARTIFACT
        integration_result_artifact_sha256: str | None = None
        if not result_path.is_file():
            result_bytes = json.dumps(
                {
                    "commit_sha256": commit_sha,
                    "tree_sha256": accepted_tree,
                    "parent_head": definition.parent_head,
                    "integrated_at": now_text,
                    "intent_sha256": intent_sha256 or "",
                },
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            stored_result = self.artifacts.write_bytes(
                recovery_id,
                RECOVERY_INTEGRATION_RESULT_ARTIFACT,
                result_bytes,
                max_bytes=len(result_bytes) + 1,
            )
            integration_result_artifact_sha256 = stored_result.sha256
        return self._transition_cleanup_pending(
            recovery_id=recovery_id,
            pending_state=pending_state,
            definition=definition,
            recovery_run_id=recovery_run_id,
            accepted_outcome=accepted_outcome,
            commit_sha=commit_sha,
            now=now,
            integration_intent_artifact_sha256=intent_artifact_sha256,
            integration_trusted_tree_artifact_sha256=trusted_artifact_sha256,
            integration_result_artifact_sha256=integration_result_artifact_sha256,
        )

    def _load_integration_evidence(self, recovery_id: str) -> dict[str, str | None]:
        evidence_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
        )
        if not evidence_path.is_file():
            return {}
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery integration evidence is invalid",
            )
        return {
            str(key): (str(value) if value is not None else None) for key, value in payload.items()
        }

    def _integrate_target(
        self,
        *,
        intent: RecoveryIntegrationIntent,
        intent_sha256: str,
        trusted: RecoveryCheckpointTrustedTree,
        patch_path: Path,
        source_patch_path: Path,
        source_staged_patch_sha256: str,
        managed_root: Path,
        definition_parent_head: str,
        source_run_id: str,
        target_worktree_key: str,
        sequence_is_final: bool,
        recovery_version: int,
        skip_frozen_source_checks: bool = False,
    ) -> str:
        target_root = Path(intent.target_repository_root)
        evidence = self._load_integration_evidence(intent.recovery_id)
        commit_sha = evidence.get("commit_sha256")
        if commit_sha:
            branch_head = checkpoint_git_rev_parse(target_root, intent.target_branch_ref)
            if branch_head == commit_sha:
                reconciled = self._reconcile_standalone_target_cas_if_proven(
                    intent=intent,
                    intent_sha256=intent_sha256,
                    trusted=trusted,
                    target_root=target_root,
                    source_run_id=source_run_id,
                    evidence=evidence,
                )
                if reconciled is not None:
                    return reconciled

        now = self._now_factory()
        with self.store.begin_read() as conn:
            deadline = recovery_git_deadline(self.store, conn, context=self._tick_context)
        self._fence_target_mutation(
            source_run_id=source_run_id,
            recovery_id=intent.recovery_id,
            worktree_key=target_worktree_key,
            intent_sha256=intent_sha256,
            recovery_version=recovery_version,
        )

        identity = GitIdentity(
            author_name=intent.git_identity.author_name,
            author_email=intent.git_identity.author_email,
            author_date=intent.git_identity.author_date,
            committer_name=intent.git_identity.committer_name,
            committer_email=intent.git_identity.committer_email,
            committer_date=intent.git_identity.committer_date,
        )
        managed_commit = evidence.get("managed_commit_sha256")
        if not managed_commit:
            managed_commit = checkpoint_git_commit_tree(
                managed_root,
                tree_sha=trusted.reviewed_tree_sha256,
                parent_sha=definition_parent_head,
                message=intent.commit_message,
                identity=identity,
                timeout=deadline.remaining_timeout(now),
            )
            evidence["managed_commit_sha256"] = managed_commit
            self._persist_integration_evidence(intent.recovery_id, evidence)

        target_repo = Path(intent.target_repository_root)
        private_ref_sha = recovery_git_private_ref_peek(
            target_repo,
            ref=intent.private_ref,
            timeout=deadline.remaining_timeout(self._now_factory()),
        )
        if private_ref_sha == managed_commit:
            pass
        elif private_ref_sha in {definition_parent_head, None}:
            self._fence_target_mutation(
                source_run_id=source_run_id,
                recovery_id=intent.recovery_id,
                worktree_key=target_worktree_key,
                intent_sha256=intent_sha256,
                recovery_version=recovery_version,
            )
            recovery_git_update_private_ref_cas(
                target_repo,
                ref=intent.private_ref,
                new_sha=managed_commit,
                old_sha=definition_parent_head,
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
            evidence["private_ref_sha256"] = managed_commit
            self._persist_integration_evidence(intent.recovery_id, evidence)
        elif private_ref_sha != managed_commit:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed private ref drift during integration replay",
            )

        managed_head = checkpoint_git_rev_parse(
            managed_root,
            "HEAD",
            timeout=deadline.remaining_timeout(self._now_factory()),
        )
        if managed_head != managed_commit:
            self._fence_target_mutation(
                source_run_id=source_run_id,
                recovery_id=intent.recovery_id,
                worktree_key=target_worktree_key,
                intent_sha256=intent_sha256,
                recovery_version=recovery_version,
            )
            recovery_git_checkout_detach(
                managed_root,
                commit_sha=managed_commit,
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
            managed_head = checkpoint_git_rev_parse(
                managed_root,
                "HEAD",
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
        if managed_head != managed_commit:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree HEAD drift after integration checkout",
            )
        managed_status = checkpoint_git_status_porcelain(
            managed_root,
            timeout=deadline.remaining_timeout(self._now_factory()),
        )
        if managed_status.strip():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree is not clean at integrated commit",
            )
        branch_head = checkpoint_git_rev_parse(target_root, intent.target_branch_ref)
        if branch_head == managed_commit:
            evidence["commit_sha256"] = managed_commit
            self._persist_integration_evidence(intent.recovery_id, evidence)
            return managed_commit

        if intent.source_tree_sha256 != intent.accepted_tree_sha256:
            live_tree = checkpoint_git_write_tree(target_root)
            delta_already_applied = evidence.get("delta_applied") == "true"
            if live_tree == intent.accepted_tree_sha256 and not delta_already_applied:
                self._ensure_correction_delta_applied(
                    target_root,
                    intent=intent,
                    trusted=trusted,
                    accepted_patch_path=patch_path,
                    evidence=evidence,
                    recovery_id=intent.recovery_id,
                )
                delta_already_applied = True
            elif live_tree == intent.source_tree_sha256:
                if (
                    branch_head == intent.parent_head
                    and not delta_already_applied
                    and not skip_frozen_source_checks
                ):
                    source_patch_artifact = source_patch_path.relative_to(
                        self.artifacts.run_root(intent.recovery_id)
                    ).as_posix()
                    self.artifacts.read_verified_bytes(
                        intent.recovery_id,
                        source_patch_artifact,
                        expected_sha256=source_staged_patch_sha256,
                    )
                    self._verify_target_frozen_source(
                        target_root,
                        intent=intent,
                        source_patch_path=source_patch_path,
                    )
                elif branch_head != intent.parent_head and not delta_already_applied:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.VALIDATION,
                        "target repository drift before integration delta application",
                    )
                if not delta_already_applied:
                    self._fence_target_mutation(
                        source_run_id=source_run_id,
                        recovery_id=intent.recovery_id,
                        worktree_key=target_worktree_key,
                        intent_sha256=intent_sha256,
                        recovery_version=recovery_version,
                    )
                    self._ensure_correction_delta_applied(
                        target_root,
                        intent=intent,
                        trusted=trusted,
                        accepted_patch_path=patch_path,
                        evidence=evidence,
                        recovery_id=intent.recovery_id,
                    )
            elif not delta_already_applied:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target tree drift before correction delta application",
                )
        self._verify_target_accepted_tree(
            target_root,
            intent=intent,
            trusted=trusted,
            patch_path=patch_path,
        )

        if (
            intent.sequence_id is not None
            and intent.sequence_ordinal is not None
            and not sequence_is_final
        ):
            return self._integrate_target_sequence_checkpoint(
                intent=intent,
                intent_sha256=intent_sha256,
                trusted=trusted,
                patch_path=patch_path,
                target_root=target_root,
                target_worktree_key=target_worktree_key,
                recovery_version=recovery_version,
            )
        return self._integrate_target_standalone(
            intent=intent,
            intent_sha256=intent_sha256,
            trusted=trusted,
            patch_path=patch_path,
            target_root=target_root,
            identity=identity,
            source_run_id=source_run_id,
            target_worktree_key=target_worktree_key,
            evidence=evidence,
            recovery_version=recovery_version,
        )

    def _reconcile_standalone_target_cas_if_proven(
        self,
        *,
        intent: RecoveryIntegrationIntent,
        intent_sha256: str,
        trusted: RecoveryCheckpointTrustedTree,
        target_root: Path,
        source_run_id: str,
        evidence: dict[str, str | None],
    ) -> str | None:
        commit_sha = evidence.get("commit_sha256")
        if not commit_sha:
            return None
        branch_head = checkpoint_git_rev_parse(target_root, intent.target_branch_ref)
        if branch_head != commit_sha:
            return None
        if branch_head == intent.parent_head:
            return None
        if not _verify_recovery_commit_against_intent(
            target_root,
            intent=intent,
            trusted=trusted,
            commit_sha=commit_sha,
        ):
            return None
        post_tree = checkpoint_git_write_tree(target_root)
        if post_tree != trusted.reviewed_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target tree drift after proven integration CAS",
            )
        status = checkpoint_git_status_porcelain(target_root)
        if paths_with_unstaged_changes(status) or paths_with_untracked(status):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target worktree is not clean after proven integration CAS",
            )
        with self.store.begin_immediate() as conn:
            if self.store.has_checkpoint_reconciliation_hold(conn, source_run_id):
                self.store.release_checkpoint_reconciliation_hold(
                    conn,
                    run_id=source_run_id,
                    intent_sha256=intent_sha256,
                )
        return commit_sha

    def reconcile_pre_cas_abort_hold(
        self,
        recovery_id: str,
        *,
        require_tick_lease: bool = True,
    ) -> bool:
        """Release an authorized-but-unapplied CAS hold when abort and parent HEAD are proven."""

        abort_path = self.artifacts.run_root(recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
        if not abort_path.is_file():
            return False
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
            if row is None or str(row["state_kind"]) != INTEGRATION_PENDING_RECOVERY_STATE_KIND:
                return False
            pending = IntegrationPendingRecoveryState.model_validate_json(str(row["state_payload"]))
            if not self.store.has_checkpoint_reconciliation_hold(conn, pending.source_run_id):
                return False
            hold = self.store.get_checkpoint_reconciliation_hold_row(conn, pending.source_run_id)
            if hold is None:
                return False
            if str(hold["hold_reason"]) != CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON:
                return False
            intent_sha256 = str(hold["intent_sha256"])
            if self.store.get_nonterminal_attempt_for_run(conn, pending.source_run_id) is not None:
                return False
            if self.store.has_pending_effects_for_run(conn, pending.source_run_id):
                return False
            if require_tick_lease:
                from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active

                if self._tick_context is None:
                    return False
                if not tick_lease_is_active(
                    self.store,
                    conn,
                    owner_id=self._tick_context.tick_owner_id or "",
                    generation=self._tick_context.tick_lease_generation or 0,
                    now=self._now_factory(),
                ):
                    return False
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=pending.definition_sha256,
            definition_artifact_sha256=pending.definition_artifact_sha256,
        )
        target_root = Path(definition.repository.root)
        evidence = self._load_integration_evidence(recovery_id)
        commit_sha = evidence.get("commit_sha256")
        if not commit_sha:
            return False
        parent_head = definition.parent_head
        if pending.integration_intent_artifact_sha256:
            try:
                intent = load_recovery_integration_intent(
                    self.artifacts,
                    recovery_id,
                    expected_sha256=pending.integration_intent_artifact_sha256,
                )
                parent_head = intent.parent_head
            except SchedulerEngineError:
                pass
        try:
            live_head = checkpoint_git_rev_parse(target_root, definition.target_branch_ref)
        except Exception:
            return False
        if live_head == commit_sha:
            return False
        if live_head != parent_head:
            return False
        with self.store.begin_immediate() as conn:
            self.store.release_checkpoint_reconciliation_hold(
                conn,
                run_id=pending.source_run_id,
                intent_sha256=intent_sha256,
            )
        return True

    def _load_or_freeze_integration_publication_inputs(
        self,
        recovery_id: str,
    ) -> RecoveryIntegrationPublicationInputs:
        inputs_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT
        )
        if inputs_path.is_file():
            return RecoveryIntegrationPublicationInputs.model_validate_json(
                inputs_path.read_text(encoding="utf-8")
            )
        intent_path = self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_INTENT_ARTIFACT
        if intent_path.is_file():
            intent = RecoveryIntegrationIntent.model_validate_json(
                intent_path.read_text(encoding="utf-8")
            )
            return RecoveryIntegrationPublicationInputs(
                recorded_at=intent.recorded_at,
                git_identity=intent.git_identity,
            )
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        inputs = RecoveryIntegrationPublicationInputs(
            recorded_at=now_text,
            git_identity=self._default_git_identity(now_text),
        )
        inputs_bytes = json.dumps(inputs.model_dump(mode="json"), indent=2, sort_keys=True).encode(
            "utf-8"
        )
        self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_INTEGRATION_PUBLICATION_INPUTS_ARTIFACT,
            inputs_bytes,
            max_bytes=len(inputs_bytes) + 1,
        )
        return inputs

    def _integrate_target_standalone(
        self,
        *,
        intent: RecoveryIntegrationIntent,
        intent_sha256: str,
        trusted: RecoveryCheckpointTrustedTree,
        patch_path: Path,
        target_root: Path,
        identity: GitIdentity,
        source_run_id: str,
        target_worktree_key: str,
        evidence: dict[str, str | None],
        recovery_version: int,
    ) -> str:
        reconciled = self._reconcile_standalone_target_cas_if_proven(
            intent=intent,
            intent_sha256=intent_sha256,
            trusted=trusted,
            target_root=target_root,
            source_run_id=source_run_id,
            evidence=evidence,
        )
        if reconciled is not None:
            return reconciled

        evidence_state = dict(evidence)
        live_tree = checkpoint_git_write_tree(target_root)
        if live_tree != trusted.reviewed_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target tree drift before standalone integration commit",
            )
        evidence_state["tree_sha256"] = live_tree
        self._persist_integration_evidence(intent.recovery_id, evidence_state)
        commit_sha = evidence_state.get("commit_sha256")
        if not commit_sha:
            commit_sha = checkpoint_git_commit_tree(
                target_root,
                tree_sha=live_tree,
                parent_sha=intent.parent_head,
                message=intent.commit_message,
                identity=identity,
            )
            evidence_state["commit_sha256"] = commit_sha
            self._persist_integration_evidence(intent.recovery_id, evidence_state)

        now = self._now_factory()
        with self.store.begin_read() as conn:
            deadline = recovery_git_deadline(self.store, conn, context=self._tick_context)
            self._fence_target_mutation(
                source_run_id=source_run_id,
                recovery_id=intent.recovery_id,
                worktree_key=target_worktree_key,
                intent_sha256=intent_sha256,
                recovery_version=recovery_version,
            )
        with self.store.begin_immediate() as conn:
            self.store.acquire_checkpoint_reconciliation_hold(
                conn,
                run_id=source_run_id,
                intent_sha256=intent_sha256,
                hold_reason=CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
                ref_may_have_advanced=True,
                now=now,
            )
        mutation_succeeded = False
        try:
            branch_head = checkpoint_git_rev_parse(
                target_root,
                intent.target_branch_ref,
                timeout=deadline.remaining_timeout(now),
            )
            if branch_head != commit_sha:
                checkpoint_git_update_ref_cas(
                    target_root,
                    ref=intent.target_branch_ref,
                    new_sha=commit_sha,
                    old_sha=intent.parent_head,
                    timeout=deadline.remaining_timeout(self._now_factory()),
                )
            post_head = checkpoint_git_rev_parse(
                target_root,
                intent.target_branch_ref,
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
            if post_head != commit_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target branch does not match integrated commit after CAS",
                )
            post_tree = checkpoint_git_write_tree(
                target_root,
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
            if post_tree != trusted.reviewed_tree_sha256:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target tree drift after integration CAS",
                )
            status = checkpoint_git_status_porcelain(
                target_root,
                timeout=deadline.remaining_timeout(self._now_factory()),
            )
            if paths_with_unstaged_changes(status) or paths_with_untracked(status):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target worktree is not clean after integration CAS",
                )
            mutation_succeeded = True
        finally:
            if mutation_succeeded:
                with self.store.begin_immediate() as conn:
                    self.store.release_checkpoint_reconciliation_hold(
                        conn,
                        run_id=source_run_id,
                        intent_sha256=intent_sha256,
                    )
        return commit_sha

    def _ensure_correction_delta_applied(
        self,
        target_root: Path,
        *,
        intent: RecoveryIntegrationIntent,
        trusted: RecoveryCheckpointTrustedTree,
        accepted_patch_path: Path,
        evidence: dict[str, str | None],
        recovery_id: str,
    ) -> None:
        if intent.source_tree_sha256 == intent.accepted_tree_sha256:
            return
        live_tree = checkpoint_git_write_tree(target_root)
        delta = _tree_delta_patch(
            intent.source_tree_sha256,
            intent.accepted_tree_sha256,
            target_root,
        )
        if live_tree == intent.accepted_tree_sha256:
            status = checkpoint_git_status_porcelain(target_root)
            if paths_with_unstaged_changes(status) or paths_with_untracked(status):
                if not delta:
                    raise SchedulerEngineError(
                        SchedulerEngineErrorKind.VALIDATION,
                        "target worktree drift with no correction delta available",
                    )
                recovery_git_apply_worktree_patch(target_root, patch_bytes=delta)
        elif live_tree == intent.source_tree_sha256:
            if delta:
                recovery_git_apply_staged_patch(target_root, patch_bytes=delta)
        else:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target tree drift before correction delta application",
            )
        self._verify_correction_delta_postconditions(
            target_root,
            intent=intent,
            trusted=trusted,
            accepted_patch_path=accepted_patch_path,
        )
        evidence["delta_applied"] = "true"
        self._persist_integration_evidence(recovery_id, evidence)

    def _verify_correction_delta_postconditions(
        self,
        target_root: Path,
        *,
        intent: RecoveryIntegrationIntent,
        trusted: RecoveryCheckpointTrustedTree,
        accepted_patch_path: Path,
    ) -> None:
        live_tree = checkpoint_git_write_tree(target_root)
        if live_tree != intent.accepted_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target index drift after correction delta application",
            )
        status = checkpoint_git_status_porcelain(target_root)
        if paths_with_unstaged_changes(status) or paths_with_untracked(status):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target worktree is not clean after correction delta application",
            )
        self._verify_target_accepted_tree(
            target_root,
            intent=intent,
            trusted=trusted,
            patch_path=accepted_patch_path,
        )

    def _integrate_target_sequence_checkpoint(
        self,
        *,
        intent: RecoveryIntegrationIntent,
        intent_sha256: str,
        trusted: RecoveryCheckpointTrustedTree,
        patch_path: Path,
        target_root: Path,
        target_worktree_key: str,
        recovery_version: int,
    ) -> str:
        assert intent.sequence_id is not None
        assert intent.sequence_ordinal is not None
        evidence_state = dict(self._load_integration_evidence(intent.recovery_id))
        reconciled = self._reconcile_standalone_target_cas_if_proven(
            intent=intent,
            intent_sha256=intent_sha256,
            trusted=trusted,
            target_root=target_root,
            source_run_id=intent.source_run_id,
            evidence=evidence_state,
        )
        if reconciled is not None:
            return reconciled
        with self.store.begin_read() as conn:
            _, source_version, _ = self.store.load_validated_snapshot(conn, intent.source_run_id)
            sequence_state = self.store.load_validated_sequence_state(conn, intent.sequence_id)
            if not isinstance(sequence_state, BlockedSequenceState):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "sequence must be blocked for recovery integration",
                )
            successor_entry = sequence_state.definition.entries[intent.sequence_ordinal]
            successor_run_id = successor_entry.planned_run_id
        seq_intent = SequenceCheckpointIntent(
            sequence_id=intent.sequence_id,
            sequence_version=sequence_state.version,
            predecessor_run_id=intent.source_run_id,
            predecessor_run_version=source_version,
            predecessor_ordinal=intent.sequence_ordinal,
            successor_run_id=successor_run_id,
            successor_ordinal=intent.sequence_ordinal + 1,
            accepted_outcome=intent.accepted_outcome,
            branch_ref=intent.target_branch_ref,
            parent_head=intent.parent_head,
            reviewed_patch_sha256=intent.reviewed_patch_sha256,
            commit_message=intent.commit_message,
            git_identity=intent.git_identity,
            repository_root=intent.target_repository_root,
            git_common_dir=intent.target_git_common_dir,
            git_dir=intent.target_git_dir,
            staged_patch_artifact_path=str(
                patch_path.relative_to(self.artifacts.run_root(intent.recovery_run_id))
            ),
            review_result_artifact_path="codex/reviews/01.json",
            review_result_sha256=intent.review_result_sha256,
        )
        seq_trusted = SequenceCheckpointTrustedTree(
            intent_sha256=intent_sha256,
            reviewed_tree_sha256=trusted.reviewed_tree_sha256,
            reviewed_patch_sha256=trusted.reviewed_patch_sha256,
            parent_head=trusted.parent_head,
            recorded_at=trusted.recorded_at,
        )

        def persist_tree_sha(tree_sha: str) -> None:
            if tree_sha != trusted.reviewed_tree_sha256:
                raise ValidationError("checkpoint tree disagrees with trusted tree")
            evidence_state["tree_sha256"] = tree_sha
            self._persist_integration_evidence(intent.recovery_id, evidence_state)

        def persist_commit_sha(commit_sha: str) -> None:
            evidence_state["commit_sha256"] = commit_sha
            self._persist_integration_evidence(intent.recovery_id, evidence_state)

        def authorize_ref_update() -> None:
            self._fence_target_mutation(
                source_run_id=intent.source_run_id,
                recovery_id=intent.recovery_id,
                worktree_key=target_worktree_key,
                intent_sha256=intent_sha256,
                recovery_version=recovery_version,
            )
            self._verify_target_accepted_tree(
                target_root,
                intent=intent,
                trusted=trusted,
                patch_path=patch_path,
            )

        now = self._now_factory()
        with self.store.begin_read() as conn:
            git_deadline = recovery_git_deadline(self.store, conn, context=self._tick_context)
        with self.store.begin_immediate() as conn:
            self.store.acquire_checkpoint_reconciliation_hold(
                conn,
                run_id=intent.source_run_id,
                intent_sha256=intent_sha256,
                hold_reason=CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
                ref_may_have_advanced=True,
                now=now,
            )
        mutation_succeeded = False
        try:
            commit = self._git_checkpoint.execute_checkpoint(
                seq_intent,
                patch_path=patch_path,
                trusted_tree=seq_trusted,
                evidence=SequenceCheckpointEvidence(
                    tree_sha256=evidence_state.get("tree_sha256"),
                    commit_sha256=evidence_state.get("commit_sha256"),
                )
                if evidence_state.get("tree_sha256") or evidence_state.get("commit_sha256")
                else None,
                persist_tree_sha=persist_tree_sha,
                persist_commit_sha=persist_commit_sha,
                mutation_fence=lambda _boundary: authorize_ref_update(),
                authorize_ref_update=authorize_ref_update,
                on_ref_advanced=lambda: None,
                deadline=git_deadline,
                now_factory=self._now_factory,
            )
            branch_head = checkpoint_git_rev_parse(
                target_root,
                intent.target_branch_ref,
                timeout=git_deadline.remaining_timeout(self._now_factory()),
            )
            if branch_head != commit.commit_sha:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target branch does not match integrated commit after sequence CAS",
                )
            post_tree = checkpoint_git_write_tree(
                target_root,
                timeout=git_deadline.remaining_timeout(self._now_factory()),
            )
            if post_tree != trusted.reviewed_tree_sha256:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target tree drift after sequence integration CAS",
                )
            status = checkpoint_git_status_porcelain(
                target_root,
                timeout=git_deadline.remaining_timeout(self._now_factory()),
            )
            if paths_with_unstaged_changes(status) or paths_with_untracked(status):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "target worktree is not clean after sequence integration CAS",
                )
            mutation_succeeded = True
            return commit.commit_sha
        finally:
            if mutation_succeeded:
                with self.store.begin_immediate() as conn:
                    self.store.release_checkpoint_reconciliation_hold(
                        conn,
                        run_id=intent.source_run_id,
                        intent_sha256=intent_sha256,
                    )

    def _verify_target_reservation(self, source_run_id: str, *, worktree_key: str) -> None:
        with self.store.begin_read() as conn:
            self.store.verify_recovery_target_reservation(
                conn,
                source_run_id=source_run_id,
                worktree_key=worktree_key,
            )

    def _fence_target_mutation(
        self,
        *,
        source_run_id: str,
        recovery_id: str,
        worktree_key: str,
        intent_sha256: str,
        recovery_version: int,
    ) -> None:
        now = self._now_factory()
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
            if (
                row is not None
                and str(row["state_kind"]) != INTEGRATION_PENDING_RECOVERY_STATE_KIND
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "recovery integration fence requires integration_pending state",
                )
            assert_recovery_mutation_allowed(
                self.store,
                conn,
                source_run_id=source_run_id,
                recovery_id=recovery_id,
                worktree_key=worktree_key,
                recovery_version=recovery_version,
                integration_intent_sha256=intent_sha256,
                context=self._tick_context,
                now=now,
            )
            abort_path = self.artifacts.run_root(recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
            if abort_path.is_file():
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "recovery abort intent blocks integration",
                )

    def _verify_target_frozen_source(
        self,
        target_root: Path,
        *,
        intent: RecoveryIntegrationIntent,
        source_patch_path: Path,
    ) -> None:
        checkpoint_validate_repository_layout(
            target_root,
            expected_root=intent.target_repository_root,
            expected_git_common_dir=intent.target_git_common_dir,
            expected_git_dir=intent.target_git_dir,
            expected_branch=intent.target_branch_ref.removeprefix("refs/heads/"),
            context="recovery target repository identity",
        )
        head = checkpoint_git_rev_parse(target_root, "HEAD")
        if head != intent.parent_head:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target repository HEAD drift before integration",
            )
        live_tree = checkpoint_git_write_tree(target_root)
        if live_tree != intent.source_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target staged tree drift before integration",
            )
        checkpoint_validate_staged_patch_matches_artifact(target_root, source_patch_path)
        status = checkpoint_git_status_porcelain(target_root)
        if paths_with_unstaged_changes(status) or paths_with_untracked(status):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target repository has unstaged or untracked changes before integration",
            )

    def _verify_target_accepted_tree(
        self,
        target_root: Path,
        *,
        intent: RecoveryIntegrationIntent,
        trusted: RecoveryCheckpointTrustedTree,
        patch_path: Path,
    ) -> None:
        live_tree = checkpoint_git_write_tree(target_root)
        if live_tree != trusted.reviewed_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target tree drift from accepted reviewed tree",
            )
        live_patch = checkpoint_git_diff_cached_patch_bytes(target_root)
        live_patch_sha = hashlib.sha256(live_patch).hexdigest()
        if live_patch_sha != trusted.reviewed_patch_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target patch drift from accepted reviewed patch",
            )
        checkpoint_validate_staged_patch_matches_artifact(target_root, patch_path)

    @staticmethod
    def _authenticate_managed_repository(seed: RecoverySeedEvidence, managed_root: Path) -> None:
        admission = discover_repository_bounded(managed_root)
        if admission.git_common_dir != seed.managed_git_common_dir:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree git_common_dir drift",
            )
        if admission.git_dir != seed.managed_git_dir:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree git_dir drift",
            )
        if admission.head != seed.parent_head:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree HEAD drift from frozen parent",
            )

    @staticmethod
    def _measure_managed_accepted_tree(
        managed_root: Path,
        *,
        reviewed_patch_sha: str,
        patch_path: Path,
        expected_tree_sha: str,
    ) -> str:
        checkpoint_validate_staged_patch_matches_artifact(managed_root, patch_path)
        live_patch = checkpoint_git_diff_cached_patch_bytes(managed_root)
        live_patch_sha = hashlib.sha256(live_patch).hexdigest()
        if live_patch_sha != reviewed_patch_sha:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "managed worktree patch drift from reviewed binding",
            )
        tree_sha = checkpoint_git_write_tree(managed_root)
        if tree_sha == expected_tree_sha and reviewed_patch_sha:
            return tree_sha
        return tree_sha

    def _resume_proven_integration(
        self,
        *,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        definition: object,
    ) -> CleanupPendingRecoveryState:
        assert isinstance(definition, FreshReviewRecoveryDefinition)
        bindings = self._load_authenticated_integration_bindings(recovery_id, pending_state)
        result = load_recovery_integration_result(self.artifacts, recovery_id)
        intent_sha = result.get("intent_sha256", "")
        if not intent_sha or intent_sha != bindings.intent_artifact_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "integration result intent binding mismatch",
            )
        result_tree = result.get("tree_sha256", "")
        if not result_tree or result_tree != bindings.accepted_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "integration result tree binding mismatch",
            )
        commit_sha = result.get("commit_sha256", "")
        if not commit_sha:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "integration result lacks commit binding",
            )
        return self._complete_proven_target_cas_replay(
            recovery_id,
            pending_state,
            definition,
            commit_sha=commit_sha,
        )

    def _transition_cleanup_pending(
        self,
        *,
        recovery_id: str,
        pending_state: IntegrationPendingRecoveryState,
        definition: object,
        recovery_run_id: str,
        accepted_outcome: Literal["completed", "completed_with_residual_risk"],
        commit_sha: str,
        now: datetime,
        release_standalone_reservation: bool = True,
        integration_intent_artifact_sha256: str | None = None,
        integration_trusted_tree_artifact_sha256: str | None = None,
        integration_result_artifact_sha256: str | None = None,
    ) -> CleanupPendingRecoveryState:
        from ai_dev_loop.scheduler.domain.recovery import FreshReviewRecoveryDefinition

        assert isinstance(definition, FreshReviewRecoveryDefinition)
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cleanup = CleanupPendingRecoveryState(
            recovery_id=recovery_id,
            version=pending_state.version + 1,
            updated_at=now_text,
            definition_sha256=pending_state.definition_sha256,
            definition_artifact_sha256=pending_state.definition_artifact_sha256,
            source_run_id=pending_state.source_run_id,
            source_run_id_prefix=pending_state.source_run_id_prefix,
            recovery_run_id=recovery_run_id,
            sequence=pending_state.sequence,
            started_at=pending_state.started_at,
            accepted_outcome=accepted_outcome,
            residual_risk=pending_state.residual_risk,
            integrated_commit_sha256=commit_sha,
            integrated_commit_sha256_prefix=commit_sha[:8],
            seed_evidence_artifact_sha256=pending_state.seed_evidence_artifact_sha256,
            integration_intent_artifact_sha256=integration_intent_artifact_sha256,
            integration_trusted_tree_artifact_sha256=integration_trusted_tree_artifact_sha256,
            integration_result_artifact_sha256=integration_result_artifact_sha256,
        )
        with self.store.begin_immediate() as conn:
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=cleanup,
                expected_version=pending_state.version,
                now=now,
            )
            if release_standalone_reservation and definition.sequence is None:
                self.store.release_reservation(
                    conn,
                    worktree_key=definition.repository.worktree_key,
                    now=now,
                )
        return cleanup

    @staticmethod
    def _validate_matching_sequence_resolution(
        existing: SequenceRecoveryResolution,
        expected: SequenceRecoveryResolution,
    ) -> None:
        if existing.recovery_id != expected.recovery_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery resolution recovery_id mismatch",
            )
        if existing.recovery_run_id != expected.recovery_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery resolution recovery_run_id mismatch",
            )
        if existing.commit_sha256 != expected.commit_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery resolution commit mismatch",
            )
        if existing.reviewed_tree_sha256 != expected.reviewed_tree_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery resolution tree mismatch",
            )
        if existing.reviewed_patch_sha256 != expected.reviewed_patch_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery resolution patch mismatch",
            )

    def _sequence_recovery_effects_complete(
        self,
        conn: object,
        *,
        definition: FreshReviewRecoveryDefinition,
        resolution: SequenceRecoveryResolution,
        commit_sha: str,
    ) -> bool:
        import sqlite3

        from ai_dev_loop.scheduler.application.contracts import ReservationStatus

        assert isinstance(conn, sqlite3.Connection)
        assert definition.sequence is not None
        sequence_id = definition.sequence.sequence_id
        sequence_state = self.store.load_validated_sequence_state(conn, sequence_id)
        if definition.sequence.is_final_phase:
            if not isinstance(sequence_state, RecoveryIntegratedFinalizationSequenceState):
                return False
            return (
                sequence_state.recovery_id == resolution.recovery_id
                and sequence_state.integrated_commit_sha256 == commit_sha
            )
        if not isinstance(sequence_state, ActiveSequenceState):
            return False
        successor_entry = sequence_state.definition.entries[definition.sequence.ordinal]
        if sequence_state.current_ordinal != definition.sequence.ordinal + 1:
            return False
        if sequence_state.current_run_id != successor_entry.planned_run_id:
            return False
        reservation = conn.execute(
            """
            SELECT run_id FROM scheduler_repository_reservations
            WHERE worktree_key = ? AND status = ?
            """,
            (
                definition.repository.worktree_key,
                ReservationStatus.ACTIVE.value,
            ),
        ).fetchone()
        if reservation is None:
            return False
        return str(reservation["run_id"]) == successor_entry.planned_run_id

    def _complete_sequence_recovery(
        self,
        conn: object,
        *,
        definition: object,
        resolution: SequenceRecoveryResolution,
        commit_sha: str,
        trusted: RecoveryCheckpointTrustedTree,
        now: datetime,
    ) -> None:
        import sqlite3

        from ai_dev_loop.scheduler.application.contracts import ReservationStatus

        assert isinstance(conn, sqlite3.Connection)
        assert isinstance(definition, FreshReviewRecoveryDefinition)
        assert definition.sequence is not None
        sequence_id = definition.sequence.sequence_id
        existing_resolution = self.store.get_sequence_recovery_resolution(
            conn,
            sequence_id=sequence_id,
            ordinal=definition.sequence.ordinal,
        )
        if existing_resolution is not None:
            assert isinstance(existing_resolution, SequenceRecoveryResolution)
            self._validate_matching_sequence_resolution(existing_resolution, resolution)
            if self._sequence_recovery_effects_complete(
                conn,
                definition=definition,
                resolution=resolution,
                commit_sha=commit_sha,
            ):
                return
        sequence_state = self.store.load_validated_sequence_state(conn, sequence_id)
        if not isinstance(sequence_state, BlockedSequenceState):
            if self._sequence_recovery_effects_complete(
                conn,
                definition=definition,
                resolution=resolution,
                commit_sha=commit_sha,
            ):
                return
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence recovery requires blocked sequence state",
            )
        if sequence_state.current_run_id != definition.source_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "blocked sequence current run disagrees with recovery source",
            )
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        residual_ordinals = tuple(sequence_state.residual_risk_ordinals)
        if resolution.residual_risk:
            residual_ordinals = tuple(
                sorted(set(residual_ordinals + (definition.sequence.ordinal,)))
            )
        if definition.sequence.is_final_phase:
            finalized = RecoveryIntegratedFinalizationSequenceState(
                schema_version=sequence_state.schema_version,
                sequence_id=sequence_id,
                version=sequence_state.version + 1,
                prepared_at=sequence_state.prepared_at,
                updated_at=now_text,
                started_at=sequence_state.started_at,
                finalized_at=now_text,
                idempotency_key=sequence_state.idempotency_key,
                definition=sequence_state.definition,
                source_run_id=definition.source_run_id,
                recovery_id=resolution.recovery_id,
                recovery_run_id=resolution.recovery_run_id,
                final_outcome=resolution.accepted_outcome,
                integrated_commit_sha256=commit_sha,
                integrated_commit_sha256_prefix=commit_sha[:8],
                materialized_entries=sequence_state.materialized_entries,
                residual_risk_ordinals=residual_ordinals,
            )
            from ai_dev_loop.scheduler.application.sequence_report import (
                build_recovery_integrated_completion_report,
                persist_recovery_integrated_completion_report_if_absent,
            )

            report = build_recovery_integrated_completion_report(
                self.store,
                self.artifacts,
                finalized,
                conn=conn,
            )
            report_sha = persist_recovery_integrated_completion_report_if_absent(
                self.artifacts,
                report,
            )
            finalized = finalized.model_copy(
                update={"completion_report_sha256": report_sha},
            )
            if not self.store.compare_and_swap_sequence_state(
                conn,
                sequence_id=sequence_id,
                expected_version=sequence_state.version,
                new_state=finalized,
                now=now,
            ):
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "sequence recovery finalization CAS failed",
                )
            self.store.release_reservation(
                conn,
                worktree_key=definition.repository.worktree_key,
                now=now,
            )
            return

        successor_entry = sequence_state.definition.entries[definition.sequence.ordinal]
        materializer = SequenceRunMaterializer(self.artifacts)
        context, entry_hash = materializer.materialize_next_entry(
            sequence_id=sequence_id,
            definition=sequence_state.definition,
            entry=successor_entry,
        )
        successor_run_id = successor_entry.planned_run_id
        existing_run = conn.execute(
            "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
            (successor_run_id,),
        ).fetchone()
        if existing_run is None:
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
            self.store._insert_handoff_successor_run(
                conn,
                run_id=successor_run_id,
                submitted_state=submitted_state,
                authorized_state=authorized_state,
                submitted_event_id=f"evt-{secrets.token_hex(16)}",
                submitted_event=submitted_event,
                authorized_event_id=f"evt-{secrets.token_hex(16)}",
                authorized_event=authorized_event,
                now=now,
            )
        updated_entries = (
            *sequence_state.materialized_entries,
            MaterializedSequenceEntry(
                ordinal=definition.sequence.ordinal + 1,
                run_id=successor_entry.planned_run_id,
                entry_hash=entry_hash,
                materialized_at=now_text,
            ),
        )
        active = ActiveSequenceState(
            schema_version=sequence_state.schema_version,
            sequence_id=sequence_id,
            version=sequence_state.version + 1,
            prepared_at=sequence_state.prepared_at,
            updated_at=now_text,
            started_at=sequence_state.started_at,
            idempotency_key=sequence_state.idempotency_key,
            definition=sequence_state.definition,
            current_ordinal=definition.sequence.ordinal + 1,
            current_run_id=successor_entry.planned_run_id,
            materialized_entries=updated_entries,
            residual_risk_ordinals=residual_ordinals,
        )
        if not self.store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=sequence_state.version,
            new_state=active,
            now=now,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence recovery continuation CAS failed",
            )
        transfer = conn.execute(
            """
            UPDATE scheduler_repository_reservations
            SET run_id = ?, updated_at = ?
            WHERE worktree_key = ? AND run_id = ? AND status = ?
            """,
            (
                successor_entry.planned_run_id,
                now_text,
                definition.repository.worktree_key,
                definition.source_run_id,
                ReservationStatus.ACTIVE.value,
            ),
        )
        if transfer.rowcount != 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "sequence recovery reservation transfer failed",
            )

    def _persist_integration_evidence(
        self,
        recovery_id: str,
        evidence_state: dict[str, str | None],
    ) -> None:
        payload = json.dumps(evidence_state, indent=2, sort_keys=True).encode("utf-8")
        evidence_path = (
            self.artifacts.run_root(recovery_id) / RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT
        )
        if evidence_path.is_file():
            self.artifacts.replace_text(
                recovery_id,
                RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
                payload.decode("utf-8"),
                max_bytes=len(payload) + 1,
            )
        else:
            self.artifacts.write_bytes(
                recovery_id,
                RECOVERY_INTEGRATION_EVIDENCE_ARTIFACT,
                payload,
                max_bytes=len(payload) + 1,
            )

    @staticmethod
    def _default_git_identity(now_text: str) -> GitIdentitySnapshot:
        return GitIdentitySnapshot(
            author_name="ai_dev_loop",
            author_email="ai-dev-loop@local",
            author_date=now_text,
            committer_name="ai_dev_loop",
            committer_email="ai-dev-loop@local",
            committer_date=now_text,
        )
