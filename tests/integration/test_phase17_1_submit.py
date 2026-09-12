"""Integration tests for Phase 17.1 scheduler submit boundary."""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TypeVar
from unittest.mock import patch

import pytest
from tests.conftest import (
    FIXTURE_REPO,
)
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase17_6_abort_corrections import (
    _persist_abort_cancellation_without_cleanup,
)
from tests.unit.scheduler.test_tick import FakeAgentProcessBackend, FakeGitAdmissionPort

import ai_dev_loop.scheduler.application.submission as submission_module
from ai_dev_loop.commands.scheduler import render_submit_output
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.paths import runs_dir
from ai_dev_loop.scheduler.application.abort import scheduler_abort_run
from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    SchedulerEngineError,
    SubmitResult,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.application.submission import (
    SubmissionService,
    SubmitOptions,
    submit_run,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.common import worktree_key
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root, safe_run_directory_key
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

RESUBMISSION_ID = "22222222-2222-2222-2222-222222222222"
OTHER_RESUBMISSION_ID = "33333333-3333-3333-3333-333333333333"


def _active_reservation_run_id(
    store: SqliteSchedulerStore,
    conn: object,
    git_repo: Path,
) -> str | None:
    row = store.get_active_reservation(conn, worktree_key(str(git_repo.resolve())))  # type: ignore[arg-type]
    if row is None:
        return None
    return str(row["run_id"])


def _fake_repo_target(repo: Path) -> RepositoryTarget:
    return RepositoryTarget(root=repo.resolve())


def _submit_options(
    repo: Path,
    *,
    db_path: Path,
    artifact_root: Path,
    controller_session_id: str = CONTROLLER_SESSION,
) -> SubmitOptions:
    return SubmitOptions(
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=controller_session_id,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=db_path,
        artifact_root=artifact_root,
    )


def _submission_service(
    *,
    db_path: Path,
    artifact_root: Path,
    repo: Path,
) -> SubmissionService:
    return SubmissionService(
        SqliteSchedulerStore(db_path),
        ProtectedArtifactStore(artifact_root),
        repository_discoverer=lambda _path: _fake_repo_target(repo),
    )


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    del fake_clis
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


@pytest.fixture
def codex_env(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduler submit no longer reads Codex session rollouts at submission time."""

    return None


def test_submit_is_side_effect_free(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))

    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after
    assert not runs_dir().exists()
    assert scheduler_paths["db_path"].exists()
    assert (scheduler_paths["artifact_root"] / "runs").exists()
    assert result.state_kind == "queued"
    assert result.safe_next_action.command == f"ai_dev_loop scheduler start {result.run_id}"
    assert "--controller-session-id" not in result.safe_next_action.command


def test_identical_submit_reuses_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        first = service.submit(_submit_options(git_repo, **scheduler_paths))
    with patch("sys.stdin", StringIO(prompt)):
        second = service.submit(_submit_options(git_repo, **scheduler_paths))
    assert first.run_id == second.run_id
    assert second.reused_existing is True
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 1


def test_submit_freezes_resolved_scheduler_executables(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        submitted = submit_run(_submit_options(git_repo, **scheduler_paths))

    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn,
            submitted.run_id,
        )
    assert state.context.cursor.command == str((fake_clis["bin_dir"] / "agent").resolve())
    assert state.context.codex.command == str((fake_clis["bin_dir"] / "codex").resolve())

    run_root = run_artifact_root(scheduler_paths["artifact_root"], submitted.run_id)
    source_config = (run_root / "source-config.yaml").read_text(encoding="utf-8")
    effective_config = (run_root / "effective-config.yaml").read_text(encoding="utf-8")
    assert "command: agent" in source_config
    assert "command: codex" in source_config
    assert state.context.cursor.command in effective_config
    assert state.context.codex.command in effective_config


def test_submit_rejects_an_unresolvable_scheduler_executable(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = replace(
        _submit_options(git_repo, **scheduler_paths),
        cursor_command="definitely-missing-cursor-command",
    )

    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="Cursor executable not found while freezing"),
    ):
        submit_run(options)

    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 0


def test_conflicting_worktree_rejected(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        service.submit(_submit_options(git_repo, **scheduler_paths))
    with (
        patch("sys.stdin", StringIO(prompt + "\n")),
        pytest.raises(SchedulerEngineError, match="active scheduler reservation"),
    ):
        service.submit(_submit_options(git_repo, **scheduler_paths))


def test_status_and_list_are_redacted(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    from ai_dev_loop.commands.scheduler import render_list_output, render_status_output
    from ai_dev_loop.scheduler.application.status import scheduler_list, scheduler_status

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(_submit_options(git_repo, **scheduler_paths))

    status = scheduler_status(submitted.run_id, db_path=scheduler_paths["db_path"])
    listings = scheduler_list(db_path=scheduler_paths["db_path"])
    status_text = render_status_output(status, output="text")
    list_text = render_list_output(listings, output="text")
    # full session IDs must not appear in default text output
    assert CONTROLLER_SESSION not in status_text
    assert CONTROLLER_SESSION not in list_text
    assert CONTROLLER_SESSION[:8] in status_text
    status_json = json.loads(render_status_output(status, output="json"))
    assert CONTROLLER_SESSION not in json.dumps(status_json)
    assert status_json["summary"]["state_kind"] == "queued"
    assert status_json["summary"]["reviewer_session_id_prefix"] is None


def test_cli_help_lists_scheduler_submit() -> None:
    result = subprocess.run(
        ["uv", "run", "ai_dev_loop", "scheduler", "--help"],
        check=False,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0
    assert "submit" in result.stdout


def test_submit_ignores_worktree_git_state_and_writes_no_baseline(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = git_repo / "ai_dev_loop.yaml"
    config_text = config_path.read_text(encoding="utf-8").replace(
        "require_clean_worktree: true",
        "require_clean_worktree: false",
    )
    config_path.write_text(config_text, encoding="utf-8")
    (git_repo / "dirty-untracked.txt").write_text("dirty\n", encoding="utf-8")
    tracked = git_repo / "docs/plans/sample-plan.md"
    tracked.write_text(tracked.read_text(encoding="utf-8") + "\nstaged edit\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "docs/plans/sample-plan.md"], cwd=git_repo, check=True, capture_output=True
    )

    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")

    def forbid_subprocess(*args: object, **kwargs: object) -> None:
        raise AssertionError("scheduler submit must not invoke subprocesses")

    monkeypatch.setattr("ai_dev_loop.process.run_process", forbid_subprocess)
    monkeypatch.setattr("ai_dev_loop.process.run_process_bytes", forbid_subprocess)
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(_submit_options(git_repo, **scheduler_paths))

    baseline_path = (
        run_artifact_root(scheduler_paths["artifact_root"], result.run_id)
        / "git"
        / "baseline-status.txt"
    )
    assert not baseline_path.exists()
    assert result.state_kind == "queued"
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn,
            result.run_id,
        )
    assert state.context.schema_version == 3
    assert state.context.workflow.require_clean_worktree is False
    assert state.context.baseline_status_artifact_path is None
    assert state.context.baseline_status_sha256 is None


def test_identical_submit_after_abort_reports_terminal_state_without_start(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(options)
    scheduler_abort_run(
        submitted.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )
    with patch("sys.stdin", StringIO(prompt)):
        replay = service.submit(options)

    assert replay.run_id == submitted.run_id
    assert replay.reused_existing is True
    assert replay.state_kind == "aborted"
    assert replay.safe_next_action.kind is SafeNextActionKind.NONE
    assert replay.safe_next_action.command is None
    assert "scheduler start" not in (replay.safe_next_action.command or "")

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        assert _active_reservation_run_id(store, conn, git_repo) is None
    assert count == 1


def test_explicit_resubmission_after_abort_creates_distinct_queued_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    base_options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        source = service.submit(base_options)
    scheduler_abort_run(
        source.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )

    fresh_options = replace(base_options, resubmission_id=RESUBMISSION_ID)
    with patch("sys.stdin", StringIO(prompt)):
        fresh = service.submit(fresh_options)

    assert fresh.run_id != source.run_id
    assert fresh.reused_existing is False
    assert fresh.state_kind == "queued"
    assert fresh.safe_next_action.kind is SafeNextActionKind.SCHEDULER_START
    assert "scheduler start" in (fresh.safe_next_action.command or "")

    start = start_run(
        fresh.run_id,
        db_path=scheduler_paths["db_path"],
    )
    assert start.state_kind == "authorized"

    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
        source_state, _, _ = SqliteSchedulerStore(
            scheduler_paths["db_path"]
        ).load_validated_snapshot(
            conn,
            source.run_id,
        )
    assert count == 2
    assert source_state.kind == "aborted"


def test_resubmission_replay_is_idempotent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    base_options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        source = service.submit(base_options)
    scheduler_abort_run(
        source.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )

    fresh_options = replace(base_options, resubmission_id=RESUBMISSION_ID)
    with patch("sys.stdin", StringIO(prompt)):
        first = service.submit(fresh_options)
    with patch("sys.stdin", StringIO(prompt)):
        second = service.submit(fresh_options)

    assert first.run_id == second.run_id
    assert second.reused_existing is True
    assert second.state_kind == "queued"
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 2


def test_different_resubmission_id_conflicts_while_fresh_run_is_active(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    base_options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        source = service.submit(base_options)
    scheduler_abort_run(
        source.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )
    with patch("sys.stdin", StringIO(prompt)):
        service.submit(replace(base_options, resubmission_id=RESUBMISSION_ID))
    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(SchedulerEngineError, match="active scheduler reservation"),
    ):
        service.submit(replace(base_options, resubmission_id=OTHER_RESUBMISSION_ID))


def test_invalid_resubmission_id_rejected_before_writes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = replace(
        _submit_options(git_repo, **scheduler_paths),
        resubmission_id="not-a-valid-resubmission-id",
    )
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="--resubmission-id must be a UUID"),
    ):
        service.submit(options)
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 0


def test_submit_output_redacts_resubmission_id_and_matches_state(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    base_options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        source = service.submit(base_options)
    scheduler_abort_run(
        source.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )

    fresh_options = replace(base_options, resubmission_id=RESUBMISSION_ID)
    with patch("sys.stdin", StringIO(prompt)):
        fresh = service.submit(fresh_options)

    json_text = render_submit_output(fresh, output="json")
    text_output = render_submit_output(fresh, output="text")
    payload = json.loads(json_text)

    assert payload["status"] == fresh.state_kind
    assert payload["state_kind"] == fresh.state_kind
    assert RESUBMISSION_ID not in json_text
    assert RESUBMISSION_ID not in text_output
    assert CONTROLLER_SESSION not in json_text
    assert CONTROLLER_SESSION not in text_output
    assert prompt.strip() not in json_text
    assert prompt.strip() not in text_output

    with patch("sys.stdin", StringIO(prompt)):
        replay = service.submit(base_options)
    replay_json = json.loads(render_submit_output(replay, output="json"))
    assert replay_json["status"] == "aborted"
    assert replay_json["state_kind"] == "aborted"
    assert replay_json["safe_next_action"]["kind"] == SafeNextActionKind.NONE.value


def test_abort_preserves_terminal_source_and_git_repo(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    before = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(options)
    source_artifact_root = run_artifact_root(scheduler_paths["artifact_root"], submitted.run_id)
    source_plan = (source_artifact_root / "plan/plan.md").read_bytes()

    scheduler_abort_run(
        submitted.run_id,
        db_path=scheduler_paths["db_path"],
        backend=FakeAgentProcessBackend(),
    )

    with patch("sys.stdin", StringIO(prompt)):
        service.submit(replace(options, resubmission_id=RESUBMISSION_ID))

    after = {path.relative_to(git_repo) for path in git_repo.rglob("*") if path.is_file()}
    assert before == after
    assert (source_artifact_root / "plan/plan.md").read_bytes() == source_plan

    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        source_state, _, _ = SqliteSchedulerStore(
            scheduler_paths["db_path"]
        ).load_validated_snapshot(
            conn,
            submitted.run_id,
        )
    assert source_state.kind == "aborted"


def _artifact_run_ids(artifact_root: Path) -> set[str]:
    runs_root = artifact_root / "runs"
    if not runs_root.exists():
        return set()
    return {path.name for path in runs_root.iterdir() if path.is_dir()}


def _assert_no_orphan_artifact_dirs(
    store: SqliteSchedulerStore,
    artifact_root: Path,
) -> None:
    artifact_dir_keys = _artifact_run_ids(artifact_root)
    with store.begin_read() as conn:
        run_ids = {
            str(row[0]) for row in conn.execute("SELECT run_id FROM scheduler_runs").fetchall()
        }
    expected_dir_keys = {safe_run_directory_key(run_id) for run_id in run_ids}
    assert artifact_dir_keys == expected_dir_keys


_T = TypeVar("_T")
_WORKER = threading.local()


def _instrument_store_begin_immediate(
    store: SqliteSchedulerStore,
    *,
    on_competitor_lock_attempt: Callable[[str], None],
) -> None:
    original_begin = store.begin_immediate

    @contextmanager
    def instrumented_begin_immediate() -> Iterator[object]:
        worker = getattr(_WORKER, "name", None)
        if worker in {"identical", "distinct"}:
            on_competitor_lock_attempt(worker)
        with original_begin() as conn:
            yield conn

    store.begin_immediate = instrumented_begin_immediate  # type: ignore[method-assign]


def _run_named_worker(
    name: str,
    action: Callable[[], _T],
    outcomes: dict[str, _T | BaseException],
) -> None:
    _WORKER.name = name
    try:
        outcomes[name] = action()
    except BaseException as exc:
        outcomes[name] = exc


def test_concurrent_submit_serializes_admission_without_orphan_artifacts(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths)
    distinct_options = replace(options, resubmission_id=OTHER_RESUBMISSION_ID)
    primary_admitted = threading.Event()
    release_primary = threading.Event()
    competitor_lock_attempted = {
        "identical": threading.Event(),
        "distinct": threading.Event(),
    }
    outcomes: dict[str, SubmitResult | SchedulerEngineError | BaseException] = {}

    def pause_after_admission() -> None:
        primary_admitted.set()
        assert release_primary.wait(timeout=10)

    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    _instrument_store_begin_immediate(
        store,
        on_competitor_lock_attempt=lambda worker: competitor_lock_attempted[worker].set(),
    )
    service = SubmissionService(
        store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        repository_discoverer=lambda _path: _fake_repo_target(git_repo),
        admission_pause_hook=pause_after_admission,
    )

    with patch.object(submission_module, "_read_stdin_prompt", return_value=prompt):
        primary = threading.Thread(
            target=_run_named_worker,
            args=("primary", lambda: service.submit(options), outcomes),
        )
        primary.start()
        assert primary_admitted.wait(timeout=10)

        identical = threading.Thread(
            target=_run_named_worker,
            args=("identical", lambda: service.submit(options), outcomes),
        )
        distinct = threading.Thread(
            target=_run_named_worker,
            args=("distinct", lambda: service.submit(distinct_options), outcomes),
        )
        identical.start()
        distinct.start()
        assert competitor_lock_attempted["identical"].wait(timeout=10)
        assert competitor_lock_attempted["distinct"].wait(timeout=10)

        release_primary.set()
        primary.join(timeout=10)
        identical.join(timeout=10)
        distinct.join(timeout=10)

    assert primary.is_alive() is False
    assert identical.is_alive() is False
    assert distinct.is_alive() is False

    primary_outcome = outcomes["primary"]
    identical_outcome = outcomes["identical"]
    distinct_outcome = outcomes["distinct"]
    assert isinstance(primary_outcome, SubmitResult)
    assert isinstance(identical_outcome, SubmitResult)
    assert isinstance(distinct_outcome, SchedulerEngineError)
    assert primary_outcome.run_id == identical_outcome.run_id
    assert primary_outcome.reused_existing is False
    assert identical_outcome.reused_existing is True
    assert "active scheduler reservation" in str(distinct_outcome)

    with store.begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 1
    _assert_no_orphan_artifact_dirs(store, scheduler_paths["artifact_root"])


def test_base_submit_replay_matches_status_for_pending_abort_cleanup(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(options)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    _persist_abort_cancellation_without_cleanup(store, submitted.run_id)

    status = scheduler_status(submitted.run_id, db_path=scheduler_paths["db_path"])
    with patch("sys.stdin", StringIO(prompt)):
        replay = service.submit(options)

    assert replay.state_kind == "aborted"
    assert replay.reused_existing is True
    assert replay.safe_next_action == status.summary.safe_next_action
    assert replay.safe_next_action.kind is SafeNextActionKind.SCHEDULER_TICK


def test_resubmission_blocked_before_abort_resource_cleanup(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths)
    fresh_options = replace(options, resubmission_id=RESUBMISSION_ID)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(options)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    _persist_abort_cancellation_without_cleanup(store, submitted.run_id)
    artifact_ids_before = _artifact_run_ids(scheduler_paths["artifact_root"])

    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(SchedulerEngineError, match="reservation still held by aborted run"),
    ):
        service.submit(fresh_options)

    artifact_ids_after = _artifact_run_ids(scheduler_paths["artifact_root"])
    assert artifact_ids_before == artifact_ids_after
    with store.begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 1
    assert RESUBMISSION_ID not in str(scheduler_paths["artifact_root"])


def test_resubmission_succeeds_after_abort_resource_cleanup(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    codex_env: None,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = _submit_options(git_repo, **scheduler_paths)
    fresh_options = replace(options, resubmission_id=RESUBMISSION_ID)
    service = _submission_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    with patch("sys.stdin", StringIO(prompt)):
        submitted = service.submit(options)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    _persist_abort_cancellation_without_cleanup(store, submitted.run_id)

    tick = TickService(
        store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 9, 12, 30, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-17-9-cleanup",
        attempt_backend=FakeAgentProcessBackend(),
    )
    receipt = tick.run_once()
    assert any(item.action == "abort_resource_cleanup" for item in receipt.run_receipts)

    with patch("sys.stdin", StringIO(prompt)):
        fresh = service.submit(fresh_options)
    with patch("sys.stdin", StringIO(prompt)):
        replay = service.submit(fresh_options)

    assert fresh.run_id != submitted.run_id
    assert fresh.state_kind == "queued"
    assert replay.run_id == fresh.run_id
    assert replay.reused_existing is True
    with store.begin_read() as conn:
        count = conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]
    assert count == 2
