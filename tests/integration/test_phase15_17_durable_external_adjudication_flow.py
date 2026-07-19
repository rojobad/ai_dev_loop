"""Phase 15.17: durable external adjudication, scoped lineage, worker handoff."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_loop.commands.pr_review import (
    _run_pr_review_worker_loop_inner,
    resume_pr_review_cycle,
    run_pr_review_worker_loop,
)
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.external_adjudication import (
    build_external_adjudication_checkpoint,
    mark_checkpoint_status,
)
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.iterations import is_external_cursor_prompt_iteration, read_cursor_prompt
from ai_dev_loop.resume_planner import WorkflowActionKind, plan_next_action
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import (
    GithubError,
    GithubErrorKind,
    GithubPullRequest,
    GithubReviewThread,
    GithubWriteResult,
)
from ai_dev_loop.state import (
    ControllerState,
    ExternalReplyIntent,
    GithubPrReviewState,
    RecoveryState,
    RunStatus,
    atomic_write_json,
    atomic_write_text,
    load_run_state,
    save_run_state,
    sha256_file,
    sha256_text,
    utc_now,
)

CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_C3 = "PRRT_CYCLE3_A"
THREAD_C4A = "PRRT_CYCLE4_A"
THREAD_C4B = "PRRT_CYCLE4_B"
THREAD_C5A = "PRRT_CYCLE5_A"
THREAD_C5B = "PRRT_CYCLE5_B"
PROMPT_03 = "Fix cycle-3 threads.\n"
PROMPT_04 = "Fix cycle-4 threads.\n"
PROMPT_05 = "Please fix the two cycle-5 threads exactly.\n"


def _enable_github(repo: Path) -> str:
    """Enable github config and commit so external Cursor baseline stays clean."""

    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "github:" not in text:
        config.write_text(
            text
            + "\ngithub:\n  enabled: true\n  poll_interval_seconds: 1\n  poll_timeout_hours: 1\n",
            encoding="utf-8",
        )
        subprocess.check_call(["git", "add", "ai_dev_loop.yaml"], cwd=repo)
        subprocess.check_call(["git", "commit", "-m", "enable github"], cwd=repo)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def _open_pr(branch: str, *, head_sha: str) -> GithubPullRequest:
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


def _thread(thread_id: str, *, commit_sha: str) -> GithubReviewThread:
    return GithubReviewThread(
        thread_id=thread_id,
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="a.py",
        line=1,
        commit_sha=commit_sha,
        root_comment_id=f"C-{thread_id}",
        root_comment_body_sha256="b" * 64,
        created_at="2026-07-19T02:00:00+00:00",
        review_id=None,
    )


def _actionable_result(thread_ids: list[str], prompt: str) -> dict:
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
        "review_markdown": "report\n",
        "cursor_fix_prompt": prompt,
        "tests_status": "not_applicable",
        "summary": "all actionable",
        "residual_risk_comment": None,
        "highest_severity": "P2",
    }


def _non_actionable_result(thread_ids: list[str]) -> dict:
    return {
        "eligible_thread_ids": thread_ids,
        "thread_decisions": [
            {
                "thread_id": thread_ids[0],
                "decision": "not_applicable",
                "inline_reply": "@rojobad not applicable here",
                "summary": "na",
            },
            {
                "thread_id": thread_ids[1],
                "decision": "uncertain",
                "inline_reply": "@rojobad need more context",
                "summary": "unc",
            },
        ],
        "all_actionable": False,
        "review_markdown": "mixed\n",
        "cursor_fix_prompt": None,
        "tests_status": "not_applicable",
        "summary": "needs attention",
        "residual_risk_comment": None,
        "highest_severity": None,
    }


@contextmanager
def _mock_remote_pr(
    pr: GithubPullRequest,
    threads: list[GithubReviewThread],
    *,
    repo: Path | None = None,
) -> Iterator[None]:
    if repo is not None:
        from ai_dev_loop.config import load_project_config

        fake_config = load_project_config(repo / "ai_dev_loop.yaml")
    else:
        fake_config = MagicMock()
        fake_config.github.command = "gh"
        fake_config.github.reviewer_logins = ["chatgpt-codex-connector"]
        fake_config.github.poll_timeout_hours = 1
        fake_config.github.poll_interval_seconds = 1
        fake_config.github.user_mention = "rojobad"
        fake_config.github.external_review_skill = "review-staged-cursor-execution"
        fake_config.github.acknowledgement.enabled = False
        fake_config.github.no_findings_completion.enabled = False
    with (
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=fake_config),
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
            "ai_dev_loop.commands.pr_review_independent.validate_independent_pre_cursor_baseline",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
    ):
        yield


def _write_cycle(
    run_path: Path,
    *,
    cycle: int,
    payload: dict,
    write_prompt: bool = True,
) -> None:
    label = f"{cycle:02d}"
    cycle_dir = run_path / "github" / "cycles" / label
    cycle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cycle_dir / "result.json", payload, sensitive=True)
    atomic_write_json(
        cycle_dir / "threads.snapshot.json",
        {
            "threads": [
                {"thread_id": tid, "body_sha256": "a" * 64}
                for tid in payload["eligible_thread_ids"]
            ]
        },
        sensitive=True,
    )
    atomic_write_text(cycle_dir / "report.md", str(payload["review_markdown"]), sensitive=True)
    if write_prompt and payload.get("cursor_fix_prompt"):
        prompt_path = run_path / "prompts" / "fixes" / f"github-{label}.txt"
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(prompt_path, str(payload["cursor_fix_prompt"]), sensitive=True)


def _seed_crypto_sentinel_fixture(
    prepared_run: dict[str, Path | str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, str]:
    """Interrupted cycle-5 with github-04 pointers and github-03 recovery lineage."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)

    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id
    chat_id = state.cursor.chat_id
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    _write_cycle(
        run_path,
        cycle=3,
        payload=_actionable_result([THREAD_C3], PROMPT_03),
    )
    _write_cycle(
        run_path,
        cycle=4,
        payload=_actionable_result([THREAD_C4A, THREAD_C4B], PROMPT_04),
    )
    _write_cycle(
        run_path,
        cycle=5,
        payload=_actionable_result([THREAD_C5A, THREAD_C5B], PROMPT_05),
    )

    state.status = RunStatus.INTERRUPTED
    state.last_error = "GitHub GraphQL request timed out"
    state.result = None
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.workflow.current_review_iteration = 4
    state.workflow.local_review_count = 0
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    state.recovery = RecoveryState(
        source_run_id="prior-failed-external-03",
        source_status=RunStatus.FAILED.value,
        source_iteration=3,
        recovered_checkpoint="external_feedback_cursor",
        source_staged_patch_sha256=None,
        created_at=utc_now(),
        runtime_migration="none",
        reason_code="external_feedback_cursor_not_started",
        expected_eligible_thread_ids=[THREAD_C3],
        source_prompt_path="prompts/fixes/github-03.txt",
        source_prompt_sha256=sha256_file(run_path / "prompts/fixes/github-03.txt"),
    )
    state.github_pr_review = GithubPrReviewState(
        origin="independent_pr",
        lifecycle="interrupted",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
        eligible_thread_ids=[THREAD_C5A, THREAD_C5B],
        expected_eligible_thread_ids=[THREAD_C5A, THREAD_C5B],
        processed_thread_ids=[THREAD_C3, THREAD_C4A, THREAD_C4B],
        resolved_thread_ids=[THREAD_C3, THREAD_C4A, THREAD_C4B],
        last_external_result_path="github/cycles/04/result.json",
        last_snapshot_path="github/cycles/04/threads.snapshot.json",
        external_fix_prompt_path="prompts/fixes/github-04.txt",
        external_cursor_iteration=4,
        worker_outcome="timeout",
        external_adjudication=None,
    )
    save_run_state(run_path, state)
    return run_path, state.run_id, chat_id


