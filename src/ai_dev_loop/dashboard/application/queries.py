"""Shared semantic dashboard queries over one bounded read observation."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from ai_dev_loop.dashboard.application.ports import (
    DashboardObservationSet,
    DashboardReadOperation,
    DashboardReadPort,
    DashboardReadScope,
    RunObservation,
    SequenceBindingObservation,
)
from ai_dev_loop.dashboard.domain.filters import DashboardFilters, DashboardPage
from ai_dev_loop.dashboard.domain.metrics import (
    STATE_GROUPS,
    CoverageCounts,
    DashboardMetric,
    first_execution_launches,
    first_review_round_launches,
    local_dates,
    process_second_buckets,
    state_group,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant


@dataclass(frozen=True)
class PageResult:
    offset: int
    limit: int
    next_offset: int | None
    has_more: bool


@dataclass(frozen=True)
class FiltersResult:
    from_utc: str
    to_utc: str
    time_zone: str
    project_name: str | None


@dataclass(frozen=True)
class CoverageResult:
    data_state: str
    unstarted_runs: int
    missing_launch_times: int
    unknown_execution_kinds: int
    missing_duration_attempts: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ProjectItemResult:
    project_name: str
    run_count: int


@dataclass(frozen=True)
class ProjectsResult:
    observed_at: str
    items: tuple[ProjectItemResult, ...]
    page: PageResult
    total_count: int


@dataclass(frozen=True)
class StateCountsResult:
    completed: int
    residual_risk: int
    running: int
    waiting: int
    blocked: int
    aborted: int
    other: int


@dataclass(frozen=True)
class SummaryTotalsResult:
    runs_started: int
    completed: int
    residual_risk: int
    waiting_blocked: int


@dataclass(frozen=True)
class ProjectSummaryResult:
    project_name: str
    total: int
    state_counts: StateCountsResult


@dataclass(frozen=True)
class WorkloadResult:
    project_name: str
    sequence_id: str | None
    sequence_name: str | None
    run_count: int


@dataclass(frozen=True)
class SummaryResult:
    observed_at: str
    filters: FiltersResult
    coverage: CoverageResult
    totals: SummaryTotalsResult
    projects: tuple[ProjectSummaryResult, ...]
    workload: tuple[WorkloadResult, ...]


@dataclass(frozen=True)
class SequenceBindingResult:
    sequence_id: str
    sequence_name: str
    ordinal: int
    phase_name: str
    generation: int
    attempt_kind: str
    source_run_id: str | None


@dataclass(frozen=True)
class RunItemResult:
    run_id: str
    project_name: str
    implementer_provider: str
    state: str
    submitted_at: str
    first_execution_launch_at: str
    sequence_binding: SequenceBindingResult | None


@dataclass(frozen=True)
class RunsResult:
    observed_at: str
    filters: FiltersResult
    coverage: CoverageResult
    items: tuple[RunItemResult, ...]
    page: PageResult
    total_count: int


@dataclass(frozen=True)
class ActivityDayResult:
    date: str
    value: int | float


@dataclass(frozen=True)
class ActivityResult:
    observed_at: str
    filters: FiltersResult
    coverage: CoverageResult
    metric: DashboardMetric
    unit: str
    days: tuple[ActivityDayResult, ...]
    total: int | float


def _page(page: DashboardPage, total: int) -> PageResult:
    next_offset = page.offset + page.limit
    has_more = next_offset < total
    return PageResult(
        offset=page.offset,
        limit=page.limit,
        next_offset=next_offset if has_more else None,
        has_more=has_more,
    )


def _filters(filters: DashboardFilters) -> FiltersResult:
    return FiltersResult(
        from_utc=filters.from_text,
        to_utc=filters.to_text,
        time_zone=filters.time_zone,
        project_name=filters.project_name,
    )


def _coverage(
    counts: CoverageCounts,
    *,
    contributions: int,
    missing_duration_attempts: int = 0,
    duration_partial: bool = False,
    include_launch_issues: bool = True,
) -> CoverageResult:
    partial = duration_partial or (
        include_launch_issues
        and (counts.partial_evidence or counts.missing_launch_times > 0)
    )
    warnings: list[str] = []
    if include_launch_issues and counts.missing_launch_times:
        warnings.append("missing_launch_times")
    if include_launch_issues and counts.unknown_execution_kinds:
        warnings.append("unknown_execution_kinds")
    if missing_duration_attempts:
        warnings.append("missing_duration_attempts")
    data_state = "partial" if partial else ("empty" if contributions == 0 else "complete")
    return CoverageResult(
        data_state=data_state,
        unstarted_runs=counts.unstarted_runs,
        missing_launch_times=counts.missing_launch_times,
        unknown_execution_kinds=counts.unknown_execution_kinds,
        missing_duration_attempts=missing_duration_attempts,
        warnings=tuple(warnings),
    )


def _state_counts(runs: list[RunObservation]) -> StateCountsResult:
    counts = Counter(state_group(run.state) for run in runs)
    return StateCountsResult(
        completed=counts["completed"],
        residual_risk=counts["residual-risk"],
        running=counts["running"],
        waiting=counts["waiting"],
        blocked=counts["blocked"],
        aborted=counts["aborted"],
        other=counts["other"],
    )


def _binding(value: SequenceBindingObservation | None) -> SequenceBindingResult | None:
    if value is None:
        return None
    return SequenceBindingResult(
        sequence_id=value.sequence_id,
        sequence_name=value.sequence_name,
        ordinal=value.ordinal,
        phase_name=value.phase_name,
        generation=value.generation,
        attempt_kind=value.attempt_kind,
        source_run_id=value.source_run_id,
    )


class DashboardQueries:
    def __init__(
        self,
        repository: DashboardReadPort,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self._now = now or (lambda: datetime.now(tz=UTC))

    def _observed_at(self) -> str:
        return encode_utc_instant(self._now())

    def projects(self, page: DashboardPage) -> ProjectsResult:
        observation = self.repository.read_project_catalog(
            offset=page.offset,
            limit=page.limit,
        )
        return ProjectsResult(
            observed_at=self._observed_at(),
            items=tuple(
                ProjectItemResult(project_name=item.project_name, run_count=item.run_count)
                for item in observation.items
            ),
            page=_page(page, observation.total_count),
            total_count=observation.total_count,
        )

    def _observation(
        self,
        filters: DashboardFilters,
        operation: DashboardReadOperation,
    ) -> DashboardObservationSet:
        return self.repository.read_observations(
            DashboardReadScope(
                operation=operation,
                project_name=filters.project_name,
                from_utc=filters.from_utc,
                to_utc=filters.to_utc,
            )
        )

    def summary(self, filters: DashboardFilters) -> SummaryResult:
        observation = self._observation(filters, DashboardReadOperation.SUMMARY)
        launches, counts = first_execution_launches(observation.runs, observation.attempts)
        cohort = [
            run
            for run in observation.runs
            if run.run_id in launches and filters.contains(launches[run.run_id])
        ]
        by_project: dict[str, list[RunObservation]] = defaultdict(list)
        workload: Counter[tuple[str, str | None, str | None]] = Counter()
        for run in cohort:
            by_project[run.project_name].append(run)
            binding = run.sequence_binding
            workload[
                (
                    run.project_name,
                    binding.sequence_id if binding else None,
                    binding.sequence_name if binding else None,
                )
            ] += 1
        grouped = Counter(state_group(run.state) for run in cohort)
        return SummaryResult(
            observed_at=self._observed_at(),
            filters=_filters(filters),
            coverage=_coverage(counts, contributions=len(cohort)),
            totals=SummaryTotalsResult(
                runs_started=len(cohort),
                completed=grouped["completed"],
                residual_risk=grouped["residual-risk"],
                waiting_blocked=grouped["waiting"] + grouped["blocked"],
            ),
            projects=tuple(
                ProjectSummaryResult(
                    project_name=project_name,
                    total=len(project_runs),
                    state_counts=_state_counts(project_runs),
                )
                for project_name, project_runs in sorted(by_project.items())
            ),
            workload=tuple(
                WorkloadResult(
                    project_name=key[0],
                    sequence_id=key[1],
                    sequence_name=key[2],
                    run_count=count,
                )
                for key, count in sorted(
                    workload.items(),
                    key=lambda item: (
                        item[0][0],
                        item[0][1] is None,
                        item[0][2] or "",
                        item[0][1] or "",
                    ),
                )
            ),
        )

    def runs(self, filters: DashboardFilters, page: DashboardPage) -> RunsResult:
        observation = self._observation(filters, DashboardReadOperation.RUNS)
        launches, counts = first_execution_launches(observation.runs, observation.attempts)
        cohort = sorted(
            (
                run
                for run in observation.runs
                if run.run_id in launches and filters.contains(launches[run.run_id])
            ),
            key=lambda run: (launches[run.run_id], run.run_id),
        )
        selected = cohort[page.offset : page.offset + page.limit]
        return RunsResult(
            observed_at=self._observed_at(),
            filters=_filters(filters),
            coverage=_coverage(counts, contributions=len(cohort)),
            items=tuple(
                RunItemResult(
                    run_id=run.run_id,
                    project_name=run.project_name,
                    implementer_provider=run.implementer_provider,
                    state=run.state,
                    submitted_at=encode_utc_instant(run.submitted_at),
                    first_execution_launch_at=encode_utc_instant(launches[run.run_id]),
                    sequence_binding=_binding(run.sequence_binding),
                )
                for run in selected
            ),
            page=_page(page, len(cohort)),
            total_count=len(cohort),
        )

    def activity(self, filters: DashboardFilters, metric: DashboardMetric) -> ActivityResult:
        if metric not in {"submissions", "review-rounds", "process-hours"}:
            raise ValueError("metric must be submissions, review-rounds, or process-hours")
        operation = {
            "submissions": DashboardReadOperation.ACTIVITY_SUBMISSIONS,
            "review-rounds": DashboardReadOperation.ACTIVITY_REVIEW_ROUNDS,
            "process-hours": DashboardReadOperation.ACTIVITY_PROCESS_HOURS,
        }[metric]
        observation = self._observation(filters, operation)
        _, counts = first_execution_launches(observation.runs, observation.attempts)
        values: dict[str, int | float]
        missing_duration = 0
        duration_partial = False
        include_launch_issues = True
        if metric == "submissions":
            submission_values: Counter[str] = Counter()
            for run in observation.runs:
                if filters.contains(run.submitted_at):
                    submission_values[
                        run.submitted_at.astimezone(filters.zone).date().isoformat()
                    ] += 1
            values = dict(submission_values)
            include_launch_issues = False
            unit = "count"
        elif metric == "review-rounds":
            round_values: Counter[str] = Counter()
            rounds, review_missing = first_review_round_launches(observation.attempts)
            if review_missing:
                counts = CoverageCounts(
                    unstarted_runs=counts.unstarted_runs,
                    missing_launch_times=counts.missing_launch_times,
                    unknown_execution_kinds=counts.unknown_execution_kinds,
                    missing_duration_attempts=counts.missing_duration_attempts,
                    partial_evidence=True,
                )
            for launched_at in rounds.values():
                if filters.contains(launched_at):
                    round_values[
                        launched_at.astimezone(filters.zone).date().isoformat()
                    ] += 1
            values = dict(round_values)
            unit = "count"
        else:
            values, missing_duration, duration_partial = process_second_buckets(
                observation.attempts,
                filters,
            )
            unit = "process-seconds"
        coverage = _coverage(
            counts,
            contributions=len(values),
            missing_duration_attempts=missing_duration,
            duration_partial=duration_partial,
            include_launch_issues=include_launch_issues,
        )
        days = []
        for local_date in local_dates(filters):
            key = local_date.isoformat()
            if coverage.data_state == "partial" and key not in values:
                continue
            days.append(ActivityDayResult(date=key, value=values.get(key, 0)))
        total = sum((item.value for item in days), start=cast(int | float, 0))
        return ActivityResult(
            observed_at=self._observed_at(),
            filters=_filters(filters),
            coverage=coverage,
            metric=metric,
            unit=unit,
            days=tuple(days),
            total=total,
        )


__all__ = [
    "ActivityResult",
    "DashboardQueries",
    "ProjectsResult",
    "RunsResult",
    "STATE_GROUPS",
    "SummaryResult",
]
