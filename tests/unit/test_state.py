"""Unit tests for state helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError
from pydantic_core import PydanticUndefined

from ai_dev_loop.state import (
    CodexState,
    RunState,
    RunStatus,
    atomic_write_json,
    generate_run_id,
    load_run_state,
    sha256_text,
    transition_status,
)


def test_run_id_format() -> None:
    run_id = generate_run_id(
        "fixture-project",
        now=datetime(2026, 7, 4, 13, 45, 12, tzinfo=UTC),
    )
    assert re.fullmatch(r"fixture-project-20260704T134512Z-[0-9a-f]{6}", run_id)


def test_sha256_text() -> None:
    assert (
        sha256_text("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    )


def test_atomic_write_json(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    atomic_write_json(target, {"schema_version": 1, "status": "prepared"})
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["status"] == "prepared"


def test_historical_run_state_without_review_reasoning_effort_loads(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "prepared",
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
            "review_model": "o4-mini",
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
            "chat_id": None,
        },
        "workflow": {
            "max_review_iterations": 3,
            "current_review_iteration": 0,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "iterations": [],
        "result": None,
        "last_error": None,
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    state = load_run_state(path)
    assert isinstance(state, RunState)
    assert state.codex.review_model == "o4-mini"
    assert state.codex.review_reasoning_effort is None


def test_codex_state_rejects_invalid_review_reasoning_effort() -> None:
    with pytest.raises(PydanticValidationError, match="review_reasoning_effort"):
        CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model=None,
            review_reasoning_effort="turbo",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        )


def test_codex_state_rejects_empty_review_model() -> None:
    with pytest.raises(PydanticValidationError, match="review_model"):
        CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_model="   ",
            review_reasoning_effort=None,
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        )


def test_codex_state_review_model_required_nullable_aligns_with_schema() -> None:
    schema_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "ai_dev_loop"
        / "schemas"
        / "run-state-v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    codex_schema = schema["properties"]["codex"]
    assert "review_model" in codex_schema["required"]
    assert "review_reasoning_effort" not in codex_schema["required"]

    field = CodexState.model_fields["review_model"]
    assert field.is_required()
    assert field.default is PydanticUndefined

    with pytest.raises(PydanticValidationError, match="review_model"):
        CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        )

    state = CodexState(
        command="codex",
        session_id="019abc00-0000-0000-0000-000000000000",
        review_model=None,
        review_skill="review-staged-cursor-execution",
        sandbox="workspace-write",
    )
    assert state.review_model is None
    assert "review_model" in state.model_dump()
    assert state.model_dump()["review_model"] is None

    omitted_payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "prepared",
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
            "chat_id": None,
        },
        "workflow": {
            "max_review_iterations": 3,
            "current_review_iteration": 0,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "iterations": [],
        "result": None,
        "last_error": None,
    }
    with pytest.raises(PydanticValidationError, match="review_model"):
        RunState.model_validate(omitted_payload)


def test_load_run_state_rejects_invalid_review_reasoning_effort(tmp_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "prepared",
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
            "review_reasoning_effort": "turbo",
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
            "chat_id": None,
        },
        "workflow": {
            "max_review_iterations": 3,
            "current_review_iteration": 0,
            "stage_mode": "all",
            "cursor_timeout_minutes": 90,
            "codex_timeout_minutes": 90,
        },
        "iterations": [],
        "result": None,
        "last_error": None,
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(PydanticValidationError, match="review_reasoning_effort"):
        load_run_state(path)


def test_status_transitions() -> None:
    transition_status(RunStatus.PREPARED, RunStatus.VALIDATING)
    transition_status(RunStatus.STAGING, RunStatus.REVIEWING)
    transition_status(RunStatus.REVIEWING, RunStatus.COMPLETED)
    transition_status(RunStatus.REVIEWING, RunStatus.WAITING_FOR_CURSOR_FIX)
    transition_status(RunStatus.REVIEWING, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.RUNNING_CURSOR)
    transition_status(RunStatus.WAITING_FOR_CURSOR_FIX, RunStatus.MAX_ITERATIONS_REACHED)
    transition_status(RunStatus.INTERRUPTED, RunStatus.VALIDATING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.STAGING)
    transition_status(RunStatus.INTERRUPTED, RunStatus.REVIEWING)
    transition_status(RunStatus.VALIDATING, RunStatus.STAGING)
    transition_status(RunStatus.VALIDATING, RunStatus.REVIEWING)
    for terminal in (
        RunStatus.COMPLETED,
        RunStatus.COMPLETED_WITH_RESIDUAL_RISK,
        RunStatus.MAX_ITERATIONS_REACHED,
        RunStatus.FAILED,
        RunStatus.ABORTED,
    ):
        try:
            transition_status(terminal, RunStatus.VALIDATING)
        except ValueError:
            continue
        raise AssertionError(f"expected terminal status {terminal.value} to reject transitions")
    try:
        transition_status(RunStatus.PREPARED, RunStatus.COMPLETED)
    except ValueError:
        return
    raise AssertionError("expected invalid transition to fail")
