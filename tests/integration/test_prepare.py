"""Integration tests for prepare and CLI."""

from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.commands.prepare import PrepareOptions, prepare_run
from ai_dev_loop.paths import run_dir
from ai_dev_loop.state import load_manifest, load_run_state

runner = CliRunner()


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "prepare",
        "start",
        "resume",
        "status",
        "list",
        "logs",
        "inspect",
        "abort",
        "doctor",
        "integrations",
        "config",
    ):
        assert command in result.stdout


def test_abort_marks_non_terminal_run(prepared_run) -> None:
    result = runner.invoke(app, ["abort", prepared_run["run_id"]])
    assert result.exit_code == 0
    assert "aborted" in result.stdout.lower()


def test_config_validate(git_repo: Path, isolated_xdg) -> None:
    result = runner.invoke(app, ["config", "validate", "--repo", str(git_repo), "--output", "json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "valid"
    assert payload["project"] == "fixture-project"
    assert payload["codex_review_model"] is None
    assert payload["codex_review_reasoning_effort"] is None

    text = runner.invoke(app, ["config", "validate", "--repo", str(git_repo)])
    assert text.exit_code == 0
    assert "inherited from session" in text.stdout


def test_prepare_rejects_empty_stdin(git_repo: Path, isolated_xdg, fixture_codex_session) -> None:
    with patch("sys.stdin", StringIO("")), pytest.raises(Exception, match="empty"):
        prepare_run(
            PrepareOptions(
                repo_path=git_repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                codex_session_id="019abc00-0000-0000-0000-000000000000",
            )
        )


def test_prepare_json_creates_artifacts(
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
                output="json",
            )
        )

    run_path = run_dir("fixture-project", result.run_id)
    assert run_path.is_dir()
    assert not str(run_path).startswith(str(git_repo))

    state = load_run_state(run_path / "state.json")
    manifest = load_manifest(run_path / "manifest.json")
    assert state.status.value == "prepared"
    assert state.plan.sha256
    assert state.prompt.sha256
    assert state.codex.session_id == "019abc00-0000-0000-0000-000000000000"
    assert state.codex.session_model == "gpt-5.6-sol"
    assert state.codex.session_reasoning_effort == "high"
    assert state.codex.review_model == "gpt-5.6-sol"
    assert state.codex.review_reasoning_effort == "high"
    assert state.codex.review_model_source == "session"
    assert state.codex.review_reasoning_source == "session"
    assert (run_path / "prompts" / "cursor-initial.txt").read_text(encoding="utf-8") == prompt
    assert len(manifest.artifacts) >= 4
    source_entry = next(a for a in manifest.artifacts if a.path == "source-config.yaml")
    assert (
        source_entry.sha256
        == hashlib.sha256((run_path / "source-config.yaml").read_bytes()).hexdigest()
    )

    cli_result = runner.invoke(
        app,
        [
            "status",
            result.run_id,
            "--output",
            "json",
        ],
    )
    assert cli_result.exit_code == 0
    status_payload = json.loads(cli_result.stdout)
    assert status_payload["run_id"] == result.run_id
    assert status_payload["codex_review_model"] == "gpt-5.6-sol"
    assert status_payload["codex_review_reasoning_effort"] == "high"
    assert status_payload["codex_review_model_source"] == "session"
    assert status_payload["codex_session_model"] == "gpt-5.6-sol"

    status_text = runner.invoke(app, ["status", result.run_id])
    assert status_text.exit_code == 0
    assert "gpt-5.6-sol" in status_text.stdout
    assert "(session)" in status_text.stdout

    inspect_text = runner.invoke(app, ["inspect", result.run_id])
    assert inspect_text.exit_code == 0
    assert "gpt-5.6-sol" in inspect_text.stdout
    assert "(session)" in inspect_text.stdout


def test_logs_rejects_invalid_component(
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
    cli_result = runner.invoke(app, ["logs", result.run_id, "--component", "invalid"])
    assert cli_result.exit_code == 4
    assert (
        "component must be one of" in cli_result.stderr
        or "component must be one of" in cli_result.stdout
    )


def test_schemas_are_valid_json() -> None:
    schema_root = Path(__file__).resolve().parents[2] / "src" / "ai_dev_loop" / "schemas"
    for name in (
        "project-config-v1.json",
        "run-state-v1.json",
        "codex-review-result-v1.json",
    ):
        payload = json.loads((schema_root / name).read_text(encoding="utf-8"))
        assert "$schema" in payload


def test_prepare_persists_explicit_codex_overrides(
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
                codex_review_model="gpt-5.5",
                codex_review_reasoning_effort="high",
            )
        )
    state = load_run_state(run_dir("fixture-project", result.run_id) / "state.json")
    assert state.codex.review_model == "gpt-5.5"
    assert state.codex.review_reasoning_effort == "high"
    assert state.codex.review_model_source == "explicit"
    assert state.codex.review_reasoning_source == "explicit"
    assert state.codex.session_model == "gpt-5.6-sol"


def test_prepare_help_lists_reasoning_override() -> None:
    result = runner.invoke(
        app,
        ["prepare", "--help"],
        env={"COLUMNS": "200", "LINES": "80", "TERM": "dumb"},
    )
    assert result.exit_code == 0
    combined = result.stdout + result.stderr
    assert "--codex-review-model" in combined
    assert "--codex-review-reasoning-effort" in combined
