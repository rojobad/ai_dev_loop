"""Phase 28.3 operation-specific dashboard paging contracts."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError
from tests.integration.test_phase26_1_dashboard_queries import _fixture_ledger
from tests.integration.test_phase26_2_dashboard_server import _request
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.dashboard.adapters.http import (
    DashboardHttpDispatcher,
    HttpServiceFactories,
    default_http_service_factories,
)
from ai_dev_loop.dashboard.application.ports import (
    AttemptObservation,
    DashboardObservationSet,
    DashboardReadScope,
    ProjectCatalogObservation,
    RunObservation,
)
from ai_dev_loop.dashboard.application.queries import DashboardQueries
from ai_dev_loop.dashboard.server import DashboardServer
from ai_dev_loop.integration_api.dashboard_models import (
    DashboardPageData,
    DashboardProjectsData,
)
from ai_dev_loop.integration_api.dashboard_service import IntegrationDashboardReadService
from ai_dev_loop.integration_api.errors import IntegrationApiError, IntegrationErrorCode
from ai_dev_loop.integration_api.schemas import (
    DASHBOARD_PROJECTS_DATA_SCHEMA,
    DASHBOARD_RUNS_DATA_SCHEMA,
    validate_integration_instance,
)

START = "2026-01-01T00:00:00Z"
END = "2026-01-03T00:00:00Z"
FILTERS = {"fromUtc": START, "toUtc": END, "timeZone": "UTC"}
runner = CliRunner()


def _post(
    server: DashboardServer,
    operation: str,
    parameters: dict[str, object],
) -> tuple[int, dict[str, object]]:
    body = json.dumps({"operation": operation, "parameters": parameters}).encode()
    status, _, payload = _request(
        server.port,
        "POST",
        "/api/dashboard/read",
        body=body,
        headers={
            "Origin": f"http://127.0.0.1:{server.port}",
            "Content-Type": "application/json",
        },
    )
    decoded = json.loads(payload)
    assert isinstance(decoded, dict)
    return status, decoded


def _server(factories: HttpServiceFactories) -> tuple[DashboardServer, threading.Thread]:
    server = DashboardServer(0, dispatcher=DashboardHttpDispatcher(factories))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class _LargeRunRepository:
    """Bounded fake provider large enough to exercise the real 240 KiB envelope."""

    def __init__(self, count: int = 1000) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        project_name = f"large-project-{'p' * 180}"
        self.observation = DashboardObservationSet(
            runs=tuple(
                RunObservation(
                    run_id=f"large-run-{index:04d}-{'r' * 36}",
                    short_name=f"Alias {index:04d} {'a' * 24}",
                    project_name=project_name,
                    implementer_provider="cursor" if index % 2 == 0 else "codex",
                    state="completed",
                    submitted_at=base + timedelta(seconds=index),
                    sequence_binding=None,
                )
                for index in range(count)
            ),
            attempts=tuple(
                AttemptObservation(
                    run_id=f"large-run-{index:04d}-{'r' * 36}",
                    project_name=project_name,
                    component="cursor",
                    effect_kind="cursor.run_turn",
                    iteration=1,
                    status="completed",
                    launch_requested_at=base + timedelta(seconds=index),
                    completed_at=base + timedelta(seconds=index + 1),
                    ingested=True,
                )
                for index in range(count)
            ),
        )

    def read_project_catalog(self, *, offset: int, limit: int) -> ProjectCatalogObservation:
        del offset, limit
        return ProjectCatalogObservation(items=(), total_count=0)

    def read_observations(self, scope: DashboardReadScope) -> DashboardObservationSet:
        del scope
        return self.observation


def test_c283_2_runs_only_bounds_across_service_cli_http_models_and_schemas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    xdg_state = tmp_path / "state"
    db_path = _fixture_ledger(xdg_state)
    service = default_http_service_factories(db_path=db_path).dashboard()

    runs = service.runs(
        from_value=START,
        to_value=END,
        time_zone="UTC",
        limit=1000,
    )
    assert runs.page.limit == 1000
    validate_integration_instance(
        runs.model_dump(mode="json", by_alias=True),
        DASHBOARD_RUNS_DATA_SCHEMA,
    )

    for rejected in (1001, 0, True):
        with pytest.raises(IntegrationApiError) as caught:
            service.runs(
                from_value=START,
                to_value=END,
                time_zone="UTC",
                limit=rejected,
            )
        assert caught.value.code is IntegrationErrorCode.INVALID_ARGUMENT
    with pytest.raises(IntegrationApiError):
        service.projects(limit=101)

    with pytest.raises(ValidationError):
        DashboardPageData.model_validate(
            {"offset": 0, "limit": 1000, "nextOffset": None, "hasMore": False}
        )
    with pytest.raises(ValidationError):
        DashboardProjectsData.model_validate(
            {
                "observedAt": START,
                "items": [],
                "page": {"offset": 0, "limit": 1000, "nextOffset": None, "hasMore": False},
                "totalCount": 0,
            }
        )
    invalid_runs = runs.model_dump(mode="json", by_alias=True)
    invalid_runs["page"]["limit"] = 1001  # type: ignore[index]
    with pytest.raises(jsonschema.ValidationError):
        validate_integration_instance(invalid_runs, DASHBOARD_RUNS_DATA_SCHEMA)
    projects = service.projects(limit=100)
    validate_integration_instance(
        projects.model_dump(mode="json", by_alias=True),
        DASHBOARD_PROJECTS_DATA_SCHEMA,
    )

    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_state))
    accepted = runner.invoke(
        app,
        [
            "integration",
            "dashboard",
            "runs",
            "--from",
            START,
            "--to",
            END,
            "--time-zone",
            "UTC",
            "--limit",
            "1000",
        ],
    )
    assert accepted.exit_code == 0, accepted.stdout
    assert json.loads(accepted.stdout)["data"]["page"]["limit"] == 1000
    rejected = runner.invoke(
        app,
        [
            "integration",
            "dashboard",
            "runs",
            "--from",
            START,
            "--to",
            END,
            "--limit",
            "1001",
        ],
    )
    assert rejected.exit_code == 2
    assert json.loads(rejected.stdout)["error"]["code"] == "INVALID_ARGUMENT"

    factories = default_http_service_factories(db_path=db_path)
    server, thread = _server(factories)
    try:
        status, payload = _post(server, "dashboard.runs", {**FILTERS, "limit": 1000})
        assert status == 200
        assert payload["data"]["page"]["limit"] == 1000  # type: ignore[index]
        for operation, parameters in (
            ("dashboard.runs", {**FILTERS, "limit": 1001}),
            ("dashboard.runs", {**FILTERS, "limit": True}),
            ("dashboard.runs", {**FILTERS, "limit": 0}),
            ("dashboard.projects", {"limit": 101}),
            ("run.timeline", {"runId": "cursor-run", "limit": 101}),
        ):
            status, payload = _post(server, operation, parameters)
            assert status == 400
            assert payload["error"]["code"] == "INVALID_ARGUMENT"  # type: ignore[index]
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_c283_2_oversize_run_page_is_typed_without_raising_any_budget() -> None:
    large_service = IntegrationDashboardReadService(DashboardQueries(_LargeRunRepository()))
    with pytest.raises(IntegrationApiError) as caught:
        large_service.runs(
            from_value=START,
            to_value=END,
            time_zone="UTC",
            limit=1000,
        )
    assert caught.value.code is IntegrationErrorCode.PAGE_ENVELOPE_TOO_LARGE
    assert "smaller limit" in caught.value.message

    defaults = default_http_service_factories()
    factories = HttpServiceFactories(
        dashboard=lambda: large_service,
        run=defaults.run,
        sequence=defaults.sequence,
        review=defaults.review,
        process_output=defaults.process_output,
        capacity=defaults.capacity,
    )
    server, thread = _server(factories)
    try:
        status, payload = _post(server, "dashboard.runs", {**FILTERS, "limit": 1000})
        assert status == 413
        assert payload["error"]["code"] == "PAGE_ENVELOPE_TOO_LARGE"  # type: ignore[index]
    finally:
        server.shutdown()
        thread.join(timeout=5)

    # The established open generic error envelope accepts the additive typed code.
    assert isinstance(payload["error"]["message"], str)  # type: ignore[index]
    assert large_service.envelope_max_bytes == 240 * 1024
