"""Unit tests for logs command rendering."""

from __future__ import annotations

import json
from pathlib import Path

from ai_dev_loop.commands.logs import _render_codex_logs, _summarize_review_result
from ai_dev_loop.state import (
    CodexState,
    CursorState,
    PlanState,
    ProjectRef,
    PromptState,
    RepositoryState,
    RunState,
    RunStatus,
    WorkflowState,
    utc_now,
)


def _sample_state(*, iterations: list[dict]) -> RunState:
    now = utc_now()
    return RunState(
        run_id="fixture-project-20260704T134512Z-abc123",
        project=ProjectRef(name="fixture-project"),
        status=RunStatus.WAITING_FOR_CURSOR_FIX,
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
        codex=CodexState(
            command="codex",
            session_id="019abc00-0000-0000-0000-000000000000",
            session_model=None,
            review_model="o4-mini",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
        ),
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
        iterations=iterations,
    )


def test_summarize_review_result_redacts_sensitive_fields() -> None:
    summary = _summarize_review_result(
        {
            "has_actionable_findings": True,
            "findings_count": 1,
            "highest_severity": "P1",
            "review_markdown": "# Secret review\n\nproprietary context",
            "cursor_fix_prompt": "Fix the secret issue immediately.",
            "tests_status": "skipped_findings_present",
            "summary": "One finding.",
        }
    )
    assert summary["cursor_fix_prompt"] == "<redacted>"
    assert summary["review_markdown"] == "<redacted>"
    assert summary["summary"] == "One finding."


def test_render_codex_logs_redacts_findings_artifacts(tmp_path: Path) -> None:
    run_path = tmp_path / "run"
    reviews = run_path / "codex" / "reviews"
    events = run_path / "codex" / "events"
    reviews.mkdir(parents=True)
    events.mkdir(parents=True)

    review_payload = {
        "has_actionable_findings": True,
        "findings_count": 1,
        "highest_severity": "P1",
        "review_markdown": "# Secret review\n\nproprietary code context",
        "cursor_fix_prompt": "Fix the secret issue immediately.",
        "tests_status": "skipped_findings_present",
        "summary": "One finding.",
    }
    (reviews / "01.json").write_text(json.dumps(review_payload), encoding="utf-8")
    (reviews / "01.md").write_text("# Secret review\n", encoding="utf-8")
    (reviews / "01.metadata.json").write_text(
        json.dumps(
            {
                "session_id": "019abc00-0000-0000-0000-000000000000",
                "exit_code": 0,
                "args": ["codex", "exec", "<stdin-prompt>"],
            }
        ),
        encoding="utf-8",
    )
    (events / "01.jsonl").write_text('{"type":"message","content":"secret"}\n', encoding="utf-8")
    (events / "01.stderr.txt").write_text("secret stderr\n", encoding="utf-8")

    state = _sample_state(
        iterations=[
            {
                "number": 1,
                "kind": "initial_implementation",
                "codex": {
                    "events_path": "codex/events/01.jsonl",
                    "stderr_path": "codex/events/01.stderr.txt",
                    "result_path": "codex/reviews/01.json",
                    "report_path": "codex/reviews/01.md",
                    "metadata_path": "codex/reviews/01.metadata.json",
                    "fix_prompt_path": "prompts/fixes/01.txt",
                    "exit_code": 0,
                },
                "review": {
                    "has_actionable_findings": True,
                    "findings_count": 1,
                    "highest_severity": "P1",
                    "tests_status": "skipped_findings_present",
                    "summary": "One finding.",
                },
            }
        ]
    )

    rendered = _render_codex_logs(run_path, state)
    assert "Fix the secret issue immediately." not in rendered
    assert "proprietary code context" not in rendered
    assert "019abc00-0000-0000-0000-000000000000" not in rendered
    assert "019abc00" in rendered
    assert "content redacted" in rendered
    assert '"cursor_fix_prompt": "<redacted>"' in rendered
