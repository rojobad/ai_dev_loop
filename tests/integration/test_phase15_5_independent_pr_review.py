"""Phase 15.5 independent PR-review prepare/start/model-selection coverage."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError as PydanticValidationError
from tests.conftest import DEFAULT_FIXTURE_SESSION_ID, FIXTURE_REPO, write_session_rollout
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.pr_review import (
    ensure_independent_cursor_chat,
    render_pr_review_status,
    run_pr_review_worker_loop,
)
from ai_dev_loop.commands.pr_review_independent import (
    IndependentPrReviewPrepareOptions,
    prepare_independent_pr_review,
    set_independent_cursor_model,
    start_independent_pr_review,
)
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.github_pr_review_result import GithubPrReviewResult
from ai_dev_loop.runners.codex_github import GithubAdjudicationArtifacts
from ai_dev_loop.runners.github import GithubPullRequest, GithubReviewThread, GithubWriteResult
from ai_dev_loop.runners.probes import CompatibilityClassification, ModelCompatibilityResult
from ai_dev_loop.state import (
    GithubPrReviewState,
    RunStatus,
    load_run_state,
    save_run_state,
    transition_status,
)

runner = CliRunner()
CONTROLLER_SESSION_ID = "019abc00-aaaa-bbbb-cccc-ddddeeeeffff"


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
        import subprocess

        subprocess.run(
            ["git", "add", "ai_dev_loop.yaml"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "enable github pr review"],
            cwd=repo,
            check=True,
            capture_output=True,
        )


def _head_sha(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _mock_pr(repo: Path, *, number: int = 42) -> GithubPullRequest:
    return GithubPullRequest(
        number=number,
        url=f"https://example.test/pr/{number}",
        title="Independent",
        state="OPEN",
        head_ref=_branch(repo),
        head_sha=_head_sha(repo),
        base_ref="master",
        is_cross_repository=False,
        repository_name_with_owner="acme/demo",
    )


@pytest.fixture
def independent_env(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Path | str]:
    _enable_github(git_repo)
    codex_home = isolated_home / ".codex"
    sessions = codex_home / "sessions"
    write_session_rollout(sessions)
    write_session_rollout(sessions, session_id=CONTROLLER_SESSION_ID)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return {"repo": git_repo, "xdg": isolated_xdg}


def _prepare_options(repo: Path, **overrides: object) -> IndependentPrReviewPrepareOptions:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    base = {
        "repo_path": repo,
        "pr_number": 42,
        "branch": _branch(repo),
        "plan_path": Path("docs/plans/sample-plan.md"),
        "prompt_source_path": Path("docs/plans/prompt_sample-plan.txt"),
        "codex_session_id": DEFAULT_FIXTURE_SESSION_ID,
    }
    base.update(overrides)
    options = IndependentPrReviewPrepareOptions(**base)  # type: ignore[arg-type]
    return options, prompt  # type: ignore[return-value]


def test_github_pr_review_origin_compatibility() -> None:
    legacy = GithubPrReviewState(
        source_run_id="src-run",
        lifecycle="awaiting_bot_review",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=1,
        head_branch="feature",
        bound_head_sha="a" * 40,
    )
    assert legacy.origin == "source_run"
    assert legacy.source_run_id == "src-run"

    independent = GithubPrReviewState(
        origin="independent_pr",
        lifecycle="prepared_independent",
        cycle_number=1,
        max_external_cycles=8,
        pr_number=7,
        repository_name_with_owner="acme/demo",
        head_branch="feature",
        bound_head_sha="b" * 40,
    )
    assert independent.source_run_id is None

    with pytest.raises(PydanticValidationError):
        GithubPrReviewState(
            origin="independent_pr",
            source_run_id="nope",
            lifecycle="prepared_independent",
            cycle_number=1,
            max_external_cycles=8,
            pr_number=7,
            repository_name_with_owner="acme/demo",
            head_branch="feature",
            bound_head_sha="b" * 40,
        )

    with pytest.raises(PydanticValidationError):
        GithubPrReviewState(
            origin="source_run",
            lifecycle="prepared_independent",
            cycle_number=1,
            max_external_cycles=8,
            pr_number=1,
            head_branch="feature",
            bound_head_sha="a" * 40,
        )


def test_prepared_to_awaiting_bot_review_transition() -> None:
    transition_status(RunStatus.PREPARED, RunStatus.AWAITING_BOT_REVIEW)


def test_independent_prepare_is_non_mutating(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    comment_writes = {"count": 0}

    def fail_comment(*_a, **_k):
        comment_writes["count"] += 1
        raise AssertionError("prepare must not post GitHub comments")

    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=fail_comment,
        ),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        result = prepare_independent_pr_review(options)

    assert result.reused_existing is False
    assert result.lifecycle == "prepared_independent"
    assert comment_writes["count"] == 0
    from ai_dev_loop.paths import run_dir

    run_path = run_dir(result.project, result.run_id)
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.PREPARED
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").exists()
    assert state.github_pr_review is not None
    assert state.github_pr_review.origin == "independent_pr"
    assert state.github_pr_review.source_run_id is None
    assert state.github_pr_review.pr_number == 42
    assert state.github_pr_review.bound_head_sha == pr.head_sha
    assert (run_path / "github" / "pr-binding.json").is_file()
    assert result.requires_codex_exit is True
    assert "pr-review start" in result.start_command


def test_independent_prepare_ab_routes_start_to_controller(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo, controller_session_id=CONTROLLER_SESSION_ID)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        result = prepare_independent_pr_review(options)

    assert result.reviewer_must_remain_inactive is True
    assert result.requires_codex_exit is False
    assert CONTROLLER_SESSION_ID in result.start_command
    assert "--controller-session-id" in result.start_command

    with pytest.raises(ValidationError, match="controller session id does not match"):
        start_independent_pr_review(result.run_id, controller_session_id=DEFAULT_FIXTURE_SESSION_ID)


def test_independent_prepare_rejects_head_mismatch(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    pr = GithubPullRequest(
        number=pr.number,
        url=pr.url,
        title=pr.title,
        state=pr.state,
        head_ref=pr.head_ref,
        head_sha="c" * 40,
        base_ref=pr.base_ref,
        is_cross_repository=False,
        repository_name_with_owner=pr.repository_name_with_owner,
    )
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="local HEAD does not match"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepare_independent_pr_review(options)


def test_independent_prepare_idempotent_reuse(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        first = prepare_independent_pr_review(options)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        second = prepare_independent_pr_review(options)
    assert second.reused_existing is True
    assert second.run_id == first.run_id


def test_set_cursor_model_before_chat(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo, cursor_model="auto")
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    compat = ModelCompatibilityResult(
        tool="cursor",
        command="agent",
        required_model="composer-2.5-fast",
        classification=CompatibilityClassification.COMPATIBLE,
        installed_version="1.0.0",
        detail="model available: composer-2.5-fast",
        catalog_source="agent_models",
    )
    with (
        patch(
            "ai_dev_loop.commands.pr_review_independent.probe_cursor_model_compatibility",
            return_value=compat,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.probe_version",
            return_value=type("V", (), {"version": "1.0.0", "ok": True})(),
        ),
    ):
        result = set_independent_cursor_model(prepared.run_id, cursor_model="composer-2.5-fast")
    state = load_run_state(run_path / "state.json")
    assert result.previous_model == "auto"
    assert result.new_model == "composer-2.5-fast"
    assert state.cursor.model == "composer-2.5-fast"
    artifact = json.loads(
        (run_path / "cursor" / "model-selection.json").read_text(encoding="utf-8")
    )
    assert artifact["current_model"] == "composer-2.5-fast"
    assert artifact["selections"][-1]["previous_model"] == "auto"
    effective = (run_path / "effective-config.yaml").read_text(encoding="utf-8")
    assert "auto" in effective or "cursor" in effective

    state.cursor.chat_id = "019abc00-1111-2222-3333-444444444444"
    save_run_state(run_path, state)
    with pytest.raises(ValidationError, match="immutable"):
        set_independent_cursor_model(prepared.run_id, cursor_model="auto")


def test_independent_start_posts_trigger_once(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    writes = {"count": 0}

    def create_comment(*_a, **_k):
        writes["count"] += 1
        return GithubWriteResult(ok=True, resource_id="99")

    compat = ModelCompatibilityResult(
        tool="cursor",
        command="agent",
        required_model="composer-2.5-fast",
        classification=CompatibilityClassification.COMPATIBLE,
        installed_version="1.0.0",
        detail="ok",
        catalog_source="agent_models",
    )
    codex_compat = ModelCompatibilityResult(
        tool="codex",
        command="codex",
        required_model="gpt-5.6-sol",
        classification=CompatibilityClassification.COMPATIBLE,
        installed_version="1.0.0",
        detail="ok",
        catalog_source="debug_models",
    )
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_upstream",
            return_value=("origin", _branch(repo)),
        ),
        patch("ai_dev_loop.commands.pr_review_independent.verify_ssh_push_ready"),
        patch(
            "ai_dev_loop.commands.pr_review_independent.run_tool_compatibility_probes",
            return_value=([compat, codex_compat], []),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=create_comment,
        ),
        patch("ai_dev_loop.commands.pr_review_independent._spawn_pr_review_worker"),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        started = start_independent_pr_review(prepared.run_id)

    assert started.already_running is False
    assert started.lifecycle == "awaiting_bot_review"
    assert writes["count"] == 1
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    assert state.github_pr_review.request_comment_id == "99"
    assert state.cursor.chat_id is None


def test_uncertain_independent_keeps_chat_null(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "request_comment_id": "1",
            "request_marker": "marker",
            "request_created_at": "2026-07-16T12:00:00+00:00",
        }
    )
    save_run_state(run_path, state)

    thread = GithubReviewThread(
        thread_id="THREAD1",
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="x.py",
        line=1,
        commit_sha=pr.head_sha,
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
                    "inline_reply": "@rojobad Need more context.",
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

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch(
            "ai_dev_loop.commands.pr_review.get_pull_request",
            return_value=pr,
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
                    "commit_sha": pr.head_sha,
                    "body": "why?",
                    "created_at": "2026-07-16T12:05:00+00:00",
                    "root_comment_id": "C1",
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
        patch(
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=lambda *_a, **_k: cursor_called.__setitem__("value", True),
        ),
    ):
        from ai_dev_loop.config import resolve_effective_config

        effective, _, _ = resolve_effective_config(repo_root=repo)
        cfg.return_value = effective
        run_pr_review_worker_loop(prepared.run_id)

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.WAITING_FOR_USER_ATTENTION
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").exists()
    assert cursor_called["value"] is False


def test_actionable_independent_creates_one_chat(
    independent_env: dict[str, Path | str],
    fake_clis: dict[str, Path],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "request_comment_id": "1",
            "request_marker": "marker",
            "request_created_at": "2026-07-16T12:00:00+00:00",
        }
    )
    save_run_state(run_path, state)

    fix_rel = "prompts/fixes/01.txt"
    (run_path / fix_rel).parent.mkdir(parents=True, exist_ok=True)
    (run_path / fix_rel).write_text("exact fix body\n", encoding="utf-8")

    thread = GithubReviewThread(
        thread_id="THREAD1",
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="x.py",
        line=1,
        commit_sha=pr.head_sha,
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
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "fix it",
                }
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "exact fix body",
            "tests_status": "not_applicable",
            "summary": "actionable",
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
        fix_prompt_path=fix_rel,
    )
    chats: list[str] = []

    def fake_create_chat(_command: str) -> str:
        chat_id = "019abc00-9999-8888-7777-666655554444"
        chats.append(chat_id)
        return chat_id

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review_independent.get_pull_request",
            return_value=pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
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
                    "commit_sha": pr.head_sha,
                    "body": "fix",
                    "created_at": "2026-07-16T12:05:00+00:00",
                    "root_comment_id": "C1",
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
        patch("ai_dev_loop.runners.cursor.create_chat", side_effect=fake_create_chat),
        patch("ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop"),
    ):
        from ai_dev_loop.config import resolve_effective_config

        effective, _, _ = resolve_effective_config(repo_root=repo)
        cfg.return_value = effective
        run_pr_review_worker_loop(prepared.run_id)

    state = load_run_state(run_path / "state.json")
    assert len(chats) == 1
    assert state.cursor.chat_id == chats[0]
    chat_payload = json.loads((run_path / "cursor" / "chat.json").read_text(encoding="utf-8"))
    assert chat_payload["chat_id"] == chats[0]
    assert state.github_pr_review is not None
    assert state.github_pr_review.lifecycle == "fixing_external_feedback"
    assert state.github_pr_review.lifecycle != "publishing_initial"

    # Second create attempt must reuse, not replace.
    chat2 = ensure_independent_cursor_chat(run_path, state)
    assert chat2 == chats[0]
    assert len(chats) == 1


def test_status_hides_sensitive_and_shows_origin(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    text = render_pr_review_status(prepared.run_id, output="json")
    payload = json.loads(text)
    assert payload["github_pr_review"]["origin"] == "independent_pr"
    assert payload["cursor_model_mutable"] is True
    assert payload["cursor_chat_present"] is False
    assert DEFAULT_FIXTURE_SESSION_ID not in text
    assert "cursor-initial" not in text or "snapshot" not in payload.get("result", "")


def test_cli_help_lists_new_commands() -> None:
    result = runner.invoke(app, ["pr-review", "--help"])
    assert result.exit_code == 0
    assert "prepare" in result.stdout
    assert "start" in result.stdout
    assert "set-cursor-model" in result.stdout
    assert "create" in result.stdout


def _compatible_pair() -> list[ModelCompatibilityResult]:
    return [
        ModelCompatibilityResult(
            tool="cursor",
            command="agent",
            required_model="composer-2.5-fast",
            classification=CompatibilityClassification.COMPATIBLE,
            installed_version="1.0.0",
            detail="ok",
            catalog_source="agent_models",
        ),
        ModelCompatibilityResult(
            tool="codex",
            command="codex",
            required_model="gpt-5.6-sol",
            classification=CompatibilityClassification.COMPATIBLE,
            installed_version="1.0.0",
            detail="ok",
            catalog_source="debug_models",
        ),
    ]


def test_start_spawn_failure_recovers_via_resume_without_duplicate_trigger(
    independent_env: dict[str, Path | str],
) -> None:
    from ai_dev_loop.commands.pr_review import resume_pr_review_cycle

    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    writes = {"count": 0}
    spawns = {"count": 0}

    def create_comment(*_a, **_k):
        writes["count"] += 1
        return GithubWriteResult(ok=True, resource_id="99")

    def fail_spawn(*_a, **_k):
        spawns["count"] += 1
        if spawns["count"] == 1:
            raise OSError("launcher failed")
        return None

    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_upstream",
            return_value=("origin", _branch(repo)),
        ),
        patch("ai_dev_loop.commands.pr_review_independent.verify_ssh_push_ready"),
        patch(
            "ai_dev_loop.commands.pr_review_independent.run_tool_compatibility_probes",
            return_value=(_compatible_pair(), []),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=None,
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=create_comment,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent._spawn_pr_review_worker",
            side_effect=fail_spawn,
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker", side_effect=fail_spawn),
        pytest.raises(ValidationError, match="worker failed to start"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        start_independent_pr_review(prepared.run_id)

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.INTERRUPTED
    assert state.github_pr_review is not None
    assert state.github_pr_review.lifecycle == "awaiting_bot_review"
    assert state.github_pr_review.request_comment_id == "99"
    assert writes["count"] == 1
    assert spawns["count"] == 1

    with (
        patch(
            "ai_dev_loop.commands.pr_review.find_issue_comment_with_marker",
            return_value=("99", "2026-07-17T00:00:00+00:00"),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=create_comment,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent._spawn_pr_review_worker",
            side_effect=fail_spawn,
        ),
        patch("ai_dev_loop.commands.pr_review._spawn_pr_review_worker", side_effect=fail_spawn),
    ):
        message = resume_pr_review_cycle(prepared.run_id)

    assert "Resumed PR-review polling" in message
    assert writes["count"] == 1
    assert spawns["count"] == 2
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    assert state.github_pr_review.request_comment_id == "99"


def test_pre_cursor_pr_head_drift_interrupts_without_chat(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    from ai_dev_loop.paths import run_dir

    run_path = run_dir(prepared.project, prepared.run_id)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "request_comment_id": "1",
            "request_marker": "marker",
            "request_created_at": "2026-07-16T12:00:00+00:00",
        }
    )
    save_run_state(run_path, state)

    fix_rel = "prompts/fixes/01.txt"
    (run_path / fix_rel).parent.mkdir(parents=True, exist_ok=True)
    (run_path / fix_rel).write_text("exact fix body\n", encoding="utf-8")

    drifted = GithubPullRequest(
        number=pr.number,
        url=pr.url,
        title=pr.title,
        state="OPEN",
        head_ref=pr.head_ref,
        head_sha="d" * 40,
        base_ref=pr.base_ref,
        is_cross_repository=False,
        repository_name_with_owner=pr.repository_name_with_owner,
    )
    thread = GithubReviewThread(
        thread_id="THREAD1",
        is_resolved=False,
        author_login="chatgpt-codex-connector",
        path="x.py",
        line=1,
        commit_sha=pr.head_sha,
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
                    "decision": "actionable",
                    "inline_reply": None,
                    "summary": "fix it",
                }
            ],
            "all_actionable": True,
            "review_markdown": "report",
            "cursor_fix_prompt": "exact fix body",
            "tests_status": "not_applicable",
            "summary": "actionable",
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
        fix_prompt_path=fix_rel,
    )
    chats: list[str] = []
    cursor_called = {"value": False}
    pr_views = {"count": 0}

    def get_pr(*_a, **_k):
        pr_views["count"] += 1
        return pr if pr_views["count"] == 1 else drifted

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch("ai_dev_loop.commands.pr_review.get_pull_request", side_effect=get_pr),
        patch(
            "ai_dev_loop.commands.pr_review_independent.get_pull_request",
            side_effect=get_pr,
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
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
                    "commit_sha": pr.head_sha,
                    "body": "fix",
                    "created_at": "2026-07-16T12:05:00+00:00",
                    "root_comment_id": "C1",
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
            "ai_dev_loop.runners.cursor.create_chat",
            side_effect=lambda *_a, **_k: (
                chats.append("x") or "019abc00-9999-8888-7777-666655554444"
            ),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=lambda *_a, **_k: cursor_called.__setitem__("value", True),
        ),
    ):
        from ai_dev_loop.config import resolve_effective_config

        effective, _, _ = resolve_effective_config(repo_root=repo)
        cfg.return_value = effective
        run_pr_review_worker_loop(prepared.run_id)

    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.INTERRUPTED
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").exists()
    assert chats == []
    assert cursor_called["value"] is False
    assert state.github_pr_review is not None
    assert state.github_pr_review.worker_outcome == "pre_cursor_binding_drift"


def test_start_fails_before_github_write_when_codex_incompatible(
    independent_env: dict[str, Path | str],
) -> None:
    repo = Path(str(independent_env["repo"]))
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    writes = {"count": 0}
    cursor_ok, _codex_ok = _compatible_pair()
    codex_bad = ModelCompatibilityResult(
        tool="codex",
        command="codex",
        required_model="gpt-5.6-sol",
        classification=CompatibilityClassification.INCOMPATIBLE_MODEL,
        installed_version="1.0.0",
        detail="model missing",
        catalog_source="debug_models",
    )

    def create_comment(*_a, **_k):
        writes["count"] += 1
        return GithubWriteResult(ok=True, resource_id="99")

    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_upstream",
            return_value=("origin", _branch(repo)),
        ),
        patch("ai_dev_loop.commands.pr_review_independent.verify_ssh_push_ready"),
        patch(
            "ai_dev_loop.commands.pr_review_independent.run_tool_compatibility_probes",
            return_value=([cursor_ok, codex_bad], []),
        ),
        patch(
            "ai_dev_loop.commands.pr_review.create_issue_comment",
            side_effect=create_comment,
        ),
        patch("ai_dev_loop.commands.pr_review_independent._spawn_pr_review_worker"),
        pytest.raises(ValidationError, match="tool compatibility preflight failed"),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        start_independent_pr_review(prepared.run_id)

    assert writes["count"] == 0
    from ai_dev_loop.paths import run_dir

    state = load_run_state(run_dir(prepared.project, prepared.run_id) / "state.json")
    assert state.status == RunStatus.PREPARED
    assert state.github_pr_review is not None
    assert state.github_pr_review.lifecycle == "prepared_independent"
    assert state.github_pr_review.request_comment_id is None


def test_independent_no_findings_completes_without_cursor(
    independent_env: dict[str, Path | str],
    fake_clis: dict[str, Path],
) -> None:
    from ai_dev_loop.config import load_project_config, resolve_effective_config
    from ai_dev_loop.paths import run_dir
    from ai_dev_loop.runners.github import (
        GithubIssueCommentDetail,
        NoFindingsCompletionMatch,
    )
    from ai_dev_loop.state import sha256_text

    repo = Path(str(independent_env["repo"]))
    config = repo / "ai_dev_loop.yaml"
    text = config.read_text(encoding="utf-8")
    if "no_findings_completion:" not in text:
        config.write_text(
            text + "\n  acknowledgement:\n    enabled: true\n"
            "  no_findings_completion:\n    enabled: true\n"
            "    accepted_comment_prefixes:\n"
            '      - "Codex Review: Didn\'t find any major issues."\n'
            "    reviewed_commit_prefix_length: 12\n",
            encoding="utf-8",
        )
        import subprocess

        subprocess.run(
            ["git", "add", "ai_dev_loop.yaml"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "enable no-findings completion"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
    options, prompt = _prepare_options(repo)
    pr = _mock_pr(repo)
    with (
        patch("ai_dev_loop.commands.pr_review_independent.check_gh_auth") as auth,
        patch(
            "ai_dev_loop.commands.pr_review_independent.resolve_repository_nwo",
            return_value="acme/demo",
        ),
        patch("ai_dev_loop.commands.pr_review_independent.get_pull_request", return_value=pr),
        patch("sys.stdin", StringIO(prompt)),
    ):
        auth.return_value.authenticated = True
        auth.return_value.detail = "ok"
        prepared = prepare_independent_pr_review(options)

    run_path = run_dir(prepared.project, prepared.run_id)
    state = load_run_state(run_path / "state.json")
    state.status = RunStatus.AWAITING_BOT_REVIEW
    assert state.github_pr_review is not None
    head = pr.head_sha
    body = f"Codex Review: Didn't find any major issues.\n\n**Reviewed commit:** `{head[:12]}`\n"
    state.github_pr_review = state.github_pr_review.model_copy(
        update={
            "lifecycle": "awaiting_bot_review",
            "request_comment_id": "1",
            "request_created_at": "2026-07-16T12:00:00+00:00",
            "bound_head_sha": head,
        }
    )
    # Independent prepare binds local HEAD to the PR head; keep them aligned.
    state.repository = state.repository.model_copy(update={"initial_head": head})
    save_run_state(run_path, state)
    match = NoFindingsCompletionMatch(
        comment_id="55",
        created_at="2026-07-16T12:10:00+00:00",
        body_sha256=sha256_text(body),
        rule_id="accepted_comment_prefix:0",
        reviewed_commit_prefix=head[:12],
    )
    cursor_called = {"value": False}

    with (
        patch("ai_dev_loop.commands.pr_review._require_github_config") as cfg,
        patch("ai_dev_loop.commands.pr_review.get_pull_request", return_value=pr),
        patch("ai_dev_loop.commands.pr_review.list_review_threads", return_value=[]),
        patch("ai_dev_loop.commands.pr_review.filter_eligible_threads", return_value=[]),
        patch(
            "ai_dev_loop.commands.pr_review.list_issue_comment_details",
            return_value=[
                GithubIssueCommentDetail(
                    comment_id="55",
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
            "ai_dev_loop.commands.pr_review.resume_legacy_pr_local_fix_loop",
            side_effect=lambda *_a, **_k: cursor_called.__setitem__("value", True),
        ),
    ):
        effective, _, _ = resolve_effective_config(repo_root=repo)
        cfg.return_value = effective
        run_pr_review_worker_loop(prepared.run_id)

    final = load_run_state(run_path / "state.json")
    assert final.status == RunStatus.COMPLETED
    assert final.cursor.chat_id is None
    assert cursor_called["value"] is False
    assert final.github_pr_review is not None
    assert final.github_pr_review.lifecycle == "completed"
    assert final.github_pr_review.no_findings_completion is not None
    assert not (run_path / "cursor" / "chat.json").exists()
    load_project_config(repo / "ai_dev_loop.yaml")
