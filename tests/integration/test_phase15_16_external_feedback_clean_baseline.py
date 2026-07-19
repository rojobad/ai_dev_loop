"""Phase 15.16: clean external Cursor baseline and pre-Cursor recovery."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_loop.commands.pr_review import (
    _publish_external_fix,
    resume_pr_review_cycle,
)
from ai_dev_loop.commands.pr_review_recover import (
    analyze_pr_review_recovery,
    recover_pr_review_cycle,
)
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import (
    begin_external_local_review_budget,
    cursor_prompt_path,
    is_external_cursor_prompt_iteration,
    read_cursor_prompt,
)
from ai_dev_loop.resume_planner import WorkflowActionKind, plan_next_action
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread, GithubWriteResult
from ai_dev_loop.runners.publish import (
    PublicationText,
    PublishResult,
    commit_staged_patch,
    read_staged_patch,
)
from ai_dev_loop.state import (
    ControllerState,
    GithubPrReviewState,
    RunStatus,
    load_run_state,
    save_run_state,
    sha256_file,
    sha256_text,
)
from ai_dev_loop.workflow_engine import _apply_review_result, _run_cursor_turn

CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_C2A = "PRRT_CYCLE2_A"
THREAD_C2B = "PRRT_CYCLE2_B"
THREAD_C3A = "PRRT_CYCLE3_A"
THREAD_C3B = "PRRT_CYCLE3_B"
THREAD_C4A = "PRRT_CYCLE4_A"
EXTERNAL_PROMPT_C3 = "Please fix both cycle-3 actionable threads.\n"
EXTERNAL_PROMPT_C4 = "Please fix the cycle-4 actionable thread.\n"


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
        created_at="2026-07-19T00:30:00+00:00",
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


def _seed_incident_empty_dir_03(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, str, str, str]:
    """CryptoSentinel-shaped source: published 01/02, cycle 3, empty cursor/03."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)

    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id
    chat_id = state.cursor.chat_id
    bound_sha = state.repository.initial_head

    # Historical published patch that must not gate the next external Cursor turn.
    (run_path / "git/diffs").mkdir(parents=True, exist_ok=True)
    (run_path / "codex/reviews").mkdir(parents=True, exist_ok=True)
    accepted_review = {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "cursor_fix_prompt": None,
        "review_markdown": "ok",
        "tests_status": "not_applicable",
        "summary": "accepted",
    }
    for label, body in (("01", "one"), ("02", "two")):
        (run_path / f"git/diffs/{label}.patch").write_text(
            f"diff --git a/{body}.txt b/{body}.txt\n+{body}\n", encoding="utf-8"
        )
        cursor_iter = run_path / "cursor" / "iterations" / label
        cursor_iter.mkdir(parents=True, exist_ok=True)
        (cursor_iter / "metadata.json").write_text(
            '{"exit_code": 0, "timed_out": false}\n', encoding="utf-8"
        )
        (run_path / f"codex/reviews/{label}.json").write_text(
            json.dumps(accepted_review, indent=2) + "\n", encoding="utf-8"
        )

    # Empty directory created before the buggy preflight — not execution evidence.
    (run_path / "cursor" / "iterations" / "03").mkdir(parents=True, exist_ok=True)
    assert not any((run_path / "cursor" / "iterations" / "03").iterdir())

    thread_ids = [THREAD_C3A, THREAD_C3B]
    cycle_dir = run_path / "github/cycles/03"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    (cycle_dir / "result.json").write_text(
        json.dumps(_actionable_result(thread_ids, EXTERNAL_PROMPT_C3), indent=2) + "\n",
        encoding="utf-8",
    )
    (cycle_dir / "threads.snapshot.json").write_text("[]\n", encoding="utf-8")
    fix_prompt = run_path / "prompts/fixes/github-03.txt"
    fix_prompt.parent.mkdir(parents=True, exist_ok=True)
    fix_prompt.write_text(EXTERNAL_PROMPT_C3, encoding="utf-8")

    state.status = RunStatus.FAILED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.workflow.current_review_iteration = 2
    state.workflow.local_review_count = 0
    state.result = "staged patch no longer matches recorded artifact"
    state.last_error = "staged patch no longer matches recorded artifact"
    state.iterations = [
        {
            "number": 1,
            "kind": "initial_implementation",
            "cursor": {"prompt_path": "prompts/cursor-initial.txt"},
            "git": {"staged_diff_path": "git/diffs/01.patch"},
        },
        {
            "number": 2,
            "kind": "cursor_correction",
            "cursor": {"prompt_path": "prompts/fixes/github-02.txt"},
            "git": {"staged_diff_path": "git/diffs/02.patch"},
        },
    ]
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=3,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9003",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:3:{bound_sha}",
        request_created_at="2026-07-19T00:21:00+00:00",
        eligible_thread_ids=thread_ids,
        expected_eligible_thread_ids=thread_ids,
        processed_thread_ids=[THREAD_C2A, THREAD_C2B],
        resolved_thread_ids=[THREAD_C2A, THREAD_C2B],
        last_external_result_path="github/cycles/03/result.json",
        last_snapshot_path="github/cycles/03/threads.snapshot.json",
        external_fix_prompt_path="prompts/fixes/github-03.txt",
        external_cursor_iteration=3,
        worker_outcome=None,
    )
    save_run_state(run_path, state)
    return run_path, state.run_id, chat_id, bound_sha


