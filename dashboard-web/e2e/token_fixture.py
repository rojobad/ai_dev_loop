"""Deterministic token overlay for the dashboard browser fixture.

The ledger has no authenticated usage artifacts. The real query still decides
membership, missing runs, and filter errors. This module replaces only a
successful map-project or unfiltered result with producer leaves.
"""

from __future__ import annotations

import time
from pathlib import Path

from ai_dev_loop.dashboard.application.queries import CoverageResult, FiltersResult
from ai_dev_loop.dashboard.application.token_usage import (
    SelectedRunResult,
    TokenUsageResult,
    _row,
    _source,
)
from ai_dev_loop.dashboard.domain.token_analytics import (
    AttemptFact,
    aggregate_token_usage,
)
from ai_dev_loop.integration_api.dashboard_service import _strict_payload
from ai_dev_loop.integration_api.dashboard_token_models import DashboardTokenUsageData
from ai_dev_loop.scheduler.domain.token_usage import (
    NormalizedUsage,
    ProviderNativeUsage,
    UsageObservationV1,
    usage_observation_id,
)

OBSERVED_AT = "2026-10-19T12:05:00Z"
OLDER_RUN_ID = "older-map-run"
CURRENT_RUN_ID = "current-map-run"
XSS_MODEL = 'review<img alt="x">'
_OBSERVED_TICK = 0


def _next_observed_at() -> str:
    """Keep the displayed minute stable while every read has a distinct instant."""

    global _OBSERVED_TICK
    _OBSERVED_TICK += 1
    return f"2026-10-19T12:05:00.{_OBSERVED_TICK:03d}Z"


def _observation(
    *,
    provider: str,
    role: str,
    stage: str,
    attempt_id: str,
    native: ProviderNativeUsage,
    normalized: NormalizedUsage,
) -> UsageObservationV1:
    return UsageObservationV1(
        observation_id=usage_observation_id(
            provider=provider,  # type: ignore[arg-type]
            role=role,  # type: ignore[arg-type]
            scheduler_attempt_id=attempt_id,
            native_key="1",
        ),
        run_id="fixture-token-run",
        provider=provider,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        scheduler_attempt_id=attempt_id,
        publisher_attempt_id=attempt_id,
        source_dispatch_id=f"dispatch-{attempt_id}",
        native_key="1",
        source_artifact_path="usage/source.json",
        source_artifact_sha256="a" * 64,
        source_format="provider-usage-v1",
        source_event_ordinal=1,
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
    state: str,
    observation: UsageObservationV1 | None,
) -> AttemptFact:
    return AttemptFact(
        provider=provider,  # type: ignore[arg-type]
        role=role,  # type: ignore[arg-type]
        stage=stage,  # type: ignore[arg-type]
        configured_model=model,
        observations=() if observation is None else (observation,),
        state=state,  # type: ignore[arg-type]
    )


def _cursor_140() -> UsageObservationV1:
    return _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="fixture-cursor-140",
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
        attempt_id="fixture-codex-115",
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


def _cursor_amount(attempt_id: str, total: int, *, role: str = "implementer", stage: str = "initial") -> UsageObservationV1:
    return _observation(
        provider="cursor",
        role=role,
        stage=stage,
        attempt_id=attempt_id,
        native=ProviderNativeUsage(
            input_tokens=total,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=total,
            reasoning_tokens=0,
        ),
        normalized=NormalizedUsage(
            input_total=total,
            input_uncached=total,
            cache_read=0,
            cache_write=0,
            output_total=0,
            reasoning_output=0,
            total_tokens=total,
        ),
    )


def _cohort_attempts() -> tuple[AttemptFact, ...]:
    shared_cursor = _observation(
        provider="cursor",
        role="implementer",
        stage="initial",
        attempt_id="fixture-cursor-shared-30",
        native=ProviderNativeUsage(
            input_tokens=20,
            output_tokens=5,
            cache_read_tokens=4,
            cache_write_tokens=1,
            total_tokens=30,
            reasoning_tokens=1,
        ),
        normalized=NormalizedUsage(
            input_total=25,
            input_uncached=20,
            cache_read=4,
            cache_write=1,
            output_total=5,
            reasoning_output=1,
            total_tokens=30,
        ),
    )
    shared_codex = _observation(
        provider="codex",
        role="implementer",
        stage="initial",
        attempt_id="fixture-codex-shared-26",
        native=ProviderNativeUsage(
            input_tokens=20,
            cached_input_tokens=6,
            cache_write_input_tokens=2,
            output_tokens=6,
            reasoning_output_tokens=2,
            total_tokens=26,
        ),
        normalized=NormalizedUsage(
            input_total=20,
            input_uncached=14,
            cache_read=6,
            cache_write=2,
            output_total=6,
            reasoning_output=2,
            total_tokens=26,
        ),
    )
    xss = _observation(
        provider="codex",
        role="reviewer",
        stage="review",
        attempt_id="fixture-codex-xss",
        native=ProviderNativeUsage(input_tokens=8, output_tokens=2, total_tokens=10),
        normalized=NormalizedUsage(input_total=8, output_total=2, total_tokens=10),
    )
    extras = tuple(
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model=f"extra-model-{index}",
            state="complete",
            observation=_cursor_amount(f"fixture-extra-{index}", 8),
        )
        for index in range(4)
    )
    return (
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model="grok-4.7",
            state="complete",
            observation=_cursor_140(),
        ),
        _attempt(
            provider="codex",
            role="implementer",
            stage="initial",
            model="gpt-5.5",
            state="complete",
            observation=_codex_115(),
        ),
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model="shared",
            state="complete",
            observation=shared_cursor,
        ),
        _attempt(
            provider="codex",
            role="implementer",
            stage="initial",
            model="shared",
            state="complete",
            observation=shared_codex,
        ),
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model=None,
            state="unknown",
            observation=None,
        ),
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model="recorded-zero",
            state="complete",
            observation=_cursor_amount("fixture-cursor-zero", 0),
        ),
        _attempt(
            provider="cursor",
            role="reviewer",
            stage="correction",
            model="partial-model",
            state="partial",
            observation=_cursor_amount(
                "fixture-cursor-partial",
                10,
                role="reviewer",
                stage="correction",
            ),
        ),
        _attempt(
            provider="codex",
            role="reviewer",
            stage="review",
            model=XSS_MODEL,
            state="complete",
            observation=xss,
        ),
        *extras,
    )


