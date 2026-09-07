"""Unit tests for scheduler domain schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_ADAPTER,
    SUBMITTED_STATE_ADAPTER,
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


def _sample_context() -> SubmittedRunContext:
    digest = "a" * 64
    return SubmittedRunContext(
        project_name="fixture-project",
        repository=RepositoryBinding(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
            initial_head="abc123",
            worktree_key=digest,
        ),
        plan_prompt=PlanPromptBinding(
            plan_repository_path="docs/plans/sample-plan.md",
            prompt_source_repository_path="docs/plans/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256=digest,
            prompt_artifact_path="prompts/cursor-initial.txt",
            prompt_sha256=digest,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path="effective-config.yaml",
            effective_config_sha256=digest,
            source_config_artifact_path="source-config.yaml",
            source_config_sha256=digest,
        ),
        codex=CodexRuntimeBinding(
            session_id="019abc00-0000-0000-0000-000000000000",
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
            session_runtime_sha256=digest,
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
        controller=ControllerBinding(
            controller_session_id="11111111-1111-1111-1111-111111111111",
        ),
        baseline_status_artifact_path="git/baseline-status.txt",
        baseline_status_sha256=digest,
    )


def test_submitted_context_model_schema_alignment() -> None:
    context = _sample_context()
    json_payload = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(context))
    schema = json.loads(
        schema_path("scheduler-submitted-run-context-v1.json").read_text(encoding="utf-8")
    )
    # Pydantic round-trip is the contract; schema file documents the persisted shape.
    assert json_payload["schema_version"] == 1
    assert (
        json_payload["controller"]["controller_session_id"] != json_payload["codex"]["session_id"]
    )
    assert schema["properties"]["schema_version"]["const"] == 1


def test_submitted_state_model_schema_alignment() -> None:
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state = SubmittedState(
        run_id="fixture-project-20260904T120000Z-abc123",
        version=1,
        submitted_at=now,
        updated_at=now,
        idempotency_key="b" * 64,
        context=_sample_context(),
    )
    json_payload = json.loads(SUBMITTED_STATE_ADAPTER.dump_json(state))
    schema = json.loads(
        schema_path("scheduler-submitted-state-v1.json").read_text(encoding="utf-8")
    )
    assert json_payload["kind"] == "queued"
    assert schema["properties"]["kind"]["const"] == "queued"


def test_submitted_context_rejects_empty_required_strings() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError):
        SubmittedRunContext(
            project_name="",
            repository=RepositoryBinding(
                root="/tmp/repo",
                git_common_dir="/tmp/repo/.git",
                git_dir="/tmp/repo/.git",
                branch="main",
                initial_head="abc123",
                worktree_key=digest,
            ),
            plan_prompt=PlanPromptBinding(
                plan_repository_path="docs/plans/sample-plan.md",
                prompt_source_repository_path="docs/plans/prompt.txt",
                plan_artifact_path="plan/plan.md",
                plan_sha256=digest,
                prompt_artifact_path="prompts/cursor-initial.txt",
                prompt_sha256=digest,
            ),
            effective_config=EffectiveConfigBinding(
                effective_config_artifact_path="effective-config.yaml",
                effective_config_sha256=digest,
                source_config_artifact_path="source-config.yaml",
                source_config_sha256=digest,
            ),
            codex=CodexRuntimeBinding(
                session_id="019abc00-0000-0000-0000-000000000000",
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
                session_runtime_sha256=digest,
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
            controller=ControllerBinding(
                controller_session_id="11111111-1111-1111-1111-111111111111",
            ),
            baseline_status_artifact_path="git/baseline-status.txt",
            baseline_status_sha256=digest,
        )


def test_submitted_state_rejects_empty_run_id() -> None:
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with pytest.raises(ValidationError):
        SubmittedState(
            run_id="",
            version=1,
            submitted_at=now,
            updated_at=now,
            idempotency_key="b" * 64,
            context=_sample_context(),
        )


def test_repository_binding_rejects_empty_branch() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError):
        RepositoryBinding(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="",
            initial_head="abc123",
            worktree_key=digest,
        )
