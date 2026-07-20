"""Phase 15.12: fresh external Cursor iteration and external_feedback_cursor recovery."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import resume_pr_review_cycle, run_pr_review_worker_loop
from ai_dev_loop.commands.pr_review_recover import (
    analyze_pr_review_recovery,
    recover_pr_review_cycle,
)
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.iterations import cursor_prompt_path, read_cursor_prompt
from ai_dev_loop.legacy_pr_review_local_adapter import scheduled_cursor_turn_from_legacy_pr_state
from ai_dev_loop.resume_planner import LocalInvocationContext, WorkflowActionKind, plan_next_action
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread
from ai_dev_loop.state import (
    ControllerState,
    GithubPrReviewState,
    RecoveryState,
    RunStatus,
    load_run_state,
    save_run_state,
    sha256_file,
    utc_now,
)


def _local_resume_result(
    *, needs_external_continuation: bool = False, status: str = "running_cursor"
):
    result = MagicMock()
    result.needs_external_continuation = needs_external_continuation
    result.status = status
    return result


runner = CliRunner()
CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_C1 = "PRRT_CYCLE1_A"
THREAD_C1B = "PRRT_CYCLE1_B"
THREAD_C2A = "PRRT_CYCLE2_A"
THREAD_C2B = "PRRT_CYCLE2_B"
THREAD_C2C = "PRRT_CYCLE2_C"
HEAD_SHA = "c29e15e6608a1111222233334444555566667777"
EXTERNAL_PROMPT = "Please fix all three cycle-2 actionable threads.\n"


def _legacy_context(state, run_directory: Path) -> LocalInvocationContext:
    turn = scheduled_cursor_turn_from_legacy_pr_state(state, run_directory)
    return LocalInvocationContext(scheduled_first_cursor_turn=turn)


def _enable_github(repo: Path) -> None:
    """Append github config after prepare/start so baseline checks stay clean."""

    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "github:" not in text:
        config.write_text(
            text
            + "\ngithub:\n  enabled: true\n  poll_interval_seconds: 1\n  poll_timeout_hours: 1\n",
            encoding="utf-8",
        )


def _open_pr(branch: str, *, head_sha: str = HEAD_SHA) -> GithubPullRequest:
    return GithubPullRequest(
        number=45,
        url="https://example.test/pr/45",
        title="t",
        state="OPEN",
        head_ref=branch,
        head_sha=head_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )


def _thread(thread_id: str, *, commit_sha: str = HEAD_SHA) -> GithubReviewThread:
    return GithubReviewThread(
        thread_id=thread_id,
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="a.py",
        line=1,
        commit_sha=commit_sha,
        root_comment_id=f"C-{thread_id}",
        root_comment_body_sha256="b" * 64,
        created_at="2026-07-18T12:10:00+00:00",
        review_id=None,
    )


def _actionable_result(thread_ids: list[str], prompt: str = EXTERNAL_PROMPT) -> dict:
    return {
        "eligible_thread_ids": thread_ids,
        "thread_decisions": [
            {
                "thread_id": thread_id,
                "decision": "actionable",
                "inline_reply": None,
                "summary": f"finding-{thread_id}",
            }
            for thread_id in thread_ids
        ],
        "all_actionable": True,
        "review_markdown": "report",
        "cursor_fix_prompt": prompt,
        "tests_status": "not_applicable",
        "summary": "all actionable",
        "residual_risk_comment": None,
        "highest_severity": "P2",
    }


@contextmanager
def _mock_remote_pr(
    pr: GithubPullRequest,
    threads: list[GithubReviewThread],
) -> Iterator[None]:
    fake_config = MagicMock()
    fake_config.github.command = "gh"
    fake_config.github.reviewer_logins = ["chatgpt-codex-connector"]
    fake_config.github.poll_timeout_hours = 1
    fake_config.github.poll_interval_seconds = 1
    with (
        patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=fake_config),
        patch(
            "ai_dev_loop.commands.pr_review_recover.list_review_threads",
            return_value=threads,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=threads,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_recover.filter_eligible_threads",
            side_effect=lambda items, **kwargs: [
                t
                for t in items
                if t.thread_id not in set(kwargs.get("already_processed_thread_ids") or set())
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda items, **kwargs: [
                t
                for t in items
                if t.thread_id not in set(kwargs.get("already_processed_thread_ids") or set())
            ],
        ),
    ):
        yield


def _seed_failed_external_feedback_cursor_run(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_typed_iteration: bool = False,
    continue_comment_id: str | None = None,
    with_nested_reviewing_recovery: bool = False,
) -> tuple[Path, str, str, str]:
    """Incident-shaped source: completed 01, cycle-2 actionable, Cursor never started."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    # Clean worktree for recovery baseline checks (fixture-local git only).
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)

    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id
    chat_id = state.cursor.chat_id
    bound_sha = state.repository.initial_head
    state.status = RunStatus.FAILED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.workflow.current_review_iteration = 0
    state.result = "no staged changes after git add -A"
    state.last_error = "no staged changes after git add -A"
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    assert state.iterations, "expected completed local iteration 01 from start_run"
    for stale in (run_path / "cursor" / "iterations").glob("*"):
        if stale.name != "01":
            shutil.rmtree(stale, ignore_errors=True)

    if with_nested_reviewing_recovery:
        # Mirror nested Phase 15.11 lineage: reviewing recovery over an earlier
        # external_adjudication freeze that was cleared via an authorized continue.
        state.recovery = RecoveryState(
            source_run_id="nested-failed-reviewing-source",
            source_status=RunStatus.FAILED.value,
            source_iteration=1,
            recovered_checkpoint="reviewing",
            created_at=utc_now(),
            runtime_migration="none",
            reason_code="codex_review_result_artifact_missing",
            source_staged_patch_sha256="b" * 64,
        )

    cycle_dir = run_path / "github/cycles/02"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    thread_ids = [THREAD_C2A, THREAD_C2B, THREAD_C2C]
    (cycle_dir / "result.json").write_text(
        json.dumps(_actionable_result(thread_ids), indent=2) + "\n", encoding="utf-8"
    )
    (cycle_dir / "threads.snapshot.json").write_text(
        json.dumps(
            {"threads": [{"thread_id": tid, "body_sha256": "a" * 64} for tid in thread_ids]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (cycle_dir / "codex.events.jsonl").write_text("{}\n", encoding="utf-8")
    fix_prompt = run_path / "prompts/fixes/github-02.txt"
    fix_prompt.parent.mkdir(parents=True, exist_ok=True)
    fix_prompt.write_text(EXTERNAL_PROMPT, encoding="utf-8")
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9002",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:2:{bound_sha}",
        request_created_at="2026-07-18T12:00:00+00:00",
        eligible_thread_ids=thread_ids,
        expected_eligible_thread_ids=thread_ids,
        processed_thread_ids=[THREAD_C1, THREAD_C1B],
        resolved_thread_ids=[THREAD_C1, THREAD_C1B],
        continue_comment_id=continue_comment_id,
        last_external_result_path="github/cycles/02/result.json",
        last_snapshot_path="github/cycles/02/threads.snapshot.json",
        external_fix_prompt_path="prompts/fixes/github-02.txt",
        external_cursor_iteration=2 if with_typed_iteration else None,
        worker_outcome=None,
    )
    save_run_state(run_path, state)
    return run_path, state.run_id, chat_id, bound_sha


def test_worker_schedules_fresh_iteration_and_exact_external_prompt(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    # Clean worktree so a later Cursor turn is not confused by prior edits.
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    # Keep completed iteration 01 only.
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    assert state.iterations
    for stale in (run_path / "cursor" / "iterations").glob("*"):
        if stale.name != "01":
            shutil.rmtree(stale, ignore_errors=True)
    head = state.repository.initial_head
    thread_ids = [THREAD_C2A, THREAD_C2B, THREAD_C2C]
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="9002",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:2:{head}",
        request_created_at="2026-07-18T12:00:00+00:00",
        eligible_thread_ids=[],
        expected_eligible_thread_ids=None,
        processed_thread_ids=[THREAD_C1, THREAD_C1B],
        resolved_thread_ids=[THREAD_C1, THREAD_C1B],
    )
    save_run_state(run_path, state)

    review = GithubPrReviewResult.model_validate(_actionable_result(thread_ids))
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/02/codex.events.jsonl",
        stderr_path="github/cycles/02/codex.stderr.txt",
        result_path="github/cycles/02/result.json",
        report_path="github/cycles/02/report.md",
        metadata_path="github/cycles/02/codex.metadata.json",
        snapshot_path="github/cycles/02/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-02.txt",
    )
    threads = [_thread(tid, commit_sha=head) for tid in thread_ids]
    resume_calls: list[str] = []

    def fake_codex(state_arg, run_directory, **kwargs):
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text(EXTERNAL_PROMPT, encoding="utf-8")
        (run_directory / artifacts.result_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.result_path).write_text(
            json.dumps(_actionable_result(thread_ids), indent=2) + "\n",
            encoding="utf-8",
        )
        result_path = run_directory / artifacts.result_path
        result_path.parent.mkdir(parents=True, exist_ok=True)
        if not result_path.is_file():
            result_path.write_text(
                json.dumps(review.model_dump(mode="json"), indent=2) + "\n",
                encoding="utf-8",
            )
        snap_path = run_directory / artifacts.snapshot_path
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        if not snap_path.is_file() or snap_path.read_text(encoding="utf-8").strip() in {"", "[]"}:
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
        return review, artifacts

    def capture_resume(run_id: str):
        resume_calls.append(run_id)
        planned = load_run_state(run_path / "state.json")
        action = plan_next_action(planned, run_path, context=_legacy_context(planned, run_path))
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        assert action.iteration_number == 2
        turn = scheduled_cursor_turn_from_legacy_pr_state(planned, run_path)
        assert turn is not None
        assert cursor_prompt_path(planned, 2, scheduled_prompt_path=turn.prompt_path) == (
            "prompts/fixes/github-02.txt"
        )
        assert (
            read_cursor_prompt(planned, run_path, 2, scheduled_prompt_path=turn.prompt_path)
            == EXTERNAL_PROMPT
        )
        return _local_resume_result()

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(state.repository.branch, head_sha=head),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=threads),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda items, **kwargs: [
                t
                for t in items
                if t.thread_id not in set(kwargs.get("already_processed_thread_ids") or set())
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review._load_thread_bodies",
            return_value=[
                {
                    "thread_id": tid,
                    "author_login": "chatgpt-codex-connector",
                    "path": "a.py",
                    "line": 1,
                    "commit_sha": head,
                    "body": f"body-{tid}",
                }
                for tid in thread_ids
            ],
        ),
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review", side_effect=fake_codex),
        patch(
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=capture_resume,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
    ):
        run_pr_review_worker_loop(state.run_id)

    post.assert_not_called()
    assert resume_calls == [state.run_id]
    final = load_run_state(run_path / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "fixing_external_feedback"
    assert final.github_pr_review.external_cursor_iteration == 2
    assert final.github_pr_review.external_fix_prompt_path == "prompts/fixes/github-02.txt"
    assert final.workflow.current_review_iteration == 1
    assert final.status == RunStatus.RUNNING_CURSOR


def test_recover_creates_immutable_successor_and_resume_opens_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run, fake_clis, monkeypatch, with_typed_iteration=False
    )
    source_before = (run_path / "state.json").read_bytes()
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
    assert dry.analysis.eligible
    assert dry.analysis.checkpoint == "external_feedback_cursor"
    assert dry.analysis.reason_code == "external_feedback_cursor_not_started"
    assert dry.analysis.iteration == 2
    assert dry.recovery_run_id is None
    assert (run_path / "state.json").read_bytes() == source_before

    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    assert (run_path / "state.json").read_bytes() == source_before
    source_after = load_run_state(run_path / "state.json")
    assert source_after.status == RunStatus.FAILED

    successor_dir = find_run_directory(created.recovery_run_id)
    assert successor_dir is not None
    successor = load_run_state(successor_dir / "state.json")
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "external_feedback_cursor"
    assert successor.recovery.reason_code == "external_feedback_cursor_not_started"
    assert successor.recovery.source_iteration == 2
    assert successor.recovery.source_prompt_sha256 == sha256_file(
        run_path / "prompts/fixes/github-02.txt"
    )
    assert set(successor.recovery.expected_eligible_thread_ids or []) == {
        THREAD_C2A,
        THREAD_C2B,
        THREAD_C2C,
    }
    assert successor.cursor.chat_id == chat_id
    assert successor.codex.session_id == REVIEWER_B
    assert successor.controller is not None
    assert successor.controller.controller_session_id == CONTROLLER_A
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.external_cursor_iteration == 2
    assert successor.github_pr_review.lifecycle == "fixing_external_feedback"
    assert (successor_dir / "prompts/fixes/github-02.txt").read_text(
        encoding="utf-8"
    ) == EXTERNAL_PROMPT
    assert not (successor_dir / "cursor" / "iterations" / "02").exists()

    with _mock_remote_pr(pr, threads):
        reused = recover_pr_review_cycle(run_id, dry_run=False)
    assert reused.recovery_run_id == created.recovery_run_id
    assert reused.analysis.reused_existing_successor

    cursor_prompts: list[str] = []

    def fake_resume(run_id_arg: str):
        planned = load_run_state(successor_dir / "state.json")
        action = plan_next_action(
            planned, successor_dir, context=_legacy_context(planned, successor_dir)
        )
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        assert action.iteration_number == 2
        turn = scheduled_cursor_turn_from_legacy_pr_state(planned, successor_dir)
        assert turn is not None
        cursor_prompts.append(
            read_cursor_prompt(planned, successor_dir, 2, scheduled_prompt_path=turn.prompt_path)
        )
        result = MagicMock()
        result.needs_external_continuation = False
        return result

    with (
        _mock_remote_pr(pr, threads),
        patch(
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=fake_resume,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
    ):
        message = resume_pr_review_cycle(
            created.recovery_run_id,
            controller_session_id=CONTROLLER_A,
        )
    assert "Resumed local fix loop" in message
    assert cursor_prompts == [EXTERNAL_PROMPT]
    post.assert_not_called()
    after_resume = load_run_state(successor_dir / "state.json")
    assert after_resume.status == RunStatus.RUNNING_CURSOR
    assert after_resume.github_pr_review is not None
    assert after_resume.github_pr_review.external_cursor_iteration == 2


def test_recover_rejects_dirty_baseline_and_partial_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run, fake_clis, monkeypatch
    )
    repo = Path(str(prepared_run["repo"]))
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]

    dirty = repo / "dirty-for-recovery.txt"
    dirty.write_text("dirty\n", encoding="utf-8")
    with _mock_remote_pr(pr, threads):
        dirty_analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not dirty_analysis.eligible
    assert "baseline_not_clean" in dirty_analysis.blockers
    dirty.unlink()

    (run_path / "cursor" / "iterations" / "02").mkdir(parents=True)
    (run_path / "cursor" / "iterations" / "02" / "stderr.txt").write_text("x", encoding="utf-8")
    with _mock_remote_pr(pr, threads):
        partial = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not partial.eligible
    assert partial.checkpoint != "external_feedback_cursor"


