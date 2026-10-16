"""Pure dashboard state, execution, and activity metric rules."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from ai_dev_loop.dashboard.domain.filters import DashboardFilters
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
    RESUME_CODEX_REVIEW_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.cursor_contract import (
    CREATE_CHAT_EFFECT_KIND,
    INGEST_CURSOR_RESULT_EFFECT_KIND,
    NORMALIZE_STAGING_EFFECT_KIND,
    PREFLIGHT_EFFECT_KIND,
    RUN_CURSOR_TURN_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.effects import (
    FAKE_AGENT_SELF_TEST_EFFECT_KIND,
    SYNTHETIC_SELF_TEST_EFFECT_KIND,
)

DashboardMetric = Literal["submissions", "review-rounds", "process-hours"]
ExecutionRole = Literal["implementer", "reviewer"]


class RunMetricInput(Protocol):
    @property
    def run_id(self) -> str: ...


class AttemptMetricInput(Protocol):
    @property
    def run_id(self) -> str: ...

    @property
    def component(self) -> str: ...

    @property
    def effect_kind(self) -> str | None: ...

    @property
    def iteration(self) -> int: ...

    @property
    def status(self) -> str: ...

    @property
    def launch_requested_at(self) -> datetime | None: ...

    @property
    def completed_at(self) -> datetime | None: ...

    @property
    def ingested(self) -> bool: ...


EXECUTION_EFFECT_OWNERS: dict[str, tuple[str, ExecutionRole]] = {
    RUN_CURSOR_TURN_EFFECT_KIND: ("cursor", "implementer"),
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND: ("codex_implementer", "implementer"),
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND: ("codex_implementer", "implementer"),
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND: ("codex", "reviewer"),
    RESUME_CODEX_REVIEW_EFFECT_KIND: ("codex", "reviewer"),
}
KNOWN_NON_EXECUTION_EFFECTS = frozenset(
    {
        CREATE_CHAT_EFFECT_KIND,
        PREFLIGHT_EFFECT_KIND,
        INGEST_CURSOR_RESULT_EFFECT_KIND,
        NORMALIZE_STAGING_EFFECT_KIND,
        SYNTHETIC_SELF_TEST_EFFECT_KIND,
        FAKE_AGENT_SELF_TEST_EFFECT_KIND,
    }
)

STATE_GROUPS = (
    "completed",
    "residual-risk",
    "running",
    "waiting",
    "blocked",
    "aborted",
    "other",
)
_STATE_GROUP_BY_RAW = {
    "completed": "completed",
    "completed_with_residual_risk": "residual-risk",
    "preflight_complete": "running",
    "cursor_ready": "running",
    "awaiting_codex_review": "running",
    "waiting_for_cursor_fix": "running",
    "checkpoint_pending": "running",
    "queued": "waiting",
    "authorized": "waiting",
    "admitted": "waiting",
    "waiting_usage_limit": "waiting",
    "waiting_codex_capacity": "waiting",
    "waiting_codex_review_retry": "waiting",
    "pending_sequence_review_recovery": "waiting",
    "blocked": "blocked",
    "max_iterations_reached": "blocked",
    "aborted": "aborted",
}


@dataclass(frozen=True)
class CoverageCounts:
    unstarted_runs: int = 0
    missing_launch_times: int = 0
    unknown_execution_kinds: int = 0
    missing_duration_attempts: int = 0
    partial_evidence: bool = False


def state_group(raw_state: str) -> str:
    """Map reviewed scheduler state variants; new values remain Other."""
    return _STATE_GROUP_BY_RAW.get(raw_state, "other")


def execution_role(attempt: AttemptMetricInput) -> ExecutionRole | None:
    if attempt.effect_kind is None:
        return None
    owner = EXECUTION_EFFECT_OWNERS.get(attempt.effect_kind)
    if owner is None or owner[0] != attempt.component:
        return None
    return owner[1]


def is_unknown_execution(attempt: AttemptMetricInput) -> bool:
    kind = attempt.effect_kind
    if kind in KNOWN_NON_EXECUTION_EFFECTS:
        return False
    owner = EXECUTION_EFFECT_OWNERS.get(kind or "")
    return owner is None or owner[0] != attempt.component


def first_execution_launches(
    runs: tuple[RunMetricInput, ...],
    attempts: tuple[AttemptMetricInput, ...],
) -> tuple[dict[str, datetime], CoverageCounts]:
    launches: dict[str, datetime] = {}
    missing_launch = 0
    unknown = 0
    for attempt in attempts:
        role = execution_role(attempt)
        if role is None:
            if is_unknown_execution(attempt):
                unknown += 1
            continue
        if attempt.launch_requested_at is None:
            missing_launch += 1
            continue
        existing = launches.get(attempt.run_id)
        if existing is None or attempt.launch_requested_at < existing:
            launches[attempt.run_id] = attempt.launch_requested_at
    return launches, CoverageCounts(
        unstarted_runs=sum(1 for run in runs if run.run_id not in launches),
        missing_launch_times=missing_launch,
        unknown_execution_kinds=unknown,
        partial_evidence=missing_launch > 0 or unknown > 0,
    )


def first_review_round_launches(
    attempts: tuple[AttemptMetricInput, ...],
) -> tuple[dict[tuple[str, int], datetime], int]:
    rounds: dict[tuple[str, int], datetime] = {}
    missing = 0
    for attempt in attempts:
        if execution_role(attempt) != "reviewer":
            continue
        if attempt.launch_requested_at is None:
            missing += 1
            continue
        key = (attempt.run_id, attempt.iteration)
        existing = rounds.get(key)
        if existing is None or attempt.launch_requested_at < existing:
            rounds[key] = attempt.launch_requested_at
    return rounds, missing


def resolved_process_interval(
    attempt: AttemptMetricInput,
) -> tuple[datetime, datetime] | None:
    if execution_role(attempt) is None:
        return None
    launch = attempt.launch_requested_at
    end = attempt.completed_at
    resolved = attempt.status in {"completed", "failed"} or (
        attempt.status == "cancelled" and attempt.ingested
    )
    if not resolved or launch is None or end is None:
        return None
    if end < launch:
        raise ValueError("attempt completion precedes launch")
    return launch, end


def _next_existing_local_midnight(value: datetime, filters: DashboardFilters) -> datetime:
    local_date = value.astimezone(filters.zone).date() + timedelta(days=1)
    for _ in range(3):
        candidate = datetime.combine(local_date, time.min, tzinfo=filters.zone)
        utc = candidate.astimezone(UTC)
        if utc.astimezone(filters.zone).date() >= local_date and utc > value:
            return utc
        local_date += timedelta(days=1)
    return value + timedelta(days=1)


def process_second_buckets(
    attempts: tuple[AttemptMetricInput, ...],
    filters: DashboardFilters,
) -> tuple[dict[str, float], int, bool]:
    buckets: dict[str, float] = defaultdict(float)
    missing_duration = 0
    partial = False
    for attempt in attempts:
        owned_execution = execution_role(attempt) is not None
        if not owned_execution:
            continue
        resolved = attempt.status in {"completed", "failed"} or (
            attempt.status == "cancelled" and attempt.ingested
        )
        interval = resolved_process_interval(attempt)
        if interval is None:
            missing_duration += 1
            if resolved:
                partial = True
            continue
        launch, end = interval
        start = max(launch, filters.from_utc)
        finish = min(end, filters.to_utc)
        if start >= finish:
            continue
        cursor = start
        while cursor < finish:
            boundary = min(_next_existing_local_midnight(cursor, filters), finish)
            if boundary <= cursor:
                raise ValueError("local day boundary did not advance")
            key = cursor.astimezone(filters.zone).date().isoformat()
            buckets[key] += (boundary - cursor).total_seconds()
            cursor = boundary
    return dict(buckets), missing_duration, partial


def local_dates(filters: DashboardFilters) -> tuple[date, ...]:
    first = filters.from_utc.astimezone(filters.zone).date()
    last = (filters.to_utc - timedelta(microseconds=1)).astimezone(filters.zone).date()
    return tuple(first + timedelta(days=index) for index in range((last - first).days + 1))
