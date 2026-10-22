"""Production-path coverage for one delayed SDK no-progress continuation."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from tests.integration.test_phase25_2_sdk_workflow import (
    _configure_v2,
    _credential,
    _install_fake_sdk,
)
from tests.integration.test_phase25_3_sdk_recovery import (
    _ATTEMPTS,
    _bind_fake_sdk_run,
    _submit_sdk_run,
    _tick_until_kind,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.scheduler.application import cursor_sdk_no_progress as no_progress
from ai_dev_loop.scheduler.application.cursor_sdk_retention import preview_cursor_sdk_cleanup
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_prepare import (
    SequencePrepareOptions,
    prepare_sequence,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

EXACT = "Agent turn stopped after repeated resume attempts made no progress"


def _instant(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _sdk_successors(store: SqliteSchedulerStore, source_run_id: str) -> list[object]:
    with store.begin_read() as conn:
        rows = store.list_cursor_initial_recoveries_for_source(conn, source_run_id=source_run_id)
    return [
        row for row in rows if json.loads(str(row["intent_payload"])).get("schema_version") == 3
    ]


def _tick_until_sdk_successor(tick, store, run_id, *, limit: int = 8):  # type: ignore[no-untyped-def]
    successors: list[object] = []
    for _ in range(limit):
        tick.run_once()
        successors = [
            item
            for item in _sdk_successors(store, run_id)
            if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
        ]
        if successors:
            return successors
    return successors


def test_exact_no_progress_waits_then_continues_once_and_holds_the_second(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    clock = [datetime(2027, 1, 1, tzinfo=UTC)]
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK no progress",
        now_factory=lambda: clock[0],
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    summary = blocked.block_reason_summary  # type: ignore[attr-defined]
    assert "did not establish" not in summary
    assert "reserved until" in summary
    assert EXACT not in summary
    with store.begin_read() as conn:
        reserved = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert reserved is not None
    assert str(reserved["status"]) == "reserved"
    assert int(reserved["consumed_count"]) == 1
    assert not isinstance(reserved["consumed_count"], bool)
    due = _instant(str(reserved["due_at"]))
    assert due - _instant(str(reserved["authorized_at"])) == timedelta(seconds=60)

    status = CliRunner().invoke(app, ["scheduler", "status", run_id, "--output", "json"])
    assert status.exit_code == 0, status.output
    payload = json.loads(status.stdout)["summary"]
    assert payload["sdk_no_progress_category"] == "cursor_sdk_resume_no_progress"
    assert payload["sdk_no_progress_action"] == "automatic"
    assert payload["sdk_no_progress_consumed"] == 1
    assert "network" not in status.stdout.lower()
    assert EXACT not in status.stdout
    history = CliRunner().invoke(app, ["scheduler", "history", run_id])
    assert history.exit_code == 0, history.output
    assert (
        "cursor_sdk_no_progress_authorized: category=cursor_sdk_resume_no_progress"
        in history.output
    )
    assert "action=automatic" in history.output
    assert "consumed=1" in history.output
    assert EXACT not in history.output
    early = CliRunner().invoke(app, ["scheduler", "implementer-retry", run_id, "--force"])
    assert early.exit_code == 0, early.output
    assert "publication=pending" in early.output
    assert "agent_execution_ready=no" in early.output
    assert _sdk_successors(store, run_id) == []

    clock[0] = due - timedelta(microseconds=1)
    tick.run_once()
    assert _sdk_successors(store, run_id) == []
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=isolated_xdg / "state/ai_dev_loop",
        now=clock[0] + timedelta(hours=169),
    )
    assert preview.conversations
    assert any("sdk_no_progress_pending" in item.manual_reasons for item in preview.conversations)

    clock[0] = due
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])
    with store.begin_read() as conn:
        published = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert published is not None and str(published["status"]) == "published"
    assert int(published["consumed_count"]) == 1

    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    second = _tick_until_kind(tick, store, successor_id, "blocked")
    assert second.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    assert "already used" in second.block_reason_summary  # type: ignore[attr-defined]
    for _ in range(3):
        tick.run_once()
    assert _sdk_successors(store, successor_id) == []
    with store.begin_read() as conn:
        still = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert still is not None and int(still["consumed_count"]) == 1
    forced = CliRunner().invoke(
        app,
        ["scheduler", "implementer-retry", successor_id, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    manual = json.loads(forced.stdout)
    assert manual["run_id"] not in {run_id, successor_id}
    assert _sdk_successors(store, successor_id)
    with store.begin_read() as conn:
        carried = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert carried is not None and int(carried["consumed_count"]) == 1


def test_prefixed_or_assistant_text_stays_manual(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_STATUS_MESSAGE", f"prefix {EXACT}")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK prefixed status",
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    assert "did not establish" not in blocked.block_reason_summary  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        assert store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id) is None


def test_assistant_text_equal_to_the_status_phrase_stays_manual(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_ASSISTANT_TEXT", EXACT)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK assistant text",
    )
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        assert store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id) is None


def test_abort_and_crash_and_duplicate_publish_share_one_continuation(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    clock = [datetime(2027, 2, 1, tzinfo=UTC)]
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK no progress race",
        now_factory=lambda: clock[0],
    )
    _tick_until_kind(tick, store, run_id, "blocked")
    with store.begin_read() as conn:
        reserved = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert reserved is not None
    due = _instant(str(reserved["due_at"]))

    def crash(point: str) -> None:
        if point == "after_claim":
            raise RuntimeError("injected publication crash")

    clock[0] = due
    no_progress.publish_fault_hook = crash
    try:
        with pytest.raises(RuntimeError, match="injected publication crash"):
            no_progress.publish_due_no_progress_continuations(
                store,
                ProtectedArtifactStore(_artifacts),
                now=clock[0],
            )
    finally:
        no_progress.publish_fault_hook = None
    assert _sdk_successors(store, run_id) == []
    with store.begin_read() as conn:
        claimed = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert claimed is not None and str(claimed["status"]) == "publishing"

    for _ in range(2):
        tick.run_once()
    assert _sdk_successors(store, run_id) == []

    barrier = threading.Barrier(2)
    entered = threading.Barrier(2)

    def overlap(point: str) -> None:
        if point == "before_force":
            entered.wait(timeout=5)
            barrier.wait(timeout=5)

    no_progress.publish_fault_hook = overlap
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            no_progress.publish_due_no_progress_continuations(
                store,
                ProtectedArtifactStore(_artifacts),
                now=clock[0],
            )
        except BaseException as exc:  # noqa: BLE001 - the race records any publisher failure
            errors.append(exc)

    try:
        first = threading.Thread(target=publish)
        second = threading.Thread(target=publish)
        first.start()
        second.start()
        first.join(10)
        second.join(10)
    finally:
        no_progress.publish_fault_hook = None
    assert errors == []
    assert len(_sdk_successors(store, run_id)) == 1


def test_abort_before_due_does_not_dispatch(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    clock = [datetime(2027, 3, 1, tzinfo=UTC)]
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK no progress abort",
        now_factory=lambda: clock[0],
    )
    _tick_until_kind(tick, store, run_id, "blocked")
    aborted = CliRunner().invoke(app, ["scheduler", "abort", run_id])
    assert aborted.exit_code == 0, aborted.output
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None
    assert str(row["status"]) == "cancelled"
    assert int(row["consumed_count"]) == 1
    clock[0] = _instant(str(row["due_at"]))
    tick.run_once()
    assert _sdk_successors(store, run_id) == []


def _write_no_progress_manifest(git_repo):  # type: ignore[no-untyped-def]
    manifest = git_repo / "sdk-no-progress-v4.yaml"
    sdk_cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "sdk no progress sequence",
                "short_name": "SDK no progress seq",
                "phases": [
                    {
                        "name": "sdk-leaf",
                        "short_name": "SDK no progress leaf",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "commit_message": "accepted SDK no-progress checkpoint",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {"provider": "cursor"},
                    },
                    {
                        "name": "codex-final",
                        "short_name": "Codex final leaf",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {
                            "provider": "codex",
                            "command": "codex",
                            "model": "gpt-6.1-sol",
                            "reasoning_effort": "high",
                            "timeout_minutes": 90,
                        },
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return manifest


def test_current_sequence_leaf_is_held_until_the_continuation_is_adopted(
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "sequence no-progress baseline"], cwd=git_repo, check=True
    )
    fake_sdk = tmp_path / "fake-sdk-no-progress-sequence"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    manifest = _write_no_progress_manifest(git_repo)
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 4, 1, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-sequence-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    blocked = _tick_until_kind(tick, store, started.run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    for _ in range(4):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        assert sequence.current_run_id == started.run_id  # type: ignore[attr-defined]
        assert sequence.current_ordinal == 1  # type: ignore[attr-defined]
        assert _sdk_successors(store, started.run_id) == []
    assert type(sequence).__name__ == "BlockedSequenceState"
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
        reservation = store.get_reservation_for_run(conn, started.run_id)
    assert row is not None and str(row["status"]) == "reserved"
    assert reservation is not None
    clock[0] = _instant(str(row["due_at"]))
    adopted = None
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            adopted = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(adopted, "current_run_id", None) != started.run_id:
            break
    assert adopted is not None
    assert type(adopted).__name__ != "BlockedSequenceState"
    assert adopted.current_run_id != started.run_id  # type: ignore[attr-defined]
    assert adopted.current_ordinal == 1  # type: ignore[attr-defined]
    _bind_fake_sdk_run(monkeypatch, artifacts_root, str(adopted.current_run_id))  # type: ignore[attr-defined]
    later = adopted
    for _ in range(16):
        tick.run_once()
        with store.begin_read() as conn:
            later = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if type(later).__name__ == "BlockedSequenceState":
            break
    assert type(later).__name__ == "BlockedSequenceState"
    assert later.current_ordinal == 1  # type: ignore[attr-defined]
    assert later.current_run_id != started.run_id  # type: ignore[attr-defined]


def _pending_no_progress(git_repo, isolated_xdg, monkeypatch, tmp_path, *, short_name: str):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    counter = tmp_path / f"sends-{short_name.replace(' ', '-')}"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(counter))
    clock = [datetime(2027, 5, 1, tzinfo=UTC)]
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name=short_name,
        now_factory=lambda: clock[0],
    )
    _tick_until_kind(tick, store, run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None
    assert counter.read_text(encoding="utf-8") == "1"
    return run_id, artifacts_root, store, tick, clock, row, counter


@pytest.mark.parametrize("native_state", ["running", "finished", "unavailable"])
def test_changed_native_evidence_does_not_send_again(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
    native_state: str,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name=f"SDK native {native_state}",
    )
    if native_state == "unavailable":
        monkeypatch.setenv("FAKE_CURSOR_SDK_NATIVE_UNAVAILABLE", "1")
    else:
        monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", native_state)
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    clock[0] = _instant(str(row["due_at"]))
    if native_state == "finished":
        finished = _tick_until_kind(tick, store, run_id, "completed")
        assert finished.kind == "completed"  # type: ignore[attr-defined]
        with store.begin_read() as conn:
            replayed = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        assert replayed is not None and str(replayed["status"]) == "cancelled"
    else:
        for _ in range(8):
            tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            held = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        assert state.kind == "blocked"
        assert held is not None and str(held["status"]) == "held"
    successors = [
        item
        for item in _sdk_successors(store, run_id)
        if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
    ]
    assert successors == []
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert kept is not None and int(kept["consumed_count"]) == 1
    del artifacts_root


def test_changed_capture_blocks_automatic_publication(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, _artifacts, store, tick, clock, row, _counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK capture digest",
    )
    root = ProtectedArtifactStore(_artifacts).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    path = root / witness["events_path"]
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(line for line in lines if b"repeated resume attempts" in line))
    clock[0] = _instant(str(row["due_at"]))
    tick.run_once()
    assert _sdk_successors(store, run_id) == []
    with store.begin_read() as conn:
        held = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert held is not None and str(held["status"]) == "held"
    assert int(held["consumed_count"]) == 1


def test_abort_during_publication_cancels_without_dispatch(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import CursorSdkSuccessorService

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK abort publication",
    )
    errors: list[str] = []
    original = CursorSdkSuccessorService._publish_artifacts

    def abort_then_publish(self, intent, **kwargs):  # type: ignore[no-untyped-def]
        try:
            SchedulerAbortService(
                store,
                FakeAgentProcessBackend(),
                artifacts=ProtectedArtifactStore(artifacts_root),
                now_factory=lambda: clock[0],
            ).abort_run(run_id)
        except Exception as exc:  # noqa: BLE001 - the regression records the abort failure
            errors.append(str(exc))
        return original(self, intent, **kwargs)

    monkeypatch.setattr(CursorSdkSuccessorService, "_publish_artifacts", abort_then_publish)
    clock[0] = _instant(str(row["due_at"]))
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "aborted":
            break
    assert errors == []
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        authorization = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        reservation = store.get_reservation_for_run(conn, run_id)
    assert state.kind == "aborted"
    assert authorization is not None and str(authorization["status"]) == "cancelled"
    assert reservation is None
    ready = [
        item
        for item in _sdk_successors(store, run_id)
        if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
    ]
    assert ready == []
    for _ in range(3):
        tick.run_once()
    assert counter.read_text(encoding="utf-8") == "1"


def test_crash_after_force_publishes_the_existing_successor(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, _artifacts, store, tick, clock, row, _counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK crash after force",
    )

    def crash(point: str) -> None:
        if point == "after_force":
            raise RuntimeError("review crash after force")

    clock[0] = _instant(str(row["due_at"]))
    monkeypatch.setattr(no_progress, "publish_fault_hook", crash)
    with pytest.raises(RuntimeError, match="review crash"):
        for _ in range(8):
            tick.run_once()
    monkeypatch.setattr(no_progress, "publish_fault_hook", None)
    tick.run_once()
    with store.begin_read() as conn:
        recovered = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert recovered is not None
    assert str(recovered["status"]) == "published"
    assert int(recovered["consumed_count"]) == 1
    assert len(_sdk_successors(store, run_id)) == 1


def test_manual_force_during_authorization_reuses_the_pending_continuation(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
        ImplementerRetryService,
    )

    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    clock = [datetime(2027, 6, 1, tzinfo=UTC)]
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK manual race",
        now_factory=lambda: clock[0],
    )
    queried = threading.Event()
    proceed = threading.Event()
    original = no_progress.pending_automatic_retry_result

    def pause_before_write(*args, **kwargs):  # type: ignore[no-untyped-def]
        result = original(*args, **kwargs)
        assert result is None
        queried.set()
        assert proceed.wait(timeout=20)
        return result

    monkeypatch.setattr(no_progress, "pending_automatic_retry_result", pause_before_write)
    results: list[object] = []
    errors: list[str] = []

    def force() -> None:
        try:
            results.append(
                ImplementerRetryService(
                    store,
                    ProtectedArtifactStore(artifacts_root),
                    now_factory=lambda: clock[0],
                ).retry(run_id, force=True)
            )
        except Exception as exc:  # noqa: BLE001 - the race records the manual caller failure
            errors.append(str(exc))

    thread = threading.Thread(target=force)
    thread.start()
    assert queried.wait(timeout=20)
    _tick_until_kind(tick, store, run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None and clock[0] < _instant(str(row["due_at"]))
    proceed.set()
    thread.join(timeout=20)
    assert not thread.is_alive()
    assert errors == []
    assert results
    assert results[0].changed is False  # type: ignore[attr-defined]
    assert results[0].available_at  # type: ignore[attr-defined]
    assert _sdk_successors(store, run_id) == []


def test_successful_automatic_initial_continuation_reaches_review(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK automatic success",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    durable = tmp_path / "native-store.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    completed = _tick_until_kind(tick, store, successor_id, "completed")
    assert completed.kind == "completed"
    assert counter.read_text(encoding="utf-8") == "2"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        head = conn.execute("SELECT COUNT(*) AS n FROM scheduler_runs").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert str(kept["status"]) == "published"
    assert head is not None


def test_abort_at_relation_boundary_cancels_without_dispatch(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import CursorSdkSuccessorService

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK abort relation",
    )
    errors: list[str] = []
    original = CursorSdkSuccessorService._cancel_if_aborted

    def abort_at_relation(self, intent):  # type: ignore[no-untyped-def]
        try:
            SchedulerAbortService(
                store,
                FakeAgentProcessBackend(),
                artifacts=ProtectedArtifactStore(artifacts_root),
                now_factory=lambda: clock[0],
            ).abort_run(run_id)
        except Exception as exc:  # noqa: BLE001 - the regression records the abort failure
            errors.append(str(exc))
        return original(self, intent)

    monkeypatch.setattr(CursorSdkSuccessorService, "_cancel_if_aborted", abort_at_relation)
    clock[0] = _instant(str(row["due_at"]))
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
        if state.kind == "aborted":
            break
    assert errors == []
    assert counter.read_text(encoding="utf-8") == "1"
    ready = [
        item
        for item in _sdk_successors(store, run_id)
        if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
    ]
    assert ready == []
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        reservation = store.get_reservation_for_run(conn, run_id)
    assert state.kind == "aborted"
    assert reservation is None


def test_capacity_descendant_does_not_refresh_the_allowance(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK capacity descendant",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    monkeypatch.setenv("FAKE_CURSOR_SDK_RATE_LIMIT", "1")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    waiting = _tick_until_kind(tick, store, successor_id, "waiting_usage_limit")
    assert waiting.kind == "waiting_usage_limit"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1
    assert counter.read_text(encoding="utf-8") == "2"
    clock[0] = _instant(str(waiting.cursor.wait_until)) + timedelta(seconds=1)  # type: ignore[attr-defined]
    monkeypatch.delenv("FAKE_CURSOR_SDK_RATE_LIMIT", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    challenged = _tick_until_kind(tick, store, successor_id, "blocked")
    assert "already used" in challenged.block_reason_summary  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "3"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1


def test_automatic_correction_keeps_reviewer_base_note_and_ceiling(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.domain.cursor_initial_recovery import RECOVERY_NOTE
    from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
        SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL,
    )

    reviewer_id = "019abc00-1111-4222-8333-777777777777"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", reviewer_id)
    clock = [datetime(2027, 7, 1, tzinfo=UTC)]
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK correction continuation",
        now_factory=lambda: clock[0],
    )
    waiting = _tick_until_kind(tick, store, run_id, "waiting_for_cursor_fix")
    ceiling = waiting.context.workflow.max_review_iterations  # type: ignore[attr-defined]
    envelope_path = waiting.codex.latest_correction_envelope_path  # type: ignore[attr-defined]
    envelope = (
        ProtectedArtifactStore(artifacts_root).run_root(run_id) / str(envelope_path)
    ).read_bytes()
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None and int(row["consumed_count"]) == 1
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    effective = (
        ProtectedArtifactStore(artifacts_root).run_root(successor_id)
        / SDK_SUCCESSOR_EFFECTIVE_PROMPT_REL
    ).read_bytes()
    assert effective.startswith(envelope)
    assert effective.count(RECOVERY_NOTE.encode()) == 1
    root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    durable = tmp_path / "correction-native.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    completed = _tick_until_kind(tick, store, successor_id, "completed")
    assert completed.codex.reviewer_session_id == reviewer_id  # type: ignore[attr-defined]
    assert completed.context.workflow.max_review_iterations == ceiling  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert kept is not None and int(kept["consumed_count"]) == 1


def test_abort_during_sequence_adoption_does_not_adopt_or_dispatch(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    import subprocess

    from ai_dev_loop.scheduler.application import sequence_cursor_recovery
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService

    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(
        ["git", "commit", "-qm", "sequence adoption abort baseline"], cwd=git_repo, check=True
    )
    fake_sdk = tmp_path / "fake-sdk-adoption-abort"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    counter = tmp_path / "adoption-sends"
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    manifest = _write_no_progress_manifest(git_repo)
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 8, 1, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-adopt-abort-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    _tick_until_kind(tick, store, started.run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert row is not None
    errors: list[str] = []
    original = sequence_cursor_recovery.ensure_sequence_cursor_adoption

    def abort_then_adopt(*args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            SchedulerAbortService(
                store,
                FakeAgentProcessBackend(),
                artifacts=ProtectedArtifactStore(artifacts_root),
                now_factory=lambda: clock[0],
            ).abort_run(started.run_id)
        except Exception as exc:  # noqa: BLE001 - the regression records the abort failure
            errors.append(str(exc))
        return original(*args, **kwargs)

    monkeypatch.setattr(
        sequence_cursor_recovery,
        "ensure_sequence_cursor_adoption",
        abort_then_adopt,
    )
    clock[0] = _instant(str(row["due_at"]))
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            source, _, _ = store.load_validated_snapshot(conn, started.run_id)
        if source.kind == "aborted":
            break
    assert errors == []
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        source, _, _ = store.load_validated_snapshot(conn, started.run_id)
        reservation = store.get_reservation_for_run(conn, started.run_id)
    assert source.kind == "aborted"
    assert sequence.current_run_id == started.run_id  # type: ignore[attr-defined]
    assert sequence.current_ordinal == 1  # type: ignore[attr-defined]
    assert reservation is None
    ready = [
        item
        for item in _sdk_successors(store, started.run_id)
        if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
    ]
    assert ready == []
    for _ in range(3):
        tick.run_once()
    assert counter.read_text(encoding="utf-8") == "1"


def test_successful_sequence_continuation_checkpoints_the_non_final_phase_once(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    import subprocess

    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sequence success baseline"], cwd=git_repo, check=True)
    baseline = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fake_sdk = tmp_path / "fake-sdk-sequence-success"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    manifest = _write_no_progress_manifest(git_repo)
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 9, 1, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-seq-success-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    _tick_until_kind(tick, store, started.run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert row is not None
    clock[0] = _instant(str(row["due_at"]))
    successor_id = None
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(sequence, "current_run_id", None) != started.run_id:
            successor_id = str(sequence.current_run_id)  # type: ignore[attr-defined]
            break
    assert successor_id is not None
    root = ProtectedArtifactStore(artifacts_root).run_root(started.run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    durable = tmp_path / "sequence-native.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    advanced = None
    for _ in range(24):
        tick.run_once()
        with store.begin_read() as conn:
            advanced = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(advanced, "current_ordinal", None) == 2:
            break
    assert advanced is not None
    assert advanced.current_ordinal == 2  # type: ignore[attr-defined]
    after_checkpoint = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert int(after_checkpoint) == int(baseline) + 1
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert message == "accepted SDK no-progress checkpoint"
    for _ in range(8):
        tick.run_once()
    with store.begin_read() as conn:
        final = store.load_validated_sequence_state(conn, prepared.sequence_id)
    assert final.current_ordinal == 2  # type: ignore[attr-defined]
    final_count = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert final_count == after_checkpoint


def test_manual_descendant_executes_without_refreshing_the_allowance(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK manual descendant",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    second = _tick_until_kind(tick, store, successor_id, "blocked")
    assert "already used" in second.block_reason_summary  # type: ignore[attr-defined]
    forced = CliRunner().invoke(
        app,
        ["scheduler", "implementer-retry", successor_id, "--force", "--output", "json"],
    )
    assert forced.exit_code == 0, forced.output
    manual_id = str(json.loads(forced.stdout)["run_id"])
    assert manual_id not in {run_id, successor_id}
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, manual_id)
    completed = _tick_until_kind(tick, store, manual_id, "completed")
    assert completed.kind == "completed"
    assert counter.read_text(encoding="utf-8") == "3"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert str(kept["status"]) == "published"
    assert rows is not None and int(rows["n"]) == 1


def test_timeout_descendant_does_not_refresh_the_allowance(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK timeout descendant",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", "1")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    state = None
    for _ in range(16):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, successor_id)
        if getattr(getattr(state, "cursor", None), "timeout_automatic_retries", 0) >= 1:
            break
    assert state is not None
    assert state.cursor.timeout_automatic_retries == 1  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "2"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert str(kept["status"]) == "published"
    assert rows is not None and int(rows["n"]) == 1
    from ai_dev_loop.scheduler.application.cursor_timeout_retry import TIMEOUT_RETRY_DELAY

    clock[0] = clock[0] + TIMEOUT_RETRY_DELAY + timedelta(seconds=1)
    monkeypatch.delenv("FAKE_CURSOR_SDK_TRIGGER_DEADLINE", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    challenged = _tick_until_kind(tick, store, successor_id, "blocked")
    assert "already used" in challenged.block_reason_summary  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "3"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1


def test_new_correction_receives_a_fresh_allowance(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK fresh correction",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    durable = tmp_path / "fresh-native.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    waiting = _tick_until_kind(tick, store, successor_id, "waiting_for_cursor_fix")
    assert waiting.kind == "waiting_for_cursor_fix"
    monkeypatch.delenv("FAKE_CURSOR_SDK_DURABLE_STORE", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    blocked = _tick_until_kind(tick, store, successor_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    assert "reserved until" in blocked.block_reason_summary  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        original = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        fresh = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=successor_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert original is not None and fresh is not None and rows is not None
    assert int(original["consumed_count"]) == 1 and str(original["status"]) == "published"
    assert int(fresh["consumed_count"]) == 1 and str(fresh["status"]) == "reserved"
    assert str(original["logical_turn_key"]) != str(fresh["logical_turn_key"])
    assert int(rows["n"]) == 2
    assert counter.read_text(encoding="utf-8") == "3"


def test_final_sequence_phase_stays_staged_without_a_commit(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    import subprocess

    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "final phase baseline"], cwd=git_repo, check=True)
    baseline = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    fake_sdk = tmp_path / "fake-sdk-final-phase"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    sdk_cursor = {
        "runtime": "sdk-local",
        "model": "grok-4.7",
        "parameters": {"context": "256k", "reasoning_effort": "high", "fast": False},
    }
    manifest = git_repo / "sdk-final-phase-v4.yaml"
    manifest.write_text(
        yaml.safe_dump(
            {
                "schema_version": 4,
                "name": "sdk final phase sequence",
                "short_name": "SDK final phase seq",
                "phases": [
                    {
                        "name": "sdk-first",
                        "short_name": "SDK first checkpoint",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "commit_message": "accepted SDK first checkpoint",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {"provider": "cursor"},
                    },
                    {
                        "name": "sdk-final",
                        "short_name": "SDK final staged",
                        "plan_path": "docs/plans/sample-plan.md",
                        "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                        "cursor": sdk_cursor,
                        "codex": {
                            "review_model": "gpt-6.1-sol",
                            "review_reasoning_effort": "high",
                        },
                        "implementer": {"provider": "cursor"},
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 10, 1, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-final-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    phase2_id = None
    for _ in range(40):
        with store.begin_read() as conn:
            phase1, _, _ = store.load_validated_snapshot(conn, started.run_id)
        if phase1.kind in {"completed", "completed_with_residual_risk"}:
            monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
            monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if (
            getattr(sequence, "current_ordinal", None) == 2
            and getattr(sequence, "current_run_id", None) != started.run_id
        ):
            phase2_id = str(sequence.current_run_id)  # type: ignore[attr-defined]
            monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
            monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
            break
    assert phase2_id is not None
    after_checkpoint = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert after_checkpoint == baseline + 1
    _bind_fake_sdk_run(monkeypatch, artifacts_root, phase2_id)
    blocked = _tick_until_kind(tick, store, phase2_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=phase2_id)
    assert row is not None and int(row["consumed_count"]) == 1
    clock[0] = _instant(str(row["due_at"]))
    successor_id = None
    for _ in range(8):
        tick.run_once()
        with store.begin_read() as conn:
            sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(sequence, "current_run_id", None) not in {started.run_id, phase2_id}:
            successor_id = str(sequence.current_run_id)  # type: ignore[attr-defined]
            break
    assert successor_id is not None
    witness = json.loads(
        (
            ProtectedArtifactStore(artifacts_root).run_root(phase2_id) / str(row["witness_path"])
        ).read_text(encoding="utf-8")
    )
    durable = tmp_path / "final-native.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    finalized = None
    for _ in range(40):
        tick.run_once()
        with store.begin_read() as conn:
            finalized = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if type(finalized).__name__ == "AwaitingFinalizationSequenceState":
            break
    assert type(finalized).__name__ == "AwaitingFinalizationSequenceState"
    final_count = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert final_count == after_checkpoint
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "tracked.txt" in staged


def test_finished_correction_replay_reaches_completion(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    counter = tmp_path / "correction-replay-sends"
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(counter))
    clock = [datetime(2027, 9, 1, tzinfo=UTC)]
    run_id, _db, _artifacts, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK correction replay",
        now_factory=lambda: clock[0],
    )
    _tick_until_kind(tick, store, run_id, "waiting_for_cursor_fix")
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    _tick_until_kind(tick, store, run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None
    assert counter.read_text(encoding="utf-8") == "2"
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    clock[0] = _instant(str(row["due_at"]))
    completed = _tick_until_kind(tick, store, run_id, "completed")
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "2"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert str(kept["status"]) == "cancelled"
    assert _sdk_successors(store, run_id) == []


def test_finished_sequence_replay_advances_without_another_prompt(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    import subprocess

    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sequence replay baseline"], cwd=git_repo, check=True)
    baseline = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    fake_sdk = tmp_path / "fake-sdk-sequence-replay"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    counter = tmp_path / "sequence-replay-sends"
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=_write_no_progress_manifest(git_repo),
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 8, 2, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-seq-replay-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    _tick_until_kind(tick, store, started.run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert row is not None
    assert counter.read_text(encoding="utf-8") == "1"
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    clock[0] = _instant(str(row["due_at"]))
    advanced = None
    for _ in range(40):
        tick.run_once()
        with store.begin_read() as conn:
            advanced = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(advanced, "current_ordinal", 1) > 1:
            break
    assert advanced is not None
    with store.begin_read() as conn:
        leaf_id = str(getattr(advanced, "current_run_id", ""))
        leaf, _, _ = store.load_validated_snapshot(conn, leaf_id)
        source, _, _ = store.load_validated_snapshot(conn, started.run_id)
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert getattr(advanced, "current_ordinal", None) == 2, (
        f"sequence={type(advanced).__name__} ordinal={getattr(advanced, 'current_ordinal', None)} "
        f"leaf={leaf_id} leaf_kind={getattr(leaf, 'kind', None)} "
        f"leaf_block={getattr(leaf, 'block_reason_kind', None)}:{getattr(leaf, 'block_reason_summary', None)} "
        f"source_kind={source.kind} auth={None if auth is None else auth['status']} "
        f"sends={counter.read_text(encoding='utf-8')}"
    )
    assert advanced.current_run_id != started.run_id  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "1"
    after = int(
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=git_repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    assert after == baseline + 1
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert message == "accepted SDK no-progress checkpoint"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert kept is not None and int(kept["consumed_count"]) == 1


def test_postpublication_capture_damage_blocks_send(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK predispatch damage",
    )

    def crash(point: str) -> None:
        if point == "after_force":
            raise RuntimeError("review crash after force")

    clock[0] = _instant(str(row["due_at"]))
    monkeypatch.setattr(no_progress, "publish_fault_hook", crash)
    with pytest.raises(RuntimeError, match="review crash"):
        for _ in range(8):
            tick.run_once()
    monkeypatch.setattr(no_progress, "publish_fault_hook", None)
    tick.run_once()
    successors = _sdk_successors(store, run_id)
    assert len(successors) == 1
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    capture = root / witness["events_path"]
    capture.write_bytes(
        b"".join(
            line
            for line in capture.read_bytes().splitlines(keepends=True)
            if b"repeated resume attempts" in line
        )
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    for _ in range(5):
        tick.run_once()
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and str(kept["status"]) == "published"
    assert int(kept["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1


def test_abort_ready_unstarted_successor_validates_cancelled_intent(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import (
        SDK_SUCCESSOR_RECORD_REL,
        CursorSdkSuccessorIntentV1,
    )

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK cancelled intent",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    assert len(successors) == 1
    assert str(successors[0]["status"]) == "ready"  # type: ignore[index]
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    record = (
        ProtectedArtifactStore(artifacts_root).run_root(successor_id) / SDK_SUCCESSOR_RECORD_REL
    )
    record_bytes = record.read_bytes()
    SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    ).abort_run(run_id)
    cancelled = _sdk_successors(store, run_id)[0]
    intent = CursorSdkSuccessorIntentV1.model_validate_json(str(cancelled["intent_payload"]))  # type: ignore[index]
    assert intent.status == "cancelled"
    assert intent.effective_prompt_path is None
    assert intent.effective_prompt_sha256 is None
    assert intent.record_sha256 is None
    assert record.read_bytes() == record_bytes
    status = CliRunner().invoke(app, ["scheduler", "status", run_id, "--output", "json"])
    assert status.exit_code == 0, status.output
    history = CliRunner().invoke(app, ["scheduler", "history", run_id])
    assert history.exit_code == 0, history.output
    retry = CliRunner().invoke(app, ["scheduler", "implementer-retry", run_id, "--force"])
    assert counter.read_text(encoding="utf-8") == "1"
    for _ in range(3):
        tick.run_once()
    assert counter.read_text(encoding="utf-8") == "1"
    assert [
        item
        for item in _sdk_successors(store, run_id)
        if str(item["status"]) in {"pending", "ready"}  # type: ignore[index]
    ] == []
    with store.begin_read() as conn:
        attempt = store.get_nonterminal_attempt_for_run(conn, successor_id)
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert attempt is None
    assert kept is not None and int(kept["consumed_count"]) == 1
    del retry


def test_completed_successor_ancestry_survives_old_source_abort(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
    from ai_dev_loop.scheduler.application.cursor_sdk_successor import (
        authenticate_sdk_successor_authority,
    )

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK old source abort",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    root = ProtectedArtifactStore(artifacts_root).run_root(run_id)
    witness = json.loads((root / str(row["witness_path"])).read_text(encoding="utf-8"))
    durable = tmp_path / "old-source-native.json"
    durable.write_text(
        json.dumps(
            {
                "runs": [
                    {
                        "id": witness["native_run_id"],
                        "status": "error",
                        "agent_id": witness["agent_id"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_CURSOR_SDK_DURABLE_STORE", str(durable))
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    completed = _tick_until_kind(tick, store, successor_id, "completed")
    assert completed.kind == "completed"  # type: ignore[attr-defined]
    artifacts = ProtectedArtifactStore(artifacts_root)
    assert authenticate_sdk_successor_authority(store, artifacts, None, successor_id) is not None
    with store.begin_read() as conn:
        before = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor_id)
        authorization = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert before is not None and authorization is not None
    payload_sha = str(before["intent_payload_sha256"])
    record_sha = str(before["record_sha256"])
    authorization_sha = str(authorization["payload_sha256"])
    service = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=artifacts,
        now_factory=lambda: clock[0],
    )
    with pytest.raises(SchedulerEngineError, match="abort the current run"):
        service.abort_run(run_id)
    refused = CliRunner().invoke(app, ["scheduler", "abort", run_id])
    assert refused.exit_code != 0
    assert authenticate_sdk_successor_authority(store, artifacts, None, successor_id) is not None
    with store.begin_read() as conn:
        after = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor_id)
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        still = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert source.kind == "blocked"
    assert after is not None
    assert str(after["status"]) == "ready"
    assert str(after["intent_payload_sha256"]) == payload_sha
    assert str(after["record_sha256"]) == record_sha
    assert still is not None and str(still["payload_sha256"]) == authorization_sha
    status = CliRunner().invoke(app, ["scheduler", "status", successor_id, "--output", "json"])
    assert status.exit_code == 0, status.output
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=isolated_xdg / "state/ai_dev_loop",
        now=clock[0],
    )
    assert preview.conversations
    assert counter.read_text(encoding="utf-8") == "2"


def test_owned_reobserve_stays_nonterminal_until_aborted(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK owned reobserve",
    )
    backend = tick._attempt_service.backend
    assert isinstance(backend, FakeAgentProcessBackend)
    backend.default_scenario = FakeAttemptScenario(active_ticks=8, exit_code=0)
    clock[0] = _instant(str(row["due_at"]))
    attempt_id = None
    for _ in range(4):
        tick.run_once()
        with store.begin_read() as conn:
            attempt = store.get_nonterminal_attempt_for_run(conn, run_id)
            if attempt is None:
                continue
            attempt_id = str(attempt["attempt_id"])
            dispatch = store.get_effect_by_dispatch_id(conn, str(attempt["dispatch_id"]))
            assert dispatch is not None
            payload = json.loads(str(dispatch["effect_payload"]))
            assert payload["no_progress_reobserve"] is True
            break
    assert attempt_id is not None
    tick.run_once()
    with store.begin_read() as conn:
        again = store.get_nonterminal_attempt_for_run(conn, run_id)
        again_id = None if again is None else str(again["attempt_id"])
    assert again_id == attempt_id
    assert counter.read_text(encoding="utf-8") == "1"
    SchedulerAbortService(
        store,
        backend,
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    ).abort_run(run_id)
    assert any(call_attempt == attempt_id for _, call_attempt in backend.terminate_calls)
    for _ in range(3):
        tick.run_once()
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert kept is not None and str(kept["status"]) == "cancelled"
    assert int(kept["consumed_count"]) == 1


class _BoundaryCrash(BaseException):
    """Escapes the launch helper, which catches Exception and would hide a crash."""


def _restart_tick(store, artifacts_root, git_repo, clock):  # type: ignore[no-untyped-def]
    return TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-restart-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def test_dispatch_crash_restarts_the_same_continuation(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK dispatch crash",
    )
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    successor_id = str(successors[0]["successor_run_id"])  # type: ignore[index]
    with store.begin_read() as conn:
        before = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        reservation = store.get_reservation_for_run(conn, successor_id)
    assert before is not None and reservation is not None
    payload_sha = str(before["payload_sha256"])
    backend = tick._attempt_service.backend
    assert isinstance(backend, FakeAgentProcessBackend)
    backend.default_scenario = FakeAttemptScenario(active_ticks=8, exit_code=0)
    original_observe = backend.observe
    crashed_once = {"done": False}

    def crash_while_dispatching(*, unit_identity, attempt_id):  # type: ignore[no-untyped-def]
        result = original_observe(unit_identity=unit_identity, attempt_id=attempt_id)
        if not crashed_once["done"] and result.lifecycle_state.value == "active":
            crashed_once["done"] = True
            raise _BoundaryCrash("dispatch crash")
        return result

    monkeypatch.setattr(backend, "observe", crash_while_dispatching)
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    with pytest.raises(_BoundaryCrash, match="dispatch crash"):
        for _ in range(8):
            tick.run_once()
    assert crashed_once["done"]
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        crashed = store.get_nonterminal_attempt_for_run(conn, successor_id)
    assert crashed is not None
    crashed_id = str(crashed["attempt_id"])
    monkeypatch.setattr(backend, "observe", original_observe)
    backend.default_scenario = FakeAttemptScenario(active_ticks=0, exit_code=0)
    restarted = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-restart-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )
    challenged = _tick_until_kind(restarted, store, successor_id, "blocked")
    assert "already used" in challenged.block_reason_summary  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "2"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
        turns = conn.execute(
            """
            SELECT scheduler_attempts.attempt_id
            FROM scheduler_attempts
            JOIN scheduler_effects
              ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
            WHERE scheduler_attempts.run_id = ? AND scheduler_effects.effect_kind = 'cursor.run_turn'
            """,
            (successor_id,),
        ).fetchall()
        still = store.get_reservation_for_run(conn, successor_id)
        same = store.get_attempt_by_id(conn, crashed_id)
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert str(kept["payload_sha256"]) == payload_sha
    assert str(kept["successor_run_id"]) == successor_id
    assert rows is not None and int(rows["n"]) == 1
    assert [str(item["attempt_id"]) for item in turns] == [crashed_id]
    assert same is not None and str(same["attempt_id"]) == crashed_id
    assert still is not None
    assert _sdk_successors(store, successor_id) == []


def test_extended_review_ceiling_is_frozen_on_the_continuation(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
    from ai_dev_loop.scheduler.domain.cursor_sdk_recovery import CursorSdkSuccessorIntentV1

    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    clock = [datetime(2027, 10, 1, tzinfo=UTC)]
    run_id, _db, artifacts_root, store, tick = _submit_sdk_run(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK extended ceiling",
        now_factory=lambda: clock[0],
        max_review_iterations=1,
    )
    exhausted = _tick_until_kind(tick, store, run_id, "max_iterations_reached")
    assert exhausted.kind == "max_iterations_reached"  # type: ignore[attr-defined]
    extended = ReviewBudgetExtendService(
        store,
        ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    ).extend(run_id, target_total=2)
    assert extended.new_effective_total == 2
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
    assert row is not None
    clock[0] = _instant(str(row["due_at"]))
    successors = _tick_until_sdk_successor(tick, store, run_id)
    intent = CursorSdkSuccessorIntentV1.model_validate_json(
        str(successors[0]["intent_payload"])  # type: ignore[index]
    )
    assert intent.turn_kind == "correction"
    assert intent.submitted_max_review_iterations == 1
    assert intent.effective_review_ceiling == 2


def test_pending_retention_apply_and_maintenance_keep_the_conversation(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.cursor_sdk_retention import (
        apply_cursor_sdk_cleanup,
        maintain_cursor_sdk_conversations,
    )

    run_id, _artifacts, store, _tick, clock, row, _counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK retention pending",
    )
    del run_id
    assert str(row["status"]) == "reserved"
    state_root = isolated_xdg / "state/ai_dev_loop"
    now = _instant(str(row["due_at"])) + timedelta(hours=169)
    preview = preview_cursor_sdk_cleanup(store, state_root=state_root, now=now)
    item = next(
        candidate
        for candidate in preview.conversations
        if "sdk_no_progress_pending" in candidate.manual_reasons
    )
    applied = apply_cursor_sdk_cleanup(
        store,
        state_root=state_root,
        now=now,
        mode="manual",
        conversation_key=item.conversation_key,
        conversation_owner_run_id=item.conversation_owner_run_id,
    )
    assert applied.deleted is False
    assert "sdk_no_progress_pending" in applied.reasons
    maintained = maintain_cursor_sdk_conversations(store, state_root=state_root, now=now)
    assert item.conversation_key not in maintained.deleted_keys
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(
            conn, source_run_id=str(row["source_run_id"])
        )
    assert kept is not None and str(kept["status"]) == "reserved"
    del clock


def _blocked_sequence(git_repo, isolated_xdg, monkeypatch, tmp_path, *, label: str):  # type: ignore[no-untyped-def]
    import subprocess

    _configure_v2(git_repo)
    (git_repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-qm", f"{label} baseline"], cwd=git_repo, check=True)
    fake_sdk = tmp_path / f"fake-sdk-{label.replace(' ', '-')}"
    fake_sdk.mkdir()
    _install_fake_sdk(fake_sdk)
    _credential(isolated_xdg)
    counter = tmp_path / f"{label.replace(' ', '-')}-sends"
    monkeypatch.setenv("PYTHONPATH", str(fake_sdk))
    monkeypatch.setenv("FAKE_CURSOR_SDK_SEND_COUNTER", str(counter))
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "no_findings")
    state_root = isolated_xdg / "state/ai_dev_loop"
    db = state_root / "engine.sqlite3"
    artifacts_root = state_root / "artifacts"
    prepared = prepare_sequence(
        SequencePrepareOptions(
            manifest_path=_write_no_progress_manifest(git_repo),
            repo_path=git_repo,
            db_path=db,
            artifact_root=artifacts_root,
        )
    )
    started = start_sequence(prepared.sequence_id, db_path=db)
    clock = [datetime(2027, 11, 1, tzinfo=UTC)]
    store = SqliteSchedulerStore(db)
    tick = TickService(
        store,
        ProtectedArtifactStore(artifacts_root),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock[0],
        tick_owner_factory=lambda: f"phase258-{label}-{next(_ATTEMPTS)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPTS):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    _bind_fake_sdk_run(monkeypatch, artifacts_root, started.run_id)
    _tick_until_kind(tick, store, started.run_id, "blocked")
    with store.begin_read() as conn:
        row = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert row is not None
    return started, prepared, artifacts_root, store, tick, clock, row, counter


def test_replay_artifact_fault_restarts_without_another_prompt(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    import subprocess

    started, prepared, artifacts_root, store, tick, clock, row, counter = _blocked_sequence(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        label="replay fault",
    )
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    original = ProtectedArtifactStore.write_bytes_or_verify
    faults: list[str] = []

    def fail_replay_copy(self, run_id, relative_path, content, **kwargs):  # type: ignore[no-untyped-def]
        if run_id != started.run_id and b"aggregate_sha256" in content and not faults:
            faults.append(run_id)
            raise OSError("replay artifact publication fault")
        return original(self, run_id, relative_path, content, **kwargs)

    monkeypatch.setattr(ProtectedArtifactStore, "write_bytes_or_verify", fail_replay_copy)
    clock[0] = _instant(str(row["due_at"]))
    successor_id = None
    for _ in range(20):
        tick.run_once()
        rows = _sdk_successors(store, started.run_id)
        if rows:
            successor_id = str(rows[0]["successor_run_id"])  # type: ignore[index]
            break
    assert faults and successor_id is not None
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
        turn = conn.execute(
            """
            SELECT status FROM scheduler_effects
            WHERE run_id = ? AND effect_kind = 'cursor.run_turn'
              AND status IN ('pending', 'claimed', 'retry_wait')
            """,
            (successor_id,),
        ).fetchone()
        sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
    assert auth is not None and str(auth["status"]) == "publishing"
    assert int(auth["consumed_count"]) == 1
    assert turn is None
    assert sequence.current_run_id == successor_id  # type: ignore[attr-defined]
    monkeypatch.setattr(ProtectedArtifactStore, "write_bytes_or_verify", original)
    restarted = _restart_tick(store, artifacts_root, git_repo, clock)
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    advanced = None
    for _ in range(40):
        restarted.run_once()
        with store.begin_read() as conn:
            advanced = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(advanced, "current_ordinal", 1) > 1:
            break
    assert getattr(advanced, "current_ordinal", None) == 2
    assert advanced.current_run_id != started.run_id  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
        same = [
            item
            for item in store.list_cursor_initial_recoveries_for_source(
                conn, source_run_id=started.run_id
            )
            if str(item["successor_run_id"]) == successor_id
        ]
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert len(same) == 1
    message = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert message == "accepted SDK no-progress checkpoint"


def test_replay_install_fault_restarts_without_another_prompt(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    started, prepared, artifacts_root, store, tick, clock, row, counter = _blocked_sequence(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        label="replay install",
    )
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    original = no_progress._install_replay_turn
    calls = {"n": 0}

    def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return original(*args, **kwargs)

    monkeypatch.setattr(no_progress, "_install_replay_turn", fail_once)
    clock[0] = _instant(str(row["due_at"]))
    successor_id = None
    for _ in range(20):
        tick.run_once()
        rows = _sdk_successors(store, started.run_id)
        if rows and calls["n"]:
            successor_id = str(rows[0]["successor_run_id"])  # type: ignore[index]
            break
    assert successor_id is not None and calls["n"] == 1
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert auth is not None and str(auth["status"]) == "publishing"
    monkeypatch.setattr(no_progress, "_install_replay_turn", original)
    restarted = _restart_tick(store, artifacts_root, git_repo, clock)
    _bind_fake_sdk_run(monkeypatch, artifacts_root, successor_id)
    advanced = None
    for _ in range(40):
        restarted.run_once()
        with store.begin_read() as conn:
            advanced = store.load_validated_sequence_state(conn, prepared.sequence_id)
        if getattr(advanced, "current_ordinal", 1) > 1:
            break
    assert getattr(advanced, "current_ordinal", None) == 2
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert kept is not None and int(kept["consumed_count"]) == 1


def test_aborted_unstarted_successor_becomes_retention_eligible(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService

    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK aborted lineage",
    )
    clock[0] = _instant(str(row["due_at"]))
    rows = _tick_until_sdk_successor(tick, store, run_id)
    successor_id = str(rows[0]["successor_run_id"])  # type: ignore[index]
    SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    ).abort_run(run_id)
    for _ in range(4):
        tick.run_once()
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, run_id)
        child, _, _ = store.load_validated_snapshot(conn, successor_id)
        live = store.get_nonterminal_attempt_for_run(conn, successor_id)
        cancelled = store.get_cursor_initial_recovery_by_successor(
            conn, successor_run_id=successor_id
        )
    assert source.kind == "aborted"
    assert child.kind == "aborted"
    assert live is None
    assert cancelled is not None and str(cancelled["status"]) == "cancelled"
    assert counter.read_text(encoding="utf-8") == "1"
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=isolated_xdg / "state/ai_dev_loop",
        now=clock[0] + timedelta(hours=169),
    )
    assert preview.conversations
    assert all(item.manual_eligible for item in preview.conversations)
    status = CliRunner().invoke(app, ["scheduler", "status", successor_id, "--output", "json"])
    assert status.exit_code == 0, status.output


def test_post_adoption_source_abort_keeps_the_current_leaf(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError

    started, prepared, artifacts_root, store, tick, clock, row, counter = _blocked_sequence(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        label="post adoption abort",
    )
    observed: list[str] = []

    def abort_after_handoff(point: str) -> None:
        if point != "after_force" or observed:
            return
        with store.begin_read() as conn:
            source, _, _ = store.load_validated_snapshot(conn, started.run_id)
            sequence = store.load_validated_sequence_state(
                conn, source.context.sequence.sequence_id
            )  # type: ignore[union-attr]
            auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
        assert sequence.current_run_id != started.run_id  # type: ignore[attr-defined]
        assert auth is not None and str(auth["status"]) == "publishing"
        try:
            SchedulerAbortService(
                store,
                FakeAgentProcessBackend(),
                artifacts=ProtectedArtifactStore(artifacts_root),
                now_factory=lambda: clock[0],
            ).abort_run(started.run_id)
        except SchedulerEngineError:
            observed.append("refused")
        else:
            observed.append("accepted")

    monkeypatch.setattr(no_progress, "publish_fault_hook", abort_after_handoff)
    clock[0] = _instant(str(row["due_at"]))
    for _ in range(12):
        tick.run_once()
        if observed:
            break
    assert observed == ["refused"]
    with store.begin_read() as conn:
        source, _, _ = store.load_validated_snapshot(conn, started.run_id)
        sequence = store.load_validated_sequence_state(conn, prepared.sequence_id)
        recovery = store.list_cursor_initial_recoveries_for_source(
            conn, source_run_id=started.run_id
        )
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert source.kind == "blocked"
    assert sequence.current_run_id != started.run_id  # type: ignore[attr-defined]
    assert sequence.current_ordinal == 1  # type: ignore[attr-defined]
    assert any(str(item["status"]) == "ready" for item in recovery)
    assert auth is not None and int(auth["consumed_count"]) == 1
    assert counter.read_text(encoding="utf-8") == "1"


def test_relation_crash_restarts_onto_the_same_successor(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, artifacts_root, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK relation crash",
    )

    def crash(point: str) -> None:
        if point == "before_force":
            raise _BoundaryCrash("relation crash")

    clock[0] = _instant(str(row["due_at"]))
    monkeypatch.setattr(no_progress, "publish_fault_hook", crash)
    with pytest.raises(_BoundaryCrash, match="relation crash"):
        for _ in range(10):
            tick.run_once()
    with store.begin_read() as conn:
        crashed = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        reservation = store.get_reservation_for_run(conn, run_id)
    assert crashed is not None and str(crashed["status"]) == "publishing"
    assert int(crashed["consumed_count"]) == 1
    assert reservation is not None
    assert _sdk_successors(store, run_id) == []
    assert counter.read_text(encoding="utf-8") == "1"
    monkeypatch.setattr(no_progress, "publish_fault_hook", None)
    restarted = _restart_tick(store, artifacts_root, git_repo, clock)
    successors = _tick_until_sdk_successor(restarted, store, run_id)
    assert len(successors) == 1
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert kept is not None and int(kept["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1


def _reobserve_attempts(store, run_id: str) -> list[object]:
    with store.begin_read() as conn:
        return list(
            conn.execute(
                """
                SELECT scheduler_attempts.*
                FROM scheduler_attempts
                JOIN scheduler_effects
                  ON scheduler_effects.dispatch_id = scheduler_attempts.dispatch_id
                WHERE scheduler_attempts.run_id = ?
                  AND json_extract(scheduler_effects.effect_payload, '$.no_progress_reobserve') = 1
                ORDER BY scheduler_attempts.rowid
                """,
                (run_id,),
            ).fetchall()
        )


def test_lost_reobserve_before_publication_retries_the_same_allowance(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, _artifacts, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK lost reobserve",
    )
    historical_sha = str(row["witness_sha256"])
    with store.begin_read() as conn:
        relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(row["source_attempt_id"])
        )
    assert relation is not None
    result_sha = str(relation["result_sha256"])
    backend = tick._attempt_service.backend
    assert isinstance(backend, FakeAgentProcessBackend)
    backend.default_scenario = FakeAttemptScenario(missing_after_launch=True, active_ticks=0)
    clock[0] = _instant(str(row["due_at"]))
    tick.run_once()
    with store.begin_read() as conn:
        missing = store.get_nonterminal_attempt_for_run(conn, run_id)
    assert missing is not None
    missing_id = str(missing["attempt_id"])
    backend.set_scenario(missing_id, FakeAttemptScenario(missing_after_launch=True, active_ticks=0))
    backend.default_scenario = FakeAttemptScenario(active_ticks=0, exit_code=0)
    successors: list[object] = []
    for _ in range(16):
        tick.run_once()
        successors = _sdk_successors(store, run_id)
        if successors:
            break
    lost = next(
        item for item in _reobserve_attempts(store, run_id) if str(item["attempt_id"]) == missing_id
    )
    assert str(lost["status"]) in {"completed", "failed"}
    assert int(lost["ingested"]) == 1
    assert str(lost["status"]) != "uncertain"
    assert len(successors) == 1
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        kept = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(row["source_attempt_id"])
        )
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert auth is not None and int(auth["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1
    assert kept is not None and str(kept["status"]) == "ready"
    assert str(kept["result_sha256"]) == result_sha
    assert str(auth["witness_sha256"]) == historical_sha
    assert len(_reobserve_attempts(store, run_id)) == 2


def test_lost_reobserve_after_record_publication_does_not_reread(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
) -> None:
    del fake_clis
    run_id, _artifacts, store, tick, clock, row, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK lost published read",
    )
    with store.begin_read() as conn:
        relation = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(row["source_attempt_id"])
        )
    assert relation is not None
    result_sha = str(relation["result_sha256"])
    monkeypatch.setenv("AI_DEV_LOOP_SDK_INTERRUPT_AFTER", "reconciliation_record")
    clock[0] = _instant(str(row["due_at"]))
    successors: list[object] = []
    for _ in range(16):
        tick.run_once()
        successors = _sdk_successors(store, run_id)
        if successors:
            break
    monkeypatch.delenv("AI_DEV_LOOP_SDK_INTERRUPT_AFTER", raising=False)
    assert len(successors) == 1
    assert counter.read_text(encoding="utf-8") == "1"
    attempts = _reobserve_attempts(store, run_id)
    assert len(attempts) == 1
    assert int(attempts[0]["ingested"]) == 1
    assert str(attempts[0]["status"]) != "uncertain"
    with store.begin_read() as conn:
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        kept = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(row["source_attempt_id"])
        )
        rows = conn.execute("SELECT COUNT(*) AS n FROM scheduler_cursor_sdk_no_progress").fetchone()
    assert auth is not None and int(auth["consumed_count"]) == 1
    assert rows is not None and int(rows["n"]) == 1
    assert kept is not None and str(kept["status"]) == "ready"
    assert str(kept["result_sha256"]) == result_sha


@pytest.mark.parametrize("abort_scope", ["run", "sequence"])
def test_abort_during_replay_install_cancels_the_current_leaf(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
    abort_scope: str,
) -> None:
    del fake_clis
    from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
    from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
    from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService

    started, prepared, artifacts_root, store, tick, clock, row, counter = _blocked_sequence(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        label=f"replay install abort {abort_scope}",
    )
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    original = ProtectedArtifactStore.write_bytes_or_verify
    faults: list[str] = []

    def fail_copy(self, run_id, relative_path, content, **kwargs):  # type: ignore[no-untyped-def]
        if run_id != started.run_id and b"aggregate_sha256" in content and not faults:
            faults.append(run_id)
            raise OSError("review replay fault")
        return original(self, run_id, relative_path, content, **kwargs)

    monkeypatch.setattr(ProtectedArtifactStore, "write_bytes_or_verify", fail_copy)
    clock[0] = _instant(str(row["due_at"]))
    for _ in range(20):
        tick.run_once()
        if faults:
            break
    assert faults
    successor_id = faults[0]
    with store.begin_read() as conn:
        recovery = store.get_cursor_initial_recovery_by_successor(
            conn, successor_run_id=successor_id
        )
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
    assert recovery is not None and auth is not None
    assert str(auth["status"]) == "publishing"
    assert auth["successor_run_id"] in {None, ""}
    intent_sha = str(recovery["intent_payload_sha256"])
    record_sha = str(recovery["record_sha256"])
    consumed = int(auth["consumed_count"])
    abort = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    )
    if abort_scope == "run":
        result = abort.abort_run(successor_id)
        assert result.state_kind == "aborted"
    else:
        aborted = SequenceAbortService(
            store, run_abort=abort, now_factory=lambda: clock[0]
        ).abort_sequence(prepared.sequence_id)
        assert aborted.abort_persisted
    monkeypatch.setattr(ProtectedArtifactStore, "write_bytes_or_verify", original)
    restarted = _restart_tick(store, artifacts_root, git_repo, clock)
    for _ in range(12):
        restarted.run_once()
    with store.begin_read() as conn:
        child, _, _ = store.load_validated_snapshot(conn, successor_id)
        auth = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=started.run_id)
        kept = store.get_cursor_initial_recovery_by_successor(conn, successor_run_id=successor_id)
        open_work = conn.execute(
            """
            SELECT effect_kind, effect_payload FROM scheduler_effects
            WHERE run_id IN (?, ?) AND status IN ('pending', 'claimed', 'retry_wait')
            """,
            (started.run_id, successor_id),
        ).fetchall()
    assert child.kind == "aborted"
    assert counter.read_text(encoding="utf-8") == "1"
    assert auth is not None and str(auth["status"]) == "cancelled"
    assert int(auth["consumed_count"]) == consumed == 1
    assert kept is not None and str(kept["status"]) == "ready"
    assert str(kept["intent_payload_sha256"]) == intent_sha
    assert str(kept["record_sha256"]) == record_sha
    assert [
        item
        for item in open_work
        if str(item["effect_kind"]) == "cursor.run_turn"
        or json.loads(str(item["effect_payload"])).get("no_progress_reobserve") is True
    ] == []
    service = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=ProtectedArtifactStore(artifacts_root),
        now_factory=lambda: clock[0],
    )
    with pytest.raises(SchedulerEngineError, match="abort the current run"):
        service.abort_run(started.run_id)
    preview = preview_cursor_sdk_cleanup(
        store,
        state_root=isolated_xdg / "state/ai_dev_loop",
        now=clock[0] + timedelta(hours=169),
    )
    assert preview.conversations
    assert all(item.manual_eligible for item in preview.conversations)
    assert all(
        "sdk_no_progress_pending" not in item.manual_reasons for item in preview.conversations
    )


@pytest.mark.parametrize("retry_mode", ["automatic", "manual_before_due"])
def test_later_logical_turn_reads_its_own_native_run(  # type: ignore[no-untyped-def]
    git_repo,
    isolated_xdg,
    fake_clis,
    monkeypatch,
    tmp_path,
    retry_mode: str,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings")
    run_id, artifacts_root, store, tick, clock, initial, counter = _pending_no_progress(
        git_repo,
        isolated_xdg,
        monkeypatch,
        tmp_path,
        short_name="SDK same run fresh turn",
    )
    monkeypatch.delenv("FAKE_CURSOR_SDK_NO_PROGRESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "finished")
    monkeypatch.setenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", "1")
    clock[0] = _instant(str(initial["due_at"]))
    waiting = _tick_until_kind(tick, store, run_id, "waiting_for_cursor_fix")
    assert waiting.kind == "waiting_for_cursor_fix"  # type: ignore[attr-defined]
    assert counter.read_text(encoding="utf-8") == "1"
    with store.begin_read() as conn:
        historical = store.get_cursor_sdk_no_progress_for_source(conn, source_run_id=run_id)
        first = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(initial["source_attempt_id"])
        )
    assert historical is not None and first is not None
    historical_payload = str(historical["payload_sha256"])
    historical_witness = str(historical["witness_sha256"])
    first_result = str(first["result_sha256"])
    first_key = str(first["recovery_key"])
    monkeypatch.delenv("FAKE_CURSOR_SDK_REPLAY_SUCCESS", raising=False)
    monkeypatch.setenv("FAKE_CURSOR_SDK_NO_PROGRESS", "1")
    monkeypatch.setenv("FAKE_CURSOR_SDK_TERMINAL_STATUS", "error")
    blocked = _tick_until_kind(tick, store, run_id, "blocked")
    assert blocked.block_reason_kind == "cursor_failure"  # type: ignore[attr-defined]
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduler_cursor_sdk_no_progress
            WHERE source_run_id = ? ORDER BY rowid
            """,
            (run_id,),
        ).fetchall()
    assert len(rows) == 2
    fresh = rows[-1]
    assert str(fresh["status"]) == "reserved"
    assert int(fresh["consumed_count"]) == 1
    assert int(rows[0]["consumed_count"]) == 1
    assert str(rows[0]["payload_sha256"]) == historical_payload
    assert str(rows[0]["witness_sha256"]) == historical_witness
    assert counter.read_text(encoding="utf-8") == "2"
    status = CliRunner().invoke(app, ["scheduler", "status", run_id, "--output", "json"])
    assert status.exit_code == 0, status.output
    summary = json.loads(status.stdout)["summary"]
    assert summary["sdk_no_progress_due_at"] == str(fresh["due_at"])
    assert summary["sdk_no_progress_consumed"] == 1
    if retry_mode == "manual_before_due":
        from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
            ImplementerRetryService,
        )

        assert clock[0] < _instant(str(fresh["due_at"]))
        result = ImplementerRetryService(
            store,
            ProtectedArtifactStore(artifacts_root),
            now_factory=lambda: clock[0],
        ).retry(run_id, force=True)
        assert result.changed is False
        assert _sdk_successors(store, run_id) == []
        with store.begin_read() as conn:
            kept = store.get_cursor_sdk_reconciliation_by_source_attempt(
                conn, source_attempt_id=str(initial["source_attempt_id"])
            )
            still = conn.execute(
                """
                SELECT payload_sha256, consumed_count, status
                FROM scheduler_cursor_sdk_no_progress
                WHERE logical_turn_key = ?
                """,
                (str(rows[0]["logical_turn_key"]),),
            ).fetchone()
        assert kept is not None and str(kept["result_sha256"]) == first_result
        assert still is not None
        assert str(still["payload_sha256"]) == historical_payload
        assert int(still["consumed_count"]) == 1
        assert str(still["status"]) == "cancelled"
        return
    clock[0] = _instant(str(fresh["due_at"]))
    successors: list[object] = []
    for _ in range(14):
        tick.run_once()
        successors = _sdk_successors(store, run_id)
        if successors:
            break
    assert len(successors) == 1
    assert counter.read_text(encoding="utf-8") == "2"
    with store.begin_read() as conn:
        kept = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(initial["source_attempt_id"])
        )
        current = store.get_cursor_sdk_reconciliation_by_source_attempt(
            conn, source_attempt_id=str(fresh["source_attempt_id"])
        )
        reads = conn.execute(
            """
            SELECT e.effect_payload FROM scheduler_attempts a
            JOIN scheduler_effects e ON e.dispatch_id = a.dispatch_id
            WHERE a.run_id = ?
              AND json_extract(e.effect_payload, '$.no_progress_reobserve') = 1
            ORDER BY a.rowid
            """,
            (run_id,),
        ).fetchall()
        historical_row = conn.execute(
            """
            SELECT payload_sha256, consumed_count FROM scheduler_cursor_sdk_no_progress
            WHERE logical_turn_key = ?
            """,
            (str(rows[0]["logical_turn_key"]),),
        ).fetchone()
    assert len(reads) == 2
    keys = [json.loads(str(item["effect_payload"]))["recovery_key"] for item in reads]
    assert keys[0] == first_key
    assert current is not None and keys[1] == str(current["recovery_key"])
    assert keys[0] != keys[1]
    assert kept is not None and str(kept["result_sha256"]) == first_result
    assert historical_row is not None
    assert str(historical_row["payload_sha256"]) == historical_payload
    assert int(historical_row["consumed_count"]) == 1
    assert int(fresh["consumed_count"]) == 1
