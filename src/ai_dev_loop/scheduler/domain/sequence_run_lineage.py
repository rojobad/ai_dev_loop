"""Per-ordinal run-attempt lineage for materialized sequence phases."""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Literal, cast

import jsonschema  # type: ignore[import-untyped]
from pydantic import Field, TypeAdapter, ValidationError, field_validator, model_validator
from referencing import Registry, Resource

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.common import DomainModel, NonEmptyStr
from ai_dev_loop.scheduler.domain.sequence import (
    AbortedSequenceState,
    AbortPendingSequenceState,
    ActiveSequenceState,
    AwaitingFinalizationSequenceState,
    BlockedSequenceState,
    MaterializedSequenceEntry,
    PreparedSequenceDefinition,
    PreparedSequenceState,
)

SEQUENCE_RUN_ATTEMPT_SCHEMA_VERSION = 1
SEQUENCE_PHASE_EXECUTION_SCHEMA_VERSION = 1
SEQUENCE_RUN_LINEAGE_SCHEMA_VERSION = 1

SEQUENCE_ATTEMPT_KIND_PLANNED = "planned_run"
SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY = "same_reviewer_retry"

SequenceAttemptKind = Literal["planned_run", "same_reviewer_retry"]

SEQUENCE_ADVANCING_TERMINAL_OUTCOMES = frozenset({"completed", "completed_with_residual_risk"})
SEQUENCE_NON_ADVANCING_TERMINAL_OUTCOMES = frozenset({"blocked", "aborted"})

SequenceTerminalOutcome = Literal[
    "completed",
    "completed_with_residual_risk",
    "blocked",
    "aborted",
]

MaterializedSequenceState = (
    ActiveSequenceState
    | AbortPendingSequenceState
    | BlockedSequenceState
    | AbortedSequenceState
    | AwaitingFinalizationSequenceState
)


class SequenceRunLineageValidationError(ValueError):
    """Raised when sequence run-attempt lineage violates invariants."""


def _json_integral_as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _coerce_json_integral_number(value: object, *, field_name: str) -> int:
    if isinstance(value, (bool, str)):
        raise ValueError(f"{field_name} must be an integer")
    coerced = _json_integral_as_int(value)
    if coerced is None:
        raise ValueError(f"{field_name} must be an integer")
    return coerced


LineagePositiveInt = Annotated[int, Field(ge=1)]


class SequenceRunAttempt(DomainModel):
    """One scheduler run attempt within a materialized sequence phase."""

    schema_version: Literal[1]
    generation: LineagePositiveInt
    run_id: NonEmptyStr
    source_run_id: NonEmptyStr | None
    attempt_kind: SequenceAttemptKind
    materialized_at: NonEmptyStr
    terminal_outcome: SequenceTerminalOutcome | None
    resolved_at: NonEmptyStr | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def coerce_schema_version(cls, value: object) -> object:
        return _coerce_json_integral_number(value, field_name="schema_version")

    @field_validator("generation", mode="before")
    @classmethod
    def coerce_generation(cls, value: object) -> object:
        return _coerce_json_integral_number(value, field_name="generation")

    @model_validator(mode="after")
    def validate_terminal_resolution(self) -> SequenceRunAttempt:
        if self.terminal_outcome is None:
            if self.resolved_at is not None:
                raise ValueError("resolved_at requires terminal_outcome")
            return self
        if self.resolved_at is None:
            raise ValueError("terminal_outcome requires resolved_at")
        return self

    @model_validator(mode="after")
    def validate_generation_rules(self) -> SequenceRunAttempt:
        if self.generation == 1:
            if self.source_run_id is not None:
                raise ValueError("generation 1 must not include source_run_id")
            if self.attempt_kind != SEQUENCE_ATTEMPT_KIND_PLANNED:
                raise ValueError("generation 1 attempt_kind must be planned_run")
        else:
            if self.source_run_id is None:
                raise ValueError("source_run_id required for generation >= 2")
            if self.attempt_kind != SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY:
                raise ValueError("same_reviewer_retry requires generation >= 2")
        return self


