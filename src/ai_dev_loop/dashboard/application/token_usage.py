"""Cohort-scoped token analytics over authenticated direct usage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ai_dev_loop.dashboard.application.ports import (
    DashboardObservationSet,
    DashboardReadError,
    DashboardReadErrorKind,
    DashboardReadOperation,
    DashboardReadPort,
    DashboardReadScope,
    RunObservation,
)
from ai_dev_loop.dashboard.application.queries import (
    CoverageResult,
    FiltersResult,
    _coverage,
    _filters,
)
from ai_dev_loop.dashboard.domain.filters import DashboardFilters
from ai_dev_loop.dashboard.domain.metrics import CoverageCounts, first_execution_launches
from ai_dev_loop.dashboard.domain.token_analytics import (
    AttemptFact,
    CategoryFact,
    FlowFact,
    TokenAggregate,
    TokenAnalyticsMagnitudeError,
    TokenRowFact,
    aggregate_token_usage,
)
from ai_dev_loop.scheduler.application.token_usage_query import (
    DirectUsageEvidence,
    TokenUsageScopeLimit,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant

TOKEN_SCOPE_TOO_LARGE = "Dashboard scope is too large; narrow the project or time range."
TOKEN_RUN_NOT_IN_COHORT = "The selected run is outside this dashboard cohort."
TOKEN_MODEL_PROVENANCE = "configured"
TOKEN_NORMALIZATION_VERSION = "provider-usage-v1"
DEFAULT_TOKEN_QUERY_SECONDS = 5.0
DEFAULT_TOKEN_MAX_RUNS = 10_000
DEFAULT_TOKEN_MAX_ATTEMPTS = 100_000


@dataclass(frozen=True)
class TokenCategoryResult:
    known_total: int | None
    known_observations: int
    observation_count: int
    coverage: str


@dataclass(frozen=True)
class TokenFlowResult:
    name: str
    known_total: int | None
    known_observations: int
    observation_count: int
    coverage: str


@dataclass(frozen=True)
class TokenUsageRowResult:
    provider: str
    configured_model: str | None
    model_provenance: str
    role: str
    stage: str
    known_total_tokens: int | None
    inference_attempts: int
    known_attempts: int
    partial_attempts: int
    unknown_attempts: int
    coverage: str
    categories: dict[str, TokenCategoryResult]
    native_categories: dict[str, TokenCategoryResult]
    flows: tuple[TokenFlowResult, ...]


@dataclass(frozen=True)
class SelectedRunResult:
    run_id: str
    short_name: str
    project_name: str


@dataclass(frozen=True)
class TokenSourceResult:
    known_total_tokens: int | None
    inference_attempts: int
    known_attempts: int
    partial_attempts: int
    unknown_attempts: int
    coverage: str
    diagnostics: tuple[str, ...]
    categories: dict[str, TokenCategoryResult]
    flows: tuple[TokenFlowResult, ...]


@dataclass(frozen=True)
class TokenUsageResult:
    observed_at: str
    filters: FiltersResult
    scope: str
    model_provenance: str
    normalization_version: str
    selected_run: SelectedRunResult | None
    selected_run_count: int
    cohort_run_count: int
    cohort_coverage: CoverageResult
    source: TokenSourceResult
    rows: tuple[TokenUsageRowResult, ...]


class DirectUsageReader(Protocol):
    def collect(
        self,
        run_ids: tuple[str, ...],
        *,
        max_attempts: int,
        checkpoint: Callable[[], None],
    ) -> DirectUsageEvidence: ...


class TokenUsageQueries:
    """One bounded read for cohort membership and that cohort's direct usage."""

    def __init__(
        self,
        cohort_factory: Callable[[float], DashboardReadPort],
        usage_factory: Callable[[], DirectUsageReader],
        *,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        cancelled: Callable[[], bool] | None = None,
        budget_seconds: float = DEFAULT_TOKEN_QUERY_SECONDS,
        max_runs: int = DEFAULT_TOKEN_MAX_RUNS,
        max_attempts: int = DEFAULT_TOKEN_MAX_ATTEMPTS,
    ) -> None:
        if budget_seconds <= 0:
            raise ValueError("budget_seconds must be positive")
        if max_runs < 1 or max_attempts < 1:
            raise ValueError("token query bounds must be positive")
        self._cohort_factory = cohort_factory
        self._usage_factory = usage_factory
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._monotonic = monotonic or _default_monotonic
        self._cancelled = cancelled or (lambda: False)
        self.budget_seconds = budget_seconds
        self.max_runs = max_runs
        self.max_attempts = max_attempts

    def query(self, filters: DashboardFilters, run_id: str | None) -> TokenUsageResult:
        try:
            return self._query_bounded(filters, run_id)
        except TokenUsageScopeLimit as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.INVALID_ARGUMENT,
                TOKEN_SCOPE_TOO_LARGE,
            ) from exc

    def _query_bounded(self, filters: DashboardFilters, run_id: str | None) -> TokenUsageResult:
        deadline = self._monotonic() + self.budget_seconds

        def checkpoint() -> None:
            if self._cancelled() or self._monotonic() >= deadline:
                raise TokenUsageScopeLimit

        checkpoint()
        observation = self._cohort_factory(deadline).read_observations(
            DashboardReadScope(
                operation=DashboardReadOperation.SUMMARY,
                project_name=filters.project_name,
                from_utc=filters.from_utc,
                to_utc=filters.to_utc,
            )
        )
        checkpoint()
        cohort, counts = _cohort_runs(observation, filters)
        if len(cohort) > self.max_runs:
            raise DashboardReadError(DashboardReadErrorKind.INVALID_ARGUMENT, TOKEN_SCOPE_TOO_LARGE)
        selected = None
        usage_runs = cohort
        scope = "cohort"
        if run_id is not None:
            selected = next((run for run in cohort if run.run_id == run_id), None)
            if selected is None:
                raise DashboardReadError(
                    DashboardReadErrorKind.NOT_FOUND,
                    TOKEN_RUN_NOT_IN_COHORT,
                )
            usage_runs = (selected,)
            scope = "direct"
        if usage_runs:
            evidence = self._usage_factory().collect(
                tuple(run.run_id for run in usage_runs),
                max_attempts=self.max_attempts,
                checkpoint=checkpoint,
            )
        else:
            evidence = DirectUsageEvidence(attempts=(), unattributed_attempts=0, diagnostics=())
        try:
            aggregate = aggregate_token_usage(
                tuple(
                    AttemptFact(
                        provider=attempt.provider,
                        role=attempt.role,
                        stage=attempt.stage,
                        configured_model=attempt.configured_model,
                        observations=attempt.observations,
                        state=attempt.state,
                    )
                    for attempt in evidence.attempts
                ),
                unattributed_attempts=evidence.unattributed_attempts,
                diagnostics=evidence.diagnostics,
            )
        except TokenAnalyticsMagnitudeError as exc:
            raise DashboardReadError(
                DashboardReadErrorKind.INVALID_ARGUMENT,
                TOKEN_SCOPE_TOO_LARGE,
            ) from exc
        checkpoint()
        return TokenUsageResult(
            observed_at=encode_utc_instant(self._now()),
            filters=_filters(filters),
            scope=scope,
            model_provenance=TOKEN_MODEL_PROVENANCE,
            normalization_version=TOKEN_NORMALIZATION_VERSION,
            selected_run=(
                None
                if selected is None
                else SelectedRunResult(
                    run_id=selected.run_id,
                    short_name=selected.short_name,
                    project_name=selected.project_name,
                )
            ),
            selected_run_count=len(usage_runs),
            cohort_run_count=len(cohort),
            cohort_coverage=_coverage(counts, contributions=len(cohort)),
            source=_source(aggregate),
            rows=tuple(_row(item) for item in aggregate.rows),
        )


