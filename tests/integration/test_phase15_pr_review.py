"""Integration coverage for Phase 15 GitHub PR-review cycle boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import create_pr_review_cycle, render_pr_review_status
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubWriteResult
from ai_dev_loop.runners.publish import PublicationText, PublishResult
from ai_dev_loop.state import (
    GithubPrReviewState,
    RunStatus,
    load_run_state,
    save_run_state,
)

runner = CliRunner()


def _write_adjudication_snapshot(run_directory, review, artifacts):
    result_path = run_directory / artifacts.result_path
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if not result_path.is_file():
        result_path.write_text(
            json.dumps(review.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
    report_path = run_directory / artifacts.report_path
    if not report_path.is_file():
        report_path.write_text(review.review_markdown, encoding="utf-8")
    snap_path = run_directory / artifacts.snapshot_path
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snap_path.write_text(
        json.dumps(
            {
                "threads": [
                    {"thread_id": tid, "body_sha256": "a" * 64}
                    for tid in review.eligible_thread_ids
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if artifacts.fix_prompt_path and review.cursor_fix_prompt:
        prompt_path = run_directory / artifacts.fix_prompt_path
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        if not prompt_path.is_file():
            prompt_path.write_text(review.cursor_fix_prompt, encoding="utf-8")


def _enable_github(repo: Path) -> None:
    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "github:" not in text:
        config.write_text(
            text
            + "\ngithub:\n  enabled: true\n  poll_interval_seconds: 1\n  poll_timeout_hours: 1\n",
            encoding="utf-8",
        )


def _stage_change(repo: Path) -> None:
    target = repo / "feature.txt"
    target.write_text("implemented\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "feature.txt"], cwd=repo, check=True, capture_output=True)


def test_github_doctor_cli(isolated_xdg: Path, fake_clis: dict[str, Path], tmp_path: Path) -> None:
    result = runner.invoke(app, ["github", "doctor", "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    names = {item["name"] for item in payload["checks"]}
    assert "schema:github-pr-review-result-v1.json" in names


def test_pr_review_create_and_status(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.run_discovery import find_run_directory

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.COMPLETED
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.result = "Local review accepted"
    save_run_state(run_path, state)
    _stage_change(repo)
    head_sha = "c" * 40

    pr = GithubPullRequest(
        number=42,
        url="https://example.test/pr/42",
        title="Add feature",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha=head_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )

    with (
        patch("ai_dev_loop.commands.pr_review.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review.validate_clean_except_staged", return_value="PATCH"),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            return_value=PublicationText("s", "b", "t", "pb"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            return_value=PublishResult(
                head_sha,
                "origin",
                "origin/branch",
                "d" * 64,
                None,
                False,
            ),
        ),
        patch("ai_dev_loop.commands.pr_review.create_or_update_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="999"),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch("ai_dev_loop.commands.pr_review.sha256_text", return_value="d" * 64),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        created = create_pr_review_cycle(state.run_id)

    successor_dir = find_run_directory(created.run_id)
    successor = load_run_state(successor_dir / "state.json")
    assert successor.status == RunStatus.AWAITING_BOT_REVIEW
    assert successor.cursor.chat_id == state.cursor.chat_id
    assert successor.codex.session_id == state.codex.session_id
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.pr_number == 42
    assert successor.github_pr_review.bound_head_sha == head_sha
    # Source remains terminal/immutable.
    source = load_run_state(run_path / "state.json")
    assert source.status == RunStatus.COMPLETED
    assert source.github_pr_review is None

    status_text = render_pr_review_status(created.run_id, output="json")
    payload = json.loads(status_text)
    assert payload["github_pr_review"]["pr_number"] == 42
    assert "comment body" not in status_text.lower()


def test_uncertain_adjudication_stops_without_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.runners.github import GithubReviewThread

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_comment_id="1",
        request_marker="marker",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)

    thread = GithubReviewThread(
        thread_id="THREAD1",
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="x.py",
        line=1,
        commit_sha="a" * 40,
        root_comment_id="C1",
        root_comment_body_sha256="b" * 64,
        created_at="2026-07-16T12:05:00+00:00",
        review_id=None,
    )
    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": ["THREAD1"],
            "thread_decisions": [
                {
                    "thread_id": "THREAD1",
                    "decision": "uncertain",
                    "inline_reply": "@rojobad Need more context on the intended behavior.",
                    "summary": "unclear",
                }
            ],
            "all_actionable": False,
            "review_markdown": "report",
            "cursor_fix_prompt": None,
            "tests_status": "not_applicable",
            "summary": "uncertain",
            "residual_risk_comment": None,
            "highest_severity": None,
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/01/codex.events.jsonl",
        stderr_path="github/cycles/01/codex.stderr.txt",
        result_path="github/cycles/01/result.json",
        report_path="github/cycles/01/report.md",
        metadata_path="github/cycles/01/codex.metadata.json",
        snapshot_path="github/cycles/01/threads.snapshot.json",
        fix_prompt_path=None,
    )

    cursor_called = {"value": False}

    def fail_if_resume(*_a, **_k):
        cursor_called["value"] = True
        raise AssertionError("Cursor/local loop must not run for uncertain findings")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha="a" * 40,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[thread]),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            return_value=[thread],
        ),
        patch(
            "ai_dev_loop.commands.pr_review._load_thread_bodies",
            return_value=[
                {
                    "thread_id": "THREAD1",
                    "author_login": "chatgpt-codex-connector",
                    "path": "x.py",
                    "line": 1,
                    "commit_sha": "a" * 40,
                    "body": "Why this?",
                }
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=lambda state, run_directory, **kwargs: (
                _write_adjudication_snapshot(run_directory, review, artifacts)
                or (review, artifacts)
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="R1"),
        ),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fail_if_resume),
    ):
        from ai_dev_loop.config import load_project_config

        config = load_project_config(repo / "ai_dev_loop.yaml")
        cfg.return_value = config
        run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    assert "THREAD1" in final.github_pr_review.replied_thread_ids
    assert cursor_called["value"] is False
    # Default CLI/status surfaces must not embed the comment body.
    status = render_pr_review_status(state.run_id)
    assert "Why this?" not in status
    assert "@rojobad Need more context" not in status


def test_continue_rejects_other_author(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import continue_pr_review_cycle
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import ValidationError
    from ai_dev_loop.process import ProcessResult

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.WAITING_FOR_USER_ATTENTION
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="waiting_for_user_attention",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_comment_id="1",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)
    continue_body = "@rojobad /ai-dev-loop continue"
    other_author_comments = json.dumps(
        [
            {
                "id": 40,
                "user": {"login": "someone-else"},
                "body": continue_body,
            }
        ]
    )
    authorized_comments = json.dumps(
        [
            {
                "id": 40,
                "user": {"login": "someone-else"},
                "body": continue_body,
            },
            {
                "id": 55,
                "user": {"login": "rojobad"},
                "body": continue_body,
            },
        ]
    )

    def _gh_result(payload: str) -> ProcessResult:
        return ProcessResult(
            args=["gh", "api"],
            returncode=0,
            stdout=payload,
            stderr="",
            timed_out=False,
        )

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_result(other_author_comments),
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        with pytest.raises(ValidationError, match="configured user"):
            continue_pr_review_cycle(state.run_id)

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_result(authorized_comments),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        message = continue_pr_review_cycle(state.run_id)
    assert "Continue accepted" in message
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.AWAITING_BOT_REVIEW
    assert final.github_pr_review is not None
    assert final.github_pr_review.continue_comment_id == "55"


def test_resume_fixing_external_feedback_releases_locks(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import _run_locks, resume_pr_review_cycle

    run_path = Path(str(prepared_run["run_path"]))
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.INTERRUPTED
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="fixing_external_feedback",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_created_at="2026-07-16T12:00:00+00:00",
        external_fix_prompt_path="prompts/fixes/github-01.txt",
    )
    save_run_state(run_path, state)
    order: list[str] = []

    def fake_resume(run_id: str) -> object:
        order.append("resume_run")
        locks = _run_locks(run_path, run_id=run_id, repository_path=state.repository.root)
        locks.acquire()
        order.append("lock_acquired_during_resume")
        locks.release()
        return None

    with patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fake_resume):
        message = resume_pr_review_cycle(state.run_id)
    assert "Resumed local fix loop" in message
    assert order == ["resume_run", "lock_acquired_during_resume"]
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.RUNNING_CURSOR


def test_worker_failure_persists_interrupted_lifecycle(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.errors import AiDevLoopError

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            side_effect=AiDevLoopError("gh is not authenticated or lacks required permissions"),
        ),
    ):
        from ai_dev_loop.config import load_project_config

        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        with pytest.raises(AiDevLoopError):
            run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "interrupted"
    assert final.github_pr_review.worker_outcome == "auth"
    assert final.status != RunStatus.EVALUATING_BOT_FEEDBACK
    assert "token" not in (final.last_error or "").lower()


def test_publication_push_failure_keeps_committed_checkpoint(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import _run_publication_pipeline
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError
    from ai_dev_loop.runners.publish import PublicationText

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="publishing_initial",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=None,
        head_branch=state.repository.branch,
        bound_head_sha="b" * 40,
        staged_patch_sha256="d" * 64,
        publication_phase="pre_commit",
        publication_text_path="github/cycles/01/publication-text.json",
    )
    save_run_state(run_path, state)
    (run_path / "github/cycles/01").mkdir(parents=True, exist_ok=True)
    (run_path / "github/cycles/01/publication-text.json").write_text(
        json.dumps(
            {
                "commit_subject": "s",
                "commit_body": "b",
                "pr_title": "t",
                "pr_body": "pb",
            }
        ),
        encoding="utf-8",
    )

    def fake_publish(*_a, **kwargs):
        after_commit = kwargs.get("after_commit")
        if after_commit is not None:
            after_commit("c" * 40, None, "origin", state.repository.branch)
        raise AiDevLoopError("git push failed; repository left with local commit")

    config = load_project_config(repo / "ai_dev_loop.yaml")
    with (
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=fake_publish,
        ),
        pytest.raises(AiDevLoopError, match="git push failed"),
    ):
        _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )

    checkpoint = load_run_state(run_path / "state.json")
    assert checkpoint.github_pr_review is not None
    assert checkpoint.github_pr_review.publication_phase == "committed"
    assert checkpoint.github_pr_review.local_commit_sha == "c" * 40

    # Retry resumes without calling after_commit again (no duplicate commit).
    calls: list[str] = []

    def resume_publish(*_a, **kwargs):
        calls.append("publish")
        assert kwargs.get("resume_from_commit") == "c" * 40
        assert kwargs.get("after_commit") is None
        return PublishResult("c" * 40, "origin", "origin/branch", "d" * 64, None, True)

    pr = GithubPullRequest(
        number=7,
        url="https://example.test/pr/7",
        title="t",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha="c" * 40,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    with (
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=resume_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="88"),
        ),
    ):
        final = _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )
    assert calls == ["publish"]
    assert final.status == RunStatus.AWAITING_BOT_REVIEW
    assert final.github_pr_review is not None
    assert final.github_pr_review.pr_number == 7
    assert final.github_pr_review.publication_phase is None


def test_publication_pr_operation_failure_keeps_pushed_checkpoint(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import _run_publication_pipeline
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError
    from ai_dev_loop.runners.publish import PublicationText

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="publishing_initial",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=None,
        head_branch=state.repository.branch,
        bound_head_sha="b" * 40,
        staged_patch_sha256="d" * 64,
        publication_phase="pre_commit",
        publication_text_path="github/cycles/01/publication-text.json",
    )
    save_run_state(run_path, state)
    (run_path / "github/cycles/01").mkdir(parents=True, exist_ok=True)
    (run_path / "github/cycles/01/publication-text.json").write_text(
        json.dumps(
            {
                "commit_subject": "s",
                "commit_body": "b",
                "pr_title": "t",
                "pr_body": "pb",
            }
        ),
        encoding="utf-8",
    )
    config = load_project_config(repo / "ai_dev_loop.yaml")

    with (
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            return_value=PublishResult("c" * 40, "origin", "origin/branch", "d" * 64, None, False),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            side_effect=AiDevLoopError("GitHub PR create failed"),
        ),
        pytest.raises(AiDevLoopError, match="PR create failed"),
    ):
        _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )

    checkpoint = load_run_state(run_path / "state.json")
    assert checkpoint.github_pr_review is not None
    assert checkpoint.github_pr_review.publication_phase == "pushed"
    assert checkpoint.github_pr_review.local_commit_sha == "c" * 40

    pr = GithubPullRequest(
        number=11,
        url="https://example.test/pr/11",
        title="t",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha="c" * 40,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    with (
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=AssertionError("must not republish after pushed checkpoint"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="99"),
        ),
    ):
        final = _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )
    assert final.status == RunStatus.AWAITING_BOT_REVIEW
    assert final.github_pr_review is not None
    assert final.github_pr_review.pr_number == 11
    assert final.github_pr_review.publication_phase is None


def _write_publication_text(run_path: Path) -> None:
    (run_path / "github/cycles/01").mkdir(parents=True, exist_ok=True)
    (run_path / "github/cycles/01/publication-text.json").write_text(
        json.dumps(
            {
                "commit_subject": "s",
                "commit_body": "b",
                "pr_title": "t",
                "pr_body": "pb",
            }
        ),
        encoding="utf-8",
    )


def test_create_push_failure_resume_via_cli_without_duplicate_commit(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import (
        create_pr_review_cycle,
        resume_pr_review_cycle,
        run_pr_review_worker_loop,
    )
    from ai_dev_loop.errors import AiDevLoopError
    from ai_dev_loop.run_discovery import find_run_directory

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.COMPLETED
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.result = "Local review accepted"
    save_run_state(run_path, state)
    _stage_change(repo)
    head_sha = "c" * 40
    publish_calls: list[str] = []

    def failing_publish(*_a, **kwargs):
        publish_calls.append("first")
        after_commit = kwargs.get("after_commit")
        if after_commit is not None:
            after_commit(head_sha, None, "origin", state.repository.branch)
        raise AiDevLoopError("git push failed; repository left with local commit")

    with (
        patch("ai_dev_loop.commands.pr_review.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review.validate_clean_except_staged", return_value="PATCH"),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            return_value=PublicationText("s", "b", "t", "pb"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=failing_publish,
        ),
        patch("ai_dev_loop.commands.pr_review.sha256_text", return_value="d" * 64),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        pytest.raises(AiDevLoopError, match="git push failed"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        create_pr_review_cycle(state.run_id)

    # Discover the interrupted successor created before publication failed.
    from ai_dev_loop.run_discovery import list_run_directories

    interrupted = next(
        state
        for path, state in list_run_directories()
        if path != run_path
        and state.github_pr_review is not None
        and state.status == RunStatus.INTERRUPTED
    )
    assert interrupted.github_pr_review is not None
    assert interrupted.github_pr_review.lifecycle == "publishing_initial"
    assert interrupted.github_pr_review.publication_phase == "committed"
    assert interrupted.github_pr_review.local_commit_sha == head_sha
    assert interrupted.github_pr_review.worker_outcome == "push_failed"

    pr = GithubPullRequest(
        number=42,
        url="https://example.test/pr/42",
        title="t",
        state="OPEN",
        head_ref=interrupted.repository.branch,
        head_sha=head_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )

    def resume_publish(*_a, **kwargs):
        publish_calls.append("resume")
        assert kwargs.get("resume_from_commit") == head_sha
        assert kwargs.get("after_commit") is None
        return PublishResult(head_sha, "origin", "origin/branch", "d" * 64, None, True)

    with (
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker") as spawn,
        patch("ai_dev_loop.workflow_engine.resume_run"),
    ):
        message = resume_pr_review_cycle(interrupted.run_id)
    assert "Resumed publication" in message
    spawn.assert_called_once()
    resumed = load_run_state(find_run_directory(interrupted.run_id) / "state.json")
    assert resumed.status == RunStatus.PUBLISHING_EXTERNAL_FIX
    assert resumed.github_pr_review is not None
    assert resumed.github_pr_review.lifecycle == "publishing_initial"
    assert resumed.github_pr_review.publication_phase == "committed"

    clock = {"now": 0.0}

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=resume_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="999"),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.time.monotonic",
            side_effect=lambda: clock["now"],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.time.sleep",
            side_effect=lambda _s: clock.__setitem__("now", clock["now"] + 10_000.0),
        ),
    ):
        from ai_dev_loop.config import load_project_config

        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(interrupted.run_id)

    final = load_run_state(find_run_directory(interrupted.run_id) / "state.json")
    assert publish_calls == ["first", "resume"]
    assert final.github_pr_review is not None
    assert final.github_pr_review.pr_number == 42
    # Worker continues polling in-process after publish; empty window + expired clock.
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review.worker_outcome == "timeout"


def test_external_fix_push_failure_resume_via_cli(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import (
        resume_pr_review_cycle,
        run_pr_review_worker_loop,
    )
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="publishing_external_fix",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        staged_patch_sha256="d" * 64,
        publication_phase="pre_commit",
        publication_text_path="github/cycles/01/publication-text.json",
        eligible_thread_ids=["T1"],
    )
    save_run_state(run_path, state)
    _write_publication_text(run_path)

    def failing_publish(*_a, **kwargs):
        after_commit = kwargs.get("after_commit")
        if after_commit is not None:
            after_commit("c" * 40, None, "origin", state.repository.branch)
        raise AiDevLoopError("git push failed; repository left with local commit")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=failing_publish,
        ),
        pytest.raises(AiDevLoopError, match="git push failed"),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)

    failed = load_run_state(run_path / "state.json")
    assert failed.status == RunStatus.INTERRUPTED
    assert failed.github_pr_review is not None
    assert failed.github_pr_review.lifecycle == "publishing_external_fix"
    assert failed.github_pr_review.publication_phase == "committed"
    assert failed.github_pr_review.worker_outcome == "push_failed"

    publish_calls: list[str] = []

    def resume_publish(*_a, **kwargs):
        publish_calls.append("resume")
        assert kwargs.get("resume_from_commit") == "c" * 40
        return PublishResult("c" * 40, "origin", "origin/branch", "d" * 64, None, True)

    with patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"):
        assert "Resumed publication" in resume_pr_review_cycle(state.run_id)

    pr = GithubPullRequest(
        number=9,
        url="https://example.test/pr/9",
        title="t",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha="c" * 40,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    clock = {"now": 0.0}
    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=resume_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="T1"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="1001"),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.time.monotonic",
            side_effect=lambda: clock["now"],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.time.sleep",
            side_effect=lambda _s: clock.__setitem__("now", clock["now"] + 10_000.0),
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert publish_calls == ["resume"]
    assert final.github_pr_review is not None
    assert final.github_pr_review.request_comment_id == "1001"
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review.worker_outcome == "timeout"
    assert final.github_pr_review.cycle_number == 2
    assert final.github_pr_review.request_comment_id == "1001"


def test_review_trigger_crash_window_posts_exactly_once(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import _run_publication_pipeline
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    head_sha = "c" * 40
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="publishing_initial",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=None,
        head_branch=state.repository.branch,
        bound_head_sha="b" * 40,
        staged_patch_sha256="d" * 64,
        publication_phase="pushed",
        local_commit_sha=head_sha,
        publication_commit_sha=head_sha,
        publication_text_path="github/cycles/01/publication-text.json",
    )
    save_run_state(run_path, state)
    _write_publication_text(run_path)
    config = load_project_config(repo / "ai_dev_loop.yaml")
    pr = GithubPullRequest(
        number=7,
        url="https://example.test/pr/7",
        title="t",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha=head_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    create_calls: list[str] = []
    posted_bodies: list[str] = []

    def create_once(*_a, **kwargs):
        create_calls.append("create")
        body = str(kwargs.get("body") or "")
        posted_bodies.append(body)
        # Simulate crash after GitHub accepted the comment but before local bind.
        raise AiDevLoopError("failed to post review trigger comment: connection reset")

    marker = f"ai_dev_loop-pr-review:{state.run_id}:cycle:1:{head_sha}"

    with (
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=create_once,
        ),
        pytest.raises(AiDevLoopError, match="review trigger"),
    ):
        _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )

    checkpoint = load_run_state(run_path / "state.json")
    assert checkpoint.github_pr_review is not None
    assert checkpoint.github_pr_review.publication_phase == "pr_bound"
    assert checkpoint.github_pr_review.request_marker == marker
    assert checkpoint.github_pr_review.request_comment_id is None
    assert create_calls == ["create"]
    assert any("@codex review" in body or "codex" in body.lower() for body in posted_bodies) or (
        config.github is not None and config.github.review_trigger_body in posted_bodies[0]
    )

    # Retry: marker already on GitHub; bind without a second create.
    with (
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=("777", "2026-07-16T12:00:00+00:00"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=AssertionError("must not create a duplicate trigger comment"),
        ),
    ):
        final = _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=True,
        )
    assert create_calls == ["create"]
    assert final.status == RunStatus.AWAITING_BOT_REVIEW
    assert final.github_pr_review is not None
    assert final.github_pr_review.request_comment_id == "777"
    assert final.github_pr_review.request_marker == marker


def test_next_cycle_trigger_crash_window_posts_exactly_once(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import _run_publication_pipeline
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    head_sha = "c" * 40
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="publishing_external_fix",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        staged_patch_sha256="d" * 64,
        publication_phase="pushed",
        local_commit_sha=head_sha,
        publication_commit_sha=head_sha,
        publication_text_path="github/cycles/01/publication-text.json",
        eligible_thread_ids=["T1"],
    )
    save_run_state(run_path, state)
    _write_publication_text(run_path)
    config = load_project_config(repo / "ai_dev_loop.yaml")
    create_calls: list[str] = []
    marker = f"ai_dev_loop-pr-review:{state.run_id}:cycle:2:{head_sha}"

    def _boom_create(*_a, **_k):
        create_calls.append("create")
        raise AiDevLoopError("failed to post review trigger comment: network")

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="https://example.test/pr/9",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head_sha,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="T1"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=_boom_create,
        ),
        pytest.raises(AiDevLoopError, match="review trigger"),
    ):
        _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=False,
        )

    mid = load_run_state(run_path / "state.json")
    assert mid.github_pr_review is not None
    assert mid.github_pr_review.request_marker == marker
    assert mid.github_pr_review.request_comment_id is None
    assert create_calls == ["create"]

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="https://example.test/pr/9",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head_sha,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="T1"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=("888", "2026-07-16T13:00:00+00:00"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=AssertionError("must not create a duplicate next-cycle trigger"),
        ),
    ):
        final = _run_publication_pipeline(
            run_path,
            config=config,
            text=PublicationText("s", "b", "t", "pb"),
            branch=state.repository.branch,
            create_pr=False,
        )
    assert create_calls == ["create"]
    assert final.github_pr_review is not None
    assert final.github_pr_review.cycle_number == 2
    assert final.github_pr_review.request_comment_id == "888"


def test_create_publication_text_failure_resume_via_cli_exactly_once(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import (
        create_pr_review_cycle,
        resume_pr_review_cycle,
        run_pr_review_worker_loop,
    )
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.errors import AiDevLoopError, ValidationError
    from ai_dev_loop.run_discovery import find_run_directory, list_run_directories

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.COMPLETED
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.result = "Local review accepted"
    source_session = state.codex.session_id
    save_run_state(run_path, state)
    _stage_change(repo)
    head_sha = "c" * 40
    text_calls: list[str] = []

    def fail_publication_text(run_state, *_a, **_k):
        text_calls.append("fail")
        assert run_state.codex.session_id == source_session
        raise AiDevLoopError(
            "Codex publication-text turn failed; inspect github/cycles/01/publication.events.jsonl"
        )

    with (
        patch("ai_dev_loop.commands.pr_review.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review.validate_clean_except_staged", return_value="PATCH"),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            side_effect=fail_publication_text,
        ),
        patch("ai_dev_loop.commands.pr_review.sha256_text", return_value="d" * 64),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=AssertionError("must not mutate git before publication text succeeds"),
        ),
        pytest.raises(AiDevLoopError, match="publication-text"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        create_pr_review_cycle(state.run_id)

    interrupted = next(
        item
        for path, item in list_run_directories()
        if path != run_path
        and item.github_pr_review is not None
        and item.status == RunStatus.INTERRUPTED
    )
    assert interrupted.github_pr_review is not None
    assert interrupted.github_pr_review.lifecycle == "publishing_initial"
    assert interrupted.github_pr_review.publication_phase == "pre_commit"
    assert interrupted.github_pr_review.publication_text_path is None
    assert interrupted.github_pr_review.worker_outcome == "publication_text_failed"
    assert interrupted.codex.session_id == source_session
    assert text_calls == ["fail"]

    # Public create must not invent a second successor for the same source.
    with (
        patch("ai_dev_loop.commands.pr_review.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review.validate_clean_except_staged", return_value="PATCH"),
        pytest.raises(ValidationError, match="active PR-review cycle already exists"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        create_pr_review_cycle(state.run_id)

    with patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"):
        assert "Resumed publication" in resume_pr_review_cycle(interrupted.run_id)

    def succeed_publication_text(run_state, *_a, **_k):
        text_calls.append("resume")
        assert run_state.codex.session_id == source_session
        return PublicationText("s", "b", "t", "pb")

    pr = GithubPullRequest(
        number=42,
        url="https://example.test/pr/42",
        title="t",
        state="OPEN",
        head_ref=interrupted.repository.branch,
        head_sha=head_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    clock = {"now": 0.0}
    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            side_effect=succeed_publication_text,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            return_value=PublishResult(head_sha, "origin", "origin/branch", "d" * 64, None, False),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_or_update_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="999"),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.time.monotonic",
            side_effect=lambda: clock["now"],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.time.sleep",
            side_effect=lambda _s: clock.__setitem__("now", clock["now"] + 10_000.0),
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(interrupted.run_id)

    final = load_run_state(find_run_directory(interrupted.run_id) / "state.json")
    assert text_calls == ["fail", "resume"]
    assert final.github_pr_review is not None
    assert final.github_pr_review.pr_number == 42
    assert final.codex.session_id == source_session
    assert final.github_pr_review.publication_text_path == "github/cycles/01/publication-text.json"
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review.worker_outcome == "timeout"


def _enable_no_findings_github(repo: Path) -> None:
    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "no_findings_completion:" in text:
        return
    if "github:" not in text:
        _enable_github(repo)
        text = config.read_text(encoding="utf-8")
    config.write_text(
        text + "\n  acknowledgement:\n    enabled: true\n    timeout_seconds: 1\n"
        "  no_findings_completion:\n    enabled: true\n"
        "    accepted_comment_prefixes:\n"
        '      - "Codex Review: Didn\'t find any major issues."\n'
        "    reviewed_commit_prefix_length: 12\n",
        encoding="utf-8",
    )


def test_no_findings_comment_completes_without_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        NoFindingsCompletionMatch,
    )
    from ai_dev_loop.state import sha256_text

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_no_findings_github(repo)
    state = load_run_state(run_path / "state.json")
    head = "a" * 40
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{head[:12]}`\n"
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="1",
        request_marker="marker",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)

    detail = GithubIssueCommentDetail(
        comment_id="88",
        author_login="chatgpt-codex-connector",
        body=body,
        body_sha256=sha256_text(body),
        created_at="2026-07-16T12:10:00+00:00",
    )
    expected_match = NoFindingsCompletionMatch(
        comment_id="88",
        created_at="2026-07-16T12:10:00+00:00",
        body_sha256=sha256_text(body),
        rule_id="accepted_comment_prefix:0",
        reviewed_commit_prefix=head[:12],
    )
    forbidden = {
        "cursor": False,
        "codex": False,
        "commit": False,
        "resolve": False,
        "trigger": False,
    }

    def fail_cursor(*_a, **_k):
        forbidden["cursor"] = True
        raise AssertionError("Cursor must not run for no-findings completion")

    def fail_codex(*_a, **_k):
        forbidden["codex"] = True
        raise AssertionError("Codex adjudication must not run")

    def fail_comment(*_a, **_k):
        forbidden["trigger"] = True
        raise AssertionError("must not post another trigger")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_details",
            return_value=[detail],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.match_no_findings_completion",
            return_value=expected_match,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=fail_codex,
        ),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fail_cursor),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            side_effect=lambda *_a, **_k: forbidden.__setitem__("resolve", True),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=fail_comment,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=lambda *_a, **_k: forbidden.__setitem__("commit", True),
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "completed"
    assert final.github_pr_review.no_findings_completion is not None
    assert final.github_pr_review.no_findings_completion.comment_id == "88"
    assert final.github_pr_review.no_findings_completion.body_sha256 == sha256_text(body)
    assert "Didn't find" not in (final.result or "")
    assert all(not v for v in forbidden.values())
    status = render_pr_review_status(state.run_id)
    assert body not in status
    assert "Didn't find any major issues" not in status

    # Idempotent: already completed worker is a no-op.
    with patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg:
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)
    again = load_run_state(run_path / "state.json")
    assert again.status == RunStatus.COMPLETED
    assert again.github_pr_review is not None
    assert again.github_pr_review.no_findings_completion is not None


def test_eligible_thread_wins_over_no_findings_during_revalidation(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        GithubReviewThread,
        NoFindingsCompletionMatch,
    )
    from ai_dev_loop.state import sha256_text

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_no_findings_github(repo)
    state = load_run_state(run_path / "state.json")
    head = "b" * 40
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{head[:12]}`\n"
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="1",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)

    thread = GithubReviewThread(
        thread_id="THREAD_RACE",
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="x.py",
        line=1,
        commit_sha=head,
        root_comment_id="C1",
        root_comment_body_sha256="b" * 64,
        created_at="2026-07-16T12:05:00+00:00",
        review_id=None,
    )
    match = NoFindingsCompletionMatch(
        comment_id="88",
        created_at="2026-07-16T12:10:00+00:00",
        body_sha256=sha256_text(body),
        rule_id="accepted_comment_prefix:0",
        reviewed_commit_prefix=head[:12],
    )
    thread_reads = {"n": 0}

    def threads_side_effect(*_a, **_k):
        thread_reads["n"] += 1
        if thread_reads["n"] == 1:
            return []
        return [thread]

    def eligible_side_effect(threads, **_k):
        return list(threads)

    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": ["THREAD_RACE"],
            "thread_decisions": [
                {
                    "thread_id": "THREAD_RACE",
                    "decision": "uncertain",
                    "inline_reply": "@rojobad Need clarification.",
                    "summary": "unclear",
                }
            ],
            "all_actionable": False,
            "review_markdown": "report",
            "cursor_fix_prompt": None,
            "tests_status": "not_applicable",
            "summary": "uncertain",
            "residual_risk_comment": None,
            "highest_severity": None,
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/01/codex.events.jsonl",
        stderr_path="github/cycles/01/codex.stderr.txt",
        result_path="github/cycles/01/result.json",
        report_path="github/cycles/01/report.md",
        metadata_path="github/cycles/01/codex.metadata.json",
        snapshot_path="github/cycles/01/threads.snapshot.json",
        fix_prompt_path=None,
    )

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            side_effect=threads_side_effect,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=eligible_side_effect,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_details",
            return_value=[
                GithubIssueCommentDetail(
                    comment_id="88",
                    author_login="chatgpt-codex-connector",
                    body=body,
                    body_sha256=sha256_text(body),
                    created_at="2026-07-16T12:10:00+00:00",
                )
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.match_no_findings_completion",
            return_value=match,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
        patch(
            "ai_dev_loop.commands.pr_review._load_thread_bodies",
            return_value=[
                {
                    "thread_id": "THREAD_RACE",
                    "author_login": "chatgpt-codex-connector",
                    "path": "x.py",
                    "line": 1,
                    "commit_sha": head,
                    "body": "Why?",
                }
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=lambda state, run_directory, **kwargs: (
                _write_adjudication_snapshot(run_directory, review, artifacts)
                or (review, artifacts)
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="R1"),
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    assert final.github_pr_review.no_findings_completion is None
    assert "THREAD_RACE" in final.github_pr_review.replied_thread_ids


def test_eyes_ack_and_timeout_are_diagnostic_only(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.runners.github import GithubCommentReaction

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_no_findings_github(repo)
    state = load_run_state(run_path / "state.json")
    head = "c" * 40
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="1",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)

    trigger_calls = {"n": 0}
    polls = {"n": 0}

    def reactions_side_effect(*_a, **_k):
        polls["n"] += 1
        if polls["n"] == 1:
            return [
                GithubCommentReaction(
                    reaction_id="1",
                    user_login="chatgpt-codex-connector",
                    content="eyes",
                )
            ]
        return []

    def sleep_and_interrupt(_seconds: float) -> None:
        # Force the worker out after acknowledgement persistence.
        raise KeyboardInterrupt("stop after ack poll")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_details",
            return_value=[],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.match_no_findings_completion",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            side_effect=reactions_side_effect,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=lambda *_a, **_k: trigger_calls.__setitem__("n", trigger_calls["n"] + 1),
        ),
        patch("ai_dev_loop.commands.pr_review.time.sleep", side_effect=sleep_and_interrupt),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        with pytest.raises(KeyboardInterrupt):
            run_pr_review_worker_loop(state.run_id)

    mid = load_run_state(run_path / "state.json")
    assert mid.status == RunStatus.AWAITING_BOT_REVIEW
    assert mid.github_pr_review is not None
    assert mid.github_pr_review.bot_acknowledgement is not None
    assert mid.github_pr_review.bot_acknowledgement.first_observed_at is not None
    assert mid.github_pr_review.lifecycle == "awaiting_bot_review"
    assert trigger_calls["n"] == 0
    status = render_pr_review_status(state.run_id)
    assert "Bot acknowledgement: observed" in status
    assert mid.github_pr_review.no_findings_completion is None


def test_no_findings_completes_without_ever_observing_eyes(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.commands.pr_review import run_pr_review_worker_loop
    from ai_dev_loop.config import load_project_config
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        NoFindingsCompletionMatch,
    )
    from ai_dev_loop.state import sha256_text

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_no_findings_github(repo)
    state = load_run_state(run_path / "state.json")
    head = "d" * 40
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{head[:12]}`\n"
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=9,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="1",
        request_created_at="2026-07-16T12:00:00+00:00",
    )
    save_run_state(run_path, state)
    match = NoFindingsCompletionMatch(
        comment_id="77",
        created_at="2026-07-16T12:10:00+00:00",
        body_sha256=sha256_text(body),
        rule_id="accepted_comment_prefix:0",
        reviewed_commit_prefix=head[:12],
    )
    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=GithubPullRequest(
                number=9,
                url="",
                title="t",
                state="OPEN",
                head_ref=state.repository.branch,
                head_sha=head,
                base_ref="master",
                is_cross_repository=False,
                repository_name_with_owner="acme/demo",
            ),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_details",
            return_value=[
                GithubIssueCommentDetail(
                    comment_id="77",
                    author_login="chatgpt-codex-connector",
                    body=body,
                    body_sha256=sha256_text(body),
                    created_at="2026-07-16T12:10:00+00:00",
                )
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.match_no_findings_completion",
            return_value=match,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        run_pr_review_worker_loop(state.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert final.github_pr_review is not None
    assert final.github_pr_review.bot_acknowledgement is not None
    assert final.github_pr_review.bot_acknowledgement.first_observed_at is None
    assert final.github_pr_review.no_findings_completion is not None
