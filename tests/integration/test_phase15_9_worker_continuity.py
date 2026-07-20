"""Phase 15.9: worker publish→poll continuity, cycle reset, and reattach resume."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import (
    _publish_external_fix,
    _run_pr_review_worker_loop_inner,
    resume_pr_review_cycle,
    run_pr_review_worker_loop,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread, GithubWriteResult
from ai_dev_loop.runners.publish import PublishResult
from ai_dev_loop.state import (
    ControllerState,
    GithubPrReviewState,
    RecoveryState,
    RunStatus,
    atomic_write_json,
    load_run_state,
    save_run_state,
    utc_now,
)

runner = CliRunner()
CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_C1 = "PRRT_CYCLE1_A"
THREAD_C1B = "PRRT_CYCLE1_B"
THREAD_C2A = "PRRT_CYCLE2_A"
THREAD_C2B = "PRRT_CYCLE2_B"
HEAD_SHA = "c29e15e6608a1111222233334444555566667777"


def _enable_github(repo: Path) -> None:
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


def _seed_awaiting(
    prepared_run: dict[str, Path | str],
    *,
    cycle_number: int = 2,
    expected: list[str] | None = None,
    processed: list[str] | None = None,
    with_controller: bool = True,
) -> tuple[Path, str]:
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    if with_controller:
        state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=cycle_number,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=HEAD_SHA,
        request_comment_id="9002",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:{cycle_number}:{HEAD_SHA}",
        request_created_at="2026-07-18T12:00:00+00:00",
        eligible_thread_ids=[],
        expected_eligible_thread_ids=expected,
        processed_thread_ids=list(processed or []),
        resolved_thread_ids=list(processed or []),
    )
    save_run_state(run_path, state)
    return run_path, state.run_id


def _write_stale_launcher(run_path: Path, run_id: str) -> None:
    atomic_write_json(
        run_path / "locks/pr-review-worker.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "worker_token": "stale-token",
            "pid": 2_147_483_600,
            "started_at": "2026-07-18T11:00:00+00:00",
            "argv_redacted": [
                "python",
                "-m",
                "ai_dev_loop.pr_review_worker",
                run_id,
                "<worker-token>",
            ],
        },
        sensitive=True,
    )


def test_worker_publish_continues_to_polling_without_self_spawn(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.codex.session_id = REVIEWER_B
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="publishing_external_fix",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_comment_id="1",
        request_marker="m1",
        request_created_at="2026-07-18T11:00:00+00:00",
        eligible_thread_ids=[THREAD_C1],
        expected_eligible_thread_ids=[THREAD_C1],
        processed_thread_ids=[],
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
    # Simulate the detonated worker owning the launcher PID (self).
    atomic_write_json(
        run_path / "locks/pr-review-worker.json",
        {
            "schema_version": 1,
            "run_id": state.run_id,
            "worker_token": "live-token",
            "pid": __import__("os").getpid(),
            "started_at": "2026-07-18T11:59:00+00:00",
            "argv_redacted": [
                "python",
                "-m",
                "ai_dev_loop.pr_review_worker",
                state.run_id,
                "<worker-token>",
            ],
        },
        sensitive=True,
    )
    pr = _open_pr(state.repository.branch)
    cycle2_threads = [_thread(THREAD_C2A), _thread(THREAD_C2B)]
    spawn_calls: list[str] = []
    github_writes: list[str] = []
    poll_iterations = {"n": 0}

    def fake_list_threads(*_a, **_k):
        poll_iterations["n"] += 1
        # 1) publication frozen-set revalidation still sees cycle-1 threads
        if poll_iterations["n"] == 1:
            return [_thread(THREAD_C1, commit_sha="a" * 40)]
        # 2) first await poll: empty window
        if poll_iterations["n"] == 2:
            return []
        return cycle2_threads

    def track_spawn(*_a, **_k):
        spawn_calls.append("spawn")
        raise AssertionError("worker must not self-spawn after in-process publication")

    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": [THREAD_C2A, THREAD_C2B],
            "thread_decisions": [
                {
                    "thread_id": THREAD_C2A,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "a",
                },
                {
                    "thread_id": THREAD_C2B,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "b",
                },
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "fix cycle 2",
            "tests_status": "not_applicable",
            "summary": "ok",
            "residual_risk_comment": None,
            "highest_severity": "P2",
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/02/codex.events.jsonl",
        stderr_path="github/cycles/02/codex.stderr.txt",
        result_path="github/cycles/02/result.json",
        report_path="github/cycles/02/report.md",
        metadata_path="github/cycles/02/codex.metadata.json",
        snapshot_path="github/cycles/02/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-02.txt",
    )

    def fake_codex(state_arg, run_directory, **kwargs):
        assert state_arg.codex.session_id == REVIEWER_B
        assert {item["thread_id"] for item in kwargs["eligible_thread_payload"]} == {
            THREAD_C2A,
            THREAD_C2B,
        }
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text("fix cycle 2", encoding="utf-8")
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

    def track_comment(*_a, **_k):
        github_writes.append("comment")
        return GithubWriteResult(ok=True, resource_id="9002")

    with (
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker", side_effect=track_spawn),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            return_value=PublishResult(HEAD_SHA, "origin", "origin/branch", "d" * 64, None, True),
        ),
        patch("ai_dev_loop.commands.pr_review.resolve_review_thread") as resolve,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=track_comment,
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", side_effect=fake_list_threads),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda threads, **_k: threads,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._load_thread_bodies",
            return_value=[
                {
                    "thread_id": THREAD_C2A,
                    "author_login": "chatgpt-codex-connector",
                    "path": "a.py",
                    "line": 1,
                    "commit_sha": HEAD_SHA,
                    "body": "A",
                },
                {
                    "thread_id": THREAD_C2B,
                    "author_login": "chatgpt-codex-connector",
                    "path": "b.py",
                    "line": 2,
                    "commit_sha": HEAD_SHA,
                    "body": "B",
                },
            ],
        ),
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review", side_effect=fake_codex),
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop"),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
    ):
        from ai_dev_loop.config import load_project_config

        resolve.return_value = GithubWriteResult(ok=True, resource_id="R1")
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        _run_pr_review_worker_loop_inner(state.run_id, run_path)

    assert spawn_calls == []
    assert github_writes == ["comment"]  # exactly one new cycle trigger
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.RUNNING_CURSOR
    assert final.github_pr_review is not None
    assert final.github_pr_review.cycle_number == 2
    # Phase 15.17: after cycle-2 adjudication the durable checkpoint freezes the
    # current window; publish had cleared the prior cycle's freeze first.
    assert final.github_pr_review.expected_eligible_thread_ids == [THREAD_C2A, THREAD_C2B]
    assert THREAD_C1 in final.github_pr_review.processed_thread_ids
    assert set(final.github_pr_review.eligible_thread_ids) == {THREAD_C2A, THREAD_C2B}
    assert final.github_pr_review.external_adjudication is not None
    assert final.github_pr_review.external_adjudication.application_status == ("cursor_scheduled")


def test_sync_publish_schedules_exactly_one_poller(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.PUBLISHING_EXTERNAL_FIX
    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="publishing_external_fix",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_comment_id="1",
        request_marker="m1",
        request_created_at="2026-07-18T11:00:00+00:00",
        eligible_thread_ids=[THREAD_C1],
        expected_eligible_thread_ids=[THREAD_C1],
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
    spawn_calls: list[bool] = []

    with (
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append(True) or True,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            return_value=PublishResult(HEAD_SHA, "origin", "origin/branch", "d" * 64, None, True),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            return_value=GithubWriteResult(ok=True, resource_id="R1"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(state.repository.branch),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=[_thread(THREAD_C1, commit_sha="a" * 40)],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda threads, **_k: threads,
        ),
        patch("ai_dev_loop.commands.pr_review.find_issue_comment_with_marker", return_value=None),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            return_value=GithubWriteResult(ok=True, resource_id="9002"),
        ),
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
    ):
        from ai_dev_loop.config import load_project_config

        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        _publish_external_fix(run_path, load_run_state(run_path / "state.json"), cfg.return_value)

    assert spawn_calls == [True]
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.AWAITING_BOT_REVIEW
    assert final.github_pr_review is not None
    assert final.github_pr_review.expected_eligible_thread_ids is None


def test_cycle_reset_accepts_new_threads_and_keeps_processed(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_awaiting(
        prepared_run,
        cycle_number=2,
        expected=None,
        processed=[THREAD_C1, THREAD_C1B],
    )
    # Recovery lineage freeze from cycle 1 must not apply after cycle advanced.
    state = load_run_state(run_path / "state.json")
    state.recovery = RecoveryState(
        source_run_id="failed-source",
        source_status=RunStatus.FAILED.value,
        source_iteration=1,
        recovered_checkpoint="external_adjudication",
        created_at=utc_now(),
        runtime_migration="none",
        reason_code="github_adjudication_schema_incompatible",
        expected_eligible_thread_ids=[THREAD_C1, THREAD_C1B],
    )
    save_run_state(run_path, state)
    threads = [_thread(THREAD_C2A), _thread(THREAD_C2B)]
    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": [THREAD_C2A, THREAD_C2B],
            "thread_decisions": [
                {
                    "thread_id": THREAD_C2A,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "a",
                },
                {
                    "thread_id": THREAD_C2B,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "b",
                },
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "fix",
            "tests_status": "not_applicable",
            "summary": "ok",
            "residual_risk_comment": None,
            "highest_severity": "P2",
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/02/codex.events.jsonl",
        stderr_path="github/cycles/02/codex.stderr.txt",
        result_path="github/cycles/02/result.json",
        report_path="github/cycles/02/report.md",
        metadata_path="github/cycles/02/codex.metadata.json",
        snapshot_path="github/cycles/02/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-02.txt",
    )

    def fake_codex(state_arg, run_directory, **kwargs):
        assert {item["thread_id"] for item in kwargs["eligible_thread_payload"]} == {
            THREAD_C2A,
            THREAD_C2B,
        }
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text("fix", encoding="utf-8")
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

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(state.repository.branch),
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
                    "thread_id": THREAD_C2A,
                    "author_login": "chatgpt-codex-connector",
                    "path": "a.py",
                    "line": 1,
                    "commit_sha": HEAD_SHA,
                    "body": "A",
                },
                {
                    "thread_id": THREAD_C2B,
                    "author_login": "chatgpt-codex-connector",
                    "path": "b.py",
                    "line": 2,
                    "commit_sha": HEAD_SHA,
                    "body": "B",
                },
            ],
        ),
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review", side_effect=fake_codex),
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop"),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
    ):
        run_pr_review_worker_loop(run_id)

    post.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome != "eligible_thread_set_drift"
    assert set(final.github_pr_review.eligible_thread_ids) == {THREAD_C2A, THREAD_C2B}
    assert THREAD_C1 in final.github_pr_review.processed_thread_ids


def test_same_cycle_drift_still_stops(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_awaiting(
        prepared_run,
        cycle_number=2,
        expected=[THREAD_C2A, THREAD_C2B],
    )
    drifted = [_thread(THREAD_C2A), _thread("PRRT_EXTRA")]
    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(load_run_state(run_path / "state.json").repository.branch),
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=drifted),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda threads, **_k: threads,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review") as codex,
    ):
        run_pr_review_worker_loop(run_id)
    post.assert_not_called()
    codex.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome == "eligible_thread_set_drift"


def test_resume_reattaches_stale_awaiting_without_github_writes(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_awaiting(prepared_run, cycle_number=2, expected=None)
    _write_stale_launcher(run_path, run_id)
    state_before = load_run_state(run_path / "state.json")
    marker_before = (
        state_before.github_pr_review.request_marker if state_before.github_pr_review else None
    )
    comment_before = (
        state_before.github_pr_review.request_comment_id if state_before.github_pr_review else None
    )
    spawn_calls: list[str] = []

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(state_before.repository.branch),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append("spawn") or True,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop") as workflow,
    ):
        message = resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)

    assert "Reattached" in message
    assert spawn_calls == ["spawn"]
    post.assert_not_called()
    workflow.assert_not_called()
    after = load_run_state(run_path / "state.json")
    assert after.status == RunStatus.AWAITING_BOT_REVIEW
    assert after.github_pr_review is not None
    assert after.github_pr_review.request_marker == marker_before
    assert after.github_pr_review.request_comment_id == comment_before
    assert after.cursor.chat_id == state_before.cursor.chat_id
    events = (run_path / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "pr_review_worker_reattached" in events
    assert "stale-token" not in events


def test_resume_awaiting_live_worker_is_idempotent(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_awaiting(prepared_run)
    starttime = read_process_starttime(os.getpid())
    assert starttime is not None
    atomic_write_json(
        run_path / "locks/pr-review-worker.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "worker_token": "live",
            "pid": os.getpid(),
            "pid_starttime": starttime,
            "started_at": "2026-07-18T12:00:00+00:00",
            "argv_redacted": [
                "python",
                "-m",
                "ai_dev_loop.pr_review_worker",
                run_id,
                "<worker-token>",
            ],
        },
        sensitive=True,
    )
    with (
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker") as spawn,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.get_pull_request") as pr_get,
    ):
        message = resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)
    assert "already polling" in message
    spawn.assert_not_called()
    post.assert_not_called()
    pr_get.assert_not_called()


def test_resume_awaiting_rejects_pid_reuse_without_spawn(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """A live PID with mismatched starttime is unsafe, not an idempotent live worker."""

    run_path, run_id = _seed_awaiting(prepared_run)
    current = read_process_starttime(os.getpid())
    assert current is not None
    atomic_write_json(
        run_path / "locks/pr-review-worker.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "worker_token": "reused-pid",
            "pid": os.getpid(),
            "pid_starttime": current + 999_999,
            "started_at": "2026-07-18T12:00:00+00:00",
            "argv_redacted": [
                "python",
                "-m",
                "ai_dev_loop.pr_review_worker",
                run_id,
                "<worker-token>",
            ],
        },
        sensitive=True,
    )
    with (
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker") as spawn,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.get_pull_request") as pr_get,
        pytest.raises(ValidationError, match="identity mismatch"),
    ):
        resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)
    spawn.assert_not_called()
    post.assert_not_called()
    pr_get.assert_not_called()


def test_resume_awaiting_rejects_wrong_controller_and_head_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_awaiting(prepared_run)
    _write_stale_launcher(run_path, run_id)
    with pytest.raises(ValidationError, match="does not match"):
        resume_pr_review_cycle(
            run_id,
            controller_session_id="019abc00-ffff-eeee-dddd-cccbbbaaa999",
        )
    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(
                load_run_state(run_path / "state.json").repository.branch,
                head_sha="d" * 40,
            ),
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker") as spawn,
        pytest.raises(ValidationError, match="head changed"),
    ):
        resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)
    spawn.assert_not_called()


def test_resume_comparable_cycle2_fixture_adjudicates_two_threads(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """Shape comparable to crypto-sentinel-20260718T115934Z-176634 cycle 2."""

    run_path, run_id = _seed_awaiting(
        prepared_run,
        cycle_number=2,
        expected=None,
        processed=[THREAD_C1, THREAD_C1B],
    )
    _write_stale_launcher(run_path, run_id)
    threads = [_thread(THREAD_C2A), _thread(THREAD_C2B)]
    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": [THREAD_C2A, THREAD_C2B],
            "thread_decisions": [
                {
                    "thread_id": THREAD_C2A,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "a",
                },
                {
                    "thread_id": THREAD_C2B,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "b",
                },
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "Please fix both cycle-2 threads.",
            "tests_status": "not_applicable",
            "summary": "all actionable",
            "residual_risk_comment": None,
            "highest_severity": "P2",
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/02/codex.events.jsonl",
        stderr_path="github/cycles/02/codex.stderr.txt",
        result_path="github/cycles/02/result.json",
        report_path="github/cycles/02/report.md",
        metadata_path="github/cycles/02/codex.metadata.json",
        snapshot_path="github/cycles/02/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-02.txt",
    )
    codex_sessions: list[str] = []

    def fake_codex(state_arg, run_directory, **kwargs):
        codex_sessions.append(state_arg.codex.session_id)
        assert set(item["thread_id"] for item in kwargs["eligible_thread_payload"]) == {
            THREAD_C2A,
            THREAD_C2B,
        }
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text(
            "Please fix both cycle-2 threads.", encoding="utf-8"
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

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(load_run_state(run_path / "state.json").repository.branch),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: True,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
    ):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "resume",
                run_id,
                "--controller-session-id",
                CONTROLLER_A,
            ],
        )
        assert result.exit_code == 0, result.stdout
        assert "Reattached" in result.stdout
        post.assert_not_called()

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(load_run_state(run_path / "state.json").repository.branch),
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
                    "thread_id": THREAD_C2A,
                    "author_login": "chatgpt-codex-connector",
                    "path": "a.py",
                    "line": 1,
                    "commit_sha": HEAD_SHA,
                    "body": "A",
                },
                {
                    "thread_id": THREAD_C2B,
                    "author_login": "chatgpt-codex-connector",
                    "path": "b.py",
                    "line": 2,
                    "commit_sha": HEAD_SHA,
                    "body": "B",
                },
            ],
        ),
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review", side_effect=fake_codex),
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop"),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post2,
    ):
        run_pr_review_worker_loop(run_id)

    post2.assert_not_called()
    assert codex_sessions == [REVIEWER_B]
    final = load_run_state(run_path / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "fixing_external_feedback"
    assert set(final.github_pr_review.eligible_thread_ids) == {THREAD_C2A, THREAD_C2B}
