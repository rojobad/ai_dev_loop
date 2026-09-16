"""Process-free authenticated rollover prepare for Phase 20.6.5."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.git import checkpoint_git_rev_parse, compute_ephemeral_staged_tree_sha
from ai_dev_loop.scheduler.application.authenticated_rollover_source import (
    analyze_authenticated_rollover_source,
)
from ai_dev_loop.scheduler.application.contracts import (
    RolloverPrepareResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    prepared_rollover_start_next_action,
)
from ai_dev_loop.scheduler.application.cursor_evidence import frozen_repository_identity
from ai_dev_loop.scheduler.application.review_budget_artifacts import (
    _read_hash_verified_artifact_bytes,
)
from ai_dev_loop.scheduler.application.rollover_artifacts import (
    ROLLOVER_SOURCE_STAGED_PATCH_ARTIFACT,
    persist_rollover_definition,
    rollover_definition_digest_binding,
)
from ai_dev_loop.scheduler.application.sequence_materializer import FRESH_REVIEWER_INPUT_ARTIFACT
from ai_dev_loop.scheduler.application.submission import _fresh_input_artifact_bytes
from ai_dev_loop.scheduler.domain.codex_contract import MAX_CODEX_REVIEW_RESULT_BYTES
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256, worktree_key
from ai_dev_loop.scheduler.domain.rollover import (
    AuthenticatedRolloverDefinition,
    PreparedRolloverState,
    RolloverSequenceBinding,
)
from ai_dev_loop.scheduler.domain.state import FreshCodexReviewerBinding, RepositoryBinding
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    rollover_worktree_root,
    scheduler_state_dir,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_bytes, utc_now


def _rollover_id_from_definition_sha(definition_sha256: str) -> str:
    return f"rol-{definition_sha256[:32]}"


def _private_ref_for_rollover(rollover_id: str) -> str:
    digest = hashlib.sha256(rollover_id.encode("utf-8")).hexdigest()
    return f"refs/ai-dev-loop/rollover/{digest}"


def _frozen_sequence_commit_message(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    sequence: RolloverSequenceBinding,
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
    fresh_codex: FreshCodexReviewerBinding,
    source_final_review_result_path: str,
    repository: RepositoryBinding,
    source_run_id: str,
    source_run_id_prefix: str,
    parent_head: str,
    staged_tree_sha: str,
    staged_patch_path: str,
    prepared_at: str,
    rollover_id: str,
    definition_sha256: str,
) -> AuthenticatedRolloverDefinition:
    from ai_dev_loop.scheduler.application.authenticated_rollover_source import (
        AuthenticatedRolloverSourceEvidence,
    )

    assert isinstance(evidence, AuthenticatedRolloverSourceEvidence)
    state_root = scheduler_state_dir()
    worktree_path = rollover_worktree_root(state_root, rollover_id)
    return AuthenticatedRolloverDefinition(
        rollover_id=rollover_id,
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
        source_final_review_result_path=source_final_review_result_path,
        source_final_review_result_sha256=evidence.source_final_review_result_sha256,
        plan_prompt=evidence.context.plan_prompt,
        effective_config=evidence.context.effective_config,
        codex=fresh_codex,
        cursor=evidence.context.cursor,
        workflow=evidence.context.workflow,
        controller=evidence.context.controller,
        integration=evidence.integration,
        managed_worktree_path_token=hashlib.sha256(str(worktree_path).encode("utf-8")).hexdigest(),
        private_ref=_private_ref_for_rollover(rollover_id),
        rollover_worktree_key=worktree_key(str(worktree_path)),
        prepared_at=prepared_at,
    )


class RolloverPrepareService:
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
    ) -> RolloverPrepareResult:
        with self.store.begin_read() as conn:
            sequence_commit_message: str | None = None
            state, _, _ = self.store.load_validated_snapshot(conn, source_run_id)
            if state.context.sequence is not None:
                sequence_binding = RolloverSequenceBinding(
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

        evidence = analyze_authenticated_rollover_source(
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
        rollover_id = _rollover_id_from_definition_sha(definition_sha256)
        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        with self.store.begin_read() as conn:
            existing = self.store.get_authenticated_rollover_by_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
            )
            if existing is not None:
                return self._replay_result(str(existing["rollover_id"]), changed=False)

        fresh_binding_bytes = _fresh_input_artifact_bytes(
            review_model=evidence.fresh_codex.review_model,
            review_reasoning_effort=evidence.fresh_codex.review_reasoning_effort,
        )
        self.artifacts.write_bytes(
            rollover_id,
            FRESH_REVIEWER_INPUT_ARTIFACT,
            fresh_binding_bytes,
            max_bytes=len(fresh_binding_bytes) + 1,
        )
        fresh_codex = FreshCodexReviewerBinding(
            review_model=evidence.fresh_codex.review_model,
            review_reasoning_effort=evidence.fresh_codex.review_reasoning_effort,
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command=evidence.fresh_codex.command,
            review_skill=evidence.fresh_codex.review_skill,
            sandbox=evidence.fresh_codex.sandbox,
            binding_artifact_path=FRESH_REVIEWER_INPUT_ARTIFACT,
            binding_sha256=sha256_bytes(fresh_binding_bytes),
        )
        rollover_review_path = "rollover/source-final-review-result.json"
        definition = _build_definition(
            evidence,
            fresh_codex=fresh_codex,
            source_final_review_result_path=rollover_review_path,
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
            staged_patch_path=ROLLOVER_SOURCE_STAGED_PATCH_ARTIFACT,
            prepared_at=now_text,
            rollover_id=rollover_id,
            definition_sha256=definition_sha256,
        )
        if rollover_definition_digest_binding(definition) != definition_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "definition digest binding mismatch",
            )
        self.artifacts.write_bytes(
            rollover_id,
            ROLLOVER_SOURCE_STAGED_PATCH_ARTIFACT,
            patch_bytes,
            max_bytes=len(patch_bytes) + 1,
        )
        review_bytes = _read_hash_verified_artifact_bytes(
            self.artifacts.run_root(source_run_id),
            evidence.source_final_review_result_path,
            expected_sha256=evidence.source_final_review_result_sha256,
            max_bytes=MAX_CODEX_REVIEW_RESULT_BYTES,
        )
        self.artifacts.write_bytes(
            rollover_id,
            rollover_review_path,
            review_bytes,
            max_bytes=len(review_bytes) + 1,
        )
        stored_definition = persist_rollover_definition(self.artifacts, rollover_id, definition)
        prepared_state = PreparedRolloverState(
            rollover_id=rollover_id,
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
            replay = self.store.get_authenticated_rollover_by_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
            )
            if replay is not None:
                return self._replay_result(str(replay["rollover_id"]), changed=False)
            self.store.insert_authenticated_rollover(
                conn,
                rollover_id=rollover_id,
                state=prepared_state,
                now=now,
            )
            self.store.insert_authenticated_rollover_idempotency(
                conn,
                source_run_id=source_run_id,
                definition_sha256=definition_sha256,
                rollover_id=rollover_id,
                now=now,
            )
        return RolloverPrepareResult(
            rollover_id=rollover_id,
            source_run_id=source_run_id,
            state_kind=prepared_state.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=prepared_rollover_start_next_action(rollover_id),
        )

    def _replay_result(self, rollover_id: str, *, changed: bool) -> RolloverPrepareResult:
        with self.store.begin_read() as conn:
            row = self.store.get_authenticated_rollover(conn, rollover_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.INTERNAL,
                "rollover idempotency row missing rollover record",
            )
        return RolloverPrepareResult(
            rollover_id=rollover_id,
            source_run_id=str(row["source_run_id"]),
            state_kind=str(row["state_kind"]),
            changed=changed,
            idempotent_replay=not changed,
            safe_next_action=prepared_rollover_start_next_action(rollover_id),
        )


def default_rollover_prepare_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
) -> RolloverPrepareService:
    db = db_path or default_engine_db_path()
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return RolloverPrepareService(SqliteSchedulerStore(db), artifacts)


def prepare_rollover(
    source_run_id: str,
    *,
    commit_message: str | None = None,
    db_path: Path | None = None,
) -> RolloverPrepareResult:
    service = default_rollover_prepare_service(db_path=db_path)
    return service.prepare(source_run_id, commit_message=commit_message)
