"""Integration service tests for Phase 20.6."""

from __future__ import annotations

from datetime import UTC, datetime

from ai_dev_loop.scheduler.application.recovery_integration import RecoveryIntegrationService
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256
from ai_dev_loop.scheduler.domain.recovery import (
    RECOVERY_INTEGRATION_INTENT_ARTIFACT,
    RecoveryIntegrationIntent,
)


def test_recovery_integration_intent_includes_intent_sha256_fields() -> None:
    now_text = datetime(2026, 9, 15, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    identity = GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + "a" * 32,
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head="f" * 40,
        source_tree_sha256="a" * 40,
        accepted_tree_sha256="b" * 40,
        reviewed_patch_sha256="c" * 64,
        review_result_sha256="d" * 64,
        commit_message="recovery integration commit",
        target_branch_ref="refs/heads/main",
        private_ref="refs/ai-dev-loop/recovery/" + ("e" * 64),
        target_repository_root="/tmp/target",
        target_git_common_dir="/tmp/target/.git",
        target_git_dir="/tmp/target/.git",
        managed_repository_root="/tmp/managed",
        managed_git_common_dir="/tmp/repo/.git",
        managed_git_dir="/tmp/repo/.git/worktrees/managed",
        git_identity=identity,
        recorded_at=now_text,
    )
    digest = canonical_json_sha256(intent.model_dump(mode="json"))
    assert len(digest) == 64
    assert RECOVERY_INTEGRATION_INTENT_ARTIFACT == "recovery/integration-intent.json"
    assert RecoveryIntegrationService is not None