def _default_monotonic() -> float:
    import time

    return time.monotonic()


def _cohort_runs(
    observation: DashboardObservationSet,
    filters: DashboardFilters,
) -> tuple[tuple[RunObservation, ...], CoverageCounts]:
    launches, counts = first_execution_launches(observation.runs, observation.attempts)
    selected: list[RunObservation] = []
    seen: set[str] = set()
    for run in observation.runs:
        launch = launches.get(run.run_id)
        if launch is None or not filters.contains(launch) or run.run_id in seen:
            continue
        seen.add(run.run_id)
        selected.append(run)
    selected.sort(key=lambda run: (launches[run.run_id], run.run_id))
    return tuple(selected), counts


def _category(fact: CategoryFact) -> TokenCategoryResult:
    return TokenCategoryResult(
        known_total=fact.known_total,
        known_observations=fact.known_observations,
        observation_count=fact.observation_count,
        coverage=fact.coverage,
    )


def _flow(fact: FlowFact) -> TokenFlowResult:
    return TokenFlowResult(
        name=fact.name,
        known_total=fact.known_total,
        known_observations=fact.known_observations,
        observation_count=fact.observation_count,
        coverage=fact.coverage,
    )


def _source(aggregate: TokenAggregate) -> TokenSourceResult:
    return TokenSourceResult(
        known_total_tokens=aggregate.known_total_tokens,
        inference_attempts=aggregate.inference_attempts,
        known_attempts=aggregate.known_attempts,
        partial_attempts=aggregate.partial_attempts,
        unknown_attempts=aggregate.unknown_attempts,
        coverage=aggregate.coverage,
        diagnostics=aggregate.diagnostics,
        categories={name: _category(fact) for name, fact in aggregate.categories.items()},
        flows=tuple(_flow(fact) for fact in aggregate.flows),
    )


def _row(item: TokenRowFact) -> TokenUsageRowResult:
    return TokenUsageRowResult(
        provider=item.provider,
        configured_model=item.configured_model,
        model_provenance=TOKEN_MODEL_PROVENANCE,
        role=item.role,
        stage=item.stage,
        known_total_tokens=item.known_total_tokens,
        inference_attempts=item.inference_attempts,
        known_attempts=item.known_attempts,
        partial_attempts=item.partial_attempts,
        unknown_attempts=item.unknown_attempts,
        coverage=item.coverage,
        categories={name: _category(fact) for name, fact in item.categories.items()},
        native_categories={name: _category(fact) for name, fact in item.native_categories.items()},
        flows=tuple(_flow(fact) for fact in item.flows),
    )
