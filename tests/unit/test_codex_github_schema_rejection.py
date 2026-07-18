"""Unit tests for GitHub adjudication schema-rejection classification."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.errors import AdjudicationSchemaIncompatibleError, AiDevLoopError
from ai_dev_loop.process import StreamingProcessResult
from ai_dev_loop.runners.codex_github import run_codex_github_review
from ai_dev_loop.state import RunState, RunStatus, utc_now


def _state(tmp_path: Path) -> RunState:
    repo = tmp_path / "repo"
    repo.mkdir()
    (tmp_path / "plan").mkdir()
    (tmp_path / "prompts").mkdir()
    (tmp_path / "plan" / "plan.md").write_text("plan\n", encoding="utf-8")
    (tmp_path / "prompts" / "cursor-initial.txt").write_text("prompt\n", encoding="utf-8")
    now = utc_now()
    payload = {
        "schema_version": 1,
        "run_id": "demo-20260718T010234Z-317683",
        "project": {"name": "demo"},
        "status": RunStatus.EVALUATING_BOT_FEEDBACK.value,
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
        "repository": {
            "root": str(repo),
            "git_common_dir": str(repo / ".git"),
            "git_dir": str(repo / ".git"),
            "branch": "feature",
            "initial_head": "a" * 40,
            "baseline_status_path": "git/baseline-status.txt",
        },
        "plan": {
            "repository_path": "docs/plans/p.md",
            "snapshot_path": "plan/plan.md",
            "sha256": "a" * 64,
        },
        "prompt": {
            "source_repository_path": "docs/plans/prompt.txt",
            "snapshot_path": "prompts/cursor-initial.txt",
            "sha256": "b" * 64,
        },
        "codex": {
            "command": "codex",
            "session_id": "019abc00-0000-0000-0000-0000000000bb",
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
    return RunState.model_validate(payload)


def test_invalid_json_schema_rejection_is_typed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    run_dir = tmp_path
    events_rel = "github/cycles/01/codex.events.jsonl"
    (run_dir / "github/cycles/01").mkdir(parents=True)
    # Real Codex exec --json transport: API envelope JSON-serialized in message.
    api_envelope = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_json_schema",
            "message": (
                "Invalid schema for response_format 'codex_output_schema': "
                "In context=('properties', 'eligible_thread_ids'), "
                "'uniqueItems' is not supported."
            ),
            "param": "text.format.schema",
        },
        "status": 400,
    }
    (run_dir / events_rel).write_text(
        json.dumps({"type": "error", "message": json.dumps(api_envelope)}) + "\n",
        encoding="utf-8",
    )

    def _fake_streaming(*_a, **kwargs):
        # Preserve the pre-seeded rejection events written by the test.
        stdout_path = kwargs.get("stdout_path")
        if stdout_path is not None and not Path(stdout_path).is_file():
            Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
            Path(stdout_path).write_text("", encoding="utf-8")
        return StreamingProcessResult(
            args=["codex"],
            returncode=1,
            stdout="",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.1,
        )

    with (
        patch(
            "ai_dev_loop.runners.codex_github.run_process_streaming",
            side_effect=_fake_streaming,
        ),
        pytest.raises(AdjudicationSchemaIncompatibleError, match="invalid_json_schema") as exc,
    ):
        run_codex_github_review(
            state,
            run_dir,
            cycle_number=1,
            external_review_skill="review-external",
            eligible_thread_payload=[
                {
                    "thread_id": "THREAD1",
                    "author_login": "bot",
                    "path": "a.py",
                    "commit_sha": "a" * 40,
                    "body": "comment body must not leak",
                }
            ],
            bound_head_sha="a" * 40,
            pr_number=45,
        )
    assert "comment body" not in str(exc.value)
    assert "uniqueItems is unsupported" not in str(exc.value)


def test_unrelated_codex_failure_remains_generic(tmp_path: Path) -> None:
    state = _state(tmp_path)
    run_dir = tmp_path
    (run_dir / "github/cycles/01").mkdir(parents=True)
    (run_dir / "github/cycles/01/codex.events.jsonl").write_text(
        json.dumps(
            {
                "type": "error",
                "message": json.dumps(
                    {
                        "type": "error",
                        "error": {"type": "server_error", "code": "server_error"},
                        "status": 500,
                    }
                ),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_streaming(*_a, **_kwargs):
        return StreamingProcessResult(
            args=["codex"],
            returncode=2,
            stdout="",
            stderr="boom",
            timed_out=False,
            elapsed_seconds=0.1,
        )

    with (
        patch(
            "ai_dev_loop.runners.codex_github.run_process_streaming",
            side_effect=_fake_streaming,
        ),
        pytest.raises(AiDevLoopError, match="exit code 2") as exc,
    ):
        run_codex_github_review(
            state,
            run_dir,
            cycle_number=1,
            external_review_skill="review-external",
            eligible_thread_payload=[
                {
                    "thread_id": "THREAD1",
                    "author_login": "bot",
                    "path": "a.py",
                    "commit_sha": "a" * 40,
                    "body": "x",
                }
            ],
            bound_head_sha="a" * 40,
            pr_number=45,
        )
    assert not isinstance(exc.value, AdjudicationSchemaIncompatibleError)
    assert "boom" not in str(exc.value)
