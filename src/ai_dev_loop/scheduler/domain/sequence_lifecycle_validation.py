"""Shared invariant validation for abort-pending, blocked, and aborted sequence states."""

from __future__ import annotations

from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    RecoveryIntegratedFinalizationSequenceState,
    future_entry_ordinals,
)


class SequenceLifecycleValidationError(ValueError):
    """Raised when a sequence lifecycle projection violates invariants."""


def _validate_materialized_projection(
    *,
    definition_entry_count: int,
    current_ordinal: int,
    current_run_id: str,
    materialized_entries: tuple[MaterializedSequenceEntry, ...],
) -> None:
    if current_ordinal < 1 or current_ordinal > definition_entry_count:
        raise SequenceLifecycleValidationError("current_ordinal out of range")
    ordinals = [entry.ordinal for entry in materialized_entries]
    if len(set(ordinals)) != len(ordinals):
        raise SequenceLifecycleValidationError("materialized entry ordinals must be unique")
    if any(entry.ordinal > current_ordinal for entry in materialized_entries):
        raise SequenceLifecycleValidationError(
            "materialized_entries must not include future ordinals"
        )
    if sorted(ordinals) != list(range(1, len(materialized_entries) + 1)):
        raise SequenceLifecycleValidationError(
            "materialized_entries must be contiguous from ordinal 1"
        )
    current_entry = next(
        (entry for entry in materialized_entries if entry.ordinal == current_ordinal),
        None,
    )
    if current_entry is None:
        raise SequenceLifecycleValidationError("materialized_entries must include current_ordinal")
    if current_entry.run_id != current_run_id:
        raise SequenceLifecycleValidationError("current_run_id disagrees with materialized entry")


def _validate_residual_ordinals(
    *,
    definition_entry_count: int,
    residual_risk_ordinals: tuple[int, ...],
) -> None:
    for ordinal in residual_risk_ordinals:
        if ordinal < 1 or ordinal > definition_entry_count:
            raise SequenceLifecycleValidationError("residual_risk ordinal out of range")


def validate_abort_pending_state(state: AbortPendingSequenceState) -> None:
    total = len(state.definition.entries)
    _validate_materialized_projection(
        definition_entry_count=total,
        current_ordinal=state.current_ordinal,
        current_run_id=state.current_run_id,
        materialized_entries=state.materialized_entries,
    )
    _validate_residual_ordinals(
        definition_entry_count=total,
        residual_risk_ordinals=state.residual_risk_ordinals,
    )
    expected_cancelled = future_entry_ordinals(state.definition, state.current_ordinal)
    if state.cancelled_ordinals != expected_cancelled:
        raise SequenceLifecycleValidationError(
            "cancelled_ordinals must match future entry ordinals"
        )


def validate_blocked_state(state: BlockedSequenceState) -> None:
    total = len(state.definition.entries)
    _validate_materialized_projection(
        definition_entry_count=total,
        current_ordinal=state.current_ordinal,
        current_run_id=state.current_run_id,
        materialized_entries=state.materialized_entries,
    )
    _validate_residual_ordinals(
        definition_entry_count=total,
        residual_risk_ordinals=state.residual_risk_ordinals,
    )


def validate_aborted_state(state: AbortedSequenceState) -> None:
    total = len(state.definition.entries)
    _validate_residual_ordinals(
        definition_entry_count=total,
        residual_risk_ordinals=state.residual_risk_ordinals,
    )
    if state.started_at is None:
        if state.current_ordinal is not None or state.current_run_id is not None:
            raise SequenceLifecycleValidationError(
                "prepared abort must not retain current run pointers"
            )
        if state.materialized_entries:
            raise SequenceLifecycleValidationError(
                "prepared abort must not retain materialized entries"
            )
        if len(state.cancelled_ordinals) != total:
            raise SequenceLifecycleValidationError("prepared abort must cancel all entries")
        return
    if state.current_ordinal is None or state.current_run_id is None:
        raise SequenceLifecycleValidationError("started abort must retain current run pointers")
    _validate_materialized_projection(
        definition_entry_count=total,
        current_ordinal=state.current_ordinal,
        current_run_id=state.current_run_id,
        materialized_entries=state.materialized_entries,
    )
    expected_cancelled = future_entry_ordinals(state.definition, state.current_ordinal)
    if state.cancelled_ordinals != expected_cancelled:
        raise SequenceLifecycleValidationError(
            "cancelled_ordinals must match future entry ordinals"
        )


