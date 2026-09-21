"""Public wire DTOs for the local Integration API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ai_dev_loop.integration_api.version import API_MAJOR, API_MINOR


class PublicWireModel(BaseModel):
    """Public JSON DTO; unknown additive fields are tolerated on ingest."""

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
        validate_by_name=True,
        strict=True,
    )


class ApiVersion(PublicWireModel):
    major: StrictInt
    minor: StrictInt


class IntegrationErrorBody(PublicWireModel):
    code: str
    message: str


class IntegrationCapabilities(PublicWireModel):
    runs: StrictBool = False
    sequences: StrictBool = False
    review_inspection: StrictBool = Field(False, alias="reviewInspection")
    process_output: StrictBool = Field(False, alias="processOutput")
    codex_capacity: StrictBool = Field(False, alias="codexCapacity")


class IntegrationInfoData(PublicWireModel):
    ai_dev_loop_version: str = Field(alias="aiDevLoopVersion")
    capabilities: IntegrationCapabilities


class IntegrationEnvelope(PublicWireModel):
    api_version: ApiVersion = Field(alias="apiVersion")
    ok: StrictBool
    observed_at: datetime = Field(alias="observedAt")
    data: Any | None
    error: IntegrationErrorBody | None

    @field_validator("observed_at", mode="before")
    @classmethod
    def _validate_observed_at(cls, value: object) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() != timedelta(0):
                raise ValueError("observedAt must be a UTC RFC3339 timestamp.")
            return value.astimezone(UTC)
        if isinstance(value, str):
            from ai_dev_loop.integration_api.errors import IntegrationApiError
            from ai_dev_loop.integration_api.validation import parse_observed_at_rfc3339_utc

            try:
                return parse_observed_at_rfc3339_utc(value)
            except IntegrationApiError as exc:
                raise ValueError(exc.message) from exc
        raise ValueError("observedAt must be a UTC RFC3339 timestamp.")

    @model_validator(mode="after")
    def _check_ok_error_data(self) -> IntegrationEnvelope:
        if self.ok:
            if self.error is not None:
                raise ValueError("error must be null when ok is true")
            if self.data is None:
                raise ValueError("data must be present when ok is true")
        else:
            if self.data is not None:
                raise ValueError("data must be null when ok is false")
            if self.error is None:
                raise ValueError("error must be present when ok is false")
        return self


class ArtifactChunk(PublicWireModel):
    """Shared artifact byte-chunk shape for later integration read commands."""

    available: StrictBool
    reason: str | None
    encoding: str = "base64"
    media_type: str | None = Field(None, alias="mediaType")
    byte_offset: StrictInt = Field(0, alias="byteOffset", ge=0)
    returned_bytes: StrictInt = Field(0, alias="returnedBytes", ge=0)
    next_offset: StrictInt | None = Field(None, alias="nextOffset", ge=0)
    available_bytes: StrictInt | None = Field(None, alias="availableBytes", ge=0)
    has_more: StrictBool = Field(False, alias="hasMore")
    content_base64: str = Field("", alias="contentBase64")
    sha256: str | None = None

    @model_validator(mode="after")
    def _check_available_invariants(self) -> ArtifactChunk:
        if self.encoding != "base64":
            raise ValueError("encoding must be base64")
        if self.available:
            if self.reason is not None:
                raise ValueError("reason must be null when available is true")
            if self.available_bytes is None:
                raise ValueError("availableBytes required when available is true")
            if self.returned_bytes > self.available_bytes:
                raise ValueError("returnedBytes cannot exceed availableBytes")
            if self.has_more and self.next_offset is None:
                raise ValueError("nextOffset required when hasMore is true")
        else:
            if self.returned_bytes != 0:
                raise ValueError("returnedBytes must be 0 when unavailable")
            if self.content_base64:
                raise ValueError("contentBase64 must be empty when unavailable")
            if self.next_offset is not None:
                raise ValueError("nextOffset must be null when unavailable")
            if self.has_more:
                raise ValueError("hasMore must be false when unavailable")
            if not self.reason:
                raise ValueError("reason required when unavailable")
            if self.available_bytes is not None or self.sha256 is not None:
                raise ValueError("availableBytes and sha256 must be null when unavailable")
        return self


class CollectionPageMeta(PublicWireModel):
    """Pagination metadata for later collection commands."""

    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=500)
    next_offset: StrictInt | None = Field(None, alias="nextOffset", ge=0)
    has_more: StrictBool = Field(alias="hasMore")

    @model_validator(mode="after")
    def _check_pagination_offsets(self) -> CollectionPageMeta:
        if not self.has_more and self.next_offset is not None:
            raise ValueError("nextOffset must be null when hasMore is false")
        if self.has_more and self.next_offset is None:
            raise ValueError("nextOffset is required when hasMore is true")
        return self


def current_api_version() -> ApiVersion:
    return ApiVersion(major=API_MAJOR, minor=API_MINOR)


class IntegrationReviewBudget(PublicWireModel):
    completed: StrictInt = Field(ge=0)
    max_reviews: StrictInt = Field(alias="max", ge=0)
    submitted_max: StrictInt | None = Field(None, alias="submittedMax", ge=0)


class IntegrationSequenceBinding(PublicWireModel):
    sequence_id: str = Field(alias="sequenceId")
    ordinal: StrictInt = Field(ge=1)
    phase_count: StrictInt = Field(alias="phaseCount", ge=1)


class IntegrationSafeNextAction(PublicWireModel):
    kind: str
    run_id: str | None = Field(None, alias="runId")
    sequence_id: str | None = Field(None, alias="sequenceId")
    wait_until: str | None = Field(None, alias="waitUntil")


class IntegrationRunListItem(PublicWireModel):
    run_id: str = Field(alias="runId")
    project_name: str = Field(alias="projectName")
    repository_root: str = Field(alias="repositoryRoot")
    state: str
    submitted_at: str = Field(alias="submittedAt")
    updated_at: str = Field(alias="updatedAt")
    review_budget: IntegrationReviewBudget = Field(alias="reviewBudget")
    sequence_binding: IntegrationSequenceBinding | None = Field(
        None,
        alias="sequenceBinding",
    )


class IntegrationRunInspectData(PublicWireModel):
    run_id: str = Field(alias="runId")
    project_name: str = Field(alias="projectName")
    repository_root: str = Field(alias="repositoryRoot")
    state: str
    submitted_at: str = Field(alias="submittedAt")
    updated_at: str = Field(alias="updatedAt")
    review_budget: IntegrationReviewBudget = Field(alias="reviewBudget")
    sequence_binding: IntegrationSequenceBinding | None = Field(
        None,
        alias="sequenceBinding",
    )
    residual_risk: bool | None = Field(None, alias="residualRisk")
    block_reason: str | None = Field(None, alias="blockReason")
    cursor_wait_until: str | None = Field(None, alias="cursorWaitUntil")
    safe_next_action: IntegrationSafeNextAction = Field(alias="safeNextAction")
    attempt_count: StrictInt = Field(alias="attemptCount", ge=0)


class IntegrationAttemptItem(PublicWireModel):
    attempt_id: str = Field(alias="attemptId")
    component: str
    effect_kind: str | None = Field(None, alias="effectKind")
    iteration: StrictInt = Field(ge=1)
    phase_attempt: StrictInt = Field(alias="phaseAttempt", ge=1)
    status: str
    created_at: str = Field(alias="createdAt")
    launch_requested_at: str | None = Field(None, alias="launchRequestedAt")
    completed_at: str | None = Field(None, alias="completedAt")
    observed_duration_seconds: float | None = Field(None, alias="observedDurationSeconds")


class IntegrationHistoryItem(PublicWireModel):
    sequence: StrictInt = Field(ge=1)
    kind: str
    timestamp: str
    safe_detail: str = Field(alias="safeDetail")


class IntegrationCollectionPage(PublicWireModel):
    offset: StrictInt = Field(ge=0)
    limit: StrictInt = Field(ge=1, le=500)
    next_offset: StrictInt | None = Field(None, alias="nextOffset", ge=0)
    has_more: StrictBool = Field(alias="hasMore")

    @model_validator(mode="after")
    def _check_pagination_offsets(self) -> IntegrationCollectionPage:
        if not self.has_more and self.next_offset is not None:
            raise ValueError("nextOffset must be null when hasMore is false")
        if self.has_more and self.next_offset is None:
            raise ValueError("nextOffset is required when hasMore is true")
        return self


class IntegrationRunListData(PublicWireModel):
    items: tuple[IntegrationRunListItem, ...]
    page: IntegrationCollectionPage


class IntegrationAttemptListData(PublicWireModel):
    run_id: str = Field(alias="runId")
    items: tuple[IntegrationAttemptItem, ...]
    page: IntegrationCollectionPage


class IntegrationHistoryListData(PublicWireModel):
    run_id: str = Field(alias="runId")
    items: tuple[IntegrationHistoryItem, ...]
    page: IntegrationCollectionPage


class IntegrationFrozenArtifactChunk(ArtifactChunk):
    run_id: str = Field(alias="runId")
    artifact_kind: str = Field(alias="artifactKind")
    source_repository_path: str = Field(alias="sourceRepositoryPath")


class IntegrationSequenceAggregateCounts(PublicWireModel):
    planned: StrictInt = Field(ge=0)
    materialized: StrictInt = Field(ge=0)
    accepted: StrictInt = Field(ge=0)
    residual_risk: StrictInt = Field(alias="residualRisk", ge=0)
    checkpointed: StrictInt = Field(ge=0)
    cancelled: StrictInt = Field(ge=0)
    remaining: StrictInt = Field(ge=0)
    attempts: StrictInt = Field(ge=0)


class IntegrationReportAvailability(PublicWireModel):
    available: StrictBool
    reason: str | None = None
    sha256_prefix: str | None = Field(None, alias="sha256Prefix")


class IntegrationPhaseCheckpointSummary(PublicWireModel):
    available: StrictBool
    commit_sha256_prefix: str | None = Field(None, alias="commitSha256Prefix")


class IntegrationPhaseFrozenInputs(PublicWireModel):
    plan_available: StrictBool = Field(alias="planAvailable")
    prompt_available: StrictBool = Field(alias="promptAvailable")


class IntegrationSequencePhaseRunItem(PublicWireModel):
    run_id: str = Field(alias="runId")
    generation: StrictInt = Field(ge=1)
    source_run_id: str | None = Field(None, alias="sourceRunId")
    attempt_kind: str = Field(alias="attemptKind")
    materialized_at: str = Field(alias="materializedAt")
    resolved_at: str | None = Field(None, alias="resolvedAt")
    state: str
    terminal_outcome: str | None = Field(None, alias="terminalOutcome")


class IntegrationSequencePhaseSummary(PublicWireModel):
    ordinal: StrictInt = Field(ge=1)
    name: str
    initial_planned_run_id: str = Field(alias="initialPlannedRunId")
    materialized: StrictBool
    current_run_id: str | None = Field(None, alias="currentRunId")
    accepted_run_id: str | None = Field(None, alias="acceptedRunId")
    cancelled: StrictBool
    accepted_outcome: str | None = Field(None, alias="acceptedOutcome")
    residual_risk: StrictBool = Field(alias="residualRisk")
    checkpoint: IntegrationPhaseCheckpointSummary
    frozen_inputs: IntegrationPhaseFrozenInputs = Field(alias="frozenInputs")
    runs: tuple[IntegrationSequencePhaseRunItem, ...]
    runs_has_more: StrictBool = Field(alias="runsHasMore")
    runs_next_offset: StrictInt | None = Field(None, alias="runsNextOffset", ge=0)
    run_count: StrictInt = Field(alias="runCount", ge=0)


class IntegrationSequenceListItem(PublicWireModel):
    sequence_id: str = Field(alias="sequenceId")
    name: str
    project_name: str = Field(alias="projectName")
    repository_root: str = Field(alias="repositoryRoot")
    state: str
    prepared_at: str = Field(alias="preparedAt")
    updated_at: str = Field(alias="updatedAt")


class IntegrationSequenceListData(PublicWireModel):
    items: tuple[IntegrationSequenceListItem, ...]
    page: IntegrationCollectionPage


class IntegrationSequenceInspectData(PublicWireModel):
    sequence_id: str = Field(alias="sequenceId")
    name: str
    project_name: str = Field(alias="projectName")
    repository_root: str = Field(alias="repositoryRoot")
    state: str
    prepared_at: str = Field(alias="preparedAt")
    started_at: str | None = Field(None, alias="startedAt")
    updated_at: str = Field(alias="updatedAt")
    finalized_at: str | None = Field(None, alias="finalizedAt")
    current_phase_ordinal: StrictInt | None = Field(None, alias="currentPhaseOrdinal", ge=1)
    current_run_id: str | None = Field(None, alias="currentRunId")
    current_run_state: str | None = Field(None, alias="currentRunState")
    aggregate_counts: IntegrationSequenceAggregateCounts = Field(alias="aggregateCounts")
    residual_risk: bool | None = Field(None, alias="residualRisk")
    residual_risk_ordinals: tuple[StrictInt, ...] = Field(
        default=(),
        alias="residualRiskOrdinals",
    )
    block_reason: str | None = Field(None, alias="blockReason")
    safe_next_action: IntegrationSafeNextAction = Field(alias="safeNextAction")
    report: IntegrationReportAvailability
    phases: tuple[IntegrationSequencePhaseSummary, ...]


class IntegrationSequencePhaseRunListData(PublicWireModel):
    sequence_id: str = Field(alias="sequenceId")
    ordinal: StrictInt = Field(ge=1)
    items: tuple[IntegrationSequencePhaseRunItem, ...]
    page: IntegrationCollectionPage
    run_count: StrictInt = Field(alias="runCount", ge=0)


class IntegrationFrozenSequenceArtifactChunk(ArtifactChunk):
    sequence_id: str = Field(alias="sequenceId")
    ordinal: StrictInt = Field(ge=1)
    artifact_kind: str = Field(alias="artifactKind")
    source_repository_path: str = Field(alias="sourceRepositoryPath")


class IntegrationSequenceReportChunk(ArtifactChunk):
    sequence_id: str = Field(alias="sequenceId")
    integrity: str | None = None


class IntegrationContentAvailability(PublicWireModel):
    available: StrictBool
    reason: str | None = None


class IntegrationReviewContentAvailability(PublicWireModel):
    prompt: IntegrationContentAvailability
    response: IntegrationContentAvailability
    review_markdown: IntegrationContentAvailability = Field(alias="reviewMarkdown")
    cursor_fix_prompt: IntegrationContentAvailability = Field(alias="cursorFixPrompt")


class IntegrationReviewListItem(PublicWireModel):
    attempt_id: str = Field(alias="attemptId")
    iteration: StrictInt = Field(ge=1)
    phase_attempt: StrictInt = Field(alias="phaseAttempt", ge=1)
    status: str
    review_mode: str = Field(alias="reviewMode")
    review_model: str = Field(alias="reviewModel")
    reasoning_effort: str = Field(alias="reasoningEffort")
    created_at: str = Field(alias="createdAt")
    launch_requested_at: str | None = Field(None, alias="launchRequestedAt")
    completed_at: str | None = Field(None, alias="completedAt")
    findings_count: StrictInt | None = Field(None, alias="findingsCount", ge=0)
    highest_severity: str | None = Field(None, alias="highestSeverity")
    tests_status: str | None = Field(None, alias="testsStatus")
    reviewer_session_ref: str | None = Field(None, alias="reviewerSessionRef")
    content: IntegrationReviewContentAvailability
    process_output: IntegrationProcessOutputAvailability = Field(alias="processOutput")


class IntegrationReviewListData(PublicWireModel):
    run_id: str = Field(alias="runId")
    items: tuple[IntegrationReviewListItem, ...]
    page: IntegrationCollectionPage


class IntegrationReviewResponseBody(PublicWireModel):
    has_actionable_findings: StrictBool = Field(alias="hasActionableFindings")
    findings_count: StrictInt = Field(alias="findingsCount", ge=0)
    highest_severity: str | None = Field(None, alias="highestSeverity")
    review_markdown: str = Field(alias="reviewMarkdown")
    cursor_fix_prompt: str | None = Field(None, alias="cursorFixPrompt")
    tests_status: str = Field(alias="testsStatus")
    summary: str


class IntegrationReviewDetailData(PublicWireModel):
    run_id: str = Field(alias="runId")
    attempt_id: str = Field(alias="attemptId")
    iteration: StrictInt = Field(ge=1)
    phase_attempt: StrictInt = Field(alias="phaseAttempt", ge=1)
    status: str
    review_mode: str = Field(alias="reviewMode")
    review_model: str = Field(alias="reviewModel")
    reasoning_effort: str = Field(alias="reasoningEffort")
    created_at: str = Field(alias="createdAt")
    launch_requested_at: str | None = Field(None, alias="launchRequestedAt")
    completed_at: str | None = Field(None, alias="completedAt")
    findings_count: StrictInt | None = Field(None, alias="findingsCount", ge=0)
    highest_severity: str | None = Field(None, alias="highestSeverity")
    tests_status: str | None = Field(None, alias="testsStatus")
    reviewer_session_ref: str | None = Field(None, alias="reviewerSessionRef")
    result_state: str = Field(alias="resultState")
    response: IntegrationReviewResponseBody | None = None
    content: IntegrationReviewContentAvailability
    process_output: IntegrationProcessOutputAvailability = Field(alias="processOutput")


class IntegrationReviewContentChunk(ArtifactChunk):
    run_id: str = Field(alias="runId")
    attempt_id: str = Field(alias="attemptId")
    content_kind: str = Field(alias="contentKind")


class IntegrationProcessStreamAvailability(PublicWireModel):
    stream: str
    available: StrictBool
    reason: str | None = None


class IntegrationProcessOutputAvailability(PublicWireModel):
    stdout: IntegrationProcessStreamAvailability
    stderr: IntegrationProcessStreamAvailability


class IntegrationProcessOutputChunk(ArtifactChunk):
    run_id: str = Field(alias="runId")
    attempt_id: str = Field(alias="attemptId")
    component: str
    stream: str
    stream_format: str = Field(alias="format")
    process_state: str = Field(alias="processState")
    complete: StrictBool
    truncated_at_source: bool | None = Field(None, alias="truncatedAtSource")
    stored_bytes: StrictInt | None = Field(None, alias="storedBytes", ge=0)
