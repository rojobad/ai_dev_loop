"""Unit tests for recovery planner and RecoveryState schema alignment."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from ai_dev_loop.recovery_planner import (
    SessionRuntimeAction,
    analyze_recovery,
    derive_recovery_reason_code,
    resolve_recovery_runtime,
)
from ai_dev_loop.review_runtime import is_legacy_phase9_codex_state
from ai_dev_loop.state import (
    RECOVERY_CHECKPOINTS,
    RECOVERY_REASON_CODES,
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RecoveryState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    load_run_state,
    utc_now,
)


def _sample_failed_state(**codex_overrides: object) -> RunState:
    now = utc_now()
    codex_kwargs: dict[str, object] = {
        "command": "codex",
        "session_id": "019abc00-0000-0000-0000-000000000000",
        "session_model": "gpt-5.6-sol",
        "session_reasoning_effort": "high",
        "review_model": "gpt-5.6-sol",
        "review_reasoning_effort": "high",
        "review_model_source": "session",
        "review_reasoning_source": "session",
        "review_skill": "review-staged-cursor-execution",
        "sandbox": "workspace-write",
    }
    codex_kwargs.update(codex_overrides)
    return RunState(
        run_id="fixture-project-20260711T010911Z-abcdef",
        project=ProjectRef(name="fixture-project"),
        status=RunStatus.FAILED,
        created_at=now,
        updated_at=now,
        repository=RepositoryState(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
            initial_head="abc123",
            baseline_status_path="git/baseline-status.txt",
        ),
        plan=PlanState(
            repository_path="docs/plans/sample-plan.md",
            snapshot_path="plan/plan.md",
            sha256="a" * 64,
        ),
        prompt=PromptState(
            source_repository_path="docs/plans/prompt_sample-plan.txt",
            snapshot_path="prompts/cursor-initial.txt",
            sha256="b" * 64,
        ),
        codex=CodexState(**codex_kwargs),  # type: ignore[arg-type]
        cursor=CursorState(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
            chat_id="019abc00-1111-2222-3333-444444444444",
        ),
        workflow=WorkflowState(
            max_review_iterations=3,
            current_review_iteration=1,
            stage_mode="all",
            cursor_timeout_minutes=90,
            codex_timeout_minutes=90,
        ),
        iterations=[
            {
                "number": 1,
                "kind": "initial_implementation",
                "started_at": now.isoformat(),
                "git": {"staged_diff_path": "git/diffs/01.patch"},
            }
        ],
        result=None,
        last_error="Codex review failed with exit code 2",
    )


def test_recovery_state_aligns_with_schema() -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ai_dev_loop"
        / "schemas"
        / "run-state-v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "recovery" in schema["properties"]
    assert "recovery" not in schema["required"]
    recovery_schema = schema["properties"]["recovery"]
    assert set(recovery_schema["required"]) == {
        "source_run_id",
        "source_status",
        "source_iteration",
        "recovered_checkpoint",
        "source_staged_patch_sha256",
        "created_at",
        "runtime_migration",
        "reason_code",
    }
    assert RecoveryState.model_fields.keys() == set(recovery_schema["properties"].keys())
    assert set(recovery_schema["properties"]["recovered_checkpoint"]["enum"]) == set(
        RECOVERY_CHECKPOINTS
    )
    assert "cursor" in RECOVERY_CHECKPOINTS
    assert set(recovery_schema["properties"]["reason_code"]["enum"]) == set(RECOVERY_REASON_CODES)
    assert "cursor_usage_limit" in RECOVERY_REASON_CODES


def test_historical_run_state_without_recovery_loads(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "failed",
        "created_at": "2026-07-04T13:45:12+00:00",
        "updated_at": "2026-07-04T13:45:12+00:00",
        "repository": {
            "root": "/tmp/repo",
            "git_common_dir": "/tmp/repo/.git",
            "git_dir": "/tmp/repo/.git",
            "branch": "main",
            "initial_head": "abc123",
            "baseline_status_path": "git/baseline-status.txt",
        },
        "plan": {
            "repository_path": "docs/plans/sample-plan.md",
            "snapshot_path": "plan/plan.md",
            "sha256": "a" * 64,
        },
        "prompt": {
            "source_repository_path": "docs/plans/prompt_sample-plan.txt",
            "snapshot_path": "prompts/cursor-initial.txt",
            "sha256": "b" * 64,
        },
        "codex": {
            "command": "codex",
            "session_id": "019abc00-0000-0000-0000-000000000000",
            "session_model": None,
            "review_model": None,
            "review_skill": "review-staged-cursor-execution",
            "sandbox": "workspace-write",
        },
        "cursor": {
            "command": "agent",
            "model": "composer-2.5-fast",
            "output_format": "stream-json",
            "force": True,
            "trust_workspace": True,
            "sandbox": "disabled",
            "chat_id": "019abc00-1111-2222-3333-444444444444",
        },
        "workflow": {
            "max_review_iterations": 3,
            "current_review_iteration": 1,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "iterations": [],
        "result": None,
        "last_error": "failed",
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = load_run_state(path)
    assert state.recovery is None


def test_recovery_state_accepts_cursor_checkpoint_with_required_fields() -> None:
    now = datetime.now(tz=UTC)
    recovery = RecoveryState(
        source_run_id="source-run",
        source_status="failed",
        source_iteration=1,
        recovered_checkpoint="cursor",
        source_staged_patch_sha256=None,
        created_at=now,
        runtime_migration="none",
        reason_code="cursor_usage_limit",
        source_cursor_model="composer-2.5-fast",
        cursor_model_fallback="auto",
        source_prompt_path="prompts/cursor-initial.txt",
        source_prompt_sha256="b" * 64,
        usage_limit_fingerprint_sha256="c" * 64,
        usage_limit_fingerprint_path="git/cursor-output/01.usage-limit-failure.json",
        continuation_envelope_path="prompts/cursor-recovery/01.usage-limit-continuation.txt",
        continuation_envelope_sha256="d" * 64,
    )
    assert recovery.recovered_checkpoint == "cursor"
    assert recovery.source_staged_patch_sha256 is None


def test_recovery_state_cursor_checkpoint_rejects_missing_cursor_model_fallback() -> None:
    now = datetime.now(tz=UTC)
    with pytest.raises(PydanticValidationError, match="cursor_model_fallback"):
        RecoveryState(
            source_run_id="source-run",
            source_status="failed",
            source_iteration=1,
            recovered_checkpoint="cursor",
            source_staged_patch_sha256=None,
            created_at=now,
            runtime_migration="none",
            reason_code="cursor_usage_limit",
            source_cursor_model="composer-2.5-fast",
            cursor_model_fallback=None,
            source_prompt_path="prompts/cursor-initial.txt",
            source_prompt_sha256="b" * 64,
            usage_limit_fingerprint_sha256="c" * 64,
            usage_limit_fingerprint_path="git/cursor-output/01.usage-limit-failure.json",
            continuation_envelope_path="prompts/cursor-recovery/01.usage-limit-continuation.txt",
            continuation_envelope_sha256="d" * 64,
        )


def test_recovery_state_staging_checkpoint_rejects_cursor_only_fields() -> None:
    now = datetime.now(tz=UTC)
    with pytest.raises(PydanticValidationError, match="cursor recovery fields"):
        RecoveryState(
            source_run_id="source-run",
            source_status="failed",
            source_iteration=2,
            recovered_checkpoint="staging",
            source_staged_patch_sha256="a" * 64,
            created_at=now,
            runtime_migration="none",
            reason_code="correction_staging_failed",
            cursor_output_fingerprint_sha256="e" * 64,
            previous_staged_patch_sha256="a" * 64,
            source_cursor_model="composer-2.5-fast",
        )


def test_recovery_state_rejects_empty_source_run_id() -> None:
    with pytest.raises(PydanticValidationError, match="source_run_id"):
        RecoveryState(
            source_run_id="",
            source_status="failed",
            source_iteration=1,
            recovered_checkpoint="reviewing",
            source_staged_patch_sha256="a" * 64,
            created_at=datetime.now(tz=UTC),
            runtime_migration="none",
            reason_code="codex_review_failed",
        )


def test_recovery_state_rejects_invalid_reason_code() -> None:
    with pytest.raises(PydanticValidationError, match="reason_code"):
        RecoveryState(
            source_run_id="source",
            source_status="failed",
            source_iteration=1,
            recovered_checkpoint="reviewing",
            source_staged_patch_sha256="a" * 64,
            created_at=datetime.now(tz=UTC),
            runtime_migration="none",
            reason_code="not_a_real_code",
        )


def test_recovery_state_rejects_zero_source_iteration() -> None:
    with pytest.raises(PydanticValidationError, match="source_iteration"):
        RecoveryState(
            source_run_id="source-run",
            source_status="failed",
            source_iteration=0,
            recovered_checkpoint="reviewing",
            source_staged_patch_sha256="a" * 64,
            created_at=datetime.now(tz=UTC),
            runtime_migration="none",
            reason_code="codex_review_failed",
        )


def test_load_run_state_rejects_invalid_recovery_lineage(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "interrupted",
        "created_at": "2026-07-04T13:45:12+00:00",
        "updated_at": "2026-07-04T13:45:12+00:00",
        "repository": {
            "root": "/tmp/repo",
            "git_common_dir": "/tmp/repo/.git",
            "git_dir": "/tmp/repo/.git",
            "branch": "main",
            "initial_head": "abc123",
            "baseline_status_path": "git/baseline-status.txt",
        },
        "plan": {
            "repository_path": "docs/plans/sample-plan.md",
            "snapshot_path": "plan/plan.md",
            "sha256": "a" * 64,
        },
        "prompt": {
            "source_repository_path": "docs/plans/prompt_sample-plan.txt",
            "snapshot_path": "prompts/cursor-initial.txt",
            "sha256": "b" * 64,
        },
        "codex": {
            "command": "codex",
            "session_id": "019abc00-0000-0000-0000-000000000000",
            "session_model": "gpt-5.6-sol",
            "session_reasoning_effort": "high",
            "review_model": "gpt-5.6-sol",
            "review_reasoning_effort": "high",
            "review_model_source": "session",
            "review_reasoning_source": "session",
            "review_skill": "review-staged-cursor-execution",
            "sandbox": "workspace-write",
        },
        "cursor": {
            "command": "agent",
            "model": "composer-2.5-fast",
            "output_format": "stream-json",
            "force": True,
            "trust_workspace": True,
            "sandbox": "disabled",
            "chat_id": "019abc00-1111-2222-3333-444444444444",
        },
        "workflow": {
            "max_review_iterations": 3,
            "current_review_iteration": 1,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "iterations": [],
        "result": None,
        "last_error": None,
        "recovery": {
            "source_run_id": "",
            "source_status": "failed",
            "source_iteration": 0,
            "recovered_checkpoint": "reviewing",
            "source_staged_patch_sha256": "a" * 64,
            "created_at": "2026-07-04T13:45:12+00:00",
            "runtime_migration": "none",
            "reason_code": "codex_review_failed",
        },
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PydanticValidationError):
        load_run_state(path)


def test_resolve_phase9_model_only_override(hermetic_codex_env: Path) -> None:
    from tests.conftest import write_session_rollout

    write_session_rollout(
        hermetic_codex_env / "sessions",
        session_id="019abc00-0000-0000-0000-000000000000",
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )
    state = _sample_failed_state(
        session_model=None,
        session_reasoning_effort=None,
        review_model="o4-mini",
        review_reasoning_effort=None,
        review_model_source=None,
        review_reasoning_source=None,
    )
    resolved = resolve_recovery_runtime(state)
    assert resolved.runtime_migration == "phase9_session_capture"
    assert resolved.review_model == "o4-mini"
    assert resolved.review_model_source == "explicit"
    assert resolved.review_reasoning_effort == "high"
    assert resolved.review_reasoning_source == "session"
    assert resolved.session_model == "gpt-5.6-sol"


def test_resolve_phase9_reasoning_only_override(hermetic_codex_env: Path) -> None:
    from tests.conftest import write_session_rollout

    write_session_rollout(
        hermetic_codex_env / "sessions",
        session_id="019abc00-0000-0000-0000-000000000000",
        model="gpt-5.6-sol",
        reasoning_effort="medium",
    )
    state = _sample_failed_state(
        session_model=None,
        session_reasoning_effort=None,
        review_model=None,
        review_reasoning_effort="xhigh",
        review_model_source=None,
        review_reasoning_source=None,
    )
    resolved = resolve_recovery_runtime(state)
    assert resolved.runtime_migration == "phase9_session_capture"
    assert resolved.review_model == "gpt-5.6-sol"
    assert resolved.review_model_source == "session"
    assert resolved.review_reasoning_effort == "xhigh"
    assert resolved.review_reasoning_source == "explicit"


def test_analyze_recovery_rejects_non_failed_status(tmp_path: Path) -> None:
    state = _sample_failed_state()
    state.status = RunStatus.COMPLETED
    analysis = analyze_recovery(state, tmp_path)
    assert analysis.eligible is False
    assert analysis.blockers == ["source_status_not_failed"]


def test_derive_reason_codes(tmp_path: Path) -> None:
    assert (
        derive_recovery_reason_code(tmp_path, checkpoint="process_review", iteration_number=1)
        == "codex_review_processing_failed"
    )
    assert (
        derive_recovery_reason_code(tmp_path, checkpoint="reviewing", iteration_number=1)
        == "codex_review_failed"
    )
    reviews = tmp_path / "codex" / "reviews"
    reviews.mkdir(parents=True)
    (reviews / "01.json").write_text("{not-json", encoding="utf-8")
    assert (
        derive_recovery_reason_code(tmp_path, checkpoint="reviewing", iteration_number=1)
        == "codex_review_result_invalid"
    )


def test_resolve_phase10_runtime_preserved() -> None:
    state = _sample_failed_state()
    resolved = resolve_recovery_runtime(state)
    assert resolved.session_runtime_action == SessionRuntimeAction.PRESERVE_PHASE10
    assert resolved.runtime_migration == "none"
    assert resolved.review_model == "gpt-5.6-sol"
    assert resolved.review_reasoning_effort == "high"


def test_resolve_phase9_runtime_migrates(hermetic_codex_env: Path) -> None:
    from tests.conftest import write_session_rollout

    write_session_rollout(
        hermetic_codex_env / "sessions",
        session_id="019abc00-0000-0000-0000-000000000000",
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )
    state = _sample_failed_state(
        session_model=None,
        session_reasoning_effort=None,
        review_model=None,
        review_reasoning_effort=None,
        review_model_source=None,
        review_reasoning_source=None,
    )
    assert is_legacy_phase9_codex_state(
        review_model=state.codex.review_model,
        review_reasoning_effort=state.codex.review_reasoning_effort,
        review_model_source=state.codex.review_model_source,
        review_reasoning_source=state.codex.review_reasoning_source,
    )
    resolved = resolve_recovery_runtime(state)
    assert resolved.session_runtime_action == SessionRuntimeAction.MIGRATE_PHASE9
    assert resolved.runtime_migration == "phase9_session_capture"
    assert resolved.session_model == "gpt-5.6-sol"
    assert resolved.review_model == "gpt-5.6-sol"
    assert resolved.review_model_source == "session"
    assert resolved.review_reasoning_source == "session"
