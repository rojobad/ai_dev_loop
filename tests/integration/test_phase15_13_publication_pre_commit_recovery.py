"""Phase 15.13: publication_pre_commit recovery and publish-only resume."""

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
from ai_dev_loop.commands.pr_review_recover import (
    analyze_pr_review_recovery,
    recover_pr_review_cycle,
)
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread
from ai_dev_loop.runners.publish import PublishResult, read_staged_patch
from ai_dev_loop.state import (
    ControllerState,
    GithubPrReviewState,
    RunStatus,
    load_run_state,
    save_run_state,
    sha256_text,
)

runner = CliRunner()
CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_A = "PRRT_PUB_A"
THREAD_B = "PRRT_PUB_B"
THREAD_C = "PRRT_PUB_C"


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
        created_at="2026-07-18T21:00:00+00:00",
        review_id=None,
    )


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


def _seed_failed_publication_pre_commit_run(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    carried_forward_commit_shas: bool = False,
) -> tuple[Path, str, str, str, str]:
    """Incident-shaped source: local review accepted, publication stuck at pre_commit.

    When ``carried_forward_commit_shas`` is true, mirrors
    ``crypto-sentinel-20260718T212910Z-9488fe``: non-null
    ``local_commit_sha`` / ``publication_commit_sha`` equal to the bound HEAD
    from the prior published cycle, with ``publication_phase`` still
    ``pre_commit`` and no new commit on HEAD.
    """

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    result = start_run(str(prepared_run["run_id"]))
    assert result.status in {"completed", "completed_with_residual_risk"}

    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id
    chat_id = state.cursor.chat_id
    bound_sha = state.repository.initial_head
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert head == bound_sha

    # Ensure a non-empty staged index matching the recorded iteration patch.
    patch_artifact = run_path / "git/diffs/01.patch"
    assert patch_artifact.is_file()
    staged_names = subprocess.check_output(
        ["git", "diff", "--cached", "--name-only"], cwd=repo, text=True
    ).strip()
    if not staged_names:
        target = repo / "phase1513.txt"
        target.write_text("phase-15-13 staged fix\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "phase1513.txt"], cwd=repo)
    patch_artifact.write_text(read_staged_patch(repo), encoding="utf-8")
    patch_sha = sha256_text(read_staged_patch(repo))

    review_path = run_path / "codex/reviews/01.json"
    assert review_path.is_file()
    review_payload = json.loads(review_path.read_text(encoding="utf-8"))
    assert review_payload.get("has_actionable_findings") is False

    cycle_dir = run_path / "github/cycles/02"
    cycle_dir.mkdir(parents=True, exist_ok=True)
    publication = {
        "commit_subject": "Accept staged PR fix",
        "commit_body": "Local review accepted.",
        "pr_title": "Accept staged PR fix",
        "pr_body": "Publication text for recovery.",
    }
    (cycle_dir / "publication-text.json").write_text(
        json.dumps(publication, indent=2) + "\n", encoding="utf-8"
    )
    (cycle_dir / "result.json").write_text("{}\n", encoding="utf-8")
    (cycle_dir / "threads.snapshot.json").write_text("[]\n", encoding="utf-8")
    thread_ids = [THREAD_A, THREAD_B, THREAD_C]
    state.status = RunStatus.FAILED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.last_error = "ssh-agent has no usable keys; preload the SSH key before publication"
    state.result = "publication failed before commit"
    # Historical incident shape: prior-cycle publish SHAs equal bound HEAD.
    recorded_local = bound_sha if carried_forward_commit_shas else None
    recorded_publication = bound_sha if carried_forward_commit_shas else None
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="failed",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        repository_name_with_owner="acme/demo",
        head_branch=state.repository.branch,
        bound_head_sha=bound_sha,
        request_comment_id="9002",
        request_marker=f"ai_dev_loop-pr-review:{state.run_id}:cycle:2:{bound_sha}",
        request_created_at="2026-07-18T21:00:00+00:00",
        eligible_thread_ids=thread_ids,
        expected_eligible_thread_ids=thread_ids,
        processed_thread_ids=[],
        replied_thread_ids=[],
        resolved_thread_ids=[],
        last_external_result_path="github/cycles/02/result.json",
        last_snapshot_path="github/cycles/02/threads.snapshot.json",
        publication_phase="pre_commit",
        publication_text_path="github/cycles/02/publication-text.json",
        staged_patch_sha256=patch_sha,
        local_commit_sha=recorded_local,
        publication_commit_sha=recorded_publication,
        worker_outcome="validation_error",
    )
    save_run_state(run_path, state)
    return run_path, state.run_id, chat_id, bound_sha, patch_sha


