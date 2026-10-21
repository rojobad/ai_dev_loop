"""Public wire DTOs for dashboard token analytics."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, StrictInt, model_validator

from ai_dev_loop.dashboard.domain.token_analytics import JS_MAX_SAFE_INTEGER, TOKEN_FLOW_NAMES
from ai_dev_loop.integration_api.dashboard_models import (
    DashboardCoverageData,
    DashboardFiltersData,
)
from ai_dev_loop.integration_api.models import PublicWireModel

UsageCoverageName = Literal["complete", "partial", "unknown", "not_applicable"]
TokenProviderName = Literal["cursor", "codex"]
TokenRoleName = Literal["implementer", "reviewer"]
TokenStageName = Literal["initial", "correction", "review"]
_SAFE = JS_MAX_SAFE_INTEGER


class DashboardTokenCategoryData(PublicWireModel):
    known_total: StrictInt | None = Field(None, alias="knownTotal", ge=0, le=_SAFE)
    known_observations: StrictInt = Field(alias="knownObservations", ge=0, le=_SAFE)
    observation_count: StrictInt = Field(alias="observationCount", ge=0, le=_SAFE)
    coverage: UsageCoverageName


class DashboardTokenFlowData(PublicWireModel):
    name: Literal[
        "input_other",
        "cache_read",
        "cache_write",
        "input_unsplit",
        "output_other",
        "reasoning_output",
        "output_unsplit",
    ]
    known_total: StrictInt | None = Field(None, alias="knownTotal", ge=0, le=_SAFE)
    known_observations: StrictInt = Field(alias="knownObservations", ge=0, le=_SAFE)
    observation_count: StrictInt = Field(alias="observationCount", ge=0, le=_SAFE)
    coverage: UsageCoverageName


class DashboardTokenDimensionData(PublicWireModel):
    known_total_tokens: StrictInt | None = Field(None, alias="knownTotalTokens", ge=0, le=_SAFE)
    inference_attempts: StrictInt = Field(alias="inferenceAttempts", ge=0, le=_SAFE)
    known_attempts: StrictInt = Field(alias="knownAttempts", ge=0, le=_SAFE)
    partial_attempts: StrictInt = Field(alias="partialAttempts", ge=0, le=_SAFE)
    unknown_attempts: StrictInt = Field(alias="unknownAttempts", ge=0, le=_SAFE)
    coverage: UsageCoverageName
    categories: dict[str, DashboardTokenCategoryData]
    flows: tuple[DashboardTokenFlowData, ...]

    @model_validator(mode="after")
    def _flows_are_a_partition(self) -> DashboardTokenDimensionData:
        names = [item.name for item in self.flows]
        if names != list(TOKEN_FLOW_NAMES):
            raise ValueError("token flows must be the complete disjoint leaf set")
        known = [item.known_total for item in self.flows if item.known_total is not None]
        if self.known_total_tokens is None:
            if known:
                raise ValueError("token flow leaves disagree with the known total")
        elif sum(known) != self.known_total_tokens:
            raise ValueError("token flow leaves disagree with the known total")
        attempt_total = self.known_attempts + self.partial_attempts + self.unknown_attempts
        if attempt_total != self.inference_attempts:
            raise ValueError("token attempt counts must add up")
        return self


class DashboardTokenRowData(DashboardTokenDimensionData):
    provider: TokenProviderName
    configured_model: str | None = Field(None, alias="configuredModel")
    model_provenance: Literal["configured"] = Field(alias="modelProvenance")
    role: TokenRoleName
    stage: TokenStageName
    native_categories: dict[str, DashboardTokenCategoryData] = Field(alias="nativeCategories")

    @model_validator(mode="after")
    def _null_model_stays_null(self) -> DashboardTokenRowData:
        if self.configured_model is not None and not self.configured_model.strip():
            raise ValueError("configuredModel must be null when unrecorded")
        return self


class DashboardTokenSourceData(DashboardTokenDimensionData):
    diagnostics: tuple[str, ...]


class DashboardSelectedRunData(PublicWireModel):
    run_id: str = Field(alias="runId", min_length=1, max_length=256)
    short_name: str = Field(alias="shortName", min_length=1, max_length=35)
    project_name: str = Field(alias="projectName", min_length=1)


class DashboardTokenUsageData(PublicWireModel):
    observed_at: str = Field(alias="observedAt")
    filters: DashboardFiltersData
    scope: Literal["cohort", "direct"]
    model_provenance: Literal["configured"] = Field(alias="modelProvenance")
    normalization_version: Literal["provider-usage-v1"] = Field(alias="normalizationVersion")
    selected_run: DashboardSelectedRunData | None = Field(alias="selectedRun")
    selected_run_count: StrictInt = Field(alias="selectedRunCount", ge=0, le=_SAFE)
    cohort_run_count: StrictInt = Field(alias="cohortRunCount", ge=0, le=_SAFE)
    cohort_coverage: DashboardCoverageData = Field(alias="cohortCoverage")
    source: DashboardTokenSourceData
    rows: tuple[DashboardTokenRowData, ...]

    @model_validator(mode="after")
    def _selection_matches_scope(self) -> DashboardTokenUsageData:
        if self.scope == "cohort" and self.selected_run is not None:
            raise ValueError("cohort token usage has no selected run")
        if self.scope == "direct" and self.selected_run is None:
            raise ValueError("direct token usage requires the selected run")
        if self.scope == "direct" and self.selected_run_count != 1:
            raise ValueError("direct token usage includes one run")
        if self.selected_run_count > self.cohort_run_count:
            raise ValueError("selected run count cannot exceed the cohort")
        if self.scope == "cohort" and self.selected_run_count != self.cohort_run_count:
            raise ValueError("cohort token usage counts every included run")
        return self