class SequencePhaseExecution(DomainModel):
    """Authoritative attempt lineage projection for one materialized ordinal."""

    schema_version: Literal[1]
    ordinal: LineagePositiveInt
    planned_run_id: NonEmptyStr
    attempts: tuple[SequenceRunAttempt, ...] = Field(min_length=1)
    current_run_id: NonEmptyStr
    accepted_run_id: NonEmptyStr | None

    @field_validator("schema_version", mode="before")
    @classmethod
    def coerce_schema_version(cls, value: object) -> object:
        return _coerce_json_integral_number(value, field_name="schema_version")

    @field_validator("ordinal", mode="before")
    @classmethod
    def coerce_ordinal(cls, value: object) -> object:
        return _coerce_json_integral_number(value, field_name="ordinal")

    @model_validator(mode="after")
    def validate_attempt_chain(self) -> SequencePhaseExecution:
        generations = [attempt.generation for attempt in self.attempts]
        if generations != list(range(1, len(self.attempts) + 1)):
            raise ValueError("attempt generations must be contiguous from 1")
        run_ids = [attempt.run_id for attempt in self.attempts]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("attempt run_ids must be unique")
        first = self.attempts[0]
        if first.generation != 1:
            raise ValueError("first attempt generation must be 1")
        if first.run_id != self.planned_run_id:
            raise ValueError("generation 1 run_id must equal planned_run_id")
        if first.attempt_kind != SEQUENCE_ATTEMPT_KIND_PLANNED:
            raise ValueError("generation 1 attempt_kind must be planned_run")
        if first.source_run_id is not None:
            raise ValueError("generation 1 must not include source_run_id")
        for index in range(1, len(self.attempts)):
            previous = self.attempts[index - 1]
            current = self.attempts[index]
            if current.source_run_id != previous.run_id:
                raise ValueError("attempt source_run_id must reference immediately preceding leaf")
            if current.attempt_kind not in {
                SEQUENCE_ATTEMPT_KIND_PLANNED,
                SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY,
            }:
                raise ValueError("attempt_kind is unsupported")
            if current.attempt_kind == SEQUENCE_ATTEMPT_KIND_PLANNED and current.generation != 1:
                raise ValueError("planned_run kind is only valid for generation 1")
            if (
                current.attempt_kind == SEQUENCE_ATTEMPT_KIND_SAME_REVIEWER_RETRY
                and current.generation < 2
            ):
                raise ValueError("same_reviewer_retry requires generation >= 2")
            if previous.terminal_outcome is None or previous.resolved_at is None:
                raise ValueError("superseded attempt must be terminal")
        leaf = self.attempts[-1]
        if leaf.run_id != self.current_run_id:
            raise ValueError("current_run_id must equal attempt leaf run_id")
        if leaf.terminal_outcome in SEQUENCE_ADVANCING_TERMINAL_OUTCOMES:
            if self.accepted_run_id != leaf.run_id:
                raise ValueError("accepted_run_id must equal authenticated current leaf")
        elif self.accepted_run_id is not None:
            raise ValueError("accepted_run_id requires advancing terminal outcome on current leaf")
        return self


class SequenceRunLineage(DomainModel):
    """Complete unpaginated run-attempt lineage for one sequence."""

    schema_version: Literal[1]
    sequence_id: NonEmptyStr
    phase_executions: tuple[SequencePhaseExecution, ...]

    @field_validator("schema_version", mode="before")
    @classmethod
    def coerce_schema_version(cls, value: object) -> object:
        return _coerce_json_integral_number(value, field_name="schema_version")

    @model_validator(mode="after")
    def validate_phase_ordinals(self) -> SequenceRunLineage:
        ordinals = [phase.ordinal for phase in self.phase_executions]
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("phase execution ordinals must be unique")
        if sorted(ordinals) != ordinals:
            raise ValueError("phase executions must be ordered by ascending ordinal")
        run_ids = [attempt.run_id for phase in self.phase_executions for attempt in phase.attempts]
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("attempt run_ids must be unique across sequence lineage")
        return self


