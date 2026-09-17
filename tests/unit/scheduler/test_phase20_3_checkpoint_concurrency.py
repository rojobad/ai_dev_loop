"""Service-level concurrency, crash recovery, and authentication tests for Phase 20.3."""

from __future__ import annotations

import hashlib
import json
import secrets
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, DIGEST, sample_submitted_state
from tests.unit.scheduler.test_tick import _bootstrap_run

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.runners.git import CheckpointGitDeadline
from ai_dev_loop.scheduler.application.abort import (
    SchedulerAbortService,
    default_abort_service,
)
from ai_dev_loop.scheduler.application.abort_reconcile import release_abort_hold_resources
from ai_dev_loop.scheduler.application.checkpoint_abort_coordination import (
    CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
    CheckpointRefInspectionOutcome,
    abort_blocks_checkpoint_mutation,
    checkpoint_cas_uncertainty_active,
    inspect_checkpoint_ref_advancement,
    should_defer_checkpoint_abort_transition,
)
from ai_dev_loop.scheduler.application.contracts import TickRunReceipt
from ai_dev_loop.scheduler.application.cursor_evidence import FrozenRepositoryIdentity
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.git_checkpoint import (
    CheckpointFenceError,
    ProductionGitCheckpointPort,
)
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
    SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
    SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
    SequenceCheckpointEvidence,
    SequenceCheckpointIntent,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.common import worktree_key
from ai_dev_loop.scheduler.domain.events import (
    AbortRequestedEvent,
    RunAbortedEvent,
    SequenceCheckpointRequestedEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_run_aborted,
    apply_sequence_checkpoint_requested,
)
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    FrozenSequenceEntry,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    CodexWorkflowCheckpoint,
    ControllerBinding,
    CursorBinding,
    CursorWorkflowCheckpoint,
    EffectiveConfigBinding,
    FreshCodexReviewerBinding,
    PlanPromptBinding,
    RepositoryTargetBinding,
    WorkflowLimits,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _abort_service(store: SqliteSchedulerStore) -> SchedulerAbortService:
    return SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
    )


def _handoff_service(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    handoff_step_hook=None,
    git_checkpoint=None,
) -> SequenceHandoffService:
    return SequenceHandoffService(
        store,
        artifacts,
        git_checkpoint=git_checkpoint,
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
        handoff_step_hook=handoff_step_hook,
    )


def _checkpoint_pending_state(run_id: str) -> AwaitingCodexReviewState:
    base = sample_submitted_state(run_id=run_id)
    context = base.context.model_copy(
        update={
            "schema_version": 4,
            "sequence": {
                "sequence_id": "seq-1",
                "ordinal": 1,
                "total_phases": 2,
                "entry_hash": "a" * 64,
            },
        }
    )
    return AwaitingCodexReviewState(
        run_id=run_id,
        version=3,
        submitted_at=base.submitted_at,
        updated_at=base.updated_at,
        idempotency_key=base.idempotency_key,
        context=context,
        checkpoint={
            "authorized_at": base.submitted_at,
            "admitted_at": base.submitted_at,
            "admission_status_artifact_path": "git/status/01-admission.txt",
            "admission_status_sha256": "b" * 64,
        },
        cursor=CursorWorkflowCheckpoint(
            staged_patch_path="git/diffs/01.patch",
            staged_patch_sha256="c" * 64,
        ),
        codex=CodexWorkflowCheckpoint(
            reviewer_session_id="019def00-0000-0000-0000-0000000000bb",
            latest_review_result_path="codex/reviews/01.json",
            latest_review_result_sha256="d" * 64,
        ),
    )


def _promote_to_checkpoint_pending(
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    now: datetime,
) -> None:
    with store.begin_immediate() as conn:
        loaded, version, _ = store.load_validated_snapshot(conn, run_id)
    awaiting = _checkpoint_pending_state(run_id).model_copy(
        update={
            "context": _checkpoint_pending_state(run_id).context.model_copy(
                update={"repository": loaded.context.repository}
            )
        }
    )
    pending = apply_sequence_checkpoint_requested(
        awaiting,
        SequenceCheckpointRequestedEvent(
            run_id=run_id,
            sequence_id="seq-1",
            accepted_outcome="completed",
            review_iteration=1,
            checkpoint_intent_artifact_path=SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
            checkpoint_intent_sha256="e" * 64,
            checkpoint_trusted_tree_sha256="f" * 64,
        ),
        now_text=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    with store.begin_immediate() as conn:
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=pending,
            now=now,
        )


def _disposable_repo(tmp_path: Path) -> Path:
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
    tracked.write_text("base\nstaged\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    return repo


def test_checkpoint_hold_blocks_abort_cleanup_and_reservation_release(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="a" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=now,
        )
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        aborted = apply_run_aborted(
            state,
            RunAbortedEvent(
                run_id=run_id,
                reason="user_requested_abort",
                prior_state_kind=state.kind,
            ),
            now_text=now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=aborted,
            now=now,
        )
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.has_unresolved_abort_hold(conn, run_id)
        released = release_abort_hold_resources(store, conn, run_id=run_id, now=now)
        assert released is False
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_pre_cas_hold_allows_abort_and_releases_reservation(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="b" * 64,
            hold_reason="checkpoint_commit_persisted",
            ref_may_have_advanced=False,
            now=now,
        )
    result = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=artifacts,
        now_factory=lambda: now,
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
    ).abort_run(run_id)
    assert result.state_kind == "aborted"
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id) is False
        assert store.get_reservation_for_run(conn, run_id) is None


def test_commit_evidence_without_cas_does_not_defer_terminal_abort(tmp_path: Path) -> None:
    store, artifacts, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_to_checkpoint_pending(store, run_id, now=now)
    evidence = SequenceCheckpointEvidence(
        tree_sha256="a" * 40,
        commit_sha256="b" * 40,
    )
    artifacts.write_text(
        run_id,
        SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        max_bytes=8_192,
    )
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="c" * 64,
            hold_reason="checkpoint_commit_persisted",
            ref_may_have_advanced=False,
            now=now,
        )
        assert not checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert not should_defer_checkpoint_abort_transition(
            store, conn, run_id=run_id, artifacts=artifacts
        )
    service = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    )
    result = service.abort_run(run_id)
    assert result.state_kind == "aborted"
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        assert store.get_reservation_for_run(conn, run_id) is None


