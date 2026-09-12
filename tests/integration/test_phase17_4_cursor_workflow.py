"""Integration tests for Phase 17.4 scheduler cursor workflow."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.cursor_retry_after import DEFAULT_USAGE_LIMIT_RETRY_SECONDS
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
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
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


def test_admit_and_preflight_without_legacy_fake_agent(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-17-4",
        attempt_id_factory=lambda: "att-" + "b" * 32,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    receipt = tick.run_once()
    assert any(item.action == "admitted" for item in receipt.run_receipts)
    assert any(item.action == "preflight_completed" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "preflight_complete"
        create_chat = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.create_chat' AND status = 'pending'
            """,
            (run_id,),
        ).fetchone()
        assert create_chat is not None


def test_create_chat_effect_pending_after_preflight(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-17-4-chat",
        attempt_id_factory=lambda: "att-" + "d" * 32,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    receipt = tick.run_once()
    assert any(item.action == "preflight_completed" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT effect_kind, status FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.create_chat'
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        assert str(row[0]) == "cursor.create_chat"
        assert str(row[1]) in {"pending", "claimed"}


def test_create_chat_execution_failure_is_not_reported_as_an_invalid_chat_id(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )

    for _ in range(8):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == "blocked":
            assert state.block_reason_kind == "cursor_chat_create_failed"
            assert (
                state.block_reason_summary
                == "Cursor chat creation failed before a chat ID was received"
            )
            return
    raise AssertionError("create-chat failure did not reach a blocked state")


def test_released_worktree_reservation_allows_a_fresh_submit(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
    blocked_run_id = _submit(git_repo, scheduler_paths)
    start_run(blocked_run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, blocked_run_id, target_kind="blocked", max_ticks=8)

    fresh_run_id = _submit(git_repo, scheduler_paths, max_review_iterations=8)

    assert fresh_run_id != blocked_run_id
    with tick.store.begin_read() as conn:
        fresh_state, _, _ = tick.store.load_validated_snapshot(conn, fresh_run_id)
        reservation = conn.execute(
            """
            SELECT run_id, status FROM scheduler_repository_reservations
            WHERE worktree_key = ?
            """,
            (fresh_state.context.repository.worktree_key,),
        ).fetchone()
    assert fresh_state.kind == "queued"
    assert reservation is not None
    assert str(reservation["run_id"]) == fresh_run_id
    assert str(reservation["status"]) == "active"


def test_happy_path_reaches_awaiting_codex_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(git_repo, scheduler_paths, now=now, backend=backend)
    _run_until(tick, run_id, target_kind="awaiting_codex_review")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        assert state.cursor.chat_id == "019abc00-1111-2222-3333-444444444444"
        staged = conn.execute(
            "SELECT 1 FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "staging_completed"),
        ).fetchone()
        assert staged is not None
    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT:") == 1


def test_cursor_uses_the_command_frozen_before_detached_path_changes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker PATH must not need the interactive directory containing agent."""

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = _submit(git_repo, scheduler_paths)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )

    _run_until(tick, run_id, target_kind="awaiting_codex_review")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
    assert state.context.cursor.command == str((fake_clis["bin_dir"] / "agent").resolve())
    assert fake_clis["agent_log"].read_text(encoding="utf-8").count("CREATE_CHAT:") == 1


def test_usage_limit_waits_for_structured_retry_before_continuation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RETRY_AFTER_SECONDS", "120")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit_retry,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "usage-limit-retry-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(git_repo, scheduler_paths, now=now, backend=backend)
    _run_until(tick, run_id, target_kind="waiting_usage_limit")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_usage_limit"
        chat_id = state.cursor.chat_id
        assert chat_id
        wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
        assert wait_until == now + timedelta(seconds=120)
    tick_early = _tick_service(
        git_repo,
        scheduler_paths,
        now=now + timedelta(seconds=30),
        backend=backend,
    )
    tick_early.run_once()
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_usage_limit"
    tick_late = _tick_service(
        git_repo,
        scheduler_paths,
        now=now + timedelta(seconds=121),
        backend=backend,
    )
    _run_until(tick_late, run_id, target_kind="awaiting_codex_review", max_ticks=10)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.cursor.chat_id == chat_id
    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT:") == 1


def test_usage_limit_missing_retry_uses_five_hour_fallback(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "usage-limit-fallback-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=now,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_usage_limit")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
        assert wait_until == now + timedelta(seconds=DEFAULT_USAGE_LIMIT_RETRY_SECONDS)


def test_unclassified_cursor_failure_blocks(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_MODE", "fail")
    monkeypatch.delenv("FAKE_AGENT_RUN_SEQUENCE", raising=False)
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "unclassified-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=now,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="blocked", max_ticks=15)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
