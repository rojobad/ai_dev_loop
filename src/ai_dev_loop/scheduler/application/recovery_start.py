"""Fresh-review recovery start authorization for Phase 20.6."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    RecoveryStartResult,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    active_recovery_tick_next_action,
)
from ai_dev_loop.scheduler.application.fresh_review_recovery_source import (
    analyze_fresh_review_recovery_source,
)
from ai_dev_loop.scheduler.application.git_admission import format_admission_artifact_text
from ai_dev_loop.scheduler.application.recovery_artifacts import (
    RECOVERY_ADMISSION_STATUS_ARTIFACT,
    load_recovery_definition,
    load_recovery_seed_evidence,
    load_recovery_start_intent,
    persist_recovery_start_intent,
    recovery_run_id_for,
)
from ai_dev_loop.scheduler.application.recovery_worktree import (
    ProductionRecoveryWorktreePort,
    RecoverySeedEvidence,
    RecoveryWorktreePort,
)
from ai_dev_loop.scheduler.domain.events import FreshReviewRecoveryStartedEvent
from ai_dev_loop.scheduler.domain.recovery import (
    ACTIVE_RECOVERY_STATE_KIND,
    PREPARED_RECOVERY_STATE_KIND,
    RECOVERY_ABORT_INTENT_ARTIFACT,
    RECOVERY_PRE_SEED_ADMISSION_ARTIFACT,
    RECOVERY_SEED_EVIDENCE_ARTIFACT,
    RECOVERY_START_INTENT_ARTIFACT,
    RECOVERY_START_PROGRESS_ARTIFACT,
    ActiveRecoveryState,
    FreshReviewRecoveryDefinition,
    PreparedRecoveryState,
)
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_SEQUENCE,
    AdmittedRunCheckpoint,
    AwaitingCodexReviewState,
    CursorWorkflowCheckpoint,
    FreshCodexReviewerBinding,
    FreshReviewRecoveryLineage,
    RepositoryBinding,
    RepositoryTargetBinding,
    SubmittedRunContext,
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


def _managed_repository_binding(
    schema_version: int,
    *,
    worktree_root: str,
    worktree_key: str,
    seed: RecoverySeedEvidence,
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


class RecoveryStartService:
    def __init__(
        self,
        store: SqliteSchedulerStore,
        artifacts: ProtectedArtifactStore,
        *,
        worktree_port: RecoveryWorktreePort | None = None,
        now_factory: Callable[[], datetime] | None = None,
        event_id_factory: Callable[[], str] | None = None,
        run_id_factory: Callable[[str, datetime], str] | None = None,
        start_step_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self._worktree = worktree_port or ProductionRecoveryWorktreePort()
        self._now_factory = now_factory or (lambda: utc_now())
        self._event_id_factory = event_id_factory or (lambda: f"evt-{secrets.token_hex(16)}")
        self._run_id_factory = run_id_factory
        self._start_step_hook = start_step_hook

    def _step(self, name: str) -> None:
        if self._start_step_hook is not None:
            self._start_step_hook(name)

    def _reject_abort_intent(self, recovery_id: str) -> None:
        abort_path = self.artifacts.run_root(recovery_id) / RECOVERY_ABORT_INTENT_ARTIFACT
        if abort_path.is_file():
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "recovery abort intent blocks start continuation",
            )

    def start(self, recovery_id: str) -> RecoveryStartResult:
        with self.store.begin_read() as conn:
            row = self.store.get_fresh_review_recovery(conn, recovery_id)
        if row is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.NOT_FOUND,
                f"recovery {recovery_id} not found",
            )
        state_kind = str(row["state_kind"])
        if state_kind == ACTIVE_RECOVERY_STATE_KIND:
            return RecoveryStartResult(
                recovery_id=recovery_id,
                recovery_run_id=str(row["recovery_run_id"]),
                source_run_id=str(row["source_run_id"]),
                state_kind=state_kind,
                changed=False,
                idempotent_replay=True,
                safe_next_action=active_recovery_tick_next_action(recovery_id),
            )
        if state_kind != PREPARED_RECOVERY_STATE_KIND:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                f"recovery state {state_kind} is not eligible for start",
            )

        prepared = PreparedRecoveryState.model_validate_json(str(row["state_payload"]))
        definition = load_recovery_definition(
            self.artifacts,
            recovery_id,
            definition_sha256=prepared.definition_sha256,
            definition_artifact_sha256=prepared.definition_artifact_sha256,
        )
        source_run_id = definition.source_run_id
        evidence = analyze_fresh_review_recovery_source(
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
            else recovery_run_id_for(recovery_id)
        )
        idempotency_key = hashlib.sha256(
            f"{recovery_id}:{prepared.definition_sha256}".encode()
        ).hexdigest()

        start_intent_path = self.artifacts.run_root(recovery_id) / RECOVERY_START_INTENT_ARTIFACT
        if start_intent_path.is_file():
            reconcile = self._reconcile_existing_start(
                recovery_id=recovery_id,
                prepared=prepared,
                definition=definition,
                source_run_id=source_run_id,
                run_id=run_id,
            )
            if reconcile is not None:
                return reconcile
            start_intent_sha256 = hashlib.sha256(start_intent_path.read_bytes()).hexdigest()
        else:
            stored_intent = persist_recovery_start_intent(
                self.artifacts,
                recovery_id,
                definition_sha256=prepared.definition_sha256,
                definition_artifact_sha256=prepared.definition_artifact_sha256,
                recovery_run_id=run_id,
                requested_at=now_text,
            )
            start_intent_sha256 = stored_intent.sha256

        self._reject_abort_intent(recovery_id)

        state_root = scheduler_state_dir()
        worktree_path = recovery_worktree_root(state_root, recovery_id)
        patch_path = self.artifacts.run_root(recovery_id) / definition.source_staged_patch_path
        seed_path = self.artifacts.run_root(recovery_id) / RECOVERY_SEED_EVIDENCE_ARTIFACT
        if seed_path.is_file():
            seed_evidence_sha256 = hashlib.sha256(seed_path.read_bytes()).hexdigest()
            seed = self._load_authenticated_seed(
                recovery_id,
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
                recovery_id,
                RECOVERY_START_PROGRESS_ARTIFACT,
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
                recovery_id,
                RECOVERY_SEED_EVIDENCE_ARTIFACT,
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
            recovery_id,
            RECOVERY_PRE_SEED_ADMISSION_ARTIFACT,
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
            RECOVERY_ADMISSION_STATUS_ARTIFACT,
            admission_bytes,
            max_bytes=len(admission_bytes) + 1,
        )

        managed_repo = _managed_repository_binding(
            evidence.context.schema_version,
            worktree_root=str(worktree_path.resolve()),
            worktree_key=definition.recovery_worktree_key,
            seed=seed,
            parent_head=definition.parent_head,
        )
        context = SubmittedRunContext(
            schema_version=evidence.context.schema_version,
            project_name=evidence.context.project_name,
            repository=managed_repo,
            plan_prompt=evidence.context.plan_prompt,
            effective_config=evidence.context.effective_config,
            codex=evidence.context.codex,
            cursor=evidence.context.cursor,
            workflow=evidence.context.workflow,
            controller=evidence.context.controller,
            sequence=evidence.context.sequence,
        )
        self._copy_context_artifacts(
            source_run_id,
            run_id,
            recovery_id,
            context,
            definition=definition,
        )
        lineage = FreshReviewRecoveryLineage(
            recovery_id=recovery_id,
            source_run_id=source_run_id,
            source_staged_patch_sha256=definition.source_staged_patch_sha256,
            source_parent_head=definition.parent_head,
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
                admission_status_artifact_path=RECOVERY_ADMISSION_STATUS_ARTIFACT,
                admission_status_sha256=admission_artifact.sha256,
            ),
            cursor=CursorWorkflowCheckpoint(
                iteration=evidence.cursor.iteration,
                staged_patch_path=definition.source_staged_patch_path,
                staged_patch_sha256=definition.source_staged_patch_sha256,
            ),
            fresh_recovery=lineage,
        )
        event = FreshReviewRecoveryStartedEvent(
            recovery_id=recovery_id,
            source_run_id=source_run_id,
            recovery_run_id=run_id,
        )
        active = ActiveRecoveryState(
            recovery_id=recovery_id,
            version=prepared.version + 1,
            updated_at=now_text,
            definition_sha256=prepared.definition_sha256,
            definition_artifact_sha256=prepared.definition_artifact_sha256,
            source_run_id=source_run_id,
            source_run_id_prefix=prepared.source_run_id_prefix,
            recovery_run_id=run_id,
            sequence=prepared.sequence,
            started_at=now_text,
            start_intent_artifact_sha256=start_intent_sha256,
            seed_evidence_artifact_sha256=seed_evidence_sha256,
        )
        with self.store.begin_immediate() as conn:
            replay = self.store.get_fresh_review_recovery(conn, recovery_id)
            if replay is not None and str(replay["state_kind"]) == ACTIVE_RECOVERY_STATE_KIND:
                return RecoveryStartResult(
                    recovery_id=recovery_id,
                    recovery_run_id=str(replay["recovery_run_id"]),
                    source_run_id=source_run_id,
                    state_kind=ACTIVE_RECOVERY_STATE_KIND,
                    changed=False,
                    idempotent_replay=True,
                    safe_next_action=active_recovery_tick_next_action(recovery_id),
                )
            self.store.ensure_recovery_target_reservation(
                conn,
                source_run_id=source_run_id,
                worktree_key=definition.repository.worktree_key,
                repository_root=definition.repository.root,
                now=now,
            )
            self.store.insert_fresh_review_recovery_run(
                conn,
                run_id=run_id,
                state=run_state,
                event_id=self._event_id_factory(),
                event=event,
                now=now,
            )
            self.store.update_fresh_review_recovery(
                conn,
                recovery_id=recovery_id,
                state=active,
                expected_version=prepared.version,
                now=now,
            )
        return RecoveryStartResult(
            recovery_id=recovery_id,
            recovery_run_id=run_id,
            source_run_id=source_run_id,
            state_kind=active.kind,
            changed=True,
            idempotent_replay=False,
            safe_next_action=active_recovery_tick_next_action(recovery_id),
        )

    def _load_authenticated_seed(
        self,
        recovery_id: str,
        definition: FreshReviewRecoveryDefinition,
        *,
        expected_seed_sha256: str | None = None,
    ) -> RecoverySeedEvidence:
        return load_recovery_seed_evidence(
            self.artifacts,
            recovery_id,
            definition=definition,
            expected_sha256=expected_seed_sha256,
        )

    def _reconcile_existing_start(
        self,
        *,
        recovery_id: str,
        prepared: PreparedRecoveryState,
        definition: object,
        source_run_id: str,
        run_id: str,
    ) -> RecoveryStartResult | None:
        intent_sha = hashlib.sha256(
            (self.artifacts.run_root(recovery_id) / RECOVERY_START_INTENT_ARTIFACT).read_bytes()
        ).hexdigest()
        intent = load_recovery_start_intent(
            self.artifacts,
            recovery_id,
            expected_sha256=intent_sha,
        )
        if intent.get("recovery_run_id") != run_id:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery start intent run identity mismatch",
            )
        if intent.get("definition_sha256") != prepared.definition_sha256:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "recovery start intent definition mismatch",
            )
        with self.store.begin_read() as conn:
            replay = self.store.get_fresh_review_recovery(conn, recovery_id)
            if replay is not None and str(replay["state_kind"]) == ACTIVE_RECOVERY_STATE_KIND:
                return RecoveryStartResult(
                    recovery_id=recovery_id,
                    recovery_run_id=str(replay["recovery_run_id"]),
                    source_run_id=source_run_id,
                    state_kind=ACTIVE_RECOVERY_STATE_KIND,
                    changed=False,
                    idempotent_replay=True,
                    safe_next_action=active_recovery_tick_next_action(recovery_id),
                )
            existing_run = conn.execute(
                "SELECT run_id FROM scheduler_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if existing_run is not None:
            now = self._now_factory()
            now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            active = ActiveRecoveryState(
                recovery_id=recovery_id,
                version=prepared.version + 1,
                updated_at=now_text,
                definition_sha256=prepared.definition_sha256,
                definition_artifact_sha256=prepared.definition_artifact_sha256,
                source_run_id=source_run_id,
                source_run_id_prefix=prepared.source_run_id_prefix,
                recovery_run_id=run_id,
                sequence=prepared.sequence,
                started_at=now_text,
                start_intent_artifact_sha256=intent_sha,
            )
            with self.store.begin_immediate() as conn:
                self.store.update_fresh_review_recovery(
                    conn,
                    recovery_id=recovery_id,
                    state=active,
                    expected_version=prepared.version,
                    now=now,
                )
            return RecoveryStartResult(
                recovery_id=recovery_id,
                recovery_run_id=run_id,
                source_run_id=source_run_id,
                state_kind=active.kind,
                changed=True,
                idempotent_replay=False,
                safe_next_action=active_recovery_tick_next_action(recovery_id),
            )
        return None

    def _copy_context_artifacts(
        self,
        source_run_id: str,
        recovery_run_id: str,
        recovery_id: str,
        context: SubmittedRunContext,
        *,
        definition: object,
    ) -> None:
        from ai_dev_loop.scheduler.domain.recovery import FreshReviewRecoveryDefinition

        assert isinstance(definition, FreshReviewRecoveryDefinition)
        if not isinstance(context.codex, FreshCodexReviewerBinding):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "fresh recovery requires FreshCodexReviewerBinding",
            )
        bindings = (
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
            (context.codex.binding_artifact_path, context.codex.binding_sha256),
        )
        for relative, expected_sha in bindings:
            content = self.artifacts.read_verified_bytes(
                source_run_id,
                relative,
                expected_sha256=expected_sha,
            )
            self.artifacts.write_bytes(
                recovery_run_id,
                relative,
                content,
                max_bytes=len(content) + 1,
            )
        patch = self.artifacts.read_verified_bytes(
            recovery_id,
            definition.source_staged_patch_path,
            expected_sha256=definition.source_staged_patch_sha256,
        )
        self.artifacts.write_bytes(
            recovery_run_id,
            definition.source_staged_patch_path,
            patch,
            max_bytes=len(patch) + 1,
        )


def default_recovery_start_service(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    worktree_port: RecoveryWorktreePort | None = None,
) -> RecoveryStartService:
    db = db_path or default_engine_db_path()
    artifacts = ProtectedArtifactStore(artifact_root or default_artifact_root())
    return RecoveryStartService(
        SqliteSchedulerStore(db),
        artifacts,
        worktree_port=worktree_port,
    )


def start_recovery(recovery_id: str, *, db_path: Path | None = None) -> RecoveryStartResult:
    service = default_recovery_start_service(db_path=db_path)
    return service.start(recovery_id)