def advancing_outcome_for_ordinal(
    ordinal: int,
    *,
    residual_risk_ordinals: tuple[int, ...],
) -> SequenceTerminalOutcome:
    if ordinal in residual_risk_ordinals:
        return "completed_with_residual_risk"
    return "completed"


def _successor_materialized_at(
    materialized_entries: tuple[MaterializedSequenceEntry, ...],
    ordinal: int,
) -> str | None:
    for entry in materialized_entries:
        if entry.ordinal == ordinal + 1:
            return entry.materialized_at
    return None


def _historical_resolved_at_for_phase(
    materialized_entries: tuple[MaterializedSequenceEntry, ...],
    ordinal: int,
    *,
    phase_outcome: SequenceTerminalOutcome | None,
    current_terminal_at: str,
    is_current_phase: bool,
) -> str | None:
    if phase_outcome is None:
        return None
    if is_current_phase:
        return current_terminal_at
    if phase_outcome in SEQUENCE_ADVANCING_TERMINAL_OUTCOMES:
        successor_at = _successor_materialized_at(materialized_entries, ordinal)
        if successor_at is None:
            raise SequenceRunLineageValidationError(
                "advancing phase missing successor materialization evidence"
            )
        return successor_at
    return current_terminal_at


def build_historical_phase_execution(
    *,
    ordinal: int,
    planned_run_id: str,
    materialized: MaterializedSequenceEntry,
    terminal_outcome: SequenceTerminalOutcome | None,
    resolved_at: str | None,
) -> SequencePhaseExecution:
    try:
        attempt = SequenceRunAttempt(
            schema_version=1,
            generation=1,
            run_id=materialized.run_id,
            source_run_id=None,
            attempt_kind=cast(SequenceAttemptKind, SEQUENCE_ATTEMPT_KIND_PLANNED),
            materialized_at=materialized.materialized_at,
            terminal_outcome=terminal_outcome,
            resolved_at=resolved_at,
        )
        accepted_run_id = (
            materialized.run_id
            if terminal_outcome in SEQUENCE_ADVANCING_TERMINAL_OUTCOMES
            else None
        )
        return SequencePhaseExecution(
            schema_version=1,
            ordinal=ordinal,
            planned_run_id=planned_run_id,
            attempts=(attempt,),
            current_run_id=materialized.run_id,
            accepted_run_id=accepted_run_id,
        )
    except ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage phase payload is invalid") from exc


def _terminal_resolution_timestamp(state: MaterializedSequenceState) -> str:
    if isinstance(state, BlockedSequenceState):
        return state.blocked_at
    if isinstance(state, AbortedSequenceState):
        return state.aborted_at
    if isinstance(state, AwaitingFinalizationSequenceState):
        return state.finalized_at
    return state.updated_at


