"""Phase 16.2 integration: reusable local boundary without GithubPrReviewState."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from ai_dev_loop.local_review_loop import (
    LocalReviewFixRequest,
    LocalReviewOperation,
    LocalReviewOutcome,
    run_local_review_fix,
)
from ai_dev_loop.state import RunStatus, load_run_state


def test_local_boundary_full_loop_without_github_pr_state(
    prepared_run,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)

    run_id = prepared_run["run_id"]
    run_path = Path(prepared_run["run_path"])
    repo = Path(prepared_run["repo"])

    before = load_run_state(run_path / "state.json")
    assert before.github_pr_review is None
    prompt_sha = before.prompt.sha256
    plan_sha = before.plan.sha256
    session_id = before.codex.session_id
    review_model = before.codex.review_model
    commits_before = subprocess.check_output(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        text=True,
    ).strip()

    result = run_local_review_fix(
        LocalReviewFixRequest(
            run_id=run_id,
            operation=LocalReviewOperation.START,
        )
    )

    assert result.status == "completed"
    assert result.outcome is LocalReviewOutcome.ACCEPTED
    assert result.needs_external_continuation is False
    assert result.iteration_count == 2
    assert result.chat_id

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.COMPLETED
    assert state.github_pr_review is None
    assert state.cursor.chat_id == result.chat_id
    assert state.codex.session_id == session_id
    assert state.codex.review_model == review_model
    assert state.prompt.sha256 == prompt_sha
    assert state.plan.sha256 == plan_sha
    assert state.prompt.snapshot_path == "prompts/cursor-initial.txt"
    assert (run_path / "prompts/fixes/01.txt").is_file()
    assert (run_path / "cursor/iterations/01").is_dir()
    assert (run_path / "cursor/iterations/02").is_dir()
    assert (run_path / "git/diffs/01.patch").is_file()
    assert (run_path / "git/diffs/02.patch").is_file()
    assert (run_path / "codex/reviews/01.json").is_file()
    assert (run_path / "codex/reviews/02.json").is_file()
    assert not (run_path / "github").exists()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    resume_ids = re.findall(r"'--resume', '([^']+)'", agent_log)
    assert len(resume_ids) >= 2
    assert len(set(resume_ids)) == 1
    assert resume_ids[0] == result.chat_id

    codex_log = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert codex_log.count(session_id) >= 2
    assert "--last" not in codex_log

    commits_after = subprocess.check_output(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        text=True,
    ).strip()
    assert commits_after == commits_before
    remotes = subprocess.check_output(
        ["git", "remote"],
        cwd=repo,
        text=True,
    ).strip()
    assert remotes == ""
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert staged.stdout.strip()


def test_public_start_adapter_preserves_status_and_outcome_fields(
    prepared_run,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.commands.start import start_run

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")

    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"
    assert result.outcome is LocalReviewOutcome.ACCEPTED
    assert result.needs_external_continuation is False
    state = load_run_state(Path(prepared_run["run_path"]) / "state.json")
    assert state.github_pr_review is None
