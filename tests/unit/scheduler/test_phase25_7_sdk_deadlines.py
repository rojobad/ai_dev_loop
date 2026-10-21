"""Phase 25.7 delivery B: send deadlines and safe wait evidence."""

from __future__ import annotations

import hashlib
import json
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cursor_sdk._connect import encode_stream_envelope
from tests.unit.scheduler.helpers import sample_submitted_state
from tests.unit.scheduler.test_phase25_2_sdk_worker import (
    AGENT_ID,
    FakeSdkAdapter,
    _runner_argv,
)
from tests.unit.scheduler.test_phase25_2_sdk_worker import (
    _evidence as _worker_evidence,
)
from tests.unit.scheduler.test_phase25_2_sdk_worker import (
    _repo as _worker_repo,
)
from tests.unit.scheduler.test_phase25_3_sdk_dispatch_uncertainty import InventoryAdapter
from tests.unit.scheduler.test_phase25_3_sdk_recovery import (
    NATIVE,
    RecoveryAdapter,
)
from tests.unit.scheduler.test_phase25_3_sdk_recovery import (
    _evidence as _recovery_evidence,
)
from tests.unit.scheduler.test_phase25_6_sdk_busy import (
    CREATION,
    ScriptedSdk,
    _prepare,
)

import ai_dev_loop.scheduler.cursor_attempt_runner as cursor_runner
from ai_dev_loop.commands.scheduler import render_scheduler_timeline_output
from ai_dev_loop.integrations.cursor_sdk.adapter import (
    ADMINISTRATIVE_RPC_TIMEOUT_SECONDS,
    CursorSdkAdapter,
    CursorSdkOperationBudgetExhausted,
    bind_cursor_sdk_operation_deadline,
    operation_budget_remaining,
    reset_cursor_sdk_operation_deadline,
)
from ai_dev_loop.integrations.cursor_sdk.port import (
    CursorSdkBridgeHealth,
    CursorSdkClientConfig,
    CursorSdkOutcome,
    CursorSdkPreflight,
)
from ai_dev_loop.paths import cursor_conversation_key
from ai_dev_loop.process import ProcessResult
from ai_dev_loop.scheduler.application.abort_reconcile import unit_observation_stopped
from ai_dev_loop.scheduler.application.attempt_backend import LaunchRequest
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_stderr_rel,
    attempt_stdout_rel,
)
from ai_dev_loop.scheduler.application.attempt_identity import unit_identity_from_attempt_id
from ai_dev_loop.scheduler.application.cursor_sdk_progress import (
    NATIVE_HANDLE_BOUND,
    SEND_AUTHORIZED,
    TERMINAL_OBSERVED,
    UNKNOWN_WAIT,
    append_sdk_wait_progress,
    cursor_sdk_wait_progress_is_diagnostic,
    format_sdk_wait_log,
    inspect_sdk_wait,
    seal_interrupted_sdk_wait_progress,
)
from ai_dev_loop.scheduler.application.systemd_backend import SystemdUserBackend
from ai_dev_loop.scheduler.application.timeline import SchedulerTimelineService
from ai_dev_loop.scheduler.cursor_attempt_runner import _run_sdk_reconciliation_operation
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_attempt_stderr_rel,
    cursor_sdk_wait_progress_rel,
)
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

SECRET = "phase257-wait-secret-sentinel"


def _progress(root: Path, attempt_id: str) -> Path:
    return root / cursor_sdk_wait_progress_rel(attempt_id)