def project_historical_lineage_from_state(state: MaterializedSequenceState) -> SequenceRunLineage:
    """Deterministically project generation-1 lineage from legacy single-run rows."""

    materialized_entries = state.materialized_entries
    if not materialized_entries:
        return SequenceRunLineage(
            schema_version=1,
            sequence_id=state.sequence_id,
            phase_executions=(),
        )

    residual = tuple(state.residual_risk_ordinals)
    current_terminal_at = _terminal_resolution_timestamp(state)
    phases: list[SequencePhaseExecution] = []

    if isinstance(state, AwaitingFinalizationSequenceState):
        final_ordinal = len(state.definition.entries)
        for materialized in materialized_entries:
            entry = state.definition.entries[materialized.ordinal - 1]
            if materialized.ordinal == final_ordinal:
                outcome: SequenceTerminalOutcome | None = state.final_outcome
            else:
                outcome = advancing_outcome_for_ordinal(
                    materialized.ordinal, residual_risk_ordinals=residual
                )
            phase_resolved_at = _historical_resolved_at_for_phase(
                materialized_entries,
                materialized.ordinal,
                phase_outcome=outcome,
                current_terminal_at=current_terminal_at,
                is_current_phase=materialized.ordinal == final_ordinal,
            )
            phases.append(
                build_historical_phase_execution(
                    ordinal=materialized.ordinal,
                    planned_run_id=entry.planned_run_id,
                    materialized=materialized,
                    terminal_outcome=outcome,
                    resolved_at=phase_resolved_at,
                )
            )
        try:
            return SequenceRunLineage(
                schema_version=1,
                sequence_id=state.sequence_id,
                phase_executions=tuple(phases),
            )
        except ValidationError as exc:
            raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc

    current_ordinal = getattr(state, "current_ordinal", None)
    if current_ordinal is None:
        raise SequenceRunLineageValidationError("materialized sequence missing current ordinal")
    for materialized in materialized_entries:
        entry = state.definition.entries[materialized.ordinal - 1]
        phase_outcome: SequenceTerminalOutcome | None
        if materialized.ordinal < current_ordinal:
            phase_outcome = advancing_outcome_for_ordinal(
                materialized.ordinal, residual_risk_ordinals=residual
            )
        elif materialized.ordinal == current_ordinal:
            if isinstance(state, BlockedSequenceState):
                phase_outcome = "blocked"
            elif isinstance(state, AbortedSequenceState):
                phase_outcome = "aborted"
            else:
                phase_outcome = None
        else:
            raise SequenceRunLineageValidationError("materialized ordinal exceeds current ordinal")
        phase_resolved_at = _historical_resolved_at_for_phase(
            materialized_entries,
            materialized.ordinal,
            phase_outcome=phase_outcome,
            current_terminal_at=current_terminal_at,
            is_current_phase=materialized.ordinal == current_ordinal,
        )
        phases.append(
            build_historical_phase_execution(
                ordinal=materialized.ordinal,
                planned_run_id=entry.planned_run_id,
                materialized=materialized,
                terminal_outcome=phase_outcome,
                resolved_at=phase_resolved_at,
            )
        )
    try:
        return SequenceRunLineage(
            schema_version=1,
            sequence_id=state.sequence_id,
            phase_executions=tuple(phases),
        )
    except ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc


def validate_lineage_against_definition(
    definition: PreparedSequenceDefinition,
    lineage: SequenceRunLineage,
    *,
    materialized_entries: tuple[MaterializedSequenceEntry, ...],
    current_ordinal: int | None,
    current_run_id: str | None,
    final_run_id: str | None = None,
) -> None:
    if lineage.sequence_id != definition.sequence_id:
        raise SequenceRunLineageValidationError("lineage sequence_id disagrees with definition")
    if len(lineage.phase_executions) != len(materialized_entries):
        raise SequenceRunLineageValidationError(
            "lineage phase count disagrees with materialized entries"
        )
    materialized_by_ordinal = {entry.ordinal: entry for entry in materialized_entries}
    for phase in lineage.phase_executions:
        if phase.ordinal < 1 or phase.ordinal > len(definition.entries):
            raise SequenceRunLineageValidationError("lineage ordinal out of definition range")
        frozen = definition.entries[phase.ordinal - 1]
        if phase.planned_run_id != frozen.planned_run_id:
            raise SequenceRunLineageValidationError("planned_run_id disagrees with frozen entry")
        materialized = materialized_by_ordinal.get(phase.ordinal)
        if materialized is None:
            raise SequenceRunLineageValidationError("lineage ordinal missing materialized entry")
        if phase.current_run_id != materialized.run_id:
            raise SequenceRunLineageValidationError(
                "lineage current_run_id disagrees with materialized entry"
            )
        leaf = phase.attempts[-1]
        if leaf.materialized_at != materialized.materialized_at:
            raise SequenceRunLineageValidationError(
                "lineage leaf materialized_at disagrees with materialized entry"
            )
        if (
            current_ordinal is not None
            and phase.ordinal == current_ordinal
            and current_run_id is not None
            and phase.current_run_id != current_run_id
        ):
            raise SequenceRunLineageValidationError(
                "lineage current leaf disagrees with sequence state"
            )
        if (
            final_run_id is not None
            and phase.ordinal == len(definition.entries)
            and phase.current_run_id != final_run_id
        ):
            raise SequenceRunLineageValidationError(
                "final lineage leaf disagrees with final_run_id"
            )


