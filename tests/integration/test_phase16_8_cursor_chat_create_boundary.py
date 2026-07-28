"""Phase 16.8 Gate B cursor chat-create boundary integration tests."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest
from tests.integration.test_phase16_8_local_restart import (
    PLAN_BYTES,
    PROMPT_BYTES,
    RUN_ID,
    THREAD_ID,
    _ctx,
    _git_repo,
)
from typer.testing import CliRunner

from ai_dev_loop.abort_control import ACTIVE_PROCESS_REL_PATH
from ai_dev_loop.cli import app
from ai_dev_loop.commands.abort import run_abort
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.pr_review_v2.domain.common import PullRequestBinding, RepositoryIdentity
from ai_dev_loop.pr_review_v2.domain.effects import RunLocalFixEffect
from ai_dev_loop.pr_review_v2.infrastructure.local_fix_adapter import (
    CARRIER_CHAT_CREATE_FAILURE_SAFE,
    LocalFixAdapter,
    LocalFixAdapterError,
    carrier_run_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import ProtectedResultStore
from ai_dev_loop.pr_review_v2_carrier import FilesystemLocalCarrierRuntime
from ai_dev_loop.run_discovery import load_run
from ai_dev_loop.runners.cursor import CREATE_CHAT_ITERATION, CURSOR_CHAT_CREATE_TIMEOUT_MESSAGE
from ai_dev_loop.state import RunStatus, load_run_state
from ai_dev_loop.workflow_engine import ABORT_RESULT_MESSAGE

runner = CliRunner()


@pytest.fixture(autouse=True)
def _native_tmpdir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", "/tmp")
    monkeypatch.setenv("TMP", "/tmp")
    monkeypatch.setenv("TEMP", "/tmp")


def _wait_for_file(path: Path, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {path}")


def _wait_for_active_iteration(run_path: Path, iteration: int, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        path = run_path / ACTIVE_PROCESS_REL_PATH
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("component") == "cursor" and payload.get("iteration") == iteration:
                return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for active cursor iteration {iteration}")


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_start_create_chat_uses_repo_cwd_timeout_and_artifacts(prepared_run, fake_clis) -> None:
    result = start_run(prepared_run["run_id"])
    assert result.status == "completed"

    run_path = prepared_run["run_path"]
    metadata = json.loads(
        (run_path / "cursor" / "create-chat" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["timed_out"] is False
    assert metadata["exit_code"] == 0
    assert (run_path / "cursor" / "create-chat" / "stdout.txt").is_file()
    assert (run_path / "cursor" / "create-chat" / "stderr.txt").is_file()

    agent_log = fake_clis["agent_log"].read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT") == 1


def test_create_chat_timeout_terminates_child_and_leaves_no_live_metadata(
    prepared_run, fake_clis, monkeypatch, tmp_path: Path
) -> None:
    ready_path = tmp_path / "create_chat_ready.txt"
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_READY", str(ready_path))
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_SLEEP_SECONDS", "30")

    import ai_dev_loop.workflow_engine as workflow_engine
    from ai_dev_loop.runners import cursor as cursor_runner

    original = cursor_runner.create_chat

    def short_timeout_create_chat(cursor_command: str, **kwargs):
        kwargs["timeout_seconds"] = 0.5
        return original(cursor_command, **kwargs)

    monkeypatch.setattr(workflow_engine, "create_chat", short_timeout_create_chat)

    with pytest.raises(AiDevLoopError, match="timed out before a chat ID was received"):
        start_run(prepared_run["run_id"])

    _wait_for_file(ready_path)
    pid_text = ready_path.read_text(encoding="utf-8").strip().splitlines()[0]
    pid = int(pid_text)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and _process_alive(pid):
        time.sleep(0.05)
    assert not _process_alive(pid)

    run_path = prepared_run["run_path"]
    active_path = run_path / ACTIVE_PROCESS_REL_PATH
    if active_path.is_file():
        payload = json.loads(active_path.read_text(encoding="utf-8"))
        assert payload.get("cleared_at") is not None
        assert payload.get("cleared_reason") == "timed_out"
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").is_file()
    assert state.last_error == CURSOR_CHAT_CREATE_TIMEOUT_MESSAGE
    metadata = json.loads(
        (run_path / "cursor" / "create-chat" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["failure_category"] == "timeout"
    assert fake_clis["agent_log"].read_text(encoding="utf-8").count("CREATE_CHAT") == 1


def test_create_chat_nonzero_exit_fails_closed_without_chat_or_iterations(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")

    with pytest.raises(AiDevLoopError, match="inspect protected create-chat artifacts"):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.status == RunStatus.FAILED
    assert state.cursor.chat_id is None
    assert not state.iterations
    assert not (run_path / "cursor" / "chat.json").is_file()
    assert not (run_path / "cursor" / "iterations" / "01").exists()
    metadata = json.loads(
        (run_path / "cursor" / "create-chat" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["failure_category"] == "nonzero_exit"


def test_abort_during_blocking_create_chat_persists_aborted_without_chat(
    prepared_run, fake_clis, monkeypatch, tmp_path: Path
) -> None:
    ready_path = tmp_path / "create_chat_ready.txt"
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_READY", str(ready_path))
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_SLEEP_SECONDS", "30")

    outcome: dict[str, object] = {}

    def run_start() -> None:
        outcome["result"] = start_run(prepared_run["run_id"])

    thread = threading.Thread(target=run_start)
    thread.start()
    try:
        run_path = prepared_run["run_path"]
        _wait_for_active_iteration(run_path, CREATE_CHAT_ITERATION)
        abort_result = run_abort(prepared_run["run_id"])
        assert abort_result.active_process_signaled is True
        thread.join(timeout=15)
        assert not thread.is_alive()
        workflow_result = outcome["result"]
        assert workflow_result.status == "aborted"
        state = load_run_state(run_path / "state.json")
        assert state.status == RunStatus.ABORTED
        assert state.cursor.chat_id is None
        assert not (run_path / "cursor" / "chat.json").is_file()
        assert ABORT_RESULT_MESSAGE in (state.result or "")
        repo_before = subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=prepared_run["repo"]
        )
        assert repo_before.strip() == b""
    finally:
        thread.join(timeout=1)


def test_abort_wins_over_valid_create_chat_output(prepared_run, fake_clis, monkeypatch) -> None:
    import ai_dev_loop.workflow_engine as workflow_engine
    from ai_dev_loop.runners import cursor as cursor_runner

    original_create = cursor_runner.create_chat

    def create_then_request_abort(cursor_command: str, **kwargs):
        chat_id = original_create(cursor_command, **kwargs)
        run_abort(prepared_run["run_id"])
        return chat_id

    monkeypatch.setattr(workflow_engine, "create_chat", create_then_request_abort)

    result = start_run(prepared_run["run_id"])
    assert result.status == "aborted"
    run_path = prepared_run["run_path"]
    state = load_run_state(run_path / "state.json")
    assert state.cursor.chat_id is None
    assert not (run_path / "cursor" / "chat.json").is_file()


def test_failed_create_chat_source_cannot_rerun_create_chat_on_resume(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
    with pytest.raises(AiDevLoopError):
        start_run(prepared_run["run_id"])

    create_before = fake_clis["agent_log"].read_text(encoding="utf-8").count("CREATE_CHAT")
    result = runner.invoke(app, ["resume", prepared_run["run_id"]])
    assert result.exit_code != 0
    create_after = fake_clis["agent_log"].read_text(encoding="utf-8").count("CREATE_CHAT")
    assert create_after == create_before


def test_v2_carrier_create_chat_timeout_maps_to_nonretryable_parent_block(
    tmp_path: Path,
    fake_clis,
    isolated_xdg,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready_path = tmp_path / "create_chat_ready.txt"
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "sleep")
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_READY", str(ready_path))
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_SLEEP_SECONDS", "30")

    import ai_dev_loop.workflow_engine as workflow_engine
    from ai_dev_loop.runners import cursor as cursor_runner

    original = cursor_runner.create_chat

    def short_timeout_create_chat(cursor_command: str, **kwargs):
        kwargs["timeout_seconds"] = 0.5
        return original(cursor_command, **kwargs)

    monkeypatch.setattr(workflow_engine, "create_chat", short_timeout_create_chat)

    repo, head = _git_repo(tmp_path)
    store = ProtectedResultStore(tmp_path / "artifacts")
    base_ctx = _ctx(repo_root=str(repo.resolve()), head=head)
    ctx = base_ctx.model_copy(
        update={"cursor": base_ctx.cursor.model_copy(update={"chat_id": None})}
    )
    ctx_ref = store.persist_execution_context(run_id=RUN_ID, artifact=ctx)
    store.persist_source_plan_bytes(run_id=RUN_ID, data=PLAN_BYTES)
    store.persist_source_prompt_bytes(run_id=RUN_ID, data=PROMPT_BYTES)
    fix_prompt = b"Codex adjudicated fix\n"
    fix_ref = store.persist_fix_prompt(run_id=RUN_ID, text=fix_prompt.decode())
    effect = RunLocalFixEffect(
        effect_id="pr-review:run-p168-local:cycle:01:run_local_fix",
        idempotency_key="pr-review:run-p168-local:cycle:01:run_local_fix",
        run_id=RUN_ID,
        cycle_number=1,
        attempt=1,
        max_attempts=6,
        repository=RepositoryIdentity(name_with_owner="acme/demo"),
        bound_head_sha=head,
        binding=PullRequestBinding(
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            pr_number=1,
            head_branch="main",
            base_branch="main",
            head_sha=head,
        ),
        actionable_thread_ids=(THREAD_ID,),
        fix_prompt_ref=fix_ref,
        execution_context_ref=ctx_ref,
    )
    adapter = LocalFixAdapter(runtime=FilesystemLocalCarrierRuntime(), store=store)
    with pytest.raises(LocalFixAdapterError, match=CARRIER_CHAT_CREATE_FAILURE_SAFE):
        adapter.execute(
            run_id=RUN_ID,
            effect=effect,
            execution_context=ctx,
            fix_prompt_bytes=fix_prompt,
            plan_bytes=PLAN_BYTES,
            prompt_bytes=PROMPT_BYTES,
        )

    carrier_id = carrier_run_id(RUN_ID, 1, effect.effect_id)
    _, carrier_state = load_run(carrier_id)
    assert carrier_state.status == RunStatus.FAILED
    assert carrier_state.cursor.chat_id is None
    assert store.read_cached_local_fix_result(effect) is None
    assert fake_clis["agent_log"].read_text(encoding="utf-8").count("CREATE_CHAT") == 1


def test_create_chat_artifacts_use_sensitive_permissions(
    prepared_run, fake_clis, monkeypatch
) -> None:
    monkeypatch.setenv("FAKE_AGENT_CREATE_CHAT_MODE", "fail")
    with pytest.raises(AiDevLoopError):
        start_run(prepared_run["run_id"])

    run_path = prepared_run["run_path"]
    for name in ("stdout.txt", "stderr.txt", "metadata.json"):
        artifact = run_path / "cursor" / "create-chat" / name
        assert artifact.is_file()
        assert stat.S_IMODE(artifact.stat().st_mode) == SENSITIVE_FILE_MODE
