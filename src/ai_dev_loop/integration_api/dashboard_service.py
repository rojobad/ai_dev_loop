"""Integration API projection over shared dashboard queries."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ai_dev_loop.dashboard.adapters.sqlite_read import SqliteDashboardReadRepository
from ai_dev_loop.dashboard.application.ports import (
    DashboardReadError,
    DashboardReadErrorKind,
)
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.domain.filters import (
    DASHBOARD_DEFAULT_LIMIT,
    DEFAULT_TIME_ZONE,
    PROJECT_NAME_UNSET,
    DashboardFilterError,
    DashboardFilters,
    DashboardPage,
)
from ai_dev_loop.dashboard.domain.metrics import DashboardMetric
from ai_dev_loop.integration_api.dashboard_models import (
    DashboardActivityData,
    DashboardProjectsData,
    DashboardRunsData,
    DashboardSummaryData,
)
from ai_dev_loop.integration_api.envelope import build_success_envelope
from ai_dev_loop.integration_api.errors import IntegrationApiError

DASHBOARD_MAX_ENVELOPE_BYTES = 240 * 1024
_DASHBOARD_SCOPE_ENVELOPE_MESSAGE = (
    "Dashboard result is too large; narrow the project or time range."
)
_DASHBOARD_CATALOG_ENVELOPE_MESSAGE = (
    "Dashboard project catalog is too large; retry or reduce the page size."
)


def _map_read_error(exc: DashboardReadError) -> IntegrationApiError:
    if exc.kind is DashboardReadErrorKind.INVALID_ARGUMENT:
        return IntegrationApiError.invalid_argument(exc.message)
    if exc.kind is DashboardReadErrorKind.UNSUPPORTED:
        return IntegrationApiError.unsupported(exc.message)
    if exc.kind is DashboardReadErrorKind.DATA_INTEGRITY:
        return IntegrationApiError.data_integrity(exc.message)
    if exc.kind is DashboardReadErrorKind.IO:
        return IntegrationApiError.io_error(exc.message)
    return IntegrationApiError.internal()


class IntegrationDashboardReadService:
    def __init__(
        self,
        queries: DashboardQueries,
        *,
        envelope_max_bytes: int = DASHBOARD_MAX_ENVELOPE_BYTES,
    ) -> None:
        if envelope_max_bytes < 1:
            raise ValueError("envelope_max_bytes must be positive")
        self.queries = queries
        self.envelope_max_bytes = envelope_max_bytes

    def _bounded(
        self,
        model: (
            DashboardProjectsData | DashboardSummaryData | DashboardRunsData | DashboardActivityData
        ),
        *,
        oversize_message: str = _DASHBOARD_SCOPE_ENVELOPE_MESSAGE,
    ) -> DashboardProjectsData | DashboardSummaryData | DashboardRunsData | DashboardActivityData:
        payload = model.model_dump(mode="json", by_alias=True)
        envelope = build_success_envelope(
            payload,
            observed_at=datetime(2000, 1, 1, 0, 0, 0, 999999, tzinfo=UTC),
        )
        encoded = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.envelope_max_bytes:
            raise IntegrationApiError.invalid_argument(oversize_message)
        return model

    def projects(
        self,
        *,
        offset: object = 0,
        limit: object = DASHBOARD_DEFAULT_LIMIT,
    ) -> DashboardProjectsData:
        try:
            page = DashboardPage.parse(offset=offset, limit=limit)
            result = self.queries.projects(page)
        except DashboardFilterError as exc:
            raise IntegrationApiError.invalid_argument(str(exc)) from exc
        except DashboardReadError as exc:
            raise _map_read_error(exc) from exc
        return cast(
            DashboardProjectsData,
            self._bounded(
                DashboardProjectsData.model_validate(asdict(result)),
                oversize_message=_DASHBOARD_CATALOG_ENVELOPE_MESSAGE,
            ),
        )

    @staticmethod
    def _filters(
        *,
        from_value: object,
        to_value: object,
        project_name: object,
        time_zone: object,
    ) -> DashboardFilters:
        try:
            return DashboardFilters.parse(
                from_value=from_value,
                to_value=to_value,
                project_name=project_name,
                time_zone=time_zone,
            )
        except DashboardFilterError as exc:
            raise IntegrationApiError.invalid_argument(str(exc)) from exc

    def summary(
        self,
        *,
        from_value: object,
        to_value: object,
        project_name: object = PROJECT_NAME_UNSET,
        time_zone: object = DEFAULT_TIME_ZONE,
    ) -> DashboardSummaryData:
        filters = self._filters(
            from_value=from_value,
            to_value=to_value,
            project_name=project_name,
            time_zone=time_zone,
        )
        try:
            result = self.queries.summary(filters)
        except DashboardReadError as exc:
            raise _map_read_error(exc) from exc
        return cast(
            DashboardSummaryData,
            self._bounded(DashboardSummaryData.model_validate(asdict(result))),
        )

    def runs(
        self,
        *,
        from_value: object,
        to_value: object,
        project_name: object = PROJECT_NAME_UNSET,
        time_zone: object = DEFAULT_TIME_ZONE,
        offset: object = 0,
        limit: object = DASHBOARD_DEFAULT_LIMIT,
    ) -> DashboardRunsData:
        filters = self._filters(
            from_value=from_value,
            to_value=to_value,
            project_name=project_name,
            time_zone=time_zone,
        )
        try:
            page = DashboardPage.parse(offset=offset, limit=limit)
            result = self.queries.runs(filters, page)
        except DashboardFilterError as exc:
            raise IntegrationApiError.invalid_argument(str(exc)) from exc
        except DashboardReadError as exc:
            raise _map_read_error(exc) from exc
        return cast(
            DashboardRunsData,
            self._bounded(DashboardRunsData.model_validate(asdict(result))),
        )

    def activity(
        self,
        *,
        from_value: object,
        to_value: object,
        metric: object,
        project_name: object = PROJECT_NAME_UNSET,
        time_zone: object = DEFAULT_TIME_ZONE,
    ) -> DashboardActivityData:
        filters = self._filters(
            from_value=from_value,
            to_value=to_value,
            project_name=project_name,
            time_zone=time_zone,
        )
        if not isinstance(metric, str) or metric not in {
            "submissions",
            "review-rounds",
            "process-hours",
        }:
            raise IntegrationApiError.invalid_argument(
                "metric must be submissions, review-rounds, or process-hours."
            )
        try:
            result = self.queries.activity(filters, cast(DashboardMetric, metric))
        except DashboardReadError as exc:
            raise _map_read_error(exc) from exc
        except ValueError as exc:
            raise IntegrationApiError.data_integrity(
                "Scheduler dashboard attempt data failed integrity validation."
            ) from exc
        return cast(
            DashboardActivityData,
            self._bounded(DashboardActivityData.model_validate(asdict(result))),
        )


def default_dashboard_read_service(
    *,
    db_path: Path | None = None,
) -> IntegrationDashboardReadService:
    repository = SqliteDashboardReadRepository(db_path=db_path)
    return IntegrationDashboardReadService(DashboardQueries(repository))
