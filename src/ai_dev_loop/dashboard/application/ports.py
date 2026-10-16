"""Typed read-side observations consumed by dashboard queries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class DashboardReadErrorKind(StrEnum):
    INVALID_ARGUMENT = "invalid_argument"
    UNSUPPORTED = "unsupported"
    DATA_INTEGRITY = "data_integrity"
    IO = "io"
    INTERNAL = "internal"


class DashboardReadError(Exception):
    def __init__(self, kind: DashboardReadErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class DashboardReadOperation(StrEnum):
    SUMMARY = "summary"
    RUNS = "runs"
    ACTIVITY_SUBMISSIONS = "activity_submissions"
    ACTIVITY_REVIEW_ROUNDS = "activity_review_rounds"
    ACTIVITY_PROCESS_HOURS = "activity_process_hours"


@dataclass(frozen=True)
class DashboardReadScope:
    operation: DashboardReadOperation
    project_name: str | None
    from_utc: datetime
    to_utc: datetime


@dataclass(frozen=True)
class SequenceBindingObservation:
    sequence_id: str
    sequence_name: str
    ordinal: int
    phase_name: str
    generation: int
    attempt_kind: str
    source_run_id: str | None
    sequence_short_name: str = "sequence"


@dataclass(frozen=True)
class RunObservation:
    run_id: str
    project_name: str
    implementer_provider: str
    state: str
    submitted_at: datetime
    sequence_binding: SequenceBindingObservation | None
    short_name: str = "run"


@dataclass(frozen=True)
class AttemptObservation:
    run_id: str
    project_name: str
    component: str
    effect_kind: str | None
    iteration: int
    status: str
    launch_requested_at: datetime | None
    completed_at: datetime | None
    ingested: bool


@dataclass(frozen=True)
class DashboardObservationSet:
    runs: tuple[RunObservation, ...]
    attempts: tuple[AttemptObservation, ...]


@dataclass(frozen=True)
class ProjectCountObservation:
    project_name: str
    run_count: int


@dataclass(frozen=True)
class ProjectCatalogObservation:
    items: tuple[ProjectCountObservation, ...]
    total_count: int


class DashboardReadPort(Protocol):
    def read_project_catalog(self, *, offset: int, limit: int) -> ProjectCatalogObservation: ...

    def read_observations(self, scope: DashboardReadScope) -> DashboardObservationSet: ...