def test_post_cas_hold_defers_terminal_abort(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_to_checkpoint_pending(store, run_id, now=now)
    evidence = SequenceCheckpointEvidence(
        tree_sha256="a" * 40,
        commit_sha256="b" * 40,
    )
    artifacts.write_text(
        run_id,
        SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        max_bytes=8_192,
    )
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="c" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=now,
        )
        assert should_defer_checkpoint_abort_transition(
            store, conn, run_id=run_id, artifacts=artifacts
        )
    service = SchedulerAbortService(
        store,
        FakeAgentProcessBackend(),
        artifacts=artifacts,
        now_factory=lambda: now,
        event_id_factory=lambda: f"evt-{secrets.token_hex(8)}",
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        prior_kind = state.kind
    result = service.abort_run(run_id)
    assert result.state_kind == prior_kind
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == prior_kind
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_abort_blocks_pre_cas_mutation_but_not_post_cas_reconciliation(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.append_event(
            conn,
            event_id="evt-abort-req",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=AbortRequestedEvent(run_id=run_id, reason="user_requested_abort"),
            now=now,
        )
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert abort_blocks_checkpoint_mutation(
            store,
            conn,
            run_id=run_id,
            state=state,
            allow_post_cas_reconciliation=False,
        )
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="e" * 64,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=now,
        )
        assert not abort_blocks_checkpoint_mutation(
            store,
            conn,
            run_id=run_id,
            state=state,
            allow_post_cas_reconciliation=True,
        )


def test_orphan_intent_crash_reconstructs_trusted_tree_and_evidence(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
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
        reviewed_patch_sha256=patch_sha,
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
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-orphan-crash"
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    artifacts.write_text(run_id, SEQUENCE_CHECKPOINT_INTENT_ARTIFACT, intent_text, max_bytes=32_768)
    artifacts.write_bytes(run_id, "git/diffs/01.patch", patch_bytes, max_bytes=1_048_576)
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    service = _handoff_service(store, artifacts)
    trusted_sha, trusted_tree = service._reconstruct_missing_checkpoint_artifacts(
        run_id,
        intent,
        intent_sha256=hashlib.sha256(intent_text.encode()).hexdigest(),
        recorded_at="2026-09-13T12:00:00.000000Z",
        git_deadline=CheckpointGitDeadline.from_lease(
            datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
        ),
    )
    assert trusted_tree.reviewed_tree_sha256
    assert (artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT).is_file()
    evidence = service._load_checkpoint_evidence(run_id)
    assert evidence is not None
    assert evidence.tree_sha256 == trusted_tree.reviewed_tree_sha256
    assert evidence.commit_sha256 is None
    assert len(trusted_sha) == 64


def test_evidence_tree_tamper_refused_with_unchanged_trusted_tree(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
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
        reviewed_patch_sha256=patch_sha,
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
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    run_id = "run-tamper"
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    intent_sha = hashlib.sha256(intent_text.encode()).hexdigest()
    artifacts.write_text(run_id, SEQUENCE_CHECKPOINT_INTENT_ARTIFACT, intent_text, max_bytes=32_768)
    artifacts.write_bytes(run_id, "git/diffs/01.patch", patch_bytes, max_bytes=1_048_576)
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    service = _handoff_service(store, artifacts)
    trusted_sha, trusted_tree = service._reconstruct_missing_checkpoint_artifacts(
        run_id,
        intent,
        intent_sha256=intent_sha,
        recorded_at="2026-09-13T12:00:00.000000Z",
        git_deadline=CheckpointGitDeadline.from_lease(
            datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
        ),
    )
    tampered = SequenceCheckpointEvidence(
        tree_sha256="f" * 40,
        commit_sha256="e" * 40,
    )
    artifacts.replace_text(
        run_id,
        SEQUENCE_CHECKPOINT_EVIDENCE_ARTIFACT,
        json.dumps(tampered.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        max_bytes=8_192,
    )
    assert trusted_sha
    assert trusted_tree.intent_sha256 == intent_sha
    port = ProductionGitCheckpointPort()
    patch_path = artifacts.run_root(run_id) / "git/diffs/01.patch"
    with pytest.raises(Exception, match="tree SHA drift|trusted|evidence"):
        port.execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=tampered,
            persist_tree_sha=lambda _: None,
            persist_commit_sha=lambda _: None,
            deadline=CheckpointGitDeadline.from_lease(
                datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
            ),
            now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        )


def test_lease_expiry_prevents_late_git_mutation(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
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
        reviewed_patch_sha256=patch_sha,
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
    trusted_tree = SequenceCheckpointTrustedTree(
        intent_sha256="a" * 64,
        reviewed_tree_sha256="b" * 40,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at="2026-09-13T12:00:00.000000Z",
    )
    from ai_dev_loop.runners import git as git_module

    call_count = 0

    def shrinking_timeout(*, lease_expires_at, now):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count >= 4:
            raise ValidationError("checkpoint tick lease expired before Git subprocess")
        return 30.0

    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    port = ProductionGitCheckpointPort()
    with (
        patch.object(git_module, "checkpoint_git_timeout_for_lease", shrinking_timeout),
        pytest.raises(ValidationError, match="lease expired"),
    ):
        port.execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=None,
            persist_tree_sha=lambda _: None,
            persist_commit_sha=lambda _: None,
            deadline=CheckpointGitDeadline.from_lease(
                datetime(2026, 9, 13, 12, 0, 2, tzinfo=UTC),
            ),
            now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        )
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head
    )


def test_concurrent_abort_fence_before_cas_prevents_ref_movement(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    tree_sha = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
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
        reviewed_patch_sha256=patch_sha,
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
    trusted_tree = SequenceCheckpointTrustedTree(
        intent_sha256="c" * 64,
        reviewed_tree_sha256=tree_sha,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at="2026-09-13T12:00:00.000000Z",
    )
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)

    def fence(boundary: str) -> None:
        if boundary == "pre_update_ref":
            raise CheckpointFenceError("checkpoint_aborted")

    port = ProductionGitCheckpointPort()
    with pytest.raises(CheckpointFenceError, match="checkpoint_aborted"):
        port.execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=None,
            persist_tree_sha=lambda _: None,
            persist_commit_sha=lambda _: None,
            mutation_fence=fence,
            deadline=CheckpointGitDeadline.from_lease(
                datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
            ),
            now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        )
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head
    )


def test_emit_reconciliation_hold_records_durable_row(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    service = _handoff_service(store, artifacts)
    receipt = service._emit_reconciliation_hold(
        run_id="run-hold-row",
        intent_sha256="d" * 64,
        ref_may_have_advanced=True,
    )
    assert receipt.action == "checkpoint_reconciliation_hold"
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, "run-hold-row")
        row = conn.execute(
            "SELECT ref_may_have_advanced FROM scheduler_checkpoint_holds WHERE run_id = ?",
            ("run-hold-row",),
        ).fetchone()
    assert row is not None
    assert int(row[0]) == 1


