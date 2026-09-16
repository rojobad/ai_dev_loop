"""Sequence handoff tests for Phase 20.6.5 authenticated rollover."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
from tests.unit.scheduler.rollover_test_helpers import maxed_rollover_fixture
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_NOW
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)

from ai_dev_loop.scheduler.application.rollover_start import (
    _authorize_sequence_block_for_rollover,
)
from ai_dev_loop.scheduler.domain.rollover import (
    ROLLOVER_SEQUENCE_BLOCK_REASON,
    SequenceRolloverResolution,
)
from ai_dev_loop.scheduler.domain.sequence import (
    BlockedSequenceState,
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


def test_authorize_sequence_block_transitions_active_to_blocked(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        _authorize_sequence_block_for_rollover(
            store,
            conn,
            sequence_id=sequence_id,
            source_run_id=start.run_id,
            now=FIXED_NOW,
            event_id_factory=lambda: "evt-seq-block",
        )
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, BlockedSequenceState)
    assert sequence.block_reason_kind == ROLLOVER_SEQUENCE_BLOCK_REASON
    assert sequence.current_run_id == start.run_id


def test_authorize_sequence_block_is_idempotent_for_already_blocked(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    sequence_id = _prepare_sequence(git_repo, scheduler_paths)
    start = _start_service(scheduler_paths).start(sequence_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_immediate() as conn:
        _authorize_sequence_block_for_rollover(
            store,
            conn,
            sequence_id=sequence_id,
            source_run_id=start.run_id,
            now=FIXED_NOW,
            event_id_factory=lambda: "evt-seq-block-1",
        )
        before = store.load_validated_sequence_state(conn, sequence_id)
        _authorize_sequence_block_for_rollover(
            store,
            conn,
            sequence_id=sequence_id,
            source_run_id=start.run_id,
            now=FIXED_NOW,
            event_id_factory=lambda: "evt-seq-block-2",
        )
        after = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(before, BlockedSequenceState)
    assert isinstance(after, BlockedSequenceState)
    assert after.version == before.version


def test_rollover_start_blocks_sequence_for_maxed_sequence_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.rollover_prepare import RolloverPrepareService
    from ai_dev_loop.scheduler.application.rollover_start import RolloverStartService
    from ai_dev_loop.scheduler.application.rollover_worktree import FakeRolloverWorktreePort
    from ai_dev_loop.scheduler.domain.state import MaxIterationsReachedState

    source_run_id, store, artifacts, _ = maxed_rollover_fixture(
        git_repo,
        scheduler_paths,
        monkeypatch,
    )
    with store.begin_read() as conn:
        source_state, _, _ = store.load_validated_snapshot(conn, source_run_id)
    assert isinstance(source_state, MaxIterationsReachedState)
    if source_state.context.sequence is None:
        pytest.skip("standalone maxed fixture lacks sequence binding")
    sequence_id = source_state.context.sequence.sequence_id
    prepared = RolloverPrepareService(store, artifacts).prepare(
        source_run_id,
        commit_message="sequence rollover commit",
    )
    RolloverStartService(
        store,
        artifacts,
        worktree_port=FakeRolloverWorktreePort(),
    ).start(prepared.rollover_id)
    with store.begin_read() as conn:
        sequence = store.load_validated_sequence_state(conn, sequence_id)
    assert isinstance(sequence, BlockedSequenceState)


def test_complete_sequence_rollover_replay_is_idempotent_non_final(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from ai_dev_loop.scheduler.application.rollover_integration import RolloverIntegrationService
    from ai_dev_loop.scheduler.domain.rollover import (
        AuthenticatedRolloverDefinition,
        RolloverCheckpointTrustedTree,
        RolloverSequenceBinding,
    )
    from ai_dev_loop.scheduler.domain.state import RepositoryBinding

    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sequence_id = "seq-rollover-nonfinal"
    source_run_id = "run-seq-rollover-source"
    worktree_key = "a" * 64
    resolution = SequenceRolloverResolution(
        sequence_id=sequence_id,
        ordinal=1,
        source_run_id=source_run_id,
        rollover_id="rol-seq-nonfinal",
        rollover_run_id="run-rollover-seq",
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="b" * 64,
        reviewed_tree_sha256="c" * 40,
        commit_sha256="d" * 40,
        recorded_at=now_text,
    )
    trusted = RolloverCheckpointTrustedTree(
        intent_sha256="e" * 64,
        reviewed_tree_sha256="c" * 40,
        reviewed_patch_sha256="b" * 64,
        parent_head="f" * 40,
        recorded_at=now_text,
    )
    rollover_definition = AuthenticatedRolloverDefinition.model_construct(
        source_run_id=source_run_id,
        sequence=RolloverSequenceBinding(
            sequence_id=sequence_id,
            ordinal=1,
            total_phases=2,
            is_final_phase=False,
        ),
        repository=RepositoryBinding.model_construct(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
            initial_head="f" * 40,
            worktree_key=worktree_key,
        ),
    )
    service = RolloverIntegrationService(store, artifacts, now_factory=lambda: now)
    cas_calls = 0

    def fake_cas(*args: object, **kwargs: object) -> bool:
        nonlocal cas_calls
        cas_calls += 1
        return True

    monkeypatch.setattr(
        store,
        "get_sequence_rollover_resolution",
        lambda conn, **kwargs: resolution,
    )
    monkeypatch.setattr(
        service,
        "_sequence_rollover_effects_complete",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", fake_cas)
    with store.begin_immediate() as conn:
        service._complete_sequence_recovery(
            conn,
            definition=rollover_definition,
            resolution=resolution,
            commit_sha="d" * 40,
            trusted=trusted,
            now=now,
        )
    assert cas_calls == 0


def test_complete_sequence_rollover_replay_is_idempotent_final(
    isolated_xdg: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime

    from ai_dev_loop.scheduler.application.rollover_integration import RolloverIntegrationService
    from ai_dev_loop.scheduler.domain.rollover import (
        AuthenticatedRolloverDefinition,
        RolloverCheckpointTrustedTree,
        RolloverSequenceBinding,
    )
    from ai_dev_loop.scheduler.domain.state import RepositoryBinding

    store = SqliteSchedulerStore(isolated_xdg / "state" / "ai_dev_loop" / "engine.sqlite3")
    artifacts = ProtectedArtifactStore(isolated_xdg / "state" / "ai_dev_loop" / "artifacts")
    now = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    now_text = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    sequence_id = "seq-rollover-final"
    worktree_key = "b" * 64
    resolution = SequenceRolloverResolution(
        sequence_id=sequence_id,
        ordinal=2,
        source_run_id="run-seq-final-source",
        rollover_id="rol-seq-final",
        rollover_run_id="run-rollover-final",
        accepted_outcome="completed",
        residual_risk=False,
        reviewed_patch_sha256="1" * 64,
        reviewed_tree_sha256="2" * 40,
        commit_sha256="3" * 40,
        recorded_at=now_text,
    )
    trusted = RolloverCheckpointTrustedTree(
        intent_sha256="4" * 64,
        reviewed_tree_sha256="2" * 40,
        reviewed_patch_sha256="1" * 64,
        parent_head="5" * 40,
        recorded_at=now_text,
    )
    rollover_definition = AuthenticatedRolloverDefinition.model_construct(
        source_run_id="run-seq-final-source",
        sequence=RolloverSequenceBinding(
            sequence_id=sequence_id,
            ordinal=2,
            total_phases=2,
            is_final_phase=True,
        ),
        repository=RepositoryBinding.model_construct(
            root="/tmp/repo",
            git_common_dir="/tmp/repo/.git",
            git_dir="/tmp/repo/.git",
            branch="main",
            initial_head="5" * 40,
            worktree_key=worktree_key,
        ),
    )
    service = RolloverIntegrationService(store, artifacts, now_factory=lambda: now)
    cas_calls = 0
    release_calls = 0

    def fake_cas(*args: object, **kwargs: object) -> bool:
        nonlocal cas_calls
        cas_calls += 1
        return True

    def fake_release(conn: object, *, worktree_key: str, now: object) -> None:
        nonlocal release_calls
        release_calls += 1

    monkeypatch.setattr(
        store,
        "get_sequence_rollover_resolution",
        lambda conn, **kwargs: resolution,
    )
    monkeypatch.setattr(
        service,
        "_sequence_rollover_effects_complete",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(store, "compare_and_swap_sequence_state", fake_cas)
    monkeypatch.setattr(store, "release_reservation", fake_release)
    with store.begin_immediate() as conn:
        service._complete_sequence_recovery(
            conn,
            definition=rollover_definition,
            resolution=resolution,
            commit_sha="3" * 40,
            trusted=trusted,
            now=now,
        )
    assert cas_calls == 0
    assert release_calls == 0
