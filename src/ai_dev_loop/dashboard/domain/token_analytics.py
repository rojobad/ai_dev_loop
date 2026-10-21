"""Disjoint token-flow accounting for dashboard analytics.

Pure calculations. They do not read SQLite, HTTP, the CLI, or a chart library.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ai_dev_loop.scheduler.domain.token_usage import (
    NormalizedUsage,
    UsageCoverage,
    UsageObservationV1,
    UsageProvider,
    UsageRole,
    UsageStage,
)

JS_MAX_SAFE_INTEGER = 9_007_199_254_740_991
TokenFlowName = Literal[
    "input_other",
    "cache_read",
    "cache_write",
    "input_unsplit",
    "output_other",
    "reasoning_output",
    "output_unsplit",
]
TOKEN_FLOW_NAMES: tuple[TokenFlowName, ...] = (
    "input_other",
    "cache_read",
    "cache_write",
    "input_unsplit",
    "output_other",
    "reasoning_output",
    "output_unsplit",
)
NORMALIZED_CATEGORY_NAMES = (
    "input_total",
    "input_uncached",
    "cache_read",
    "cache_write",
    "output_total",
    "reasoning_output",
    "total_tokens",
)
NATIVE_CATEGORY_NAMES = {
    "cursor": (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "total_tokens",
        "reasoning_tokens",
    ),
    "codex": (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ),
}
AttemptEvidenceState = Literal["complete", "partial", "unknown"]
PreparedObservations = tuple[
    tuple[UsageObservationV1, tuple[tuple[TokenFlowName, int], ...]],
    ...,
]
PreparedAttempt = tuple[AttemptEvidenceState, PreparedObservations]
GroupKey = tuple[UsageProvider, str | None, UsageRole, UsageStage]


class TokenAnalyticsMagnitudeError(ValueError):
    """A counter cannot be represented as a JavaScript safe integer."""


@dataclass(frozen=True)
class AttemptFact:
    provider: UsageProvider
    role: UsageRole
    stage: UsageStage
    configured_model: str | None
    observations: tuple[UsageObservationV1, ...]
    state: AttemptEvidenceState


GroupedAttempt = tuple[AttemptFact, AttemptEvidenceState, PreparedObservations]


@dataclass(frozen=True)
class CategoryFact:
    known_total: int | None
    known_observations: int
    observation_count: int
    coverage: UsageCoverage


@dataclass(frozen=True)
class FlowFact:
    name: TokenFlowName
    known_total: int | None
    known_observations: int
    observation_count: int
    coverage: UsageCoverage


@dataclass(frozen=True)
class TokenRowFact:
    provider: UsageProvider
    configured_model: str | None
    role: UsageRole
    stage: UsageStage
    known_total_tokens: int | None
    inference_attempts: int
    known_attempts: int
    partial_attempts: int
    unknown_attempts: int
    coverage: UsageCoverage
    categories: dict[str, CategoryFact]
    native_categories: dict[str, CategoryFact]
    flows: tuple[FlowFact, ...]


@dataclass(frozen=True)
class TokenAggregate:
    known_total_tokens: int | None
    inference_attempts: int
    known_attempts: int
    partial_attempts: int
    unknown_attempts: int
    coverage: UsageCoverage
    diagnostics: tuple[str, ...]
    categories: dict[str, CategoryFact]
    flows: tuple[FlowFact, ...]
    rows: tuple[TokenRowFact, ...]


def _require_safe(value: int) -> None:
    if value < 0 or value > JS_MAX_SAFE_INTEGER:
        raise TokenAnalyticsMagnitudeError("token counter exceeds the supported integer range")


def safe_sum(values: list[int]) -> int:
    total = 0
    for value in values:
        _require_safe(value)
        if total > JS_MAX_SAFE_INTEGER - value:
            raise TokenAnalyticsMagnitudeError("token counter exceeds the supported integer range")
        total += value
    return total


def flow_leaves(
    provider: UsageProvider,
    normalized: NormalizedUsage,
) -> tuple[tuple[TokenFlowName, int], ...] | None:
    """Partition one observation. None means contradictory arithmetic."""

    leaves: list[tuple[TokenFlowName, int]] = []
    if provider == "cursor":
        if (
            normalized.input_uncached is None
            or normalized.cache_read is None
            or normalized.cache_write is None
            or normalized.output_total is None
        ):
            return None
        leaves.extend(
            (
                ("input_other", normalized.input_uncached),
                ("cache_read", normalized.cache_read),
                ("cache_write", normalized.cache_write),
            )
        )
    elif provider == "codex":
        if normalized.input_total is None or normalized.output_total is None:
            return None
        if normalized.cache_read is None:
            leaves.append(("input_unsplit", normalized.input_total))
        else:
            uncached = normalized.input_total - normalized.cache_read
            if uncached < 0:
                return None
            leaves.extend((("input_other", uncached), ("cache_read", normalized.cache_read)))
    else:
        return None
    if normalized.reasoning_output is None:
        if normalized.output_total is None:
            return None
        leaves.append(("output_unsplit", normalized.output_total))
    else:
        if normalized.output_total is None:
            return None
        output_other = normalized.output_total - normalized.reasoning_output
        if output_other < 0:
            return None
        leaves.extend(
            (
                ("output_other", output_other),
                ("reasoning_output", normalized.reasoning_output),
            )
        )
    for _, value in leaves:
        if value < 0:
            return None
    if safe_sum_or_none(leaves) != normalized.total_tokens:
        return None
    return tuple(leaves)


def safe_sum_or_none(leaves: list[tuple[TokenFlowName, int]]) -> int | None:
    return safe_sum([value for _, value in leaves])


def usage_coverage(
    inference_attempts: int,
    known_attempts: int,
    partial_attempts: int,
) -> UsageCoverage:
    if inference_attempts == 0:
        return "not_applicable"
    if known_attempts == inference_attempts:
        return "complete"
    if known_attempts or partial_attempts:
        return "partial"
    return "unknown"


def category_fact(
    values: list[int | None],
    *,
    inference_attempts: int,
    complete_attempts: int,
) -> CategoryFact:
    known = [value for value in values if value is not None]
    if inference_attempts == 0:
        coverage: UsageCoverage = "not_applicable"
    elif complete_attempts == inference_attempts:
        coverage = "complete"
    elif known:
        coverage = "partial"
    else:
        coverage = "unknown"
    return CategoryFact(
        known_total=safe_sum(known) if known else None,
        known_observations=len(known),
        observation_count=len(values),
        coverage=coverage,
    )


def _prepared_attempt(
    attempt: AttemptFact,
    diagnostics: list[str],
) -> tuple[AttemptEvidenceState, PreparedObservations]:
    kept: list[tuple[UsageObservationV1, tuple[tuple[TokenFlowName, int], ...]]] = []
    dropped = False
    for observation in attempt.observations:
        _require_safe(observation.normalized.total_tokens)
        if observation.provider != attempt.provider:
            diagnostics.append("usage_observation_integrity_error")
            dropped = True
            continue
        leaves = flow_leaves(observation.provider, observation.normalized)
        if leaves is None:
            diagnostics.append("usage_observation_integrity_error")
            dropped = True
            continue
        for _, value in leaves:
            _require_safe(value)
        kept.append((observation, leaves))
    if not kept:
        state: AttemptEvidenceState = "unknown"
    elif dropped or attempt.state != "complete":
        state = "partial"
    else:
        state = "complete"
    return state, tuple(kept)


def _flow_facts(
    prepared: tuple[PreparedAttempt, ...],
    *,
    inference_attempts: int,
) -> tuple[FlowFact, ...]:
    facts: list[FlowFact] = []
    for name in TOKEN_FLOW_NAMES:
        values: list[int | None] = []
        complete_attempts = 0
        for state, observations in prepared:
            observation_values: list[int | None] = []
            for _observation, leaves in observations:
                found = next((value for leaf_name, value in leaves if leaf_name == name), None)
                observation_values.append(found)
                values.append(found)
            if (
                state == "complete"
                and observations
                and all(value is not None for value in observation_values)
            ):
                complete_attempts += 1
        category = category_fact(
            values,
            inference_attempts=inference_attempts,
            complete_attempts=complete_attempts,
        )
        facts.append(
            FlowFact(
                name=name,
                known_total=category.known_total,
                known_observations=category.known_observations,
                observation_count=category.observation_count,
                coverage=category.coverage,
            )
        )
    return tuple(facts)


def _named_categories(
    names: tuple[str, ...],
    prepared: tuple[PreparedAttempt, ...],
    *,
    inference_attempts: int,
    native: bool,
) -> dict[str, CategoryFact]:
    categories: dict[str, CategoryFact] = {}
    for name in names:
        values: list[int | None] = []
        complete_attempts = 0
        for state, observations in prepared:
            observation_values: list[int | None] = []
            for observation, _leaves in observations:
                source = observation.native if native else observation.normalized
                value = getattr(source, name)
                if value is not None:
                    _require_safe(value)
                observation_values.append(value)
                values.append(value)
            if (
                state == "complete"
                and observations
                and all(
                    value is not None and observation.availability == "known"
                    for value, (observation, _leaves) in zip(
                        observation_values, observations, strict=True
                    )
                )
            ):
                complete_attempts += 1
        categories[name] = category_fact(
            values,
            inference_attempts=inference_attempts,
            complete_attempts=complete_attempts,
        )
    return categories


def aggregate_token_usage(
    attempts: tuple[AttemptFact, ...],
    *,
    unattributed_attempts: int = 0,
    diagnostics: tuple[str, ...] = (),
) -> TokenAggregate:
    """Aggregate direct observations without inventing missing splits or models."""

    if unattributed_attempts < 0:
        raise TokenAnalyticsMagnitudeError("token counter exceeds the supported integer range")
    diagnostic_list = list(diagnostics)
    prepared_by_attempt: list[GroupedAttempt] = []
    for attempt in attempts:
        state, kept = _prepared_attempt(attempt, diagnostic_list)
        prepared_by_attempt.append((attempt, state, kept))

    groups: dict[GroupKey, list[GroupedAttempt]] = {}
    for attempt, state, kept in prepared_by_attempt:
        groups.setdefault(
            (attempt.provider, attempt.configured_model, attempt.role, attempt.stage),
            [],
        ).append((attempt, state, kept))

    rows: list[TokenRowFact] = []
    for key in sorted(
        groups, key=lambda item: (item[0], item[1] is None, item[1] or "", item[2], item[3])
    ):
        provider, configured_model, role, stage = key
        grouped = groups[key]
        prepared = tuple((state, kept) for _attempt, state, kept in grouped)
        known_attempts = sum(1 for _attempt, state, _kept in grouped if state == "complete")
        partial_attempts = sum(1 for _attempt, state, _kept in grouped if state == "partial")
        unknown_attempts = sum(1 for _attempt, state, _kept in grouped if state == "unknown")
        totals = [
            observation.normalized.total_tokens
            for _state, kept in prepared
            for observation, _leaves in kept
        ]
        rows.append(
            TokenRowFact(
                provider=provider,
                configured_model=configured_model,
                role=role,
                stage=stage,
                known_total_tokens=safe_sum(totals) if totals else None,
                inference_attempts=len(grouped),
                known_attempts=known_attempts,
                partial_attempts=partial_attempts,
                unknown_attempts=unknown_attempts,
                coverage=usage_coverage(len(grouped), known_attempts, partial_attempts),
                categories=_named_categories(
                    NORMALIZED_CATEGORY_NAMES,
                    prepared,
                    inference_attempts=len(grouped),
                    native=False,
                ),
                native_categories=_named_categories(
                    NATIVE_CATEGORY_NAMES[provider],
                    prepared,
                    inference_attempts=len(grouped),
                    native=True,
                ),
                flows=_flow_facts(prepared, inference_attempts=len(grouped)),
            )
        )

    scope_prepared = tuple((state, kept) for _attempt, state, kept in prepared_by_attempt)
    scope_attempts = len(prepared_by_attempt) + unattributed_attempts
    known_attempts = sum(row.known_attempts for row in rows)
    partial_attempts = sum(row.partial_attempts for row in rows)
    unknown_attempts = sum(row.unknown_attempts for row in rows) + unattributed_attempts
    totals = [
        observation.normalized.total_tokens
        for _state, kept in scope_prepared
        for observation, _leaves in kept
    ]
    return TokenAggregate(
        known_total_tokens=safe_sum(totals) if totals else None,
        inference_attempts=scope_attempts,
        known_attempts=known_attempts,
        partial_attempts=partial_attempts,
        unknown_attempts=unknown_attempts,
        coverage=usage_coverage(scope_attempts, known_attempts, partial_attempts),
        diagnostics=tuple(sorted(set(diagnostic_list))),
        categories=_named_categories(
            NORMALIZED_CATEGORY_NAMES,
            scope_prepared,
            inference_attempts=scope_attempts,
            native=False,
        ),
        flows=_flow_facts(scope_prepared, inference_attempts=scope_attempts),
        rows=tuple(rows),
    )
