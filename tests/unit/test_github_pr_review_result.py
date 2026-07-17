"""Unit tests for GitHub PR review result schema/model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.paths import schema_path


def _base(**overrides: object) -> dict:
    payload = {
        "eligible_thread_ids": ["T1", "T2"],
        "thread_decisions": [
            {
                "thread_id": "T1",
                "decision": "actionable",
                "inline_reply": None,
                "summary": "fix path",
            },
            {
                "thread_id": "T2",
                "decision": "actionable",
                "inline_reply": None,
                "summary": "fix naming",
            },
        ],
        "all_actionable": True,
        "review_markdown": "# report",
        "cursor_fix_prompt": "Please fix both threads.",
        "tests_status": "not_applicable",
        "summary": "all actionable",
        "residual_risk_comment": None,
        "highest_severity": "P2",
    }
    payload.update(overrides)
    return payload


def test_all_actionable_requires_fix_prompt() -> None:
    result = GithubPrReviewResult.model_validate(_base())
    assert result.all_actionable is True
    assert result.cursor_fix_prompt is not None


def test_uncertain_requires_rojobad_reply() -> None:
    payload = _base(
        all_actionable=False,
        cursor_fix_prompt=None,
        thread_decisions=[
            {
                "thread_id": "T1",
                "decision": "actionable",
                "inline_reply": None,
                "summary": "ok",
            },
            {
                "thread_id": "T2",
                "decision": "uncertain",
                "inline_reply": "@rojobad Need clarification on expected API.",
                "summary": "unclear",
            },
        ],
    )
    result = GithubPrReviewResult.model_validate(payload)
    assert result.all_actionable is False


def test_reply_must_begin_with_rojobad() -> None:
    payload = _base(
        all_actionable=False,
        cursor_fix_prompt=None,
        thread_decisions=[
            {
                "thread_id": "T1",
                "decision": "not_applicable",
                "inline_reply": "Does not apply",
                "summary": "na",
            },
            {
                "thread_id": "T2",
                "decision": "actionable",
                "inline_reply": None,
                "summary": "ok",
            },
        ],
    )
    with pytest.raises(ValidationError, match="@rojobad"):
        GithubPrReviewResult.model_validate(payload)


def test_incomplete_thread_coverage_fails() -> None:
    payload = _base(
        eligible_thread_ids=["T1", "T2", "T3"],
    )
    with pytest.raises(ValidationError, match="exactly"):
        GithubPrReviewResult.model_validate(payload)


def test_schema_file_exists_and_parses() -> None:
    path = schema_path("github-pr-review-result-v1.json")
    assert path.is_file()
    json.loads(path.read_text(encoding="utf-8"))


def test_historical_run_state_without_github_section_loads(tmp_path: Path) -> None:
    from ai_dev_loop.state import RunState

    payload = {
        "schema_version": 1,
        "run_id": "fixture-project-20260704T134512Z-abc123",
        "project": {"name": "fixture-project"},
        "status": "completed",
        "created_at": "2026-07-04T13:45:12+00:00",
        "updated_at": "2026-07-04T13:45:12+00:00",
        "repository": {
            "root": "/tmp/repo",
            "git_common_dir": "/tmp/repo/.git",
            "git_dir": "/tmp/repo/.git",
            "branch": "feature",
            "initial_head": "a" * 40,
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
        "result": "done",
        "last_error": None,
    }
    state = RunState.model_validate(payload)
    assert state.github_pr_review is None
