"""Phase 10 integration tests for session runtime capture and CLI updates."""

from __future__ import annotations

import ast
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.abort_control import write_abort_request
from ai_dev_loop.cli import app
from ai_dev_loop.commands.prepare import PrepareOptions, PrepareResult, prepare_run
from ai_dev_loop.commands.start import start_run
from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.review_runtime import is_legacy_phase9_codex_state
from ai_dev_loop.runners.codex import build_codex_review_args
from ai_dev_loop.runners.git import discover_repository
from ai_dev_loop.runners.tool_updates import (
    ToolCompatibilityPolicy,
    UpdateMode,
)
from ai_dev_loop.state import RunStatus, load_run_state
from conftest import (
    DEFAULT_FIXTURE_SESSION_ID,
    SENSITIVE_SENTINEL,
    write_session_rollout,
)

runner = CliRunner()


@pytest.fixture
def codex_session(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = isolated_home / ".codex"
    write_session_rollout(home / "sessions")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def _prepare(
    git_repo: Path,
    *,
    codex_review_model: str | None = None,
    codex_review_reasoning_effort: str | None = None,
) -> PrepareResult:
    prompt = (git_repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    with patch("sys.stdin", StringIO(prompt)):
        return prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id=DEFAULT_FIXTURE_SESSION_ID,
                codex_review_model=codex_review_model,
                codex_review_reasoning_effort=codex_review_reasoning_effort,
            )
        )


def _logged_review_args(codex_log: Path) -> list[str]:
    args_line = next(
        line
        for line in codex_log.read_text(encoding="utf-8").splitlines()
        if line.startswith("ARGS:")
    )
    value = ast.literal_eval(args_line.removeprefix("ARGS:"))
    assert isinstance(value, list)
    return [str(item) for item in value]


def test_prepare_captures_session_runtime_and_freezes_session_provenance(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    codex_session: Path,
) -> None:
    prepared = _prepare(git_repo)
    state = load_run_state(prepared.run_directory / "state.json")

    assert prepared.session_model == "gpt-5.6-sol"
    assert prepared.session_reasoning_effort == "high"
    assert prepared.review_model == "gpt-5.6-sol"
    assert prepared.review_reasoning_effort == "high"
    assert prepared.review_model_source == "session"
    assert prepared.review_reasoning_source == "session"
    assert state.codex.session_model == "gpt-5.6-sol"
    assert state.codex.session_reasoning_effort == "high"
    assert state.codex.review_model == "gpt-5.6-sol"
    assert state.codex.review_reasoning_effort == "high"
    assert state.codex.review_model_source == "session"
    assert state.codex.review_reasoning_source == "session"

    artifact = json.loads(
        (prepared.run_directory / "codex/session-runtime.json").read_text(encoding="utf-8")
    )
    assert artifact["model"] == "gpt-5.6-sol"
    assert artifact["reasoning_effort"] == "high"
    assert artifact["review_model_source"] == "session"
    assert artifact["review_reasoning_source"] == "session"
    assert "rollout" not in artifact


