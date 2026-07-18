"""Phase 15.11: one-hop nested lineage for legacy external-cycle freeze recovery."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from ai_dev_loop.commands.pr_review import (
    _classify_legacy_external_cycle_freeze,
    continue_pr_review_cycle,
    run_pr_review_worker_loop,
)
from ai_dev_loop.config import load_project_config
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.launcher import read_process_starttime
from ai_dev_loop.paths import ensure_dir, run_dir
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread
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

CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
CURSOR_CHAT = "019abc00-1111-2222-3333-444444444444"
THREAD_C1 = "PRRT_CYCLE1_A"
THREAD_C1B = "PRRT_CYCLE1_B"
THREAD_C2A = "PRRT_CYCLE2_A"
THREAD_C2B = "PRRT_CYCLE2_B"
THREAD_C2C = "PRRT_CYCLE2_C"
HEAD_SHA = "c29e15e6608a1111222233334444555566667777"
CONTINUE_BODY = "@rojobad /ai-dev-loop continue"
SOURCE_RUN_ID = "crypto-sentinel-20260718T024655Z-ff42cb"
REPO_NWO = "acme/demo"


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


def _gh_comments(payload: list[dict]) -> ProcessResult:
    return ProcessResult(
        args=["gh", "api"],
        returncode=0,
        stdout=json.dumps(payload),
        stderr="",
        timed_out=False,
    )


def _seed_nested_lineage_runs(
    prepared_run: dict[str, Path | str],
    *,
    continue_comment_id: str | None = None,
    source_cycle: int = 1,
    current_cycle: int = 2,
    expected: list[str] | None = None,
    processed: list[str] | None = None,
    resolved: list[str] | None = None,
    source_status: RunStatus = RunStatus.FAILED,
    source_recovery_checkpoint: str = "external_adjudication",
) -> tuple[Path, Path, str]:
    """Seed terminal external source + reviewing successor with inherited freeze."""

    current_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    current = load_run_state(current_path / "state.json")
    project = current.project.name
    source_path = run_dir(project, SOURCE_RUN_ID)
    if source_path.exists():
        shutil.rmtree(source_path)
    ensure_dir(source_path)
    # Minimal durable source tree: copy current skeleton then overwrite state.
    for name in ("manifest.json", "effective-config.yaml", "source-config.yaml"):
        src = current_path / name
        if src.is_file():
            shutil.copy2(src, source_path / name)

    expected_ids = list(expected if expected is not None else [THREAD_C1, THREAD_C1B])
    processed_ids = list(processed if processed is not None else [THREAD_C1, THREAD_C1B])
    resolved_ids = list(resolved if resolved is not None else [THREAD_C1, THREAD_C1B])

    source = load_run_state(current_path / "state.json")
    source.run_id = SOURCE_RUN_ID
    source.status = source_status
    source.codex.session_id = REVIEWER_B
    source.controller = ControllerState(controller_session_id=CONTROLLER_A)
    source.cursor.chat_id = CURSOR_CHAT
    source_marker = f"ai_dev_loop-pr-review:{SOURCE_RUN_ID}:cycle:{source_cycle}:{HEAD_SHA}"
    if source_recovery_checkpoint == "external_adjudication":
        source.recovery = RecoveryState(
            source_run_id="original-failed-adjudication",
            source_status=RunStatus.FAILED.value,
            source_iteration=source_cycle,
            recovered_checkpoint="external_adjudication",
            created_at=utc_now(),
            runtime_migration="none",
            reason_code="github_adjudication_schema_incompatible",
            expected_eligible_thread_ids=list(expected_ids),
        )
    else:
        source.recovery = RecoveryState(
            source_run_id="original-failed-adjudication",
            source_status=RunStatus.FAILED.value,
            source_iteration=source_cycle,
            recovered_checkpoint=source_recovery_checkpoint,
            created_at=utc_now(),
            runtime_migration="none",
            reason_code="codex_review_result_artifact_missing",
            source_staged_patch_sha256="a" * 64,
        )
    source.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="interrupted",
        cycle_number=source_cycle,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner=REPO_NWO,
        head_branch=source.repository.branch,
        bound_head_sha=HEAD_SHA,
        request_comment_id="8001",
        request_marker=source_marker,
        request_created_at="2026-07-18T02:00:00+00:00",
        eligible_thread_ids=[],
        expected_eligible_thread_ids=list(expected_ids),
        processed_thread_ids=list(expected_ids),
        resolved_thread_ids=list(expected_ids),
        worker_outcome="adjudication_schema_incompatible",
    )
    source.result = "schema incompatible adjudication"
    source.last_error = "github adjudication schema incompatible"
    save_run_state(source_path, source)

    current.status = RunStatus.WAITING_FOR_USER_ATTENTION
    current.codex.session_id = REVIEWER_B
    current.controller = ControllerState(controller_session_id=CONTROLLER_A)
    current.cursor.chat_id = CURSOR_CHAT
    current_marker = f"ai_dev_loop-pr-review:{current.run_id}:cycle:{current_cycle}:{HEAD_SHA}"
    current.recovery = RecoveryState(
        source_run_id=SOURCE_RUN_ID,
        source_status=source_status.value,
        source_iteration=source_cycle,
        recovered_checkpoint="reviewing",
        created_at=utc_now(),
        runtime_migration="none",
        reason_code="codex_review_result_artifact_missing",
        source_staged_patch_sha256="b" * 64,
    )
    current.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="waiting_for_user_attention",
        cycle_number=current_cycle,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner=REPO_NWO,
        head_branch=current.repository.branch,
        bound_head_sha=HEAD_SHA,
        request_comment_id="9002",
        request_marker=current_marker,
        request_created_at="2026-07-18T12:00:00+00:00",
        eligible_thread_ids=[],
        expected_eligible_thread_ids=list(expected_ids),
        processed_thread_ids=processed_ids,
        resolved_thread_ids=resolved_ids,
        continue_comment_id=continue_comment_id,
        worker_outcome="eligible_thread_set_drift",
    )
    current.result = (
        "Eligible thread set no longer matches the frozen recovery set "
        "(expected=2, observed=3); no GitHub writes were performed"
    )
    current.last_error = (
        "Eligible GitHub review thread set drifted from the frozen recovery set; "
        "resolve drift before resuming"
    )
    save_run_state(current_path, current)
    return source_path, current_path, current.run_id


def test_nested_lineage_continue_clears_only_current_and_spawns_once(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    source_path, current_path, run_id = _seed_nested_lineage_runs(prepared_run)
    source_before = (source_path / "state.json").read_bytes()
    before = load_run_state(current_path / "state.json")
    assert before.github_pr_review is not None
    marker_before = before.github_pr_review.request_marker
    sha_before = before.github_pr_review.bound_head_sha
    request_before = before.github_pr_review.request_comment_id
    recovery_before = before.recovery.model_dump(mode="json") if before.recovery else None
    chat_before = before.cursor.chat_id
    session_before = before.codex.session_id
    spawn_calls: list[str] = []
    comments = [
        {"id": 9002, "user": {"login": "rojobad"}, "body": "trigger"},
        {"id": 9100, "user": {"login": "rojobad"}, "body": CONTINUE_BODY},
    ]

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_comments(comments),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append("spawn") or True,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
        patch("ai_dev_loop.commands.pr_review.reply_to_review_thread") as reply,
        patch("ai_dev_loop.commands.pr_review.resolve_review_thread") as resolve,
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review") as codex,
        patch("ai_dev_loop.workflow_engine.resume_run") as workflow,
    ):
        cfg.return_value = load_project_config(Path(str(prepared_run["repo"])) / "ai_dev_loop.yaml")
        message = continue_pr_review_cycle(run_id)

    assert "legacy cycle freeze cleared" in message.lower() or "Legacy" in message
    assert "already-published" in message
    assert "@codex review" in message
    assert spawn_calls == ["spawn"]
    post.assert_not_called()
    reply.assert_not_called()
    resolve.assert_not_called()
    codex.assert_not_called()
    workflow.assert_not_called()

    # Source terminal is immutable (byte-for-byte).
    assert (source_path / "state.json").read_bytes() == source_before

    after = load_run_state(current_path / "state.json")
    assert after.status == RunStatus.AWAITING_BOT_REVIEW
    assert after.github_pr_review is not None
    assert after.github_pr_review.lifecycle == "awaiting_bot_review"
    assert after.github_pr_review.expected_eligible_thread_ids is None
    assert after.github_pr_review.worker_outcome is None
    assert after.github_pr_review.cycle_number == 2
    assert after.github_pr_review.continue_comment_id == "9100"
    assert after.github_pr_review.request_marker == marker_before
    assert after.github_pr_review.bound_head_sha == sha_before
    assert after.github_pr_review.request_comment_id == request_before
    assert after.github_pr_review.pr_number == 45
    assert set(after.github_pr_review.processed_thread_ids) == {THREAD_C1, THREAD_C1B}
    assert set(after.github_pr_review.resolved_thread_ids) == {THREAD_C1, THREAD_C1B}
    assert after.cursor.chat_id == chat_before
    assert after.codex.session_id == session_before
    assert after.recovery is not None
    assert after.recovery.model_dump(mode="json") == recovery_before
    assert after.recovery.recovered_checkpoint == "reviewing"
    assert after.recovery.source_run_id == SOURCE_RUN_ID
    assert after.last_error is None

    events = (current_path / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "pr_review_legacy_cycle_freeze_cleared" in events
    event_line = next(
        line for line in events.splitlines() if "pr_review_legacy_cycle_freeze_cleared" in line
    )
    detail = json.loads(event_line)["detail"]
    assert detail == {
        "source_cycle": 1,
        "current_cycle": 2,
        "expected_thread_count": 2,
    }
    assert THREAD_C1 not in event_line
    assert THREAD_C1B not in event_line
    assert SOURCE_RUN_ID not in event_line
    assert REVIEWER_B not in event_line
    assert CURSOR_CHAT not in event_line
    assert CONTINUE_BODY not in event_line
    assert str(source_path) not in event_line
    assert "worker_token" not in event_line
    assert SOURCE_RUN_ID not in message
    assert REVIEWER_B not in message


def test_worker_after_nested_clear_adjudicates_current_cycle_threads(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    _source_path, current_path, run_id = _seed_nested_lineage_runs(prepared_run)
    comments = [
        {"id": 9100, "user": {"login": "rojobad"}, "body": CONTINUE_BODY},
    ]
    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_comments(comments),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            return_value=True,
        ),
    ):
        cfg.return_value = load_project_config(Path(str(prepared_run["repo"])) / "ai_dev_loop.yaml")
        continue_pr_review_cycle(run_id)

    current_threads = [_thread(THREAD_C2A), _thread(THREAD_C2B), _thread(THREAD_C2C)]
    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": [THREAD_C2A, THREAD_C2B, THREAD_C2C],
            "thread_decisions": [
                {
                    "thread_id": tid,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "a",
                }
                for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "fix three",
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
    seen_sessions: list[str | None] = []

    def fake_codex(state_arg, run_directory, **kwargs):
        seen_sessions.append(state_arg.codex.session_id)
        assert {item["thread_id"] for item in kwargs["eligible_thread_payload"]} == {
            THREAD_C2A,
            THREAD_C2B,
            THREAD_C2C,
        }
        assert THREAD_C1 not in {item["thread_id"] for item in kwargs["eligible_thread_payload"]}
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text("fix three", encoding="utf-8")
        return review, artifacts

    with (
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=_open_pr(load_run_state(current_path / "state.json").repository.branch),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=current_threads,
        ),
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
                    "commit_sha": HEAD_SHA,
                    "body": "finding",
                }
                for tid in (THREAD_C2A, THREAD_C2B, THREAD_C2C)
            ],
        ),
        patch("ai_dev_loop.commands.pr_review.run_codex_github_review", side_effect=fake_codex),
        patch("ai_dev_loop.workflow_engine.resume_run"),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
    ):
        run_pr_review_worker_loop(run_id)

    post.assert_not_called()
    assert seen_sessions == [REVIEWER_B]
    final = load_run_state(current_path / "state.json")
    assert final.github_pr_review is not None
    assert set(final.github_pr_review.eligible_thread_ids) == {
        THREAD_C2A,
        THREAD_C2B,
        THREAD_C2C,
    }
    assert final.codex.session_id == REVIEWER_B
    assert final.cursor.chat_id == CURSOR_CHAT


def test_nested_continue_without_newer_comment_is_noop(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    source_path, current_path, run_id = _seed_nested_lineage_runs(
        prepared_run,
        continue_comment_id="9100",
    )
    source_before = (source_path / "state.json").read_bytes()
    current_before = (current_path / "state.json").read_bytes()
    comments = [
        {"id": 9100, "user": {"login": "rojobad"}, "body": CONTINUE_BODY},
    ]
    spawn_calls: list[str] = []
    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_comments(comments),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: spawn_calls.append("spawn") or True,
        ),
        patch("ai_dev_loop.commands.pr_review.create_issue_comment") as post,
    ):
        cfg.return_value = load_project_config(Path(str(prepared_run["repo"])) / "ai_dev_loop.yaml")
        with pytest.raises(ValidationError, match="already consumed|continue command"):
            continue_pr_review_cycle(run_id)
    assert spawn_calls == []
    post.assert_not_called()
    assert (source_path / "state.json").read_bytes() == source_before
    assert (current_path / "state.json").read_bytes() == current_before


def test_nested_live_worker_leaves_freeze_and_source_untouched(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    source_path, current_path, run_id = _seed_nested_lineage_runs(prepared_run)
    source_before = (source_path / "state.json").read_bytes()
    before = load_run_state(current_path / "state.json")
    launcher_path = current_path / "locks/pr-review-worker.json"
    starttime = read_process_starttime(__import__("os").getpid())
    assert starttime is not None
    launcher_payload = {
        "schema_version": 1,
        "run_id": run_id,
        "worker_token": "live-token",
        "pid": __import__("os").getpid(),
        "pid_starttime": starttime,
        "started_at": "2026-07-18T12:00:00+00:00",
        "argv_redacted": [
            "python",
            "-m",
            "ai_dev_loop.pr_review_worker",
            run_id,
            "<worker-token>",
        ],
    }
    atomic_write_json(launcher_path, launcher_payload, sensitive=True)
    events_path = current_path / "logs/events.jsonl"
    events_before = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    comments = [
        {"id": 9100, "user": {"login": "rojobad"}, "body": CONTINUE_BODY},
    ]
    popen_calls: list[object] = []

    def fail_popen(*_a, **_k):
        popen_calls.append("popen")
        raise AssertionError("must not spawn when worker is live")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_comments(comments),
        ),
        patch("ai_dev_loop.commands.pr_review.subprocess.Popen", side_effect=fail_popen),
    ):
        cfg.return_value = load_project_config(Path(str(prepared_run["repo"])) / "ai_dev_loop.yaml")
        message = continue_pr_review_cycle(run_id)

    assert "left unconsumed" in message
    assert "no state mutation" in message
    assert popen_calls == []
    assert (source_path / "state.json").read_bytes() == source_before
    after = load_run_state(current_path / "state.json")
    assert after.model_dump(mode="json") == before.model_dump(mode="json")
    assert after.github_pr_review is not None
    assert after.github_pr_review.expected_eligible_thread_ids == [THREAD_C1, THREAD_C1B]
    assert after.github_pr_review.continue_comment_id is None
    events_after = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    assert events_after == events_before
    assert "pr_review_legacy_cycle_freeze_cleared" not in events_after


def _assert_nested_continue_fail_closed(
    *,
    source_path: Path | None,
    current_path: Path,
    run_id: str,
    repo: Path,
    source_before: bytes | None,
    current_before: bytes,
) -> None:
    """Shared assertions for nested-candidate fail-closed continue."""

    events_path = current_path / "logs/events.jsonl"
    events_before = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    spawn_calls: list[str] = []
    comments = [
        {"id": 9100, "user": {"login": "rojobad"}, "body": CONTINUE_BODY},
    ]

    def fail_spawn(*_a, **_k):
        spawn_calls.append("spawn")
        raise AssertionError("invalid nested lineage must not spawn a worker")

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.runners.github.run_gh",
            return_value=_gh_comments(comments),
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=fail_spawn,
        ),
    ):
        cfg.return_value = load_project_config(repo / "ai_dev_loop.yaml")
        with pytest.raises(
            ValidationError,
            match="nested legacy-cycle freeze lineage is incomplete or invalid",
        ):
            continue_pr_review_cycle(run_id)

    assert spawn_calls == []
    if source_path is not None and source_before is not None:
        assert (source_path / "state.json").read_bytes() == source_before
    assert (current_path / "state.json").read_bytes() == current_before
    after = load_run_state(current_path / "state.json")
    assert after.github_pr_review is not None
    assert after.github_pr_review.continue_comment_id is None
    assert after.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert after.github_pr_review.lifecycle == "waiting_for_user_attention"
    assert set(after.github_pr_review.expected_eligible_thread_ids or []) == {
        THREAD_C1,
        THREAD_C1B,
    }
    assert after.github_pr_review.worker_outcome == "eligible_thread_set_drift"
    events_after = events_path.read_text(encoding="utf-8") if events_path.is_file() else ""
    assert events_after == events_before
    assert "pr_review_legacy_cycle_freeze_cleared" not in events_after


def test_missing_source_nested_lineage_fails_closed(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """Absent one-hop source must not consume continue or fall through."""

    source_path, current_path, run_id = _seed_nested_lineage_runs(prepared_run)
    shutil.rmtree(source_path)
    current_before = (current_path / "state.json").read_bytes()
    before = load_run_state(current_path / "state.json")
    assert _classify_legacy_external_cycle_freeze(before, load_run_fn=load_run) is None
    _assert_nested_continue_fail_closed(
        source_path=None,
        current_path=current_path,
        run_id=run_id,
        repo=Path(str(prepared_run["repo"])),
        source_before=None,
        current_before=current_before,
    )
    # Source directory must remain absent (no recreate / mutation).
    assert not source_path.exists()


def test_invalid_source_checkpoint_nested_lineage_fails_closed(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """Ancestor without external_adjudication must fail closed."""

    source_path, current_path, run_id = _seed_nested_lineage_runs(
        prepared_run,
        source_recovery_checkpoint="reviewing",
    )
    source_before = (source_path / "state.json").read_bytes()
    current_before = (current_path / "state.json").read_bytes()
    _assert_nested_continue_fail_closed(
        source_path=source_path,
        current_path=current_path,
        run_id=run_id,
        repo=Path(str(prepared_run["repo"])),
        source_before=source_before,
        current_before=current_before,
    )


def test_ancestor_cycle_mismatch_does_not_migrate_or_spawn(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """Disagreeing ancestor cycle fields (both older than current) must fail closed."""

    source_path, current_path, run_id = _seed_nested_lineage_runs(
        prepared_run,
        source_cycle=1,
        current_cycle=3,
    )
    source = load_run_state(source_path / "state.json")
    assert source.recovery is not None
    assert source.github_pr_review is not None
    # Keep both values below current cycle 3, but make them disagree.
    source.recovery = source.recovery.model_copy(update={"source_iteration": 2})
    source.github_pr_review = source.github_pr_review.model_copy(update={"cycle_number": 1})
    save_run_state(source_path, source)
    source_before = (source_path / "state.json").read_bytes()
    current_before = (current_path / "state.json").read_bytes()
    before = load_run_state(current_path / "state.json")
    assert before.github_pr_review is not None
    assert before.github_pr_review.cycle_number == 3
    assert source.recovery.source_iteration == 2
    assert source.github_pr_review.cycle_number == 1
    assert _classify_legacy_external_cycle_freeze(before, load_run_fn=load_run) is None
    _assert_nested_continue_fail_closed(
        source_path=source_path,
        current_path=current_path,
        run_id=run_id,
        repo=Path(str(prepared_run["repo"])),
        source_before=source_before,
        current_before=current_before,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        "head_branch",
        "repository_name_with_owner",
    ],
)
def test_nested_identity_mismatch_preserves_freeze(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    mutate: str,
) -> None:
    """Mismatched persisted GitHub head or owner must fail closed."""

    source_path, current_path, run_id = _seed_nested_lineage_runs(prepared_run)
    source = load_run_state(source_path / "state.json")
    assert source.github_pr_review is not None
    if mutate == "head_branch":
        source.github_pr_review = source.github_pr_review.model_copy(
            update={"head_branch": "other-github-head"}
        )
    else:
        source.github_pr_review = source.github_pr_review.model_copy(
            update={"repository_name_with_owner": "other/owner"}
        )
    save_run_state(source_path, source)
    source_before = (source_path / "state.json").read_bytes()
    current_before = (current_path / "state.json").read_bytes()
    _assert_nested_continue_fail_closed(
        source_path=source_path,
        current_path=current_path,
        run_id=run_id,
        repo=Path(str(prepared_run["repo"])),
        source_before=source_before,
        current_before=current_before,
    )
