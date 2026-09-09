"""Service-path regression tests for Phase 17.4 cursor workflow corrections."""

from __future__ import annotations

import itertools
import json
import subprocess
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.iterations import build_usage_limit_continuation_envelope
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.runners.probes import AUTH_PROBE_TIMEOUT_SECONDS, probe_cursor_auth
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    parse_result_envelope,
    sha256_file,
)
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    authenticate_pinned_invocation_evidence,
    invocation_evidence_sha256,
    load_authenticated_cursor_outcome,
    usage_limit_continuation_path_for_attempt,
    validate_cursor_turn_outcome_semantics,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.application.cursor_workflow_service import CursorWorkflowService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.scheduler_preflight import (
    SchedulerPreflightPort,
    SchedulerPreflightResult,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_events_rel,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import (
    ProtectedArtifactError,
    ProtectedArtifactStore,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _assert_capacity_free(store: SqliteSchedulerStore) -> None:
    with store.begin_read() as conn:
        capacity = store.get_capacity_row(conn)
        assert capacity["holder_run_id"] is None


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
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
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def test_tampered_invocation_evidence_rejected_before_launch(tmp_path: Path) -> None:
    attempt_id = "att-" + "a" * 32
    run_id = "run-test"
    dispatch_id = "fx-test"
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "prompt_path": "prompts/cursor-initial.txt",
        "prompt_sha256": "b" * 64,
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    from ai_dev_loop.scheduler.domain.common import payload_sha256

    with pytest.raises(CursorEvidenceError, match="prompt artifact missing"):
        verify_cursor_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )
    evidence["run_id"] = "tampered"
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CursorEvidenceError, match="launch intent does not match"):
        verify_cursor_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_replaced_stdout_fails_completion_envelope_authentication(tmp_path: Path) -> None:
    attempt_id = "att-test"
    unit_identity = f"unit-{attempt_id}"
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = tmp_path / stdout_rel
    stderr_path = tmp_path / stderr_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("", encoding="utf-8")
    original_stdout = (
        '{"effect_kind":"cursor.run_turn","attempt_id":"att-test","dispatch_id":"fx-test",'
        '"returncode":0,"parse_ok":true,"has_completion_signal":true}\n'
    )
    stdout_path.write_text(original_stdout, encoding="utf-8")
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    (tmp_path / result_rel).write_bytes(envelope)
    stdout_path.write_text('{"effect_kind":"cursor.run_turn","chat_id":"evil"}\n', encoding="utf-8")
    with pytest.raises((CursorEvidenceError, ValueError)):
        load_authenticated_cursor_outcome(
            tmp_path,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=0,
            expected_envelope_sha256=envelope_sha256(envelope),
            expected_dispatch_id="fx-test",
            expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_usage_limit_drift_blocks_durably(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "drift-block-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-drift-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(12):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    (git_repo / "drift-untracked.txt").write_text("drift\n", encoding="utf-8")
    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    tick_late = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: wait_until + timedelta(seconds=1),
        tick_owner_factory=lambda: f"tick-drift-late-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    receipt = tick_late.run_once()
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert "usage_limit" in state.block_reason_kind


def test_expired_tick_lease_skips_create_chat_scheduling(
    scheduler_paths: dict[str, Path],
) -> None:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    workflow = CursorWorkflowService(store, artifacts)
    dispatch_row = {"dispatch_id": "fx-preflight"}
    with patch(
        "ai_dev_loop.scheduler.application.cursor_workflow_service.tick_lease_is_active",
        return_value=False,
    ):
        receipt = workflow._run_preflight("stale-owner", 0, "run-test", dispatch_row)
    assert receipt.action == "preflight_stale"


def test_auth_probe_uses_bounded_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _hang(*args: object, **kwargs: object) -> ProcessResult:
        return ProcessResult(
            args=["agent", "status", "--format", "json"],
            returncode=124,
            stdout="",
            stderr="",
            timed_out=True,
        )

    monkeypatch.setattr("ai_dev_loop.runners.probes.run_process", _hang)
    result = probe_cursor_auth("agent")
    assert not result.ok
    assert "timed out" in result.detail.lower()


def test_auth_probe_timeout_constant_matches_scheduler_contract() -> None:
    from ai_dev_loop.scheduler.domain.cursor_contract import SCHEDULER_PROBE_TIMEOUT_SECONDS

    assert AUTH_PROBE_TIMEOUT_SECONDS == SCHEDULER_PROBE_TIMEOUT_SECONDS


def test_attempt_scoped_cursor_evidence_paths_are_distinct() -> None:
    first = cursor_attempt_events_rel(1, "att-first")
    second = cursor_attempt_events_rel(1, "att-second")
    assert first != second
    assert "att-first" in first
    assert "att-second" in second


def test_continuation_envelope_reused_without_overwrite(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    workflow = CursorWorkflowService(store, artifacts)
    run_id = "run-envelope"
    envelope = build_usage_limit_continuation_envelope("exact prompt bytes")
    rel = usage_limit_continuation_path_for_attempt(1, "att-one")
    first_path, first_sha = workflow._store_or_verify_continuation_envelope(run_id, rel, envelope)
    path = artifacts.run_root(run_id) / rel
    before_mtime = path.stat().st_mtime
    second_path, second_sha = workflow._store_or_verify_continuation_envelope(run_id, rel, envelope)
    assert first_path == second_path
    assert first_sha == second_sha
    assert path.stat().st_mtime == before_mtime
    with pytest.raises(CursorEvidenceError, match="conflicts"):
        workflow._store_or_verify_continuation_envelope(
            run_id,
            rel,
            build_usage_limit_continuation_envelope("different prompt"),
        )


def test_empty_successful_cursor_output_blocks_at_ingest() -> None:
    outcome = {
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "returncode": 0,
        "timed_out": False,
        "parse_ok": True,
        "has_completion_signal": False,
        "failure_code": None,
    }
    with pytest.raises(CursorEvidenceError, match="completion signal"):
        validate_cursor_turn_outcome_semantics(outcome)


def test_completion_envelope_hash_mismatch_blocks_authenticated_outcome(
    tmp_path: Path,
) -> None:
    attempt_id = "att-test"
    unit_identity = f"unit-{attempt_id}"
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = tmp_path / stdout_rel
    stderr_path = tmp_path / stderr_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("", encoding="utf-8")
    stdout_path.write_text(
        json.dumps(
            {
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "attempt_id": attempt_id,
                "dispatch_id": "fx-test",
                "returncode": 0,
                "parse_ok": True,
                "has_completion_signal": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    (tmp_path / result_rel).write_bytes(envelope)
    with pytest.raises(CursorEvidenceError, match="completion envelope hash"):
        load_authenticated_cursor_outcome(
            tmp_path,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=0,
            expected_envelope_sha256="0" * 64,
            expected_dispatch_id="fx-test",
            expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


class _TamperEvidenceBackend(FakeAgentProcessBackend):
    """Mutate invocation evidence after launch dispatch and before runner execution."""

    def _execute_cursor_attempt(self, request):  # type: ignore[no-untyped-def]
        from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

        argv = request.agent_argv
        run_id = argv[argv.index("--run-id") + 1]
        artifact_root = Path(argv[argv.index("--artifact-root") + 1])
        evidence_path = run_artifact_root(artifact_root, run_id) / invocation_evidence_rel(
            request.attempt_id
        )
        if evidence_path.is_file():
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                payload["run_id"] = "tampered-after-dispatch"
                evidence_path.write_text(
                    json.dumps(payload, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        return super()._execute_cursor_attempt(request)


class _ProtectedArtifactPreflightPort(SchedulerPreflightPort):
    def run(self, **kwargs: object) -> SchedulerPreflightResult:
        raise ProtectedArtifactError("protected prompt artifact missing")


def test_pinned_invocation_evidence_rejects_post_dispatch_tamper(tmp_path: Path) -> None:
    attempt_id = "att-" + "c" * 32
    run_id = "run-test"
    dispatch_id = "fx-dispatch"
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "prompt_path": "prompts/cursor-initial.txt",
        "prompt_sha256": "b" * 64,
        "repository_root": "/tmp/repo",
        "repository_git_common_dir": "/tmp/repo/.git",
        "repository_git_dir": "/tmp/repo/.git",
        "repository_branch": "main",
        "repository_initial_head": "abc123",
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    pinned = invocation_evidence_sha256(evidence)
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "invocation_evidence_sha256": pinned,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence["run_id"] = "tampered-after-dispatch"
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CursorEvidenceError, match="pinned invocation evidence digest mismatch"):
        authenticate_pinned_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            pinned_invocation_evidence_sha256=pinned,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_runner_rejects_evidence_tampered_after_backend_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "tamper-runner-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    backend = _TamperEvidenceBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
    )
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-tamper-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    for _ in range(16):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                assert state.block_reason_kind in {
                    "launch_guard_failed",
                    "outcome_evidence_invalid",
                    "invalid_chat_id",
                }
                _assert_capacity_free(store)
                return
            if state.kind in {"cursor_ready", "running_cursor"} and backend.launch_calls:
                break
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                _assert_capacity_free(store)
                return
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    assert state.kind == "blocked"
    _assert_capacity_free(store)


def test_fingerprint_drift_between_schedule_and_launch_blocks(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "schedule-launch-drift-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-schedule-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(12):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    tick_continue = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: wait_until + timedelta(seconds=1),
        tick_owner_factory=lambda: f"tick-schedule-late-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    tick_continue.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "cursor_ready"
    (git_repo / "drift-before-launch.txt").write_text("drift\n", encoding="utf-8")
    receipt = tick_continue.run_once()
    assert any(
        item.action in {"blocked", "attempt_launch_blocked", "attempt_ingest_blocked"}
        for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "launch_guard_failed"
    _assert_capacity_free(store)
    with store.begin_read() as conn:
        assert store.get_nonterminal_attempt_for_run(conn, run_id) is None
    tick_continue.run_once()
    _assert_capacity_free(store)


def test_preflight_lease_expiry_during_probe_skips_successor_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    base = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: base,
        tick_owner_factory=lambda: "tick-preflight-expire",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
        lease_ttl_seconds=30,
    )
    with patch.object(
        CursorWorkflowService,
        "_run_preflight",
        return_value=TickRunReceipt(run_id=run_id, action="preflight_deferred"),
    ):
        tick.run_once()
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "admitted"
        row = conn.execute(
            """
            SELECT dispatch_id FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.preflight_and_probes'
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        assert row is not None
        dispatch_id = str(row[0])
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id="tick-preflight-expire",
            now=base,
            ttl_seconds=30,
        )
        assert lease is not None
        lease_generation, _ = lease
    workflow = CursorWorkflowService(
        store,
        artifacts,
        preflight_port=OkPreflightPort(),
        now_factory=lambda: base + timedelta(seconds=31),
    )
    lease_checks = 0

    def lease_side_effect(store_obj, conn, *, owner_id, generation, now):  # type: ignore[no-untyped-def]
        nonlocal lease_checks
        lease_checks += 1
        return lease_checks == 1

    with patch(
        "ai_dev_loop.scheduler.application.cursor_workflow_service.tick_lease_is_active",
        side_effect=lease_side_effect,
    ):
        receipt = workflow._run_preflight(
            "tick-preflight-expire",
            lease_generation,
            run_id,
            {"dispatch_id": dispatch_id},
        )
    assert receipt.action == "preflight_stale"
    with store.begin_read() as conn:
        create_chat = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.create_chat'
            """,
            (run_id,),
        ).fetchone()
        assert create_chat is None


def test_preflight_protected_artifact_error_blocks_durably(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        tick_owner_factory=lambda: "tick-preflight-block",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=_ProtectedArtifactPreflightPort(),
    )
    receipt = tick.run_once()
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "preflight_validation_failed"


def test_scheduler_status_projects_wait_until_and_block_reason(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "status-projection-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-status-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(12):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    assert status.summary.cursor_wait_until == state.cursor.wait_until
    assert status.summary.block_reason_kind is None

    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    tick_late = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: wait_until + timedelta(seconds=1),
        tick_owner_factory=lambda: f"tick-status-late-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    (git_repo / "status-drift.txt").write_text("drift\n", encoding="utf-8")
    tick_late.run_once()
    blocked_status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    assert blocked_status.summary.block_reason_kind is not None
    assert "usage_limit" in blocked_status.summary.block_reason_kind


def test_cursor_attempt_runner_guard_failure_writes_result_envelope(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler import cursor_attempt_runner

    attempt_id = "att-" + "e" * 32
    run_id = "run-guard-envelope"
    dispatch_id = "fx-guard"
    artifact_root = tmp_path / "artifacts"
    run_root = run_artifact_root(artifact_root, run_id)
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "prompt_path": "prompts/cursor-initial.txt",
        "prompt_sha256": "b" * 64,
        "repository_root": str(tmp_path / "missing-repo"),
        "repository_git_common_dir": str(tmp_path / "missing-repo" / ".git"),
        "repository_git_dir": str(tmp_path / "missing-repo" / ".git"),
        "repository_branch": "main",
        "repository_initial_head": "abc123",
        "iteration": 1,
        "chat_id": "chat-test",
        "cursor_command": "agent",
        "cursor_model": "composer-2.5-fast",
        "cursor_output_format": "stream-json",
        "cursor_sandbox": "disabled",
        "timeout_seconds": 30,
    }
    rel = invocation_evidence_rel(attempt_id)
    path = run_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    pinned = invocation_evidence_sha256(evidence)
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "invocation_evidence_sha256": pinned,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    exit_code = cursor_attempt_runner.main(
        [
            "--run-id",
            run_id,
            "--attempt-id",
            attempt_id,
            "--unit-identity",
            f"unit-{attempt_id}",
            "--artifact-root",
            str(artifact_root),
            "--invocation-evidence-sha256",
            pinned,
            "--launch-intent-sha256",
            payload_sha256(launch_intent),
            "--launch-nonce",
            "nonce",
            "--dispatch-id",
            dispatch_id,
            "--effect-kind",
            RUN_CURSOR_TURN_EFFECT_KIND,
        ]
    )
    assert exit_code == 1
    result_path = run_root / attempt_result_rel(attempt_id)
    assert result_path.is_file()
    envelope = parse_result_envelope(result_path.read_bytes())
    assert envelope.attempt_id == attempt_id
    assert envelope.exit_code == 1
    assert envelope.termination_class == TerminationClass.NONZERO_EXIT.value


def test_head_drift_while_waiting_blocks_without_continuation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "head-drift-wait-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-head-wait-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(12):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    _git(git_repo, "commit", "--allow-empty", "-m", "head drift while waiting")
    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    tick_late = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: wait_until + timedelta(seconds=1),
        tick_owner_factory=lambda: f"tick-head-wait-late-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    receipt = tick_late.run_once()
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "usage_limit_continuation_guard_failed"
        pending_turn = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed')
            """,
            (run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
        assert pending_turn is None
    _assert_capacity_free(store)


def test_usage_limit_ingest_blocks_on_head_drift_after_deferred_tick(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(fake_clis["agent_log"].parent / "head-drift-ingest-counter.txt"),
    )
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-ingest-defer-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    defer_state = {"skipped": False}
    original_ingest = CursorWorkflowService._maybe_ingest_completed_attempt

    def defer_first_cursor_turn_ingest(
        self: CursorWorkflowService,
        tick_owner_id: str,
        tick_lease_generation: int,
        run_id_value: str,
    ) -> TickRunReceipt | None:
        with self.store.begin_read() as conn:
            attempt = self.store.get_latest_completed_cursor_attempt(conn, run_id_value)
            if attempt is None or int(attempt["ingested"]) != 0:
                return original_ingest(self, tick_owner_id, tick_lease_generation, run_id_value)
            dispatch = self.store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
            if (
                dispatch is not None
                and str(dispatch["effect_kind"]) == RUN_CURSOR_TURN_EFFECT_KIND
                and not defer_state["skipped"]
            ):
                defer_state["skipped"] = True
                return None
        return original_ingest(self, tick_owner_id, tick_lease_generation, run_id_value)

    with patch.object(
        CursorWorkflowService,
        "_maybe_ingest_completed_attempt",
        defer_first_cursor_turn_ingest,
    ):
        for _ in range(24):
            tick.run_once()
            with store.begin_read() as conn:
                attempt = store.get_latest_completed_cursor_attempt(conn, run_id)
                state, _, _ = store.load_validated_snapshot(conn, run_id)
                if (
                    defer_state["skipped"]
                    and attempt is not None
                    and int(attempt["ingested"]) == 0
                    and state.kind == "cursor_ready"
                ):
                    break
        assert defer_state["skipped"]
        with store.begin_read() as conn:
            attempt = store.get_latest_completed_cursor_attempt(conn, run_id)
            assert attempt is not None
            assert int(attempt["ingested"]) == 0
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            assert state.kind == "cursor_ready"
            usage_limit_attempt_id = str(attempt["attempt_id"])

    _git(git_repo, "commit", "--allow-empty", "-m", "head drift before usage-limit ingest")
    receipt = tick.run_once()
    assert any(item.action == "blocked" for item in receipt.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "usage_limit_ingest_guard_failed"
        attempt = store.get_attempt_by_id(conn, usage_limit_attempt_id)
        assert attempt is not None
        assert int(attempt["ingested"]) == 1
        pending_turn = conn.execute(
            """
            SELECT 1 FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = ? AND status IN ('pending', 'claimed')
            """,
            (run_id, RUN_CURSOR_TURN_EFFECT_KIND),
        ).fetchone()
        assert pending_turn is None
        assert store.get_nonterminal_attempt_for_run(conn, run_id) is None
    _assert_capacity_free(store)

    follow_up = tick.run_once()
    assert not any(item.action == "waiting_usage_limit" for item in follow_up.run_receipts)
    assert not any(item.action == "blocked" for item in follow_up.run_receipts)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        attempt = store.get_attempt_by_id(conn, usage_limit_attempt_id)
        assert attempt is not None
        assert int(attempt["ingested"]) == 1
    _assert_capacity_free(store)