def test_empty_iteration_dir_is_recoverable_external_feedback_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id, bound_sha = _seed_incident_empty_dir_03(
        prepared_run, fake_clis, monkeypatch
    )
    source_before = (run_path / "state.json").read_bytes()
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C3A, THREAD_C3B)]

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
    assert dry.analysis.eligible
    assert dry.analysis.checkpoint == "external_feedback_cursor"
    assert dry.analysis.reason_code == "external_feedback_cursor_not_started"
    assert dry.analysis.iteration == 3
    assert dry.recovery_run_id is None
    assert (run_path / "state.json").read_bytes() == source_before
    # Must not misclassify as reviewing because of the empty directory.
    assert dry.analysis.checkpoint != "reviewing"

    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    assert (run_path / "state.json").read_bytes() == source_before

    successor_dir = find_run_directory(created.recovery_run_id)
    assert successor_dir is not None
    successor = load_run_state(successor_dir / "state.json")
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "external_feedback_cursor"
    assert successor.recovery.source_iteration == 3
    assert successor.cursor.chat_id == chat_id
    assert successor.codex.session_id == REVIEWER_B
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.external_cursor_iteration == 3
    assert not (successor_dir / "cursor" / "iterations" / "03").exists()

    with _mock_remote_pr(pr, threads):
        reused = recover_pr_review_cycle(run_id, dry_run=False)
    assert reused.recovery_run_id == created.recovery_run_id
    assert reused.analysis.reused_existing_successor

    cursor_prompts: list[str] = []

    def fake_resume(run_id_arg: str) -> None:
        planned = load_run_state(successor_dir / "state.json")
        action = plan_next_action(planned, successor_dir)
        assert action is not None
        assert action.kind == WorkflowActionKind.CURSOR
        assert action.iteration_number == 3
        assert is_external_cursor_prompt_iteration(planned, 3)
        cursor_prompts.append(read_cursor_prompt(planned, successor_dir, 3))

    with (
        _mock_remote_pr(pr, threads),
        patch("ai_dev_loop.workflow_engine.resume_run", side_effect=fake_resume),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
        patch("ai_dev_loop.commands.pr_review.resolve_review_thread") as resolve,
    ):
        message = resume_pr_review_cycle(
            created.recovery_run_id,
            controller_session_id=CONTROLLER_A,
        )
    assert "Resumed local fix loop" in message
    assert cursor_prompts == [EXTERNAL_PROMPT_C3]
    post.assert_not_called()
    reply.assert_not_called()
    resolve.assert_not_called()


