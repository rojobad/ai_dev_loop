"""Disjoint token analytics, configured-model attribution, and the public DTO."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from ai_dev_loop.dashboard.application.ports import (
    DashboardObservationSet,
    DashboardReadError,
    DashboardReadErrorKind,
    DashboardReadPort,
)
from ai_dev_loop.dashboard.application.token_usage import (
    TOKEN_SCOPE_TOO_LARGE,
    DirectUsageReader,
    TokenUsageQueries,
)
from ai_dev_loop.dashboard.domain import token_analytics
from ai_dev_loop.dashboard.domain.filters import DashboardFilters
from ai_dev_loop.dashboard.domain.token_analytics import (
    JS_MAX_SAFE_INTEGER,
    TOKEN_FLOW_NAMES,
    AttemptFact,
    TokenAggregate,
    TokenAnalyticsMagnitudeError,
    aggregate_token_usage,
)
from ai_dev_loop.integration_api.dashboard_token_models import DashboardTokenUsageData
from ai_dev_loop.integration_api.info import build_integration_info_data
from ai_dev_loop.integration_api.models import IntegrationCapabilities
from ai_dev_loop.integration_api.schemas import (
    DASHBOARD_TOKEN_USAGE_DATA_SCHEMA,
    validate_integration_instance,
)
from ai_dev_loop.scheduler.application.token_usage_query import configured_model_from_invocation
from ai_dev_loop.scheduler.domain.token_usage import (
    NormalizedUsage,
    ProviderNativeUsage,
    UsageObservationV1,
    usage_observation_id,
)


def _observation(
    *,
    provider: str,
    role: str,
    stage: str,
    attempt_id: str,
    native: ProviderNativeUsage,
    normalized: NormalizedUsage,
    ordinal: int = 1,
) -> UsageObservationV1:
    native_key = str(ordinal)
    return UsageObservationV1(
        observation_id=usage_observation_id(
            provider=provider,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            scheduler_attempt_id=attempt_id,
            native_key=native_key,
        ),
        run_id="run-1",
        provider=provider,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        scheduler_attempt_id=attempt_id,
        publisher_attempt_id=attempt_id,
        source_dispatch_id=f"dispatch-{attempt_id}",
        native_key=native_key,
        source_artifact_path="usage/source.json",
        source_artifact_sha256="a" * 64,
        source_format="provider-usage-v1",
        source_event_ordinal=ordinal,
        capture_complete=True,
        availability="known",
        native=native,
        normalized=normalized,
    )


def _attempt(
    *,
    provider: str,
    role: str,
    stage: str,
    model: str | None,
    observations: tuple[UsageObservationV1, ...],
    state: str = "complete",
) -> AttemptFact:
    return AttemptFact(
        provider=provider,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        configured_model=model,
        observations=observations,
        state=state,  # type: ignore[arg-type]
    )


def _cursor_140() -> UsageObservationV1:
    return _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="cursor-attempt",
        native=ProviderNativeUsage(
            input_tokens=100,
            output_tokens=15,
            cache_read_tokens=20,
            cache_write_tokens=5,
            total_tokens=140,
            reasoning_tokens=3,
        ),
        normalized=NormalizedUsage(
            input_total=125,
            input_uncached=100,
            cache_read=20,
            cache_write=5,
            output_total=15,
            reasoning_output=3,
            total_tokens=140,
        ),
    )


def _codex_115() -> UsageObservationV1:
    return _observation(
        provider="codex",
        role="implementer",
        stage="initial",
        attempt_id="codex-attempt",
        native=ProviderNativeUsage(
            input_tokens=100,
            cached_input_tokens=20,
            cache_write_input_tokens=7,
            output_tokens=15,
            reasoning_output_tokens=3,
            total_tokens=115,
        ),
        normalized=NormalizedUsage(
            input_total=100,
            input_uncached=80,
            cache_read=20,
            cache_write=7,
            output_total=15,
            reasoning_output=3,
            total_tokens=115,
        ),
    )


def _leaf(result: object, name: str) -> int | None:
    flows = result.flows  # type: ignore[attr-defined]
    return next(item.known_total for item in flows if item.name == name)


def test_t291_03_cursor_and_codex_leaves_are_fixed_literals() -> None:
    cursor = aggregate_token_usage(
        (
            _attempt(
                provider="cursor",
                role="implementer",
                stage="initial",
                model="grok",
                observations=(_cursor_140(),),
            ),
        )
    )
    row = cursor.rows[0]
    assert cursor.known_total_tokens == 140
    assert _leaf(row, "input_other") == 100
    assert _leaf(row, "cache_read") == 20
    assert _leaf(row, "cache_write") == 5
    assert _leaf(row, "output_other") == 12
    assert _leaf(row, "reasoning_output") == 3
    assert _leaf(row, "input_unsplit") is None
    assert _leaf(row, "output_unsplit") is None
    assert row.native_categories["cache_write_tokens"].known_total == 5

    codex = aggregate_token_usage(
        (
            _attempt(
                provider="codex",
                role="implementer",
                stage="initial",
                model="gpt",
                observations=(_codex_115(),),
            ),
        )
    )
    codex_row = codex.rows[0]
    assert codex.known_total_tokens == 115
    assert _leaf(codex_row, "input_other") == 80
    assert _leaf(codex_row, "cache_read") == 20
    assert _leaf(codex_row, "output_other") == 12
    assert _leaf(codex_row, "reasoning_output") == 3
    assert _leaf(codex_row, "cache_write") is None
    assert _leaf(codex_row, "input_unsplit") is None
    overlapping = 100 + 20 + 15 + 3
    assert overlapping == 138
    assert codex_row.known_total_tokens == 115
    assert codex_row.categories["input_total"].known_total == 100
    assert codex_row.categories["cache_read"].known_total == 20
    assert codex_row.native_categories["cache_write_input_tokens"].known_total == 7


def test_t291_03_missing_splits_stay_null_and_zero_stays_zero() -> None:
    unsplit = _observation(
        provider="codex",
        role="reviewer",
        stage="review",
        attempt_id="unsplit",
        native=ProviderNativeUsage(input_tokens=100, output_tokens=15, total_tokens=115),
        normalized=NormalizedUsage(
            input_total=100,
            output_total=15,
            total_tokens=115,
        ),
    )
    result = aggregate_token_usage(
        (
            _attempt(
                provider="codex",
                role="reviewer",
                stage="review",
                model=None,
                observations=(unsplit,),
            ),
        )
    )
    row = result.rows[0]
    assert result.known_total_tokens == 115
    assert _leaf(row, "input_unsplit") == 100
    assert _leaf(row, "output_unsplit") == 15
    assert _leaf(row, "input_other") is None
    assert _leaf(row, "cache_read") is None
    assert _leaf(row, "reasoning_output") is None
    assert row.categories["cache_read"].known_total is None
    assert row.configured_model is None

    zero = _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="zero",
        native=ProviderNativeUsage(
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=0,
            reasoning_tokens=0,
        ),
        normalized=NormalizedUsage(
            input_total=0,
            input_uncached=0,
            cache_read=0,
            cache_write=0,
            output_total=0,
            reasoning_output=0,
            total_tokens=0,
        ),
    )
    recorded = aggregate_token_usage(
        (
            _attempt(
                provider="cursor",
                role="implementer",
                stage="initial",
                model="m",
                observations=(zero,),
            ),
        )
    )
    assert recorded.known_total_tokens == 0
    assert _leaf(recorded.rows[0], "input_other") == 0
    assert _leaf(recorded.rows[0], "reasoning_output") == 0


def test_t291_03_mixed_observations_partition_individually() -> None:
    result = aggregate_token_usage(
        (
            _attempt(
                provider="cursor",
                role="implementer",
                stage="initial",
                model="shared",
                observations=(_cursor_140(),),
            ),
            _attempt(
                provider="codex",
                role="implementer",
                stage="initial",
                model="shared",
                observations=(_codex_115(),),
            ),
        )
    )
    assert result.known_total_tokens == 255
    assert _leaf(result, "input_other") == 180
    assert _leaf(result, "cache_read") == 40
    assert _leaf(result, "cache_write") == 5
    assert _leaf(result, "output_other") == 24
    assert _leaf(result, "reasoning_output") == 6
    assert [row.provider for row in result.rows] == ["codex", "cursor"]
    assert {row.provider for row in result.rows} == {"codex", "cursor"}
    assert all(row.configured_model == "shared" for row in result.rows)


def test_t291_03_contradiction_magnitude_and_unattributed_do_not_invent_widths() -> None:
    contradictory = _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="bad",
        native=ProviderNativeUsage(
            input_tokens=100,
            output_tokens=15,
            cache_read_tokens=20,
            cache_write_tokens=5,
            total_tokens=140,
            reasoning_tokens=3,
        ),
        normalized=NormalizedUsage(
            input_total=125,
            input_uncached=100,
            cache_read=20,
            cache_write=5,
            output_total=15,
            reasoning_output=3,
            total_tokens=999,
        ),
    )
    dropped = aggregate_token_usage(
        (
            _attempt(
                provider="cursor",
                role="implementer",
                stage="initial",
                model="grok",
                observations=(contradictory,),
            ),
        )
    )
    assert dropped.known_total_tokens is None
    assert dropped.rows[0].known_total_tokens is None
    assert dropped.unknown_attempts == 1
    assert "usage_observation_integrity_error" in dropped.diagnostics
    assert all(item.known_total is None for item in dropped.flows)

    empty = aggregate_token_usage(
        (), unattributed_attempts=2, diagnostics=("attempt_usage_evidence_invalid",)
    )
    assert empty.rows == ()
    assert empty.known_total_tokens is None
    assert empty.inference_attempts == 2
    assert empty.unknown_attempts == 2
    assert empty.coverage == "unknown"

    huge = _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="huge",
        native=ProviderNativeUsage(
            input_tokens=JS_MAX_SAFE_INTEGER,
            output_tokens=1,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=JS_MAX_SAFE_INTEGER,
        ),
        normalized=NormalizedUsage(
            input_total=JS_MAX_SAFE_INTEGER,
            input_uncached=JS_MAX_SAFE_INTEGER,
            cache_read=0,
            cache_write=0,
            output_total=1,
            total_tokens=JS_MAX_SAFE_INTEGER,
        ),
    )
    with pytest.raises(TokenAnalyticsMagnitudeError):
        aggregate_token_usage(
            (
                _attempt(
                    provider="cursor",
                    role="implementer",
                    stage="initial",
                    model="m",
                    observations=(huge,),
                ),
            )
        )


def test_t291_02_configured_model_rejects_blank_controls_and_non_strings() -> None:
    assert (
        configured_model_from_invocation("cursor", "implementer", {"cursor_model": " grok-4.7 "})
        == "grok-4.7"
    )
    assert (
        configured_model_from_invocation("codex", "implementer", {"model": "gpt-5.5"}) == "gpt-5.5"
    )
    assert (
        configured_model_from_invocation("codex", "reviewer", {"review_model": "gpt-5.6-sol"})
        == "gpt-5.6-sol"
    )
    assert (
        configured_model_from_invocation("codex", "implementer", {"review_model": "gpt-5.6-sol"})
        is None
    )
    for raw in (None, "", "   ", 5, True, "model\nname", "x" * 257):
        assert (
            configured_model_from_invocation("cursor", "implementer", {"cursor_model": raw}) is None
        )


def test_t291_04_public_dto_schema_and_capability_are_additive() -> None:
    result = aggregate_token_usage(
        (
            _attempt(
                provider="cursor",
                role="implementer",
                stage="initial",
                model=None,
                observations=(_cursor_140(),),
            ),
        )
    )
    row = result.rows[0]
    payload = {
        "observedAt": "2026-10-21T12:00:00Z",
        "filters": {
            "fromUtc": "2026-10-01T00:00:00Z",
            "toUtc": "2026-10-02T00:00:00Z",
            "timeZone": "UTC",
            "projectName": None,
        },
        "scope": "cohort",
        "modelProvenance": "configured",
        "normalizationVersion": "provider-usage-v1",
        "selectedRun": None,
        "selectedRunCount": 1,
        "cohortRunCount": 1,
        "cohortCoverage": {
            "dataState": "complete",
            "unstartedRuns": 0,
            "missingLaunchTimes": 0,
            "unknownExecutionKinds": 0,
            "missingDurationAttempts": 0,
            "warnings": (),
        },
        "source": {
            "knownTotalTokens": result.known_total_tokens,
            "inferenceAttempts": result.inference_attempts,
            "knownAttempts": result.known_attempts,
            "partialAttempts": result.partial_attempts,
            "unknownAttempts": result.unknown_attempts,
            "coverage": result.coverage,
            "diagnostics": tuple(result.diagnostics),
            "categories": {
                name: {
                    "knownTotal": fact.known_total,
                    "knownObservations": fact.known_observations,
                    "observationCount": fact.observation_count,
                    "coverage": fact.coverage,
                }
                for name, fact in result.categories.items()
            },
            "flows": tuple(
                {
                    "name": item.name,
                    "knownTotal": item.known_total,
                    "knownObservations": item.known_observations,
                    "observationCount": item.observation_count,
                    "coverage": item.coverage,
                }
                for item in result.flows
            ),
        },
        "rows": (
            {
                "provider": row.provider,
                "configuredModel": None,
                "modelProvenance": "configured",
                "role": row.role,
                "stage": row.stage,
                "knownTotalTokens": row.known_total_tokens,
                "inferenceAttempts": row.inference_attempts,
                "knownAttempts": row.known_attempts,
                "partialAttempts": row.partial_attempts,
                "unknownAttempts": row.unknown_attempts,
                "coverage": row.coverage,
                "categories": {
                    name: {
                        "knownTotal": fact.known_total,
                        "knownObservations": fact.known_observations,
                        "observationCount": fact.observation_count,
                        "coverage": fact.coverage,
                    }
                    for name, fact in row.categories.items()
                },
                "nativeCategories": {
                    name: {
                        "knownTotal": fact.known_total,
                        "knownObservations": fact.known_observations,
                        "observationCount": fact.observation_count,
                        "coverage": fact.coverage,
                    }
                    for name, fact in row.native_categories.items()
                },
                "flows": tuple(
                    {
                        "name": item.name,
                        "knownTotal": item.known_total,
                        "knownObservations": item.known_observations,
                        "observationCount": item.observation_count,
                        "coverage": item.coverage,
                    }
                    for item in row.flows
                ),
            },
        ),
    }
    parsed = DashboardTokenUsageData.model_validate(payload)
    assert parsed.rows[0].configured_model is None
    assert [item.name for item in parsed.source.flows] == list(TOKEN_FLOW_NAMES)
    dumped = parsed.model_dump(mode="json", by_alias=True)
    validate_integration_instance(dumped, DASHBOARD_TOKEN_USAGE_DATA_SCHEMA)
    broken_flows = tuple(
        {**item, "knownTotal": 1} if item["name"] == "input_other" else item
        for item in payload["source"]["flows"]
    )
    broken = {
        **payload,
        "source": {**payload["source"], "flows": broken_flows},
    }
    with pytest.raises(ValidationError, match="token flow leaves disagree"):
        DashboardTokenUsageData.model_validate(broken)

    assert build_integration_info_data().capabilities.dashboard_token_usage is True
    historical = IntegrationCapabilities.model_validate({"runs": True, "tokenUsage": True})
    assert historical.dashboard_token_usage is False
    assert historical.token_usage is True
    with pytest.raises(ValidationError):
        IntegrationCapabilities.model_validate({"dashboardTokenUsage": None})


def test_t291_domain_calculation_does_not_import_storage_or_transport() -> None:
    source = Path(token_analytics.__file__).read_text(encoding="utf-8")
    for forbidden in ("sqlite3", "http.server", "typer", "echarts", "subprocess"):
        assert forbidden not in source


class _EmptyCohort:
    def __init__(self) -> None:
        self.calls = 0

    def read_observations(self, scope: object) -> DashboardObservationSet:
        self.calls += 1
        return DashboardObservationSet(runs=(), attempts=())


class _UnusedUsage:
    def __init__(self) -> None:
        self.calls = 0

    def collect(self, run_ids: object, *, max_attempts: int, checkpoint: object) -> object:
        self.calls += 1
        raise AssertionError("usage collection is outside this deadline boundary")


def _deadline_filters() -> DashboardFilters:
    return DashboardFilters.parse(
        from_value="2026-01-02T00:00:00Z",
        to_value="2026-01-03T00:00:00Z",
        time_zone="UTC",
    )


def _scripted_clock(values: tuple[float, ...]) -> Callable[[], float]:
    pending = list(values)

    def monotonic() -> float:
        if not pending:
            raise AssertionError("token query clock exhausted")
        return pending.pop(0)

    return monotonic


def _assert_narrow_scope(error: pytest.ExceptionInfo[DashboardReadError]) -> None:
    assert error.value.kind is DashboardReadErrorKind.INVALID_ARGUMENT
    assert error.value.message == TOKEN_SCOPE_TOO_LARGE
    assert error.value.__cause__ is not None


@pytest.mark.parametrize(
    ("ticks", "cancelled", "cohort_calls", "aggregate_calls"),
    [
        ((0.0,), True, 0, 0),
        ((0.0, 0.0, 10.0), False, 1, 0),
        ((0.0, 0.0, 0.0, 10.0), False, 1, 1),
    ],
)
def test_token_query_deadline_checkpoints_are_narrow_scope_errors(
    monkeypatch: pytest.MonkeyPatch,
    ticks: tuple[float, ...],
    cancelled: bool,
    cohort_calls: int,
    aggregate_calls: int,
) -> None:
    cohort = _EmptyCohort()
    usage = _UnusedUsage()
    aggregated = {"calls": 0}
    original = aggregate_token_usage

    def aggregate(*args: Any, **kwargs: Any) -> TokenAggregate:
        aggregated["calls"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        "ai_dev_loop.dashboard.application.token_usage.aggregate_token_usage",
        aggregate,
    )
    queries = TokenUsageQueries(
        lambda _deadline: cast(DashboardReadPort, cohort),
        lambda: cast(DirectUsageReader, usage),
        monotonic=_scripted_clock(ticks),
        cancelled=lambda: cancelled,
    )
    with pytest.raises(DashboardReadError) as error:
        queries.query(_deadline_filters(), None)
    _assert_narrow_scope(error)
    assert cohort.calls == cohort_calls
    assert usage.calls == 0
    assert aggregated["calls"] == aggregate_calls