def _orphan_binding_fixture(
    repo: Path,
    *,
    run_id: str = "run-1",
) -> dict[str, object]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    intent = SequenceCheckpointIntent(
        sequence_id="seq-1",
        sequence_version=1,
        predecessor_run_id=run_id,
        predecessor_run_version=3,
        predecessor_ordinal=1,
        successor_run_id="run-2",
        successor_ordinal=2,
        accepted_outcome="completed",
        branch_ref="refs/heads/master",
        parent_head=head,
        reviewed_patch_sha256=patch_sha,
        commit_message="checkpoint after phase one",
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
    state = _checkpoint_pending_state(run_id)
    predecessor_entry = SimpleNamespace(
        commit_message="checkpoint after phase one",
        planned_run_id=run_id,
        ordinal=1,
    )
    successor_entry = SimpleNamespace(
        commit_message=None,
        planned_run_id="run-2",
        ordinal=2,
    )
    sequence_state = SimpleNamespace(
        sequence_id="seq-1",
        version=1,
        definition=SimpleNamespace(entries=(predecessor_entry, successor_entry)),
    )
    identity = FrozenRepositoryIdentity(
        root=str(repo.resolve()),
        git_common_dir=str((repo / ".git").resolve()),
        git_dir=str((repo / ".git").resolve()),
        branch="master",
        initial_head=head,
    )
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    return {
        "intent": intent,
        "intent_sha256": hashlib.sha256(intent_text.encode()).hexdigest(),
        "state": state,
        "version": 3,
        "sequence_state": sequence_state,
        "sequence_binding_ordinal": 1,
        "accepted_outcome": "completed",
        "result_path": "codex/reviews/01.json",
        "result_sha": "a" * 64,
        "identity": identity,
        "expected_parent": head,
        "predecessor_entry": predecessor_entry,
        "successor_entry": successor_entry,
        "expected_branch_ref": "refs/heads/master",
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("commit_message", "tampered checkpoint message", "commit_message mismatch"),
        ("successor_run_id", "run-evil", "successor_run_id mismatch"),
        ("successor_ordinal", 9, "successor_ordinal mismatch"),
        ("branch_ref", "refs/heads/evil", "branch_ref mismatch"),
    ],
)
def test_orphan_intent_binding_refuses_tampered_fields(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    repo = _disposable_repo(tmp_path)
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    service = _handoff_service(store, ProtectedArtifactStore(tmp_path / "artifacts"))
    bindings = _orphan_binding_fixture(repo)
    if field == "successor_ordinal":
        payload = bindings["intent"].model_dump()
        payload[field] = value
        intent = SequenceCheckpointIntent.model_construct(**payload)
    else:
        intent = bindings["intent"].model_copy(update={field: value})
    with pytest.raises(ValidationError, match=message):
        service._validate_orphan_intent_bindings(
            intent,
            intent_sha256=bindings["intent_sha256"],
            state=bindings["state"],
            version=bindings["version"],
            sequence_state=bindings["sequence_state"],
            sequence_binding_ordinal=bindings["sequence_binding_ordinal"],
            accepted_outcome=bindings["accepted_outcome"],
            result_path=bindings["result_path"],
            result_sha=bindings["result_sha"],
            identity=bindings["identity"],
            expected_parent=bindings["expected_parent"],
            predecessor_entry=bindings["predecessor_entry"],
            successor_entry=bindings["successor_entry"],
            expected_branch_ref=bindings["expected_branch_ref"],
        )


def test_default_abort_service_uses_artifact_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_root = tmp_path / "artifacts"
    db_path = tmp_path / "engine.sqlite3"
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.abort.default_artifact_root",
        lambda: artifact_root,
    )
    service = default_abort_service(db_path=db_path, backend=FakeAgentProcessBackend())
    assert service._artifacts is not None
    assert service._artifacts.artifact_root == artifact_root


def test_abort_after_cas_before_ledger_preserves_reservation(tmp_path: Path) -> None:
    store, artifacts, run_id = _bootstrap_run(tmp_path)
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_to_checkpoint_pending(store, run_id, now=now)
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="d" * 64,
            hold_reason="checkpoint_ref_advanced",
            ref_may_have_advanced=True,
            now=now,
        )
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert store.has_abort_requested_for_run(conn, run_id)