def test_recovery_rejects_real_partial_evidence_and_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha = _seed_incident_empty_dir_03(
        prepared_run, fake_clis, monkeypatch
    )
    repo = Path(str(prepared_run["repo"]))
    branch = load_run_state(run_path / "state.json").repository.branch
    pr = _open_pr(branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C3A, THREAD_C3B)]

    # Non-empty iteration directory is real partial evidence.
    (run_path / "cursor" / "iterations" / "03" / "stderr.txt").write_text("x", encoding="utf-8")
    with _mock_remote_pr(pr, threads):
        partial = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not partial.eligible or partial.checkpoint != "external_feedback_cursor"
    (run_path / "cursor" / "iterations" / "03" / "stderr.txt").unlink()

    # Durable iterations[03] entry.
    state = load_run_state(run_path / "state.json")
    state.iterations.append(
        {
            "number": 3,
            "kind": "cursor_correction",
            "cursor": {"prompt_path": "prompts/fixes/github-03.txt"},
        }
    )
    save_run_state(run_path, state)
    with _mock_remote_pr(pr, threads):
        durable = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not durable.eligible or durable.checkpoint != "external_feedback_cursor"
    state.iterations = [entry for entry in state.iterations if int(entry.get("number", 0)) < 3]
    save_run_state(run_path, state)

    dirty = repo / "dirty-for-recovery.txt"
    dirty.write_text("dirty\n", encoding="utf-8")
    with _mock_remote_pr(pr, threads):
        dirty_analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not dirty_analysis.eligible
    assert dirty_analysis.checkpoint == "external_feedback_cursor"
    assert "baseline_not_clean" in dirty_analysis.blockers
    dirty.unlink()

    drifted = _open_pr(branch, head_sha="d" * 40)
    with _mock_remote_pr(drifted, threads):
        sha_analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert not sha_analysis.eligible
    assert "head_sha_drift" in sha_analysis.blockers

    # Failed successor must not spawn a parallel active successor from the source.
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    assert (run_path / "state.json").read_bytes() == source_before
    successor_dir = find_run_directory(created.recovery_run_id)
    assert successor_dir is not None
    successor = load_run_state(successor_dir / "state.json")
    successor.status = RunStatus.FAILED
    save_run_state(successor_dir, successor)
    with (
        _mock_remote_pr(pr, threads),
        pytest.raises(ValidationError, match="itself failed"),
    ):
        recover_pr_review_cycle(run_id, dry_run=False)
    assert (run_path / "state.json").read_bytes() == source_before
    assert load_run_state(run_path / "state.json").status == RunStatus.FAILED


@pytest.mark.parametrize(
    "relative_artifact",
    [
        "git/status/03-before-staging.txt",
        "git/status/03-after-staging.txt",
        "git/diffs/03.stat",
        "git/diffs/03.name-only.txt",
        "git/cursor-output/03.post-normalization.json",
    ],
)
def test_recovery_rejects_staging_review_artifacts_as_partial_cursor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    relative_artifact: str,
) -> None:
    """Staging/review artifacts must block external_feedback_cursor recovery."""

    run_path, run_id, _chat, bound_sha = _seed_incident_empty_dir_03(
        prepared_run, fake_clis, monkeypatch
    )
    source_before = (run_path / "state.json").read_bytes()
    branch = load_run_state(run_path / "state.json").repository.branch
    pr = _open_pr(branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C3A, THREAD_C3B)]

    artifact = run_path / relative_artifact
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("partial\n", encoding="utf-8")

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
    assert dry.analysis.checkpoint != "external_feedback_cursor"
    assert dry.recovery_run_id is None
    assert (run_path / "state.json").read_bytes() == source_before

    with _mock_remote_pr(pr, threads):
        if dry.analysis.eligible:
            # Some other checkpoint may still be eligible; never create a
            # successor that can reopen the pending external Cursor turn.
            created = recover_pr_review_cycle(run_id, dry_run=False)
            if created.recovery_run_id is not None:
                successor_dir = find_run_directory(created.recovery_run_id)
                assert successor_dir is not None
                successor = load_run_state(successor_dir / "state.json")
                assert successor.recovery is not None
                assert successor.recovery.recovered_checkpoint != "external_feedback_cursor"
                assert successor.github_pr_review is not None
                assert successor.github_pr_review.external_cursor_iteration != 3
        else:
            with pytest.raises(ValidationError, match="pr-review recover rejected"):
                recover_pr_review_cycle(run_id, dry_run=False)
    assert (run_path / "state.json").read_bytes() == source_before
    assert load_run_state(run_path / "state.json").status == RunStatus.FAILED


