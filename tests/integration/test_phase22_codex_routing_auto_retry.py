"""Integration slice: Phase 22 routing auto-retry through production TickService."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase22_codex_routing_auto_retry import (
    TestAutomaticRoutingRetryTick,
    _routing_failure_cycle,
    _run_until,
    _tick_service,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.schemas import (
    RUN_INSPECT_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_DELAY_SECONDS,
)
from ai_dev_loop.scheduler.domain.state import WaitingCodexReviewRetryState

runner = CliRunner()


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _invoke(args: list[str]) -> tuple[int, dict[str, object], str]:
    result = runner.invoke(app, args)
    payload = json.loads(result.stdout) if result.stdout.strip() else {}
    return result.exit_code, payload, result.stderr


def test_integration_tick_authorizes_routing_retry_after_due(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
) -> None:
    TestAutomaticRoutingRetryTick().test_tick_authorizes_only_after_due_time(
        git_repo,
        scheduler_paths,
        fake_clis,
        monkeypatch,
    )


def test_integration_dispatch_ingest_and_inspect_routing_diagnostics(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexReviewRetryState)
        assert state.codex.review_retry_failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT

    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    due_tick.run_once()
    _run_until(due_tick, run_id, target_kind="waiting_codex_review_retry", max_ticks=60)

    code, payload, _stderr = _invoke(["integration", "run", "inspect", run_id, "--output", "json"])
    assert code == 0
    data = payload["data"]
    validate_integration_instance(data, RUN_INSPECT_DATA_SCHEMA)
    assert data["state"] == "waiting_codex_review_retry"
    assert data["codexReviewRetryFailureKind"] == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
    assert data["codexRoutingFailurePostProbeStatus"] == "unavailable"
    assert data["codexRoutingAutoRetryAuthorizationsUsed"] == 1
    assert "cursor_fix_prompt" not in json.dumps(data)