def test_tampered_predecessor_commit_sha256_blocks_phase_two_checkpoint_parent(
    tmp_path: Path,
) -> None:
    repo = _disposable_repo(tmp_path)
    base_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    port = ProductionGitCheckpointPort()
    intent, _ = _intent_for_repo_from_disposable(repo, patch_sha=patch_sha, head=base_head)
    intent_text_for_tree = (
        json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    intent_sha_for_tree = hashlib.sha256(intent_text_for_tree.encode()).hexdigest()
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    trusted_tree = SequenceCheckpointTrustedTree(
        intent_sha256=intent_sha_for_tree,
        reviewed_tree_sha256=subprocess.check_output(
            ["git", "write-tree"], cwd=repo, text=True
        ).strip(),
        reviewed_patch_sha256=patch_sha,
        parent_head=base_head,
        recorded_at="2026-09-13T12:00:00.000000Z",
    )
    commit = port.execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=lambda _: None,
        persist_commit_sha=lambda _: None,
        deadline=CheckpointGitDeadline.from_lease(datetime(2026, 9, 13, 12, 5, tzinfo=UTC)),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )
    checkpoint_head = commit.commit_sha
    assert checkpoint_head != base_head
    tracked = repo / "a.txt"

    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    prev_run = "run-phase-one"
    cur_run = "run-phase-two"
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    intent_sha = hashlib.sha256(intent_text.encode()).hexdigest()
    artifacts.write_text(
        prev_run, SEQUENCE_CHECKPOINT_INTENT_ARTIFACT, intent_text, max_bytes=32_768
    )
    trusted_text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    trusted_sha = hashlib.sha256(trusted_text.encode()).hexdigest()
    artifacts.write_text(
        prev_run, SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT, trusted_text, max_bytes=8_192
    )
    from ai_dev_loop.scheduler.application.git_checkpoint import checkpoint_result_from_commit

    result = checkpoint_result_from_commit(
        intent,
        commit=commit,
        intent_sha256=intent_sha,
        trusted_tree_sha256=trusted_sha,
    )
    unrelated_path = repo / "unrelated.txt"
    unrelated_path.write_text("unrelated\n", encoding="utf-8")
    subprocess.run(["git", "add", unrelated_path], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "unrelated"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    unrelated_head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    subprocess.run(
        ["git", "reset", "--hard", checkpoint_head], cwd=repo, check=True, capture_output=True
    )
    tracked.write_text("base\nstaged\nphase-two\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True, capture_output=True)
    phase_two_patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    phase_two_patch_sha = hashlib.sha256(phase_two_patch_bytes).hexdigest()
    live_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert live_head == checkpoint_head

    tampered = result.model_copy(update={"commit_sha256": unrelated_head})
    artifacts.write_text(
        prev_run,
        "sequence-checkpoints/result.json",
        json.dumps(tampered.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        max_bytes=8_192,
    )

    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    service = _handoff_service(store, artifacts)
    predecessor_entry = SimpleNamespace(
        commit_message="checkpoint after phase one",
        planned_run_id=prev_run,
        ordinal=1,
    )
    sequence_state = SimpleNamespace(
        sequence_id=intent.sequence_id,
        materialized_entries=(
            SimpleNamespace(run_id=prev_run, ordinal=1),
            SimpleNamespace(run_id=cur_run, ordinal=2),
        ),
        definition=SimpleNamespace(entries=(predecessor_entry, SimpleNamespace())),
    )
    with (
        store.begin_read() as conn,
        patch(
            "ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store."
            "authenticate_checkpoint_successor_replacement_chain"
        ),
        pytest.raises(ValidationError, match="commit object tree does not match"),
    ):
        service._authenticate_predecessor_checkpoint_result(
            conn,
            prev_run_id=prev_run,
            sequence_state=sequence_state,
            current_ordinal=2,
        )
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == checkpoint_head
    )
    assert phase_two_patch_sha


def _intent_for_repo_from_disposable(
    repo: Path,
    *,
    patch_sha: str,
    head: str,
) -> tuple[SequenceCheckpointIntent, bytes]:
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    return (
        SequenceCheckpointIntent(
            sequence_id="seq-three",
            sequence_version=2,
            predecessor_run_id="run-phase-one",
            predecessor_run_version=4,
            predecessor_ordinal=1,
            successor_run_id="run-phase-two",
            successor_ordinal=2,
            accepted_outcome="completed",
            branch_ref="refs/heads/master",
            parent_head=head,
            reviewed_patch_sha256=patch_sha,
            commit_message="checkpoint after phase one",
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
        ),
        patch_bytes,
    )


def _checkpoint_intent_from_repo(
    repo: Path,
    *,
    run_id: str = "run-1",
) -> tuple[SequenceCheckpointIntent, bytes, str, SequenceCheckpointTrustedTree]:
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    patch_bytes = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    tree_sha = subprocess.check_output(["git", "write-tree"], cwd=repo, text=True).strip()
    intent = SequenceCheckpointIntent(
        sequence_id="seq-1",
        sequence_version=1,
        predecessor_run_id=run_id,
        predecessor_run_version=1,
        predecessor_ordinal=1,
        successor_run_id="run-2",
        successor_ordinal=2,
        accepted_outcome="completed",
        branch_ref="refs/heads/master",
        parent_head=head,
        reviewed_patch_sha256=patch_sha,
        commit_message="checkpoint after phase one",
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
    intent_sha = hashlib.sha256(intent_text.encode()).hexdigest()
    trusted_tree = SequenceCheckpointTrustedTree(
        intent_sha256=intent_sha,
        reviewed_tree_sha256=tree_sha,
        reviewed_patch_sha256=patch_sha,
        parent_head=head,
        recorded_at="2026-09-13T12:00:00.000000Z",
    )
    return intent, patch_bytes, intent_sha, trusted_tree


def _frozen_sequence_entry(
    *,
    ordinal: int,
    phase_name: str,
    planned_run_id: str,
    commit_message: str | None,
) -> FrozenSequenceEntry:
    return FrozenSequenceEntry(
        ordinal=ordinal,
        phase_name=phase_name,
        planned_run_id=planned_run_id,
        commit_message=commit_message,
        plan_prompt=PlanPromptBinding(
            plan_repository_path="docs/plans/sample-plan.md",
            prompt_source_repository_path="docs/plans/prompt.txt",
            plan_artifact_path="plan/plan.md",
            plan_sha256=DIGEST,
            prompt_artifact_path="prompts/cursor-initial.txt",
            prompt_sha256=DIGEST,
        ),
        effective_config=EffectiveConfigBinding(
            effective_config_artifact_path="effective-config.yaml",
            effective_config_sha256=DIGEST,
            source_config_artifact_path="source-config.yaml",
            source_config_sha256=DIGEST,
        ),
        codex=FreshCodexReviewerBinding(
            review_model="gpt-5.6-sol",
            review_reasoning_effort="high",
            review_model_source="explicit",
            review_reasoning_source="explicit",
            command="codex",
            review_skill="review-staged-cursor-execution",
            sandbox="workspace-write",
            binding_artifact_path="codex/fresh-reviewer-input.json",
            binding_sha256=DIGEST,
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
            require_clean_worktree=False,
        ),
    )


def _active_sequence_for_intent(
    intent: SequenceCheckpointIntent,
    *,
    repo_root: str,
) -> ActiveSequenceState:
    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    now_text = "2026-09-13T12:00:00.000000Z"
    first_entry = _frozen_sequence_entry(
        ordinal=1,
        phase_name="phase-one",
        planned_run_id=intent.predecessor_run_id,
        commit_message=intent.commit_message,
    )
    second_entry = _frozen_sequence_entry(
        ordinal=2,
        phase_name="phase-two",
        planned_run_id=intent.successor_run_id,
        commit_message=None,
    )
    definition = PreparedSequenceDefinition(
        sequence_id=intent.sequence_id,
        name="two-phase",
        project_name="fixture-project",
        repository=RepositoryTargetBinding(
            root=repo_root,
            worktree_key=worktree_key(repo_root),
        ),
        manifest_original_artifact_path="sequence/manifest.original.yaml",
        manifest_original_sha256=DIGEST,
        manifest_resolved_artifact_path="sequence/manifest.resolved.yaml",
        manifest_resolved_sha256=DIGEST,
        controller=ControllerBinding(controller_session_id=CONTROLLER_SESSION),
        entries=(first_entry, second_entry),
    )
    return ActiveSequenceState(
        schema_version=1,
        sequence_id=intent.sequence_id,
        version=intent.sequence_version,
        prepared_at=now_text,
        updated_at=now_text,
        started_at=now_text,
        idempotency_key=DIGEST,
        definition=definition,
        current_ordinal=1,
        current_run_id=intent.predecessor_run_id,
        materialized_entries=(
            MaterializedSequenceEntry(
                ordinal=1,
                run_id=intent.predecessor_run_id,
                entry_hash=frozen_entry_hash(first_entry),
                materialized_at=now_text,
            ),
        ),
        residual_risk_ordinals=(),
    )


def _bootstrap_run_on_store(
    store: SqliteSchedulerStore,
    *,
    repo: Path,
    run_id: str,
    now: datetime | None = None,
) -> str:
    from ai_dev_loop.scheduler.application.start import StartService
    from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent

    started_at = now or datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    state = sample_submitted_state(run_id=run_id, repo_root=str(repo.resolve()))
    state = state.model_copy(
        update={
            "context": state.context.model_copy(
                update={
                    "workflow": state.context.workflow.model_copy(
                        update={"require_clean_worktree": False}
                    )
                }
            )
        }
    )
    event = RunSubmittedEvent(
        run_id=run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=run_id,
            state=state,
            event_id=f"evt-submit-{run_id}",
            event=event,
            now=started_at,
        )
    StartService(store, now_factory=lambda: started_at).start(run_id)
    return run_id


def _promote_run_to_terminal_completed(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    now: datetime,
) -> None:
    from ai_dev_loop.scheduler.domain.events import RunCompletedEvent
    from ai_dev_loop.scheduler.domain.reducer import apply_run_completed
    from ai_dev_loop.scheduler.domain.state import CheckpointPendingState

    _promote_to_checkpoint_pending(store, run_id, now=now)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, CheckpointPendingState)
        completed = apply_run_completed(
            state,
            RunCompletedEvent(run_id=run_id, review_iteration=state.codex.review_iteration),
            now_text=now_text,
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=completed,
            now=now,
        )


def _persist_active_sequence_for_fixture(
    store: SqliteSchedulerStore,
    active: ActiveSequenceState,
    *,
    now: datetime,
) -> None:
    kind, payload, digest = store.dump_sequence_state(active)
    definition = active.definition
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
        persist_authoritative_lineage_from_state,
    )

    with store.begin_immediate() as conn:
        existing = conn.execute(
            "SELECT 1 FROM scheduler_sequences WHERE sequence_id = ?",
            (active.sequence_id,),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            """
            INSERT INTO scheduler_sequences(
                sequence_id, name, state_kind, project_name, repository_root,
                worktree_key, entry_count, idempotency_key, version,
                prepared_at, updated_at, payload, payload_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                active.sequence_id,
                definition.name,
                kind,
                definition.project_name,
                definition.repository.root,
                definition.repository.worktree_key,
                len(definition.entries),
                active.idempotency_key,
                active.version,
                active.prepared_at,
                now_text,
                payload,
                digest,
            ),
        )
        for entry in definition.entries:
            entry_payload, entry_digest = store.dump_sequence_entry(entry)
            conn.execute(
                """
                INSERT INTO scheduler_sequence_entries(
                    sequence_id, ordinal, phase_name, planned_run_id,
                    payload, payload_sha256
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    active.sequence_id,
                    entry.ordinal,
                    entry.phase_name,
                    entry.planned_run_id,
                    entry_payload,
                    entry_digest,
                ),
            )
        persist_authoritative_lineage_from_state(conn, active)


def _clear_pending_effects(store: SqliteSchedulerStore, run_id: str) -> None:
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_effects
            SET status = 'succeeded'
            WHERE run_id = ? AND status = 'pending'
            """,
            (run_id,),
        )


class _HookedGitCheckpointPort(ProductionGitCheckpointPort):
    def __init__(
        self,
        *,
        after_authorize_ref_update: Callable[[], None] | None = None,
        after_update_ref: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._after_authorize_ref_update = after_authorize_ref_update
        self._after_update_ref = after_update_ref

    def execute_checkpoint(
        self,
        intent: SequenceCheckpointIntent,
        *,
        patch_path: Path,
        trusted_tree: SequenceCheckpointTrustedTree,
        evidence: SequenceCheckpointEvidence | None,
        persist_tree_sha: Callable[[str], None],
        persist_commit_sha: Callable[[str], None],
        mutation_fence: Callable[[str], None] | None = None,
        authorize_ref_update: Callable[[], None] | None = None,
        on_ref_advanced: Callable[[], None] | None = None,
        deadline: CheckpointGitDeadline,
        now_factory: Callable[[], datetime],
    ):

        hooked_authorize = authorize_ref_update
        if authorize_ref_update is not None and self._after_authorize_ref_update is not None:

            def hooked_authorize() -> None:
                authorize_ref_update()
                self._after_authorize_ref_update()

            hooked_authorize = hooked_authorize

        if self._after_update_ref is not None:
            from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

            original_update = git_checkpoint_module.checkpoint_git_update_ref_cas

            def wrapped_update(*args, **kwargs):  # type: ignore[no-untyped-def]
                result = original_update(*args, **kwargs)
                self._after_update_ref()
                return result

            with patch.object(
                git_checkpoint_module, "checkpoint_git_update_ref_cas", wrapped_update
            ):
                return super().execute_checkpoint(
                    intent,
                    patch_path=patch_path,
                    trusted_tree=trusted_tree,
                    evidence=evidence,
                    persist_tree_sha=persist_tree_sha,
                    persist_commit_sha=persist_commit_sha,
                    mutation_fence=mutation_fence,
                    authorize_ref_update=hooked_authorize,
                    on_ref_advanced=on_ref_advanced,
                    deadline=deadline,
                    now_factory=now_factory,
                )

        return super().execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=evidence,
            persist_tree_sha=persist_tree_sha,
            persist_commit_sha=persist_commit_sha,
            mutation_fence=mutation_fence,
            authorize_ref_update=hooked_authorize,
            on_ref_advanced=on_ref_advanced,
            deadline=deadline,
            now_factory=now_factory,
        )


def _promote_checkpoint_pending_with_artifacts(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    run_id: str,
    intent: SequenceCheckpointIntent,
    trusted_tree: SequenceCheckpointTrustedTree,
    patch_bytes: bytes,
    *,
    intent_sha256: str,
    trusted_tree_sha256: str,
    now: datetime,
) -> None:
    review_bytes = json.dumps({"has_actionable_findings": False}).encode()
    review_sha = hashlib.sha256(review_bytes).hexdigest()
    if review_sha != intent.review_result_sha256:
        intent = intent.model_copy(update={"review_result_sha256": review_sha})
    intent_text = json.dumps(intent.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    intent_sha256 = hashlib.sha256(intent_text.encode()).hexdigest()
    trusted_tree = trusted_tree.model_copy(update={"intent_sha256": intent_sha256})
    trusted_text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    trusted_tree_sha256 = hashlib.sha256(trusted_text.encode()).hexdigest()
    artifacts.write_text(run_id, SEQUENCE_CHECKPOINT_INTENT_ARTIFACT, intent_text, max_bytes=32_768)
    artifacts.write_text(
        run_id,
        SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
        trusted_text,
        max_bytes=8_192,
    )
    artifacts.write_bytes(
        run_id,
        intent.staged_patch_artifact_path,
        patch_bytes,
        max_bytes=1_048_576,
    )
    artifacts.write_bytes(
        run_id,
        intent.review_result_artifact_path,
        review_bytes,
        max_bytes=1_048_576,
    )
    with store.begin_immediate() as conn:
        loaded, version, _ = store.load_validated_snapshot(conn, run_id)
    repo = Path(intent.repository_root)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    branch = subprocess.check_output(
        ["git", "symbolic-ref", "--short", "HEAD"], cwd=repo, text=True
    ).strip()
    admission_path = "git/status/01-admission.txt"
    admission_text = (
        f"branch={branch}\n"
        f"head={head}\n"
        f"git_common_dir={intent.git_common_dir}\n"
        f"git_dir={intent.git_dir}\n"
    )
    admission_sha = hashlib.sha256(admission_text.encode()).hexdigest()
    artifacts.write_text(run_id, admission_path, admission_text, max_bytes=8_192)
    from ai_dev_loop.scheduler.domain.state import AdmittedRunCheckpoint

    admitted_checkpoint = AdmittedRunCheckpoint(
        authorized_at=getattr(loaded, "authorized_at", loaded.submitted_at),
        authorized_controller_session_id=getattr(loaded, "authorized_controller_session_id", None),
        admitted_at=getattr(loaded, "authorized_at", loaded.submitted_at),
        admission_status_artifact_path=admission_path,
        admission_status_sha256=admission_sha,
    )
    template = _checkpoint_pending_state(run_id)
    awaiting = template.model_copy(
        update={
            "context": template.context.model_copy(
                update={"repository": loaded.context.repository}
            ),
            "checkpoint": admitted_checkpoint,
            "cursor": template.cursor.model_copy(
                update={
                    "staged_patch_path": intent.staged_patch_artifact_path,
                    "staged_patch_sha256": intent.reviewed_patch_sha256,
                }
            ),
            "codex": template.codex.model_copy(
                update={
                    "latest_review_result_path": intent.review_result_artifact_path,
                    "latest_review_result_sha256": intent.review_result_sha256,
                }
            ),
        }
    )
    checkpoint_event = SequenceCheckpointRequestedEvent(
        run_id=run_id,
        sequence_id=intent.sequence_id,
        accepted_outcome="completed",
        review_iteration=awaiting.codex.review_iteration,
        checkpoint_intent_artifact_path=SEQUENCE_CHECKPOINT_INTENT_ARTIFACT,
        checkpoint_intent_sha256=intent_sha256,
        checkpoint_trusted_tree_sha256=trusted_tree_sha256,
    )
    pending = apply_sequence_checkpoint_requested(
        awaiting,
        checkpoint_event,
        now_text=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    )
    with store.begin_immediate() as conn:
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=pending,
            now=now,
        )
        store.append_event(
            conn,
            event_id=f"evt-checkpoint-requested-{run_id}",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=checkpoint_event,
            now=now,
        )


def _prepare_handoff_reconcile(
    tmp_path: Path,
    *,
    git_checkpoint: ProductionGitCheckpointPort | None = None,
) -> dict[str, object]:
    repo = _disposable_repo(tmp_path)
    store, artifacts, run_id = _bootstrap_run(
        tmp_path,
        repo_root=str(repo.resolve()),
        require_clean=False,
    )
    intent, patch_bytes, intent_sha, trusted_tree = _checkpoint_intent_from_repo(
        repo, run_id=run_id
    )
    trusted_text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    trusted_sha = hashlib.sha256(trusted_text.encode()).hexdigest()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_checkpoint_pending_with_artifacts(
        store,
        artifacts,
        run_id,
        intent,
        trusted_tree,
        patch_bytes,
        intent_sha256=intent_sha,
        trusted_tree_sha256=trusted_sha,
        now=now,
    )
    tick_owner = "tick-owner-cas"
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id=tick_owner,
            now=now,
            ttl_seconds=300,
        )
    assert lease is not None
    generation, _ = lease
    active_sequence = _active_sequence_for_intent(intent, repo_root=str(repo.resolve()))
    original_load = store.load_validated_sequence_state

    def patched_load(conn, sequence_id: str):  # type: ignore[no-untyped-def]
        if sequence_id == intent.sequence_id:
            return active_sequence
        return original_load(conn, sequence_id)

    store.load_validated_sequence_state = patched_load  # type: ignore[method-assign]
    _clear_pending_effects(store, run_id)
    service = _handoff_service(store, artifacts, git_checkpoint=git_checkpoint)
    intent_bytes = (artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_INTENT_ARTIFACT).read_bytes()
    intent = SequenceCheckpointIntent.model_validate_json(intent_bytes)
    intent_sha = hashlib.sha256(intent_bytes).hexdigest()
    trusted_tree_bytes = (
        artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
    ).read_bytes()
    trusted_tree = SequenceCheckpointTrustedTree.model_validate_json(trusted_tree_bytes)
    return {
        "repo": repo,
        "store": store,
        "artifacts": artifacts,
        "run_id": run_id,
        "intent": intent,
        "intent_sha": intent_sha,
        "trusted_tree": trusted_tree,
        "service": service,
        "tick_owner": tick_owner,
        "tick_generation": generation,
        "parent_head": intent.parent_head,
        "load_sequence_state_original": original_load,
        "active_sequence": active_sequence,
    }


def _git_deadline_for_fixture() -> CheckpointGitDeadline:
    return CheckpointGitDeadline.from_lease(datetime(2026, 9, 13, 12, 5, tzinfo=UTC))


def _hold_from_evidence(fixture: dict[str, object], conn: object) -> bool:
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    trusted_tree = fixture["trusted_tree"]
    assert isinstance(trusted_tree, SequenceCheckpointTrustedTree)
    artifacts = fixture["artifacts"]
    assert isinstance(artifacts, ProtectedArtifactStore)
    run_id = str(fixture["run_id"])
    patch_path = artifacts.run_root(run_id) / intent.staged_patch_artifact_path
    return service._reconcile_checkpoint_hold_from_evidence(
        conn,
        run_id=run_id,
        intent=intent,
        intent_sha256=str(fixture["intent_sha"]),
        trusted_tree=trusted_tree,
        patch_path=patch_path,
        tick_owner_id=str(fixture["tick_owner"]),
        tick_lease_generation=int(fixture["tick_generation"]),
        git_deadline=_git_deadline_for_fixture(),
    )


def _reconcile_checkpoint(
    fixture: dict[str, object],
    *,
    stop_before_handoff: bool = False,
    tick_owner: str | None = None,
    tick_generation: int | None = None,
) -> object:
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    run_id = str(fixture["run_id"])
    owner = tick_owner if tick_owner is not None else str(fixture["tick_owner"])
    generation = tick_generation if tick_generation is not None else int(fixture["tick_generation"])
    if stop_before_handoff:
        original_handoff = service._complete_handoff

        def _stop_handoff(*args, **kwargs):  # type: ignore[no-untyped-def]
            return TickRunReceipt(run_id=run_id, action="test_stop_before_handoff")

        service._complete_handoff = _stop_handoff  # type: ignore[method-assign]
        try:
            return service._reconcile_checkpoint(
                run_id,
                tick_owner_id=owner,
                tick_lease_generation=generation,
            )
        finally:
            service._complete_handoff = original_handoff  # type: ignore[method-assign]
    return service._reconcile_checkpoint(
        run_id,
        tick_owner_id=owner,
        tick_lease_generation=generation,
    )


def test_cas_authorized_hold_defers_terminal_abort(tmp_path: Path) -> None:
    store, _, run_id = _bootstrap_run(tmp_path)
    artifacts = ProtectedArtifactStore(tmp_path / "artifacts")
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_to_checkpoint_pending(store, run_id, now=now)
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="c" * 64,
            hold_reason=CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON,
            ref_may_have_advanced=False,
            now=now,
        )
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert should_defer_checkpoint_abort_transition(
            store, conn, run_id=run_id, artifacts=artifacts
        )
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_abort_after_cas_authorization_blocks_update_ref_and_defers_abort(
    tmp_path: Path,
) -> None:
    repo = _disposable_repo(tmp_path)
    store, artifacts, run_id = _bootstrap_run(
        tmp_path,
        repo_root=str(repo.resolve()),
        require_clean=False,
    )
    intent, patch_bytes, intent_sha, trusted_tree = _checkpoint_intent_from_repo(
        repo, run_id=run_id
    )
    trusted_text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    trusted_sha = hashlib.sha256(trusted_text.encode()).hexdigest()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_checkpoint_pending_with_artifacts(
        store,
        artifacts,
        run_id,
        intent,
        trusted_tree,
        patch_bytes,
        intent_sha256=intent_sha,
        trusted_tree_sha256=trusted_sha,
        now=now,
    )
    tick_owner = "tick-owner-abort-after-auth"
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(conn, owner_id=tick_owner, now=now, ttl_seconds=300)
    assert lease is not None
    generation, _ = lease
    active_sequence = _active_sequence_for_intent(intent, repo_root=str(repo.resolve()))
    original_load = store.load_validated_sequence_state

    def patched_load(conn, sequence_id: str):  # type: ignore[no-untyped-def]
        if sequence_id == intent.sequence_id:
            return active_sequence
        return original_load(conn, sequence_id)

    store.load_validated_sequence_state = patched_load  # type: ignore[method-assign]
    _clear_pending_effects(store, run_id)
    parent_head = intent.parent_head

    def request_abort_after_authorize() -> None:
        default_abort_service(
            db_path=store.db_path,
            backend=FakeAgentProcessBackend(),
            artifact_root=artifacts.artifact_root,
        ).abort_run(run_id)

    service = _handoff_service(
        store,
        artifacts,
        git_checkpoint=_HookedGitCheckpointPort(
            after_authorize_ref_update=request_abort_after_authorize,
        ),
    )
    receipt = service._reconcile_checkpoint(
        run_id,
        tick_owner_id=tick_owner,
        tick_lease_generation=generation,
    )
    assert receipt.action == "checkpoint_reconciliation_hold"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == parent_head
    )
    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None

    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

    recovery_owner = "tick-owner-recovery"
    with store.begin_immediate() as conn:
        store.release_global_tick_lease(
            conn,
            owner_id=tick_owner,
            generation=generation,
            now=now,
        )
        lease = store.acquire_global_tick_lease(
            conn,
            owner_id=recovery_owner,
            now=now,
            ttl_seconds=300,
        )
    assert lease is not None
    recovery_generation, _ = lease
    update_ref_calls = 0
    original_update_ref = git_checkpoint_module.checkpoint_git_update_ref_cas

    def count_update_ref(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal update_ref_calls
        update_ref_calls += 1
        return original_update_ref(*args, **kwargs)

    staged_before = subprocess.check_output(["git", "diff", "--cached"], cwd=repo)
    with patch.object(git_checkpoint_module, "checkpoint_git_update_ref_cas", count_update_ref):
        recovery_receipt = service._reconcile_checkpoint(
            run_id,
            tick_owner_id=recovery_owner,
            tick_lease_generation=recovery_generation,
        )
    assert recovery_receipt.action == "checkpoint_aborted"
    assert update_ref_calls == 0
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == parent_head
    )
    assert subprocess.check_output(["git", "diff", "--cached"], cwd=repo) == staged_before
    with store.begin_read() as conn:
        assert not checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert store.has_abort_requested_for_run(conn, run_id)
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        assert store.get_reservation_for_run(conn, run_id) is None
        successor = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchone()
    assert successor is None

    repeat_receipt = service._reconcile_checkpoint(
        run_id,
        tick_owner_id=recovery_owner,
        tick_lease_generation=recovery_generation,
    )
    assert repeat_receipt.action == "checkpoint_state_changed"
    repeat_abort = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert repeat_abort.state_kind == "aborted"
    assert repeat_abort.idempotent_replay


def test_restart_after_cas_authorization_with_persisted_abort_terminalizes(
    tmp_path: Path,
) -> None:
    repo = _disposable_repo(tmp_path)
    store, artifacts, run_id = _bootstrap_run(
        tmp_path,
        repo_root=str(repo.resolve()),
        require_clean=False,
    )
    intent, patch_bytes, intent_sha, trusted_tree = _checkpoint_intent_from_repo(
        repo, run_id=run_id
    )
    trusted_text = json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    trusted_sha = hashlib.sha256(trusted_text.encode()).hexdigest()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    _promote_checkpoint_pending_with_artifacts(
        store,
        artifacts,
        run_id,
        intent,
        trusted_tree,
        patch_bytes,
        intent_sha256=intent_sha,
        trusted_tree_sha256=trusted_sha,
        now=now,
    )
    tick_owner = "tick-owner-restart"
    with store.begin_immediate() as conn:
        lease = store.acquire_global_tick_lease(conn, owner_id=tick_owner, now=now, ttl_seconds=300)
    assert lease is not None
    generation, _ = lease
    active_sequence = _active_sequence_for_intent(intent, repo_root=str(repo.resolve()))
    original_load = store.load_validated_sequence_state

    def patched_load(conn, sequence_id: str):  # type: ignore[no-untyped-def]
        if sequence_id == intent.sequence_id:
            return active_sequence
        return original_load(conn, sequence_id)

    store.load_validated_sequence_state = patched_load  # type: ignore[method-assign]
    _clear_pending_effects(store, run_id)
    parent_head = intent.parent_head

    def request_abort_after_authorize() -> None:
        default_abort_service(
            db_path=store.db_path,
            backend=FakeAgentProcessBackend(),
            artifact_root=artifacts.artifact_root,
        ).abort_run(run_id)

    service = _handoff_service(
        store,
        artifacts,
        git_checkpoint=_HookedGitCheckpointPort(
            after_authorize_ref_update=request_abort_after_authorize,
        ),
    )
    interrupted = service._reconcile_checkpoint(
        run_id,
        tick_owner_id=tick_owner,
        tick_lease_generation=generation,
    )
    assert interrupted.action == "checkpoint_reconciliation_hold"

    recovery_owner = "tick-owner-restart-recovery"
    with store.begin_immediate() as conn:
        store.release_global_tick_lease(
            conn,
            owner_id=tick_owner,
            generation=generation,
            now=now,
        )
        recovery_lease = store.acquire_global_tick_lease(
            conn,
            owner_id=recovery_owner,
            now=now,
            ttl_seconds=300,
        )
    assert recovery_lease is not None
    recovery_generation, _ = recovery_lease
    recovery_receipt = service._reconcile_checkpoint(
        run_id,
        tick_owner_id=recovery_owner,
        tick_lease_generation=recovery_generation,
    )
    assert recovery_receipt.action == "checkpoint_aborted"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == parent_head
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        assert store.get_reservation_for_run(conn, run_id) is None

    repeat_receipt = service._reconcile_checkpoint(
        run_id,
        tick_owner_id=recovery_owner,
        tick_lease_generation=recovery_generation,
    )
    assert repeat_receipt.action == "checkpoint_state_changed"
    repeat_abort = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert repeat_abort.state_kind == "aborted"
    assert repeat_abort.idempotent_replay


def test_abort_before_cas_authorization_terminalizes_without_uncertainty(
    tmp_path: Path,
) -> None:
    fixture = _prepare_handoff_reconcile(tmp_path)
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    parent_head = str(fixture["parent_head"])
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.append_event(
            conn,
            event_id="evt-abort-pre-cas",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=AbortRequestedEvent(run_id=run_id, reason="user_requested_abort"),
            now=now,
        )
    receipt = _reconcile_checkpoint(fixture)
    assert receipt.action == "checkpoint_aborted"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == parent_head
    )
    with store.begin_read() as conn:
        assert not checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        assert store.get_reservation_for_run(conn, run_id) is None

    repeat_receipt = _reconcile_checkpoint(fixture)
    assert repeat_receipt.action == "checkpoint_state_changed"
    repeat_abort = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert repeat_abort.state_kind == "aborted"
    assert repeat_abort.idempotent_replay


def test_post_cas_postcondition_error_upgrades_hold_and_defers_default_abort(
    tmp_path: Path,
) -> None:
    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

    fixture = _prepare_handoff_reconcile(tmp_path)
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    with patch.object(
        git_checkpoint_module,
        "validate_checkpoint_postconditions",
        side_effect=ValidationError("postcondition failed after cas"),
    ):
        _reconcile_checkpoint(fixture)
    with store.begin_read() as conn:
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_inspect_checkpoint_ref_advancement_distinguishes_ambiguous_reads(
    tmp_path: Path,
) -> None:
    repo = _disposable_repo(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    intent, _, _, _ = _checkpoint_intent_from_repo(repo)
    commit_sha = "c" * 40
    from ai_dev_loop.scheduler.application import (
        checkpoint_abort_coordination as coordination_module,
    )

    original_rev_parse = coordination_module.checkpoint_git_rev_parse

    def failing_head(*args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 2 and args[1] == "HEAD":
            raise OSError("ambiguous HEAD read")
        return original_rev_parse(*args, **kwargs)

    with patch.object(coordination_module, "checkpoint_git_rev_parse", failing_head):
        inspection = inspect_checkpoint_ref_advancement(intent, commit_sha)
    assert inspection.outcome == CheckpointRefInspectionOutcome.AMBIGUOUS

    applied = inspect_checkpoint_ref_advancement(intent, head)
    assert applied.outcome == CheckpointRefInspectionOutcome.APPLIED

    not_applied = inspect_checkpoint_ref_advancement(intent, "c" * 40)
    assert not_applied.outcome == CheckpointRefInspectionOutcome.NOT_APPLIED_AT_PARENT


def test_crash_after_cas_before_on_ref_advanced_defers_default_abort(
    tmp_path: Path,
) -> None:
    fixture = _prepare_handoff_reconcile(
        tmp_path,
        git_checkpoint=_HookedGitCheckpointPort(
            after_update_ref=lambda: (_ for _ in ()).throw(
                RuntimeError("simulated crash before on_ref_advanced")
            ),
        ),
    )
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    parent_head = str(fixture["parent_head"])
    with pytest.raises(RuntimeError, match="simulated crash"):
        _reconcile_checkpoint(fixture, stop_before_handoff=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    assert head != parent_head
    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip() == head
    )


def test_crash_before_on_ref_advanced_ambiguous_recovery_preserves_abort_deferral(
    tmp_path: Path,
) -> None:
    from ai_dev_loop.scheduler.application import (
        checkpoint_abort_coordination as coordination_module,
    )
    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

    fixture = _prepare_handoff_reconcile(
        tmp_path,
        git_checkpoint=_HookedGitCheckpointPort(
            after_update_ref=lambda: (_ for _ in ()).throw(
                RuntimeError("simulated crash before on_ref_advanced")
            ),
        ),
    )
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    service = fixture["service"]
    assert isinstance(intent, SequenceCheckpointIntent)
    assert isinstance(service, SequenceHandoffService)

    with pytest.raises(RuntimeError, match="simulated crash"):
        _reconcile_checkpoint(fixture, stop_before_handoff=True)

    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    original_rev_parse = coordination_module.checkpoint_git_rev_parse
    update_ref_calls = 0
    original_update_ref = git_checkpoint_module.checkpoint_git_update_ref_cas

    def count_update_ref(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal update_ref_calls
        update_ref_calls += 1
        return original_update_ref(*args, **kwargs)

    def ambiguous_head_read(*args, **kwargs):  # type: ignore[no-untyped-def]
        if len(args) >= 2 and args[1] == "HEAD":
            raise OSError("ambiguous HEAD read during recovery")
        return original_rev_parse(*args, **kwargs)

    with (
        store.begin_immediate() as conn,
        patch.object(coordination_module, "checkpoint_git_rev_parse", ambiguous_head_read),
    ):
        _hold_from_evidence(fixture, conn)

    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None

    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.has_abort_requested_for_run(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None

    with store.begin_immediate() as conn:
        _hold_from_evidence(fixture, conn)
    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert bool(int(hold["ref_may_have_advanced"]))
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)

    with patch.object(git_checkpoint_module, "checkpoint_git_update_ref_cas", count_update_ref):
        _reconcile_checkpoint(fixture, stop_before_handoff=True)
    assert update_ref_calls == 0
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == commit_sha
    )


def test_ambiguous_update_ref_after_cas_authorization_preserves_uncertainty(
    tmp_path: Path,
) -> None:
    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

    fixture = _prepare_handoff_reconcile(tmp_path)
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    parent_head = str(fixture["parent_head"])
    with patch.object(
        git_checkpoint_module,
        "checkpoint_git_update_ref_cas",
        side_effect=ValidationError("ambiguous update-ref outcome"),
    ):
        receipt = _reconcile_checkpoint(fixture)
    assert receipt.action == "checkpoint_reconciliation_hold"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == parent_head
    )
    with store.begin_read() as conn:
        hold = store.get_checkpoint_reconciliation_hold_row(conn, run_id)
        assert hold is not None
        assert str(hold["hold_reason"]) == CHECKPOINT_CAS_AUTHORIZED_HOLD_REASON
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
        assert not bool(int(hold["ref_may_have_advanced"]))
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    with store.begin_read() as conn:
        assert store.get_reservation_for_run(conn, run_id) is not None


def test_restart_recovers_false_hold_when_checkpoint_commit_is_live_head(
    tmp_path: Path,
) -> None:
    fixture = _prepare_handoff_reconcile(tmp_path)
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    _reconcile_checkpoint(fixture, stop_before_handoff=True)
    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    with store.begin_immediate() as conn:
        conn.execute(
            "UPDATE scheduler_checkpoint_holds SET ref_may_have_advanced = 0 WHERE run_id = ?",
            (run_id,),
        )
        assert not checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    with store.begin_immediate() as conn:
        _hold_from_evidence(fixture, conn)
    with store.begin_read() as conn:
        assert checkpoint_cas_uncertainty_active(store, conn, run_id=run_id)
    result = default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    assert result.state_kind == "checkpoint_pending"
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == commit_sha
    )


def test_on_ref_advanced_runs_before_postcondition_validation(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module

    repo = _disposable_repo(tmp_path)
    intent, patch_bytes, _intent_sha, trusted_tree = _checkpoint_intent_from_repo(repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(patch_bytes)
    advanced = False

    def mark_advanced() -> None:
        nonlocal advanced
        advanced = True

    with (
        patch.object(
            git_checkpoint_module,
            "validate_checkpoint_postconditions",
            side_effect=ValidationError("postcondition failed"),
        ),
        pytest.raises(ValidationError, match="postcondition failed"),
    ):
        ProductionGitCheckpointPort().execute_checkpoint(
            intent,
            patch_path=patch_path,
            trusted_tree=trusted_tree,
            evidence=None,
            persist_tree_sha=lambda _: None,
            persist_commit_sha=lambda _: None,
            on_ref_advanced=mark_advanced,
            deadline=CheckpointGitDeadline.from_lease(
                datetime(2026, 9, 13, 12, 5, tzinfo=UTC),
            ),
            now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
        )
    assert advanced