def test_recover_rejects_non_actionable_and_sha_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run, fake_clis, monkeypatch
    )
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]

    bad = _actionable_result([THREAD_C2A, THREAD_C2B, THREAD_C2C])
    bad["all_actionable"] = False
    bad["cursor_fix_prompt"] = None
    bad["thread_decisions"][0] = {
        "thread_id": THREAD_C2A,
        "decision": "uncertain",
        "inline_reply": "@rojobad please clarify",
        "summary": "u",
    }
    (run_path / "github/cycles/02/result.json").write_text(
        json.dumps(bad, indent=2) + "\n", encoding="utf-8"
    )
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not analysis.eligible
    assert "external_result_invalid_or_not_actionable" in analysis.blockers

    # Restore actionable result then drift head SHA.
    (run_path / "github/cycles/02/result.json").write_text(
        json.dumps(_actionable_result([THREAD_C2A, THREAD_C2B, THREAD_C2C]), indent=2) + "\n",
        encoding="utf-8",
    )
    drifted = _open_pr(
        load_run_state(run_path / "state.json").repository.branch,
        head_sha="d" * 40,
    )
    with _mock_remote_pr(drifted, threads):
        sha_analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not sha_analysis.eligible
    assert "head_sha_drift" in sha_analysis.blockers