_COHORT = _cohort_attempts()
_DIRECT = {
    OLDER_RUN_ID: (_COHORT[0],),
    CURRENT_RUN_ID: (_COHORT[1],),
}
_COHORT_TOTAL = aggregate_token_usage(_COHORT, unattributed_attempts=0).known_total_tokens
_OLDER_TOTAL = aggregate_token_usage(_DIRECT[OLDER_RUN_ID], unattributed_attempts=0).known_total_tokens
_CURRENT_TOTAL = aggregate_token_usage(_DIRECT[CURRENT_RUN_ID], unattributed_attempts=0).known_total_tokens
if (_COHORT_TOTAL, _OLDER_TOTAL, _CURRENT_TOTAL) != (363, 140, 115):
    raise RuntimeError("fixture token catalog does not match the Phase 29.1 literals")


def _wide_attempts(scale: int, prefix: str) -> tuple[AttemptFact, ...]:
    return tuple(
        _attempt(
            provider="cursor",
            role="implementer",
            stage="initial",
            model=f"wide-model-{index}",
            state="complete",
            observation=_cursor_amount(f"{prefix}-{index}", scale),
        )
        for index in range(12)
    )


def _compact_attempt() -> AttemptFact:
    return _attempt(
        provider="cursor",
        role="implementer",
        stage="initial",
        model="compact-model",
        state="complete",
        observation=_cursor_amount("compact-current", 20),
    )


_WIDE = {
    OLDER_RUN_ID: _wide_attempts(10, "wide-older"),
    CURRENT_RUN_ID: (_compact_attempt(),),
}
_WIDE_TOTALS = {
    run_id: aggregate_token_usage(attempts, unattributed_attempts=0).known_total_tokens
    for run_id, attempts in _WIDE.items()
}
if _WIDE_TOTALS != {OLDER_RUN_ID: 120, CURRENT_RUN_ID: 20}:
    raise RuntimeError("wide direct token fixture does not have a 12-model run and a one-model replacement")


def overlay_token_usage(
    real: DashboardTokenUsageData,
    *,
    wide: bool = False,
) -> DashboardTokenUsageData:
    """Return scripted leaves for the map cohort. Every other result stays real."""

    project = real.filters.project_name
    if project not in {None, "map-project"} or real.cohort_run_count <= 0:
        return real
    selected: SelectedRunResult | None = None
    if real.scope == "direct":
        if real.selected_run is None:
            return real
        attempts = (
            _WIDE.get(real.selected_run.run_id)
            if wide
            else _DIRECT.get(real.selected_run.run_id)
        )
        if attempts is None:
            return real
        selected = SelectedRunResult(
            run_id=real.selected_run.run_id,
            short_name=real.selected_run.short_name,
            project_name=real.selected_run.project_name,
        )
        selected_count = 1
    elif real.scope == "cohort":
        attempts = _COHORT
        selected_count = real.cohort_run_count
    else:
        return real
    aggregate = aggregate_token_usage(attempts, unattributed_attempts=0)
    coverage = real.cohort_coverage
    result = TokenUsageResult(
        observed_at=_next_observed_at(),
        filters=FiltersResult(
            from_utc=real.filters.from_utc,
            to_utc=real.filters.to_utc,
            time_zone=real.filters.time_zone,
            project_name=real.filters.project_name,
        ),
        scope=real.scope,
        model_provenance="configured",
        normalization_version="provider-usage-v1",
        selected_run=selected,
        selected_run_count=selected_count,
        cohort_run_count=real.cohort_run_count,
        cohort_coverage=CoverageResult(
            data_state=coverage.data_state,
            unstarted_runs=coverage.unstarted_runs,
            missing_launch_times=coverage.missing_launch_times,
            unknown_execution_kinds=coverage.unknown_execution_kinds,
            missing_duration_attempts=coverage.missing_duration_attempts,
            warnings=tuple(coverage.warnings),
        ),
        source=_source(aggregate),
        rows=tuple(_row(item) for item in aggregate.rows),
    )
    return DashboardTokenUsageData.model_validate(_strict_payload(result))


class FixtureTokenDashboard:
    """Real dashboard reader with a held, scripted token response for browser tests."""

    def __init__(self, delegate: object, control_root: Path) -> None:
        self._delegate = delegate
        self._control_root = control_root

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)

    def token_usage(self, **kwargs: object) -> DashboardTokenUsageData:
        self._hold()
        if (self._control_root / "fail-token-usage").is_file() and kwargs.get("run_id"):
            raise RuntimeError("token fixture failure")
        real = self._delegate.token_usage(**kwargs)  # type: ignore[attr-defined]
        try:
            return overlay_token_usage(
                real,
                wide=(self._control_root / "wide-direct-tokens").is_file(),
            )
        except Exception:
            return real

    def _hold(self) -> None:
        if not (self._control_root / "hold-token-usage").exists():
            return
        (self._control_root / "held-token-usage-started").write_text("started\n", encoding="utf-8")
        deadline = time.monotonic() + 30
        while not (self._control_root / "release-token-usage").exists():
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)