def test_public_client_timeout_view_keeps_admin_rpcs_short_and_send_on_the_deadline(
    tmp_path: Path,
) -> None:
    import cursor_sdk

    client = cursor_sdk.Client.connect(
        "http://127.0.0.1:9",
        "not-a-credential",
        timeout=ADMINISTRATIVE_RPC_TIMEOUT_SECONDS,
    )
    calls: list[dict[str, float]] = []
    original = client.with_options

    def record(**kwargs: float) -> object:
        calls.append(dict(kwargs))
        return original(**kwargs)

    client.with_options = record  # type: ignore[method-assign]
    adapter = CursorSdkAdapter(
        CursorSdkClientConfig(
            sdk_version="1.0.37",
            workspace=tmp_path,
            state_root=tmp_path / "state",
            store_root=tmp_path / "store",
            model="grok-4.7",
            parameters=(),
            credential_ref="cursor-test",
            setting_sources=("project",),
            sandbox="enabled",
        ),
        repository_root=tmp_path,
    )
    adapter._client = client
    token = bind_cursor_sdk_operation_deadline(time.monotonic() + 12.5)
    try:
        send_view = adapter._deadline_client(stream=True)
        resume_view = adapter._deadline_client(stream=False)
        assert send_view._transport.unary_timeout == ADMINISTRATIVE_RPC_TIMEOUT_SECONDS
        assert send_view._transport.stream_timeout == pytest.approx(12.5, abs=0.5)
        assert resume_view._transport.unary_timeout == pytest.approx(12.5, abs=0.5)
        assert resume_view._transport.stream_timeout == ADMINISTRATIVE_RPC_TIMEOUT_SECONDS
        assert client._transport.unary_timeout == ADMINISTRATIVE_RPC_TIMEOUT_SECONDS
        assert client._transport.stream_timeout == ADMINISTRATIVE_RPC_TIMEOUT_SECONDS
        assert set(calls[0]) == {"stream_timeout"}
        assert calls[0]["stream_timeout"] == pytest.approx(12.5, abs=0.5)
        assert set(calls[1]) == {"unary_timeout"}
        assert calls[1]["unary_timeout"] == pytest.approx(12.5, abs=0.5)
    finally:
        reset_cursor_sdk_operation_deadline(token)
        client.close()

    exhausted = bind_cursor_sdk_operation_deadline(time.monotonic())
    time.sleep(0.02)
    calls.clear()
    try:
        with pytest.raises(CursorSdkOperationBudgetExhausted):
            adapter._deadline_client(stream=True)
        assert calls == []
    finally:
        reset_cursor_sdk_operation_deadline(exhausted)
        adapter._client = None


def test_authorized_send_outlasts_the_short_thresholds_inside_the_frozen_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}])
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    monkeypatch.setattr(cursor_runner, "MAX_CREATION_READINESS_SECONDS", 0.05)
    monkeypatch.setattr(
        "ai_dev_loop.integrations.cursor_sdk.adapter.ADMINISTRATIVE_RPC_TIMEOUT_SECONDS",
        0.15,
    )
    original = adapter.send
    seen: dict[str, float] = {}

    def delayed(agent_id: str, prompt: str):  # type: ignore[no-untyped-def]
        remaining = operation_budget_remaining()
        assert remaining is not None
        seen["remaining"] = remaining
        seen["itimer"] = signal.getitimer(signal.ITIMER_REAL)[0]
        time.sleep(0.4)
        return original(agent_id, prompt)

    adapter.send = delayed  # type: ignore[method-assign]
    outcome = cursor_runner._run_sdk_turn(evidence, run_root, attempt_id=attempt_id)

    assert adapter.send_calls == 1
    assert outcome["has_completion_signal"] is True
    assert seen["remaining"] > 1.0
    assert seen["itimer"] > 0.4
    assert outcome["sdk_wait_stage"] == "terminal_observed"
    assert outcome["sdk_wait_waiting_for_initial_response"] == "false"
    progress = _progress(run_root, attempt_id)
    kinds = [json.loads(line)["kind"] for line in progress.read_text().splitlines()]
    assert kinds == ["send_authorized", "native_handle_bound", "terminal_observed"]
    child_stderr = (run_root / cursor_attempt_stderr_rel(1, attempt_id)).read_text(encoding="utf-8")
    assert child_stderr == ""
    log = str(outcome["sdk_wait_log"])
    assert "waiting_for_initial_sdk_response=false" in log
    assert "stall" not in log and "implement exactly" not in log
    assert SECRET not in log