def test_resume_hydrates_github_05_despite_stale_pointers_and_old_recovery(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id = _seed_crypto_sentinel_fixture(prepared_run, monkeypatch)
    state = load_run_state(run_path / "state.json")
    bound_sha = state.repository.initial_head
    branch = state.repository.branch
    pr = _open_pr(branch, head_sha=bound_sha)
    threads = [_thread(THREAD_C5A, commit_sha=bound_sha), _thread(THREAD_C5B, commit_sha=bound_sha)]

    cursor_prompts: list[str] = []
    codex_calls: list[str] = []

    def fake_resume(run_id_arg: str) -> None:
        planned = load_run_state(run_path / "state.json")
        action = plan_next_action(planned, run_path)
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        assert action.iteration_number == 2
        assert is_external_cursor_prompt_iteration(planned, 2)
        cursor_prompts.append(read_cursor_prompt(planned, run_path, 2))

    repo = Path(str(prepared_run["repo"]))
    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fake_resume),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=lambda *a, **k: codex_calls.append("called"),
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
        patch("ai_dev_loop.commands.pr_review.resolve_review_thread") as resolve,
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker") as spawn,
        patch("ai_dev_loop.commands.pr_review_recover.recover_pr_review_cycle") as recover,
    ):
        message = resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)

    assert "Resumed local fix loop" in message
    final = load_run_state(run_path / "state.json")
    assert final.cursor.chat_id == chat_id
    assert final.github_pr_review is not None
    assert final.github_pr_review.last_external_result_path == "github/cycles/05/result.json"
    assert final.github_pr_review.external_fix_prompt_path == "prompts/fixes/github-05.txt"
    assert final.github_pr_review.external_cursor_iteration == 2
    checkpoint = final.github_pr_review.external_adjudication
    assert checkpoint is not None
    assert checkpoint.cycle_number == 5
    assert checkpoint.application_status == "cursor_scheduled"
    assert checkpoint.fix_prompt_sha256 == sha256_text(PROMPT_05)
    assert cursor_prompts == [PROMPT_05]
    assert codex_calls == []
    post.assert_not_called()
    reply.assert_not_called()
    resolve.assert_not_called()
    spawn.assert_not_called()
    recover.assert_not_called()
    # Stale recovery lineage remains for audit but is not the operational guard.
    assert final.recovery is not None
    assert final.recovery.source_prompt_path == "prompts/fixes/github-03.txt"