def _expected_terminal_for_phase(
    state: MaterializedSequenceState,
    ordinal: int,
) -> SequenceTerminalOutcome | None:
    residual = tuple(state.residual_risk_ordinals)
    if isinstance(state, AwaitingFinalizationSequenceState):
        if ordinal == len(state.definition.entries):
            return state.final_outcome
        return advancing_outcome_for_ordinal(ordinal, residual_risk_ordinals=residual)
    current_ordinal = state.current_ordinal
    if current_ordinal is None:
        raise SequenceRunLineageValidationError("materialized sequence missing current ordinal")
    if ordinal < current_ordinal:
        return advancing_outcome_for_ordinal(ordinal, residual_risk_ordinals=residual)
    if ordinal > current_ordinal:
        raise SequenceRunLineageValidationError("lineage ordinal exceeds current ordinal")
    if isinstance(state, BlockedSequenceState):
        return "blocked"
    if isinstance(state, AbortedSequenceState):
        if ordinal == current_ordinal and state.preserved_current_leaf_terminal is not None:
            return state.preserved_current_leaf_terminal
        return "aborted"
    return None


def _expected_resolved_at_for_phase(
    state: MaterializedSequenceState,
    ordinal: int,
) -> str | None:
    expected = _expected_terminal_for_phase(state, ordinal)
    if expected is None:
        return None
    if isinstance(state, AwaitingFinalizationSequenceState):
        final_ordinal = len(state.definition.entries)
        return _historical_resolved_at_for_phase(
            state.materialized_entries,
            ordinal,
            phase_outcome=expected,
            current_terminal_at=state.finalized_at,
            is_current_phase=ordinal == final_ordinal,
        )
    current_ordinal = state.current_ordinal
    if current_ordinal is None:
        raise SequenceRunLineageValidationError("materialized sequence missing current ordinal")
    if (
        isinstance(state, AbortedSequenceState)
        and ordinal == current_ordinal
        and state.preserved_current_leaf_resolved_at is not None
        and expected == state.preserved_current_leaf_terminal
    ):
        return state.preserved_current_leaf_resolved_at
    return _historical_resolved_at_for_phase(
        state.materialized_entries,
        ordinal,
        phase_outcome=expected,
        current_terminal_at=_terminal_resolution_timestamp(state),
        is_current_phase=ordinal == current_ordinal,
    )


def validate_lineage_terminal_outcomes_for_state(
    state: MaterializedSequenceState,
    lineage: SequenceRunLineage,
) -> None:
    for phase in lineage.phase_executions:
        expected = _expected_terminal_for_phase(state, phase.ordinal)
        leaf = phase.attempts[-1]
        if expected is None:
            if leaf.terminal_outcome is not None:
                raise SequenceRunLineageValidationError(
                    "active sequence leaf must remain unresolved"
                )
            if leaf.resolved_at is not None:
                raise SequenceRunLineageValidationError(
                    "active sequence leaf must not retain resolved_at"
                )
            if phase.accepted_run_id is not None:
                raise SequenceRunLineageValidationError(
                    "active sequence leaf must not retain accepted_run_id"
                )
            continue
        if leaf.terminal_outcome != expected:
            raise SequenceRunLineageValidationError(
                "lineage terminal outcome disagrees with sequence state"
            )
        expected_resolved_at = _expected_resolved_at_for_phase(state, phase.ordinal)
        if expected_resolved_at is None or leaf.resolved_at != expected_resolved_at:
            raise SequenceRunLineageValidationError(
                "lineage resolved_at disagrees with sequence state"
            )
        if expected in SEQUENCE_ADVANCING_TERMINAL_OUTCOMES:
            if phase.accepted_run_id != leaf.run_id:
                raise SequenceRunLineageValidationError(
                    "accepted_run_id must equal authenticated current leaf"
                )
        elif phase.accepted_run_id is not None:
            raise SequenceRunLineageValidationError(
                "non-advancing terminal must not retain accepted_run_id"
            )


