"""Unit tests for configuration validation and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.config import ConfigOverrides, load_project_config, resolve_effective_config
from ai_dev_loop.errors import ValidationError


def test_load_fixture_config(git_repo: Path) -> None:
    config = load_project_config(git_repo / "ai_dev_loop.yaml")
    assert config.project.name == "fixture-project"
    assert config.cursor.model == "composer-2.5-fast"


def test_config_validation_rejects_bad_slug(tmp_path: Path) -> None:
    bad = tmp_path / "ai_dev_loop.yaml"
    bad.write_text(
        "version: 1\nproject:\n  name: Bad_Name\ncursor:\n  command: agent\n  model: m\n"
        "  output_format: stream-json\n  force: true\n  trust_workspace: true\n  sandbox: disabled\n"
        "codex:\n  command: codex\n  review_model: o4-mini\n  review_skill: review-staged-cursor-execution\n"
        "  sandbox: workspace-write\nworkflow:\n  max_review_iterations: 3\n"
        "  require_clean_worktree: true\n  stage_mode: all\n  cursor_timeout_minutes: 90\n"
        "  codex_timeout_minutes: 90\nprompt:\n  directory: docs/plans\n"
        "  filename_template: prompt_{plan_stem}.txt\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_project_config(bad)


def test_cli_override_precedence(git_repo: Path) -> None:
    effective, source, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(project_name="override-project", cursor_model="override-model"),
    )
    assert source.project.name == "fixture-project"
    assert effective.project.name == "override-project"
    assert effective.cursor.model == "override-model"
