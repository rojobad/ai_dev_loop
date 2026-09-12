"""Unit tests for scheduler domain schemas."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.state import (
    SUBMITTED_CONTEXT_ADAPTER,
    SUBMITTED_CONTEXT_SCHEMA_VERSION,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
    SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH,
    SUBMITTED_STATE_ADAPTER,
    CodexRuntimeBinding,
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    RepositoryTargetBinding,
    SubmittedRunContext,
    SubmittedState,
    WorkflowLimits,
)


def _sample_agent_led_context() -> SubmittedRunContext:
    digest = "a" * 64
    return SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
        project_name="fixture-project",
        repository=RepositoryTargetBinding(
            root="/tmp/repo",
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
        codex=FreshCodexReviewerBinding(
            review_model="gpt-5.6-sol",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
            binding_artifact_path="codex/fresh-reviewer-input.json",
            binding_sha256=digest,
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
    )


def _sample_fresh_context() -> SubmittedRunContext:
    digest = "a" * 64
    return SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_FRESH,
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
        codex=FreshCodexReviewerBinding(
            review_model="gpt-5.6-sol",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
            binding_artifact_path="codex/fresh-reviewer-input.json",
            binding_sha256=digest,
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


def _sample_legacy_context() -> SubmittedRunContext:
    digest = "a" * 64
    return SubmittedRunContext(
        schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION,
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


def test_submitted_context_accepts_null_controller_provenance() -> None:
    context = _sample_agent_led_context().model_copy(
        update={
            "controller": ControllerBinding(controller_session_id=None),
        }
    )
    payload = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(context))
    assert payload["controller"]["controller_session_id"] is None


def test_submitted_context_model_schema_alignment() -> None:
    context = _sample_agent_led_context()
    json_payload = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(context))
    assert json_payload["schema_version"] == 3
    assert json_payload["codex"]["review_model_source"] == "explicit"
    assert "baseline_status_artifact_path" not in json_payload

    fresh = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(_sample_fresh_context()))
    assert fresh["schema_version"] == 2
    assert fresh["baseline_status_artifact_path"] == "git/baseline-status.txt"

    legacy = json.loads(SUBMITTED_CONTEXT_ADAPTER.dump_json(_sample_legacy_context()))
    assert legacy["schema_version"] == 1
    assert legacy["controller"]["controller_session_id"] != legacy["codex"]["session_id"]


def test_submitted_state_model_schema_alignment() -> None:
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    state = SubmittedState(
        run_id="fixture-project-20260904T120000Z-abc123",
        version=1,
        submitted_at=now,
        updated_at=now,
        idempotency_key="b" * 64,
        context=_sample_agent_led_context(),
    )
    json_payload = json.loads(SUBMITTED_STATE_ADAPTER.dump_json(state))
    schema = json.loads(
        schema_path("scheduler-submitted-state-v1.json").read_text(encoding="utf-8")
    )
    context_v3 = json.loads(
        schema_path("scheduler-submitted-run-context-v3.json").read_text(encoding="utf-8")
    )
    assert json_payload["kind"] == "queued"
    assert schema["properties"]["kind"]["const"] == "queued"
    assert context_v3["properties"]["schema_version"]["const"] == 3
    assert "repository_target_binding" in context_v3["$defs"]


def test_run_state_v2_schema_aligns_with_fresh_codex_binding() -> None:
    from ai_dev_loop.state import FreshCodexReviewerBinding as RunFreshBinding

    schema = json.loads(schema_path("run-state-v2.json").read_text(encoding="utf-8"))
    codex_schema = schema["properties"]["codex"]
    assert schema["properties"]["schema_version"]["const"] == 2
    assert "fresh_reviewer" in codex_schema["required"]
    assert set(RunFreshBinding.model_fields) == set(
        codex_schema["properties"]["fresh_reviewer"]["properties"].keys()
    )
    assert codex_schema["properties"]["session_id"]["type"] == ["string", "null"]


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
            context=_sample_agent_led_context(),
        )


def test_submitted_context_v3_rejects_baseline_fields() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError, match="must not include baseline fields"):
        SubmittedRunContext(
            schema_version=SUBMITTED_CONTEXT_SCHEMA_VERSION_AGENT_LED,
            project_name="fixture-project",
            repository=RepositoryTargetBinding(root="/tmp/repo", worktree_key=digest),
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
            codex=FreshCodexReviewerBinding(
                review_model="gpt-5.6-sol",
                review_reasoning_effort="high",
                review_model_source="explicit",
                review_reasoning_source="explicit",
                command="codex",
                review_skill="review-staged-cursor-execution",
                sandbox="workspace-write",
                binding_artifact_path="codex/fresh-reviewer-input.json",
                binding_sha256=digest,
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


def test_submitted_context_v3_rejects_extra_repository_fields() -> None:
    digest = "a" * 64
    with pytest.raises(ValidationError):
        RepositoryTargetBinding(
            root="/tmp/repo",
            worktree_key=digest,
            branch="main",  # type: ignore[call-arg]
        )


def test_phase17_2_admission_artifact_contract() -> None:
    from ai_dev_loop.scheduler.application.submission import BASELINE_STATUS_ARTIFACT
    from ai_dev_loop.scheduler.domain.admission_contract import ADMISSION_STATUS_ARTIFACT

    assert ADMISSION_STATUS_ARTIFACT == "git/admission-status.txt"
    assert ADMISSION_STATUS_ARTIFACT != BASELINE_STATUS_ARTIFACT


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
