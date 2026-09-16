"""Fresh-recovery correction flow tests for Phase 20.6."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase20_1_reviewer_retry_corrections import (
    _blocked_recovery_fixture,
    _run_until,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort

from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.recovery_prepare import RecoveryPrepareService
from ai_dev_loop.scheduler.application.recovery_start import RecoveryStartService
from ai_dev_loop.scheduler.application.recovery_worktree import ProductionRecoveryWorktreePort
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.state import WaitingForCursorFixState

_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_fresh_recovery_review_findings_schedule_create_chat_and_correction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    _, source_run_id, artifacts, store = _blocked_recovery_fixture(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    prepared = RecoveryPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="recovery correction flow commit",
    )

    start = RecoveryStartService(
        store,
        artifacts,
        worktree_port=ProductionRecoveryWorktreePort(),
    )
    started = start.start(prepared.recovery_id)
    recovery_run_id = started.recovery_run_id

    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 15, 14, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-20-6-recovery-corr-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_COUNTER):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, recovery_run_id, target_kind="waiting_for_cursor_fix", max_ticks=80)

    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, recovery_run_id)
        assert isinstance(state, WaitingForCursorFixState)
        assert state.fresh_recovery is not None
        assert state.cursor.chat_id is None
        assert state.cursor.iteration == 2
        assert state.codex.latest_correction_envelope_path
        create_chat = conn.execute(
            """
            SELECT effect_kind, effect_payload FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ? AND status = 'pending'
            """,
            (recovery_run_id, CREATE_CHAT_EFFECT_KIND),
        ).fetchone()
    assert create_chat is not None

    for _ in range(20):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, recovery_run_id)
            if state.cursor.chat_id:
                break

    assert state.cursor.chat_id
    assert state.fresh_recovery is not None
    with store.begin_read() as conn:
        correction = conn.execute(
            """
            SELECT effect_kind, effect_payload FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ? AND status = 'pending'
            ORDER BY created_at DESC LIMIT 1
            """,
            (recovery_run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
    assert correction is not None
    payload = correction[1]
    assert '"iteration": 2' in payload or '"iteration":2' in payload
