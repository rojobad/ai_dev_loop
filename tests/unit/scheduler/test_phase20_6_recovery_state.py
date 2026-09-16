"""Domain and migration tests for Phase 20.6 fresh-review recovery."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.checkpoint import GitIdentitySnapshot
from ai_dev_loop.scheduler.domain.recovery import (
    ACTIVE_RECOVERY_STATE_KIND,
    INTEGRATED_RECOVERY_STATE_KIND,
    PREPARED_RECOVERY_STATE_KIND,
    RECOVERY_STATE_ADAPTERS,
    FreshReviewRecoveryDefinition,
    PreparedRecoveryState,
    RecoveryIntegrationIntent,
    RecoveryIntegrationPolicy,
)
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    FreshReviewRecoveryLineage,
    PlanPromptBinding,
    RepositoryBinding,
    WorkflowLimits,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    SqliteSchedulerStore,
    migration_checksum,
)


def test_recovery_state_adapters_cover_all_kinds() -> None:
    assert PREPARED_RECOVERY_STATE_KIND in RECOVERY_STATE_ADAPTERS
    assert ACTIVE_RECOVERY_STATE_KIND in RECOVERY_STATE_ADAPTERS
    assert INTEGRATED_RECOVERY_STATE_KIND in RECOVERY_STATE_ADAPTERS


def test_prepared_recovery_state_round_trip() -> None:
    state = PreparedRecoveryState(
        recovery_id="rcv-" + "a" * 32,
        version=1,
        updated_at="2026-09-14T12:00:00.000000Z",
        definition_sha256="b" * 64,
        definition_artifact_sha256="c" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        prepared_at="2026-09-14T12:00:00.000000Z",
    )
    adapter = RECOVERY_STATE_ADAPTERS[PREPARED_RECOVERY_STATE_KIND]
    payload = json.loads(adapter.dump_json(state))
    restored = adapter.validate_python(payload)
    assert restored == state


def test_fresh_review_recovery_lineage_requires_review_seed() -> None:
    lineage = FreshReviewRecoveryLineage(
        recovery_id="rcv-test",
        source_run_id="run-1",
        source_staged_patch_sha256="c" * 64,
        source_parent_head="d" * 40,
        created_at="2026-09-14T12:00:00.000000Z",
    )
    assert lineage.review_seed is True


def test_recovery_integration_policy_rejects_empty_message() -> None:
    with pytest.raises(ValueError):
        RecoveryIntegrationPolicy(commit_message="")


@pytest.fixture
def scheduler_db(isolated_xdg: Path) -> Path:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    db_path = state_root / "engine.sqlite3"
    SqliteSchedulerStore(db_path)
    return db_path


def test_v9_migration_creates_recovery_tables(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "v9.sqlite3"
    paused = False

    def pause_v10(statement: str) -> None:
        nonlocal paused
        if not paused and "CREATE TABLE scheduler_authenticated_rollovers" in statement:
            paused = True
            raise RuntimeError("pause-v10")

    with pytest.raises(RuntimeError, match="pause-v10"):
        SqliteSchedulerStore(db, migration_fault_hook=pause_v10)
    conn = sqlite3.connect(db)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 9
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    finally:
        conn.close()
    assert "scheduler_fresh_review_recoveries" in tables
    assert "scheduler_fresh_review_recovery_idempotency" in tables
    assert "scheduler_sequence_recovery_resolutions" in tables


def _sample_recovery_definition() -> FreshReviewRecoveryDefinition:
    digest = "b" * 64
    repo_root = "/tmp/recovery-repo"
    worktree_key = hashlib.sha256(repo_root.encode()).hexdigest()
    return FreshReviewRecoveryDefinition(
        recovery_id="rcv-" + ("a" * 32),
        definition_sha256=digest,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        repository=RepositoryBinding(
            root=repo_root,
            git_common_dir=f"{repo_root}/.git",
            git_dir=f"{repo_root}/.git",
            branch="main",
            initial_head="d" * 40,
            worktree_key=worktree_key,
        ),
        target_branch_ref="refs/heads/main",
        parent_head="d" * 40,
        source_staged_patch_path="recovery/source-staged.patch",
        source_staged_patch_sha256="e" * 64,
        source_staged_tree_sha256="f" * 40,
        plan_prompt=PlanPromptBinding(
            plan_repository_path="docs/plan.md",
            prompt_source_repository_path="docs/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256=digest,
            prompt_artifact_path="plan/prompt.txt",
            prompt_sha256=digest,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path="config/effective.yaml",
            effective_config_sha256=digest,
            source_config_artifact_path="config/source.yaml",
            source_config_sha256=digest,
        ),
        codex=FreshCodexReviewerBinding(
            review_model="gpt-test",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-changes",
            sandbox="workspace-write",
            binding_artifact_path="codex/binding.json",
            binding_sha256=digest,
        ),
        cursor=CursorBinding(
            command="agent",
            model="composer-2.5-fast",
            output_format="stream-json",
            force=True,
            trust_workspace=True,
            sandbox="disabled",
        ),
        workflow=WorkflowLimits(
            max_review_iterations=3,
            stage_mode="all",
            cursor_timeout_minutes=30,
            codex_timeout_minutes=30,
            require_clean_worktree=True,
        ),
        controller=ControllerBinding(controller_session_id="00000000-0000-4000-8000-000000000001"),
        integration=RecoveryIntegrationPolicy(commit_message="recovery integration commit"),
        managed_worktree_path_token=hashlib.sha256(b"token").hexdigest(),
        private_ref="refs/ai-dev-loop/recovery/" + ("c" * 64),
        recovery_worktree_key=hashlib.sha256(b"managed").hexdigest(),
        prepared_at="2026-09-15T12:00:00.000000Z",
    )


def _schema_registry():
    from jsonschema import RefResolver

    schemas_dir = schema_path("scheduler-submitted-run-context-v3.json").parent
    definition_schema = json.loads(
        schema_path("scheduler-fresh-review-recovery-definition-v1.json").read_text(
            encoding="utf-8"
        )
    )
    store: dict[str, object] = {}
    for name in (
        "scheduler-submitted-run-context-v2.json",
        "scheduler-submitted-run-context-v3.json",
        "scheduler-fresh-review-recovery-definition-v1.json",
    ):
        store[name] = json.loads((schemas_dir / name).read_text(encoding="utf-8"))
    base_uri = (schemas_dir / "scheduler-fresh-review-recovery-definition-v1.json").as_uri()
    return RefResolver(base_uri=base_uri, referrer=definition_schema, store=store)


def test_recovery_definition_schema_alignment() -> None:
    payload = json.loads(_sample_recovery_definition().model_dump_json())
    resolver = _schema_registry()
    jsonschema.validate(
        payload,
        resolver.referrer,
        resolver=resolver,
    )


def test_recovery_integration_intent_schema_alignment() -> None:
    now_text = "2026-09-15T12:00:00.000000Z"
    identity = GitIdentitySnapshot(
        author_name="ai_dev_loop",
        author_email="ai-dev-loop@local",
        author_date=now_text,
        committer_name="ai_dev_loop",
        committer_email="ai-dev-loop@local",
        committer_date=now_text,
    )
    intent = RecoveryIntegrationIntent(
        recovery_id="rcv-" + ("a" * 32),
        source_run_id="run-source",
        recovery_run_id="run-recovery",
        accepted_outcome="completed",
        parent_head="d" * 40,
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
    payload = json.loads(intent.model_dump_json())
    schema = json.loads(
        schema_path("scheduler-fresh-review-recovery-integration-intent-v1.json").read_text(
            encoding="utf-8"
        )
    )
    jsonschema.validate(payload, schema)
    assert migration_checksum(9) == migration_checksum(9)