def _attempt_generations_contiguous_ordered(attempts: list[object]) -> bool:
    generations: list[int] = []
    for attempt in attempts:
        if not isinstance(attempt, dict):
            return False
        generation = attempt.get("generation")
        coerced = _json_integral_as_int(generation)
        if coerced is None:
            return False
        generations.append(coerced)
    return generations == list(range(1, len(attempts) + 1))


def _phase_execution_format_checker(instance: object) -> bool:
    try:
        if not isinstance(instance, dict):
            return False
        attempts = instance.get("attempts")
        current_run_id = instance.get("current_run_id")
        planned_run_id = instance.get("planned_run_id")
        accepted_run_id = instance.get("accepted_run_id")
        if not isinstance(attempts, list) or not attempts or not isinstance(current_run_id, str):
            return False
        if not isinstance(planned_run_id, str):
            return False
        if accepted_run_id is not None and not isinstance(accepted_run_id, str):
            return False
        if not _attempt_generations_contiguous_ordered(attempts):
            return False
        first = attempts[0]
        if not isinstance(first, dict):
            return False
        if first.get("run_id") != planned_run_id:
            return False
        leaf = attempts[-1]
        if not isinstance(leaf, dict):
            return False
        if leaf.get("run_id") != current_run_id:
            return False
        for index in range(1, len(attempts)):
            previous = attempts[index - 1]
            current = attempts[index]
            if not isinstance(previous, dict) or not isinstance(current, dict):
                return False
            if current.get("source_run_id") != previous.get("run_id"):
                return False
            previous_outcome = previous.get("terminal_outcome")
            previous_resolved = previous.get("resolved_at")
            if previous_outcome is not None and not isinstance(previous_outcome, str):
                return False
            if previous_resolved is not None and not isinstance(previous_resolved, str):
                return False
            if previous_outcome is None or previous_resolved is None:
                return False
        leaf_outcome = leaf.get("terminal_outcome")
        if leaf_outcome is not None and not isinstance(leaf_outcome, str):
            return False
        advancing = leaf_outcome in {"completed", "completed_with_residual_risk"}
        if advancing:
            return accepted_run_id == current_run_id
        return accepted_run_id is None
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


def _lineage_aggregate_format_checker(instance: object) -> bool:
    try:
        if not isinstance(instance, dict):
            return False
        phases = instance.get("phase_executions")
        if not isinstance(phases, list):
            return False
        ordinals: list[int] = []
        run_ids: list[str] = []
        for phase in phases:
            if not isinstance(phase, dict):
                return False
            ordinal = phase.get("ordinal")
            coerced_ordinal = _json_integral_as_int(ordinal)
            if coerced_ordinal is None:
                return False
            ordinals.append(coerced_ordinal)
            attempts = phase.get("attempts")
            if not isinstance(attempts, list):
                return False
            for attempt in attempts:
                if not isinstance(attempt, dict):
                    return False
                run_id = attempt.get("run_id")
                if not isinstance(run_id, str):
                    return False
                run_ids.append(run_id)
        if len(set(ordinals)) != len(ordinals):
            return False
        if ordinals != sorted(ordinals):
            return False
        return len(set(run_ids)) == len(run_ids)
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


