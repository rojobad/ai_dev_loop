"""Current scheduler workflow coverage retained from Phase 17.4.

The Phase 25.5 cutover retired new Cursor CLI execution. Cursor SDK workflow
coverage now lives in the Phase 25.2/25.3 suites; this module keeps the shared
helpers used by later integration tests and exercises the supported Codex-only
legacy-config path without reviving the old fake ``agent`` dispatch.
"""

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

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

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


def _submit(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    max_review_iterations: int | None = None,
) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        short_name="Test run",
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        implementer_provider="codex",
        implementer_command="codex",
        implementer_model="gpt-5.6-sol",
        implementer_reasoning_effort="high",
        implementer_timeout_minutes=90,
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        max_review_iterations=max_review_iterations,
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
        tick_owner_factory=lambda: f"tick-17-4-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 20,
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


def test_codex_only_v1_preflight_schedules_no_cursor_cli_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 8, 12, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
        ),
    )
    receipt = tick.run_once()
    assert any(item.action == "preflight_completed" for item in receipt.run_receipts)
    with tick.store.begin_read() as conn:
        effects = conn.execute(
            "SELECT effect_kind FROM scheduler_effects WHERE run_id = ?",
            (run_id,),
        ).fetchall()
    kinds = {str(row["effect_kind"]) for row in effects}
    assert "codex.implementer_bootstrap" in kinds
    assert "cursor.create_chat" not in kinds
    assert "cursor.run_turn" not in kinds


def test_new_v1_cursor_submission_rejects_before_publication(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        short_name="Retired Cursor CLI",
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="Cursor CLI execution is retired"),
    ):
        submit_run(options)
    with SqliteSchedulerStore(scheduler_paths["db_path"]).begin_read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == 0
    assert not scheduler_paths["artifact_root"].exists()


def test_codex_uses_frozen_command_after_path_changes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "tracked.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "019def00-0000-0000-0000-0000000000aa")
    run_id = _submit(git_repo, scheduler_paths)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 8, 12, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=30)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
    assert state.context.implementer is not None
    assert state.context.implementer.command == str((fake_clis["bin_dir"] / "codex").resolve())
