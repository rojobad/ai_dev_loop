"""Runtime assembly regression tests for GitHub read policy opt-in gating."""

from __future__ import annotations

from tests.integration.phase16_8_helpers import (
    assert_runtime_read_policy_disabled_ignores_frozen_rules,
    assert_runtime_read_policy_enabled_preserves_rules,
    execution_context_v2,
)

from ai_dev_loop.pr_review_v2.runtime_factory import build_github_read_policy


def test_disabled_no_findings_clears_prefixes_and_thumbs_up() -> None:
    assert_runtime_read_policy_disabled_ignores_frozen_rules()


def test_enabled_no_findings_preserves_frozen_rules() -> None:
    assert_runtime_read_policy_enabled_preserves_rules()


def test_build_github_read_policy_matches_execution_context_flags() -> None:
    v2 = execution_context_v2(
        no_findings_enabled=True,
        accept_bot_thumbs_up=True,
        no_findings_prefixes=("Prefix:",),
    )
    policy = build_github_read_policy(v2, repository_cwd="/tmp/repo")
    assert policy.no_findings_enabled is True
    assert policy.accept_bot_thumbs_up is True
    assert policy.accepted_no_findings_prefixes == ("Prefix:",)
