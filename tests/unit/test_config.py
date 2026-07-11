"""Unit tests for configuration validation and precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_dev_loop.config import (
    CODEX_REVIEW_REASONING_EFFORTS,
    ConfigOverrides,
    default_config_dict,
    format_codex_override,
    load_project_config,
    resolve_effective_config,
)
from ai_dev_loop.errors import ValidationError

_BASE_CURSOR = (
    "cursor:\n  command: agent\n  model: m\n"
    "  output_format: stream-json\n  force: true\n  trust_workspace: true\n  sandbox: disabled\n"
)
_BASE_WORKFLOW = (
    "workflow:\n  max_review_iterations: 3\n"
    "  require_clean_worktree: true\n  stage_mode: all\n  cursor_timeout_minutes: 90\n"
    "  codex_timeout_minutes: 90\nprompt:\n  directory: docs/plans\n"
    "  filename_template: prompt_{plan_stem}.txt\n"
)


def _write_config(path: Path, codex_block: str, *, project_name: str = "fixture-project") -> Path:
    path.write_text(
        f"version: 1\nproject:\n  name: {project_name}\n{_BASE_CURSOR}{codex_block}{_BASE_WORKFLOW}",
        encoding="utf-8",
    )
    return path


def test_load_fixture_config(git_repo: Path) -> None:
    config = load_project_config(git_repo / "ai_dev_loop.yaml")
    assert config.project.name == "fixture-project"
    assert config.cursor.model == "composer-2.5-fast"
    assert config.codex.review_model is None
    assert config.codex.review_reasoning_effort is None


def test_default_config_inherits_codex_model_and_reasoning() -> None:
    defaults = default_config_dict()
    assert defaults["codex"]["review_model"] is None
    assert defaults["codex"]["review_reasoning_effort"] is None


def test_config_validation_rejects_bad_slug(tmp_path: Path) -> None:
    bad = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        "codex:\n  command: codex\n  review_skill: review-staged-cursor-execution\n"
        "  sandbox: workspace-write\n",
        project_name="Bad_Name",
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


@pytest.mark.parametrize(
    "codex_block",
    [
        "codex:\n  command: codex\n  review_skill: review-staged-cursor-execution\n"
        "  sandbox: workspace-write\n",
        "codex:\n  command: codex\n  review_model: null\n  review_reasoning_effort: null\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    ],
)
def test_optional_codex_overrides_omitted_or_null(tmp_path: Path, codex_block: str) -> None:
    path = _write_config(tmp_path / "ai_dev_loop.yaml", codex_block)
    config = load_project_config(path)
    assert config.codex.review_model is None
    assert config.codex.review_reasoning_effort is None


def test_explicit_model_only(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        "codex:\n  command: codex\n  review_model: gpt-5.5\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    config = load_project_config(path)
    assert config.codex.review_model == "gpt-5.5"
    assert config.codex.review_reasoning_effort is None


def test_explicit_reasoning_only(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        "codex:\n  command: codex\n  review_reasoning_effort: high\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    config = load_project_config(path)
    assert config.codex.review_model is None
    assert config.codex.review_reasoning_effort == "high"


def test_explicit_model_and_reasoning(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        "codex:\n  command: codex\n  review_model: gpt-5.5\n  review_reasoning_effort: high\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    config = load_project_config(path)
    assert config.codex.review_model == "gpt-5.5"
    assert config.codex.review_reasoning_effort == "high"


@pytest.mark.parametrize("bad_model", ["", "   "])
def test_rejects_empty_or_whitespace_review_model(tmp_path: Path, bad_model: str) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        f"codex:\n  command: codex\n  review_model: '{bad_model}'\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    with pytest.raises(ValidationError, match="review_model"):
        load_project_config(path)


def test_rejects_invalid_reasoning_effort(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        "codex:\n  command: codex\n  review_reasoning_effort: turbo\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    with pytest.raises(ValidationError, match="review_reasoning_effort"):
        load_project_config(path)
    assert {"max", "ultra", "xhigh"}.issubset(CODEX_REVIEW_REASONING_EFFORTS)


@pytest.mark.parametrize("effort", ["max", "ultra"])
def test_accepts_gpt56_max_and_ultra_reasoning_efforts(tmp_path: Path, effort: str) -> None:
    path = _write_config(
        tmp_path / "ai_dev_loop.yaml",
        f"codex:\n  command: codex\n  review_reasoning_effort: {effort}\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n",
    )
    config = load_project_config(path)
    assert config.codex.review_reasoning_effort == effort


def test_cli_override_precedence_for_codex_fields(git_repo: Path) -> None:
    effective, _, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(
            codex_review_model="cli-model",
            codex_review_reasoning_effort="medium",
        ),
    )
    assert effective.codex.review_model == "cli-model"
    assert effective.codex.review_reasoning_effort == "medium"


def test_cli_override_model_does_not_inject_reasoning(git_repo: Path) -> None:
    effective, _, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(codex_review_model="cli-model"),
    )
    assert effective.codex.review_model == "cli-model"
    assert effective.codex.review_reasoning_effort is None


def test_cli_override_reasoning_does_not_inject_model(git_repo: Path) -> None:
    effective, _, _ = resolve_effective_config(
        repo_root=git_repo,
        overrides=ConfigOverrides(codex_review_reasoning_effort="high"),
    )
    assert effective.codex.review_model is None
    assert effective.codex.review_reasoning_effort == "high"


def test_format_codex_override() -> None:
    assert format_codex_override(None) == "inherited from session"
    assert format_codex_override("gpt-5.5") == "gpt-5.5"


def _write_global_config(path: Path, *, review_model: str, review_reasoning: str | None) -> None:
    reasoning_line = (
        f"  review_reasoning_effort: {review_reasoning}\n" if review_reasoning is not None else ""
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 1\n"
        "project:\n  name: global-project\n"
        f"{_BASE_CURSOR}"
        "codex:\n  command: codex\n"
        f"  review_model: {review_model}\n"
        f"{reasoning_line}"
        "  review_skill: review-staged-cursor-execution\n"
        "  sandbox: workspace-write\n"
        f"{_BASE_WORKFLOW}",
        encoding="utf-8",
    )


def test_global_codex_overrides_survive_repo_omission(git_repo: Path, isolated_xdg: Path) -> None:
    from ai_dev_loop.paths import global_config_path

    _write_global_config(
        global_config_path(),
        review_model="global-model",
        review_reasoning="high",
    )
    # Fixture repo omits both optional Codex fields.
    effective, source, _ = resolve_effective_config(repo_root=git_repo)
    assert "review_model" not in source.model_dump(exclude_unset=True)["codex"]
    assert "review_reasoning_effort" not in source.model_dump(exclude_unset=True)["codex"]
    assert effective.codex.review_model == "global-model"
    assert effective.codex.review_reasoning_effort == "high"


def test_repo_explicit_null_clears_global_codex_overrides(
    git_repo: Path, isolated_xdg: Path
) -> None:
    from ai_dev_loop.paths import global_config_path

    _write_global_config(
        global_config_path(),
        review_model="global-model",
        review_reasoning="high",
    )
    (git_repo / "ai_dev_loop.yaml").write_text(
        "version: 1\nproject:\n  name: fixture-project\n"
        f"{_BASE_CURSOR}"
        "codex:\n  command: codex\n  review_model: null\n  review_reasoning_effort: null\n"
        "  review_skill: review-staged-cursor-execution\n  sandbox: workspace-write\n"
        f"{_BASE_WORKFLOW}",
        encoding="utf-8",
    )
    effective, source, _ = resolve_effective_config(repo_root=git_repo)
    assert source.model_dump(exclude_unset=True)["codex"]["review_model"] is None
    assert source.model_dump(exclude_unset=True)["codex"]["review_reasoning_effort"] is None
    assert effective.codex.review_model is None
    assert effective.codex.review_reasoning_effort is None
