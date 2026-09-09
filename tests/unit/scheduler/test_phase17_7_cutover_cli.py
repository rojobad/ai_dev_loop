"""CLI tests for scheduler cutover cleanup output modes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ai_dev_loop.cli import app

runner = CliRunner()


@pytest.fixture
def isolated_state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state_root = tmp_path / "xdg-state" / "ai_dev_loop"
    state_root.mkdir(parents=True)
    monkeypatch.setattr("ai_dev_loop.paths.state_dir", lambda: state_root)
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.cutover_cleanup.state_dir",
        lambda: state_root,
    )
    return state_root


def test_cutover_cleanup_json_emits_single_document_on_stdout(
    isolated_state_home: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "scheduler",
            "cutover",
            "cleanup",
            "--confirm",
            "delete-legacy-state",
            "--dry-run",
            "--output",
            "json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is True
    assert "Legacy cutover cleanup will affect only these exact paths" in result.stderr
    assert "runs" in result.stderr


def test_cutover_cleanup_text_announces_paths_on_stdout(
    isolated_state_home: Path,
) -> None:
    result = runner.invoke(
        app,
        [
            "scheduler",
            "cutover",
            "cleanup",
            "--confirm",
            "delete-legacy-state",
            "--dry-run",
            "--output",
            "text",
        ],
    )
    assert result.exit_code == 0
    assert "Legacy cutover cleanup will affect only these exact paths" in result.stdout
    assert "dry-run" in result.stdout
    assert result.stderr == ""
