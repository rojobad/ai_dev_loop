"""Process-free fresh-review recovery prepare for Phase 20.6."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.git import checkpoint_git_rev_parse, compute_ephemeral_staged_tree_sha
from ai_dev_loop.scheduler.application.contracts import (
    RecoveryPrepareResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    prepared_recovery_start_next_action,
)
from ai_dev_loop.scheduler.application.cursor_evidence import frozen_repository_identity
from ai_dev_loop.scheduler.application.fresh_review_recovery_source import (
    analyze_fresh_review_recovery_source,
)
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    RECOVERY_SOURCE_STAGED_PATCH_ARTIFACT,
    definition_digest_binding,
    persist_recovery_definition,
)
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, worktree_key
from ai_dev_loop.scheduler.domain.recovery import (
    FreshReviewRecoveryDefinition,
    PreparedRecoveryState,
    RecoverySequenceBinding,
)
from ai_dev_loop.scheduler.domain.state import (
    CodexRuntimeBinding,
    FreshCodexReviewerBinding,
    RepositoryBinding,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    recovery_worktree_root,
    scheduler_state_dir,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


def _require_fresh_codex_binding(
    codex: CodexRuntimeBinding | FreshCodexReviewerBinding,
) -> FreshCodexReviewerBinding:
    if not isinstance(codex, FreshCodexReviewerBinding):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "fresh recovery requires FreshCodexReviewerBinding",
        )
    return codex


def _recovery_id_from_definition_sha(definition_sha256: str) -> str:
    return f"rcv-{definition_sha256[:32]}"


def _private_ref_for_recovery(recovery_id: str) -> str:
    digest = hashlib.sha256(recovery_id.encode("utf-8")).hexdigest()
    return f"refs/ai-dev-loop/recovery/{digest}"


def _frozen_sequence_commit_message(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    sequence: RecoverySequenceBinding,
) -> str:
    state = store.load_validated_sequence_state(conn, sequence.sequence_id)
    for entry in state.definition.entries:
        if entry.ordinal == sequence.ordinal:
            if not entry.commit_message:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.VALIDATION,
                    "sequence entry lacks frozen commit message",
                )
            return entry.commit_message
    raise SchedulerEngineError(
        SchedulerEngineErrorKind.VALIDATION,
        "sequence entry ordinal not found in frozen definition",
    )


def _build_definition(
    evidence: object,
    *,
    repository: RepositoryBinding,
    source_run_id: str,
    source_run_id_prefix: str,
    parent_head: str,
    staged_tree_sha: str,
    staged_patch_path: str,
    prepared_at: str,
    recovery_id: str,
    definition_sha256: str,
) -> FreshReviewRecoveryDefinition:
    from ai_dev_loop.scheduler.application.fresh_review_recovery_source import (
        FreshReviewRecoverySourceEvidence,
    )

    assert isinstance(evidence, FreshReviewRecoverySourceEvidence)
    state_root = scheduler_state_dir()
    worktree_path = recovery_worktree_root(state_root, recovery_id)
    return FreshReviewRecoveryDefinition(
        recovery_id=recovery_id,
        definition_sha256=definition_sha256,
        source_run_id=source_run_id,
        source_run_id_prefix=source_run_id_prefix,
        sequence=evidence.sequence,
        repository=repository,
        target_branch_ref=f"refs/heads/{repository.branch}",
        parent_head=parent_head,
        source_staged_patch_path=staged_patch_path,
        source_staged_patch_sha256=evidence.cursor.staged_patch_sha256 or "",
        source_staged_tree_sha256=staged_tree_sha,
        plan_prompt=evidence.context.plan_prompt,
        effective_config=evidence.context.effective_config,
        codex=_require_fresh_codex_binding(evidence.context.codex),
        cursor=evidence.context.cursor,
        workflow=evidence.context.workflow,
        controller=evidence.context.controller,
        integration=evidence.integration,
        managed_worktree_path_token=hashlib.sha256(str(worktree_path).encode("utf-8")).hexdigest(),
        private_ref=_private_ref_for_recovery(recovery_id),
        recovery_worktree_key=worktree_key(str(worktree_path)),
        prepared_at=prepared_at,
    )


class RecoveryPrepareService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        now_factory: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._now_factory = now_factory or (lambda: utc_now())

    def prepare(
        self,
        source_run_id: str,
        *,
        commit_message: str | None = None,
    ) -> RecoveryPrepareResult:
        with self.store.begin_read() as conn:
            sequence_commit_message: str | None = None
            state, _, _ = self.store.load_validated_snapshot(conn, source_run_id)
            if state.context.sequence is not None:
                sequence_binding = RecoverySequenceBinding(
                    sequence_id=state.context.sequence.sequence_id,
                    ordinal=state.context.sequence.ordinal,
                    total_phases=state.context.sequence.total_phases,
                    is_final_phase=(
                        state.context.sequence.ordinal == state.context.sequence.total_phases
                    ),
                )
                if not sequence_binding.is_final_phase:
                    sequence_commit_message = _frozen_sequence_commit_message(
                        self.store,
                        conn,
                        sequence_binding,
                    )

        evidence = analyze_fresh_review_recovery_source(
            self.store,
            self.artifacts,
            source_run_id,
            commit_message=commit_message,
            sequence_commit_message=sequence_commit_message,
        )
        identity = frozen_repository_identity(
            evidence.context,
            run_id=source_run_id,
            artifacts=self.artifacts,
            checkpoint=evidence.checkpoint,
        )
        repo_root = Path(identity.root)
        parent_head = checkpoint_git_rev_parse(repo_root, "HEAD")
        if parent_head != identity.initial_head:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "target repository HEAD drift from frozen parent",
            )
        patch_bytes = self.artifacts.read_verified_bytes(
            source_run_id,
            evidence.cursor.staged_patch_path or "",
            expected_sha256=evidence.cursor.staged_patch_sha256 or "",
        )
        staged_tree_sha = compute_ephemeral_staged_tree_sha(
            repo_root,
            parent_head=parent_head,
            patch_bytes=patch_bytes,
        )
        definition_payload = {
            "source_run_id": source_run_id,
            "parent_head": parent_head,
            "staged_patch_sha256": evidence.cursor.staged_patch_sha256,
            "staged_tree_sha256": staged_tree_sha,
            "commit_message": evidence.integration.commit_message,
            "plan_sha256": evidence.context.plan_prompt.plan_sha256,
            "prompt_sha256": evidence.context.plan_prompt.prompt_sha256,
            "sequence": evidence.sequence.model_dump(mode="json") if evidence.sequence else None,
        }
        definition_sha256 = canonical_json_sha256(dict(definition_payload))
        recovery_id = _recovery_id_from_definition_sha(definition_sha256)
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        with self.store.begin_read() as conn:
            existing = self.store.get_fresh_review_recovery_by_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
            )
            if existing is not None:
                return self._replay_result(str(existing["recovery_id"]), changed=False)

        definition = _build_definition(
            evidence,
            repository=RepositoryBinding(
                root=identity.root,
                git_common_dir=identity.git_common_dir,
                git_dir=identity.git_dir,
                branch=identity.branch,
                initial_head=parent_head,
                worktree_key=evidence.context.repository.worktree_key,
            ),
            source_run_id=source_run_id,
            source_run_id_prefix=source_run_id[:8],
            parent_head=parent_head,
            staged_tree_sha=staged_tree_sha,
            staged_patch_path=RECOVERY_SOURCE_STAGED_PATCH_ARTIFACT,
            prepared_at=now_text,
            recovery_id=recovery_id,
            definition_sha256=definition_sha256,
        )
        if definition_digest_binding(definition) != definition_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "definition digest binding mismatch",
            )
        self.artifacts.write_bytes(
            recovery_id,
            RECOVERY_SOURCE_STAGED_PATCH_ARTIFACT,
            patch_bytes,
            max_bytes=len(patch_bytes) + 1,
        )
        stored_definition = persist_recovery_definition(self.artifacts, recovery_id, definition)
        prepared_state = PreparedRecoveryState(
            recovery_id=recovery_id,
            version=1,
            updated_at=now_text,
            definition_sha256=definition_sha256,
            definition_artifact_sha256=stored_definition.sha256,
            source_run_id=source_run_id,
            source_run_id_prefix=source_run_id[:8],
            prepared_at=now_text,
            sequence=evidence.sequence,
        )
        with self.store.begin_immediate() as conn:
            replay = self.store.get_fresh_review_recovery_by_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
            )
            if replay is not None:
                return self._replay_result(str(replay["recovery_id"]), changed=False)
            self.store.insert_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=prepared_state,
                now=now,
            )
            self.store.insert_fresh_review_recovery_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
                recovery_id=recovery_id,
                now=now,
            )
        return RecoveryPrepareResult(
            recovery_id=recovery_id,
            source_run_id=source_run_id,
            state_kind=prepared_state.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=prepared_recovery_start_next_action(recovery_id),
        )

    def _replay_result(self, recovery_id: str, *, changed: bool) -> RecoveryPrepareResult:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "recovery idempotency row missing recovery record",
            )
        return RecoveryPrepareResult(
            recovery_id=recovery_id,
            source_run_id=str(row["source_run_id"]),
            state_kind=str(row["state_kind"]),
            changed=changed,
            idempotent_replay=not changed,
            safe_next_action=prepared_recovery_start_next_action(recovery_id),
        )


def default_recovery_prepare_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> RecoveryPrepareService:
    db = db_path or default_engine_db_path()
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return RecoveryPrepareService(SqliteSchedulerStore(db), artifacts)


def prepare_recovery(
    source_run_id: str,
    *,
    commit_message: str | None = None,
    db_path: Path | None = None,
) -> RecoveryPrepareResult:
    service = default_recovery_prepare_service(db_path=db_path)
    return service.prepare(source_run_id, commit_message=commit_message)
