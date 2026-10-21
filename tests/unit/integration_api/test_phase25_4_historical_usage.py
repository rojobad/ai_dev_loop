"""Historical additive Integration API token usage projections."""

from __future__ import annotations

import json

from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.models import (
    IntegrationRunInspectData,
    IntegrationRunUsage,
)
from ai_dev_loop.integration_api.schemas import validate_integration_instance
from ai_dev_loop.integration_api.version import API_MAJOR, API_MINOR
from ai_dev_loop.scheduler.domain.token_usage import (
    RunUsageReport,
    UsageScopeReport,
)


def _empty_scope(scope: str) -> UsageScopeReport:
    return UsageScopeReport(
        scope=scope,  # type: ignore[arg-type]
        run_ids=("historical-run",),
        known_total_tokens=None,
        inference_attempts=1,
        known_attempts=0,
        partial_attempts=0,
        unknown_attempts=1,
        coverage="unknown",
        breakdowns=(),
    )


def test_u03_additive_api_version_capability_and_usage_schema() -> None:
    assert (API_MAJOR, API_MINOR) == (1, 13)
    assert build_integration_info_data().capabilities.token_usage is True
    report = RunUsageReport(
        run_id="historical-run",
        direct=_empty_scope("direct"),
        recovery_lineage=_empty_scope("recovery_lineage"),
    )
    wire = IntegrationRunUsage.model_validate(report.model_dump()).model_dump(
        mode="json", by_alias=True
    )
    assert wire["normalizationVersion"] == "provider-usage-v1"
    assert wire["direct"]["knownTotalTokens"] is None


def test_u04_pre_25_wire_fixture_still_parses_without_usage_field() -> None:
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures/integration_api/v1_1/run_inspect_data_minimal.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    validate_integration_instance(payload, "integration-api-run-inspect-data-v1.json")
    inspect = IntegrationRunInspectData.model_validate(
        {**payload, "shortName": "Historical run", "sequenceShortName": None}
    )
    assert inspect.token_usage is None
