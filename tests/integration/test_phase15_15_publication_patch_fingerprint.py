"""Phase 15.15: publication patch fingerprint after external correction + historical adopt."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ai_dev_loop.commands.pr_review import (
    refresh_external_publication_patch_fingerprint,
    resume_pr_review_cycle,
)
from ai_dev_loop.commands.pr_review_recover import (
    analyze_pr_review_recovery,
    recover_pr_review_cycle,
)
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.iterations import begin_external_local_review_budget
from ai_dev_loop.review_result import CodexReviewResult
from ai_dev_loop.run_discovery import find_run_directory
from ai_dev_loop.runners.git import normalize_patch_text
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread
from ai_dev_loop.runners.publish import (
    PublishResult,
    publication_staged_patch_fingerprint,
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
from ai_dev_loop.workflow_engine import _apply_review_result

CONTROLLER_A = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"
REVIEWER_B = "019abc00-0000-0000-0000-0000000000bb"
THREAD_A = "PRRT_PUB_A"
THREAD_B = "PRRT_PUB_B"
THREAD_C = "PRRT_PUB_C"
OBSOLETE_GPR_HASH = "b" * 64


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


def _seed_publication_pre_commit_with_iteration(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    obsolete_gpr_hash: bool = False,
    trailing_newline_mismatch: bool = False,
) -> tuple[Path, str, str, str, str, str]:
    """Incident-shaped source with iteration 02 artifact and optional obsolete GPR hash."""

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

    # Build a second-iteration staged patch (external correction shape).
    target = repo / "phase1515.txt"
    target.write_text("phase-15-15 external fix body\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "phase1515.txt"], cwd=repo)
    live_patch = read_staged_patch(repo)
    live_hash = sha256_text(live_patch)

    # Persist as iteration 02 durable staging + accepted local review.
    for label in ("01", "02"):
        (run_path / f"git/diffs/{label}.patch").parent.mkdir(parents=True, exist_ok=True)
    artifact_text = live_patch
    if trailing_newline_mismatch:
        # Artifact keeps a terminal newline that stripped live stdout does not.
        artifact_text = live_patch + "\n"
        assert artifact_text != live_patch
        assert normalize_patch_text(artifact_text) == normalize_patch_text(live_patch)
    (run_path / "git/diffs/02.patch").write_text(artifact_text, encoding="utf-8")
    artifact_file_hash = sha256_file(run_path / "git/diffs/02.patch")
    assert artifact_file_hash != live_hash or not trailing_newline_mismatch

    # Keep iteration 01 artifacts; add completed iteration 02 metadata.
    state.iterations = list(state.iterations or [])
    if not any(int(item.get("number", 0)) == 2 for item in state.iterations):
        state.iterations.append(
            {
                "number": 2,
                "kind": "cursor_correction",
                "cursor": {"prompt_path": "prompts/fixes/github-02.txt"},
                "git": {"staged_diff_path": "git/diffs/02.patch"},
                "codex": {"review_path": "codex/reviews/02.json"},
            }
        )
    cursor_iter = run_path / "cursor/iterations/02"
    cursor_iter.mkdir(parents=True, exist_ok=True)
    (cursor_iter / "metadata.json").write_text(
        json.dumps({"exit_code": 0, "timed_out": False}) + "\n",
        encoding="utf-8",
    )
    (run_path / "git/status").mkdir(parents=True, exist_ok=True)
    (run_path / "git/status/02-after-cursor.txt").write_text("", encoding="utf-8")
    (run_path / "git/status/02-after-stage.txt").write_text("", encoding="utf-8")

    review_payload = {
        "has_actionable_findings": False,
        "findings_count": 0,
        "highest_severity": None,
        "review_markdown": "ok",
        "cursor_fix_prompt": None,
        "tests_status": "not_applicable",
        "summary": "accepted",
    }
    (run_path / "codex/reviews/02.json").write_text(
        json.dumps(review_payload, indent=2) + "\n", encoding="utf-8"
    )
    (run_path / "codex/reviews/02.md").write_text("ok\n", encoding="utf-8")

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

    gpr_hash = OBSOLETE_GPR_HASH if obsolete_gpr_hash else live_hash
    thread_ids = [THREAD_A, THREAD_B, THREAD_C]
    state.status = RunStatus.FAILED
    state.codex.session_id = REVIEWER_B
    state.controller = ControllerState(controller_session_id=CONTROLLER_A)
    state.workflow = state.workflow.model_copy(update={"current_review_iteration": 2})
    state.last_error = "ssh-agent has no usable keys; preload the SSH key before publication"
    state.result = "publication failed before commit"
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
        staged_patch_sha256=gpr_hash,
        local_commit_sha=bound_sha,
        publication_commit_sha=bound_sha,
        worker_outcome="validation_error",
    )
    save_run_state(run_path, state)
    return run_path, state.run_id, chat_id, bound_sha, live_hash, gpr_hash


def test_publication_fingerprint_ignores_trailing_newline_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.check_call(["git", "init"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "t@example.test"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "t"], cwd=repo)
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "a.txt"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "base"], cwd=repo)
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "a.txt"], cwd=repo)
    live = read_staged_patch(repo)
    # Live fingerprint uses stripped process stdout; artifact may retain a terminal newline.
    artifact = tmp_path / "02.patch"
    artifact.write_text(live + "\n", encoding="utf-8")
    assert sha256_file(artifact) != sha256_text(live)
    assert normalize_patch_text(artifact.read_text(encoding="utf-8")) == normalize_patch_text(live)
    assert publication_staged_patch_fingerprint(repo, artifact) == sha256_text(live)


def test_external_correction_refreshes_gpr_patch_hash_before_publish(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    state = load_run_state(run_path / "state.json")

    (repo / "ext-fix.txt").write_text("external correction\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "ext-fix.txt"], cwd=repo)
    live_hash = sha256_text(read_staged_patch(repo))
    (run_path / "git/diffs").mkdir(parents=True, exist_ok=True)
    (run_path / "git/diffs/02.patch").write_text(read_staged_patch(repo), encoding="utf-8")

    state.status = RunStatus.REVIEWING
    state.workflow = state.workflow.model_copy(update={"current_review_iteration": 2})
    begin_external_local_review_budget(state)
    state.iterations = list(state.iterations or [])
    state.iterations.append(
        {
            "number": 2,
            "kind": "cursor_correction",
            "git": {"staged_diff_path": "git/diffs/02.patch"},
        }
    )
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=state.repository.initial_head,
        request_comment_id="1",
        request_marker="marker",
        request_created_at="2026-07-18T21:00:00+00:00",
        eligible_thread_ids=[THREAD_A],
        expected_eligible_thread_ids=[THREAD_A],
        staged_patch_sha256=OBSOLETE_GPR_HASH,
        external_cursor_iteration=2,
    )
    save_run_state(run_path, state)

    review = CodexReviewResult.model_validate(
        {
            "has_actionable_findings": False,
            "findings_count": 0,
            "highest_severity": None,
            "cursor_fix_prompt": None,
            "review_markdown": "ok",
            "tests_status": "passed",
            "summary": "accepted",
        }
    )
    with patch(
        "ai_dev_loop.commands.pr_review._spawn_pr_review_worker",
        side_effect=lambda *_a, **_k: None,
    ):
        _apply_review_result(
            run_path,
            state,
            iteration_number=2,
            review=review,
            review_artifact_path="codex/reviews/02.json",
        )

    assert state.status == RunStatus.PUBLISHING_EXTERNAL_FIX
    assert state.github_pr_review is not None
    assert state.github_pr_review.lifecycle == "publishing_external_fix"
    assert state.github_pr_review.staged_patch_sha256 == live_hash
    assert state.github_pr_review.staged_patch_sha256 != OBSOLETE_GPR_HASH
    persisted = load_run_state(run_path / "state.json")
    assert persisted.github_pr_review is not None
    assert persisted.github_pr_review.staged_patch_sha256 == live_hash


def test_refresh_fingerprint_rejects_real_content_drift(
    prepared_run: dict[str, Path | str],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_path = Path(str(prepared_run["run_path"]))
    repo = Path(str(prepared_run["repo"]))
    start_run(str(prepared_run["run_id"]))
    state = load_run_state(run_path / "state.json")
    (run_path / "git/diffs").mkdir(parents=True, exist_ok=True)
    (run_path / "git/diffs/02.patch").write_text("diff --git a/x b/x\n+old\n", encoding="utf-8")
    (repo / "drift.txt").write_text("new content\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "drift.txt"], cwd=repo)
    state.github_pr_review = GithubPrReviewState(
        origin="source_run",
        source_run_id="source-local",
        lifecycle="fixing_external_feedback",
        cycle_number=2,
        max_external_cycles=8,
        pr_number=45,
        head_branch=state.repository.branch,
        bound_head_sha=state.repository.initial_head,
        request_comment_id="1",
        request_marker="marker",
        request_created_at="2026-07-18T21:00:00+00:00",
        eligible_thread_ids=[THREAD_A],
        expected_eligible_thread_ids=[THREAD_A],
        staged_patch_sha256=OBSOLETE_GPR_HASH,
    )
    with pytest.raises(ValidationError, match="no longer matches"):
        refresh_external_publication_patch_fingerprint(run_path, state, iteration_number=2)


def test_historical_obsolete_gpr_hash_adopts_live_fingerprint(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        run_path,
        run_id,
        chat_id,
        bound_sha,
        live_hash,
        gpr_hash,
    ) = _seed_publication_pre_commit_with_iteration(
        prepared_run,
        fake_clis,
        monkeypatch,
        obsolete_gpr_hash=True,
        trailing_newline_mismatch=True,
    )
    assert gpr_hash == OBSOLETE_GPR_HASH
    assert gpr_hash != live_hash
    source_before = (run_path / "state.json").read_bytes()
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]

    with _mock_remote_pr(pr, threads):
        dry = recover_pr_review_cycle(run_id, dry_run=True)
        assert dry.analysis.eligible is True
        assert dry.analysis.checkpoint == "publication_pre_commit"
        assert dry.analysis.staged_patch_sha256 == live_hash
        assert dry.analysis.historical_patch_fingerprint_adopted is True
        assert "historical_publication_patch_fingerprint_adopted" in dry.analysis.warnings
        assert dry.recovery_run_id is None
        assert (run_path / "state.json").read_bytes() == source_before

        created = recover_pr_review_cycle(run_id, dry_run=False)
        reused = recover_pr_review_cycle(run_id, dry_run=False)

    assert created.recovery_run_id is not None
    assert reused.recovery_run_id == created.recovery_run_id
    assert reused.analysis.reused_existing_successor is True
    assert (run_path / "state.json").read_bytes() == source_before
    source_after = load_run_state(run_path / "state.json")
    assert source_after.github_pr_review is not None
    assert source_after.github_pr_review.staged_patch_sha256 == OBSOLETE_GPR_HASH

    successor = load_run_state(find_run_directory(created.recovery_run_id) / "state.json")
    assert successor.recovery is not None
    assert successor.recovery.source_staged_patch_sha256 == live_hash
    assert successor.github_pr_review is not None
    assert successor.github_pr_review.staged_patch_sha256 == live_hash
    assert successor.cursor.chat_id == chat_id
    assert successor.status == RunStatus.INTERRUPTED
    assert successor.github_pr_review.lifecycle == "publishing_external_fix"
    assert successor.github_pr_review.publication_phase == "pre_commit"


def test_historical_adoption_resume_publishes_only(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        run_path,
        run_id,
        chat_id,
        bound_sha,
        live_hash,
        _gpr,
    ) = _seed_publication_pre_commit_with_iteration(
        prepared_run,
        fake_clis,
        monkeypatch,
        obsolete_gpr_hash=True,
        trailing_newline_mismatch=True,
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
    assert created.recovery_run_id is not None
    successor_id = created.recovery_run_id

    publish_calls: list[str] = []
    workflow_calls: list[str] = []

    def fake_publish(*_a, **_k):
        publish_calls.append("publish")
        return PublishResult(
            commit_sha="c" * 40,
            remote_name="origin",
            remote_ref="origin/" + state.repository.branch,
            staged_patch_sha256=live_hash,
            expected_remote_sha_before_push=bound_sha,
            resumed_existing_commit=False,
        )

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
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=threads),
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
            side_effect=lambda *_a, **_k: workflow_calls.append("workflow"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resolve_review_thread",
            side_effect=lambda *_a, **_k: MagicMock(ok=True, error=None),
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
        assert successor.github_pr_review is not None
        assert successor.github_pr_review.staged_patch_sha256 == live_hash
        assert successor.cursor.chat_id == chat_id
        _publish_external_fix(
            successor_dir,
            successor,
            fake_config,
            schedule_worker=False,
        )

    assert publish_calls == ["publish"]
    assert workflow_calls == []
    final = load_run_state(find_run_directory(successor_id) / "state.json")
    assert final.status == RunStatus.AWAITING_BOT_REVIEW


def test_historical_adoption_rejects_real_content_drift(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _live, _gpr = _seed_publication_pre_commit_with_iteration(
        prepared_run,
        fake_clis,
        monkeypatch,
        obsolete_gpr_hash=True,
        trailing_newline_mismatch=True,
    )
    repo = Path(str(prepared_run["repo"]))
    (repo / "real-drift.txt").write_text("real content drift\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "real-drift.txt"], cwd=repo)
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
        with pytest.raises(ValidationError):
            recover_pr_review_cycle(run_id, dry_run=False)
    assert analysis.eligible is False
    assert "staged_patch_artifact_drift" in analysis.blockers
    assert analysis.historical_patch_fingerprint_adopted is False
    assert (run_path / "state.json").read_bytes() == source_before


def test_historical_adoption_rejects_missing_artifact(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, _live, _gpr = _seed_publication_pre_commit_with_iteration(
        prepared_run,
        fake_clis,
        monkeypatch,
        obsolete_gpr_hash=True,
        trailing_newline_mismatch=True,
    )
    (run_path / "git/diffs/02.patch").unlink()
    # Also clear the iteration pointer so staging_complete cannot soft-pass.
    state = load_run_state(run_path / "state.json")
    for entry in state.iterations or []:
        if int(entry.get("number", 0)) == 2:
            git_section = entry.get("git")
            if isinstance(git_section, dict):
                git_section.pop("staged_diff_path", None)
    save_run_state(run_path, state)
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    source_before = (run_path / "state.json").read_bytes()
    with _mock_remote_pr(pr, threads):
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
        with pytest.raises(ValidationError):
            recover_pr_review_cycle(run_id, dry_run=False)
    assert analysis.eligible is False
    assert analysis.historical_patch_fingerprint_adopted is False
    assert (
        "staged_patch_artifact_missing" in analysis.blockers
        or analysis.checkpoint != "publication_pre_commit"
    )
    assert (run_path / "state.json").read_bytes() == source_before


def test_idempotent_reuse_invalidated_by_real_index_change(
    prepared_run: dict[str, Path | str],
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_path, run_id, _chat, bound_sha, live_hash, _gpr = (
        _seed_publication_pre_commit_with_iteration(
            prepared_run,
            fake_clis,
            monkeypatch,
            obsolete_gpr_hash=True,
            trailing_newline_mismatch=True,
        )
    )
    state = load_run_state(run_path / "state.json")
    pr = _open_pr(state.repository.branch, head_sha=bound_sha)
    threads = [_thread(tid, commit_sha=bound_sha) for tid in (THREAD_A, THREAD_B, THREAD_C)]
    with _mock_remote_pr(pr, threads):
        created = recover_pr_review_cycle(run_id, dry_run=False)
        assert created.recovery_run_id is not None
        successor = load_run_state(find_run_directory(created.recovery_run_id) / "state.json")
        assert successor.recovery is not None
        assert successor.recovery.source_staged_patch_sha256 == live_hash

        repo = Path(str(prepared_run["repo"]))
        (repo / "after-recover-drift.txt").write_text("changed after recover\n", encoding="utf-8")
        subprocess.check_call(["git", "add", "after-recover-drift.txt"], cwd=repo)
        analysis = analyze_pr_review_recovery(run_id, verify_remote=True)
        with pytest.raises(ValidationError):
            recover_pr_review_cycle(run_id, dry_run=False)
    assert analysis.eligible is False
    assert "staged_patch_artifact_drift" in analysis.blockers
