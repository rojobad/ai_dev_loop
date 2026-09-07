"""Tests for Phase 14 controller state, prepare, launch, and controller status."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import write_session_rollout
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.controller import controller_status
from ai_dev_loop.commands.launch import launch_run
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run, render_prepare_output
from ai_dev_loop.commands.status import render_status
from ai_dev_loop.errors import ValidationError
from ai_dev_loop.launcher import (
    LauncherOutcome,
    LauncherRecord,
    read_launcher_record,
    validate_launcher_record,
    write_launcher_record,
)
from ai_dev_loop.state import ControllerState, load_run_state, serialize_run_state, utc_now

runner = CliRunner()

CONTROLLER_ID = "019abc00-aaaa-0000-0000-0000000000aa"
REVIEWER_ID = "019abc00-bbbb-0000-0000-0000000000bb"


@pytest.fixture
def ab_codex_home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    codex_home = isolated_home / ".codex"
    write_session_rollout(
        codex_home / "sessions",
        session_id=REVIEWER_ID,
        model="gpt-5.3-codex",
        reasoning_effort="high",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return codex_home


REVIEW_MODEL = "gpt-5.6-sol"
REVIEW_REASONING = "high"


def _prepare_ab(git_repo: Path, *, controller: str = CONTROLLER_ID):
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=controller,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
                output="json",
            )
        )


def _prepare_legacy_session_bound_ab(
    git_repo: Path, *, controller: str = CONTROLLER_ID, reviewer: str = REVIEWER_ID
):
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=reviewer,
                controller_session_id=controller,
                output="json",
            )
        )


def test_controller_state_optional_and_schema_aligned() -> None:
    schema = json.loads(
        Path("src/ai_dev_loop/schemas/run-state-v1.json").read_text(encoding="utf-8")
    )
    assert "controller" in schema["properties"]
    assert "controller" not in schema["required"]
    controller_schema = schema["properties"]["controller"]
    assert set(controller_schema["required"]) == {"schema_version", "controller_session_id"}
    assert set(ControllerState.model_fields) == {"schema_version", "controller_session_id"}


def test_historical_run_without_controller_loads(
    git_repo: Path, isolated_xdg, fixture_codex_session
) -> None:
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        result = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )
    state = load_run_state(result.run_directory / "state.json")
    assert state.controller is None
    payload = serialize_run_state(state)
    assert payload.get("controller") is None


def test_prepare_ab_persists_controller_and_launch_eligibility(
    git_repo: Path, isolated_xdg
) -> None:
    result = _prepare_ab(git_repo)
    state = load_run_state(result.run_directory / "state.json")
    assert state.schema_version == 2
    assert state.controller is not None
    assert state.controller.controller_session_id == CONTROLLER_ID
    assert state.codex.session_id is None
    assert state.codex.fresh_reviewer is not None
    assert state.codex.fresh_reviewer.review_model == REVIEW_MODEL
    assert state.codex.fresh_reviewer.review_reasoning_effort == REVIEW_REASONING
    assert result.requires_codex_exit is False
    assert result.reviewer_must_remain_inactive is False
    assert result.launch_command is not None
    assert "--controller-session-id" in result.launch_command

    rendered = json.loads(render_prepare_output(result, output="json"))
    assert rendered["requires_codex_exit"] is False
    assert rendered["reviewer_must_remain_inactive"] is False
    assert rendered["controller_session_id_present"] is True
    assert "controller_session_id" not in rendered


def test_prepare_rejects_codex_session_id_with_controller(git_repo: Path, isolated_xdg) -> None:
    prompt = "Implement the approved plan.\n"
    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="must not pass --codex-session-id"),
    ):
        prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=REVIEWER_ID,
                controller_session_id=CONTROLLER_ID,
                codex_review_model=REVIEW_MODEL,
                codex_review_reasoning_effort=REVIEW_REASONING,
            )
        )


def test_prepare_requires_frozen_review_model_for_controller(git_repo: Path, isolated_xdg) -> None:
    prompt = "Implement the approved plan.\n"
    with (
        patch("sys.stdin", StringIO(prompt)),
        pytest.raises(ValidationError, match="--codex-review-model"),
    ):
        prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_ID,
                codex_review_reasoning_effort=REVIEW_REASONING,
            )
        )


def test_prepare_rejects_malformed_controller_id(
    git_repo: Path, isolated_xdg, ab_codex_home
) -> None:
    with pytest.raises(ValidationError, match="UUID"):
        _prepare_ab(git_repo, controller="not-a-uuid")


def test_status_next_action_for_ab_prepared(git_repo: Path, isolated_xdg) -> None:
    result = _prepare_ab(git_repo)
    text = render_status(result.run_id, output="text")
    assert "launch" in text.lower()
    assert CONTROLLER_ID not in text
    assert REVIEWER_ID not in text


def test_controller_status_exact_match_and_ambiguity(git_repo: Path, isolated_xdg) -> None:
    first = _prepare_ab(git_repo)
    second = _prepare_ab(git_repo)

    zero = controller_status(
        controller_session_id="019abc00-cccc-0000-0000-0000000000cc",
        repo_path=git_repo,
    )
    assert zero.match_count == 0

    multi = controller_status(
        controller_session_id=CONTROLLER_ID,
        repo_path=git_repo,
    )
    assert multi.match_count == 2
    assert multi.run_id is None
    assert first.run_id in multi.candidate_run_ids
    assert second.run_id in multi.candidate_run_ids

    single = controller_status(
        controller_session_id=CONTROLLER_ID,
        repo_path=git_repo,
        run_id=first.run_id,
    )
    assert single.match_count == 1
    assert single.run_id == first.run_id
    assert single.reviewer_session_id is None


def test_controller_status_cli_shortens_ids(git_repo: Path, isolated_xdg) -> None:
    prepared = _prepare_ab(git_repo)
    result = runner.invoke(
        app,
        [
            "controller",
            "status",
            "--controller-session-id",
            CONTROLLER_ID,
            "--repo-path",
            str(git_repo),
            "--run-id",
            prepared.run_id,
        ],
    )
    assert result.exit_code == 0
    assert CONTROLLER_ID not in result.stdout
    assert REVIEWER_ID not in result.stdout
    assert prepared.run_id in result.stdout


def test_launch_requires_controller_and_is_idempotent(
    git_repo: Path, isolated_xdg, monkeypatch
) -> None:
    prepared = _prepare_ab(git_repo)

    with pytest.raises(ValidationError, match="mismatched"):
        launch_run(
            prepared.run_id,
            controller_session_id="019abc00-dddd-0000-0000-0000000000dd",
        )

    import os

    import ai_dev_loop.launcher as launcher_mod

    def fake_spawn(run_directory, *, run_id, policy=None):  # type: ignore[no-untyped-def]
        record = LauncherRecord(
            run_id=run_id,
            worker_token="token-live",
            pid=os.getpid(),
            pgid=os.getpgid(os.getpid()),
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=launcher_mod.read_process_starttime(os.getpid()),
            argv_redacted=[
                "python",
                "-m",
                "ai_dev_loop.launch_worker",
                "<run-id>",
                "<worker-token>",
            ],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        )
        write_launcher_record(run_directory, record)
        return record

    monkeypatch.setattr(launcher_mod, "spawn_detached_worker", fake_spawn)

    first = launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID)
    assert first.already_running is False
    assert first.launcher_pid is not None

    second = launch_run(prepared.run_id, controller_session_id=CONTROLLER_ID)
    assert second.already_running is True
    assert second.launcher_pid == first.launcher_pid


def test_launch_rejects_legacy_run(git_repo: Path, isolated_xdg, fixture_codex_session) -> None:
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        result = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )
    with pytest.raises(ValidationError, match="controller-session-id"):
        launch_run(result.run_id, controller_session_id=CONTROLLER_ID)


def test_launch_rejects_legacy_session_bound_ab(
    git_repo: Path, isolated_xdg, fixture_codex_session
) -> None:
    prompt = "Implement the approved plan.\n"
    with patch("sys.stdin", StringIO(prompt)):
        result = prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )
    state = load_run_state(result.run_directory / "state.json")
    state.controller = ControllerState(controller_session_id=CONTROLLER_ID)
    from ai_dev_loop.state import save_run_state

    save_run_state(result.run_directory, state)
    with pytest.raises(ValidationError, match="retired session-bound"):
        launch_run(result.run_id, controller_session_id=CONTROLLER_ID)


def test_stale_launcher_record_is_not_treated_as_live(git_repo: Path, isolated_xdg) -> None:
    prepared = _prepare_ab(git_repo)
    write_launcher_record(
        prepared.run_directory,
        LauncherRecord(
            run_id=prepared.run_id,
            worker_token="token-dead",
            pid=2**30,
            pgid=2**30,
            parent_pid=1,
            started_at=utc_now(),
            pid_starttime=1,
            argv_redacted=[
                "python",
                "-m",
                "ai_dev_loop.launch_worker",
                "<run-id>",
                "<worker-token>",
            ],
            stdout_path="logs/launcher.stdout.txt",
            stderr_path="logs/launcher.stderr.txt",
            outcome=LauncherOutcome.RUNNING,
        ),
    )
    validation = validate_launcher_record(prepared.run_directory, run_id=prepared.run_id)
    assert validation is not None
    assert validation.is_live is False
    assert validation.is_stale is True
    assert read_launcher_record(prepared.run_directory) is not None


def test_cli_help_includes_launch_and_controller() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "launch" in result.stdout
    assert "controller" in result.stdout