def test_graphql_timeout_after_adjudication_keeps_checkpoint_for_resume(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    state.github_pr_review = GithubPrReviewState(
        origin="independent_pr",
        lifecycle="awaiting_bot_review",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
        processed_thread_ids=[THREAD_C4A, THREAD_C4B],
        resolved_thread_ids=[THREAD_C4A, THREAD_C4B],
        last_external_result_path="github/cycles/04/result.json",
        external_fix_prompt_path="prompts/fixes/github-04.txt",
        external_cursor_iteration=4,
    )
    save_run_state(run_path, state)

    payload = _actionable_result(thread_ids, PROMPT_05)
    review = GithubPrReviewResult.model_validate(payload)
    _write_cycle(run_path, cycle=5, payload=payload)
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/05/codex.events.jsonl",
        stderr_path="github/cycles/05/codex.stderr.txt",
        result_path="github/cycles/05/result.json",
        report_path="github/cycles/05/report.md",
        metadata_path="github/cycles/05/codex.metadata.json",
        snapshot_path="github/cycles/05/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-05.txt",
    )
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    adjudication_calls = {"count": 0}

    def fake_adjudicate(*_args, **_kwargs):
        adjudication_calls["count"] += 1
        return review, artifacts

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=fake_adjudicate,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.validate_independent_pre_cursor_baseline",
            side_effect=AiDevLoopError("GitHub GraphQL request timed out"),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
    ):
        run_pr_review_worker_loop(state.run_id)

    after_timeout = load_run_state(run_path / "state.json")
    assert after_timeout.status == RunStatus.INTERRUPTED
    assert after_timeout.github_pr_review is not None
    checkpoint = after_timeout.github_pr_review.external_adjudication
    assert checkpoint is not None
    assert checkpoint.cycle_number == 5
    assert checkpoint.application_status == "cursor_pending"
    assert after_timeout.github_pr_review.last_external_result_path == (
        "github/cycles/05/result.json"
    )
    assert adjudication_calls["count"] == 1

    cursor_prompts: list[str] = []

    def fake_resume(_run_id: str) -> None:
        planned = load_run_state(run_path / "state.json")
        action = plan_next_action(planned, run_path)
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        cursor_prompts.append(read_cursor_prompt(planned, run_path, action.iteration_number))

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fake_resume),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=AssertionError("must not re-adjudicate"),
        ),
    ):
        resume_pr_review_cycle(state.run_id, controller_session_id=CONTROLLER_A)

    assert adjudication_calls["count"] == 1
    assert cursor_prompts == [PROMPT_05]
    resumed = load_run_state(run_path / "state.json")
    assert resumed.github_pr_review is not None
    assert resumed.github_pr_review.external_adjudication is not None
    assert resumed.github_pr_review.external_adjudication.application_status == ("cursor_scheduled")


