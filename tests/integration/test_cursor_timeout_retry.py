"""Real scheduler/attempt/CLI paths with bounded fake-agent process timeouts."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.integration.test_phase17_4_cursor_workflow import (
    _submit,  # noqa: F401
    _tick_service,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler import cursor_attempt_runner
from ai_dev_loop.scheduler.application.attempt_backend import LaunchRequest
from ai_dev_loop.scheduler.application.cursor_timeout_retry import CursorTimeoutRetryService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel

NOW = datetime(2026, 9, 19, 12, tzinfo=UTC)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


class ShortTimeoutBackend(FakeAgentProcessBackend):
    def _execute_cursor_attempt(self, request: LaunchRequest) -> int:
        module = "ai_dev_loop.scheduler.cursor_attempt_runner"
        if module not in request.agent_argv:
            return super()._execute_cursor_attempt(request)
        argv = list(request.agent_argv)
        original = cursor_attempt_runner._binding_float
        # Only reduce the process timeout in tests; execute the real runner and cleanup.
        with patch.object(
            cursor_attempt_runner,
            "_binding_float",
            side_effect=lambda b, k: min(original(b, k), 0.25),
        ):
            return cursor_attempt_runner.main(argv[argv.index(module) + 1 :])


def setup_run(
    git_repo, scheduler_paths, fake_clis, monkeypatch, *, correction=False, sequence=False
):
    agent = fake_clis["bin_dir"] / "agent"
    script = agent.read_text()
    script = script.replace(
        'if mode == "sleep":',
        """if mode == "sleep":
        import subprocess
        workspace = args[args.index("--workspace") + 1]
        with open(os.path.join(workspace, "partial-staged.txt"), "w") as f:
            f.write("staged partial work")
        subprocess.run(["git", "add", "partial-staged.txt"], cwd=workspace, check=True)
        with open(os.path.join(workspace, "partial-unstaged.txt"), "w") as f:
            f.write("unstaged partial work")""",
    )
    agent.write_text(script)
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_SEQUENCE", "success,sleep,success" if correction else "sleep,success"
    )
    monkeypatch.setenv("FAKE_AGENT_SLEEP_SECONDS", "30")
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings" if correction else "no_findings"
    )
    if sequence:
        from tests.integration.test_phase20_3_sequence_handoff import _prepare_three_phase
        from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_SEQUENCE_ID

        from ai_dev_loop.scheduler.application.sequence_start import start_sequence

        _prepare_three_phase(git_repo, scheduler_paths)
        run_id = start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"]).run_id
    else:
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = ShortTimeoutBackend(default_scenario=FakeAttemptScenario(active_ticks=0))
    tick = _tick_service(git_repo, scheduler_paths, now=NOW, backend=backend)
    until_timeout(tick, run_id)
    return tick, run_id, backend


def state_of(tick, run_id):
    with tick.store.begin_read() as conn:
        return tick.store.load_validated_snapshot(conn, run_id)[0]


def until_timeout(tick, run_id, previous=None):
    for _ in range(40):
        tick.run_once()
        state = state_of(tick, run_id)
        assert state.kind != "blocked", getattr(state, "block_reason_summary", "")
        if getattr(state, "cursor", None) and state.cursor.timeout_attempt_id not in (
            None,
            previous,
        ):
            return state
    raise AssertionError("Timeout did not become retryable")


def complete(tick, run_id):
    for _ in range(40):
        tick.run_once()
        state = state_of(tick, run_id)
        assert state.kind != "blocked", getattr(state, "block_reason_summary", "")
        if state.kind in {"completed", "completed_with_residual_risk"}:
            return state
    raise AssertionError("Run did not complete")


@pytest.mark.parametrize("correction", [False, True])
def test_manual_retry_during_first_wait_preserves_prompt_chat_and_dirty_work(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
    correction,
):
    tick, run_id, backend = setup_run(
        git_repo, scheduler_paths, fake_clis, monkeypatch, correction=correction
    )
    state = state_of(tick, run_id)
    assert state.cursor.timeout_automatic_retries == 1
    assert state.cursor.wait_until == "2026-09-19T12:30:00.000000Z"
    original = json.loads(
        (
            tick.artifacts.run_root(run_id)
            / invocation_evidence_rel(state.cursor.timeout_attempt_id)
        ).read_text()
    )
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=git_repo)
    assert b"A  partial-staged.txt" in dirty and b"?? partial-unstaged.txt" in dirty
    launched = len(backend.launch_calls)
    tick.run_once()
    assert len(backend.launch_calls) == launched
    service = CursorTimeoutRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    assert service.retry(run_id).changed
    assert not service.retry(run_id).changed
    assert state_of(tick, run_id).cursor.timeout_automatic_retries == 0
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=git_repo) == dirty
    final = complete(tick, run_id)
    assert final.codex.reviews_completed == (2 if correction else 1)
    assert final.cursor.chat_id == state.cursor.chat_id
    assert final.cursor.iteration == state.cursor.iteration
    if correction:
        assert final.codex.reviewer_session_id == state.codex.reviewer_session_id
    with tick.store.begin_read() as conn:
        rows = conn.execute(
            "SELECT attempt_id FROM scheduler_attempts WHERE run_id=? AND component='cursor'",
            (run_id,),
        ).fetchall()
    invocations = [
        json.loads(
            (tick.artifacts.run_root(run_id) / invocation_evidence_rel(r["attempt_id"])).read_text()
        )
        for r in rows
    ]
    retry = next(
        i for i in invocations if i.get("timeout_retry_of") == state.cursor.timeout_attempt_id
    )
    for key in ["prompt_path", "prompt_sha256", "chat_id", "iteration"]:
        assert retry[key] == original[key]
    assert (git_repo / "partial-unstaged.txt").read_text() == "unstaged partial work"


def test_three_automatic_retries_then_manual_remains_available(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
):
    tick, run_id, backend = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "sleep")
    previous = state_of(tick, run_id).cursor.timeout_attempt_id
    for number in range(1, 4):
        tick = _tick_service(
            git_repo, scheduler_paths, now=NOW + timedelta(minutes=30 * number), backend=backend
        )
        state = until_timeout(tick, run_id, previous)
        previous = state.cursor.timeout_attempt_id
    assert state.cursor.timeout_automatic_retries == 3
    assert state.cursor.wait_until is None
    launched = len(backend.launch_calls)
    tick.run_once()
    assert len(backend.launch_calls) == launched
    status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    assert "cursor-retry" in status.summary.safe_next_action.command
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success")
    result = CliRunner().invoke(app, ["scheduler", "cursor-retry", run_id, "--output", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["changed"]
    # CLI uses wall time; advance the fake clock beyond it for the queued effect.
    tick = _tick_service(
        git_repo, scheduler_paths, now=datetime(2099, 1, 1, tzinfo=UTC), backend=backend
    )
    complete(tick, run_id)


def test_parallel_manual_retry_is_idempotent_and_abort_cancels_it(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

    tick, run_id, backend = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    barrier = Barrier(2)

    def retry():
        barrier.wait(timeout=5)
        return CursorTimeoutRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(
            run_id
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: retry(), range(2)))
    assert sorted(r.changed for r in results) == [False, True]
    launched = len(backend.launch_calls)
    SchedulerAbortService(
        tick.store, backend, artifacts=tick.artifacts, now_factory=lambda: NOW
    ).abort_run(run_id)
    with pytest.raises(SchedulerEngineError):
        CursorTimeoutRetryService(tick.store, tick.artifacts).retry(run_id)
    tick.run_once()
    assert len(backend.launch_calls) == launched
    assert state_of(tick, run_id).kind == "aborted"


def test_manual_retry_rejects_tampered_prompt_without_mutation(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
):
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

    tick, run_id, _ = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    before = state_of(tick, run_id)
    path = tick.artifacts.run_root(run_id) / before.context.plan_prompt.prompt_artifact_path
    path.write_text("tampered")
    with pytest.raises(SchedulerEngineError, match="evidence"):
        CursorTimeoutRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(run_id)
    assert state_of(tick, run_id) == before


def test_active_attempt_cannot_be_retried(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
):
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

    tick, run_id, backend = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch)
    service = CursorTimeoutRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW)
    service.retry(run_id)
    backend.default_scenario.active_ticks = 100
    tick.run_once()
    with tick.store.begin_read() as conn:
        assert tick.store.get_nonterminal_attempt_for_run(conn, run_id) is not None
    with pytest.raises(SchedulerEngineError, match="termination"):
        service.retry(run_id)


def test_timeout_retry_preserves_sequence_and_advances_all_phases(
    git_repo,
    scheduler_paths,
    fake_clis,
    monkeypatch,
):
    from tests.integration.test_phase20_3_sequence_handoff import THREE_PHASE_RUN_IDS

    from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService

    tick, run_id, _ = setup_run(git_repo, scheduler_paths, fake_clis, monkeypatch, sequence=True)
    sequence_id = state_of(tick, run_id).context.sequence.sequence_id
    CursorTimeoutRetryService(tick.store, tick.artifacts, now_factory=lambda: NOW).retry(run_id)
    for phase_run in THREE_PHASE_RUN_IDS:
        complete(tick, phase_run)
    sequence = SequenceStatusService(tick.store).get_status(sequence_id)
    assert sequence.state_kind == "awaiting_finalization"
    assert sequence.current_run_id == THREE_PHASE_RUN_IDS[-1]
    with tick.store.begin_read() as conn:
        assert conn.execute("SELECT count(*) FROM scheduler_runs").fetchone()[0] == 3
        assert (
            conn.execute("SELECT count(*) FROM scheduler_sequence_run_attempts").fetchone()[0] == 3
        )


def test_timeout_persisted_models_match_schemas_and_read_old_checkpoint():
    import jsonschema

    from ai_dev_loop.paths import schema_path
    from ai_dev_loop.scheduler.domain.events import CursorTimeoutRetryEvent, parse_scheduler_event
    from ai_dev_loop.scheduler.domain.state import CursorWorkflowCheckpoint

    legacy = {"iteration": 1}
    cursor = CursorWorkflowCheckpoint.model_validate(legacy)
    assert cursor.timeout_attempt_id is None and cursor.timeout_automatic_retries == 0
    schema = json.loads(schema_path("scheduler-cursor-workflow-checkpoint-v1.json").read_text())
    jsonschema.validate(legacy, schema)
    jsonschema.validate(cursor.model_dump(mode="json"), schema)
    for action, due, count in [
        ("automatic", "2026-09-19T12:30:00Z", 1),
        ("manual", "2026-09-19T12:00:00Z", 0),
        ("exhausted", None, 3),
    ]:
        event = CursorTimeoutRetryEvent(
            run_id="run-test",
            attempt_id="att-test",
            iteration=1,
            automatic_retries=count,
            action=action,
            available_at=due,
        )
        payload = event.model_dump(mode="json")
        schema = json.loads(schema_path("scheduler-cursor-timeout-retry-event-v1.json").read_text())
        jsonschema.validate(payload, schema)
        assert parse_scheduler_event(payload) == event
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**payload, "automatic_retries": 4}, schema)
