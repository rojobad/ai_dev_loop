"""Authenticated rollover start authorization for Phase 20.6.5."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.authenticated_rollover_source import (
    analyze_authenticated_rollover_source,
)
from ai_dev_loop.scheduler.application.contracts import (
    RolloverStartResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    active_rollover_tick_next_action,
)
from ai_dev_loop.scheduler.application.fresh_agent_successor import (
    fresh_agent_successor_codex_checkpoint,
    fresh_agent_successor_context,
    fresh_agent_successor_cursor_checkpoint,
)
from ai_dev_loop.scheduler.application.git_admission import format_admission_artifact_text
from ai_dev_loop.scheduler.application.rollover_artifacts import (
    ROLLOVER_ADMISSION_STATUS_ARTIFACT,
    load_rollover_definition,
    load_rollover_seed_evidence,
    load_rollover_start_intent,
    persist_rollover_start_intent,
    rollover_run_id_for,
)
from ai_dev_loop.scheduler.application.rollover_worktree import (
    ProductionRolloverWorktreePort,
    RolloverSeedEvidence,
    RolloverWorktreePort,
)
from ai_dev_loop.scheduler.domain.events import (
    AuthenticatedRolloverStartedEvent,
    SequenceBlockedEvent,
)
from ai_dev_loop.scheduler.domain.rollover import (
    ACTIVE_ROLLOVER_STATE_KIND,
    PREPARED_ROLLOVER_STATE_KIND,
    ROLLOVER_ABORT_INTENT_ARTIFACT,
    ROLLOVER_PRE_SEED_ADMISSION_ARTIFACT,
    ROLLOVER_SEED_EVIDENCE_ARTIFACT,
    ROLLOVER_SEQUENCE_BLOCK_REASON,
    ROLLOVER_START_INTENT_ARTIFACT,
    ROLLOVER_START_PROGRESS_ARTIFACT,
    ActiveRolloverState,
    AuthenticatedRolloverDefinition,
    PreparedRolloverState,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    BlockedSequenceState,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    AdmittedRunCheckpoint,
    AuthenticatedRolloverLineage,
    AwaitingCodexReviewState,
    FreshCodexReviewerBinding,
    RepositoryBinding,
    RepositoryTargetBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.infrastructure.paths import (
    default_artifact_root,
    default_engine_db_path,
    rollover_worktree_root,
    scheduler_state_dir,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import utc_now


def _authorize_sequence_block_for_rollover(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    *,
    sequence_id: str,
    source_run_id: str,
    now: datetime,
    event_id_factory: Callable[[], str],
) -> None:
    sequence_state = store.load_validated_sequence_state(conn, sequence_id)
    if isinstance(sequence_state, BlockedSequenceState):
        if sequence_state.current_run_id != source_run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "blocked sequence current run disagrees with rollover source",
            )
        if sequence_state.block_reason_kind != ROLLOVER_SEQUENCE_BLOCK_REASON:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "sequence block reason is not rollover-eligible",
            )
        return
    if not isinstance(sequence_state, ActiveSequenceState):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "sequence source must be active or blocked for rollover",
        )
    if sequence_state.current_run_id != source_run_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "active sequence current run disagrees with rollover source",
        )
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    blocked = BlockedSequenceState(
        schema_version=sequence_state.schema_version,
        sequence_id=sequence_state.sequence_id,
        version=sequence_state.version + 1,
        prepared_at=sequence_state.prepared_at,
        updated_at=now_text,
        started_at=sequence_state.started_at,
        blocked_at=now_text,
        block_reason_kind=ROLLOVER_SEQUENCE_BLOCK_REASON,
        idempotency_key=sequence_state.idempotency_key,
        definition=sequence_state.definition,
        current_ordinal=sequence_state.current_ordinal,
        current_run_id=source_run_id,
        materialized_entries=sequence_state.materialized_entries,
        residual_risk_ordinals=sequence_state.residual_risk_ordinals,
    )
    blocked_event = SequenceBlockedEvent(
        sequence_id=sequence_state.sequence_id,
        current_run_id=source_run_id,
        block_reason_kind=ROLLOVER_SEQUENCE_BLOCK_REASON,
    )
    sequence_num = store.next_event_sequence(conn, source_run_id)
    store.append_event(
        conn,
        event_id=event_id_factory(),
        run_id=source_run_id,
        sequence=sequence_num,
        event=blocked_event,
        now=now,
    )
    if not store.compare_and_swap_sequence_state(
        conn,
        sequence_id=sequence_state.sequence_id,
        expected_version=sequence_state.version,
        new_state=blocked,
        now=now,
    ):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "sequence block CAS failed during rollover start",
        )
    reservation = store.get_reservation_for_run(conn, source_run_id)
    if reservation is not None:
        worktree_key = str(reservation["worktree_key"])
        if worktree_key == blocked.definition.repository.worktree_key:
            store.release_reservation(conn, worktree_key=worktree_key, now=now)


def _managed_repository_binding(
    schema_version: int,
    *,
    worktree_root: str,
    worktree_key: str,
    seed: RolloverSeedEvidence,
    parent_head: str,
) -> RepositoryBinding | RepositoryTargetBinding:
    if schema_version in {
        SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
        SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    }:
        return RepositoryTargetBinding(root=worktree_root, worktree_key=worktree_key)
    return RepositoryBinding(
        root=worktree_root,
        git_common_dir=seed.managed_git_common_dir,
        git_dir=seed.managed_git_dir,
        branch=seed.managed_branch,
        initial_head=parent_head,
        worktree_key=worktree_key,
    )


class RolloverStartService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        worktree_port: RolloverWorktreePort | None = None,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        start_step_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._worktree = worktree_port or ProductionRolloverWorktreePort()
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._run_id_factory = run_id_factory
        self._start_step_hook = start_step_hook

    def _step(self, name: str) -> None:
        if self._start_step_hook is not None:
            self._start_step_hook(name)

    def _reject_abort_intent(self, rollover_id: str) -> None:
        abort_path = self.artifacts.run_root(rollover_id) / ROLLOVER_ABORT_INTENT_ARTIFACT
        if abort_path.is_file():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "rollover abort intent blocks start continuation",
            )

    def start(self, rollover_id: str) -> RolloverStartResult:
        with self.store.begin_read() as conn:
            row = self.store.get_authenticated_rollover(conn, rollover_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"rollover {rollover_id} not found",
            )
        state_kind = str(row["state_kind"])
        if state_kind == ACTIVE_ROLLOVER_STATE_KIND:
            return RolloverStartResult(
                rollover_id=rollover_id,
                rollover_run_id=str(row["rollover_run_id"]),
                source_run_id=str(row["source_run_id"]),
                state_kind=state_kind,
                changed=False,
                idempotent_replay=True,
                safe_next_action=active_rollover_tick_next_action(rollover_id),
            )
        if state_kind != PREPARED_ROLLOVER_STATE_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"rollover state {state_kind} is not eligible for start",
            )

        prepared = PreparedRolloverState.model_validate_json(str(row["state_payload"]))
        definition = load_rollover_definition(
            self.artifacts,
            rollover_id,
            definition_sha256=prepared.definition_sha256,
            definition_artifact_sha256=prepared.definition_artifact_sha256,
        )
        source_run_id = definition.source_run_id
        evidence = analyze_authenticated_rollover_source(
            self.store,
            self.artifacts,
            source_run_id,
            commit_message=definition.integration.commit_message,
            sequence_commit_message=definition.integration.commit_message
            if definition.sequence and not definition.sequence.is_final_phase
            else None,
        )
        if evidence.cursor.staged_patch_sha256 != definition.source_staged_patch_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "source staged patch drift since prepare",
            )

        now = self._now_factory()
        now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        run_id = (
            self._run_id_factory(evidence.context.project_name, now)
            if self._run_id_factory is not None
            else rollover_run_id_for(rollover_id)
        )
        idempotency_key = hashlib.sha256(
            f"{rollover_id}:{prepared.definition_sha256}".encode()
        ).hexdigest()

        start_intent_path = self.artifacts.run_root(rollover_id) / ROLLOVER_START_INTENT_ARTIFACT
        if start_intent_path.is_file():
            reconcile = self._reconcile_existing_start(
                rollover_id=rollover_id,
                prepared=prepared,
                definition=definition,
                source_run_id=source_run_id,
                run_id=run_id,
            )
            if reconcile is not None:
                return reconcile
            start_intent_sha256 = hashlib.sha256(start_intent_path.read_bytes()).hexdigest()
        else:
            stored_intent = persist_rollover_start_intent(
                self.artifacts,
                rollover_id,
                definition_sha256=prepared.definition_sha256,
                definition_artifact_sha256=prepared.definition_artifact_sha256,
                rollover_run_id=run_id,
                requested_at=now_text,
            )
            start_intent_sha256 = stored_intent.sha256

        self._reject_abort_intent(rollover_id)

        with self.store.begin_read() as conn:
            conflicting_recovery = self.store.find_conflicting_live_recovery_authority(
                conn,
                source_run_id=source_run_id,
            )
            if conflicting_recovery is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "source already has a live recovery authority",
                )
            conflicting_rollover = self.store.find_conflicting_live_rollover_authority(
                conn,
                source_run_id=source_run_id,
                exclude_rollover_id=rollover_id,
            )
            if conflicting_rollover is not None:
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.CONFLICT,
                    "source already has a live rollover authority",
                )
            source_state, _, _ = self.store.load_validated_snapshot(conn, source_run_id)

        if prepared.sequence is not None:
            with self.store.begin_immediate() as conn:
                _authorize_sequence_block_for_rollover(
                    self.store,
                    conn,
                    sequence_id=prepared.sequence.sequence_id,
                    source_run_id=source_run_id,
                    now=now,
                    event_id_factory=self._event_id_factory,
                )

        state_root = scheduler_state_dir()
        worktree_path = rollover_worktree_root(state_root, rollover_id)
        patch_path = self.artifacts.run_root(rollover_id) / definition.source_staged_patch_path
        seed_path = self.artifacts.run_root(rollover_id) / ROLLOVER_SEED_EVIDENCE_ARTIFACT
        if seed_path.is_file():
            seed_evidence_sha256 = hashlib.sha256(seed_path.read_bytes()).hexdigest()
            seed = self._load_authenticated_seed(
                rollover_id,
                definition,
                expected_seed_sha256=seed_evidence_sha256,
            )
        else:
            progress_bytes = json.dumps(
                {"step": "before_worktree", "requested_at": now_text},
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            self.artifacts.write_bytes(
                rollover_id,
                ROLLOVER_START_PROGRESS_ARTIFACT,
                progress_bytes,
                max_bytes=len(progress_bytes) + 1,
            )
            self._step("before_worktree")
            seed = self._worktree.create_and_seed(
                definition,
                patch_path=patch_path,
                worktree_path=worktree_path,
            )
            seed_bytes = json.dumps(seed.__dict__, indent=2, sort_keys=True).encode("utf-8")
            seed_artifact = self.artifacts.write_bytes(
                rollover_id,
                ROLLOVER_SEED_EVIDENCE_ARTIFACT,
                seed_bytes,
                max_bytes=len(seed_bytes) + 1,
            )
            seed_evidence_sha256 = seed_artifact.sha256
            self._step("after_seed")

        from ai_dev_loop.scheduler.application.git_admission import (
            GitAdmissionEvidence,
            discover_repository_bounded,
        )

        pre_seed_bytes = seed.pre_seed_admission_status.encode("utf-8")
        self.artifacts.write_bytes(
            rollover_id,
            ROLLOVER_PRE_SEED_ADMISSION_ARTIFACT,
            pre_seed_bytes,
            max_bytes=len(pre_seed_bytes) + 1,
        )
        worktree = Path(seed.worktree_path)
        if worktree.is_dir():
            post_seed = discover_repository_bounded(worktree)
            admission_evidence = GitAdmissionEvidence(
                resolved_root=seed.worktree_path,
                branch=post_seed.branch,
                head=seed.parent_head,
                git_common_dir=post_seed.git_common_dir,
                git_dir=post_seed.git_dir,
                status_porcelain=post_seed.status_porcelain,
            )
        else:
            admission_evidence = GitAdmissionEvidence(
                resolved_root=seed.worktree_path,
                branch=seed.managed_branch,
                head=seed.parent_head,
                git_common_dir=seed.managed_git_common_dir,
                git_dir=seed.managed_git_dir,
                status_porcelain=seed.post_seed_status_porcelain,
            )
        admission_text = format_admission_artifact_text(admission_evidence)
        admission_bytes = admission_text.encode("utf-8")
        admission_artifact = self.artifacts.write_bytes(
            run_id,
            ROLLOVER_ADMISSION_STATUS_ARTIFACT,
            admission_bytes,
            max_bytes=len(admission_bytes) + 1,
        )

        managed_repo = _managed_repository_binding(
            evidence.context.schema_version,
            worktree_root=str(worktree_path.resolve()),
            worktree_key=definition.rollover_worktree_key,
            seed=seed,
            parent_head=definition.parent_head,
        )
        with self.store.begin_read() as conn:
            successor_context = fresh_agent_successor_context(
                self.store,
                conn,
                source_state,
                context=evidence.context.model_copy(update={"repository": managed_repo}),
                clear_sequence_binding=evidence.context.sequence is not None,
            )
        context = successor_context.model_copy(
            update={
                "codex": definition.codex,
                "cursor": definition.cursor,
            }
        )
        self._copy_context_artifacts(
            source_run_id,
            run_id,
            rollover_id,
            context,
            definition=definition,
        )
        lineage = AuthenticatedRolloverLineage(
            rollover_id=rollover_id,
            source_run_id=source_run_id,
            source_staged_patch_sha256=definition.source_staged_patch_sha256,
            source_parent_head=definition.parent_head,
            source_final_review_result_sha256=definition.source_final_review_result_sha256,
            created_at=now_text,
        )
        run_state = AwaitingCodexReviewState(
            run_id=run_id,
            version=1,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=idempotency_key,
            context=context,
            checkpoint=AdmittedRunCheckpoint(
                authorized_at=now_text,
                authorized_controller_session_id=evidence.context.controller.controller_session_id,
                admitted_at=now_text,
                admission_status_artifact_path=ROLLOVER_ADMISSION_STATUS_ARTIFACT,
                admission_status_sha256=admission_artifact.sha256,
            ),
            cursor=fresh_agent_successor_cursor_checkpoint(evidence.cursor).model_copy(
                update={
                    "staged_patch_path": definition.source_staged_patch_path,
                    "staged_patch_sha256": definition.source_staged_patch_sha256,
                }
            ),
            codex=fresh_agent_successor_codex_checkpoint(),
            fresh_rollover=lineage,
        )
        event = AuthenticatedRolloverStartedEvent(
            rollover_id=rollover_id,
            source_run_id=source_run_id,
            rollover_run_id=run_id,
        )
        active = ActiveRolloverState(
            rollover_id=rollover_id,
            version=prepared.version + 1,
            updated_at=now_text,
            definition_sha256=prepared.definition_sha256,
            definition_artifact_sha256=prepared.definition_artifact_sha256,
            source_run_id=source_run_id,
            source_run_id_prefix=prepared.source_run_id_prefix,
            rollover_run_id=run_id,
            sequence=prepared.sequence,
            started_at=now_text,
            start_intent_artifact_sha256=start_intent_sha256,
            seed_evidence_artifact_sha256=seed_evidence_sha256,
        )
        with self.store.begin_immediate() as conn:
            replay = self.store.get_authenticated_rollover(conn, rollover_id)
            if replay is not None and str(replay["state_kind"]) == ACTIVE_ROLLOVER_STATE_KIND:
                return RolloverStartResult(
                    rollover_id=rollover_id,
                    rollover_run_id=str(replay["rollover_run_id"]),
                    source_run_id=source_run_id,
                    state_kind=ACTIVE_ROLLOVER_STATE_KIND,
                    changed=False,
                    idempotent_replay=True,
                    safe_next_action=active_rollover_tick_next_action(rollover_id),
                )
            self.store.ensure_rollover_target_reservation(
                conn,
                source_run_id=source_run_id,
                worktree_key=definition.repository.worktree_key,
                repository_root=definition.repository.root,
                now=now,
            )
            self.store.insert_authenticated_rollover_run(
                conn,
                run_id=run_id,
                state=run_state,
                event_id=self._event_id_factory(),
                event=event,
                now=now,
            )
            self.store.update_authenticated_rollover(
                conn,
                rollover_id=rollover_id,
                state=active,
                expected_version=prepared.version,
                now=now,
            )
        return RolloverStartResult(
            rollover_id=rollover_id,
            rollover_run_id=run_id,
            source_run_id=source_run_id,
            state_kind=active.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=active_rollover_tick_next_action(rollover_id),
        )

    def _load_authenticated_seed(
        self,
        rollover_id: str,
        definition: AuthenticatedRolloverDefinition,
        *,
        expected_seed_sha256: str | None = None,
    ) -> RolloverSeedEvidence:
        return load_rollover_seed_evidence(
            self.artifacts,
            rollover_id,
            definition=definition,
            expected_sha256=expected_seed_sha256,
        )

    def _reconcile_existing_start(
        self,
        *,
        rollover_id: str,
        prepared: PreparedRolloverState,
        definition: object,
        source_run_id: str,
        run_id: str,
    ) -> RolloverStartResult | None:
        intent_sha = hashlib.sha256(
            (self.artifacts.run_root(rollover_id) / ROLLOVER_START_INTENT_ARTIFACT).read_bytes()
        ).hexdigest()
        intent = load_rollover_start_intent(
            self.artifacts,
            rollover_id,
            expected_sha256=intent_sha,
        )
        if intent.get("rollover_run_id") != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "rollover start intent run identity mismatch",
            )
        if intent.get("definition_sha256") != prepared.definition_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "rollover start intent definition mismatch",
            )
        with self.store.begin_read() as conn:
            replay = self.store.get_authenticated_rollover(conn, rollover_id)
            if replay is not None and str(replay["state_kind"]) == ACTIVE_ROLLOVER_STATE_KIND:
                return RolloverStartResult(
                    rollover_id=rollover_id,
                    rollover_run_id=str(replay["rollover_run_id"]),
                    source_run_id=source_run_id,
                    state_kind=ACTIVE_ROLLOVER_STATE_KIND,
                    changed=False,
                    idempotent_replay=True,
                    safe_next_action=active_rollover_tick_next_action(rollover_id),
                )
            existing_run = conn.execute(
                "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if existing_run is not None:
            now = self._now_factory()
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            seed_sha = None
            seed_path = self.artifacts.run_root(rollover_id) / ROLLOVER_SEED_EVIDENCE_ARTIFACT
            if seed_path.is_file():
                seed_sha = hashlib.sha256(seed_path.read_bytes()).hexdigest()
            active = ActiveRolloverState(
                rollover_id=rollover_id,
                version=prepared.version + 1,
                updated_at=now_text,
                definition_sha256=prepared.definition_sha256,
                definition_artifact_sha256=prepared.definition_artifact_sha256,
                source_run_id=source_run_id,
                source_run_id_prefix=prepared.source_run_id_prefix,
                rollover_run_id=run_id,
                sequence=prepared.sequence,
                started_at=now_text,
                start_intent_artifact_sha256=intent_sha,
                seed_evidence_artifact_sha256=seed_sha,
            )
            with self.store.begin_immediate() as conn:
                self.store.update_authenticated_rollover(
                    conn,
                    rollover_id=rollover_id,
                    state=active,
                    expected_version=prepared.version,
                    now=now,
                )
            return RolloverStartResult(
                rollover_id=rollover_id,
                rollover_run_id=run_id,
                source_run_id=source_run_id,
                state_kind=active.kind,
                changed=True,
                idempotent_replay=False,
                safe_next_action=active_rollover_tick_next_action(rollover_id),
            )
        return None

    def _copy_context_artifacts(
        self,
        source_run_id: str,
        rollover_run_id: str,
        rollover_id: str,
        context: SubmittedRunContext,
        *,
        definition: object,
    ) -> None:
        from ai_dev_loop.scheduler.domain.rollover import AuthenticatedRolloverDefinition

        assert isinstance(definition, AuthenticatedRolloverDefinition)
        if not isinstance(context.codex, FreshCodexReviewerBinding):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "authenticated rollover requires FreshCodexReviewerBinding",
            )
        source_bindings = (
            (context.plan_prompt.plan_artifact_path, context.plan_prompt.plan_sha256),
            (context.plan_prompt.prompt_artifact_path, context.plan_prompt.prompt_sha256),
            (
                context.effective_config.effective_config_artifact_path,
                context.effective_config.effective_config_sha256,
            ),
            (
                context.effective_config.source_config_artifact_path,
                context.effective_config.source_config_sha256,
            ),
        )
        for relative, expected_sha in source_bindings:
            content = self.artifacts.read_verified_bytes(
                source_run_id,
                relative,
                expected_sha256=expected_sha,
            )
            self.artifacts.write_bytes(
                rollover_run_id,
                relative,
                content,
                max_bytes=len(content) + 1,
            )
        binding_content = self.artifacts.read_verified_bytes(
            rollover_id,
            context.codex.binding_artifact_path,
            expected_sha256=context.codex.binding_sha256,
        )
        self.artifacts.write_bytes(
            rollover_run_id,
            context.codex.binding_artifact_path,
            binding_content,
            max_bytes=len(binding_content) + 1,
        )
        patch = self.artifacts.read_verified_bytes(
            rollover_id,
            definition.source_staged_patch_path,
            expected_sha256=definition.source_staged_patch_sha256,
        )
        self.artifacts.write_bytes(
            rollover_run_id,
            definition.source_staged_patch_path,
            patch,
            max_bytes=len(patch) + 1,
        )


def default_rollover_start_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    worktree_port: RolloverWorktreePort | None = None,
) -> RolloverStartService:
    db = db_path or default_engine_db_path()
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return RolloverStartService(
        SqliteSchedulerStore(db),
        artifacts,
        worktree_port=worktree_port,
    )


def start_rollover(rollover_id: str, *, db_path: Path | None = None) -> RolloverStartResult:
    service = default_rollover_start_service(db_path=db_path)
    return service.start(rollover_id)
