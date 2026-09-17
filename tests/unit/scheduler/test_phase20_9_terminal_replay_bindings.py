"""Terminal checkpoint replay authentication and hold retention (Phase 20.9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.helpers import sample_submitted_state
from tests.unit.scheduler.test_phase20_3_checkpoint_concurrency import (
    _active_sequence_for_intent,
    _disposable_repo,
    _frozen_sequence_entry,
    _persist_active_sequence_for_fixture,
    _prepare_handoff_reconcile,
    _promote_run_to_terminal_completed,
    _reconcile_checkpoint,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.application.sequence_restart_reconcile import (
    SequenceRestartReconcileService,
)
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.checkpoint import (
    SEQUENCE_CHECKPOINT_RESULT_ARTIFACT,
    SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT,
    SequenceCheckpointIntent,
    SequenceCheckpointResult,
)
from ai_dev_loop.scheduler.domain.common import worktree_key
from ai_dev_loop.scheduler.domain.events import RunCompletedEvent
from ai_dev_loop.scheduler.domain.reducer import apply_run_completed
from ai_dev_loop.scheduler.domain.sequence import (
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    ControllerBinding,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
    RepositoryTargetBinding,
)
from ai_dev_loop.scheduler.domain.state import CheckpointPendingState, CompletedState
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

FIXED_NOW = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


def _completed_leaf_for_terminal_replay(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    intent_sha: str,
) -> CompletedState:
    now_text = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, CheckpointPendingState)
        completed = apply_run_completed(
            state,
            RunCompletedEvent(run_id=run_id, review_iteration=state.codex.review_iteration),
            now_text=now_text,
        )
        assert isinstance(completed, CompletedState)
        assert store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=completed,
            now=FIXED_NOW,
        )
        store.acquire_checkpoint_reconciliation_hold(
            conn,
            run_id=run_id,
            intent_sha256=intent_sha,
            hold_reason="checkpoint_reconciliation",
            ref_may_have_advanced=True,
            now=FIXED_NOW,
        )
    return completed


def _terminal_replay_control_fixture(tmp_path: Path) -> dict[str, object]:
    fixture = _prepare_handoff_reconcile(tmp_path)
    _reconcile_checkpoint(fixture, stop_before_handoff=True)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    run_id = str(fixture["run_id"])
    intent_sha = str(fixture["intent_sha"])
    _completed_leaf_for_terminal_replay(store, run_id=run_id, intent_sha=intent_sha)
    return fixture


def _persist_invalid_terminal_fixture_sequence(fixture: dict[str, object]) -> None:
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    active_sequence = fixture["active_sequence"]
    assert isinstance(active_sequence, ActiveSequenceState)
    _persist_active_sequence_for_fixture(store, active_sequence, now=FIXED_NOW)
    original_load = fixture["load_sequence_state_original"]
    store.load_validated_sequence_state = original_load  # type: ignore[method-assign]


def _install_finalize_ready_peer_sequence(
    fixture: dict[str, object],
    tmp_path: Path,
    *,
    peer_sequence_id: str = "seq-peer-finalize",
    peer_run_id: str = "peer-finalize-run-01",
) -> tuple[str, str]:
    from tests.unit.scheduler.helpers import CONTROLLER_SESSION, DIGEST

    from ai_dev_loop.scheduler.application.sequence_materializer import frozen_entry_hash

    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    peer_parent = tmp_path / "peer-parent"
    peer_parent.mkdir(parents=True, exist_ok=True)
    peer_repo = _disposable_repo(peer_parent)
    import secrets

    peer_phase_one_run_id = f"{peer_run_id}-phase-one"
    peer_state = sample_submitted_state(run_id=peer_run_id, repo_root=str(peer_repo.resolve()))
    peer_state = peer_state.model_copy(
        update={
            "idempotency_key": secrets.token_hex(32),
            "context": peer_state.context.model_copy(
                update={
                    "workflow": peer_state.context.workflow.model_copy(
                        update={"require_clean_worktree": False}
                    )
                }
            ),
        }
    )
    from ai_dev_loop.scheduler.application.start import StartService
    from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent

    phase_one_state = peer_state.model_copy(
        update={
            "run_id": peer_phase_one_run_id,
            "idempotency_key": secrets.token_hex(32),
        }
    )
    phase_one_event = RunSubmittedEvent(
        run_id=peer_phase_one_run_id,
        idempotency_key=phase_one_state.idempotency_key,
        worktree_key=phase_one_state.context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=peer_phase_one_run_id,
            state=phase_one_state,
            event_id=f"evt-submit-{peer_phase_one_run_id}",
            event=phase_one_event,
            now=FIXED_NOW,
        )
    StartService(store, now_factory=lambda: FIXED_NOW).start(peer_phase_one_run_id)
    peer_worktree_key = worktree_key(str(peer_repo.resolve()))
    _promote_run_to_terminal_completed(store, run_id=peer_phase_one_run_id, now=FIXED_NOW)
    with store.begin_immediate() as conn:
        store.release_reservation(conn, worktree_key=peer_worktree_key, now=FIXED_NOW)
    submit_event = RunSubmittedEvent(
        run_id=peer_run_id,
        idempotency_key=peer_state.idempotency_key,
        worktree_key=peer_state.context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=peer_run_id,
            state=peer_state,
            event_id=f"evt-submit-{peer_run_id}",
            event=submit_event,
            now=FIXED_NOW,
        )
    StartService(store, now_factory=lambda: FIXED_NOW).start(peer_run_id)
    _promote_run_to_terminal_completed(store, run_id=peer_run_id, now=FIXED_NOW)
    now_text = FIXED_NOW.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    first_entry = _frozen_sequence_entry(
        ordinal=1,
        phase_name="peer-phase-one",
        planned_run_id=peer_phase_one_run_id,
        commit_message="peer phase one",
    )
    second_entry = _frozen_sequence_entry(
        ordinal=2,
        phase_name="peer-phase-two",
        planned_run_id=peer_run_id,
        commit_message=None,
    )
    definition = PreparedSequenceDefinition(
        sequence_id=peer_sequence_id,
        name="peer-finalize",
        project_name="fixture-project",
        repository=RepositoryTargetBinding(
            root=str(peer_repo.resolve()),
            worktree_key=worktree_key(str(peer_repo.resolve())),
        ),
        manifest_original_artifact_path="sequence/manifest.original.yaml",
        manifest_original_sha256=DIGEST,
        manifest_resolved_artifact_path="sequence/manifest.resolved.yaml",
        manifest_resolved_sha256=DIGEST,
        controller=ControllerBinding(controller_session_id=CONTROLLER_SESSION),
        entries=(first_entry, second_entry),
    )
    peer_active = ActiveSequenceState(
        schema_version=1,
        sequence_id=peer_sequence_id,
        version=1,
        prepared_at=now_text,
        updated_at=now_text,
        started_at=now_text,
        idempotency_key=secrets.token_hex(32),
        definition=definition,
        current_ordinal=2,
        current_run_id=peer_run_id,
        materialized_entries=(
            MaterializedSequenceEntry(
                ordinal=1,
                run_id=peer_phase_one_run_id,
                entry_hash=frozen_entry_hash(first_entry),
                materialized_at=now_text,
            ),
            MaterializedSequenceEntry(
                ordinal=2,
                run_id=peer_run_id,
                entry_hash=frozen_entry_hash(second_entry),
                materialized_at=now_text,
            ),
        ),
        residual_risk_ordinals=(),
    )
    _persist_active_sequence_for_fixture(store, peer_active, now=FIXED_NOW)
    return peer_sequence_id, peer_run_id


def test_terminal_replay_authentication_succeeds_for_control_fixture(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    run_id = str(fixture["run_id"])
    intent = SequenceCheckpointIntent.model_validate_json(
        (artifacts.run_root(run_id) / "sequence-checkpoints/intent.json").read_bytes()
    )
    result = SequenceCheckpointResult.model_validate_json(
        (artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT).read_bytes()
    )
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        assert isinstance(sequence_state, ActiveSequenceState)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        auth_failure = service._authenticate_terminal_checkpoint_for_replay(
            conn,
            run_id=run_id,
            sequence_state=sequence_state,
            run_state=run_state,
            intent=intent,
            result=result,
        )
        assert auth_failure is None


def test_missing_staged_patch_invalidates_replay_and_retains_hold(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    patch_path = artifacts.run_root(run_id) / intent.staged_patch_artifact_path
    patch_path.unlink()
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        assert isinstance(sequence_state, ActiveSequenceState)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert (
            conn.execute(
                "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
                (intent.successor_run_id,),
            ).fetchone()
            is None
        )


def test_modified_review_result_invalidates_replay_and_retains_hold(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    review_path = artifacts.run_root(run_id) / intent.review_result_artifact_path
    review_path.write_bytes(b"{}\n")
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        assert isinstance(sequence_state, ActiveSequenceState)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert (
            conn.execute(
                "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
                (intent.successor_run_id,),
            ).fetchone()
            is None
        )


def _tick_service_for_terminal_fixture(fixture: dict[str, object]) -> TickService:
    store = fixture["store"]
    artifacts = fixture["artifacts"]
    repo = fixture["repo"]
    assert isinstance(store, SqliteSchedulerStore)
    from pathlib import Path as PathType

    assert isinstance(repo, PathType)
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(repo.resolve())),
        now_factory=lambda: FIXED_NOW,
        tick_owner_factory=lambda: "tick-phase20-9-terminal",
        attempt_id_factory=lambda: "att-phase20-9-terminal-replay",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )


def test_malformed_checkpoint_intent_json_invalidates_reconcile(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    run_id = str(fixture["run_id"])
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    intent_path = artifacts.run_root(run_id) / "sequence-checkpoints/intent.json"
    intent_path.write_text("{ not-valid-json", encoding="utf-8")
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert (
            conn.execute(
                "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
                (intent.successor_run_id,),
            ).fetchone()
            is None
        )


def test_malformed_trusted_tree_json_invalidates_hold_path_reconcile(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    run_id = str(fixture["run_id"])
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    tree_path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
    tree_path.write_text("{ not-valid-json", encoding="utf-8")
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None


def _stub_completion_report_for_finalize_peer(
    _store: object,
    _artifacts: object,
    state: object,
    conn: object | None = None,
) -> object:
    from ai_dev_loop.scheduler.domain.sequence import (
        AwaitingFinalizationSequenceState,
        SequenceCompletionReport,
        SequencePhaseReportEntry,
    )

    assert isinstance(state, AwaitingFinalizationSequenceState)
    phases = tuple(
        SequencePhaseReportEntry(
            ordinal=entry.ordinal,
            phase_name=entry.phase_name,
            run_id=materialized.run_id,
            run_id_prefix=materialized.run_id[:12],
            accepted_outcome="completed",
        )
        for entry in state.definition.entries
        for materialized in state.materialized_entries
        if materialized.ordinal == entry.ordinal
    )
    return SequenceCompletionReport(
        sequence_id=state.sequence_id,
        sequence_name=state.definition.name,
        finalized_at=state.finalized_at,
        final_run_id=state.final_run_id,
        final_run_id_prefix=state.final_run_id[:12],
        base_head_sha256_prefix="0" * 12,
        final_outcome=state.final_outcome,
        phases=phases,
    )


def test_malformed_trusted_tree_tick_advances_unrelated_terminal_via_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    run_id = str(fixture["run_id"])
    _persist_invalid_terminal_fixture_sequence(fixture)
    peer_sequence_id, peer_run_id = _install_finalize_ready_peer_sequence(fixture, tmp_path)
    tree_path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
    tree_path.write_text("{ not-valid-json", encoding="utf-8")
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.application.sequence_report.build_completion_report",
        _stub_completion_report_for_finalize_peer,
    )
    tick = _tick_service_for_terminal_fixture(fixture)
    receipt = tick.run_once()
    assert receipt.lease_acquired
    assert any(
        item.run_id == run_id and item.action == "checkpoint_result_invalid"
        for item in receipt.run_receipts
    )
    assert any(
        item.run_id == peer_run_id and item.action == "sequence_finalization_reconciled"
        for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        assert store.get_reservation_for_run(conn, run_id) is not None
        assert (
            conn.execute(
                "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
                (intent.successor_run_id,),
            ).fetchone()
            is None
        )
        peer_sequence = store.load_validated_sequence_state(conn, peer_sequence_id)
    assert isinstance(peer_sequence, AwaitingFinalizationSequenceState)
    assert peer_sequence.final_run_id == peer_run_id


def test_tick_run_once_invokes_restart_reconcile_work(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    tick = _tick_service_for_terminal_fixture(fixture)
    invoked = False
    original = SequenceRestartReconcileService.reconcile_pending_restart_work

    def _record_invoke(self, conn):  # type: ignore[no-untyped-def]
        nonlocal invoked
        invoked = True
        return original(self, conn)

    SequenceRestartReconcileService.reconcile_pending_restart_work = _record_invoke  # type: ignore[method-assign]
    try:
        receipt = tick.run_once()
    finally:
        SequenceRestartReconcileService.reconcile_pending_restart_work = original  # type: ignore[method-assign]
    assert receipt.lease_acquired
    assert invoked


def test_malformed_checkpoint_result_json_invalidates_reconcile(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    result_path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
    result_path.write_text("[]", encoding="utf-8")
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"


def test_modified_trusted_tree_via_tick_restart_reconcile(tmp_path: Path) -> None:
    from ai_dev_loop.scheduler.domain.checkpoint import SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT

    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    run_id = str(fixture["run_id"])
    repo = fixture["repo"]
    assert isinstance(repo, Path)
    tree_path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_TRUSTED_TREE_ARTIFACT
    tree_path.write_bytes(tree_path.read_bytes() + b" ")
    active_sequence = _active_sequence_for_intent(intent, repo_root=str(repo.resolve()))
    original_load = store.load_validated_sequence_state

    def patched_load(conn, sequence_id: str):  # type: ignore[no-untyped-def]
        if sequence_id == intent.sequence_id:
            return active_sequence
        return original_load(conn, sequence_id)

    store.load_validated_sequence_state = patched_load  # type: ignore[method-assign]
    handoff = fixture["service"]
    assert isinstance(handoff, SequenceHandoffService)
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = handoff.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)


def test_terminal_replay_tree_binding_failure_retains_checkpoint_hold(tmp_path: Path) -> None:
    fixture = _terminal_replay_control_fixture(tmp_path)
    store = fixture["store"]
    assert isinstance(store, SqliteSchedulerStore)
    artifacts = fixture["artifacts"]
    run_id = str(fixture["run_id"])
    intent = fixture["intent"]
    assert isinstance(intent, SequenceCheckpointIntent)
    service = fixture["service"]
    assert isinstance(service, SequenceHandoffService)
    result_path = artifacts.run_root(run_id) / SEQUENCE_CHECKPOINT_RESULT_ARTIFACT
    assert result_path.is_file()
    result = SequenceCheckpointResult.model_validate_json(result_path.read_bytes())
    wrong_tree = ("0" * 39) + ("f" if result.tree_sha256[-1] != "f" else "e")
    tampered = result.model_copy(update={"tree_sha256": wrong_tree})
    result_path.write_text(
        json.dumps(tampered.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with store.begin_immediate() as conn:
        sequence_state = store.load_validated_sequence_state(conn, intent.sequence_id)
        assert isinstance(sequence_state, ActiveSequenceState)
        run_state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert isinstance(run_state, CompletedState)
        receipt = service.reconcile_interrupted_sequence_advancement(
            conn,
            sequence_state=sequence_state,
            run_state=run_state,
            now=FIXED_NOW,
        )
        assert receipt.action == "checkpoint_result_invalid"
        assert store.has_checkpoint_reconciliation_hold(conn, run_id)
        successor_exists = conn.execute(
            "SELECT 1 FROM scheduler_runs WHERE run_id = ?",
            (intent.successor_run_id,),
        ).fetchone()
        assert successor_exists is None


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_finalization_savepoint_rolls_back_failed_reservation_release(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.integration.test_phase20_3_sequence_handoff import (
        BOOTSTRAP_ID,
        _run_until,
        _tick_service,
    )
    from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_RUN_IDS
    from tests.unit.scheduler.test_phase20_2_sequence_start import _prepare_sequence

    from ai_dev_loop.scheduler.application.sequence_restart_reconcile import (
        SequenceRestartReconcileService,
    )
    from ai_dev_loop.scheduler.application.sequence_start import start_sequence
    from ai_dev_loop.scheduler.domain.sequence import AwaitingFinalizationSequenceState
    from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore

    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    finalize_calls = 0
    original_finalize = SqliteSchedulerStore.finalize_sequence_state

    def flaky_finalize(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal finalize_calls
        finalize_calls += 1
        if finalize_calls == 1:
            return None
        return original_finalize(self, conn, **kwargs)

    monkeypatch.setattr(SqliteSchedulerStore, "finalize_sequence_state", flaky_finalize)
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = start_sequence(sequence_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(git_repo, scheduler_paths)
    _run_until(tick, start.run_id, target_kind="completed")
    _run_until(tick, FIXED_RUN_IDS[1], target_kind="completed")
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    handoff = SequenceHandoffService(store, artifacts, now_factory=lambda: FIXED_NOW)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, ActiveSequenceState)
    final_run_id = sequence.current_run_id
    release_calls = 0
    original_release = SqliteSchedulerStore.release_reservation

    def flaky_release(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal release_calls
        release_calls += 1
        if release_calls == 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "simulated reservation release failure",
            )
        return original_release(self, conn, **kwargs)

    monkeypatch.setattr(SqliteSchedulerStore, "release_reservation", flaky_release)
    service = SequenceRestartReconcileService(
        store, artifacts, handoff=handoff, now_factory=lambda: FIXED_NOW
    )
    with store.begin_immediate() as conn:
        receipt = service.reconcile_terminal_current_leaf(conn, final_run_id)
    assert receipt is not None
    assert receipt.action == "sequence_finalization_cas_lost"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, ActiveSequenceState)
    with store.begin_immediate() as conn:
        receipt = service.reconcile_terminal_current_leaf(conn, final_run_id)
    assert receipt is not None
    assert receipt.action == "sequence_finalization_reconciled"
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence, AwaitingFinalizationSequenceState)
        assert store.get_reservation_for_run(conn, final_run_id) is None
