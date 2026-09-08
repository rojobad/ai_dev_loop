"""Unit tests for Phase 17.3 attempt executor boundary."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state
from tests.unit.scheduler.test_tick import (
    FakeGitAdmissionPort,
    _bootstrap_run,
    _tick_service,
)

from ai_dev_loop.paths import DIR_MODE, SENSITIVE_FILE_MODE
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.scheduler.application.attempt_backend import (
    LaunchRequest,
    TerminationClass,
    UnitLifecycleState,
)
from ai_dev_loop.scheduler.application.attempt_identity import (
    flock_wrapped_argv,
    unit_identity_from_attempt_id,
    validate_attempt_id,
    worktree_lock_path,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend
from ai_dev_loop.scheduler.application.systemd_show import observe_from_show, parse_systemctl_show
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    SCHEMA_VERSION,
    SqliteSchedulerStore,
    migration_checksum,
)
from ai_dev_loop.scheduler.infrastructure.systemd_assets import validate_packaged_assets


def _pause_v2_database(tmp_path: Path) -> Path:
    db = tmp_path / "v2.sqlite3"
    paused = False

    def pause_v3(statement: str) -> None:
        nonlocal paused
        if not paused and "ALTER TABLE scheduler_attempts" in statement:
            paused = True
            raise RuntimeError("pause-v3")

    with pytest.raises(RuntimeError, match="pause-v3"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v3)
    assert _user_version(db) == 2
    return db


def _populate_v2_fixture(db: Path) -> dict[str, int | str]:
    store = SqliteSchedulerStore(db, bootstrap=False)
    state = sample_submitted_state(repo_root="/tmp/v2-repo")
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    now_text = now.isoformat()
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-v2-submit",
            event=event,
            now=now,
        )
        conn.execute(
            """
            INSERT INTO scheduler_effects(
                dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                status, available_at, claimed_run_version, created_at, updated_at
            ) VALUES (
                'fake-agent-self-test', 'evt-v2-submit', 0, ?, 'fake-agent-self-test',
                ?, 'fake_agent.self_test', '{}', ?, 'claimed', ?, 1, ?, ?
            )
            """,
            (
                state.run_id,
                f"{state.run_id}:fake-agent-self-test",
                "c" * 64,
                now_text,
                now_text,
                now_text,
            ),
        )
        conn.execute(
            """
            INSERT INTO scheduler_claims(
                claim_id, run_id, dispatch_id, owner_id, lease_generation,
                status, acquired_at, updated_at
            ) VALUES ('clm-v2', ?, 'fake-agent-self-test', 'tick-owner-v2', 1, 'active', ?, ?)
            """,
            (state.run_id, now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """
            UPDATE scheduler_capacity
            SET holder_run_id = ?, holder_claim_id = 'clm-v2', holder_tick_generation = 1,
                updated_at = ?
            WHERE capacity_name = 'global_active_agent'
            """,
            (state.run_id, now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO scheduler_run_tick_claims(
                claim_id, run_id, tick_owner_id, tick_lease_generation, purpose,
                expected_run_version, status, acquired_at, updated_at
            ) VALUES ('adm-v2', ?, 'tick-owner-v2', 1, 'admission', 1, 'active', ?, ?)
            """,
            (state.run_id, now.isoformat(), now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO scheduler_attempts(
                attempt_id, run_id, dispatch_id, component, iteration, status,
                backend_identity, launch_intent_sha256, stdout_artifact_path,
                stderr_artifact_path, completion_envelope_sha256, created_at, updated_at
            ) VALUES (
                'att-v2', ?, 'fake-agent-self-test', 'cursor', 1, 'active',
                'ai-dev-loop-att-v2.service', ?, 'attempts/att-v2/stdout.txt',
                'attempts/att-v2/stderr.txt', NULL, ?, ?
            )
            """,
            (state.run_id, "b" * 64, now.isoformat(), now.isoformat()),
        )
        counts = {
            "runs": conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()[0],
            "effects": conn.execute("SELECT COUNT(*) FROM scheduler_effects").fetchone()[0],
            "claims": conn.execute("SELECT COUNT(*) FROM scheduler_claims").fetchone()[0],
            "attempts": conn.execute("SELECT COUNT(*) FROM scheduler_attempts").fetchone()[0],
            "run_id": state.run_id,
        }
    return counts


def test_v3_migration_preserves_populated_v2_rows(tmp_path: Path) -> None:
    db = _pause_v2_database(tmp_path)
    before = _populate_v2_fixture(db)
    SqliteSchedulerStore(db)
    assert _user_version(db) == SCHEMA_VERSION
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_runs").fetchone()[0] == before["runs"]
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_events").fetchone()[0] == before["events"]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_effects").fetchone()[0]
            == before["effects"]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_claims").fetchone()[0] == before["claims"]
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM scheduler_attempts").fetchone()[0]
            == before["attempts"]
        )
        row = conn.execute(
            "SELECT launch_nonce, unit_identity FROM scheduler_attempts WHERE attempt_id = 'att-v2'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        checksum = conn.execute(
            "SELECT checksum FROM scheduler_schema_migrations WHERE version = 3"
        ).fetchone()
        assert checksum[0] == migration_checksum(3)
    finally:
        conn.close()


def test_v3_migration_checksum_rejected_on_reopen(tmp_path: Path) -> None:
    db = _pause_v2_database(tmp_path)
    _populate_v2_fixture(db)
    SqliteSchedulerStore(db)
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE scheduler_schema_migrations SET checksum = ? WHERE version = 3",
        ("f" * 64,),
    )
    conn.commit()
    conn.close()
    with pytest.raises(SchedulerEngineError, match="checksum"):
        SqliteSchedulerStore(db)


def test_v3_migration_rollback_on_fault(tmp_path: Path) -> None:
    paused_db = _pause_v2_database(tmp_path)
    _populate_v2_fixture(paused_db)

    def boom(statement: str) -> None:
        if "idx_scheduler_attempts_active_per_run" in statement:
            raise RuntimeError("injected v3 fault")

    with pytest.raises(RuntimeError, match="injected v3 fault"):
        SqliteSchedulerStore(paused_db, migration_fault_hook=boom)
    assert _user_version(paused_db) == 2
    conn = sqlite3.connect(paused_db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM scheduler_attempts").fetchone()[0] == 1
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(scheduler_attempts)").fetchall()
        }
        assert "launch_nonce" not in columns
    finally:
        conn.close()


def test_flock_wrapped_argv_executes_agent(tmp_path: Path) -> None:
    if shutil.which("flock") is None:
        pytest.skip("flock not available")
    lock = tmp_path / "attempt.lock"
    marker = tmp_path / "marker.txt"
    argv = flock_wrapped_argv(lock, ["touch", str(marker)])
    assert "--" not in argv
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert marker.is_file()
    blocked = subprocess.run(argv, capture_output=True, text=True, check=False)
    assert blocked.returncode in {0, 69}


def test_safe_unit_identity_and_lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempt_id = "att-" + "b" * 32
    validate_attempt_id(attempt_id)
    unit = unit_identity_from_attempt_id(attempt_id)
    assert unit.endswith(".service")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    lock = worktree_lock_path(tmp_path / "state" / "ai_dev_loop", "a" * 64)
    argv = flock_wrapped_argv(lock, ["python", "-m", "ai_dev_loop.scheduler.fake_agent_runner"])
    assert argv[:4] == ["flock", "--exclusive", "--nonblock", str(lock)]
    assert argv[4] == "python"


def test_systemd_launch_argv_shape(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args: list[str], **kwargs: object) -> ProcessResult:
        calls.append(list(args))
        return ProcessResult(args=list(args), returncode=0, stdout="", stderr="")

    backend = SystemdUserBackend(runner=runner, attempt_runtime_seconds=120)
    attempt_id = "att-" + "c" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    lock = tmp_path / "lock.file"
    request = backend.build_agent_argv(
        run_id="run-1",
        attempt_id=attempt_id,
        unit_identity=unit,
        artifact_root=tmp_path / "artifacts",
    )
    backend.launch(
        LaunchRequest(
            attempt_id=attempt_id,
            unit_identity=unit,
            working_directory=tmp_path,
            agent_argv=request,
            lock_path=lock,
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            result_envelope_path=tmp_path / "result.json",
        )
    )
    assert calls
    argv = calls[0]
    assert argv[0] == "systemd-run"
    assert "--user" in argv
    assert f"--unit={unit}" in argv
    assert "--property=RemainAfterExit=yes" in argv
    assert "--property=RuntimeMaxSec=120" in argv
    assert "--property=TimeoutStopSec=120" in argv
    assert "--collect" not in argv
    flock_index = argv.index("flock")
    assert argv[flock_index : flock_index + 4] == ["flock", "--exclusive", "--nonblock", str(lock)]
    assert argv[flock_index + 4] != "--"


def test_systemctl_show_parsing_matrix() -> None:
    active_running = parse_systemctl_show(
        "Id=unit.service\nActiveState=active\nSubState=running\nLoadState=loaded\n"
    )
    parsed = observe_from_show(show=active_running, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.ACTIVE
    assert parsed.owned is True
    assert parsed.exit_code is None

    retained_success = parse_systemctl_show(
        "Id=unit.service\nActiveState=active\nSubState=exited\nResult=success\n"
        "ExecMainStatus=0\nExecMainCode=1\nLoadState=loaded\n"
    )
    parsed = observe_from_show(show=retained_success, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.INACTIVE
    assert parsed.termination_class == TerminationClass.SUCCESS
    assert parsed.exit_code == 0

    failed = parse_systemctl_show(
        "Id=unit.service\nActiveState=failed\nResult=exit-code\n"
        "ExecMainStatus=2\nExecMainCode=1\nLoadState=loaded\n"
    )
    parsed = observe_from_show(show=failed, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.FAILED
    assert parsed.exit_code == 2
    assert parsed.termination_class == TerminationClass.NONZERO_EXIT

    timeout = parse_systemctl_show(
        "Id=unit.service\nActiveState=inactive\nResult=timeout\n"
        "ExecMainStatus=0\nExecMainCode=2\nLoadState=loaded\n"
    )
    parsed = observe_from_show(show=timeout, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.FAILED
    assert parsed.termination_class == TerminationClass.TIMEOUT

    missing = parse_systemctl_show("LoadState=not-found\n")
    parsed = observe_from_show(show=missing, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.MISSING
    assert parsed.owned is True
    assert parsed.absence_proven is True

    malformed = parse_systemctl_show("not-a-property\n")
    parsed = observe_from_show(show=malformed, expected_unit_identity="unit.service")
    assert parsed.lifecycle_state == UnitLifecycleState.UNAVAILABLE
    assert parsed.owned is False

    active_without_substate = parse_systemctl_show(
        "Id=unit.service\nActiveState=active\nLoadState=loaded\n"
    )
    parsed = observe_from_show(
        show=active_without_substate,
        expected_unit_identity="unit.service",
    )
    assert parsed.lifecycle_state == UnitLifecycleState.UNAVAILABLE


def test_systemd_observe_timeout_and_stop_failure(tmp_path: Path) -> None:
    attempt_id = "att-" + "e" * 32
    unit = unit_identity_from_attempt_id(attempt_id)

    def observe_runner(args: list[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(args=args, returncode=0, stdout="LoadState=not-found\n", stderr="")

    def launch_runner(args: list[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(args=args, returncode=0, stdout="", stderr="")

    backend_observe = SystemdUserBackend(runner=observe_runner)
    launched = backend_observe.observe(unit_identity=unit, attempt_id=attempt_id)
    assert launched.lifecycle_state == UnitLifecycleState.MISSING
    assert launched.absence_proven is True
    backend_launch = SystemdUserBackend(runner=launch_runner)
    backend_launch.launch(
        LaunchRequest(
            attempt_id=attempt_id,
            unit_identity=unit,
            working_directory=tmp_path,
            agent_argv=["true"],
            lock_path=tmp_path / "lock",
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            result_envelope_path=tmp_path / "result.json",
        )
    )

    def stop_runner(args: list[str], **kwargs: object) -> ProcessResult:
        if "show" in args:
            return ProcessResult(
                args=args,
                returncode=0,
                stdout=(
                    f"Id={unit}\nActiveState=inactive\nResult=success\n"
                    "ExecMainStatus=0\nExecMainCode=0\nLoadState=loaded\n"
                ),
                stderr="",
            )
        return ProcessResult(args=args, returncode=1, stdout="", stderr="stop failed")

    backend_stop = SystemdUserBackend(runner=stop_runner)
    with pytest.raises(RuntimeError, match="stop failed"):
        backend_stop.terminate(unit_identity=unit, attempt_id=attempt_id)
    other_attempt = "att-" + "f" * 32
    other_unit = unit_identity_from_attempt_id(other_attempt)
    with pytest.raises(ValueError, match="does not match"):
        backend_stop.terminate(unit_identity=other_unit, attempt_id=attempt_id)


def test_systemd_observe_command_failure_is_unavailable() -> None:
    attempt_id = "att-" + "8" * 32
    unit = unit_identity_from_attempt_id(attempt_id)

    def failed_runner(args: list[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(args=args, returncode=1, stdout="", stderr="permission denied")

    backend = SystemdUserBackend(runner=failed_runner)
    observation = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert observation.lifecycle_state == UnitLifecycleState.UNAVAILABLE
    assert observation.owned is False
    assert observation.absence_proven is False
    with pytest.raises(RuntimeError, match="authoritative observation"):
        backend.terminate(unit_identity=unit, attempt_id=attempt_id)


def test_systemd_retained_active_exited_completes_via_backend(tmp_path: Path) -> None:
    attempt_id = "att-" + "7" * 32
    unit = unit_identity_from_attempt_id(attempt_id)

    def runner(args: list[str], **kwargs: object) -> ProcessResult:
        return ProcessResult(
            args=args,
            returncode=0,
            stdout=(
                f"Id={unit}\nLoadState=loaded\nActiveState=active\nSubState=exited\n"
                "Result=success\nExecMainStatus=0\nExecMainCode=1\n"
            ),
            stderr="",
        )

    backend = SystemdUserBackend(runner=runner)
    observation = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert observation.lifecycle_state == UnitLifecycleState.INACTIVE
    assert observation.termination_class == TerminationClass.SUCCESS
    assert observation.exit_code == 0


def test_fake_backend_lifecycle_matrix(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend()
    attempt_id = "att-" + "d" * 32
    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(
            active_ticks=2, exit_code=0, termination_class=TerminationClass.SUCCESS
        ),
    )
    unit = unit_identity_from_attempt_id(attempt_id)
    from ai_dev_loop.scheduler.application.attempt_backend import LaunchRequest

    request = LaunchRequest(
        attempt_id=attempt_id,
        unit_identity=unit,
        working_directory=tmp_path,
        agent_argv=["echo", "ok"],
        lock_path=tmp_path / "lock",
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        result_envelope_path=tmp_path / "result.json",
    )
    backend.launch(request)
    first = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert first.lifecycle_state == UnitLifecycleState.ACTIVE
    second = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert second.lifecycle_state == UnitLifecycleState.ACTIVE
    third = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert third.lifecycle_state == UnitLifecycleState.INACTIVE
    assert third.termination_class == TerminationClass.SUCCESS


def test_attempt_output_paths_are_owner_only(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=0))
    tick = _tick_service(
        store, artifacts, FakeGitAdmissionPort(resolved_root=repo_root), attempt_backend=backend
    )
    tick.run_once()
    tick.run_once()
    stdout = artifacts.run_root(run_id) / "attempts" / ("att-" + "a" * 32) / "stdout.txt"
    if stdout.exists():
        assert (stdout.stat().st_mode & 0o777) == SENSITIVE_FILE_MODE


def test_packaged_systemd_assets_are_safe() -> None:
    assert validate_packaged_assets() == []


def test_capacity_busy_blocks_second_launch(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=2))
    tick = _tick_service(
        store, artifacts, FakeGitAdmissionPort(resolved_root=repo_root), attempt_backend=backend
    )
    first = tick.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in first.run_receipts
    )
    second = tick.run_once()
    assert any(item.action in {"attempt_active", "attempt_busy"} for item in second.run_receipts)
    assert len(backend.launch_calls) == 1


def _tick_service_unique_events(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    fake_git: FakeGitAdmissionPort,
    *,
    now: datetime | None = None,
    owner: str = "tick-owner-a",
    attempt_backend: FakeAgentProcessBackend | None = None,
):
    import secrets

    tick = _tick_service(
        store,
        artifacts,
        fake_git,
        now=now,
        owner=owner,
        attempt_backend=attempt_backend,
    )
    tick._event_id_factory = lambda: f"evt-{secrets.token_hex(16)}"
    tick._fence_id_factory = lambda: f"fnc-{secrets.token_hex(16)}"
    if tick._attempt_service is not None:
        tick._attempt_service._event_id_factory = tick._event_id_factory
        tick._attempt_service._fence_id_factory = tick._fence_id_factory
    return tick


def test_cross_tick_owner_completes_once(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=0))
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted", "attempt_active"}
        for item in first.run_receipts
    )
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_completed" for item in second.run_receipts)
    third = tick_b.run_once()
    assert not any(item.action == "attempt_completed" for item in third.run_receipts)
    with store.begin_read() as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_terminal_evidence_survives_missing_unit(tmp_path: Path) -> None:
    backend = FakeAgentProcessBackend()
    attempt_id = "att-" + "f" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    backend.set_scenario(attempt_id, FakeAttemptScenario(active_ticks=0))
    request = LaunchRequest(
        attempt_id=attempt_id,
        unit_identity=unit,
        working_directory=tmp_path,
        agent_argv=["true"],
        lock_path=tmp_path / "lock",
        stdout_path=tmp_path / "stdout.txt",
        stderr_path=tmp_path / "stderr.txt",
        result_envelope_path=tmp_path / "result.json",
    )
    backend.launch(request)
    completed = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert completed.lifecycle_state == UnitLifecycleState.INACTIVE
    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=0, missing_after_launch=True),
    )
    missing = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert missing.lifecycle_state == UnitLifecycleState.INACTIVE
    assert missing.termination_class == TerminationClass.SUCCESS


def test_unowned_observation_blocks_launch(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(owned=False, active_ticks=0),
    )
    tick = _tick_service(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    receipt = tick.run_once()
    assert any(item.action == "attempt_launch_blocked" for item in receipt.run_receipts)
    assert len(backend.launch_calls) == 0


def test_malformed_envelope_stays_uncertain_without_capacity_release(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=2))
    tick = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    tick.run_once()
    attempt_id = "att-" + "a" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    run_root = artifacts.run_root(run_id)
    result_path = run_root / "attempts" / attempt_id / "result.json"
    stdout_path = run_root / "attempts" / attempt_id / "stdout.txt"
    stderr_path = run_root / "attempts" / attempt_id / "stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("out\n", encoding="utf-8")
    stderr_path.write_text("err\n", encoding="utf-8")
    result_path.write_text("{not-json", encoding="utf-8")
    backend.preload_completed(
        attempt_id,
        envelope_path=result_path,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
    )
    backend._launched[attempt_id] = LaunchRequest(
        attempt_id=attempt_id,
        unit_identity=unit,
        working_directory=Path(repo_root),
        agent_argv=["true"],
        lock_path=tmp_path / "lock",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_envelope_path=result_path,
    )
    second = tick.run_once()
    assert any(item.action == "attempt_uncertain" for item in second.run_receipts)
    with store.begin_read() as conn:
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id


def test_fake_agent_runner_creates_owner_only_outputs_under_umask_022(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("chmod not meaningful on Windows")

    from ai_dev_loop.scheduler import fake_agent_runner
    from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

    artifact_root = tmp_path / "artifacts"
    run_id = "run-umask"
    attempt_id = "att-" + "9" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    previous = os.umask(0o022)
    try:
        exit_code = fake_agent_runner.main(
            [
                "--run-id",
                run_id,
                "--attempt-id",
                attempt_id,
                "--unit-identity",
                unit,
                "--artifact-root",
                str(artifact_root),
            ]
        )
    finally:
        os.umask(previous)
    assert exit_code == 0
    run_root = run_artifact_root(artifact_root, run_id)
    stdout = run_root / "attempts" / attempt_id / "stdout.txt"
    stderr = run_root / "attempts" / attempt_id / "stderr.txt"
    assert (stdout.stat().st_mode & 0o777) == SENSITIVE_FILE_MODE
    assert (stderr.stat().st_mode & 0o777) == SENSITIVE_FILE_MODE
    assert (stdout.parent.stat().st_mode & 0o777) == DIR_MODE


def test_overlapping_tick_lease_blocks_stale_activation(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, _run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.acquire_global_tick_lease(
            conn,
            owner_id="tick-owner-a",
            now=now,
            ttl_seconds=30,
        )
    tick_b = _tick_service(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        now=now,
        owner="tick-owner-b",
    )
    blocked = tick_b.run_once()
    assert blocked.lease_acquired is False


def test_unavailable_observation_preserves_launching_without_launch(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(observation_unavailable=True),
    )
    tick = _tick_service(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    receipt = tick.run_once()
    assert any(item.action == "attempt_observation_unavailable" for item in receipt.run_receipts)
    assert len(backend.launch_calls) == 0
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "launching"
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id


def test_cross_tick_launch_after_launch_failure(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(launch_raises=True),
    )
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(item.action == "attempt_launch_failed" for item in first.run_receipts)
    assert len(backend.launch_calls) == 0
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "launching"
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id

    backend.default_scenario = FakeAttemptScenario(active_ticks=0)
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_launched" for item in second.run_receipts)
    assert len(backend.launch_calls) == 1

    third = tick_b.run_once()
    assert any(item.action == "attempt_completed" for item in third.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_cross_tick_launch_after_committed_intent_crash_window(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(observation_unavailable=True),
    )
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(item.action == "attempt_observation_unavailable" for item in first.run_receipts)
    assert len(backend.launch_calls) == 0
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "launching"
        assert attempt["launch_intent_sha256"] is not None

    backend.default_scenario = FakeAttemptScenario(active_ticks=0)
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_launched" for item in second.run_receipts)
    assert len(backend.launch_calls) == 1

    third = tick_b.run_once()
    assert any(item.action == "attempt_completed" for item in third.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_executed_uncertain_attempt_never_relaunches(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    attempt_id = "att-" + "a" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1),
    )
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted", "attempt_active"}
        for item in first.run_receipts
    )
    assert len(backend.launch_calls) == 1

    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=1, missing_after_launch=True),
    )
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_uncertain" for item in second.run_receipts)
    assert len(backend.launch_calls) == 1

    third = tick_b.run_once()
    assert not any(item.action == "attempt_launched" for item in third.run_receipts)
    assert not any(item.action == "attempt_completed" for item in third.run_receipts)
    assert len(backend.launch_calls) == 1

    fourth = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-c",
    ).run_once()
    assert not any(item.action == "attempt_launched" for item in fourth.run_receipts)
    assert not any(item.action == "attempt_completed" for item in fourth.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "uncertain"
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 0
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id


def test_finished_without_verified_result_stays_uncertain_when_unit_disappears(
    tmp_path: Path,
) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    attempt_id = "att-" + "a" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=2))
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    tick_a.run_once()
    assert len(backend.launch_calls) == 1

    run_root = artifacts.run_root(run_id)
    result_path = run_root / "attempts" / attempt_id / "result.json"
    stdout_path = run_root / "attempts" / attempt_id / "stdout.txt"
    stderr_path = run_root / "attempts" / attempt_id / "stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("out\n", encoding="utf-8")
    stderr_path.write_text("err\n", encoding="utf-8")
    result_path.write_text("{not-json", encoding="utf-8")
    backend.preload_completed(
        attempt_id,
        envelope_path=result_path,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
    )
    backend._launched[attempt_id] = LaunchRequest(
        attempt_id=attempt_id,
        unit_identity=unit,
        working_directory=Path(repo_root),
        agent_argv=["true"],
        lock_path=tmp_path / "lock",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_envelope_path=result_path,
    )

    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_uncertain" for item in second.run_receipts)
    assert len(backend.launch_calls) == 1

    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=2, missing_after_launch=True),
    )
    third = tick_b.run_once()
    assert not any(item.action == "attempt_launched" for item in third.run_receipts)
    assert not any(item.action == "attempt_completed" for item in third.run_receipts)
    assert any(item.action == "attempt_uncertain" for item in third.run_receipts)
    assert len(backend.launch_calls) == 1

    fourth = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-c",
    ).run_once()
    assert not any(item.action == "attempt_launched" for item in fourth.run_receipts)
    assert not any(item.action == "attempt_completed" for item in fourth.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "uncertain"
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 0
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id


def test_unavailable_reconcile_active_then_completed(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    attempt_id = "att-" + "a" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=2),
    )
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted", "attempt_active"}
        for item in first.run_receipts
    )
    assert len(backend.launch_calls) == 1

    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=2, observation_unavailable=True),
    )
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_observation_unavailable" for item in second.run_receipts)
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
        assert attempt is not None
        assert str(attempt["status"]) == "active"
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] == run_id

    backend.set_scenario(attempt_id, FakeAttemptScenario(active_ticks=1))
    third = tick_b.run_once()
    assert any(item.action == "attempt_active" for item in third.run_receipts)

    fourth = tick_b.run_once()
    assert any(item.action == "attempt_completed" for item in fourth.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_unavailable_reconcile_completed_directly(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    attempt_id = "att-" + "a" * 32
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1),
    )
    tick_a = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-a",
    )
    first = tick_a.run_once()
    assert any(
        item.action in {"attempt_launched", "attempt_adopted", "attempt_active"}
        for item in first.run_receipts
    )
    assert len(backend.launch_calls) == 1

    backend.set_scenario(
        attempt_id,
        FakeAttemptScenario(active_ticks=1, observation_unavailable=True),
    )
    tick_b = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        owner="tick-owner-b",
    )
    second = tick_b.run_once()
    assert any(item.action == "attempt_observation_unavailable" for item in second.run_receipts)

    backend.set_scenario(attempt_id, FakeAttemptScenario(active_ticks=0))
    third = tick_b.run_once()
    assert any(item.action == "attempt_completed" for item in third.run_receipts)
    assert len(backend.launch_calls) == 1
    with store.begin_read() as conn:
        completed = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? AND event_kind = ?",
            (run_id, "attempt_completed"),
        ).fetchone()
        assert int(completed[0]) == 1
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def test_lease_superseded_before_launch_blocks_stale_launch(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, _run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    now = datetime(2026, 9, 4, 12, 2, tzinfo=UTC)

    class LeaseSupersedingBackend(FakeAgentProcessBackend):
        def observe(self, *, unit_identity: str, attempt_id: str):
            result = super().observe(unit_identity=unit_identity, attempt_id=attempt_id)
            with store.begin_immediate() as conn:
                conn.execute(
                    """
                    UPDATE scheduler_tick_leases
                    SET expires_at = ?
                    WHERE lease_name = 'global'
                    """,
                    ("2026-09-04T11:00:00+00:00",),
                )
                store.acquire_global_tick_lease(
                    conn,
                    owner_id="tick-owner-b",
                    now=now,
                    ttl_seconds=30,
                )
            return result

    backend = LeaseSupersedingBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1),
    )
    tick = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
        now=now,
        owner="tick-owner-a",
    )
    receipt = tick.run_once()
    assert any(item.action == "attempt_launch_stale" for item in receipt.run_receipts)
    assert len(backend.launch_calls) == 0


def test_completion_cleans_up_owned_unit(tmp_path: Path) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=0))
    tick = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    tick.run_once()
    second = tick.run_once()
    assert any(item.action == "attempt_completed" for item in second.run_receipts)
    assert len(backend.terminate_calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        "[]",
        "null",
        "{}",
        '{"schema_version":1}',
        '{"schema_version":1,"attempt_id":"att","unit_identity":"u","exit_code":0,'
        '"termination_class":"success","stdout_artifact_path":"a","stdout_sha256":"0",'
        '"stderr_artifact_path":"b","stderr_sha256":"0"}',
        '{"schema_version":1,"attempt_id":"att-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"unit_identity":"ai-dev-loop-att-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.service",'
        '"exit_code":true,"termination_class":"success","stdout_artifact_path":"a",'
        '"stdout_sha256":"'
        + "a" * 64
        + '","stderr_artifact_path":"b","stderr_sha256":"'
        + "a" * 64
        + '"}',
    ],
)
def test_invalid_envelope_shapes_raise_value_error(payload: str) -> None:
    from ai_dev_loop.scheduler.application.attempt_envelope import parse_result_envelope

    with pytest.raises(ValueError):
        parse_result_envelope(payload.encode("utf-8"))


def test_invalid_envelope_service_records_uncertain_without_releasing_capacity(
    tmp_path: Path,
) -> None:
    repo_root = str(tmp_path / "repo")
    store, artifacts, run_id = _bootstrap_run(tmp_path, repo_root=repo_root)
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=2))
    tick = _tick_service_unique_events(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=repo_root),
        attempt_backend=backend,
    )
    tick.run_once()
    attempt_id = "att-" + "a" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    run_root = artifacts.run_root(run_id)
    result_path = run_root / "attempts" / attempt_id / "result.json"
    stdout_path = run_root / "attempts" / attempt_id / "stdout.txt"
    stderr_path = run_root / "attempts" / attempt_id / "stderr.txt"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text("out\n", encoding="utf-8")
    stderr_path.write_text("err\n", encoding="utf-8")
    result_path.write_text("{not-json", encoding="utf-8")
    backend.preload_completed(
        attempt_id,
        envelope_path=result_path,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
    )
    backend._launched[attempt_id] = LaunchRequest(
        attempt_id=attempt_id,
        unit_identity=unit,
        working_directory=Path(repo_root),
        agent_argv=["true"],
        lock_path=tmp_path / "lock",
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        result_envelope_path=result_path,
    )
    second = tick.run_once()
    assert any(item.action == "attempt_uncertain" for item in second.run_receipts)
    with store.begin_read() as conn:
        assert store.get_capacity_row(conn)["holder_run_id"] == run_id


def test_bounded_envelope_read_rejects_oversized_evidence(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler.application.attempt_envelope import (
        MAX_RESULT_ENVELOPE_BYTES,
        MAX_RESULT_ENVELOPE_READ_BYTES,
        read_bounded_bytes,
        validate_completion_evidence,
    )

    path = tmp_path / "oversized.json"
    path.write_bytes(b"x" * (MAX_RESULT_ENVELOPE_BYTES + 1))
    data = read_bounded_bytes(path, MAX_RESULT_ENVELOPE_READ_BYTES)
    assert len(data) == MAX_RESULT_ENVELOPE_BYTES + 1
    attempt_id = "att-" + "3" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    run_root = tmp_path / "run"
    result_rel = f"attempts/{attempt_id}/result.json"
    result_path = run_root / result_rel
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_bytes(b"x" * (MAX_RESULT_ENVELOPE_BYTES + 1))
    with pytest.raises(ValueError, match="size is invalid"):
        validate_completion_evidence(
            run_root=run_root,
            attempt_id=attempt_id,
            unit_identity=unit,
            result_rel=result_rel,
            stdout_rel=f"attempts/{attempt_id}/stdout.txt",
            stderr_rel=f"attempts/{attempt_id}/stderr.txt",
            observed_exit_code=None,
            observed_termination=None,
        )


def test_run_outside_prefix_escape_rejected(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler.application.attempt_envelope import (
        build_result_envelope,
        sha256_file,
        validate_completion_evidence,
    )

    if os.name == "nt":
        pytest.skip("symlink escape test requires posix semantics")
    run_root = tmp_path / "run"
    outside = tmp_path / "run-outside"
    run_root.mkdir()
    outside.mkdir()
    attempt_id = "att-" + "2" * 32
    unit = unit_identity_from_attempt_id(attempt_id)
    stdout_rel = f"attempts/{attempt_id}/stdout.txt"
    stderr_rel = f"attempts/{attempt_id}/stderr.txt"
    stdout_path = run_root / "attempts" / attempt_id / "stdout.txt"
    stderr_path = run_root / "attempts" / attempt_id / "stderr.txt"
    outside_stdout = outside / "stdout.txt"
    outside_stdout.write_text("out\n", encoding="utf-8")
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("err\n", encoding="utf-8")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.symlink_to(outside_stdout)
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(outside_stdout),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path = run_root / "attempts" / attempt_id / "result.json"
    result_path.write_bytes(envelope)
    with pytest.raises(ValueError, match="symlink|escapes the run artifact root"):
        validate_completion_evidence(
            run_root=run_root,
            attempt_id=attempt_id,
            unit_identity=unit,
            result_rel=f"attempts/{attempt_id}/result.json",
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=0,
            observed_termination=TerminationClass.SUCCESS,
        )


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