def test_cli_recover_json_redacts_sensitive_fields(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run, fake_clis, monkeypatch
    )
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]
    with _mock_remote_pr(pr, threads):
        result = runner.invoke(
            app,
            ["pr-review", "recover", run_id, "--dry-run", "--output", "json"],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["checkpoint"] == "external_feedback_cursor"
    assert payload["trigger_will_be_reposted"] is False
    dumped = json.dumps(payload)
    assert THREAD_C2A not in dumped
    assert THREAD_C2B not in dumped
    assert EXTERNAL_PROMPT.strip() not in dumped
    assert "Please fix" not in dumped
    assert REVIEWER_B not in dumped


def test_recover_still_blocks_current_cycle_side_effects_with_continue_comment(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run,
        fake_clis,
        monkeypatch,
        continue_comment_id="9100",
    )
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    # Current-cycle thread already processed must remain a hard blocker.
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "processed_thread_ids": [
                THREAD_C1,
                THREAD_C1B,
                THREAD_C2A,
            ]
        }
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not analysis.eligible
    assert "current_threads_processed" in analysis.blockers
    assert "continue_comment_present" not in analysis.blockers


def test_recover_allows_consumed_continue_comment_from_nested_freeze(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prior-cycle freeze clearance via continue must not block external_feedback_cursor."""

    run_path, run_id, chat_id, bound_sha = _seed_failed_external_feedback_cursor_run(
        prepared_run,
        fake_clis,
        monkeypatch,
        continue_comment_id="9100",
        with_nested_reviewing_recovery=True,
    )
    source_before = (run_path / "state.json").read_bytes()
    assert load_run_state(run_path / "state.json").github_pr_review is not None
    assert load_run_state(run_path / "state.json").github_pr_review.continue_comment_id == "9100"

    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)]

    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.analysis.eligible
    assert created.analysis.checkpoint == "external_feedback_cursor"
    assert "continue_comment_present" not in created.analysis.blockers
    assert created.recovery_run_id is not None
    assert (run_path / "state.json").read_bytes() == source_before

    successor_dir = find_run_directory(created.recovery_run_id)
    assert successor_dir is not None
    successor = load_run_state(successor_dir / "state.json")
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.continue_comment_id == "9100"
    assert successor.cursor.chat_id == chat_id
    assert successor.workflow.local_review_count == 0

    cursor_prompts: list[str] = []

    def fake_resume(run_id_arg: str):
        planned = load_run_state(successor_dir / "state.json")
        action = plan_next_action(
            planned, successor_dir, context=_legacy_context(planned, successor_dir)
        )
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        assert action.iteration_number == 2
        turn = scheduled_cursor_turn_from_legacy_pr_state(planned, successor_dir)
        assert turn is not None
        cursor_prompts.append(
            read_cursor_prompt(planned, successor_dir, 2, scheduled_prompt_path=turn.prompt_path)
        )
        result = MagicMock()
        result.needs_external_continuation = False
        return result

    with (
        _mock_remote_pr(pr, threads),
        patch(
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=fake_resume,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
        patch("ai_dev_loop.commands.pr_review.resolve_review_thread") as resolve,
    ):
        message = resume_pr_review_cycle(
            created.recovery_run_id,
            controller_session_id=CONTROLLER_A,
        )
    assert "Resumed local fix loop" in message
    assert cursor_prompts == [EXTERNAL_PROMPT]
    post.assert_not_called()
    reply.assert_not_called()
    resolve.assert_not_called()
