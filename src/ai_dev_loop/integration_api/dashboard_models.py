"""Explicit public Integration API DTOs for dashboard queries."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from ai_dev_loop.integration_api.models import PublicWireModel


class DashboardFiltersData(PublicWireModel):
    from_utc: str = Field(alias="fromUtc")
    to_utc: str = Field(alias="toUtc")
    time_zone: str = Field(alias="timeZone")
    project_name: str | None = Field(alias="projectName")


class DashboardCoverageData(PublicWireModel):
    data_state: Literal["complete", "partial", "empty"] = Field(alias="dataState")
    unstarted_runs: StrictInt = Field(alias="unstartedRuns", ge=0)
    missing_launch_times: StrictInt = Field(alias="missingLaunchTimes", ge=0)
    unknown_execution_kinds: StrictInt = Field(alias="unknownExecutionKinds", ge=0)
    missing_duration_attempts: StrictInt = Field(alias="missingDurationAttempts", ge=0)
    warnings: tuple[
        Literal[
            "missing_launch_times",
            "unknown_execution_kinds",
            "missing_duration_attempts",
        ],
        ...,
    ]


class DashboardPageData(PublicWireModel):
    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=100)
    next_offset: StrictInt | None = Field(alias="nextOffset", ge=0)
    has_more: StrictBool = Field(alias="hasMore")

    @model_validator(mode="after")
    def _page_invariants(self) -> DashboardPageData:
        if self.has_more != (self.next_offset is not None):
            raise ValueError("nextOffset presence must match hasMore")
        if self.next_offset is not None and self.next_offset != self.offset + self.limit:
            raise ValueError("nextOffset must advance by limit")
        return self


class DashboardRunsPageData(PublicWireModel):
    """Operation-specific page metadata for the wider run-map collection."""

    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=1000)
    next_offset: StrictInt | None = Field(alias="nextOffset", ge=0)
    has_more: StrictBool = Field(alias="hasMore")

    @model_validator(mode="after")
    def _page_invariants(self) -> DashboardRunsPageData:
        if self.has_more != (self.next_offset is not None):
            raise ValueError("nextOffset presence must match hasMore")
        if self.next_offset is not None and self.next_offset != self.offset + self.limit:
            raise ValueError("nextOffset must advance by limit")
        return self


class DashboardProjectItem(PublicWireModel):
    project_name: str = Field(alias="projectName", min_length=1)
    run_count: StrictInt = Field(alias="runCount", ge=0)


class DashboardProjectsData(PublicWireModel):
    observed_at: str = Field(alias="observedAt")
    items: tuple[DashboardProjectItem, ...]
    page: DashboardPageData
    total_count: StrictInt = Field(alias="totalCount", ge=0)


class DashboardStateCounts(PublicWireModel):
    completed: StrictInt = Field(ge=0)
    residual_risk: StrictInt = Field(alias="residualRisk", ge=0)
    running: StrictInt = Field(ge=0)
    waiting: StrictInt = Field(ge=0)
    blocked: StrictInt = Field(ge=0)
    aborted: StrictInt = Field(ge=0)
    other: StrictInt = Field(ge=0)


class DashboardSummaryTotals(PublicWireModel):
    runs_started: StrictInt = Field(alias="runsStarted", ge=0)
    completed: StrictInt = Field(ge=0)
    residual_risk: StrictInt = Field(alias="residualRisk", ge=0)
    waiting_blocked: StrictInt = Field(alias="waitingBlocked", ge=0)


class DashboardOtherState(PublicWireModel):
    state: str = Field(min_length=1)
    count: StrictInt = Field(ge=1)


class DashboardProjectSummary(PublicWireModel):
    project_name: str = Field(alias="projectName", min_length=1)
    total: StrictInt = Field(ge=0)
    state_counts: DashboardStateCounts = Field(alias="stateCounts")
    other_states: tuple[DashboardOtherState, ...] = Field(alias="otherStates")

    @model_validator(mode="after")
    def _other_state_invariants(self) -> DashboardProjectSummary:
        raw_states = [item.state for item in self.other_states]
        if raw_states != sorted(set(raw_states)):
            raise ValueError("otherStates must be unique and sorted by raw state")
        if sum(item.count for item in self.other_states) != self.state_counts.other:
            raise ValueError("otherStates counts must sum to stateCounts.other")
        return self


class DashboardWorkloadItem(PublicWireModel):
    project_name: str = Field(alias="projectName", min_length=1)
    sequence_id: str | None = Field(alias="sequenceId")
    sequence_name: str | None = Field(alias="sequenceName")
    sequence_short_name: str | None = Field(alias="sequenceShortName")
    run_count: StrictInt = Field(alias="runCount", ge=0)


class DashboardSummaryData(PublicWireModel):
    observed_at: str = Field(alias="observedAt")
    filters: DashboardFiltersData
    coverage: DashboardCoverageData
    totals: DashboardSummaryTotals
    projects: tuple[DashboardProjectSummary, ...]
    workload: tuple[DashboardWorkloadItem, ...]

    @model_validator(mode="after")
    def _summary_totals(self) -> DashboardSummaryData:
        for project in self.projects:
            counts = project.state_counts
            if (
                sum(
                    (
                        counts.completed,
                        counts.residual_risk,
                        counts.running,
                        counts.waiting,
                        counts.blocked,
                        counts.aborted,
                        counts.other,
                    )
                )
                != project.total
            ):
                raise ValueError("project stateCounts must sum to total")
        if sum(project.total for project in self.projects) != self.totals.runs_started:
            raise ValueError("project totals must sum to runsStarted")
        if sum(item.run_count for item in self.workload) != self.totals.runs_started:
            raise ValueError("workload counts must sum to runsStarted")
        if sum(project.state_counts.completed for project in self.projects) != (
            self.totals.completed
        ):
            raise ValueError("project completed counts must match totals")
        if sum(project.state_counts.residual_risk for project in self.projects) != (
            self.totals.residual_risk
        ):
            raise ValueError("project residualRisk counts must match totals")
        if (
            sum(
                project.state_counts.waiting + project.state_counts.blocked
                for project in self.projects
            )
            != self.totals.waiting_blocked
        ):
            raise ValueError("project waiting/blocked counts must match totals")
        return self


class DashboardSequenceBinding(PublicWireModel):
    sequence_id: str = Field(alias="sequenceId", min_length=1)
    sequence_name: str = Field(alias="sequenceName", min_length=1)
    sequence_short_name: str = Field(alias="sequenceShortName", min_length=1)
    ordinal: StrictInt = Field(ge=1)
    phase_name: str = Field(alias="phaseName", min_length=1)
    generation: StrictInt = Field(ge=1)
    attempt_kind: str = Field(alias="attemptKind", min_length=1)
    source_run_id: str | None = Field(alias="sourceRunId")


class DashboardRunItem(PublicWireModel):
    run_id: str = Field(alias="runId", min_length=1)
    short_name: str = Field(alias="shortName", min_length=1)
    project_name: str = Field(alias="projectName", min_length=1)
    implementer_provider: Literal["cursor", "codex"] = Field(alias="implementerProvider")
    state: str = Field(min_length=1)
    submitted_at: str = Field(alias="submittedAt")
    first_execution_launch_at: str = Field(alias="firstExecutionLaunchAt")
    sequence_binding: DashboardSequenceBinding | None = Field(alias="sequenceBinding")


class DashboardRunsData(PublicWireModel):
    observed_at: str = Field(alias="observedAt")
    filters: DashboardFiltersData
    coverage: DashboardCoverageData
    items: tuple[DashboardRunItem, ...]
    page: DashboardRunsPageData
    total_count: StrictInt = Field(alias="totalCount", ge=0)


class DashboardActivityDay(PublicWireModel):
    date: str
    value: StrictInt | float

    @field_validator("value")
    @classmethod
    def _finite_nonnegative_value(cls, value: int | float) -> int | float:
        if isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
            raise ValueError("activity value must be finite and nonnegative")
        return value


class DashboardActivityData(PublicWireModel):
    observed_at: str = Field(alias="observedAt")
    filters: DashboardFiltersData
    coverage: DashboardCoverageData
    metric: Literal["submissions", "review-rounds", "process-hours"]
    unit: Literal["count", "process-seconds"]
    days: tuple[DashboardActivityDay, ...]
    total: StrictInt | float

    @field_validator("total")
    @classmethod
    def _finite_nonnegative_total(cls, value: int | float) -> int | float:
        if isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
            raise ValueError("activity total must be finite and nonnegative")
        return value

    @model_validator(mode="after")
    def _metric_invariants(self) -> DashboardActivityData:
        expected_unit = "process-seconds" if self.metric == "process-hours" else "count"
        if self.unit != expected_unit:
            raise ValueError("activity unit does not match metric")
        if self.unit == "count" and (
            not isinstance(self.total, int)
            or any(not isinstance(item.value, int) for item in self.days)
        ):
            raise ValueError("count activity values must be integers")
        if [item.date for item in self.days] != sorted({item.date for item in self.days}):
            raise ValueError("activity days must be unique and chronological")
        if not math.isclose(
            float(self.total),
            sum(float(item.value) for item in self.days),
            rel_tol=0,
            abs_tol=1e-6,
        ):
            raise ValueError("activity total must equal the returned day values")
        return self
