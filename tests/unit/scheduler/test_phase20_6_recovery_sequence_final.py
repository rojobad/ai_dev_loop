"""Sequence final recovery integration state tests for Phase 20.6."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.sequence import (
    RECOVERY_INTEGRATED_FINALIZATION_SEQUENCE_STATE_KIND,
    BlockedSequenceState,
    PreparedSequenceDefinition,
    RecoveryIntegratedFinalizationSequenceState,
)
from ai_dev_loop.scheduler.domain.sequence_lifecycle_validation import (
    validate_sequence_lifecycle_transition,
)


def test_blocked_sequence_may_finalize_via_recovery_integrated_state() -> None:
    definition = PreparedSequenceDefinition.model_construct(
        sequence_id="seq-test",
        entries=(1, 2),
    )
    blocked = BlockedSequenceState.model_construct(
        current_ordinal=2,
        current_run_id="run-source",
        definition=definition,
    )
    finalized = RecoveryIntegratedFinalizationSequenceState.model_construct(
        source_run_id="run-source",
        recovery_id="rcv-test",
        recovery_run_id="run-recovery",
    )
    validate_sequence_lifecycle_transition(blocked, finalized)
    assert finalized.source_run_id == "run-source"
    assert finalized.recovery_run_id == "run-recovery"
    assert RECOVERY_INTEGRATED_FINALIZATION_SEQUENCE_STATE_KIND == (
        "recovery_integrated_finalization"
    )
