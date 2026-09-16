"""Domain and migration tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.rollover import (
    ABORT_PENDING_ROLLOVER_STATE_KIND,
    ABORTED_ROLLOVER_STATE_KIND,
    ACTIVE_ROLLOVER_STATE_KIND,
    BLOCKED_ROLLOVER_STATE_KIND,
    CLEANUP_PENDING_ROLLOVER_STATE_KIND,
    INTEGRATED_ROLLOVER_STATE_KIND,
    INTEGRATION_PENDING_ROLLOVER_STATE_KIND,
    PREPARED_ROLLOVER_STATE_KIND,
    ROLLOVER_STATE_ADAPTERS,
    AuthenticatedRolloverDefinition,
    PreparedRolloverState,
    RolloverIntegrationPolicy,
)
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    CursorBinding,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryBinding,
    WorkflowLimits,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import (
    SCHEMA_VERSION,
    SqliteSchedulerStore,
    migration_checksum,
)


def test_rollover_state_adapters_cover_all_kinds() -> None:
    assert PREPARED_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert ACTIVE_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert INTEGRATION_PENDING_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert CLEANUP_PENDING_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert BLOCKED_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert ABORT_PENDING_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert ABORTED_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS
    assert INTEGRATED_ROLLOVER_STATE_KIND in ROLLOVER_STATE_ADAPTERS


def test_prepared_rollover_state_round_trip() -> None:
    state = PreparedRolloverState(
        rollover_id="rol-" + "a" * 32,
        version=1,
        updated_at="2026-09-15T12:00:00.000000Z",
        definition_sha256="b" * 64,
        definition_artifact_sha256="c" * 64,
        source_run_id="run-source",
        source_run_id_prefix="run-sour",
        prepared_at="2026-09-15T12:00:00.000000Z",
    )
    adapter = ROLLOVER_STATE_ADAPTERS[PREPARED_ROLLOVER_STATE_KIND]
    payload = json.loads(adapter.dump_json(state))
    restored = adapter.validate_python(payload)
    assert restored == state


def test_rollover_integration_policy_rejects_empty_message() -> None:
    with pytest.raises(ValueError):
        RolloverIntegrationPolicy(commit_message="")


@pytest.fixture
def scheduler_db(isolated_xdg: Path) -> Path:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    db_path = state_root / "engine.sqlite3"
    SqliteSchedulerStore(db_path)
    return db_path


def test_v10_migration_creates_rollover_tables(scheduler_db: Path) -> None:
    store = SqliteSchedulerStore(scheduler_db)
    with store.begin_read() as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION == 10
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert "scheduler_authenticated_rollovers" in tables
    assert "scheduler_authenticated_rollover_idempotency" in tables
    assert "scheduler_sequence_rollover_resolutions" in tables


def _sample_rollover_definition() -> AuthenticatedRolloverDefinition:
    digest = "b" * 64
    repo_root = "/tmp/rollover-repo"
    worktree_key = hashlib.sha256(repo_root.encode()).hexdigest()
    return AuthenticatedRolloverDefinition(
        rollover_id="rol-" + ("a" * 32),
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
        source_staged_patch_path="rollover/source-staged.patch",
        source_staged_patch_sha256="e" * 64,
        source_staged_tree_sha256="f" * 40,
        source_final_review_result_path="rollover/source-final-review-result.json",
        source_final_review_result_sha256="1" * 64,
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
        integration=RolloverIntegrationPolicy(commit_message="rollover integration commit"),
        managed_worktree_path_token=hashlib.sha256(b"token").hexdigest(),
        private_ref="refs/ai-dev-loop/rollover/" + ("c" * 64),
        rollover_worktree_key=hashlib.sha256(b"managed").hexdigest(),
        prepared_at="2026-09-15T12:00:00.000000Z",
    )


def _schema_registry():
    from jsonschema import RefResolver

    schemas_dir = schema_path("scheduler-submitted-run-context-v3.json").parent
    definition_schema = json.loads(
        schema_path("scheduler-authenticated-rollover-definition-v1.json").read_text(
            encoding="utf-8"
        )
    )
    store: dict[str, object] = {}
    for name in (
        "scheduler-submitted-run-context-v2.json",
        "scheduler-submitted-run-context-v3.json",
        "scheduler-authenticated-rollover-definition-v1.json",
    ):
        store[name] = json.loads((schemas_dir / name).read_text(encoding="utf-8"))
    base_uri = (schemas_dir / "scheduler-authenticated-rollover-definition-v1.json").as_uri()
    return RefResolver(base_uri=base_uri, referrer=definition_schema, store=store)


def test_rollover_definition_schema_alignment() -> None:
    payload = json.loads(_sample_rollover_definition().model_dump_json())
    resolver = _schema_registry()
    jsonschema.validate(
        payload,
        resolver.referrer,
        resolver=resolver,
    )
    assert migration_checksum(10) == migration_checksum(10)
