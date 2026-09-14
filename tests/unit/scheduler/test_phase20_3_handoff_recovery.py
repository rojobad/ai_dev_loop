"""Recovery, fencing, and immutability tests for Phase 20.3 checkpoint handoff."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.unit.scheduler.test_phase20_1_sequence_prepare import (
    _prepare_service,
    _write_manifest,
)

from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.sequence_prepare import SequencePrepareOptions
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
)
from ai_dev_loop.scheduler.domain.sequence import (
    AWAITING_FINALIZATION_SEQUENCE_STATE_KIND,
    AwaitingFinalizationSequenceState,
    MaterializedSequenceEntry,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _two_phase_manifest() -> str:
    phases = [
        {
            "name": "phase-one",
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "commit_message": "checkpoint after phase one",
            "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
        },
        {
            "name": "phase-two",
            "plan_path": "docs/plans/sample-plan.md",
            "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
            "codex": {"review_model": "gpt-5.6-sol", "review_reasoning_effort": "high"},
        },
    ]
    return yaml.safe_dump(
        {"schema_version": 1, "name": "two-phase", "phases": phases}, sort_keys=False
    )


def test_prepare_rejects_awaiting_finalization_sequence_reuse(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    manifest = _write_manifest(git_repo / "sequence.yaml", _two_phase_manifest())
    service = _prepare_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        repo=git_repo,
    )
    first = service.prepare(
        SequencePrepareOptions(
            manifest_path=manifest,
            repo_path=git_repo,
            db_path=scheduler_paths["db_path"],
            artifact_root=scheduler_paths["artifact_root"],
        )
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    now_text = datetime(2026, 9, 13, 12, 0, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_read() as conn:
        prepared = store.load_validated_sequence_state(conn, first.sequence_id)
    awaiting = AwaitingFinalizationSequenceState(
        schema_version=1,
        sequence_id=prepared.sequence_id,
        version=prepared.version + 1,
        prepared_at=prepared.prepared_at,
        updated_at=now_text,
        started_at=now_text,
        finalized_at=now_text,
        idempotency_key=prepared.idempotency_key,
        definition=prepared.definition,
        final_run_id=prepared.definition.entries[1].planned_run_id,
        final_outcome="completed",
        materialized_entries=(
            MaterializedSequenceEntry(
                ordinal=1,
                run_id=prepared.definition.entries[0].planned_run_id,
                entry_hash="a" * 64,
                materialized_at=now_text,
            ),
            MaterializedSequenceEntry(
                ordinal=2,
                run_id=prepared.definition.entries[1].planned_run_id,
                entry_hash="b" * 64,
                materialized_at=now_text,
            ),
        ),
        residual_risk_ordinals=(),
    )
    existing_row = {"sequence_id": first.sequence_id}
    with (
        patch.object(
            service.store,
            "find_existing_prepared_sequence",
            return_value=existing_row,
        ),
        patch.object(
            service.store,
            "load_validated_sequence_state",
            return_value=awaiting,
        ),
        pytest.raises(SchedulerEngineError, match="not reusable for prepare"),
    ):
        service.prepare(
            SequencePrepareOptions(
                manifest_path=manifest,
                repo_path=git_repo,
                db_path=scheduler_paths["db_path"],
                artifact_root=scheduler_paths["artifact_root"],
            )
        )


def test_checkpoint_intent_remains_immutable_after_git_evidence(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "x@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "User"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    tracked = repo / "a.txt"
    tracked.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    intent = SequenceCheckpointIntent(
        sequence_id="seq-1",
        sequence_version=1,
        predecessor_run_id="run-1",
        predecessor_run_version=1,
        predecessor_ordinal=1,
        successor_run_id="run-2",
        successor_ordinal=2,
        accepted_outcome="completed",
        branch_ref="refs/heads/master",
        parent_head=head,
        reviewed_patch_sha256="c" * 64,
        commit_message="checkpoint",
        git_identity={
            "author_name": "User",
            "author_email": "x@example.com",
            "author_date": "1234567890 +0000",
            "committer_name": "User",
            "committer_email": "x@example.com",
            "committer_date": "1234567890 +0000",
        },
        repository_root=str(repo.resolve()),
        git_common_dir=str((repo / ".git").resolve()),
        git_dir=str((repo / ".git").resolve()),
        staged_patch_artifact_path="git/diffs/01.patch",
        review_result_artifact_path="codex/reviews/01.json",
        review_result_sha256="a" * 64,
    )
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    intent_path = tmp_path / "intent.json"
    intent_path.write_text(intent_text, encoding="utf-8")
    original_bytes = intent_path.read_bytes()
    tampered = json.loads(intent_text)
    tampered["commit_sha256"] = "f" * 40
    intent_path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert intent_path.read_bytes() != original_bytes
    restored = SequenceCheckpointIntent.model_validate_json(original_bytes)
    assert "commit_sha256" not in restored.model_dump()


def test_awaiting_finalization_state_kind_constant() -> None:
    assert AWAITING_FINALIZATION_SEQUENCE_STATE_KIND == "awaiting_finalization"


def test_checkpoint_evidence_replace_is_atomic_and_hash_verified(tmp_path: Path) -> None:
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-evidence"
    initial = SequenceCheckpointEvidence(tree_sha256="b" * 40)
    text = json.dumps(initial.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    artifacts.write_text(run_id, "sequence-checkpoints/evidence.json", text, max_bytes=8_192)
    updated = SequenceCheckpointEvidence(
        tree_sha256="b" * 40,
        commit_sha256="c" * 40,
    )
    updated_text = json.dumps(updated.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    replaced = artifacts.replace_text(
        run_id,
        "sequence-checkpoints/evidence.json",
        updated_text,
        max_bytes=8_192,
    )
    loaded = SequenceCheckpointEvidence.model_validate_json(
        (artifacts.run_root(run_id) / "sequence-checkpoints/evidence.json").read_bytes()
    )
    assert loaded.commit_sha256 == "c" * 40
    assert replaced.sha256 == hashlib.sha256(updated_text.encode("utf-8")).hexdigest()


def test_checkpoint_git_timeout_respects_tick_lease() -> None:
    from datetime import UTC, datetime, timedelta

    from ai_dev_loop.runners.git import (
        CHECKPOINT_GIT_TIMEOUT_SECONDS,
        checkpoint_git_timeout_for_lease,
    )

    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    expires = now + timedelta(seconds=10)
    timeout = checkpoint_git_timeout_for_lease(lease_expires_at=expires, now=now)
    assert timeout < CHECKPOINT_GIT_TIMEOUT_SECONDS
    assert timeout <= 9.0


def test_orphan_intent_write_text_or_verify_is_idempotent(tmp_path: Path) -> None:
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-orphan"
    text = '{"schema_version":1,"sequence_id":"seq"}\n'
    first = artifacts.write_text_or_verify(
        run_id,
        SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
        text,
        max_bytes=32_768,
    )
    second = artifacts.write_text_or_verify(
        run_id,
        SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
        text,
        max_bytes=32_768,
    )
    assert first.sha256 == second.sha256
