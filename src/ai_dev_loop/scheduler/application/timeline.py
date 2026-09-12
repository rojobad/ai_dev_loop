"""Bounded read-only scheduler attempt timeline projection."""

from __future__ import annotations

from pathlib import Path

from ai_dev_loop.scheduler.application.contracts import (
    DEFAULT_TIMELINE_LIMIT,
    HARD_TIMELINE_MAX,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
    TimelineEntry,
    TimelineResult,
)
from ai_dev_loop.scheduler.domain.common import coerce_utc_instant
from ai_dev_loop.scheduler.infrastructure.paths import default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_TIMELINE_ACTIVE_STATUSES = frozenset({"launching", "active"})
_TIMELINE_TERMINAL_STATUSES = frozenset({"completed", "failed", "uncertain", "cancelled"})


def _timeline_status(raw_status: str) -> str:
    if raw_status in _TIMELINE_ACTIVE_STATUSES:
        return "active"
    if raw_status in _TIMELINE_TERMINAL_STATUSES:
        return raw_status
    return raw_status


def _observed_duration_seconds(
    launch_requested_at: str | None,
    completed_at: str | None,
) -> float | None:
    if launch_requested_at is None or completed_at is None:
        return None
    start = coerce_utc_instant(launch_requested_at)
    end = coerce_utc_instant(completed_at)
    return round((end - start).total_seconds(), 3)


class SchedulerTimelineService:
    def __init__(self, store: SqliteSchedulerStore) -> None:
        self.store = store

    def get_timeline(
        self,
        run_id: str,
        *,
        limit: int = DEFAULT_TIMELINE_LIMIT,
        order: str = "oldest",
    ) -> TimelineResult:
        if limit < 1:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "timeline limit must be positive",
            )
        if order not in {"oldest", "newest"}:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "timeline order must be oldest or newest",
            )
        capped = min(limit, HARD_TIMELINE_MAX)
        newest_first = order == "newest"
        with self.store.begin_read() as conn:
            self.store.load_validated_snapshot(conn, run_id)
            rows = self.store.list_attempt_timeline_rows(
                conn,
                run_id,
                limit=capped + 1,
                newest_first=newest_first,
            )
        truncated = len(rows) > capped
        selected_rows = rows[:capped]
        entries = tuple(
            TimelineEntry(
                iteration=int(row["iteration"]),
                phase=str(row["component"]),
                phase_attempt=int(row["phase_attempt"]),
                status=_timeline_status(str(row["status"])),
                launch_requested_at=(
                    str(row["launch_requested_at"])
                    if row["launch_requested_at"] is not None
                    else None
                ),
                completed_at=(
                    str(row["completed_at"]) if row["completed_at"] is not None else None
                ),
                observed_duration_seconds=_observed_duration_seconds(
                    str(row["launch_requested_at"])
                    if row["launch_requested_at"] is not None
                    else None,
                    str(row["completed_at"]) if row["completed_at"] is not None else None,
                ),
            )
            for row in selected_rows
        )
        return TimelineResult(
            run_id=run_id,
            order=order,
            limit=capped,
            truncated=truncated,
            entries=entries,
        )


def default_timeline_service(*, db_path: Path | None = None) -> SchedulerTimelineService:
    path = db_path or default_engine_db_path()
    return SchedulerTimelineService(SqliteSchedulerStore.open_readonly(path))


def scheduler_timeline(
    run_id: str,
    *,
    limit: int = DEFAULT_TIMELINE_LIMIT,
    order: str = "oldest",
    db_path: Path | None = None,
) -> TimelineResult:
    path = db_path or default_engine_db_path()
    if not path.exists():
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.NOT_FOUND,
            "scheduler database not found",
        )
    return default_timeline_service(db_path=path).get_timeline(
        run_id,
        limit=limit,
        order=order,
    )
