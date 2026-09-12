"""Integration tests for Phase 17.5 scheduler Codex review loop."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
_ATTEMPT_COUNTER = itertools.count()


def _next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit(git_repo: Path, scheduler_paths: dict[str, Path], *, max_reviews: int = 3) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        max_review_iterations=max_reviews,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
    backend: FakeAgentProcessBackend,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-17-5-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 40,
) -> None:
    store = tick.store
    last_kind = ""
    for _ in range(max_ticks):
        receipt = tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            last_kind = state.kind
            if state.kind == target_kind:
                return
            if state.kind == "blocked":
                actions = [item.action for item in receipt.run_receipts]
                summary = getattr(state, "block_reason_summary", "")
                kind = getattr(state, "block_reason_kind", "")
                raise AssertionError(
                    f"run blocked before reaching {target_kind}; "
                    f"actions={actions} reason={kind}: {summary}"
                )
    raise AssertionError(
        f"did not reach {target_kind} within {max_ticks} ticks; last_state={last_kind}"
    )


def test_no_findings_completes_with_fresh_bootstrap_and_resume_argv(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit(git_repo, scheduler_paths)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 12, 0, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="completed")
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert "read-only" in codex_log
    assert "'resume'" not in codex_log
    assert "--last" not in codex_log
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID
        assert state.context.codex.command == str((fake_clis["bin_dir"] / "codex").resolve())


def test_findings_then_correction_then_no_findings(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 12, 30, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="completed", max_ticks=60)
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert codex_log.count("'resume'") >= 1
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1
    agent_log = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT:") == 1


def test_max_iterations_reached_without_extra_cursor(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 13, 0, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="max_iterations_reached", max_ticks=80)
    agent_log = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    prompt_runs = sum(
        1 for line in agent_log.splitlines() if line.startswith("ARGS:") and "'-p'" in line
    )
    assert prompt_runs == 2


def test_active_codex_attempt_tick_exits_without_wait(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "sleep")
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "30")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 14, 0, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=30)
    saw_active = False
    for _ in range(5):
        receipt = tick.run_once()
        if any(item.action == "attempt_active" for item in receipt.run_receipts):
            saw_active = True
            break
    assert saw_active, "tick never reported active codex attempt without waiting for completion"