def test_non_actionable_reply_intent_avoids_duplicates_on_ambiguous_write(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    payload = _non_actionable_result(thread_ids)
    _write_cycle(run_path, cycle=5, payload=payload, write_prompt=False)
    review = GithubPrReviewResult.model_validate(payload)
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/05/codex.events.jsonl",
        stderr_path="github/cycles/05/codex.stderr.txt",
        result_path="github/cycles/05/result.json",
        report_path="github/cycles/05/report.md",
        metadata_path="github/cycles/05/codex.metadata.json",
        snapshot_path="github/cycles/05/threads.snapshot.json",
        fix_prompt_path=None,
    )
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    reply_calls: list[str] = []

    def flaky_reply(*_args, **kwargs):
        thread_id = kwargs["pull_request_review_thread_id"]
        reply_calls.append(thread_id)
        if len(reply_calls) == 1:
            return GithubWriteResult(ok=True, resource_id="RC_1", error=None)
        raise AiDevLoopError("GitHub GraphQL request timed out")

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            return_value=(review, artifacts),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=flaky_reply,
        ),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
    ):
        run_pr_review_worker_loop(state.run_id)

    after = load_run_state(run_path / "state.json")
    assert after.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert after.github_pr_review is not None
    checkpoint = after.github_pr_review.external_adjudication
    assert checkpoint is not None
    assert checkpoint.application_status == "replies_pending"
    statuses = {item.thread_id: item.status for item in checkpoint.reply_intents}
    assert statuses[THREAD_C5A] == "written"
    assert statuses[THREAD_C5B] == "ambiguous"
    assert THREAD_C5A in after.github_pr_review.replied_thread_ids
    assert THREAD_C5B not in after.github_pr_review.replied_thread_ids
    assert reply_calls == [THREAD_C5A, THREAD_C5B]
    workflow.assert_not_called()

    # Resume must not duplicate the written reply or invent Cursor/publication.
    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=AssertionError("must not duplicate reply"),
        ) as reply,
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow2,
    ):
        message = resume_pr_review_cycle(state.run_id, controller_session_id=CONTROLLER_A)
    assert "non-actionable" in message.lower() or "Applied" in message
    reply.assert_not_called()
    workflow2.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION


def test_worker_continues_publication_after_resume_run(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    payload = _actionable_result(thread_ids, PROMPT_05)
    _write_cycle(run_path, cycle=5, payload=payload)
    review = GithubPrReviewResult.model_validate(payload)
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/05/codex.events.jsonl",
        stderr_path="github/cycles/05/codex.stderr.txt",
        result_path="github/cycles/05/result.json",
        report_path="github/cycles/05/report.md",
        metadata_path="github/cycles/05/codex.metadata.json",
        snapshot_path="github/cycles/05/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-05.txt",
    )
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
        processed_thread_ids=[THREAD_C4A, THREAD_C4B],
        resolved_thread_ids=[THREAD_C4A, THREAD_C4B],
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    publish_calls: list[bool] = []
    spawn_calls: list[str] = []

    def fake_resume(_run_id: str) -> None:
        current = load_run_state(run_path / "state.json")
        assert current.github_pr_review is not None
        current.status = RunStatus.PUBLISHING_EXTERNAL_FIX
        current.github_pr_review = current.github_pr_review.model_copy(
            update={
                "lifecycle": "publishing_external_fix",
                "publication_phase": "pre_commit",
                "eligible_thread_ids": thread_ids,
                "expected_eligible_thread_ids": thread_ids,
            }
        )
        save_run_state(run_path, current)

    def fake_publish(_run_directory, _state, _config, *, schedule_worker: bool = True) -> None:
        publish_calls.append(schedule_worker)
        current = load_run_state(run_path / "state.json")
        assert current.github_pr_review is not None
        # Leave awaiting_bot_review so the worker loop continues in-process once,
        # then the next poll iteration sees no eligible threads and exits via deadline.
        current.status = RunStatus.AWAITING_BOT_REVIEW
        current.github_pr_review = current.github_pr_review.model_copy(
            update={
                "lifecycle": "awaiting_bot_review",
                "publication_phase": None,
                "cycle_number": 6,
                "external_adjudication": None,
                "expected_eligible_thread_ids": None,
                "eligible_thread_ids": [],
                "processed_thread_ids": list(
                    dict.fromkeys(list(current.github_pr_review.processed_thread_ids) + thread_ids)
                ),
            }
        )
        current.result = "Published fix; awaiting cycle 6 Codex-bot review"
        save_run_state(run_path, current)

    thread_calls = {"n": 0}
    monotonic_calls = {"n": 0}

    def fake_list_threads(*_a, **_k):
        thread_calls["n"] += 1
        return threads if thread_calls["n"] == 1 else []

    def fake_monotonic() -> float:
        monotonic_calls["n"] += 1
        # First few ticks keep the loop alive through adjudication/publish;
        # later ticks expire the poll deadline.
        return 0.0 if monotonic_calls["n"] < 8 else 10_000.0

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            return_value=(review, artifacts),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fake_resume),
        patch(
            "ai_dev_loop.commands.pr_review._publish_external_fix",
            side_effect=fake_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append("spawn"),
        ),
        patch("ai_dev_loop.commands.pr_review.time.sleep", return_value=None),
        patch(
            "ai_dev_loop.commands.pr_review.time.monotonic",
            side_effect=fake_monotonic,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            side_effect=fake_list_threads,
        ),
    ):
        _run_pr_review_worker_loop_inner(state.run_id, run_path)

    assert publish_calls == [False]
    assert spawn_calls == []
    final = load_run_state(run_path / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.cycle_number == 6
    assert final.github_pr_review.external_adjudication is None


def test_controller_driven_publication_still_spawns_once(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    state.status = RunStatus.INTERRUPTED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="interrupted",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
        publication_phase="pre_commit",
        publication_text_path="github/cycles/05/publication-text.json",
        eligible_thread_ids=[THREAD_C5A, THREAD_C5B],
        expected_eligible_thread_ids=[THREAD_C5A, THREAD_C5B],
        staged_patch_sha256="a" * 64,
    )
    atomic_write_json(
        run_path / "github/cycles/05/publication-text.json",
        {
            "commit_subject": "Subject",
            "commit_body": "Body",
            "pr_title": "Title",
            "pr_body": "PR body",
        },
        sensitive=True,
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C5A, THREAD_C5B)]
    spawn_calls: list[str] = []

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append("spawn"),
        ),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
    ):
        message = resume_pr_review_cycle(state.run_id, controller_session_id=CONTROLLER_A)

    assert "Resumed publication" in message
    assert spawn_calls == ["spawn"]
    workflow.assert_not_called()


def test_old_recovery_hash_does_not_block_later_cycle_prompt(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat = _seed_crypto_sentinel_fixture(prepared_run, monkeypatch)
    state = load_run_state(run_path / "state.json")
    # Simulate a later cycle where recovery lineage is still present but the
    # operational prompt is github-05 via a durable checkpoint.
    from ai_dev_loop.external_adjudication import build_external_adjudication_checkpoint

    review = GithubPrReviewResult.model_validate(
        _actionable_result([THREAD_C5A, THREAD_C5B], PROMPT_05)
    )
    checkpoint = build_external_adjudication_checkpoint(
        run_path,
        review,
        cycle_number=5,
        bound_head_sha=state.repository.initial_head,
        eligible_thread_ids=[THREAD_C5A, THREAD_C5B],
        application_status="cursor_pending",
    )
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "external_adjudication": checkpoint,
            "last_external_result_path": checkpoint.result_path,
            "external_fix_prompt_path": checkpoint.fix_prompt_path,
            "external_cursor_iteration": None,
        }
    )
    save_run_state(run_path, state)
    # Old recovery hash must not equal github-05; scoped guard uses checkpoint.
    assert state.recovery is not None
    assert state.recovery.source_prompt_sha256 != checkpoint.fix_prompt_sha256

    pr = _open_pr(state.repository.branch, head_sha=state.repository.initial_head)
    threads = [
        _thread(THREAD_C5A, commit_sha=state.repository.initial_head),
        _thread(THREAD_C5B, commit_sha=state.repository.initial_head),
    ]
    repo = Path(str(prepared_run["repo"]))
    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch("ai_dev_loop.workflow_engine.resume_run"),
    ):
        resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)

    final = load_run_state(run_path / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.external_fix_prompt_path == "prompts/fixes/github-05.txt"
    assert final.status == RunStatus.RUNNING_CURSOR


def _seed_cursor_scheduled_independent(
    prepared_run: dict[str, Path | str],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, str, list[str]]:
    """Interrupted independent run with durable cursor_scheduled checkpoint."""

    run_path, run_id, chat_id = _seed_crypto_sentinel_fixture(prepared_run, monkeypatch)
    state = load_run_state(run_path / "state.json")
    thread_ids = [THREAD_C5A, THREAD_C5B]
    review = GithubPrReviewResult.model_validate(_actionable_result(thread_ids, PROMPT_05))
    checkpoint = mark_checkpoint_status(
        build_external_adjudication_checkpoint(
            run_path,
            review,
            cycle_number=5,
            bound_head_sha=state.repository.initial_head,
            eligible_thread_ids=thread_ids,
            application_status="cursor_pending",
        ),
        "cursor_scheduled",
    )
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "fixing_external_feedback",
            "external_adjudication": checkpoint,
            "last_external_result_path": checkpoint.result_path,
            "last_snapshot_path": checkpoint.snapshot_path,
            "external_fix_prompt_path": checkpoint.fix_prompt_path,
            "external_cursor_iteration": 2,
            "eligible_thread_ids": thread_ids,
            "expected_eligible_thread_ids": thread_ids,
            "worker_outcome": None,
        }
    )
    state.status = RunStatus.INTERRUPTED
    state.last_error = "simulated restart before Cursor"
    save_run_state(run_path, state)
    return run_path, run_id, chat_id, thread_ids


@contextmanager
def _mock_remote_pr_real_independent_preflight(
    pr: GithubPullRequest,
    threads: list[GithubReviewThread],
    *,
    repo: Path,
) -> Iterator[None]:
    """Like ``_mock_remote_pr`` but runs real independent binding/baseline preflight."""

    from ai_dev_loop.config import load_project_config

    fake_config = load_project_config(repo / "ai_dev_loop.yaml")
    with (
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review_independent.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=fake_config),
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
            "ai_dev_loop.commands.pr_review.list_issue_comment_reactions",
            return_value=[],
        ),
    ):
        yield