@pytest.mark.parametrize("operation", ["resume", "send", "stream"])
def test_frozen_deadline_interrupts_resume_send_and_stream(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
) -> None:
    repo = _worker_repo(tmp_path)
    artifact_root = tmp_path / "protected"
    run_id = f"project-sdk-deadline-{operation}"
    attempt_id = "att-" + {"resume": "d", "send": "e", "stream": "f"}[operation] * 32
    evidence = _worker_evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    prompt = run_artifact_root(artifact_root, run_id) / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    evidence.update(
        {
            "run_id": run_id,
            "conversation_owner_run_id": run_id,
            "conversation_key": cursor_conversation_key(run_id),
            "timeout_seconds": 1,
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": hashlib.sha256(b"implement exactly").hexdigest(),
        }
    )
    adapter = FakeSdkAdapter(repo)

    def block() -> None:
        while True:
            time.sleep(0.05)

    if operation == "resume":
        adapter.resume_agent = lambda agent_id: block()  # type: ignore[method-assign]
    elif operation == "send":
        adapter.before_send = block
    else:
        adapter.before_stream = block
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda binding: adapter)
    started = time.monotonic()
    exit_code = cursor_runner.main(
        _runner_argv(
            artifact_root=artifact_root,
            run_id=run_id,
            attempt_id=attempt_id,
            evidence=evidence,
        )
    )
    assert exit_code == 124
    assert time.monotonic() - started < 3
    if operation == "send":
        assert adapter.stream_calls == 0
    run_root = run_artifact_root(artifact_root, run_id)
    stdout = json.loads((run_root / attempt_stdout_rel(attempt_id)).read_text())
    stderr = (run_root / attempt_stderr_rel(attempt_id)).read_text(encoding="utf-8")
    assert "stall" not in stderr and "implement exactly" not in stderr
    assert "sdk_wait stage=" in stderr
    if operation == "resume":
        assert stdout.get("failure_kind") == "cursor_sdk_recovery_deferred"
        assert "sdk_wait_stage" not in stdout
    else:
        progress = _progress(run_root, attempt_id)
        early = inspect_sdk_wait(progress, now=datetime(2026, 10, 21, 1, 0, tzinfo=UTC))
        later = inspect_sdk_wait(progress, now=datetime(2026, 10, 22, 1, 0, tzinfo=UTC))
        assert early == later
        assert early.waiting_for_initial_response == "false"
        assert stdout["sdk_wait_stage"] == early.stage
        assert stdout["sdk_wait_waiting_for_initial_response"] == "false"
        if operation == "send":
            assert early.stage == "interrupted"
        else:
            assert early.stage == "native_handle_bound"


