"""Checkpoint successor replacement chain authentication (Phase 20.9)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase20_3_sequence_handoff import (
    BOOTSTRAP_ID,
    THREE_PHASE_RUN_IDS,
    _prepare_three_phase,
    _run_until,
)
from tests.unit.scheduler.test_phase20_1_sequence_prepare import FIXED_SEQUENCE_ID
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _legacy_block_instead_of_retry,
    _run_until_blocked_sequence,
)
from tests.unit.scheduler.test_phase20_8_sequence_review_retry import (
    _tick_service as _tick_service_phase20_8,
)

from ai_dev_loop.errors import ValidationError
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.sequence_handoff import SequenceHandoffService
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sequence_run_lineage_store import (
    authenticate_checkpoint_successor_replacement_chain,
)


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_checkpoint_successor_chain_accepts_same_reviewer_retry_replacement(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    _prepare_three_phase(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service_phase20_8(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    service = ReviewRetryService(tick.store, tick.artifacts)
    source_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_one = service.retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_one, target_kind="completed")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    blocked_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_two = service.retry(blocked_run_id).run_id
    with tick.store.begin_read() as conn:
        sequence = tick.store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        assert isinstance(sequence, ActiveSequenceState)
        authenticate_checkpoint_successor_replacement_chain(
            conn,
            sequence_id=FIXED_SEQUENCE_ID,
            ordinal=2,
            recorded_successor_run_id=THREE_PHASE_RUN_IDS[1],
            current_leaf_run_id=successor_two,
        )


def test_checkpoint_successor_chain_rejects_unrelated_leaf(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    _prepare_three_phase(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service_phase20_8(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    service = ReviewRetryService(tick.store, tick.artifacts)
    source_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_one = service.retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_one, target_kind="completed")
    with (
        tick.store.begin_read() as conn,
        pytest.raises(
            ValidationError,
            match="checkpoint successor replacement chain does not reach current leaf",
        ),
    ):
        authenticate_checkpoint_successor_replacement_chain(
            conn,
            sequence_id=FIXED_SEQUENCE_ID,
            ordinal=2,
            recorded_successor_run_id=THREE_PHASE_RUN_IDS[1],
            current_leaf_run_id="fixture-project-unrelated-run-00000001",
        )


def test_authenticate_predecessor_checkpoint_rejects_broken_chain_on_phase_two(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    _prepare_three_phase(git_repo, scheduler_paths)
    start_sequence(FIXED_SEQUENCE_ID, db_path=scheduler_paths["db_path"])
    tick = _tick_service_phase20_8(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 17, 14, 0, tzinfo=UTC),
    )
    _legacy_block_instead_of_retry(tick, monkeypatch)
    service = ReviewRetryService(tick.store, tick.artifacts)
    source_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_one = service.retry(source_run_id).run_id
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    _run_until(tick, successor_one, target_kind="completed")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
    blocked_run_id, _ = _run_until_blocked_sequence(tick, FIXED_SEQUENCE_ID)
    successor_two = service.retry(blocked_run_id).run_id
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    handoff = SequenceHandoffService(store, artifacts)
    with store.begin_immediate() as conn:
        sequence = store.load_validated_sequence_state(conn, FIXED_SEQUENCE_ID)
        assert isinstance(sequence, ActiveSequenceState)
        assert sequence.current_run_id == successor_two
        conn.execute(
            """
            UPDATE scheduler_sequence_run_attempts
            SET run_id = ?
            WHERE sequence_id = ? AND ordinal = 2 AND generation = 2
            """,
            ("fixture-project-tampered-successor-0001", FIXED_SEQUENCE_ID),
        )
        with pytest.raises(ValidationError, match="replacement chain does not reach"):
            handoff._authenticate_predecessor_checkpoint_result(
                conn,
                prev_run_id=successor_one,
                sequence_state=sequence,
                current_ordinal=2,
            )