def test_cursor_scheduled_resume_blocks_closed_pr_without_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, thread_ids = _seed_cursor_scheduled_independent(
        prepared_run, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    bound_sha = state.repository.initial_head
    closed = GithubPullRequest(
        number=45,
        url="https://example.test/pr/45",
        title="t",
        state="CLOSED",
        head_ref=state.repository.branch,
        head_sha=bound_sha,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    repo = Path(str(prepared_run["repo"]))

    with (
        _mock_remote_pr_real_independent_preflight(closed, threads, repo=repo),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
    ):
        message = resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)

    assert "preflight blocked" in message.lower() or "preserved" in message.lower()
    workflow.assert_not_called()
    post.assert_not_called()
    reply.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome == "pre_cursor_binding_drift"
    assert final.github_pr_review.external_adjudication is not None
    assert final.github_pr_review.external_adjudication.application_status == "cursor_scheduled"


def test_cursor_scheduled_resume_blocks_head_sha_drift_without_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, thread_ids = _seed_cursor_scheduled_independent(
        prepared_run, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    bound_sha = state.repository.initial_head
    drifted = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    assert drifted != bound_sha
    pr = _open_pr(state.repository.branch, head_sha=drifted)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    repo = Path(str(prepared_run["repo"]))

    with (
        _mock_remote_pr_real_independent_preflight(pr, threads, repo=repo),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
    ):
        message = resume_pr_review_cycle(run_id, controller_session_id=CONTROLLER_A)

    assert "preflight blocked" in message.lower() or "preserved" in message.lower()
    workflow.assert_not_called()
    post.assert_not_called()
    reply.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.INTERRUPTED
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome == "pre_cursor_binding_drift"


def test_inline_reply_timeout_return_marks_ambiguous_without_retry(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    payload = _non_actionable_result(thread_ids)
    _write_cycle(run_path, cycle=5, payload=payload, write_prompt=False)
    review = GithubPrReviewResult.model_validate(payload)
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/05/codex.events.jsonl",
        stderr_path="github/cycles/05/codex.stderr.txt",
        result_path="github/cycles/05/result.json",
        report_path="github/cycles/05/report.md",
        metadata_path="github/cycles/05/codex.metadata.json",
        snapshot_path="github/cycles/05/threads.snapshot.json",
        fix_prompt_path=None,
    )
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="awaiting_bot_review",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]
    reply_calls: list[str] = []

    def timeout_reply(*_args, **kwargs):
        reply_calls.append(kwargs["pull_request_review_thread_id"])
        return GithubWriteResult(
            ok=False,
            resource_id=None,
            error=GithubError(GithubErrorKind.TIMEOUT, "gh command timed out"),
        )

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            return_value=(review, artifacts),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=timeout_reply,
        ),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
    ):
        run_pr_review_worker_loop(state.run_id)

    after = load_run_state(run_path / "state.json")
    assert after.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert after.github_pr_review is not None
    checkpoint = after.github_pr_review.external_adjudication
    assert checkpoint is not None
    statuses = {item.thread_id: item.status for item in checkpoint.reply_intents}
    assert statuses[THREAD_C5A] == "ambiguous"
    assert reply_calls == [THREAD_C5A]
    workflow.assert_not_called()

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=AssertionError("must not retry ambiguous reply"),
        ),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow2,
    ):
        resume_pr_review_cycle(state.run_id, controller_session_id=CONTROLLER_A)
    workflow2.assert_not_called()