def test_wait_progress_seals_every_interrupted_prefix_without_inventing_progress(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "poison.jsonl"
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    append_sdk_wait_progress(
        secret_file,
        kind=SEND_AUTHORIZED,
        attempt_id="att-safe",
        recorded_at=moment,
    )
    record = json.loads(secret_file.read_text().splitlines()[0])
    record.update(
        {
            "text": SECRET,
            "native_status": "running",
            "cpu_percent": 99,
            "prompt": "do the secret thing",
        }
    )
    secret_file.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    poisoned = inspect_sdk_wait(secret_file, now=moment + timedelta(seconds=4))
    rendered = format_sdk_wait_log(poisoned)
    assert poisoned.stage == "waiting_for_initial_sdk_response"
    assert poisoned.elapsed_seconds == "4.000"
    assert poisoned.waiting_for_initial_response == "true"
    assert SECRET not in rendered and "running" not in rendered and "stall" not in rendered
    assert cursor_sdk_wait_progress_is_diagnostic(poisoned)

    missing = tmp_path / "absent.jsonl"
    assert inspect_sdk_wait(missing) == UNKNOWN_WAIT
    assert seal_interrupted_sdk_wait_progress(missing) is True
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert seal_interrupted_sdk_wait_progress(empty) is True
    assert empty.read_text(encoding="utf-8") == ""
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text("{", encoding="utf-8")
    assert seal_interrupted_sdk_wait_progress(corrupt) is False
    assert inspect_sdk_wait(corrupt) == UNKNOWN_WAIT
    nested = tmp_path / "nested.jsonl"
    nested.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": SEND_AUTHORIZED,
                "attempt_id": "att-safe",
                "recorded_at": moment.isoformat(),
                "extra": {"text": SECRET},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert inspect_sdk_wait(nested) == UNKNOWN_WAIT

    prefixes = (
        (SEND_AUTHORIZED,),
        (SEND_AUTHORIZED, NATIVE_HANDLE_BOUND),
    )
    for index, prefix in enumerate(prefixes):
        path = tmp_path / f"prefix-{index}.jsonl"
        for offset, kind in enumerate(prefix):
            append_sdk_wait_progress(
                path,
                kind=kind,
                attempt_id="att-safe",
                recorded_at=moment + timedelta(seconds=offset),
            )
        open_wait = inspect_sdk_wait(path, now=moment + timedelta(seconds=4))
        later_open_wait = inspect_sdk_wait(path, now=moment + timedelta(seconds=9))
        if prefix == (SEND_AUTHORIZED,):
            assert open_wait.stage == "waiting_for_initial_sdk_response"
            assert open_wait.waiting_for_initial_response == "true"
            assert open_wait.elapsed_seconds == "4.000"
            assert later_open_wait.elapsed_seconds == "9.000"
        assert seal_interrupted_sdk_wait_progress(path) is True
        assert seal_interrupted_sdk_wait_progress(path) is True
        kinds = tuple(json.loads(line)["kind"] for line in path.read_text().splitlines())
        assert kinds == (*prefix, "interrupted")
        inspection = inspect_sdk_wait(path, now=moment + timedelta(seconds=9))
        again = inspect_sdk_wait(path, now=moment + timedelta(hours=5))
        assert inspection == again
        assert "stall" not in format_sdk_wait_log(inspection)
        if NATIVE_HANDLE_BOUND in prefix:
            assert inspection.stage == NATIVE_HANDLE_BOUND
            assert inspection.waiting_for_initial_response == "false"
            assert inspection.elapsed_seconds == "1.000"
        else:
            assert inspection.stage == "interrupted"
            assert inspection.waiting_for_initial_response == "false"
            assert inspection.elapsed_seconds != "unknown"

    complete = tmp_path / "complete.jsonl"
    for offset, kind in enumerate((SEND_AUTHORIZED, NATIVE_HANDLE_BOUND, TERMINAL_OBSERVED)):
        append_sdk_wait_progress(
            complete,
            kind=kind,
            attempt_id="att-safe",
            recorded_at=moment + timedelta(seconds=offset),
        )
    assert seal_interrupted_sdk_wait_progress(complete) is True
    assert [json.loads(line)["kind"] for line in complete.read_text().splitlines()] == [
        SEND_AUTHORIZED,
        NATIVE_HANDLE_BOUND,
        TERMINAL_OBSERVED,
    ]
    symlink = tmp_path / "link.jsonl"
    symlink.symlink_to(complete)
    assert seal_interrupted_sdk_wait_progress(symlink) is False
    assert inspect_sdk_wait(symlink) == UNKNOWN_WAIT


def test_timeline_reads_wait_evidence_and_reports_unknown_without_it(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="timeline-wait-run")
    now = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    artifact_root = tmp_path / "artifacts"
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
        launch_text = now.isoformat().replace("+00:00", "Z")
        for index, (attempt_id, status) in enumerate(
            (("attempt-known", "active"), ("attempt-missing", "completed")),
            start=1,
        ):
            dispatch_id = f"dispatch-wait-{index}"
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, 'evt-submit', ?, ?, ?, ?, 'cursor.turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    index,
                    state.run_id,
                    dispatch_id,
                    f"{state.run_id}:{dispatch_id}",
                    "d" * 64,
                    launch_text,
                    launch_text,
                    launch_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, 'cursor', 1, ?, ?, ?, ?, NULL)
                """,
                (
                    attempt_id,
                    state.run_id,
                    dispatch_id,
                    status,
                    launch_text,
                    launch_text,
                    launch_text,
                ),
            )
    moment = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)
    append_sdk_wait_progress(
        run_artifact_root(artifact_root, state.run_id)
        / cursor_sdk_wait_progress_rel("attempt-known"),
        kind=SEND_AUTHORIZED,
        attempt_id="attempt-known",
        recorded_at=moment,
    )
    service = SchedulerTimelineService(store, artifact_root=artifact_root)
    timeline = service.get_timeline(state.run_id, limit=10, order="oldest")
    known = next(entry for entry in timeline.entries if entry.phase_attempt == 1)
    missing = next(entry for entry in timeline.entries if entry.phase_attempt == 2)
    assert known.sdk_wait_stage == "waiting_for_initial_sdk_response"
    assert known.sdk_wait_waiting_for_initial_response == "true"
    assert known.sdk_wait_elapsed_seconds != "unknown"
    assert missing.sdk_wait_stage == "unknown"
    assert missing.sdk_wait_elapsed_seconds == "unknown"
    assert missing.sdk_wait_waiting_for_initial_response == "unknown"
    rendered = render_scheduler_timeline_output(timeline, output="text")
    assert "sdk_wait=waiting_for_initial_sdk_response" in rendered
    assert "sdk_wait=unknown" in rendered
    assert "stall" not in rendered and SECRET not in rendered


@pytest.mark.parametrize(
    ("native", "run_ids", "expected"),
    [
        (None, (NATIVE,), "continuation_ready"),
        (None, (), "ambiguous_missing"),
        (None, (NATIVE, "run-019abc00-1111-4222-8333-666666666666"), "ambiguous_multiple"),
        (NATIVE, (NATIVE,), "continuation_ready"),
    ],
)
def test_lost_send_uses_existing_reconciliation_without_a_second_prompt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    native: str | None,
    run_ids: tuple[str, ...],
    expected: str,
) -> None:
    evidence, root = _recovery_evidence(tmp_path, native=native)
    source_attempt = str(evidence["source_attempt_id"])
    progress = _progress(root, source_attempt)
    append_sdk_wait_progress(
        progress,
        kind=SEND_AUTHORIZED,
        attempt_id=source_attempt,
        recorded_at=datetime(2026, 10, 20, 16, 0, tzinfo=UTC),
    )
    record = json.loads(progress.read_text().splitlines()[0])
    record["text"] = SECRET
    record["native_status"] = "running"
    progress.write_text(json.dumps(record) + "\n", encoding="utf-8")
    assert seal_interrupted_sdk_wait_progress(progress) is True
    inspection = inspect_sdk_wait(progress)
    rendered = format_sdk_wait_log(inspection)
    assert SECRET not in rendered and "running" not in rendered and "stall" not in rendered
    assert inspection.stage == "interrupted"
    assert inspection.waiting_for_initial_response == "false"
    assert inspect_sdk_wait(progress, now=datetime(2026, 10, 21, 16, 0, tzinfo=UTC)) == inspection
    adapter = InventoryAdapter(Path(str(evidence["repository_root"])), run_ids)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda value: adapter)

    outcome = _run_sdk_reconciliation_operation(
        evidence,
        root,
        attempt_id=str(evidence["attempt_id"]),
    )

    assert outcome["reconciliation_outcome"] == expected
    assert adapter.send_calls == 0


def test_completed_native_output_is_replayed_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    evidence, root = _recovery_evidence(tmp_path)
    adapter = RecoveryAdapter(
        Path(str(evidence["repository_root"])),
        initial="finished",
        final="finished",
    )
    calls = {"stream": 0}
    original = adapter.stream

    def once(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls["stream"] += 1
        return original(*args, **kwargs)

    adapter.stream = once  # type: ignore[method-assign]
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda value: adapter)
    outcome = _run_sdk_reconciliation_operation(
        evidence,
        root,
        attempt_id=str(evidence["attempt_id"]),
    )
    assert outcome["reconciliation_outcome"] == "terminal_recovered"
    assert calls["stream"] == 1
    assert adapter.send_calls == 0


def test_closed_wait_does_not_grow_and_unmeasurable_closure_is_unknown(tmp_path: Path) -> None:
    moment = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    active = tmp_path / "active.jsonl"
    append_sdk_wait_progress(
        active,
        kind=SEND_AUTHORIZED,
        attempt_id="att-safe",
        recorded_at=moment,
    )
    assert inspect_sdk_wait(active, now=moment + timedelta(seconds=2)).elapsed_seconds == "2.000"
    assert inspect_sdk_wait(active, now=moment + timedelta(seconds=7)).elapsed_seconds == "7.000"

    reversed_closure = tmp_path / "reversed.jsonl"
    reversed_closure.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": kind,
                    "attempt_id": "att-safe",
                    "recorded_at": recorded.isoformat(),
                },
                sort_keys=True,
            )
            for kind, recorded in (
                (SEND_AUTHORIZED, moment),
                ("interrupted", moment - timedelta(seconds=1)),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    stopped = inspect_sdk_wait(reversed_closure, now=moment + timedelta(days=2))
    assert stopped.stage == "interrupted"
    assert stopped.waiting_for_initial_response == "false"
    assert stopped.elapsed_seconds == "unknown"
    assert inspect_sdk_wait(reversed_closure, now=moment + timedelta(days=4)) == stopped


def test_ordinary_send_and_stream_failures_close_the_wait_without_touching_child_stderr(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = _worker_repo(tmp_path)
    artifact_root = tmp_path / "protected"
    cases = (
        ("send", "att-" + "a" * 32, True, False, "cursor_sdk_send_uncertain", "interrupted"),
        (
            "stream",
            "att-" + "b" * 32,
            False,
            True,
            "cursor_sdk_native_run_uncertain",
            "native_handle_bound",
        ),
    )
    for name, attempt_id, send_uncertain, stream_uncertain, failure_kind, stage in cases:
        run_id = f"project-sdk-close-{name}"
        evidence = _worker_evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
        prompt = run_artifact_root(artifact_root, run_id) / "prompts/cursor-initial.txt"
        prompt.parent.mkdir(parents=True, exist_ok=True)
        prompt.write_text("implement exactly", encoding="utf-8")
        evidence.update(
            {
                "run_id": run_id,
                "conversation_owner_run_id": run_id,
                "conversation_key": cursor_conversation_key(run_id),
                "iteration": 1,
                "agent_id": AGENT_ID,
                "prompt_path": "prompts/cursor-initial.txt",
                "prompt_sha256": hashlib.sha256(b"implement exactly").hexdigest(),
            }
        )
        adapter = FakeSdkAdapter(
            repo,
            send_uncertain=send_uncertain,
            stream_uncertain=stream_uncertain,
        )
        monkeypatch.setattr(
            cursor_runner,
            "_sdk_adapter",
            lambda binding, current=adapter: current,
        )
        exit_code = cursor_runner.main(
            _runner_argv(
                artifact_root=artifact_root,
                run_id=run_id,
                attempt_id=attempt_id,
                evidence=evidence,
            )
        )
        run_root = run_artifact_root(artifact_root, run_id)
        stdout = json.loads((run_root / attempt_stdout_rel(attempt_id)).read_text())
        result_stderr = (run_root / attempt_stderr_rel(attempt_id)).read_text(encoding="utf-8")
        child_stderr = (run_root / cursor_attempt_stderr_rel(1, attempt_id)).read_text(
            encoding="utf-8"
        )
        progress = _progress(run_root, attempt_id)
        early = inspect_sdk_wait(progress, now=datetime(2026, 10, 21, 2, 0, tzinfo=UTC))
        later = inspect_sdk_wait(progress, now=datetime(2026, 10, 23, 2, 0, tzinfo=UTC))
        assert exit_code == 1
        assert stdout["failure_kind"] == failure_kind
        assert early == later
        assert early.stage == stage
        assert early.waiting_for_initial_response == "false"
        assert stdout["sdk_wait_stage"] == stage
        assert result_stderr.startswith("sdk_wait stage=")
        assert "stall" not in result_stderr and "implement exactly" not in result_stderr
        assert child_stderr == ""


def test_readiness_send_failure_closes_the_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter = ScriptedSdk([{"id": CREATION, "status": "queued"}], send_modes=("network",))
    evidence, run_root, attempt_id = _prepare(monkeypatch, tmp_path, adapter)
    outcome = cursor_runner._run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    progress = _progress(run_root, attempt_id)
    early = inspect_sdk_wait(progress, now=datetime(2026, 10, 21, 4, 0, tzinfo=UTC))
    later = inspect_sdk_wait(progress, now=datetime(2026, 10, 24, 4, 0, tzinfo=UTC))
    assert outcome["failure_kind"] == "cursor_sdk_send_uncertain"
    assert adapter.send_calls == 1
    assert early == later
    assert early.stage == "interrupted"
    assert early.waiting_for_initial_response == "false"
    assert str(outcome["sdk_wait_log"]).startswith("sdk_wait stage=interrupted")
    assert (run_root / cursor_attempt_stderr_rel(1, attempt_id)).read_text(encoding="utf-8") == ""


_HTTP_AGENT = "019abc00-1111-4222-8333-444444444444"
_HTTP_RUN = "run-019abc00-1111-4222-8333-555555555555"


def _stream_body(*events: dict[str, object]) -> bytes:
    body = b"".join(encode_stream_envelope(event) for event in events)
    return body + encode_stream_envelope({}, end=True)


def _rpc_method(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _read_timeout(request: httpx.Request) -> float:
    timeout = request.extensions["timeout"]["read"]
    assert isinstance(timeout, (int, float))
    return float(timeout)


def _http_adapter(
    tmp_path: Path, *, admin_timeout: float = 0.15
) -> tuple[CursorSdkAdapter, list[tuple[str, float]], object]:
    calls: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = _rpc_method(str(request.url))
        read_timeout = _read_timeout(request)
        calls.append((method, read_timeout))
        if method == "ResumeAgent":
            return httpx.Response(200, json={"agentId": _HTTP_AGENT})
        if method == "ListRuns":
            return httpx.Response(200, json={"items": [], "nextCursor": ""})
        if method == "GetRun":
            return httpx.Response(
                200,
                json={
                    "run": {
                        "runId": _HTTP_RUN,
                        "agentId": _HTTP_AGENT,
                        "status": "finished",
                        "result": "finished text",
                    }
                },
            )
        if method in {"Send", "ObserveRun"}:
            if read_timeout <= admin_timeout + 0.05:
                raise httpx.ReadTimeout(
                    "short administrative cap reached the send stream",
                    request=request,
                )
            if method == "Send":
                time.sleep(0.35)
            message = {
                "offset": "1",
                "sdkMessage": {
                    "type": "system",
                    "subtype": "init",
                    "agentId": _HTTP_AGENT,
                    "runId": _HTTP_RUN,
                },
            }
            result = {
                "offset": "2",
                "result": {
                    "result": {
                        "runId": _HTTP_RUN,
                        "agentId": _HTTP_AGENT,
                        "status": "finished",
                        "result": "finished text",
                    }
                },
            }
            return httpx.Response(200, stream=httpx.ByteStream(_stream_body(message, result)))
        if method == "WaitLiveRun":
            return httpx.Response(
                200,
                json={
                    "result": {
                        "runId": _HTTP_RUN,
                        "agentId": _HTTP_AGENT,
                        "status": "finished",
                        "result": "finished text",
                    }
                },
            )
        raise AssertionError(f"unexpected RPC {method}")

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    import cursor_sdk

    client = cursor_sdk.Client.connect(
        "http://127.0.0.1:9",
        "not-a-credential",
        timeout=admin_timeout,
        http_client=http_client,
    )
    # Production launch_bridge uses this flag. A caller-supplied test bridge
    # otherwise rejects ObserveRun before the HTTP boundary.
    client._allow_api_key_env_fallback = True
    adapter = CursorSdkAdapter(
        CursorSdkClientConfig(
            sdk_version="1.0.37",
            workspace=tmp_path,
            state_root=tmp_path / "state",
            store_root=tmp_path / "store",
            model="grok-4.7",
            parameters=(("context", "256k"), ("fast", "false"), ("reasoning_effort", "high")),
            credential_ref="cursor-test",
            setting_sources=("project",),
            sandbox="enabled",
        ),
        repository_root=tmp_path,
    )
    adapter._client = client
    adapter._sdk = cursor_sdk
    adapter._api_key = "not-a-credential"
    adapter._health = CursorSdkBridgeHealth("1.0.37", "fake", "v1")
    return adapter, calls, client


def test_public_adapter_rpcs_follow_the_deadline_view_and_stop_when_it_is_gone(
    tmp_path: Path,
) -> None:
    adapter, calls, client = _http_adapter(tmp_path)
    token = bind_cursor_sdk_operation_deadline(time.monotonic() + 2.0)
    try:
        listed = adapter.list_runs(_HTTP_AGENT)
        assert listed.failure is None
        first = adapter.resume_agent(_HTTP_AGENT)
        assert first.failure is None
        time.sleep(0.25)
        second = adapter.resume_agent(_HTTP_AGENT)
        assert second.failure is None
        sent = adapter.send(_HTTP_AGENT, "implement exactly")
        assert sent.failure is None and sent.value is not None
        streamed = adapter.stream(_HTTP_AGENT, sent.value.run_id)
        assert streamed.failure is None
        replayed = adapter.stream(_HTTP_AGENT, sent.value.run_id, mode="replay")
        assert replayed.failure is None
        methods = [method for method, _timeout in calls]
        assert methods.count("Send") == 1
        assert "ObserveRun" in methods
        list_timeout = next(timeout for method, timeout in calls if method == "ListRuns")
        resume_timeouts = [timeout for method, timeout in calls if method == "ResumeAgent"]
        send_timeout = next(timeout for method, timeout in calls if method == "Send")
        observe_timeout = next(timeout for method, timeout in calls if method == "ObserveRun")
        assert list_timeout == pytest.approx(0.15, abs=0.02)
        assert resume_timeouts[0] > 1.0
        assert resume_timeouts[1] < resume_timeouts[0] - 0.15
        assert send_timeout > 1.0
        assert observe_timeout > 0.5
        seen = len(calls)
        exhausted = bind_cursor_sdk_operation_deadline(time.monotonic())
        time.sleep(0.02)
        try:
            with pytest.raises(CursorSdkOperationBudgetExhausted):
                adapter.resume_agent(_HTTP_AGENT)
            with pytest.raises(CursorSdkOperationBudgetExhausted):
                adapter.send(_HTTP_AGENT, "implement exactly")
            with pytest.raises(CursorSdkOperationBudgetExhausted):
                adapter.stream(_HTTP_AGENT, _HTTP_RUN)
            assert len(calls) == seen
        finally:
            reset_cursor_sdk_operation_deadline(exhausted)
    finally:
        reset_cursor_sdk_operation_deadline(token)
        client.close()


def test_worker_send_through_the_public_client_outlasts_the_short_caps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    adapter, calls, client = _http_adapter(tmp_path)
    health = adapter._health
    assert health is not None
    adapter.open = lambda: CursorSdkOutcome.success(health)  # type: ignore[method-assign]
    adapter.preflight = lambda: CursorSdkOutcome.success(  # type: ignore[method-assign]
        CursorSdkPreflight(
            health,
            True,
            "grok-4.7",
            (("context", "256k"), ("fast", "false"), ("reasoning_effort", "high")),
        )
    )
    repo = _worker_repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True, exist_ok=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    attempt_id = "att-" + "7" * 32
    evidence = _worker_evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "timeout_seconds": 3,
            "iteration": 1,
            "agent_id": _HTTP_AGENT,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": hashlib.sha256(b"implement exactly").hexdigest(),
        }
    )
    monkeypatch.setattr(cursor_runner, "MAX_CREATION_READINESS_SECONDS", 0.05)
    monkeypatch.setattr(cursor_runner, "_sdk_adapter", lambda binding: adapter)
    try:
        outcome = cursor_runner._run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    finally:
        client.close()
    assert outcome["has_completion_signal"] is True
    assert [method for method, _timeout in calls].count("Send") == 1
    send_timeout = next(timeout for method, timeout in calls if method == "Send")
    list_timeout = next(timeout for method, timeout in calls if method == "ListRuns")
    assert list_timeout == pytest.approx(0.15, abs=0.02)
    assert send_timeout > 1.0
    assert (run_root / cursor_attempt_stderr_rel(1, attempt_id)).read_text(encoding="utf-8") == ""


def test_owned_unit_stops_its_control_group_before_reconciliation(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    alive = {"launcher", "bridge-child", "tool-grandchild"}
    control_group_owned = False
    stopped = False
    attempt_id = "att-" + "8" * 32
    unit = unit_identity_from_attempt_id(attempt_id)

    def runner(args: list[str], **kwargs: object) -> ProcessResult:
        nonlocal control_group_owned, stopped
        del kwargs
        calls.append(list(args))
        if args[0] == "systemd-run":
            control_group_owned = "--property=KillMode=control-group" in args
            return ProcessResult(args=args, returncode=0, stdout="", stderr="")
        if args[:3] == ["systemctl", "--user", "stop"]:
            stopped = True
            alive.discard("launcher")
            if control_group_owned:
                alive.clear()
            return ProcessResult(args=args, returncode=0, stdout="", stderr="")
        if "show" in args:
            state = "inactive" if stopped else "active"
            substate = "dead" if stopped else "running"
            return ProcessResult(
                args=args,
                returncode=0,
                stdout=f"Id={unit}\nLoadState=loaded\nActiveState={state}\nSubState={substate}\n",
                stderr="",
            )
        return ProcessResult(args=args, returncode=0, stdout="", stderr="")

    backend = SystemdUserBackend(runner=runner)
    backend.launch(
        LaunchRequest(
            attempt_id=attempt_id,
            unit_identity=unit,
            working_directory=tmp_path,
            agent_argv=["fake-worker", "--spawns-bridge-and-tool-descendant"],
            lock_path=tmp_path / "attempt.lock",
            stdout_path=tmp_path / "stdout.txt",
            stderr_path=tmp_path / "stderr.txt",
            result_envelope_path=tmp_path / "result.json",
            execution_timeout_seconds=60,
        )
    )
    backend.terminate(unit_identity=unit, attempt_id=attempt_id)
    observation = backend.observe(unit_identity=unit, attempt_id=attempt_id)
    assert "--property=KillMode=control-group" in calls[0]
    assert unit_observation_stopped(observation)
    assert alive == set()
    reconciled = unit_observation_stopped(observation) and not alive
    assert reconciled is True
