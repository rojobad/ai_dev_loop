"""Phase 15.7 GitHub adjudication schema compatibility and recovery."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import (
    _apply_worker_failure,
    run_pr_review_worker_loop,
)
from ai_dev_loop.commands.pr_review_recover import recover_pr_review_cycle
from ai_dev_loop.errors import AdjudicationSchemaIncompatibleError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread
from ai_dev_loop.state import (
    ControllerState,
    GithubPrReviewState,
    RunStatus,
    load_run_state,
    save_run_state,
)

runner = CliRunner()
CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_A = "PRRT_THREAD_A"
THREAD_B = "PRRT_THREAD_B"


def _enable_github(repo: Path) -> None:
    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "github:" not in text:
        config.write_text(
            text
            + "\ngithub:\n  enabled: true\n  poll_interval_seconds: 1\n  poll_timeout_hours: 1\n",
            encoding="utf-8",
        )


def _seed_failed_schema_run(
    prepared_run: dict[str, Path | str],
    *,
    historical_worker_error: bool = False,
    with_controller: bool = True,
) -> tuple[Path, str]:
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    _enable_github(repo)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.FAILED
    state.codex.session_id = REVIEWER_B
    if with_controller:
        state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    if not state.cursor.chat_id:
        state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="failed",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha="0d9c4ce473d9a1b2c3d4e5f60718293a0b1c2d3e",
        request_comment_id="9001",
        request_marker="ai_dev_loop-pr-review:marker",
        request_created_at="2026-07-18T01:00:00+00:00",
        eligible_thread_ids=[THREAD_A, THREAD_B],
        expected_eligible_thread_ids=[THREAD_A, THREAD_B],
        worker_outcome=(
            "worker_error" if historical_worker_error else "adjudication_schema_incompatible"
        ),
    )
    state.last_error = "Codex rejected the GitHub adjudication output schema (invalid_json_schema)"
    save_run_state(run_path, state)
    events = run_path / "github/cycles/01/codex.events.jsonl"
    events.parent.mkdir(parents=True, exist_ok=True)
    # Anonymized real Codex exec --json transport from the failed PR-review run.
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "codex_adjudication_schema_rejection_events.jsonl"
    )
    events.write_text(fixture.read_text(encoding="utf-8"), encoding="utf-8")
    return run_path, state.run_id


def _threads() -> list[GithubReviewThread]:
    return [
        GithubReviewThread(
            thread_id=THREAD_A,
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=1,
            commit_sha="0d9c4ce473d9a1b2c3d4e5f60718293a0b1c2d3e",
            root_comment_id="C1",
            root_comment_body_sha256="b" * 64,
            created_at="2026-07-18T01:05:00+00:00",
            review_id=None,
        ),
        GithubReviewThread(
            thread_id=THREAD_B,
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="b.py",
            line=2,
            commit_sha="0d9c4ce473d9a1b2c3d4e5f60718293a0b1c2d3e",
            root_comment_id="C2",
            root_comment_body_sha256="c" * 64,
            created_at="2026-07-18T01:05:01+00:00",
            review_id=None,
        ),
    ]


def _open_pr(branch: str) -> GithubPullRequest:
    return GithubPullRequest(
        number=45,
        url="https://example.test/pr/45",
        title="t",
        state="OPEN",
        head_ref=branch,
        head_sha="0d9c4ce473d9a1b2c3d4e5f60718293a0b1c2d3e",
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )


def test_new_schema_incompatible_failure_is_interrupted(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.EVALUATING_BOT_FEEDBACK
    state.github_pr_review = GithubPrReviewState(
        source_run_id="source",
        lifecycle="evaluating_bot_feedback",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha="a" * 40,
        request_comment_id="1",
        request_marker="m",
        request_created_at="2026-07-18T01:00:00+00:00",
        eligible_thread_ids=[THREAD_A, THREAD_B],
    )
    save_run_state(run_path, state)
    _apply_worker_failure(
        run_path,
        AdjudicationSchemaIncompatibleError(
            "Codex rejected the GitHub adjudication output schema (invalid_json_schema); "
            "inspect github/cycles/01/codex.events.jsonl"
        ),
    )
    fresh = load_run_state(run_path / "state.json")
    assert fresh.status == RunStatus.INTERRUPTED
    assert fresh.github_pr_review is not None
    assert fresh.github_pr_review.worker_outcome == "adjudication_schema_incompatible"
    assert set(fresh.github_pr_review.expected_eligible_thread_ids or []) == {
        THREAD_A,
        THREAD_B,
    }
    assert "uniqueItems" not in (fresh.last_error or "")


def test_recover_dry_run_does_not_create_successor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    _run_path, run_id = _seed_failed_schema_run(prepared_run, historical_worker_error=True)
    with patch(
        "ai_dev_loop.commands.pr_review_recover.get_pull_request",
        return_value=_open_pr(load_run_state(_run_path / "state.json").repository.branch),
    ):
        result = runner.invoke(
            app,
            ["pr-review", "recover", run_id, "--dry-run", "--output", "json"],
        )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["eligible"] is True
    assert payload["trigger_will_be_reposted"] is False
    assert payload["expected_thread_count"] == 2
    assert payload["historical_schema_rejection"] is True
    assert payload["resume_command"] == (
        f"ai_dev_loop pr-review resume <successor-run-id> --controller-session-id {CONTROLLER_A}"
    )
    assert "controller_session_id" not in payload
    from ai_dev_loop.run_discovery import list_run_directories

    ids = [state.run_id for _, state in list_run_directories()]
    assert ids == [run_id]
    assert load_run_state(_run_path / "state.json").status == RunStatus.FAILED


def test_recover_creates_immutable_idempotent_successor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_failed_schema_run(prepared_run, historical_worker_error=True)
    source_before = load_run_state(run_path / "state.json")
    pr = _open_pr(source_before.repository.branch)
    with patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr):
        first = recover_pr_review_cycle(run_id, dry_run=False)
        second = recover_pr_review_cycle(run_id, dry_run=False)
        rendered = runner.invoke(
            app,
            ["pr-review", "recover", run_id, "--dry-run", "--output", "json"],
        )
    assert first.recovery_run_id is not None
    assert second.recovery_run_id == first.recovery_run_id
    assert second.analysis.reused_existing_successor is True
    source_after = load_run_state(run_path / "state.json")
    assert source_after.status == RunStatus.FAILED
    assert source_after.codex.session_id == REVIEWER_B
    successor_dir = find_run_directory(first.recovery_run_id)
    successor = load_run_state(successor_dir / "state.json")
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "external_adjudication"
    assert successor.recovery.reason_code == "github_adjudication_schema_incompatible"
    assert set(successor.recovery.expected_eligible_thread_ids or []) == {THREAD_A, THREAD_B}
    assert successor.codex.session_id == REVIEWER_B
    assert successor.controller is not None
    assert successor.controller.controller_session_id == CONTROLLER_A
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.pr_number == 45
    assert successor.github_pr_review.request_comment_id == "9001"
    assert (
        successor.github_pr_review.bound_head_sha == source_before.github_pr_review.bound_head_sha
    )
    expected_resume = (
        f"ai_dev_loop pr-review resume {first.recovery_run_id} "
        f"--controller-session-id {CONTROLLER_A}"
    )
    assert first.resume_command == expected_resume
    assert second.resume_command == expected_resume
    assert rendered.exit_code == 0, rendered.stdout
    dry_payload = json.loads(rendered.stdout)
    assert dry_payload["resume_command"] == expected_resume
    assert CONTROLLER_A in dry_payload["resume_command"]
    assert "controller_session_id" not in dry_payload
    status = runner.invoke(app, ["pr-review", "status", first.recovery_run_id, "--output", "json"])
    assert status.exit_code == 0, status.stdout
    status_payload = json.loads(status.stdout)
    assert CONTROLLER_A not in status.stdout
    assert "controller_session_id" not in json.dumps(status_payload)


def test_recover_without_controller_omits_controller_flag(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_failed_schema_run(
        prepared_run,
        historical_worker_error=True,
        with_controller=False,
    )
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch)
    with patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr):
        result = recover_pr_review_cycle(run_id, dry_run=False)
    assert result.recovery_run_id is not None
    assert result.analysis.has_controller is False
    assert result.analysis.controller_session_id is None
    assert result.resume_command == f"ai_dev_loop pr-review resume {result.recovery_run_id}"
    assert "--controller-session-id" not in (result.resume_command or "")


def test_successor_resume_reuses_session_and_skips_trigger(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    run_path, run_id = _seed_failed_schema_run(prepared_run)
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch)
    with patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id
    threads = _threads()
    review = GithubPrReviewResult.model_validate(
        {
            "eligible_thread_ids": [THREAD_A, THREAD_B],
            "thread_decisions": [
                {
                    "thread_id": THREAD_A,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "a",
                },
                {
                    "thread_id": THREAD_B,
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "b",
                },
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "Please fix both threads.",
            "tests_status": "not_applicable",
            "summary": "all actionable",
            "residual_risk_comment": None,
            "highest_severity": "P2",
        }
    )
    artifacts = GithubAdjudicationArtifacts(
        events_path="github/cycles/01/codex.events.jsonl",
        stderr_path="github/cycles/01/codex.stderr.txt",
        result_path="github/cycles/01/result.json",
        report_path="github/cycles/01/report.md",
        metadata_path="github/cycles/01/codex.metadata.json",
        snapshot_path="github/cycles/01/threads.snapshot.json",
        fix_prompt_path="prompts/fixes/github-01.txt",
    )
    posted = {"value": False}
    codex_sessions: list[str] = []

    def fake_codex(state, run_directory, **kwargs):
        codex_sessions.append(state.codex.session_id)
        assert set(item["thread_id"] for item in kwargs["eligible_thread_payload"]) == {
            THREAD_A,
            THREAD_B,
        }
        (run_directory / artifacts.fix_prompt_path).parent.mkdir(parents=True, exist_ok=True)
        (run_directory / artifacts.fix_prompt_path).write_text(
            "Please fix both threads.", encoding="utf-8"
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

    def fail_post(*_a, **_k):
        posted["value"] = True
        raise AssertionError("must not re-post trigger")

    with (
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=pr,
        ),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=threads),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            return_value=threads,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._load_thread_bodies",
            return_value=[
                {
                    "thread_id": THREAD_A,
                    "author_login": "chatgpt-codex-connector",
                    "path": "a.py",
                    "line": 1,
                    "commit_sha": pr.head_sha,
                    "body": "A",
                },
                {
                    "thread_id": THREAD_B,
                    "author_login": "chatgpt-codex-connector",
                    "path": "b.py",
                    "line": 2,
                    "commit_sha": pr.head_sha,
                    "body": "B",
                },
            ],
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=fake_codex,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=fail_post,
        ),
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop"),
    ):
        result = runner.invoke(
            app,
            [
                "pr-review",
                "resume",
                successor_id,
                "--controller-session-id",
                CONTROLLER_A,
            ],
        )
        assert result.exit_code == 0, result.stdout
        # Resume only spawns worker; invoke worker body directly under the same mocks.
        run_pr_review_worker_loop(successor_id)

    assert posted["value"] is False
    assert codex_sessions == [REVIEWER_B]
    successor = load_run_state(find_run_directory(successor_id) / "state.json")
    assert successor.codex.session_id == REVIEWER_B
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.lifecycle == "fixing_external_feedback"


def test_recover_rejects_side_effects_and_other_failures(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_failed_schema_run(prepared_run)
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"replied_thread_ids": [THREAD_A], "worker_outcome": "worker_error"}
    )
    # Remove schema rejection evidence so this is a plain failed run with side effects.
    events = run_path / "github/cycles/01/codex.events.jsonl"
    events.write_text("{}\n", encoding="utf-8")
    save_run_state(run_path, state)
    with patch(
        "ai_dev_loop.commands.pr_review_recover.get_pull_request",
        return_value=_open_pr(state.repository.branch),
    ):
        result = recover_pr_review_cycle(run_id, dry_run=True)
    assert result.analysis.eligible is False
    assert "replied_threads_present" in result.analysis.blockers or (
        "not_adjudication_schema_incompatible" in result.analysis.blockers
    )


def test_historical_anonymized_fixture_is_recoverable(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    """Anon fixture shaped like crypto-sentinel-20260718T010234Z-317683."""

    run_path, run_id = _seed_failed_schema_run(
        prepared_run,
        historical_worker_error=True,
        with_controller=True,
    )
    state = load_run_state(run_path / "state.json")
    # Reset expected field to mimic historical payload without the new field.
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"expected_eligible_thread_ids": None}
    )
    save_run_state(run_path, state)
    with patch(
        "ai_dev_loop.commands.pr_review_recover.get_pull_request",
        return_value=_open_pr(state.repository.branch),
    ):
        analysis = recover_pr_review_cycle(run_id, dry_run=True).analysis
    assert analysis.eligible is True
    assert analysis.historical_schema_rejection is True
    assert analysis.expected_thread_count == 2
    assert analysis.pr_number == 45


def test_thread_set_drift_stops_without_github_writes(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
) -> None:
    run_path, run_id = _seed_failed_schema_run(prepared_run)
    pr = _open_pr(load_run_state(run_path / "state.json").repository.branch)
    with patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id
    successor_dir = find_run_directory(successor_id)
    successor = load_run_state(successor_dir / "state.json")
    successor.status = RunStatus.AWAITING_BOT_REVIEW
    assert successor.github_pr_review is not None
    successor.github_pr_review = successor.github_pr_review.model_copy(
        update={"lifecycle": "awaiting_bot_review"}
    )
    save_run_state(successor_dir, successor)

    drifted = [
        GithubReviewThread(
            thread_id="PRRT_NEW",
            is_resolved=False,
            author_login="chatgpt-codex-connector",
            path="c.py",
            line=3,
            commit_sha=pr.head_sha,
            root_comment_id="C3",
            root_comment_body_sha256="d" * 64,
            created_at="2026-07-18T01:10:00+00:00",
            review_id=None,
        )
    ]
    with (
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=drifted),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            return_value=drifted,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=AssertionError("Codex must not run on drift"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=AssertionError("no GitHub writes"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=AssertionError("no replies"),
        ),
    ):
        run_pr_review_worker_loop(successor_id)

    final = load_run_state(successor_dir / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome == "eligible_thread_set_drift"