def test_two_external_cycles_use_clean_baseline_not_published_patch(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production publication updates clean baseline; next external Cursor uses it."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)

    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id
    chat_id = state.cursor.chat_id
    bound_sha = state.repository.initial_head
    branch = state.repository.branch

    (run_path / "git/diffs").mkdir(parents=True, exist_ok=True)
    (run_path / "codex/reviews").mkdir(parents=True, exist_ok=True)
    accepted_review = {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "cursor_fix_prompt": None,
        "review_markdown": "ok",
        "tests_status": "not_applicable",
        "summary": "accepted",
    }
    for label, body in (("01", "one"), ("02", "two")):
        (run_path / f"git/diffs/{label}.patch").write_text(
            f"diff --git a/{body}.txt b/{body}.txt\n+{body}\n", encoding="utf-8"
        )
        cursor_iter = run_path / "cursor" / "iterations" / label
        cursor_iter.mkdir(parents=True, exist_ok=True)
        (cursor_iter / "metadata.json").write_text(
            '{"exit_code": 0, "timed_out": false}\n', encoding="utf-8"
        )
        (run_path / f"codex/reviews/{label}.json").write_text(
            json.dumps(accepted_review, indent=2) + "\n", encoding="utf-8"
        )

    fix_prompt = run_path / "prompts/fixes/github-03.txt"
    fix_prompt.parent.mkdir(parents=True, exist_ok=True)
    fix_prompt.write_text(EXTERNAL_PROMPT_C3, encoding="utf-8")
    (run_path / "github/cycles/03").mkdir(parents=True, exist_ok=True)
    (run_path / "github/cycles/03/result.json").write_text(
        json.dumps(_actionable_result([THREAD_C3A, THREAD_C3B], EXTERNAL_PROMPT_C3), indent=2)
        + "\n",
        encoding="utf-8",
    )
    (run_path / "github/cycles/03/publication-text.json").write_text(
        json.dumps(
            {
                "commit_subject": "Accept cycle-3 external fix",
                "commit_body": "Local review accepted.",
                "pr_title": "Accept cycle-3 external fix",
                "pr_body": "Publication text for cycle 3.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    state.status = RunStatus.RUNNING_CURSOR
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.workflow.current_review_iteration = 2
    begin_external_local_review_budget(state)
    state.iterations = [
        {
            "number": 1,
            "kind": "initial_implementation",
            "git": {"staged_diff_path": "git/diffs/01.patch"},
        },
        {
            "number": 2,
            "kind": "cursor_correction",
            "git": {"staged_diff_path": "git/diffs/02.patch"},
        },
    ]
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=3,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=branch,
        bound_head_sha=bound_sha,
        request_comment_id="9003",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:3:{bound_sha}",
        request_created_at="2026-07-19T00:21:00+00:00",
        eligible_thread_ids=[THREAD_C3A, THREAD_C3B],
        expected_eligible_thread_ids=[THREAD_C3A, THREAD_C3B],
        processed_thread_ids=[THREAD_C2A, THREAD_C2B],
        resolved_thread_ids=[THREAD_C2A, THREAD_C2B],
        last_external_result_path="github/cycles/03/result.json",
        external_fix_prompt_path="prompts/fixes/github-03.txt",
        external_cursor_iteration=3,
        publication_text_path="github/cycles/03/publication-text.json",
    )
    save_run_state(run_path, state)

    assert cursor_prompt_path(state, 3) == "prompts/fixes/github-03.txt"
    assert read_cursor_prompt(state, run_path, 3) == EXTERNAL_PROMPT_C3

    with patch(
        "ai_dev_loop.workflow_engine.validate_correction_pre_cursor",
        side_effect=AssertionError("must not compare published patch for external turn"),
    ):
        _run_cursor_turn(run_path, state, chat_id=chat_id, iteration_number=3)
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.STAGING
    assert state.cursor.chat_id == chat_id
    assert state.codex.session_id == REVIEWER_B

    # Deterministic staged fix for the accepted external turn.
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    (repo / "cycle3-fix.txt").write_text("cycle3\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "cycle3-fix.txt"], cwd=repo)
    live_patch = read_staged_patch(repo)
    live_hash = sha256_text(live_patch)
    (run_path / "git/diffs/03.patch").write_text(live_patch, encoding="utf-8")
    state.iterations = list(state.iterations or [])
    for entry in state.iterations:
        if int(entry.get("number", 0)) == 3:
            git_section = dict(entry.get("git") or {})
            git_section["staged_diff_path"] = "git/diffs/03.patch"
            entry["git"] = git_section
            break
    else:
        state.iterations.append(
            {
                "number": 3,
                "kind": "cursor_correction",
                "cursor": {"prompt_path": "prompts/fixes/github-03.txt"},
                "git": {"staged_diff_path": "git/diffs/03.patch"},
            }
        )
    state.status = RunStatus.REVIEWING
    state.workflow.current_review_iteration = 3
    save_run_state(run_path, state)

    accepted = CodexReviewResult.model_validate({**accepted_review, "tests_status": "passed"})
    publish_calls: list[dict[str, object]] = []
    resolve_calls: list[str] = []
    trigger_calls: list[str] = []
    push_force_flags: list[bool] = []

    def fake_publish(
        repo_root: Path,
        *,
        branch: str,
        text: PublicationText,
        resume_from_commit: str | None = None,
        expected_remote_sha_before_push: str | None = None,
        staged_patch_sha256: str | None = None,
        after_commit=None,
    ) -> PublishResult:
        assert resume_from_commit is None
        patch = read_staged_patch(repo_root)
        commit_sha = commit_staged_patch(
            repo_root, subject=text.commit_subject, body=text.commit_body
        )
        push_force_flags.append(False)  # production path is non-force only
        publish_calls.append(
            {
                "commit_sha": commit_sha,
                "branch": branch,
                "expected_remote": expected_remote_sha_before_push or bound_sha,
            }
        )
        if after_commit is not None:
            after_commit(commit_sha, bound_sha, "origin", branch)
        return PublishResult(
            commit_sha=commit_sha,
            remote_name="origin",
            remote_ref=f"origin/{branch}",
            staged_patch_sha256=sha256_text(patch),
            expected_remote_sha_before_push=bound_sha,
            resumed_existing_commit=False,
        )

    def fake_resolve(*_a, **kwargs):
        thread_id = kwargs["pull_request_review_thread_id"]
        resolve_calls.append(thread_id)
        return GithubWriteResult(ok=True, resource_id=f"R-{thread_id}")

    def fake_ensure_trigger(*_a, **kwargs):
        marker_cycle = kwargs["cycle_number"]
        trigger_calls.append(f"cycle:{marker_cycle}")
        return ("9004", "2026-07-19T01:00:00+00:00")

    config = MagicMock()
    config.github.command = "gh"
    config.github.reviewer_logins = ["chatgpt-codex-connector"]
    config.github.pr_base = "master"
    config.github.review_trigger_body = "@codex review"
    config.github.poll_timeout_hours = 1
    config.github.poll_interval_seconds = 1

    with (
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=fake_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            side_effect=fake_resolve,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._ensure_review_trigger_comment",
            side_effect=fake_ensure_trigger,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=AssertionError("trigger must go through _ensure_review_trigger_comment"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            side_effect=AssertionError("must reuse durable publication text"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            side_effect=lambda *_a, **_k: _open_pr(
                branch,
                head_sha=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=repo, text=True
                ).strip(),
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=[_thread(tid, commit_sha=bound_sha) for tid in (THREAD_C3A, THREAD_C3B)],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda items, **kwargs: list(items),
        ),
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=config),
    ):
        _apply_review_result(
            run_path,
            state,
            iteration_number=3,
            review=accepted,
            review_artifact_path="codex/reviews/03.json",
        )
        state = load_run_state(run_path / "state.json")
        assert state.status == RunStatus.PUBLISHING_EXTERNAL_FIX
        assert state.github_pr_review is not None
        assert state.github_pr_review.staged_patch_sha256 == live_hash
        _publish_external_fix(run_path, state, config, schedule_worker=False)

    assert len(publish_calls) == 1
    assert push_force_flags == [False]
    assert set(resolve_calls) == {THREAD_C3A, THREAD_C3B}
    assert trigger_calls == ["cycle:4"]

    published = load_run_state(run_path / "state.json")
    assert published.github_pr_review is not None
    assert published.status == RunStatus.AWAITING_BOT_REVIEW
    assert published.github_pr_review.lifecycle == "awaiting_bot_review"
    assert published.github_pr_review.cycle_number == 4
    new_head = published.repository.initial_head
    assert new_head == published.github_pr_review.bound_head_sha
    assert new_head != bound_sha
    assert new_head == publish_calls[0]["commit_sha"]
    assert published.cursor.chat_id == chat_id
    assert published.codex.session_id == REVIEWER_B
    # Clean published worktree for the next external Cursor baseline.
    subprocess.check_call(["git", "reset", "--hard", "HEAD"], cwd=repo)
    subprocess.check_call(["git", "clean", "-fd"], cwd=repo)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == new_head
    )

    # Next external round after adjudication uses the published clean commit.
    fix4 = run_path / "prompts/fixes/github-04.txt"
    fix4.write_text(EXTERNAL_PROMPT_C4, encoding="utf-8")
    (run_path / "github/cycles/04").mkdir(parents=True, exist_ok=True)
    (run_path / "github/cycles/04/result.json").write_text(
        json.dumps(_actionable_result([THREAD_C4A], EXTERNAL_PROMPT_C4), indent=2) + "\n",
        encoding="utf-8",
    )
    begin_external_local_review_budget(published)
    published.status = RunStatus.RUNNING_CURSOR
    published.workflow.current_review_iteration = max(
        int(entry.get("number", 0)) for entry in published.iterations
    )
    next_iteration = published.workflow.current_review_iteration + 1
    published.github_pr_review = published.github_pr_review.model_copy(
        update={
            "lifecycle": "fixing_external_feedback",
            "external_cursor_iteration": next_iteration,
            "external_fix_prompt_path": "prompts/fixes/github-04.txt",
            "last_external_result_path": "github/cycles/04/result.json",
            "eligible_thread_ids": [THREAD_C4A],
            "expected_eligible_thread_ids": [THREAD_C4A],
            "publication_phase": None,
        }
    )
    save_run_state(run_path, published)
    assert is_external_cursor_prompt_iteration(published, next_iteration)
    assert read_cursor_prompt(published, run_path, next_iteration) == EXTERNAL_PROMPT_C4

    with patch(
        "ai_dev_loop.workflow_engine.validate_correction_pre_cursor",
        side_effect=AssertionError("second external round must use clean baseline"),
    ):
        _run_cursor_turn(
            run_path,
            published,
            chat_id=chat_id,
            iteration_number=next_iteration,
        )

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.STAGING
    assert final.cursor.chat_id == chat_id
    assert final.codex.session_id == REVIEWER_B
    assert final.repository.initial_head == new_head
    assert final.github_pr_review is not None
    assert final.github_pr_review.bound_head_sha == new_head
    assert sha256_file(run_path / "prompts/fixes/github-04.txt") == sha256_text(EXTERNAL_PROMPT_C4)
    # No duplicate publication side effects for the published cycle.
    assert len(publish_calls) == 1
    assert len(trigger_calls) == 1
    assert set(resolve_calls) == {THREAD_C3A, THREAD_C3B}