@lru_cache(maxsize=1)
def _lineage_json_schema_validator() -> jsonschema.Draft202012Validator:
    attempt_schema = json.loads(
        schema_path("scheduler-sequence-run-attempt-v1.json").read_text(encoding="utf-8")
    )
    phase_schema = json.loads(
        schema_path("scheduler-sequence-phase-execution-v1.json").read_text(encoding="utf-8")
    )
    lineage_schema = json.loads(
        schema_path("scheduler-sequence-run-lineage-v1.json").read_text(encoding="utf-8")
    )
    registry = Registry().with_resources(
        [
            (attempt_schema["$id"], Resource.from_contents(attempt_schema)),
            (phase_schema["$id"], Resource.from_contents(phase_schema)),
            (lineage_schema["$id"], Resource.from_contents(lineage_schema)),
        ]
    )
    format_checker = jsonschema.FormatChecker()
    format_checker.checks("scheduler-sequence-phase-execution-v1")(_phase_execution_format_checker)
    format_checker.checks("scheduler-sequence-run-lineage-v1")(_lineage_aggregate_format_checker)
    return jsonschema.Draft202012Validator(
        lineage_schema,
        registry=registry,
        format_checker=format_checker,
    )


def validate_lineage_json_schema(payload: dict[str, object]) -> None:
    try:
        _lineage_json_schema_validator().validate(payload)
    except jsonschema.ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc


def validate_persisted_lineage_aggregate(
    state: (
        PreparedSequenceState
        | ActiveSequenceState
        | AbortPendingSequenceState
        | BlockedSequenceState
        | AbortedSequenceState
        | AwaitingFinalizationSequenceState
    ),
    lineage: SequenceRunLineage,
) -> None:
    """Validate structural and semantic invariants for one persisted lineage aggregate."""

    payload = lineage.model_dump(mode="json")
    validate_lineage_json_schema(payload)
    try:
        SEQUENCE_RUN_LINEAGE_ADAPTER.validate_python(payload)
    except ValidationError as exc:
        raise SequenceRunLineageValidationError("lineage aggregate payload is invalid") from exc
    validate_lineage_against_state(state, lineage)


def validate_lineage_against_state(
    state: (
        PreparedSequenceState
        | ActiveSequenceState
        | AbortPendingSequenceState
        | BlockedSequenceState
        | AbortedSequenceState
        | AwaitingFinalizationSequenceState
    ),
    lineage: SequenceRunLineage,
) -> None:
    if lineage.sequence_id != state.sequence_id:
        raise SequenceRunLineageValidationError("lineage sequence_id disagrees with state")

    if isinstance(state, PreparedSequenceState):
        if lineage.phase_executions:
            raise SequenceRunLineageValidationError(
                "prepared sequence must not retain lineage rows"
            )
        return

    materialized_entries = state.materialized_entries
    current_ordinal = getattr(state, "current_ordinal", None)
    current_run_id = getattr(state, "current_run_id", None)
    final_run_id = getattr(state, "final_run_id", None)
    validate_lineage_against_definition(
        state.definition,
        lineage,
        materialized_entries=materialized_entries,
        current_ordinal=current_ordinal,
        current_run_id=current_run_id,
        final_run_id=final_run_id,
    )
    validate_lineage_terminal_outcomes_for_state(state, lineage)


SEQUENCE_RUN_ATTEMPT_ADAPTER: TypeAdapter[SequenceRunAttempt] = TypeAdapter(SequenceRunAttempt)
SEQUENCE_PHASE_EXECUTION_ADAPTER: TypeAdapter[SequencePhaseExecution] = TypeAdapter(
    SequencePhaseExecution
)
SEQUENCE_RUN_LINEAGE_ADAPTER: TypeAdapter[SequenceRunLineage] = TypeAdapter(SequenceRunLineage)
