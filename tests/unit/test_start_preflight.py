"""Unit tests for start preflight validators."""

from __future__ import annotations

import pytest

from ai_dev_loop.commands.start_preflight import (
    validate_plan_contract,
    validate_prompt_contract,
    validate_start_status,
    validate_worktree_baseline,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.state import RunStatus, load_run_state


def test_validate_start_status_rejects_non_prepared(prepared_run) -> None:
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.STAGING
    with pytest.raises(ValidationError, match="prepared status"):
        validate_start_status(state)


def test_validate_plan_contract_rejects_changed_snapshot(prepared_run) -> None:
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    snapshot = run_path / state.plan.snapshot_path
    snapshot.write_text("changed plan\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="plan snapshot hash"):
        validate_plan_contract(state, run_path)


def test_validate_prompt_contract_rejects_changed_source(prepared_run) -> None:
    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    state = load_run_state(run_path / "state.json")
    source = repo / state.prompt.source_repository_path
    source.write_text("changed prompt\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="prompt source"):
        validate_prompt_contract(state, run_path)


def test_validate_worktree_baseline_rejects_drift(prepared_run) -> None:
    run_path = prepared_run["run_path"]
    repo = prepared_run["repo"]
    state = load_run_state(run_path / "state.json")
    (repo / "unexpected.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="worktree status changed"):
        validate_worktree_baseline(state, run_path)