def test_inline_reply_writing_boundary_does_not_retry(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-write and post-write interruptions leave ``writing``; resume parks ambiguous."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    payload = _non_actionable_result(thread_ids)
    _write_cycle(run_path, cycle=5, payload=payload, write_prompt=False)
    review = GithubPrReviewResult.model_validate(payload)
    checkpoint = build_external_adjudication_checkpoint(
        run_path,
        review,
        cycle_number=5,
        bound_head_sha=bound_sha,
        eligible_thread_ids=thread_ids,
        application_status="replies_pending",
    )
    # Simulate crash after durable ``writing`` and before confirmed ``written``.
    interrupted_intents = [
        ExternalReplyIntent(
            thread_id=THREAD_C5A,
            decision="not_applicable",
            inline_reply_sha256=checkpoint.reply_intents[0].inline_reply_sha256,
            status="writing",
        ),
        checkpoint.reply_intents[1],
    ]
    checkpoint = mark_checkpoint_status(
        checkpoint, "replies_pending", reply_intents=interrupted_intents
    )
    state.status = RunStatus.INTERRUPTED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="interrupted",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
        eligible_thread_ids=thread_ids,
        expected_eligible_thread_ids=thread_ids,
        last_external_result_path=checkpoint.result_path,
        last_snapshot_path=checkpoint.snapshot_path,
        external_adjudication=checkpoint,
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=AssertionError("must not retry writing intent"),
        ) as reply,
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
    ):
        resume_pr_review_cycle(state.run_id, controller_session_id=CONTROLLER_A)

    reply.assert_not_called()
    workflow.assert_not_called()
    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    statuses = {
        item.thread_id: item.status
        for item in final.github_pr_review.external_adjudication.reply_intents  # type: ignore[union-attr]
    }
    assert statuses[THREAD_C5A] == "ambiguous"
    assert statuses[THREAD_C5B] == "pending"


def test_missing_or_empty_snapshot_cannot_schedule_cursor_or_github_writes(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    bound_sha = _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.repository = state.repository.model_copy(update={"initial_head": bound_sha})
    thread_ids = [THREAD_C5A, THREAD_C5B]
    payload = _actionable_result(thread_ids, PROMPT_05)
    review = GithubPrReviewResult.model_validate(payload)
    cycle_dir = run_path / "github" / "cycles" / "05"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(cycle_dir / "result.json", payload, sensitive=True)
    atomic_write_text(cycle_dir / "report.md", str(payload["review_markdown"]), sensitive=True)
    prompt_path = run_path / "prompts" / "fixes" / "github-05.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(prompt_path, PROMPT_05, sensitive=True)
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/05/codex.events.jsonl",
        stderr_path="github/cycles/05/codex.stderr.txt",
        result_path="github/cycles/05/result.json",
        report_path="github/cycles/05/report.md",
        metadata_path="github/cycles/05/codex.metadata.json",
        snapshot_path="github/cycles/05/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-05.txt",
    )
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.iterations = [entry for entry in state.iterations if entry.get("number") == 1]
    state.github_pr_review = GithubPrReviewState(
        origin="independent_pr",
        lifecycle="awaiting_bot_review",
        cycle_number=5,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9005",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:5:{bound_sha}",
        request_created_at="2026-07-19T02:10:00+00:00",
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in thread_ids]

    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            return_value=(review, artifacts),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
        pytest.raises(ValidationError, match="snapshot missing"),
    ):
        run_pr_review_worker_loop(state.run_id)

    workflow.assert_not_called()
    post.assert_not_called()
    reply.assert_not_called()

    # Fresh worker cycle for the empty-snapshot fail-closed path.
    (cycle_dir / "threads.snapshot.json").write_text("[]\n", encoding="utf-8")
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    state.last_error = None
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "external_adjudication": None,
            "worker_outcome": None,
        }
    )
    save_run_state(run_path, state)
    with (
        _mock_remote_pr(pr, threads, repo=repo),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            return_value=(review, artifacts),
        ),
        patch("ai_dev_loop.commands.pr_review._load_thread_bodies", return_value=[]),
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow2,
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post2,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply2,
        pytest.raises(ValidationError, match="non-empty threads list"),
    ):
        run_pr_review_worker_loop(state.run_id)

    workflow2.assert_not_called()
    post2.assert_not_called()
    reply2.assert_not_called()