def test_publication_pre_commit_recover_dry_run_and_successor(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id, bound_sha, patch_sha = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = json.loads((run_path / "state.json").read_text(encoding="utf-8"))

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
        assert dry.analysis.eligible is True
        assert dry.analysis.checkpoint == "publication_pre_commit"
        assert dry.analysis.reason_code == "publication_pre_commit_interrupted"
        assert dry.analysis.staged_patch_sha256 == patch_sha
        assert dry.recovery_run_id is None
        assert json.loads((run_path / "state.json").read_text(encoding="utf-8")) == source_before

        created = recover_pr_review_cycle(run_id, dry_run=False)
        reused = recover_pr_review_cycle(run_id, dry_run=False)

    assert created.recovery_run_id is not None
    assert reused.recovery_run_id == created.recovery_run_id
    assert reused.analysis.reused_existing_successor is True
    source_after = load_run_state(run_path / "state.json")
    assert source_after.status == RunStatus.FAILED
    assert json.loads((run_path / "state.json").read_text(encoding="utf-8"))["status"] == "failed"

    successor_dir = find_run_directory(created.recovery_run_id)
    successor = load_run_state(successor_dir / "state.json")
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.recovery is not None
    assert successor.recovery.recovered_checkpoint == "publication_pre_commit"
    assert successor.recovery.reason_code == "publication_pre_commit_interrupted"
    assert successor.recovery.source_staged_patch_sha256 == patch_sha
    assert successor.cursor.chat_id == chat_id
    assert successor.codex.session_id == REVIEWER_B
    assert successor.controller is not None
    assert successor.controller.controller_session_id == CONTROLLER_A
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.lifecycle == "publishing_external_fix"
    assert successor.github_pr_review.publication_phase == "pre_commit"
    assert successor.github_pr_review.staged_patch_sha256 == patch_sha
    assert (successor_dir / "github/cycles/02/publication-text.json").is_file()
    assert (successor_dir / "codex/reviews/01.json").is_file()
    assert created.resume_command == (
        f"ai_dev_loop pr-review resume {created.recovery_run_id} "
        f"--controller-session-id {CONTROLLER_A}"
    )


def test_publication_pre_commit_resume_publishes_only(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, chat_id, bound_sha, patch_sha = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]

    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id

    publish_calls: list[str] = []
    workflow_resume_calls: list[str] = []

    def fake_publish(*_a, **_k):
        publish_calls.append("publish")
        return PublishResult(
            commit_sha="c" * 40,
            remote_name="origin",
            remote_ref="origin/" + state.repository.branch,
            staged_patch_sha256=patch_sha,
            expected_remote_sha_before_push=bound_sha,
            resumed_existing_commit=False,
        )

    def fake_resolve(*_a, **_k):
        result = MagicMock()
        result.ok = True
        result.error = None
        return result

    advanced_pr = _open_pr(state.repository.branch, head_sha="c" * 40)
    fake_config = MagicMock()
    fake_config.github.command = "gh"
    fake_config.github.reviewer_logins = ["chatgpt-codex-connector"]
    fake_config.github.pr_base = "master"
    fake_config.github.review_trigger_body = "@codex review"
    fake_config.github.poll_timeout_hours = 1
    fake_config.github.poll_interval_seconds = 1

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config", return_value=fake_config),
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=advanced_pr),
        patch(
            "ai_dev_loop.commands.pr_review.list_review_threads",
            return_value=threads,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.filter_eligible_threads",
            side_effect=lambda items, **kwargs: list(items),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.publish_accepted_staged_patch",
            side_effect=fake_publish,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
            side_effect=lambda *_a, **_k: None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.run_codex_publication_text",
            side_effect=AssertionError("must reuse durable publication text"),
        ),
        patch(
            "ai_dev_loop.workflow_engine.resume_run",
            side_effect=lambda *_a, **_k: workflow_resume_calls.append("workflow"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            side_effect=fake_resolve,
        ),
        patch(
            "ai_dev_loop.commands.pr_review._ensure_review_trigger_comment",
            return_value=("99", "2026-07-18T22:00:00+00:00"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.discover_repository",
            side_effect=lambda root: MagicMock(
                head="c" * 40,
                branch=state.repository.branch,
                status_porcelain="",
                staged_paths=[],
            ),
        ),
    ):
        from ai_dev_loop.commands.pr_review import _publish_external_fix

        message = resume_pr_review_cycle(successor_id, controller_session_id=CONTROLLER_A)
        assert "Resumed publication" in message
        successor_dir = find_run_directory(successor_id)
        successor = load_run_state(successor_dir / "state.json")
        assert successor.status == RunStatus.PUBLISHING_EXTERNAL_FIX
        assert successor.github_pr_review is not None
        assert successor.github_pr_review.lifecycle == "publishing_external_fix"
        assert successor.github_pr_review.publication_phase == "pre_commit"
        assert successor.cursor.chat_id == chat_id

        _publish_external_fix(
            successor_dir,
            successor,
            fake_config,
            schedule_worker=False,
        )

    assert publish_calls == ["publish"]
    assert workflow_resume_calls == []
    final = load_run_state(find_run_directory(successor_id) / "state.json")
    assert final.github_pr_review is not None
    assert final.github_pr_review.publication_phase is None
    assert final.status == RunStatus.AWAITING_BOT_REVIEW


def test_publication_pre_commit_accepts_carried_forward_bound_commit_shas(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Historical PR #45 shape: non-null commit SHAs equal bound/current HEAD."""

    run_path, run_id, chat_id, bound_sha, patch_sha = _seed_failed_publication_pre_commit_run(
        prepared_run,
        fake_clis,
        monkeypatch,
        carried_forward_commit_shas=True,
    )
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    assert state.github_pr_review.local_commit_sha == bound_sha
    assert state.github_pr_review.publication_commit_sha == bound_sha
    assert state.github_pr_review.publication_phase == "pre_commit"
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
        assert dry.analysis.eligible is True
        assert dry.analysis.checkpoint == "publication_pre_commit"
        assert dry.analysis.staged_patch_sha256 == patch_sha
        created = recover_pr_review_cycle(run_id, dry_run=False)

    assert (run_path / "state.json").read_bytes() == source_before
    assert created.recovery_run_id is not None
    successor = load_run_state(find_run_directory(created.recovery_run_id) / "state.json")
    assert successor.cursor.chat_id == chat_id
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.publication_phase == "pre_commit"
    assert successor.github_pr_review.lifecycle == "publishing_external_fix"


def test_publication_pre_commit_rejects_recorded_new_commit_sha(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recorded commit SHA past the bound head is not carried-forward."""

    run_path, run_id, _chat, bound_sha, _patch = _seed_failed_publication_pre_commit_run(
        prepared_run,
        fake_clis,
        monkeypatch,
        carried_forward_commit_shas=True,
    )
    new_commit = "c" * 40
    assert new_commit != bound_sha
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "local_commit_sha": new_commit,
            "publication_commit_sha": new_commit,
        }
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
        with pytest.raises(ValidationError):
            recover_pr_review_cycle(run_id, dry_run=False)
    assert analysis.eligible is False
    assert analysis.checkpoint != "publication_pre_commit" or (
        "local_commit_diverged_from_bound" in analysis.blockers
        or "publication_commit_diverged_from_bound" in analysis.blockers
    )
    assert (run_path / "state.json").read_bytes() == source_before


def test_publication_pre_commit_rejects_committed_phase(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _patch = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "publication_phase": "committed",
            "local_commit_sha": "c" * 40,
        }
    )
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert analysis.checkpoint != "publication_pre_commit" or analysis.eligible is False
    assert (run_path / "state.json").read_bytes() == source_before


def test_publication_pre_commit_rejects_actionable_local_review(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _patch = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    review_path = run_path / "codex/reviews/01.json"
    review_path.write_text(
        json.dumps(
            {
                "has_actionable_findings": True,
                "findings_count": 1,
                "highest_severity": "P2",
                "review_markdown": "needs fix",
                "cursor_fix_prompt": "fix it",
                "tests_status": "not_applicable",
                "summary": "actionable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
    assert analysis.eligible is False
    assert "local_review_has_actionable_findings" in analysis.blockers or (
        analysis.checkpoint != "publication_pre_commit"
    )


def test_publication_pre_commit_rejects_patch_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _patch = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    repo = Path(str(prepared_run["repo"]))
    (repo / "drift.txt").write_text("drift\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "drift.txt"], cwd=repo)
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
        with pytest.raises(ValidationError):
            recover_pr_review_cycle(run_id, dry_run=False)
    assert analysis.eligible is False
    assert (
        "staged_patch_artifact_drift" in analysis.blockers
        or "staged_patch_drift" in analysis.blockers
        or "staged_baseline_invalid" in analysis.blockers
    )
    assert (run_path / "state.json").read_bytes() == source_before


def test_publication_pre_commit_help_lists_all_checkpoints() -> None:
    result = runner.invoke(app, ["pr-review", "recover", "--help"])
    assert result.exit_code == 0
    help_text = result.stdout
    assert "external_adjudication" in help_text
    assert "reviewing" in help_text
    assert "external_feedback_cursor" in help_text
    assert "publication_pre_commit" in help_text


def test_publication_pre_commit_cli_dry_run_json(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _patch = _seed_failed_publication_pre_commit_run(
        prepared_run, fake_clis, monkeypatch
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        result = runner.invoke(
            app,
            ["pr-review", "recover", run_id, "--dry-run", "--output", "json"],
        )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["checkpoint"] == "publication_pre_commit"
    assert payload["reason_code"] == "publication_pre_commit_interrupted"
    assert payload["eligible"] is True
    assert payload["trigger_will_be_reposted"] is False
    assert "ssh-add" not in result.stdout.lower()
    assert (run_path / "state.json").read_bytes() == source_before