def validate_sequence_lifecycle_state(
    state: AbortPendingSequenceState | BlockedSequenceState | AbortedSequenceState,
) -> None:
    if isinstance(state, AbortPendingSequenceState):
        validate_abort_pending_state(state)
    elif isinstance(state, BlockedSequenceState):
        validate_blocked_state(state)
    elif isinstance(state, AbortedSequenceState):
        validate_aborted_state(state)


def validate_sequence_lifecycle_transition(
    current: object,
    new: object,
) -> None:
    if isinstance(new, ActiveSequenceState) and isinstance(current, BlockedSequenceState):
        if new.current_ordinal != current.current_ordinal + 1:
            raise SequenceLifecycleValidationError(
                "recovery continuation must advance exactly one ordinal"
            )
        return
    if isinstance(new, AwaitingFinalizationSequenceState) and isinstance(
        current, BlockedSequenceState
    ):
        if current.current_ordinal != len(current.definition.entries):
            raise SequenceLifecycleValidationError(
                "recovery finalization requires blocked final ordinal"
            )
        return
    if isinstance(new, RecoveryIntegratedFinalizationSequenceState) and isinstance(
        current, BlockedSequenceState
    ):
        if current.current_ordinal != len(current.definition.entries):
            raise SequenceLifecycleValidationError(
                "recovery integrated finalization requires blocked final ordinal"
            )
        if new.source_run_id != current.current_run_id:
            raise SequenceLifecycleValidationError(
                "recovery integrated finalization must preserve materialized source run"
            )
        return
    if isinstance(new, ActiveSequenceState) and isinstance(
        current, (AbortPendingSequenceState, AbortedSequenceState)
    ):
        raise SequenceLifecycleValidationError(
            "terminal sequence state cannot transition back to active"
        )
    if (
        isinstance(current, AbortedSequenceState)
        and isinstance(new, AbortedSequenceState)
        and current != new
    ):
        raise SequenceLifecycleValidationError("aborted sequence state is terminal and immutable")
    if (
        isinstance(current, BlockedSequenceState)
        and isinstance(new, BlockedSequenceState)
        and current != new
    ):
        raise SequenceLifecycleValidationError("blocked sequence state is terminal and immutable")
    if isinstance(current, AwaitingFinalizationSequenceState) and isinstance(
        new, AwaitingFinalizationSequenceState
    ):
        if current.completion_report_sha256 is None and new.completion_report_sha256 is not None:
            invariant_new = new.model_copy(
                update={
                    "completion_report_sha256": None,
                    "version": current.version,
                    "updated_at": current.updated_at,
                }
            )
            if invariant_new != current:
                raise SequenceLifecycleValidationError(
                    "completion report publication may only add report hash"
                )
            return
        if current != new:
            raise SequenceLifecycleValidationError(
                "awaiting_finalization sequence state is terminal and immutable"
            )
        return
    if isinstance(current, AwaitingFinalizationSequenceState):
        raise SequenceLifecycleValidationError("awaiting_finalization sequence state is terminal")
    if isinstance(current, RecoveryIntegratedFinalizationSequenceState) and isinstance(
        new, RecoveryIntegratedFinalizationSequenceState
    ):
        if current != new:
            raise SequenceLifecycleValidationError(
                "recovery_integrated_finalization sequence state is terminal and immutable"
            )
        return
    if isinstance(current, RecoveryIntegratedFinalizationSequenceState):
        raise SequenceLifecycleValidationError(
            "recovery_integrated_finalization sequence state is terminal"
        )
    if isinstance(new, (AbortPendingSequenceState, BlockedSequenceState, AbortedSequenceState)):
        validate_sequence_lifecycle_state(new)
    if isinstance(new, AbortPendingSequenceState) and isinstance(current, ActiveSequenceState):
        if new.current_run_id != current.current_run_id:
            raise SequenceLifecycleValidationError("abort_pending must preserve current_run_id")
        if new.materialized_entries != current.materialized_entries:
            raise SequenceLifecycleValidationError(
                "abort_pending must preserve materialized_entries"
            )
        if new.residual_risk_ordinals != current.residual_risk_ordinals:
            raise SequenceLifecycleValidationError(
                "abort_pending must preserve residual_risk_ordinals"
            )
