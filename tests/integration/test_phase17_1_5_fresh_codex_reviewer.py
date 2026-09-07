"""Integration tests for Phase 17.1.5 fresh Codex reviewer bootstrap."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO

from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.commands.recover import recover_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError, ValidationError
from ai_dev_loop.fresh_codex_reviewer import (
    FRESH_REVIEWER_BINDING_ARTIFACT,
    FRESH_REVIEWER_BOOTSTRAP_UNCERTAINTY_ARTIFACT,
    FRESH_REVIEWER_INPUT_ARTIFACT,
)
from ai_dev_loop.recovery_planner import is_fresh_reviewer_bound
from ai_dev_loop.runners.codex import run_codex_review
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.state import RunStatus, load_run_state, save_run_state

CONTROLLER_ID = "019abc00-aaaa-0000-0000-0000000000aa"
REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING = "high"
BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
CONFLICTING_ID = "019def00-1111-1111-1111-0000000000cc"


def _prepare_controller(git_repo: Path):
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_ID,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
            )
        )


def _seed_iteration(run_directory: Path, state) -> None:
    state = state.model_copy(
        update={
            "status": RunStatus.REVIEWING,
            "iterations": [
                {
                    "number": 1,
                    "kind": "initial_implementation",
                    "started_at": state.created_at.isoformat(),
                    "cursor": {"chat_id": "chat-1"},
                    "git": {"staged_diff_path": "git/diffs/01.patch"},
                }
            ],
        }
    )
    save_run_state(run_directory, state)
    (run_directory / "cursor/iterations/01").mkdir(parents=True, exist_ok=True)
    (run_directory / "cursor/iterations/01/final.txt").write_text("done\n", encoding="utf-8")
    for rel in ("01.stat", "01.name-only.txt", "01.patch"):
        (run_directory / "git/diffs" / rel).write_text("artifact\n", encoding="utf-8")


def _write_streaming_stdout(kwargs: dict[str, object], stdout: str) -> None:
    stdout_path = kwargs.get("stdout_path")
    if isinstance(stdout_path, Path):
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stdout_path.write_text(stdout, encoding="utf-8")


def test_prepare_freezes_reviewer_without_session(
    git_repo: Path, isolated_xdg, isolated_home
) -> None:
    result = _prepare_controller(git_repo)
    state = load_run_state(result.run_directory / "state.json")
    assert state.schema_version == 2
    assert state.codex.session_id is None
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.review_model == REVIEW_MODEL
    assert (result.run_directory / FRESH_REVIEWER_INPUT_ARTIFACT).is_file()
    assert not (result.run_directory / FRESH_REVIEWER_BINDING_ARTIFACT).exists()
    manifest = json.loads((result.run_directory / "manifest.json").read_text(encoding="utf-8"))
    hashed_paths = {item["path"] for item in manifest["artifacts"]}
    assert FRESH_REVIEWER_INPUT_ARTIFACT in hashed_paths
    assert FRESH_REVIEWER_BINDING_ARTIFACT not in hashed_paths


def test_first_review_bootstraps_then_resumes_same_reviewer(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    prepared = _prepare_controller(git_repo)
    start_run(prepared.run_id)

    state = load_run_state(prepared.run_directory / "state.json")
    assert state.codex.session_id == BOOTSTRAP_ID
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.bootstrap_session_id == BOOTSTRAP_ID
    assert is_fresh_reviewer_bound(state.codex)

    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert "resume" not in codex_log
    assert "read-only" in codex_log
    events_path = prepared.run_directory / "codex/events/01.jsonl"
    assert events_path.is_file()
    events = events_path.read_text(encoding="utf-8")
    assert BOOTSTRAP_ID in events


def test_bootstrap_timeout_after_thread_started_persists_binding(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.process import StreamingProcessResult

    prepared = _prepare_controller(git_repo)
    state = load_run_state(prepared.run_directory / "state.json")
    state = state.model_copy(
        update={
            "codex": state.codex.model_copy(update={"command": "codex"}),
            "status": RunStatus.REVIEWING,
        }
    )
    _seed_iteration(prepared.run_directory, state)

    def timed_out_with_identity(args, **kwargs):  # type: ignore[no-untyped-def]
        stdout = json.dumps({"type": "thread.started", "thread_id": BOOTSTRAP_ID}) + "\n"
        _write_streaming_stdout(kwargs, stdout)
        return StreamingProcessResult(
            args=list(args),
            returncode=124,
            stdout=json.dumps({"type": "thread.started", "thread_id": BOOTSTRAP_ID}) + "\n",
            stderr="",
            timed_out=True,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr("ai_dev_loop.runners.codex.run_process_streaming", timed_out_with_identity)

    with pytest.raises(AiDevLoopError, match="timed out"):
        run_codex_review(state, prepared.run_directory, iteration="01")

    persisted = load_run_state(prepared.run_directory / "state.json")
    assert persisted.codex.session_id == BOOTSTRAP_ID
    assert persisted.codex.fresh_reviewer is not None
    assert persisted.codex.fresh_reviewer.bootstrap_session_id == BOOTSTRAP_ID
    assert (prepared.run_directory / FRESH_REVIEWER_BINDING_ARTIFACT).is_file()


def test_bootstrap_timeout_without_identity_persists_uncertainty(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.process import StreamingProcessResult

    prepared = _prepare_controller(git_repo)
    state = load_run_state(prepared.run_directory / "state.json")
    state = state.model_copy(
        update={
            "codex": state.codex.model_copy(update={"command": "codex"}),
            "status": RunStatus.REVIEWING,
        }
    )
    _seed_iteration(prepared.run_directory, state)

    def timed_out_without_identity(args, **kwargs):  # type: ignore[no-untyped-def]
        stdout = '{"type":"message"}\n'
        _write_streaming_stdout(kwargs, stdout)
        return StreamingProcessResult(
            args=list(args),
            returncode=124,
            stdout='{"type":"message"}\n',
            stderr="",
            timed_out=True,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr(
        "ai_dev_loop.runners.codex.run_process_streaming",
        timed_out_without_identity,
    )

    with pytest.raises(AiDevLoopError, match="identity capture failed"):
        run_codex_review(state, prepared.run_directory, iteration="01")

    persisted = load_run_state(prepared.run_directory / "state.json")
    assert persisted.codex.session_id is None
    assert persisted.codex.fresh_reviewer is not None
    assert persisted.codex.fresh_reviewer.bootstrap_uncertainty_reason == "timeout_without_identity"
    assert (prepared.run_directory / FRESH_REVIEWER_BOOTSTRAP_UNCERTAINTY_ARTIFACT).is_file()
    from ai_dev_loop.commands.start_preflight import validate_codex_session

    with pytest.raises(ValidationError, match="uncertain"):
        validate_codex_session(persisted)


def test_bootstrap_conflicting_events_persist_uncertainty(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.process import StreamingProcessResult

    prepared = _prepare_controller(git_repo)
    state = load_run_state(prepared.run_directory / "state.json")
    state = state.model_copy(
        update={
            "codex": state.codex.model_copy(update={"command": "codex"}),
            "status": RunStatus.REVIEWING,
        }
    )
    _seed_iteration(prepared.run_directory, state)
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": BOOTSTRAP_ID}),
            json.dumps({"type": "thread.started", "thread_id": CONFLICTING_ID}),
        ]
    )

    def conflicting_identity(args, **kwargs):  # type: ignore[no-untyped-def]
        _write_streaming_stdout(kwargs, stdout + "\n")
        return StreamingProcessResult(
            args=list(args),
            returncode=2,
            stdout=stdout + "\n",
            stderr="review failed\n",
            timed_out=False,
            elapsed_seconds=1.0,
        )

    monkeypatch.setattr("ai_dev_loop.runners.codex.run_process_streaming", conflicting_identity)

    with pytest.raises(AiDevLoopError, match="identity capture failed"):
        run_codex_review(state, prepared.run_directory, iteration="01")

    persisted = load_run_state(prepared.run_directory / "state.json")
    assert persisted.codex.fresh_reviewer is not None
    assert persisted.codex.fresh_reviewer.bootstrap_uncertainty_reason == "conflicting_identity"


def test_completed_run_bootstraps_only_once(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    prepared = _prepare_controller(git_repo)
    start_run(prepared.run_id)
    state = load_run_state(prepared.run_directory / "state.json")
    assert state.status == RunStatus.COMPLETED
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1
    assert is_fresh_reviewer_bound(state.codex)


def test_bound_fresh_reviewer_recovery_preserves_evidence(
    git_repo: Path,
    isolated_xdg,
    isolated_home,
    fake_clis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.run_discovery import load_run

    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    prepared = _prepare_controller(git_repo)
    with pytest.raises(AiDevLoopError, match="Codex review failed"):
        start_run(prepared.run_id)
    source = load_run_state(prepared.run_directory / "state.json")
    assert source.status == RunStatus.FAILED
    assert is_fresh_reviewer_bound(source.codex)

    recovered = recover_run(prepared.run_id)
    successor_path, successor = load_run(recovered.recovery_run_id)
    assert successor.codex.session_id == BOOTSTRAP_ID
    assert successor.codex.fresh_reviewer == source.codex.fresh_reviewer
    assert (successor_path / FRESH_REVIEWER_INPUT_ARTIFACT).is_file()
    binding = json.loads(
        (successor_path / FRESH_REVIEWER_BINDING_ARTIFACT).read_text(encoding="utf-8")
    )
    assert binding["review_model"] == REVIEW_MODEL
    assert binding["bootstrap_session_id_prefix"] == BOOTSTRAP_ID[:8]
    assert not (successor_path / "codex/session-runtime.json").exists()

    second = recover_run(prepared.run_id)
    assert second.recovery_run_id == recovered.recovery_run_id


def test_scheduler_submit_freezes_reviewer_without_session(
    git_repo: Path,
    isolated_xdg,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        result = submit_run(
            SubmitOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_ID,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
                db_path=scheduler_paths["db_path"],
                artifact_root=scheduler_paths["artifact_root"],
            )
        )
    assert result.state_kind == "queued"
    from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        state, _, _ = SqliteSchedulerStore(scheduler_paths["db_path"]).load_validated_snapshot(
            conn, result.run_id
        )
    assert state.context.schema_version == 2
    assert state.context.codex.review_model == REVIEW_MODEL


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }
