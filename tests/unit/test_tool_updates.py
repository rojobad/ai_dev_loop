"""Unit tests for explicit tool-update policy and official updater execution."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.abort_control import write_abort_request
from ai_dev_loop.errors import UsageError, ValidationError
from ai_dev_loop.runners.tool_updates import (
    ToolUpdateFlags,
    UpdateMode,
    policy_from_flags,
    run_official_updater,
    validate_tool_update_flags,
)


def test_contradictory_update_flags_raise_usage_error() -> None:
    flags = ToolUpdateFlags(update_tools=True, skip_tool_update=True)

    with pytest.raises(UsageError, match="contradictory flags"):
        validate_tool_update_flags(flags)
    with pytest.raises(UsageError, match="--update-tools.*--skip-tool-update"):
        policy_from_flags(flags, stdin_is_tty=True)


@pytest.mark.parametrize(
    ("flags", "stdin_is_tty", "expected_mode"),
    [
        (ToolUpdateFlags(), True, UpdateMode.ASK),
        (ToolUpdateFlags(), False, UpdateMode.NEVER),
        (ToolUpdateFlags(skip_tool_update=True), True, UpdateMode.NEVER),
        (ToolUpdateFlags(update_tools=True), False, UpdateMode.ALWAYS),
    ],
)
def test_policy_mode_follows_flags_and_tty(
    flags: ToolUpdateFlags,
    stdin_is_tty: bool,
    expected_mode: UpdateMode,
) -> None:
    def callback(prompt: object) -> bool:
        del prompt
        return True

    policy = policy_from_flags(
        flags,
        stdin_is_tty=stdin_is_tty,
        ask_callback=callback,
    )

    assert policy.update_mode == expected_mode
    assert (policy.ask_callback is callback) is (expected_mode == UpdateMode.ASK)


def test_policy_preserves_allow_incompatible_flag() -> None:
    policy = policy_from_flags(
        ToolUpdateFlags(skip_tool_update=True, allow_incompatible_tools=True),
        stdin_is_tty=False,
    )

    assert policy.update_mode == UpdateMode.NEVER
    assert policy.allow_incompatible is True
    assert policy.ask_callback is None


@pytest.mark.parametrize(
    ("tool", "command", "log_key"),
    [("cursor", "agent", "agent_log"), ("codex", "codex", "codex_log")],
)
def test_official_updater_uses_configured_command_plus_update_argv(
    tmp_path: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    tool: str,
    command: str,
    log_key: str,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()

    result = run_official_updater(
        tool=tool,  # type: ignore[arg-type]
        command=command,
        version_before="before",
        run_directory=run_directory,
        run_id="fixture-run",
    )

    assert result.argv == [command, "update"]
    assert result.returncode == 0
    assert result.timed_out is False
    assert "UPDATE" in fake_clis[log_key].read_text(encoding="utf-8")
    artifact = json.loads((run_directory / result.artifact_path).read_text(encoding="utf-8"))
    assert artifact["argv"] == [command, "update"]
    assert artifact["command"] == command
    assert artifact["stdout_path"] == f"preflight/{tool}-update.stdout.txt"
    assert (run_directory / artifact["stdout_path"]).is_file()


def test_official_updater_registers_active_process_metadata_during_run(
    tmp_path: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.abort_control import ACTIVE_PROCESS_REL_PATH
    from ai_dev_loop.process import StreamingProcessResult

    run_directory = tmp_path / "run"
    run_directory.mkdir()
    seen: dict[str, object] = {}

    def fake_streaming(args, **kwargs):
        active = kwargs.get("active_process")
        assert active is not None
        seen["component"] = active.component
        seen["argv"] = list(args)
        # Simulate registration file existence while the child would be running.
        assert kwargs.get("timeout") == 300.0
        return StreamingProcessResult(
            args=list(args),
            returncode=0,
            stdout="updated\n",
            stderr="",
            timed_out=False,
            elapsed_seconds=0.01,
        )

    monkeypatch.setattr(
        "ai_dev_loop.runners.tool_updates.run_process_streaming",
        fake_streaming,
    )

    result = run_official_updater(
        tool="codex",
        command="codex",
        version_before="before",
        run_directory=run_directory,
        run_id="fixture-run",
    )

    assert result.argv == ["codex", "update"]
    assert seen["component"] == "codex_update"
    assert seen["argv"] == ["codex", "update"]
    # After completion the streaming helper clears metadata; ensure we used that path.
    assert not (run_directory / ACTIVE_PROCESS_REL_PATH).exists()


def test_official_updater_refuses_to_launch_after_abort(
    tmp_path: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
) -> None:
    run_directory = tmp_path / "run"
    (run_directory / "locks").mkdir(parents=True)
    write_abort_request(run_directory, run_id="fixture-run")

    with pytest.raises(ValidationError, match="abort requested"):
        run_official_updater(
            tool="codex",
            command="codex",
            version_before="codex-cli 0.1",
            run_directory=run_directory,
            run_id="fixture-run",
        )

    assert not fake_clis["codex_log"].exists()


def test_official_updater_failure_records_artifact_without_raw_command_execution(
    tmp_path: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_directory = tmp_path / "run"
    run_directory.mkdir()
    monkeypatch.setenv("FAKE_CODEX_UPDATE_FAIL", "1")

    with pytest.raises(ValidationError, match="exit code 2"):
        run_official_updater(
            tool="codex",
            command="codex",
            version_before="codex-cli 0.1",
            run_directory=run_directory,
            run_id="fixture-run",
        )

    artifact = json.loads(
        (run_directory / "preflight" / "codex-update.json").read_text(encoding="utf-8")
    )
    assert artifact["argv"] == ["codex", "update"]
    assert artifact["returncode"] == 2
    assert "UPDATE" in fake_clis["codex_log"].read_text(encoding="utf-8")
