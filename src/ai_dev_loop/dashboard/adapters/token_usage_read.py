"""Read-only adapter from dashboard token queries to the authenticated usage collector."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ai_dev_loop.dashboard.adapters.sqlite_read import (
    DEFAULT_QUERY_BUDGET_SECONDS,
    SqliteDashboardReadRepository,
    _map_scheduler_error,
)
from ai_dev_loop.dashboard.application.ports import DashboardReadPort
from ai_dev_loop.dashboard.application.token_usage import (
    DEFAULT_TOKEN_MAX_ATTEMPTS,
    DEFAULT_TOKEN_MAX_RUNS,
    DEFAULT_TOKEN_QUERY_SECONDS,
    DirectUsageReader,
    TokenUsageQueries,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.token_usage_query import (
    DirectUsageEvidence,
    TokenUsageReadService,
    TokenUsageScopeLimit,
)
from ai_dev_loop.scheduler.infrastructure.paths import default_artifact_root, default_engine_db_path
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


class SqliteDirectUsageReader:
    """Collect direct usage without migrating the ledger or calling a provider."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        artifact_root: Path | None = None,
    ) -> None:
        self.db_path = db_path or default_engine_db_path()
        self.artifact_root = artifact_root or default_artifact_root()

    def collect(
        self,
        run_ids: tuple[str, ...],
        *,
        max_attempts: int,
        checkpoint: Callable[[], None],
    ) -> DirectUsageEvidence:
        try:
            service = TokenUsageReadService(
                SqliteSchedulerStore.open_readonly(self.db_path),
                artifact_root=self.artifact_root,
            )
            return service.collect_direct(
                run_ids,
                max_attempts=max_attempts,
                checkpoint=checkpoint,
            )
        except TokenUsageScopeLimit:
            raise
        except SchedulerEngineError as exc:
            raise _map_scheduler_error(exc) from exc


def token_usage_queries(
    *,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    monotonic: Callable[[], float] | None = None,
    cancelled: Callable[[], bool] | None = None,
    budget_seconds: float = DEFAULT_TOKEN_QUERY_SECONDS,
    max_runs: int = DEFAULT_TOKEN_MAX_RUNS,
    max_attempts: int = DEFAULT_TOKEN_MAX_ATTEMPTS,
    now: Callable[[], datetime] | None = None,
) -> TokenUsageQueries:
    """Build the production query with one shared deadline for SQL and artifacts."""

    clock = monotonic
    stop = cancelled

    def cohort_factory(deadline: float) -> DashboardReadPort:
        return SqliteDashboardReadRepository(
            db_path=db_path,
            query_budget_seconds=budget_seconds or DEFAULT_QUERY_BUDGET_SECONDS,
            monotonic=clock or _monotonic,
            cancelled=stop,
            absolute_deadline=deadline,
        )

    def usage_factory() -> DirectUsageReader:
        return SqliteDirectUsageReader(db_path=db_path, artifact_root=artifact_root)

    return TokenUsageQueries(
        cohort_factory,
        usage_factory,
        now=now,
        monotonic=clock,
        cancelled=stop,
        budget_seconds=budget_seconds,
        max_runs=max_runs,
        max_attempts=max_attempts,
    )


def _monotonic() -> float:
    import time

    return time.monotonic()
