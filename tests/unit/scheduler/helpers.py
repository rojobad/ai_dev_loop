"""Shared scheduler test helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_dev_loop.scheduler.domain.state import (
    CodexRuntimeBinding,
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    PlanPromptBinding,
    RepositoryBinding,
    SubmittedRunContext,
    SubmittedState,
    WorkflowLimits,
)

REVIEWER_SESSION = "019abc00-0000-0000-0000-000000000000"
CONTROLLER_SESSION = "11111111-1111-1111-1111-111111111111"
DIGEST = "a" * 64


def sample_submitted_context(*, repo_root: str = "/tmp/repo") -> SubmittedRunContext:
    return SubmittedRunContext(
        project_name="fixture-project",
        repository=RepositoryBinding(
            root=repo_root,
            git_common_dir=f"{repo_root}/.git",
            git_dir=f"{repo_root}/.git",
            branch="main",
            initial_head="abc123",
            worktree_key=DIGEST,
        ),
        plan_prompt=PlanPromptBinding(
            plan_repository_path="docs/plans/sample-plan.md",
            prompt_source_repository_path="docs/plans/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256=DIGEST,
            prompt_artifact_path="prompts/cursor-initial.txt",
            prompt_sha256=DIGEST,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path="effective-config.yaml",
            effective_config_sha256=DIGEST,
            source_config_artifact_path="source-config.yaml",
            source_config_sha256=DIGEST,
        ),
        codex=CodexRuntimeBinding(
            session_id=REVIEWER_SESSION,
            session_model="gpt-5.6-sol",
            session_reasoning_effort="high",
            review_model="gpt-5.6-sol",
            review_reasoning_effort="high",
            review_model_source="session",
            review_reasoning_source="session",
            model_family_warning=None,
            session_origin="native_wsl",
            source_event_type="turn_context",
            source_timestamp="2026-07-10T12:00:02.000Z",
            command="codex",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
            session_runtime_artifact_path="codex/session-runtime.json",
            session_runtime_sha256=DIGEST,
        ),
        cursor=CursorBinding(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowLimits(
            max_review_iterations=3,
            stage_mode="all",
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
            require_clean_worktree=True,
        ),
        controller=ControllerBinding(controller_session_id=CONTROLLER_SESSION),
        baseline_status_artifact_path="git/baseline-status.txt",
        baseline_status_sha256=DIGEST,
    )


def sample_submitted_state(
    *,
    run_id: str = "fixture-project-20260904T120000Z-abc123",
    repo_root: str = "/tmp/repo",
) -> SubmittedState:
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return SubmittedState(
        run_id=run_id,
        version=1,
        submitted_at=now,
        updated_at=now,
        idempotency_key="b" * 64,
        context=sample_submitted_context(repo_root=repo_root),
    )
