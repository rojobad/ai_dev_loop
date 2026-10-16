from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from ai_dev_loop.dashboard.application.ports import (
    AttemptObservation,
    DashboardObservationSet,
    DashboardReadScope,
    ProjectCatalogObservation,
    ProjectCountObservation,
    RunObservation,
    SequenceBindingObservation,
)
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.domain.filters import (
    DashboardFilterError,
    DashboardFilters,
    DashboardPage,
)
from ai_dev_loop.integration_api.dashboard_models import DashboardSummaryData
from ai_dev_loop.integration_api.schemas import (
    DASHBOARD_SUMMARY_DATA_SCHEMA,
    validate_integration_instance,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


class FakeRepository:
    def __init__(
        self,
        runs: tuple[RunObservation, ...],
        attempts: tuple[AttemptObservation, ...],
    ) -> None:
        self.runs = runs
        self.attempts = attempts

    def read_project_catalog(self, *, offset: int, limit: int) -> ProjectCatalogObservation:
        counts: dict[str, int] = {}
        for run in self.runs:
            counts[run.project_name] = counts.get(run.project_name, 0) + 1
        items = tuple(
            ProjectCountObservation(name, count) for name, count in sorted(counts.items())
        )
        return ProjectCatalogObservation(
            items=items[offset : offset + limit], total_count=len(items)
        )

    def read_observations(self, scope: DashboardReadScope) -> DashboardObservationSet:
        runs = tuple(
            run
            for run in self.runs
            if scope.project_name is None or run.project_name == scope.project_name
        )
        ids = {run.run_id for run in runs}
        return DashboardObservationSet(
            runs=runs,
            attempts=tuple(attempt for attempt in self.attempts if attempt.run_id in ids),
        )


def _run(
    run_id: str,
    *,
    state: str = "completed",
    submitted: datetime = BASE,
    provider: str = "cursor",
    binding: SequenceBindingObservation | None = None,
) -> RunObservation:
    return RunObservation(run_id, "project-a", provider, state, submitted, binding)


def _attempt(
    run_id: str,
    launch: datetime | None,
    *,
    effect: str = "cursor.run_turn",
    component: str = "cursor",
    iteration: int = 1,
    completed: datetime | None = None,
    status: str = "completed",
    ingested: bool = True,
) -> AttemptObservation:
    return AttemptObservation(
        run_id=run_id,
        project_name="project-a",
        component=component,
        effect_kind=effect,
        iteration=iteration,
        status=status,
        launch_requested_at=launch,
        completed_at=completed if completed is not None else launch,
        ingested=ingested,
    )


def _filters() -> DashboardFilters:
    return DashboardFilters.parse(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-01-03T00:00:00Z",
        time_zone="UTC",
        project_name="project-a",
    )


def test_filter_and_page_bounds_are_strict_and_normalize_instants() -> None:
    filters = DashboardFilters.parse(
        from_value="2025-12-31T20:00:00-04:00",
        to_value="2026-01-02T00:00:00Z",
        time_zone="America/La_Paz",
    )
    assert filters.from_text == "2026-01-01T00:00:00Z"
    for kwargs in (
        {"from_value": "2026-01-01T00:00:00Z", "to_value": "2027-01-03T00:00:00Z"},
        {"from_value": "2026-01-01 00:00:00Z", "to_value": "2026-01-02T00:00:00Z"},
        {"from_value": "20260101T000000Z", "to_value": "2026-01-02T00:00:00Z"},
        {
            "from_value": "2026-01-01T00:00:00Z",
            "to_value": "2026-01-02T00:00:00Z",
            "project_name": None,
        },
        {
            "from_value": "2026-01-01T00:00:00Z",
            "to_value": "2026-01-02T00:00:00Z",
            "time_zone": "Not/AZone",
        },
    ):
        try:
            DashboardFilters.parse(**kwargs)
        except DashboardFilterError:
            pass
        else:
            raise AssertionError("invalid dashboard filter was accepted")
    for offset, limit in ((True, 25), (0, False), (-1, 25), (0, 101)):
        try:
            DashboardPage.parse(offset=offset, limit=limit)
        except DashboardFilterError:
            pass
        else:
            raise AssertionError("invalid dashboard page was accepted")


def test_started_cohort_uses_first_execution_and_totals_outlive_first_map_page() -> None:
    runs = []
    attempts = []
    for index in range(27):
        run_id = f"run-{index:02d}"
        state = "future_state" if index == 0 else "completed"
        binding = None
        if index in {1, 2}:
            binding = SequenceBindingObservation(
                sequence_id="sequence-a",
                sequence_name="Sequence A",
                ordinal=1,
                phase_name="phase-a",
                generation=index,
                attempt_kind="planned_run" if index == 1 else "implementer_retry",
                source_run_id=None if index == 1 else "run-01",
            )
        runs.append(
            _run(
                run_id,
                state=state,
                provider="codex" if index == 2 else "cursor",
                binding=binding,
            )
        )
        attempts.append(
            _attempt(
                run_id,
                BASE + timedelta(hours=1, minutes=index),
                component=("codex_implementer" if index == 2 else "cursor"),
                effect=("codex.implementer_bootstrap" if index == 2 else "cursor.run_turn"),
            )
        )
    runs.extend(
        [
            _run("queued-only", state="queued"),
            _run("pre-range", submitted=BASE - timedelta(days=2)),
        ]
    )
    attempts.extend(
        [
            _attempt("run-00", BASE - timedelta(hours=1), effect="cursor.create_chat"),
            _attempt("pre-range", BASE - timedelta(days=1)),
        ]
    )
    query = DashboardQueries(
        FakeRepository(tuple(runs), tuple(attempts)),
        now=lambda: BASE,
    )
    summary = query.summary(_filters())
    page = query.runs(_filters(), DashboardPage.parse(offset=0, limit=25))
    assert summary.totals.runs_started == 27
    assert summary.projects[0].total == 27
    assert summary.projects[0].state_counts.other == 1
    assert page.total_count == 27 and len(page.items) == 25 and page.page.has_more
    assert summary.coverage.unstarted_runs == 1
    all_items = query.runs(_filters(), DashboardPage.parse(offset=0, limit=100)).items
    replacement = next(item for item in all_items if item.run_id == "run-02")
    assert replacement.implementer_provider == "codex"
    assert replacement.sequence_binding is not None
    assert replacement.sequence_binding.attempt_kind == "implementer_retry"
    assert replacement.sequence_binding.source_run_id == "run-01"


def test_activity_is_event_dated_and_review_rounds_do_not_use_started_cohort() -> None:
    old_run = _run("old-run", submitted=BASE - timedelta(days=4))
    successor = _run("successor", submitted=BASE)
    attempts = (
        _attempt("old-run", BASE - timedelta(days=3)),
        _attempt(
            "old-run",
            BASE + timedelta(hours=1),
            component="codex",
            effect="codex.bootstrap_review",
            iteration=1,
        ),
        _attempt(
            "old-run",
            BASE + timedelta(hours=2),
            component="codex_implementer",
            effect="codex.implementer_resume",
            iteration=1,
        ),
        _attempt(
            "old-run",
            BASE + timedelta(hours=3),
            component="codex",
            effect="codex.resume_review",
            iteration=1,
        ),
        _attempt(
            "old-run",
            BASE + timedelta(hours=4),
            component="codex",
            effect="codex.resume_review",
            iteration=2,
        ),
        _attempt(
            "successor",
            BASE + timedelta(hours=5),
            component="codex",
            effect="codex.resume_review",
            iteration=1,
        ),
    )
    query = DashboardQueries(FakeRepository((old_run, successor), attempts), now=lambda: BASE)
    activity = query.activity(_filters(), "review-rounds")
    assert activity.total == 3
    assert activity.days[0].value == 3
    assert activity.days[1].value == 0


def test_unknown_project_is_complete_empty_and_catalog_is_authoritative() -> None:
    repository = FakeRepository((_run("run-a"),), ())
    query = DashboardQueries(repository, now=lambda: BASE)
    catalog = query.projects(DashboardPage.parse(offset=0, limit=25))
    unknown = DashboardFilters.parse(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-01-02T00:00:00Z",
        project_name="unknown",
        time_zone="UTC",
    )
    summary = query.summary(unknown)
    assert [(item.project_name, item.run_count) for item in catalog.items] == [("project-a", 1)]
    assert summary.totals.runs_started == 0
    assert summary.coverage.data_state == "empty"


def test_c264_4_summary_produces_exact_raw_other_states_and_schema_rejects_bad_sum() -> None:
    malicious = '<future-state onmouseover="window.bad=1">'
    runs = (
        _run("unknown-a", state=malicious),
        _run("unknown-b", state="future_state"),
        _run("known", state="completed"),
    )
    attempts = tuple(
        _attempt(run.run_id, BASE + timedelta(hours=index + 1)) for index, run in enumerate(runs)
    )
    summary = DashboardQueries(
        FakeRepository(runs, attempts),
        now=lambda: BASE,
    ).summary(_filters())
    public = DashboardSummaryData.model_validate(asdict(summary))
    wire = public.model_dump(mode="json", by_alias=True)

    assert wire["projects"][0]["stateCounts"]["other"] == 2
    assert wire["projects"][0]["otherStates"] == [
        {"state": malicious, "count": 1},
        {"state": "future_state", "count": 1},
    ]
    assert sum(item["count"] for item in wire["projects"][0]["otherStates"]) == 2
    validate_integration_instance(wire, DASHBOARD_SUMMARY_DATA_SCHEMA)

    inconsistent = deepcopy(asdict(summary))
    inconsistent_project = inconsistent["projects"][0]
    inconsistent_project["other_states"] = (
        {"state": malicious, "count": 2},
        {"state": "future_state", "count": 1},
    )
    with pytest.raises(ValidationError, match="otherStates counts"):
        DashboardSummaryData.model_validate(inconsistent)
