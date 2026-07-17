"""Unit tests for optional GitHub configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.config import load_project_config
from ai_dev_loop.errors import ValidationError

_BASE = (
    "version: 1\nproject:\n  name: fixture-project\n"
    "cursor:\n  command: agent\n  model: m\n  output_format: stream-json\n"
    "  force: true\n  trust_workspace: true\n  sandbox: disabled\n"
    "codex:\n  command: codex\n  review_skill: review-staged-cursor-execution\n"
    "  sandbox: workspace-write\n"
    "workflow:\n  max_review_iterations: 3\n  require_clean_worktree: true\n"
    "  stage_mode: all\n  cursor_timeout_minutes: 90\n  codex_timeout_minutes: 90\n"
    "prompt:\n  directory: docs/plans\n  filename_template: prompt_{plan_stem}.txt\n"
)


def test_github_section_absent_is_disabled(tmp_path: Path) -> None:
    path = tmp_path / "ai_dev_loop.yaml"
    path.write_text(_BASE, encoding="utf-8")
    config = load_project_config(path)
    assert config.github is None
    assert config.github_enabled() is False


def test_github_enabled_defaults(tmp_path: Path) -> None:
    path = tmp_path / "ai_dev_loop.yaml"
    path.write_text(_BASE + "github:\n  enabled: true\n", encoding="utf-8")
    config = load_project_config(path)
    assert config.github_enabled()
    assert config.github is not None
    assert config.github.command == "gh"
    assert config.github.reviewer_logins == ["chatgpt-codex-connector"]
    assert config.github.poll_interval_seconds == 60
    assert config.github.poll_timeout_hours == 24
    assert config.github.max_external_cycles == 8
    assert config.github.pr_base == "master"
    assert config.github.continue_command == "@rojobad /ai-dev-loop continue"


@pytest.mark.parametrize(
    "secret_field",
    ["token", "pat", "access_token", "api_key", "github_token", "gh_token"],
)
def test_github_rejects_secret_fields(tmp_path: Path, secret_field: str) -> None:
    path = tmp_path / "ai_dev_loop.yaml"
    path.write_text(
        _BASE + f"github:\n  enabled: true\n  {secret_field}: super-secret\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="credentials|must not contain"):
        load_project_config(path)


def test_github_rejects_empty_reviewer_logins(tmp_path: Path) -> None:
    path = tmp_path / "ai_dev_loop.yaml"
    path.write_text(
        _BASE + "github:\n  enabled: true\n  reviewer_logins: []\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        load_project_config(path)
