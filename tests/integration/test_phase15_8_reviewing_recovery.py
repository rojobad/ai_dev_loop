"""Phase 15.8 local Codex review artifact dirs and PR-review reviewing recovery."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import resume_pr_review_cycle
from ai_dev_loop.commands.pr_review_recover import recover_pr_review_cycle
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.codex import FAILURE_CODE_RESULT_ARTIFACT_MISSING
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
THREAD_C = "PRRT_THREAD_C"


def _pr_for_state(state) -> GithubPullRequest:
    return GithubPullRequest(
        number=45,
        url="https://example.test/pr/45",
        title="t",
        state="OPEN",
        head_ref=state.repository.branch,
        head_sha=state.repository.initial_head,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )


def _eligible_threads(state, *, extra: list[str] | None = None, resolved: set[str] | None = None):
    resolved = resolved or set()
    ids = [THREAD_A, THREAD_B, *(extra or [])]
    return [
        GithubReviewThread(
            thread_id=thread_id,
            is_resolved=thread_id in resolved,
            author_login="chatgpt-codex-connector",
            path="a.py",
            line=1,
            commit_sha=state.repository.initial_head,
            root_comment_id=f"C-{thread_id}",
            root_comment_body_sha256="b" * 64,
            created_at="2026-07-18T01:05:00+00:00",
            review_id=None,
        )
        for thread_id in ids
    ]


@contextmanager
def _mock_remote_pr(
    pr: GithubPullRequest,
    state,
    *,
    threads: list[GithubReviewThread] | None = None,
) -> Iterator[None]:
    """Avoid mutating the target repo YAML (which would dirty the staged checkpoint)."""

    fake_config = MagicMock()
    fake_config.github.command = "gh"
    fake_config.github.reviewer_logins = ["chatgpt-codex-connector"]
    thread_list = threads if threads is not None else _eligible_threads(state)
    with (
        patch("ai_dev_loop.commands.pr_review_recover.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=fake_config),
        patch(
            "ai_dev_loop.commands.pr_review_recover.list_review_threads",
            return_value=thread_list,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=thread_list,
        ),
    ):
        yield


def _seed_failed_reviewing_pr_run(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    with_controller: bool = True,
    codex_mode: str = "output_artifact_fail",
) -> tuple[Path, str, str]:
    """Fail after Cursor/staging with a classified missing Codex result artifact."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", codex_mode)
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(str(prepared_run["run_id"]))

    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.cursor.chat_id
    assert not (run_path / "codex/reviews/01.json").exists()

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert head == state.repository.initial_head

    state.codex.session_id = REVIEWER_B
    if with_controller:
        state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    cycle_dir = run_path / "github/cycles/01"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    (cycle_dir / "result.json").write_text('{"all_actionable": true}\n', encoding="utf-8")
    (cycle_dir / "report.md").write_text("# external\n", encoding="utf-8")
    (cycle_dir / "threads.snapshot.json").write_text(
        json.dumps(
            {
                "threads": [
                    {"thread_id": tid, "body_sha256": "a" * 64} for tid in [THREAD_A, THREAD_B]
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (cycle_dir / "codex.events.jsonl").write_text("{}\n", encoding="utf-8")
    (cycle_dir / "codex.metadata.json").write_text("{}\n", encoding="utf-8")
    fix_prompt = run_path / "prompts/fixes/github-01.txt"
    fix_prompt.parent.mkdir(parents=True, exist_ok=True)
    fix_prompt.write_text("Please fix both threads.\n", encoding="utf-8")
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="failed",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=head,
        request_comment_id="9001",
        request_marker="ai_dev_loop-pr-review:marker",
        request_created_at="2026-07-18T01:00:00+00:00",
        eligible_thread_ids=[THREAD_A, THREAD_B],
        expected_eligible_thread_ids=[THREAD_A, THREAD_B],
        last_external_result_path="github/cycles/01/result.json",
        last_snapshot_path="github/cycles/01/threads.snapshot.json",
        external_fix_prompt_path="prompts/fixes/github-01.txt",
        worker_outcome="worker_error",
    )
    state.last_error = (
        "Codex review failed with exit code 2; "
        "inspect codex/events/01.jsonl and codex/events/01.stderr.txt"
    )
    save_run_state(run_path, state)
    assert state.cursor.chat_id is not None
    return run_path, state.run_id, state.cursor.chat_id


def test_local_review_succeeds_when_reviews_dir_was_removed(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex review must recreate missing artifact parents before --output-last-message."""

    import shutil

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    reviews_dir = run_path / "codex" / "reviews"
    if reviews_dir.exists():
        shutil.rmtree(reviews_dir)
    assert not reviews_dir.exists()
    result = start_run(str(prepared_run["run_id"]))
    assert result.status == "completed"
    assert (run_path / "codex/reviews/01.json").is_file()
    assert (run_path / "codex/reviews/01.metadata.json").is_file()
    assert "exit code 91" not in (result.result_message or "")


def test_reviewing_recover_dry_run_and_successor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    meta = json.loads((run_path / "codex/reviews/01.metadata.json").read_text(encoding="utf-8"))
    assert meta.get("failure_code") == FAILURE_CODE_RESULT_ARTIFACT_MISSING
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    source_before = json.loads((run_path / "state.json").read_text(encoding="utf-8"))
    with _mock_remote_pr(pr, state):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
        assert dry.analysis.eligible is True
        assert dry.analysis.checkpoint == "reviewing"
        assert dry.analysis.reason_code == "codex_review_result_artifact_missing"
        assert dry.recovery_run_id is None
        assert json.loads((run_path / "state.json").read_text(encoding="utf-8")) == source_before

        created = recover_pr_review_cycle(run_id, dry_run=False)
        reused = recover_pr_review_cycle(run_id, dry_run=False)

    assert created.recovery_run_id is not None
    assert reused.recovery_run_id == created.recovery_run_id
    assert reused.analysis.reused_existing_successor is True
    source_after = load_run_state(run_path / "state.json")
    assert source_after.status == RunStatus.FAILED
    assert source_after.codex.session_id == REVIEWER_B

    successor_dir = find_run_directory(created.recovery_run_id)
    successor = load_run_state(successor_dir / "state.json")
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "reviewing"
    assert successor.recovery.reason_code == "codex_review_result_artifact_missing"
    assert successor.cursor.chat_id == chat_id
    assert successor.codex.session_id == REVIEWER_B
    assert successor.controller is not None
    assert successor.controller.controller_session_id == CONTROLLER_A
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.lifecycle == "fixing_external_feedback"
    assert (successor_dir / "cursor/iterations/01/metadata.json").is_file()
    assert (successor_dir / "git/diffs/01.patch").is_file()
    assert (successor_dir / "github/cycles/01/result.json").is_file()
    assert (successor_dir / "github/cycles/01/threads.snapshot.json").is_file()
    assert (successor_dir / "prompts/fixes/github-01.txt").is_file()
    assert not (successor_dir / "github/cycles/01/codex.events.jsonl").exists()
    assert not (successor_dir / "github/cycles/01/codex.metadata.json").exists()
    assert not (successor_dir / "codex/reviews/01.json").exists()
    expected_resume = (
        f"ai_dev_loop pr-review resume {created.recovery_run_id} "
        f"--controller-session-id {CONTROLLER_A}"
    )
    assert created.resume_command == expected_resume


def test_reviewing_resume_skips_cursor_and_github_side_effects(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    agent_before = fake_clis["agent_log"].read_text(encoding="utf-8")
    codex_before = fake_clis["codex_log"].read_text(encoding="utf-8")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    with _mock_remote_pr(pr, state):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id
    successor_state = load_run_state(find_run_directory(successor_id) / "state.json")

    github_calls: list[str] = []

    def track_github(name: str):
        def _inner(*_a, **_k):
            github_calls.append(name)
            raise AssertionError(f"unexpected GitHub call: {name}")

        return _inner

    with (
        _mock_remote_pr(pr, successor_state),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: github_calls.append("spawn_worker"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=track_github("post_issue_comment"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.reply_to_review_thread",
            side_effect=track_github("reply"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            side_effect=track_github("resolve"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_github_review",
            side_effect=track_github("adjudicate"),
        ),
    ):
        message = resume_pr_review_cycle(
            successor_id,
            controller_session_id=CONTROLLER_A,
        )

    assert "Resumed local fix loop" in message
    agent_after = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_after == agent_before
    codex_after = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert REVIEWER_B in codex_after
    assert "--last" not in codex_after
    assert codex_after.count("ARGS:") == codex_before.count("ARGS:") + 1
    assert "post_issue_comment" not in github_calls
    assert "reply" not in github_calls
    assert "resolve" not in github_calls
    assert "adjudicate" not in github_calls
    assert github_calls.count("spawn_worker") == 1

    successor = load_run_state(find_run_directory(successor_id) / "state.json")
    assert successor.cursor.chat_id == chat_id
    assert successor.codex.session_id == REVIEWER_B
    assert (find_run_directory(successor_id) / "codex/reviews/01.json").is_file()
    assert successor.status == RunStatus.PUBLISHING_EXTERNAL_FIX
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.lifecycle == "publishing_external_fix"


def test_reviewing_recover_rejects_generic_codex_failure(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(
        prepared_run,
        fake_clis,
        monkeypatch,
        codex_mode="fail",
    )
    meta = json.loads((run_path / "codex/reviews/01.metadata.json").read_text(encoding="utf-8"))
    assert meta.get("failure_code") is None
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    with _mock_remote_pr(pr, state):
        result = recover_pr_review_cycle(run_id, dry_run=True)
    assert result.analysis.eligible is False
    assert "not_codex_output_artifact_failure" in result.analysis.blockers
    assert result.recovery_run_id is None


@pytest.mark.parametrize(
    ("threads_factory",),
    [
        (lambda state: _eligible_threads(state, extra=[THREAD_C]),),
        (lambda state: _eligible_threads(state)[:1],),
        (lambda state: _eligible_threads(state, resolved={THREAD_B}),),
    ],
)
def test_reviewing_recover_rejects_thread_set_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    threads_factory,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    with _mock_remote_pr(pr, state, threads=threads_factory(state)):
        result = recover_pr_review_cycle(run_id, dry_run=True)
    assert result.analysis.eligible is False
    assert "eligible_thread_set_drift" in result.analysis.blockers
    assert result.recovery_run_id is None
    assert load_run_state(run_path / "state.json").status == RunStatus.FAILED


def test_reviewing_resume_stops_on_thread_set_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    with _mock_remote_pr(pr, state):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id
    successor_state = load_run_state(find_run_directory(successor_id) / "state.json")
    agent_before = fake_clis["agent_log"].read_text(encoding="utf-8")
    codex_before = fake_clis["codex_log"].read_text(encoding="utf-8")

    with _mock_remote_pr(
        pr,
        successor_state,
        threads=_eligible_threads(successor_state, extra=[THREAD_C]),
    ):
        message = resume_pr_review_cycle(
            successor_id,
            controller_session_id=CONTROLLER_A,
        )
    assert "waiting for user attention" in message.lower()
    assert fake_clis["agent_log"].read_text(encoding="utf-8") == agent_before
    assert fake_clis["codex_log"].read_text(encoding="utf-8") == codex_before
    final = load_run_state(find_run_directory(successor_id) / "state.json")
    assert final.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert final.github_pr_review is not None
    assert final.github_pr_review.worker_outcome == "eligible_thread_set_drift"


def test_reviewing_recover_rejects_blockers(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)

    (run_path / "codex/reviews/01.json").write_text(
        json.dumps(
            {
                "has_actionable_findings": False,
                "findings_count": 0,
                "highest_severity": None,
                "review_markdown": "# Review",
                "cursor_fix_prompt": None,
                "tests_status": "passed",
                "summary": "ok",
            }
        ),
        encoding="utf-8",
    )
    with _mock_remote_pr(pr, state):
        present = recover_pr_review_cycle(run_id, dry_run=True)
    assert present.analysis.eligible is False
    assert "review_result_already_present" in present.analysis.blockers

    (run_path / "codex/reviews/01.json").unlink()
    (run_path / "codex/reviews/01.json").write_text("{not-json", encoding="utf-8")
    with _mock_remote_pr(pr, state):
        invalid = recover_pr_review_cycle(run_id, dry_run=True)
    assert invalid.analysis.eligible is False
    assert "review_result_invalid" in invalid.analysis.blockers

    (run_path / "codex/reviews/01.json").unlink()
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={"replied_thread_ids": [THREAD_A]}
    )
    save_run_state(run_path, state)
    with _mock_remote_pr(pr, state):
        replied = recover_pr_review_cycle(run_id, dry_run=True)
    assert replied.analysis.eligible is False
    assert "replied_threads_present" in replied.analysis.blockers

    state.github_pr_review = state.github_pr_review.model_copy(
        update={"replied_thread_ids": [], "publication_phase": "committed"}
    )
    save_run_state(run_path, state)
    with _mock_remote_pr(pr, state):
        published = recover_pr_review_cycle(run_id, dry_run=True)
    assert published.analysis.eligible is False
    assert "publication_started" in published.analysis.blockers

    closed = GithubPullRequest(
        number=45,
        url="https://example.test/pr/45",
        title="t",
        state="CLOSED",
        head_ref=state.repository.branch,
        head_sha=state.repository.initial_head,
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )
    state.github_pr_review = state.github_pr_review.model_copy(update={"publication_phase": None})
    save_run_state(run_path, state)
    with _mock_remote_pr(closed, state):
        closed_result = recover_pr_review_cycle(run_id, dry_run=True)
    assert closed_result.analysis.eligible is False
    assert "pr_not_open" in closed_result.analysis.blockers


def test_reviewing_failed_successor_requires_explicit_chain_recovery(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    with _mock_remote_pr(pr, state):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_dir = find_run_directory(created.recovery_run_id)
    successor = load_run_state(successor_dir / "state.json")
    successor.status = RunStatus.FAILED
    successor.last_error = "Codex review failed with exit code 2; inspect artifacts"
    save_run_state(successor_dir, successor)
    meta_path = successor_dir / "codex/reviews/01.metadata.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "exit_code": 2,
                "timed_out": False,
                "failure_code": FAILURE_CODE_RESULT_ARTIFACT_MISSING,
            }
        ),
        encoding="utf-8",
    )
    (successor_dir / "codex/events").mkdir(parents=True, exist_ok=True)
    (successor_dir / "codex/events/01.stderr.txt").write_text(
        "failed to write output-last-message codex/reviews/01.json: No such file or directory\n",
        encoding="utf-8",
    )

    with _mock_remote_pr(pr, successor):
        with pytest.raises(ValidationError, match="itself failed"):
            recover_pr_review_cycle(run_id, dry_run=False)
        next_gen = recover_pr_review_cycle(created.recovery_run_id, dry_run=True)
    assert next_gen.analysis.checkpoint == "reviewing"
    assert next_gen.analysis.reason_code == "codex_review_result_artifact_missing"
    assert next_gen.analysis.eligible is True


def test_reviewing_status_and_errors_omit_sensitive_payloads(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat_id = _seed_failed_reviewing_pr_run(prepared_run, fake_clis, monkeypatch)
    state = load_run_state(run_path / "state.json")
    pr = _pr_for_state(state)
    with _mock_remote_pr(pr, state):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    status = runner.invoke(
        app,
        ["pr-review", "status", created.recovery_run_id, "--output", "json"],
    )
    assert status.exit_code == 0, status.stdout
    payload = json.loads(status.stdout)
    rendered = status.stdout
    assert REVIEWER_B not in rendered
    assert CONTROLLER_A not in rendered
    assert "Please fix both threads" not in rendered
    assert "# Review" not in rendered
    assert "diff --git" not in rendered
    assert "controller_session_id" not in json.dumps(payload)