def test_prepare_captures_desktop_max_effort_from_session(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    isolated_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = isolated_home / ".codex"
    write_session_rollout(
        home / "sessions",
        model="gpt-5.6-sol",
        reasoning_effort="max",
        desktop_production_shape=True,
    )
    monkeypatch.setenv("CODEX_HOME", str(home))

    prepared = _prepare(git_repo)
    state = load_run_state(prepared.run_directory / "state.json")

    assert prepared.session_reasoning_effort == "max"
    assert prepared.review_reasoning_effort == "max"
    assert prepared.review_reasoning_source == "session"
    assert state.codex.session_reasoning_effort == "max"
    assert state.codex.review_reasoning_effort == "max"


def test_start_review_argv_explicitly_contains_session_model_and_reasoning(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    start_run(str(prepared_run["run_id"]))

    args = _logged_review_args(fake_clis["codex_log"])
    resume_index = args.index("resume")
    assert args[resume_index + 1 : resume_index + 5] == [
        "--model",
        "gpt-5.6-sol",
        "-c",
        'model_reasoning_effort="high"',
    ]
    assert args[-2] == DEFAULT_FIXTURE_SESSION_ID
    assert "--last" not in args


@pytest.mark.parametrize(
    (
        "explicit_model",
        "explicit_reasoning",
        "expected_model",
        "expected_reasoning",
        "expected_model_source",
        "expected_reasoning_source",
    ),
    [
        ("gpt-5.5", None, "gpt-5.5", "high", "explicit", "session"),
        (None, "xhigh", "gpt-5.6-sol", "xhigh", "session", "explicit"),
        (None, "ultra", "gpt-5.6-sol", "ultra", "session", "explicit"),
    ],
)
def test_explicit_model_and_reasoning_overrides_apply_independently(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    codex_session: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit_model: str | None,
    explicit_reasoning: str | None,
    expected_model: str,
    expected_reasoning: str,
    expected_model_source: str,
    expected_reasoning_source: str,
) -> None:
    prepared = _prepare(
        git_repo,
        codex_review_model=explicit_model,
        codex_review_reasoning_effort=explicit_reasoning,
    )
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    start_run(prepared.run_id)

    state = load_run_state(prepared.run_directory / "state.json")
    assert state.codex.review_model == expected_model
    assert state.codex.review_reasoning_effort == expected_reasoning
    assert state.codex.review_model_source == expected_model_source
    assert state.codex.review_reasoning_source == expected_reasoning_source
    args = _logged_review_args(fake_clis["codex_log"])
    assert args[args.index("--model") + 1] == expected_model
    assert f'model_reasoning_effort="{expected_reasoning}"' in args


def test_later_rollout_and_yaml_changes_do_not_change_prepared_runtime(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    codex_session: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = _prepare(git_repo)
    rollout = next((codex_session / "sessions").rglob("rollout-*.jsonl"))
    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-07-10T13:00:00.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "turn_context",
                        "model": "gpt-5.5",
                        "reasoning_effort": "low",
                    },
                }
            )
            + "\n"
        )
    config_path = git_repo / "ai_dev_loop.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "codex:\n  command: codex\n",
            "codex:\n  command: codex\n  review_model: gpt-5.5\n  review_reasoning_effort: low\n",
        ),
        encoding="utf-8",
    )
    # Keep the recorded baseline aligned so the test isolates the prepared runtime freeze.
    status = discover_repository(git_repo).status_porcelain
    (prepared.run_directory / "git/baseline-status.txt").write_text(
        status + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    start_run(prepared.run_id)

    state = load_run_state(prepared.run_directory / "state.json")
    assert state.codex.review_model == "gpt-5.6-sol"
    assert state.codex.review_reasoning_effort == "high"
    args = _logged_review_args(fake_clis["codex_log"])
    assert "gpt-5.6-sol" in args
    assert 'model_reasoning_effort="high"' in args
    assert 'model_reasoning_effort="low"' not in args


def test_compatible_tools_proceed_without_running_updates(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    result = start_run(str(prepared_run["run_id"]))

    assert result.status == "completed"
    assert "UPDATE" not in fake_clis["agent_log"].read_text(encoding="utf-8")
    assert "UPDATE" not in fake_clis["codex_log"].read_text(encoding="utf-8")


def test_incompatible_codex_always_updates_reprobes_and_proceeds(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.5"}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "FAKE_CODEX_UPDATE_MODELS",
        json.dumps({"models": [{"id": "gpt-5.5"}, {"id": "gpt-5.6-sol"}]}),
    )
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")

    result = start_run(
        str(prepared_run["run_id"]),
        tool_policy=ToolCompatibilityPolicy(update_mode=UpdateMode.ALWAYS),
    )

    assert result.status == "completed"
    assert "UPDATE" in fake_clis["codex_log"].read_text(encoding="utf-8")
    assert "UPDATE" not in fake_clis["agent_log"].read_text(encoding="utf-8")
    update_artifact = json.loads(
        (Path(str(prepared_run["run_path"])) / "preflight/codex-update.json").read_text(
            encoding="utf-8"
        )
    )
    assert update_artifact["argv"] == ["codex", "update"]


def test_declined_update_fails_actionably_and_keeps_run_prepared(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.5"}]}),
        encoding="utf-8",
    )

    with pytest.raises(AiDevLoopError) as exc_info:
        start_run(
            str(prepared_run["run_id"]),
            tool_policy=ToolCompatibilityPolicy(update_mode=UpdateMode.NEVER),
        )

    message = str(exc_info.value)
    assert "tool compatibility failed" in message
    assert "codex update" in message
    assert "--update-tools" in message
    assert load_run_state(Path(str(prepared_run["run_path"])) / "state.json").status == (
        RunStatus.PREPARED
    )
    assert not fake_clis["codex_log"].exists()


def test_unknown_catalog_shape_is_not_reported_as_passed(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
) -> None:
    # Valid JSON with an unrecognized catalog shape → UNKNOWN, not silent pass.
    fake_clis["codex_models_file"].write_text(
        '{"schema_version": 1, "object": "list"}',
        encoding="utf-8",
    )

    with pytest.raises(AiDevLoopError, match="tool compatibility failed"):
        start_run(
            str(prepared_run["run_id"]),
            tool_policy=ToolCompatibilityPolicy(update_mode=UpdateMode.NEVER),
        )

    events = (Path(str(prepared_run["run_path"])) / "logs/events.jsonl").read_text(encoding="utf-8")
    log = (Path(str(prepared_run["run_path"])) / "logs/ai_dev_loop.log").read_text(encoding="utf-8")
    assert "tool_compatibility_passed" not in events
    assert "unknown" in log
    assert load_run_state(Path(str(prepared_run["run_path"])) / "state.json").status == (
        RunStatus.PREPARED
    )


def test_probe_failed_fails_without_claiming_passed(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_MODELS_FAIL", "1")

    with pytest.raises(AiDevLoopError) as exc_info:
        start_run(
            str(prepared_run["run_id"]),
            tool_policy=ToolCompatibilityPolicy(update_mode=UpdateMode.NEVER),
        )

    message = str(exc_info.value)
    events = (Path(str(prepared_run["run_path"])) / "logs/events.jsonl").read_text(encoding="utf-8")
    assert "tool compatibility failed" in message
    assert "probe_failed" in message
    assert "tool_compatibility_passed" not in events
    assert load_run_state(Path(str(prepared_run["run_path"])) / "state.json").status == (
        RunStatus.PREPARED
    )


def test_update_tools_policy_needs_no_ask_callback(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_clis["agent_models_file"].write_text("different-model\n", encoding="utf-8")
    monkeypatch.setenv("FAKE_AGENT_UPDATE_MODELS", "composer-2.5-fast\n")
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    policy = ToolCompatibilityPolicy(
        update_mode=UpdateMode.ALWAYS,
        ask_callback=None,
    )

    result = start_run(str(prepared_run["run_id"]), tool_policy=policy)

    assert result.status == "completed"
    assert "UPDATE" in fake_clis["agent_log"].read_text(encoding="utf-8")


def test_abort_before_incompatible_tool_update_prevents_updater_launch(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
) -> None:
    fake_clis["codex_models_file"].write_text(
        json.dumps({"models": [{"id": "gpt-5.5"}]}),
        encoding="utf-8",
    )
    run_path = Path(str(prepared_run["run_path"]))
    write_abort_request(run_path, run_id=str(prepared_run["run_id"]))

    result = start_run(
        str(prepared_run["run_id"]),
        tool_policy=ToolCompatibilityPolicy(update_mode=UpdateMode.ALWAYS),
    )

    assert result.status == "aborted"
    assert load_run_state(run_path / "state.json").status == RunStatus.ABORTED
    assert not fake_clis["codex_log"].exists()
    assert not fake_clis["agent_log"].exists()
    assert not (run_path / "preflight/codex-update.json").exists()


def test_historical_phase9_state_remains_readable_and_omits_runtime_args(
    prepared_run: dict[str, object],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_path = Path(str(prepared_run["run_path"]))
    state_path = run_path / "state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["codex"]["review_model"] = None
    payload["codex"]["review_reasoning_effort"] = None
    for key in (
        "session_model",
        "session_reasoning_effort",
        "review_model_source",
        "review_reasoning_source",
        "model_family_warning",
    ):
        payload["codex"].pop(key, None)
    state_path.write_text(json.dumps(payload), encoding="utf-8")

    state = load_run_state(state_path)
    assert is_legacy_phase9_codex_state(
        review_model=state.codex.review_model,
        review_reasoning_effort=state.codex.review_reasoning_effort,
        review_model_source=state.codex.review_model_source,
        review_reasoning_source=state.codex.review_reasoning_source,
    )
    direct_args = build_codex_review_args(
        state.codex,
        repo_root=str(prepared_run["repo"]),
        session_id=state.codex.session_id,
        schema_file=tmp_path / "schema.json",
        result_file=tmp_path / "result.json",
    )
    assert "--model" not in direct_args
    assert "-c" not in direct_args

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    from ai_dev_loop.runners.tool_updates import ToolCompatibilityPolicy, UpdateMode

    result = start_run(
        str(prepared_run["run_id"]),
        tool_policy=ToolCompatibilityPolicy(
            update_mode=UpdateMode.NEVER,
            allow_incompatible=True,
        ),
    )

    assert result.status == "completed"
    logged_args = _logged_review_args(fake_clis["codex_log"])
    assert "--model" not in logged_args
    assert "-c" not in logged_args
    assert "legacy Phase 9" in (run_path / "logs/ai_dev_loop.log").read_text(encoding="utf-8")


def test_privacy_surfaces_never_include_transcript_or_full_session_id(
    git_repo: Path,
    isolated_xdg: Path,
    fake_clis: dict[str, Path],
    codex_session: Path,
) -> None:
    prepared = _prepare(git_repo)

    status = runner.invoke(app, ["status", prepared.run_id])
    logs = runner.invoke(app, ["logs", prepared.run_id])
    assert status.exit_code == 0
    assert logs.exit_code == 0

    surfaces = [
        status.stdout,
        status.stderr,
        logs.stdout,
        logs.stderr,
        (prepared.run_directory / "logs/ai_dev_loop.log").read_text(encoding="utf-8"),
        (prepared.run_directory / "logs/events.jsonl").read_text(encoding="utf-8"),
        (prepared.run_directory / "codex/session-runtime.json").read_text(encoding="utf-8"),
    ]
    for surface in surfaces:
        assert SENSITIVE_SENTINEL not in surface
        assert DEFAULT_FIXTURE_SESSION_ID not in surface

    runtime_artifact = json.loads(surfaces[-1])
    assert runtime_artifact["session_id_prefix"] == DEFAULT_FIXTURE_SESSION_ID[:8]
    assert set(runtime_artifact).isdisjoint({"transcript", "messages", "rollout_path"})
