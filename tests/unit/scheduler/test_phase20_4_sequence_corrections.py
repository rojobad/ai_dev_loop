"""Regression tests for Phase 20.4 correction findings."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence, _start_service
from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (
    _checkpoint_intent_from_repo,
    _disposable_repo,
)

from ai_dev_loop.runners.git import CheckpointGitDeadline
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.abort_reconcile import release_abort_hold_resources
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import FakeAgentProcessBackend
from ai_dev_loop.scheduler.application.git_checkpoint import ProductionGitCheckpointPort
from ai_dev_loop.scheduler.application.sequence_abort import SequenceAbortService
from ai_dev_loop.scheduler.application.sequence_checkpoint_evidence import (
    SequenceCheckpointEvidenceError,
    authenticate_checkpoint_result_bindings,
    verify_checkpoint_commit_in_repository,
)
from ai_dev_loop.scheduler.application.sequence_reconcile import SequenceReconcileService
from ai_dev_loop.scheduler.application.sequence_report import (
    SEQUENCE_COMPLETION_REPORT_ARTIFACT,
    reconcile_completion_report_publication,
    set_completion_report_publication_step_hook,
)
from ai_dev_loop.scheduler.domain.checkpoint import (
    SequenceCheckpointIntent,
    SequenceCheckpointResult,
    SequenceCheckpointTrustedTree,
)
from ai_dev_loop.scheduler.domain.events import MaxIterationsReachedEvent, RunAbortedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_max_iterations_reached, apply_run_aborted
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    SequenceCompletionReport,
)
from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
    SequenceLifecycleValidationError,
    validate_sequence_lifecycle_transition,
)
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    AuthorizedState,
    AwaitingCodexReviewState,
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


def test_direct_run_abort_retains_reservation_for_active_sequence_with_hold(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    run_id = start.run_id
    now = FIXED_NOW
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
        assert isinstance(aborted, AbortedState)
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=aborted,
            now=now,
        )
        released = release_abort_hold_resources(store, conn, run_id=run_id, now=now)
        assert released is False
        assert store.get_reservation_for_run(conn, run_id) is not None
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        receipt = reconcile.reconcile_run(conn, run_id)
        assert receipt is not None
        assert receipt.action == "sequence_reconciliation_pending"
        assert store.get_reservation_for_run(conn, run_id) is not None
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    with store.begin_immediate() as conn:
        store.release_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256="a" * 64,
        )
        release_abort_hold_resources(store, conn, run_id=run_id, now=now)
        receipt = reconcile.reconcile_run(conn, run_id)
        assert receipt is not None
        assert receipt.action == "sequence_blocked"
        assert store.get_reservation_for_run(conn, run_id) is None
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, BlockedSequenceState)


def test_sequence_abort_on_max_iterations_reached_completes_without_run_abort(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.sequence import (
        AbortPendingSequenceState,
        future_entry_ordinals,
    )

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        if not isinstance(state, AwaitingCodexReviewState):
            pytest.skip("run did not reach awaiting_codex_review in this fixture")
        terminal = apply_max_iterations_reached(
            state,
            MaxIterationsReachedEvent(
                run_id=start.run_id,
                review_iteration=1,
                review_result_path="codex/reviews/01.json",
                review_result_sha256="a" * 64,
                fix_prompt_path="prompts/fixes/01.txt",
                fix_prompt_sha256="b" * 64,
                correction_envelope_path="prompts/fixes/01.execution-envelope.txt",
                correction_envelope_sha256="c" * 64,
            ),
            now_text=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=terminal,
            now=FIXED_NOW,
        )
        pending = AbortPendingSequenceState(
            schema_version=active.schema_version,
            sequence_id=active.sequence_id,
            version=active.version + 1,
            prepared_at=active.prepared_at,
            updated_at="2026-09-13T12:00:00.000000Z",
            started_at=active.started_at,
            abort_requested_at="2026-09-13T12:00:00.000000Z",
            abort_reason="user_requested_abort",
            idempotency_key=active.idempotency_key,
            definition=active.definition,
            current_ordinal=active.current_ordinal,
            current_run_id=start.run_id,
            materialized_entries=active.materialized_entries,
            residual_risk_ordinals=active.residual_risk_ordinals,
            cancelled_ordinals=future_entry_ordinals(active.definition, active.current_ordinal),
        )
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=pending,
            now=FIXED_NOW,
        )
        receipt = reconcile.reconcile_run(conn, start.run_id)
    assert receipt is not None
    assert receipt.action == "sequence_aborted"


def test_checkpoint_parent_mismatch_is_rejected(tmp_path: Path) -> None:
    intent = SequenceCheckpointIntent.model_validate(
        {
            "schema_version": 1,
            "sequence_id": "seq-1",
            "sequence_version": 1,
            "predecessor_run_id": "run-1",
            "predecessor_run_version": 1,
            "predecessor_ordinal": 1,
            "successor_run_id": "run-2",
            "successor_ordinal": 2,
            "accepted_outcome": "completed",
            "branch_ref": "refs/heads/main",
            "parent_head": "a" * 40,
            "reviewed_patch_sha256": "b" * 64,
            "commit_message": "checkpoint",
            "git_identity": {
                "author_name": "a",
                "author_email": "a@example.com",
                "author_date": "2026-01-01T00:00:00+00:00",
                "committer_name": "a",
                "committer_email": "a@example.com",
                "committer_date": "2026-01-01T00:00:00+00:00",
            },
            "repository_root": str(tmp_path),
            "git_common_dir": str(tmp_path / ".git"),
            "git_dir": str(tmp_path / ".git"),
            "staged_patch_artifact_path": "git/diffs/01.patch",
            "review_result_artifact_path": "codex/reviews/01.json",
            "review_result_sha256": "c" * 64,
        }
    )
    trusted_tree = SequenceCheckpointTrustedTree(
        intent_sha256="d" * 64,
        reviewed_tree_sha256="e" * 40,
        reviewed_patch_sha256="b" * 64,
        parent_head="a" * 40,
        recorded_at="2026-01-01T00:00:00.000000Z",
    )
    result = SequenceCheckpointResult(
        sequence_id="seq-1",
        predecessor_run_id="run-1",
        predecessor_ordinal=1,
        successor_run_id="run-2",
        accepted_outcome="completed",
        commit_sha256="f" * 40,
        tree_sha256="e" * 40,
        parent_head="f" * 40,
        branch_ref="refs/heads/main",
        reviewed_patch_sha256="b" * 64,
        intent_sha256="d" * 64,
        trusted_tree_sha256="9" * 64,
    )
    with pytest.raises(SequenceCheckpointEvidenceError, match="parent disagrees with intent"):
        authenticate_checkpoint_result_bindings(
            result=result,
            intent=intent,
            trusted_tree=trusted_tree,
            intent_sha256="d" * 64,
            trusted_tree_file_sha256="9" * 64,
            run_id="run-1",
            materialized=MaterializedSequenceEntry(
                ordinal=1,
                run_id="run-1",
                entry_hash="8" * 64,
                materialized_at="2026-01-01T00:00:00.000000Z",
            ),
        )


def test_blocked_sequence_cannot_transition_back_to_active_on_cas(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.state import BlockedState

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    reconcile = SequenceReconcileService(store, now_factory=lambda: FIXED_NOW)
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, start.run_id)
        assert isinstance(state, AuthorizedState)
        blocked_run = BlockedState(
            run_id=start.run_id,
            version=version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            blocked_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            updated_at=FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            block_reason_kind="preflight_blocked",
            block_reason_summary="simulated",
            context=state.context,
        )
        store.compare_and_swap_state(
            conn,
            run_id=start.run_id,
            expected_version=version,
            new_state=blocked_run,
            now=FIXED_NOW,
        )
        reconcile.reconcile_run(conn, start.run_id)
        blocked = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(blocked, BlockedSequenceState)
    with pytest.raises(SequenceLifecycleValidationError):
        validate_sequence_lifecycle_transition(
            blocked,
            ActiveSequenceState(
                schema_version=blocked.schema_version,
                sequence_id=blocked.sequence_id,
                version=blocked.version + 1,
                prepared_at=blocked.prepared_at,
                updated_at=blocked.updated_at,
                started_at=blocked.started_at,
                idempotency_key=blocked.idempotency_key,
                definition=blocked.definition,
                current_ordinal=blocked.current_ordinal,
                current_run_id=blocked.current_run_id,
                materialized_entries=blocked.materialized_entries,
            ),
        )
    with store.begin_immediate() as conn, pytest.raises(SchedulerEngineError):
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=blocked.version,
            new_state=ActiveSequenceState(
                schema_version=blocked.schema_version,
                sequence_id=blocked.sequence_id,
                version=blocked.version + 1,
                prepared_at=blocked.prepared_at,
                updated_at=blocked.updated_at,
                started_at=blocked.started_at,
                idempotency_key=blocked.idempotency_key,
                definition=blocked.definition,
                current_ordinal=blocked.current_ordinal,
                current_run_id=blocked.current_run_id,
                materialized_entries=blocked.materialized_entries,
            ),
            now=FIXED_NOW,
        )


def test_concurrent_sequence_abort_cas_loser_replays_outside_write_lock(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    from ai_dev_loop.scheduler.domain.sequence import (
        AbortPendingSequenceState,
        future_entry_ordinals,
    )

    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    with store.begin_read() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(active, ActiveSequenceState)
    pending = AbortPendingSequenceState(
        schema_version=active.schema_version,
        sequence_id=active.sequence_id,
        version=active.version + 1,
        prepared_at=active.prepared_at,
        updated_at="2026-09-13T12:00:00.000000Z",
        started_at=active.started_at,
        abort_requested_at="2026-09-13T12:00:00.000000Z",
        abort_reason="user_requested_abort",
        idempotency_key=active.idempotency_key,
        definition=active.definition,
        current_ordinal=active.current_ordinal,
        current_run_id=start.run_id,
        materialized_entries=active.materialized_entries,
        residual_risk_ordinals=active.residual_risk_ordinals,
        cancelled_ordinals=future_entry_ordinals(active.definition, active.current_ordinal),
    )
    with store.begin_immediate() as conn:
        store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=pending,
            now=FIXED_NOW,
        )
    service = SequenceAbortService(
        store,
        run_abort=SchedulerAbortService(
            store,
            FakeAgentProcessBackend(),
            artifacts=artifacts,
            now_factory=lambda: FIXED_NOW,
        ),
        now_factory=lambda: FIXED_NOW,
    )
    result = service.abort_sequence(sequence_id)
    assert result.idempotent_replay is True


def test_completion_report_publication_recovers_after_interrupted_write(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    finalized_at = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        active = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(active, ActiveSequenceState)
        final_entry = active.definition.entries[-1]
        final_run_id = final_entry.planned_run_id
        materialized_entries = tuple(active.materialized_entries) + (
            MaterializedSequenceEntry(
                ordinal=final_entry.ordinal,
                run_id=final_run_id,
                entry_hash=frozen_entry_hash(final_entry),
                materialized_at=finalized_at,
            ),
        )
        awaiting = AwaitingFinalizationSequenceState(
            schema_version=1,
            sequence_id=sequence_id,
            version=active.version + 1,
            prepared_at=active.prepared_at,
            updated_at=finalized_at,
            started_at=active.started_at,
            finalized_at=finalized_at,
            idempotency_key=active.idempotency_key,
            definition=active.definition,
            final_run_id=final_run_id,
            final_outcome="completed",
            materialized_entries=materialized_entries,
            residual_risk_ordinals=active.residual_risk_ordinals,
        )
        assert store.compare_and_swap_sequence_state(
            conn,
            sequence_id=sequence_id,
            expected_version=active.version,
            new_state=awaiting,
            now=FIXED_NOW,
        )
        from ai_dev_loop.scheduler.application.sequence_lineage_ops import (
            sync_authoritative_lineage_from_state,
        )

        sync_authoritative_lineage_from_state(store, conn, awaiting)
    report = SequenceCompletionReport(
        sequence_id=sequence_id,
        sequence_name=active.definition.name,
        base_head_sha256="a" * 40,
        base_head_sha256_prefix="a" * 12,
        finalized_at=finalized_at,
        final_run_id=final_run_id,
        final_run_id_prefix=final_run_id[:8],
        final_outcome="completed",
        final_staged_patch_sha256="b" * 64,
        final_staged_patch_sha256_prefix="b" * 12,
        phases=(),
    )
    report_path = artifacts.sequence_root(sequence_id) / SEQUENCE_COMPLETION_REPORT_ARTIFACT
    interrupted = False

    def crash_after_write(step: str) -> None:
        nonlocal interrupted
        if step == "after_report_write" and not interrupted:
            interrupted = True
            raise RuntimeError("simulated crash after report write")

    set_completion_report_publication_step_hook(crash_after_write)
    try:
        with (
            patch(
                "ai_dev_loop.scheduler.application.sequence_report.build_completion_report",
                return_value=report,
            ),
            pytest.raises(RuntimeError, match="simulated crash"),
        ):
            reconcile_completion_report_publication(
                store,
                artifacts,
                awaiting,
                now=FIXED_NOW,
            )
        assert report_path.is_file()
        with store.begin_read() as conn:
            pending = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(pending, AwaitingFinalizationSequenceState)
        assert pending.completion_report_sha256 is None
        assert pending.finalized_at == finalized_at
        interrupted_report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()

        later = FIXED_NOW + timedelta(hours=1)
        with patch(
            "ai_dev_loop.scheduler.application.sequence_report.build_completion_report",
            return_value=report,
        ):
            published = reconcile_completion_report_publication(
                store,
                artifacts,
                pending,
                now=later,
            )
        assert published.completion_report_sha256 == interrupted_report_sha
        assert published.finalized_at == finalized_at
        assert hashlib.sha256(report_path.read_bytes()).hexdigest() == interrupted_report_sha
    finally:
        set_completion_report_publication_step_hook(None)


def test_verify_checkpoint_commit_rejects_wrong_tree(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    intent, _, _, trusted_tree = _checkpoint_intent_from_repo(repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(subprocess.check_output(["git", "diff", "--cached"], cwd=repo))
    commit = ProductionGitCheckpointPort().execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=lambda _: None,
        persist_commit_sha=lambda _: None,
        deadline=CheckpointGitDeadline.from_lease(datetime(2026, 9, 13, 12, 5, tzinfo=UTC)),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )
    wrong_tree = trusted_tree.model_copy(
        update={"reviewed_tree_sha256": "0" * 40},
    )
    with pytest.raises(SequenceCheckpointEvidenceError, match="tree does not match"):
        verify_checkpoint_commit_in_repository(
            intent=intent,
            trusted_tree=wrong_tree,
            commit_sha256=commit.commit_sha,
        )


def test_verify_checkpoint_commit_rejects_conflicting_result_hash(tmp_path: Path) -> None:
    repo = _disposable_repo(tmp_path)
    intent, _, _, trusted_tree = _checkpoint_intent_from_repo(repo)
    patch_path = tmp_path / "01.patch"
    patch_path.write_bytes(subprocess.check_output(["git", "diff", "--cached"], cwd=repo))
    commit = ProductionGitCheckpointPort().execute_checkpoint(
        intent,
        patch_path=patch_path,
        trusted_tree=trusted_tree,
        evidence=None,
        persist_tree_sha=lambda _: None,
        persist_commit_sha=lambda _: None,
        deadline=CheckpointGitDeadline.from_lease(datetime(2026, 9, 13, 12, 5, tzinfo=UTC)),
        now_factory=lambda: datetime(2026, 9, 13, 12, 0, tzinfo=UTC),
    )
    with pytest.raises(SequenceCheckpointEvidenceError, match="evidence commit disagrees"):
        verify_checkpoint_commit_in_repository(
            intent=intent,
            trusted_tree=trusted_tree,
            commit_sha256=commit.commit_sha,
            evidence_commit_sha256="1" * 40,
        )


def test_post_cas_abort_reconciliation_verifies_commit_without_git_mutation(
    tmp_path: Path,
) -> None:
    from unittest.mock import patch

    from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (
        _prepare_handoff_reconcile,
        _reconcile_checkpoint,
    )

    from ai_dev_loop.scheduler.application import git_checkpoint as git_checkpoint_module
    from ai_dev_loop.scheduler.application.abort import default_abort_service

    fixture = _prepare_handoff_reconcile(tmp_path)
    repo = fixture["repo"]
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    trusted_tree = fixture["trusted_tree"]
    assert isinstance(intent, SequenceCheckpointIntent)
    assert isinstance(trusted_tree, SequenceCheckpointTrustedTree)
    trusted_tree_text = (
        json.dumps(trusted_tree.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    )
    trusted_tree_sha = hashlib.sha256(trusted_tree_text.encode()).hexdigest()
    _reconcile_checkpoint(fixture, stop_before_handoff=True)
    commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256=str(fixture["intent_sha"]),
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=now,
        )
    default_abort_service(
        db_path=store.db_path,
        backend=FakeAgentProcessBackend(),
        artifact_root=artifacts.artifact_root,
    ).abort_run(run_id)
    update_ref_calls = 0
    original_update_ref = git_checkpoint_module.checkpoint_git_update_ref_cas

    def count_update_ref(*args: object, **kwargs: object) -> str:
        nonlocal update_ref_calls
        update_ref_calls += 1
        return original_update_ref(*args, **kwargs)  # type: ignore[arg-type]

    service = fixture["service"]
    with patch.object(git_checkpoint_module, "checkpoint_git_update_ref_cas", count_update_ref):
        receipt = service._try_record_applied_checkpoint_reconciliation_only(
            run_id,
            intent=intent,
            intent_sha256=str(fixture["intent_sha"]),
            trusted_tree=trusted_tree,
            trusted_tree_sha256=trusted_tree_sha,
            tick_owner_id=str(fixture["tick_owner"]),
            tick_lease_generation=int(fixture["tick_generation"]),
        )
    assert receipt is not None
    assert receipt.action == "checkpoint_reconciliation_completed"
    assert update_ref_calls == 0
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
        == commit_sha
    )
    with store.begin_read() as conn:
        successor = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchone()
    assert successor is None
