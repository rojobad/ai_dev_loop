"""Bridge scheduler frozen context into legacy RunState helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.scheduler.application.scheduler_checkpoint import checkpoint_from_state
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    CodexRuntimeBinding,
    RepositoryBinding,
    SubmittedRunContext,
)
from ai_dev_loop.scheduler.domain.state import (
    FreshCodexReviewerBinding as SchedulerFreshCodexReviewerBinding,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.state import (
    RUN_STATE_SCHEMA_VERSION_FRESH,
    CodexState,
    CursorState,
    FreshCodexReviewerBinding,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
)


def _repository_state(
    context: SubmittedRunContext,
    repo_root: Path,
    *,
    run_id: str | None = None,
    checkpoint: AdmittedRunCheckpoint | None = None,
    artifacts: ProtectedArtifactStore | None = None,
) -> RepositoryState:
    repository = context.repository
    if isinstance(repository, RepositoryBinding):
        baseline = ""
        if context.baseline_status_artifact_path is not None:
            baseline = context.baseline_status_artifact_path
        return RepositoryState(
            root=repository.root,
            git_common_dir=repository.git_common_dir,
            git_dir=repository.git_dir,
            branch=repository.branch,
            initial_head=repository.initial_head,
            baseline_status_path=baseline,
        )
    if checkpoint is not None and artifacts is not None and run_id is not None:
        from ai_dev_loop.scheduler.application.cursor_evidence import frozen_repository_identity

        identity = frozen_repository_identity(
            context,
            run_id=run_id,
            artifacts=artifacts,
            checkpoint=checkpoint,
        )
        baseline = ""
        if context.baseline_status_artifact_path is not None:
            baseline = context.baseline_status_artifact_path
        return RepositoryState(
            root=identity.root,
            git_common_dir=identity.git_common_dir,
            git_dir=identity.git_dir,
            branch=identity.branch,
            initial_head=identity.initial_head,
            baseline_status_path=baseline,
        )
    discovered = discover_repository(repo_root)
    return RepositoryState(
        root=str(repo_root),
        git_common_dir=str(discovered.git_common_dir),
        git_dir=str(discovered.git_dir),
        branch=discovered.branch,
        initial_head=discovered.head,
        baseline_status_path="",
    )


def _codex_state(context: SubmittedRunContext) -> CodexState:
    codex = context.codex
    if isinstance(codex, SchedulerFreshCodexReviewerBinding):
        fresh_reviewer = FreshCodexReviewerBinding(
            review_model=codex.review_model,
            review_reasoning_effort=codex.review_reasoning_effort,
        )
        return CodexState(
            command=codex.command,
            review_model=codex.review_model,
            review_reasoning_effort=codex.review_reasoning_effort,
            review_model_source=codex.review_model_source,
            review_reasoning_source=codex.review_reasoning_source,
            review_skill=codex.review_skill,
            sandbox=codex.sandbox,
            fresh_reviewer=fresh_reviewer,
        )
    assert isinstance(codex, CodexRuntimeBinding)
    return CodexState(
        command=codex.command,
        session_id=codex.session_id,
        session_model=codex.session_model,
        session_reasoning_effort=codex.session_reasoning_effort,
        review_model=codex.review_model,
        review_reasoning_effort=codex.review_reasoning_effort,
        review_model_source=codex.review_model_source,
        review_reasoning_source=codex.review_reasoning_source,
        model_family_warning=codex.model_family_warning,
        review_skill=codex.review_skill,
        sandbox=codex.sandbox,
    )


def run_state_from_scheduler_context(
    *,
    run_id: str,
    context: SubmittedRunContext,
    repo_root: Path,
    chat_id: str | None = None,
    checkpoint: AdmittedRunCheckpoint | None = None,
    artifacts: ProtectedArtifactStore | None = None,
    state: object | None = None,
) -> RunState:
    """Build a minimal RunState for shared staging/preflight helpers."""

    if checkpoint is None and state is not None:
        checkpoint = checkpoint_from_state(state)
    repository = _repository_state(
        context,
        repo_root,
        run_id=run_id,
        checkpoint=checkpoint,
        artifacts=artifacts,
    )
    plan_prompt = context.plan_prompt
    now = datetime.now(UTC)
    schema_version = (
        RUN_STATE_SCHEMA_VERSION_FRESH
        if isinstance(context.codex, SchedulerFreshCodexReviewerBinding)
        else 1
    )
    return RunState(
        schema_version=schema_version,
        run_id=run_id,
        status=RunStatus.STAGING,
        created_at=now,
        updated_at=now,
        project=ProjectRef(name=context.project_name),
        repository=repository,
        plan=PlanState(
            repository_path=plan_prompt.plan_repository_path,
            snapshot_path=plan_prompt.plan_artifact_path,
            sha256=plan_prompt.plan_sha256,
        ),
        prompt=PromptState(
            source_repository_path=plan_prompt.prompt_source_repository_path,
            snapshot_path=plan_prompt.prompt_artifact_path,
            sha256=plan_prompt.prompt_sha256,
        ),
        workflow=WorkflowState(
            max_review_iterations=context.workflow.max_review_iterations,
            stage_mode=context.workflow.stage_mode,
            cursor_timeout_minutes=context.workflow.cursor_timeout_minutes,
            codex_timeout_minutes=context.workflow.codex_timeout_minutes,
            current_review_iteration=0,
        ),
        cursor=CursorState(
            command=context.cursor.command,
            model=context.cursor.model,
            output_format=context.cursor.output_format,
            force=context.cursor.force,
            trust_workspace=context.cursor.trust_workspace,
            sandbox=context.cursor.sandbox,
            chat_id=chat_id,
        ),
        codex=_codex_state(context),
    )
