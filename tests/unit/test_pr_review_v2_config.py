"""Unit tests for optional ``pr_review_v2`` project configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from ai_dev_loop.config import ProjectConfig, PrReviewV2Section, load_project_config
from ai_dev_loop.errors import ValidationError as ConfigValidationError

_BASE = """\
version: 1
project:
  name: fixture-project
cursor:
  command: agent
  model: m
  output_format: stream-json
  force: true
  trust_workspace: true
  sandbox: disabled
codex:
  command: codex
  review_skill: review-staged-cursor-execution
  sandbox: workspace-write
workflow:
  max_review_iterations: 3
  require_clean_worktree: true
  stage_mode: all
  cursor_timeout_minutes: 90
  codex_timeout_minutes: 90
prompt:
  directory: docs/plans
  filename_template: prompt_{plan_stem}.txt
"""


def _write(path: Path, extra: str = "") -> Path:
    path.write_text(_BASE + extra, encoding="utf-8")
    return path


def test_omit_pr_review_v2_section_is_ok(tmp_path: Path) -> None:
    config = load_project_config(_write(tmp_path / "ai_dev_loop.yaml"))
    assert config.pr_review_v2 is None
    assert config.pr_review_v2_enabled() is False


def test_defaults_keep_section_disabled(tmp_path: Path) -> None:
    config = load_project_config(
        _write(tmp_path / "ai_dev_loop.yaml", "pr_review_v2:\n  enabled: false\n")
    )
    assert config.pr_review_v2 is not None
    assert config.pr_review_v2.enabled is False
    assert config.pr_review_v2_enabled() is False


def test_invalid_worker_heartbeat_ge_lease_rejected() -> None:
    with pytest.raises(ValidationError):
        PrReviewV2Section(
            enabled=True,
            worker={"lease_ttl_seconds": 10, "heartbeat_interval_seconds": 10},
        )


def test_secret_fields_rejected() -> None:
    with pytest.raises(ValidationError, match="credentials field"):
        PrReviewV2Section.model_validate({"enabled": True, "token": "secret"})


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        PrReviewV2Section.model_validate({"enabled": True, "unexpected": True})


def test_unsafe_command_rejected(tmp_path: Path) -> None:
    bad = _write(
        tmp_path / "ai_dev_loop.yaml",
        "pr_review_v2:\n  enabled: true\n  gh_command: gh; rm -rf /\n",
    )
    with pytest.raises(ConfigValidationError):
        load_project_config(bad)


def test_project_config_accepts_enabled_section(tmp_path: Path) -> None:
    config = load_project_config(
        _write(
            tmp_path / "ai_dev_loop.yaml",
            "pr_review_v2:\n  enabled: true\n",
        )
    )
    assert isinstance(config, ProjectConfig)
    assert config.pr_review_v2_enabled() is True


def test_pr_review_v2_overall_timeout_accepts_cap_and_rejects_larger_value() -> None:
    assert PrReviewV2Section(overall_timeout_seconds=7200).overall_timeout_seconds == 7200
    with pytest.raises(ValidationError):
        PrReviewV2Section(overall_timeout_seconds=7201)


def test_no_findings_enabled_requires_evidence_rule() -> None:
    from ai_dev_loop.config import PrReviewV2NoFindingsSection

    with pytest.raises(ValidationError, match="at least one evidence rule"):
        PrReviewV2NoFindingsSection(enabled=True)
    section = PrReviewV2NoFindingsSection(enabled=True, accept_bot_thumbs_up=True)
    assert section.accept_bot_thumbs_up is True
    assert section.accepted_comment_prefixes == []


def test_no_findings_thumbs_up_defaults_disabled() -> None:
    from ai_dev_loop.config import PrReviewV2NoFindingsSection

    section = PrReviewV2NoFindingsSection()
    assert section.accept_bot_thumbs_up is False
