"""Unit tests for publication helpers and GitHub PR-review state."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_loop.runners.publish import (
    PublicationText,
    PublishResult,
    commit_staged_patch,
    publish_accepted_staged_patch,
    validate_clean_except_staged,
)
from ai_dev_loop.state import GithubPrReviewState, RunStatus, transition_status


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_commit_staged_patch_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    validate_clean_except_staged(repo)
    sha = commit_staged_patch(repo, subject="Accept staged change", body="Body line")
    assert len(sha) == 40
    status = _git(repo, "status", "--porcelain")
    assert status == ""


def test_validate_clean_except_staged_rejects_untracked(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    (repo / "noise.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(Exception, match="untracked"):
        validate_clean_except_staged(repo)


def test_github_pr_review_state_requires_full_sha() -> None:
    with pytest.raises(ValidationError):
        GithubPrReviewState(
            source_run_id="src",
            lifecycle="awaiting_bot_review",
            cycle_number=1,
            max_external_cycles=8,
            pr_number=1,
            head_branch="feature",
            bound_head_sha="abc",
        )


def test_github_status_transitions() -> None:
    transition_status(RunStatus.AWAITING_BOT_REVIEW, RunStatus.EVALUATING_BOT_FEEDBACK)
    transition_status(RunStatus.EVALUATING_BOT_FEEDBACK, RunStatus.WAITING_FOR_USER_ATTENTION)
    transition_status(RunStatus.EVALUATING_BOT_FEEDBACK, RunStatus.RUNNING_CURSOR)
    transition_status(RunStatus.REVIEWING, RunStatus.PUBLISHING_EXTERNAL_FIX)
    transition_status(RunStatus.PUBLISHING_EXTERNAL_FIX, RunStatus.AWAITING_BOT_REVIEW)
    with pytest.raises(ValueError):
        transition_status(RunStatus.COMPLETED, RunStatus.AWAITING_BOT_REVIEW)


def test_publication_text_fields() -> None:
    text = PublicationText(
        commit_subject="Subject",
        commit_body="Body",
        pr_title="Title",
        pr_body="PR body",
    )
    assert text.commit_subject == "Subject"


def test_publish_resume_skips_duplicate_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "init")
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    sha = commit_staged_patch(repo, subject="Accept staged change", body="")
    assert _git(repo, "status", "--porcelain") == ""

    pushed: list[tuple[str, str | None]] = []

    def fake_push(*_args, **kwargs):
        pushed.append((kwargs["local_branch"], kwargs["expected_remote_sha"]))

    monkeypatch.setattr(
        "ai_dev_loop.runners.publish.resolve_upstream",
        lambda *_a, **_k: ("origin", "feature"),
    )
    monkeypatch.setattr("ai_dev_loop.runners.publish.verify_ssh_push_ready", lambda *_a, **_k: None)
    monkeypatch.setattr("ai_dev_loop.runners.publish.push_branch_non_force", fake_push)

    result = publish_accepted_staged_patch(
        repo,
        branch="feature",
        text=PublicationText("x", "", "t", ""),
        resume_from_commit=sha,
        expected_remote_sha_before_push=None,
        staged_patch_sha256="d" * 64,
        after_commit=lambda *_a: (_ for _ in ()).throw(AssertionError("must not commit again")),
    )
    assert isinstance(result, PublishResult)
    assert result.commit_sha == sha
    assert result.resumed_existing_commit is True
    assert pushed == [("feature", None)]


def test_codex_github_args_resume_exact_session() -> None:
    from ai_dev_loop.runners.codex import build_codex_review_args
    from ai_dev_loop.state import CodexState

    codex = CodexState(
        command="codex",
        session_id="019abc00-0000-0000-0000-000000000099",
        review_model="gpt-5.6-sol",
        review_reasoning_effort="high",
        review_model_source="session",
        review_reasoning_source="session",
        review_skill="review-github-pr-feedback",
        sandbox="workspace-write",
    )
    args = build_codex_review_args(
        codex,
        repo_root="/tmp/repo",
        session_id=codex.session_id,
        schema_file=Path("/tmp/schema.json"),
        result_file=Path("/tmp/out.json"),
    )
    assert "--last" not in args
    assert "resume" in args
    assert args[args.index("resume") + 1 :].count(codex.session_id) == 1 or codex.session_id in args
    assert args[-2] == codex.session_id
    assert args[-1] == "-"
