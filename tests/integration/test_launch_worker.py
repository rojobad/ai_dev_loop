"""Integration tests for Phase 14 detached launch with fake agent/codex CLIs."""

from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from tests.conftest import write_session_rollout

from ai_dev_loop.commands.abort import abort_run
from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.commands.launch import launch_run
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.launcher import read_launcher_record
from ai_dev_loop.state import load_run_state

CONTROLLER_ID = "019abc00-aaaa-0000-0000-0000000000aa"
REVIEWER_ID = "019abc00-bbbb-0000-0000-0000000000bb"


def _prepare_ab_run(git_repo: Path, isolated_home: Path, monkeypatch: object) -> object:
    codex_home = isolated_home / ".codex"
    write_session_rollout(
        codex_home / "sessions",
        session_id=REVIEWER_ID,
        model="gpt-5.6-sol",
        reasoning_effort="high",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))  # type: ignore[attr-defined]
    prompt = (git_repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=REVIEWER_ID,
                controller_session_id=CONTROLLER_ID,
            )
        )


def test_launch_worker_uses_reviewer_session_not_controller(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: object,
) -> None:
    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)

    launched = launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID)
    assert launched.already_running is False

    deadline = time.time() + 60
    state = load_run_state(prepared.run_directory / "state.json")
    while time.time() < deadline and state.status.value in {
        "prepared",
        "validating",
        "running_cursor",
        "staging",
        "reviewing",
        "waiting_for_cursor_fix",
    }:
        time.sleep(0.2)
        state = load_run_state(prepared.run_directory / "state.json")
        launcher = read_launcher_record(prepared.run_directory)
        if launcher is not None and launcher.outcome.value != "running":
            break

    assert state.codex.session_id == REVIEWER_ID
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_ID
    launcher = read_launcher_record(prepared.run_directory)
    assert state.status.value != "prepared", launcher.safe_error if launcher else "no launcher"
    assert state.status.value in {
        "completed",
        "completed_with_residual_risk",
        "max_iterations_reached",
        "waiting_for_cursor_fix",
        "failed",
        "interrupted",
        "aborted",
    }

    events = prepared.run_directory / "logs" / "events.jsonl"
    surfaces: list[str] = []
    if events.is_file():
        surfaces.append(events.read_text(encoding="utf-8"))
    for path in sorted((prepared.run_directory / "codex").rglob("*meta*.json")):
        surfaces.append(path.read_text(encoding="utf-8"))
    joined = "\n".join(surfaces)
    if joined:
        assert REVIEWER_ID in joined
        assert f'"session_id": "{CONTROLLER_ID}"' not in joined

    assert launcher is not None
    assert launcher.outcome.value in {"completed", "failed", "aborted"}
    assert CONTROLLER_ID not in json.dumps(launcher.argv_redacted)

    status = controller_status(
        controller_session_id=CONTROLLER_ID,
        repo_path=git_repo,
        run_id=prepared.run_id,
        include_terminal=True,
    )
    assert status.run_id == prepared.run_id


def test_abort_during_detached_launch_records_request(
    git_repo: Path,
    isolated_xdg: Path,
    isolated_home: Path,
    fake_clis: dict[str, Path],
    monkeypatch: object,
) -> None:
    prepared = _prepare_ab_run(git_repo, isolated_home, monkeypatch)

    agent = Path(fake_clis["bin_dir"]) / "agent"
    agent.write_text(
        "#!/usr/bin/env bash\n"
        "sleep 30\n"
        'echo \'{"type":"result","subtype":"success","result":"ok","session_id":"chat-1"}\'\n',
        encoding="utf-8",
    )
    agent.chmod(0o755)

    launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID)
    time.sleep(0.5)
    abort_result = abort_run(prepared.run_id)
    assert abort_result.abort_requested is True

    deadline = time.time() + 30
    state = load_run_state(prepared.run_directory / "state.json")
    while time.time() < deadline and state.status.value not in {"aborted", "failed", "completed"}:
        time.sleep(0.2)
        state = load_run_state(prepared.run_directory / "state.json")

    assert (prepared.run_directory / "locks" / "abort-request.json").is_file()
    assert state.status.value == "aborted" or abort_result.abort_requested
