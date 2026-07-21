"""Shared immutable types for the PR review v2 pure domain."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
NonEmptyId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]*$"),
]
GitSha40 = Annotated[
    str, StringConstraints(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
]
Sha256Hex = Annotated[
    str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
]
ThreadId = Annotated[str, StringConstraints(min_length=1, max_length=256, pattern=r"^[^\s/\\]+$")]


def coerce_utc_instant(value: object) -> datetime:
    """Parse a timezone-aware ISO-8601 instant and normalize to UTC.

    Naive timestamps are rejected. Equivalent instants with different offsets
    compare equal after normalization.
    """

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("timestamp must be non-empty")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("timestamp must be ISO-8601 with an explicit timezone") from exc
    else:
        raise TypeError("timestamp must be str or datetime")
    if dt.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return dt.astimezone(UTC)


UtcInstant = Annotated[datetime, BeforeValidator(coerce_utc_instant)]

T = TypeVar("T")


class DomainModel(BaseModel):
    """Base for every Phase 16.3 domain model."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TransientErrorKind(StrEnum):
    TIMEOUT = "timeout"
    DNS_FAILURE = "dns_failure"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_RESET = "connection_reset"
    NETWORK_UNAVAILABLE = "network_unavailable"
    HTTP_429 = "http_429"
    PRIMARY_RATE_LIMIT = "primary_rate_limit"
    SECONDARY_RATE_LIMIT = "secondary_rate_limit"
    HTTP_500 = "http_500"
    HTTP_502 = "http_502"
    HTTP_503 = "http_503"
    HTTP_504 = "http_504"
    OTHER_HTTP_5XX = "other_http_5xx"
    TEMPORARY_CLI_FAILURE = "temporary_cli_failure"


class PauseReasonKind(StrEnum):
    RETRY_EXHAUSTED = "retry_exhausted"
    AUTHENTICATION = "authentication"
    PERMISSIONS = "permissions"
    NOT_FOUND = "not_found"
    CLOSED_PR = "closed_pr"
    REPOSITORY_DRIFT = "repository_drift"
    HEAD_DRIFT = "head_drift"
    BRANCH_DRIFT = "branch_drift"
    PATCH_DRIFT = "patch_drift"
    THREAD_DRIFT = "thread_drift"
    HTTP_VALIDATION_REJECTION = "http_validation_rejection"
    AMBIGUOUS_WRITE_UNRESOLVED = "ambiguous_write_unresolved"
    EXTERNAL_CYCLE_LIMIT_REACHED = "external_cycle_limit_reached"
    LOCAL_FIX_PAUSED = "local_fix_paused"
    LOCAL_FIX_LIMIT_REACHED = "local_fix_limit_reached"
    LOCAL_FIX_FAILED = "local_fix_failed"
    REQUIRED_OPERATOR_ACTION = "required_operator_action"


class FailureReasonKind(StrEnum):
    CORRUPT_ARTIFACT = "corrupt_artifact"
    HASH_MISMATCH = "hash_mismatch"
    INVARIANT_VIOLATION = "invariant_violation"
    INVALID_TRUSTED_EFFECT_RESULT = "invalid_trusted_effect_result"
    INTERNAL_CORRUPTION = "internal_corruption"


class SafeActionKind(StrEnum):
    WAIT_THEN_RESUME = "wait_then_resume"
    RESUME_SAME_EFFECT = "resume_same_effect"
    FIX_AUTH_THEN_RESUME = "fix_auth_then_resume"
    FIX_PERMISSIONS_THEN_RESUME = "fix_permissions_then_resume"
    RECONCILE_OR_INSPECT = "reconcile_or_inspect"
    CONTINUE_AFTER_USER_REPLY = "continue_after_user_reply"
    OPEN_NEW_CYCLE_OR_STOP = "open_new_cycle_or_stop"
    NO_FURTHER_WORK = "no_further_work"
    INSPECT_ARTIFACTS = "inspect_artifacts"


class RejectionCode(StrEnum):
    UNSUPPORTED_TRANSITION = "unsupported_transition"
    TERMINAL_STATE = "terminal_state"
    STALE_EFFECT_ID = "stale_effect_id"
    STALE_CYCLE = "stale_cycle"
    STALE_HEAD_SHA = "stale_head_sha"
    MISMATCHED_OUTCOME_KIND = "mismatched_outcome_kind"
    MISMATCHED_ATTEMPT = "mismatched_attempt"
    EARLY_RETRY_TIMER = "early_retry_timer"
    MISSING_TRIGGER_EVIDENCE = "missing_trigger_evidence"
    DUPLICATE_COMPLETION = "duplicate_completion"
    USER_CONTINUATION_BEFORE_REPLIES = "user_continuation_before_replies"
    RESUME_WITHOUT_CONTINUATION = "resume_without_continuation"
    WRITE_UNCERTAIN_FOR_NON_MUTATING = "write_uncertain_for_non_mutating"
    INVALID_NO_FINDINGS_BINDING = "invalid_no_findings_binding"
    INVALID_THREAD_COVERAGE = "invalid_thread_coverage"
    INVALID_ORIGIN_FOR_EVENT = "invalid_origin_for_event"
    NO_ACTIVE_EFFECT = "no_active_effect"
    INVALID_TOKEN_SHAPE = "invalid_token_shape"
    WRONG_THREAD_ID = "wrong_thread_id"
    INVALID_USER_CONTINUATION = "invalid_user_continuation"
    INVALID_RETRY_TARGET = "invalid_retry_target"
    MISMATCHED_OUTCOME_BINDING = "mismatched_outcome_binding"
    MISMATCHED_ORIGINAL_WRITE = "mismatched_original_write"
    INVALID_POLL_SEQUENCE = "invalid_poll_sequence"
    RETRY_TIME_NOT_IN_FUTURE = "retry_time_not_in_future"
    MISSING_RETRY_ELIGIBILITY = "missing_retry_eligibility"
    INVARIANT_VIOLATION = "invariant_violation"


class AdjudicationDecisionKind(StrEnum):
    ACTIONABLE = "actionable"
    NOT_APPLICABLE = "not_applicable"
    UNCERTAIN = "uncertain"


class LocalFixOutcomeKind(StrEnum):
    ACCEPTED = "accepted"
    ACCEPTED_WITH_RESIDUAL_RISK = "accepted_with_residual_risk"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    PAUSED = "paused"
    FAILED = "failed"
    ABORTED = "aborted"


class ReconciliationResolutionKind(StrEnum):
    APPLIED = "applied"
    PROVEN_NOT_APPLIED = "proven_not_applied"
    UNRESOLVED = "unresolved"


class ReconciliationStrategyKind(StrEnum):
    FIND_COMMIT_AT_HEAD = "find_commit_at_head"
    FIND_REMOTE_REF = "find_remote_ref"
    FIND_PR_BY_HEAD_BASE = "find_pr_by_head_base"
    FIND_REVIEW_MARKER = "find_review_marker"
    FIND_THREAD_REPLY = "find_thread_reply"
    FIND_PR_TEXT = "find_pr_text"
    FIND_THREAD_RESOLVED = "find_thread_resolved"


class PublicationStep(StrEnum):
    GENERATE_PUBLICATION_TEXT = "generate_publication_text"
    COMMIT_PATCH = "commit_patch"
    PUSH_COMMIT = "push_commit"
    CREATE_OR_UPDATE_PR = "create_or_update_pr"
    UPDATE_PR_TEXT = "update_pr_text"
    RESOLVE_THREAD = "resolve_thread"
    COMPLETE = "complete"


class EffectClassification(StrEnum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    LOCAL = "local"
    RECONCILING = "reconciling"


class ArtifactRef(DomainModel):
    relative_path: NonEmptyStr
    sha256: Sha256Hex

    @field_validator("relative_path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("relative_path must be non-empty")
        if path.startswith("/") or path.startswith("\\"):
            raise ValueError("relative_path must be run-relative")
        if "\\" in path:
            raise ValueError("relative_path must use forward slashes")
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ValueError("relative_path must be a lexical safe run-relative path")
        return path


class RepositoryIdentity(DomainModel):
    name_with_owner: Annotated[
        str, StringConstraints(min_length=3, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    ]


class PullRequestBinding(DomainModel):
    repository: RepositoryIdentity
    pr_number: PositiveInt
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    head_sha: GitSha40


class SourceRunOrigin(DomainModel):
    kind: Literal["source_run"] = "source_run"
    source_run_id: NonEmptyId
    repository: RepositoryIdentity
    head_branch: NonEmptyStr
    base_branch: NonEmptyStr
    expected_head_sha: GitSha40
    accepted_patch: ArtifactRef
    execution_context_ref: ArtifactRef


class ExistingPrOrigin(DomainModel):
    kind: Literal["existing_pr"] = "existing_pr"
    binding: PullRequestBinding
    execution_context_ref: ArtifactRef


PrReviewOrigin = Annotated[SourceRunOrigin | ExistingPrOrigin, Field(discriminator="kind")]


class WorkflowLimits(DomainModel):
    max_external_cycles: PositiveInt
    max_local_iterations: PositiveInt
    github_max_attempts_per_batch: PositiveInt = 6

    @model_validator(mode="after")
    def validate_github_attempts(self) -> WorkflowLimits:
        if self.github_max_attempts_per_batch != 6:
            raise ValueError("github_max_attempts_per_batch must be 6 for Phase 16.3")
        return self


class EffectCompletionToken(DomainModel):
    effect_id: NonEmptyId
    expected_run_version: PositiveInt
    lease_generation: PositiveInt
    cycle_number: PositiveInt
    bound_head_sha: GitSha40


class ErrorSummary(DomainModel):
    kind: TransientErrorKind | PauseReasonKind | FailureReasonKind
    safe_summary: NonEmptyStr

    @field_validator("safe_summary")
    @classmethod
    def reject_sensitive_markers(cls, value: str) -> str:
        lowered = value.lower()
        banned = ("-----begin", "authorization:", "api_key", "token=", "password=")
        if any(marker in lowered for marker in banned):
            raise ValueError("safe_summary must not contain sensitive markers")
        return value


class SafeAction(DomainModel):
    kind: SafeActionKind
    condition: NonEmptyStr


class TriggerEvidence(DomainModel):
    marker: NonEmptyId
    comment_ref: ArtifactRef
    head_sha: GitSha40


class VerifiedNoFindingsEvidence(DomainModel):
    head_sha: GitSha40
    observation_ref: ArtifactRef
    verified_at: UtcInstant


class FrozenThreadSet(DomainModel):
    thread_ids: tuple[ThreadId, ...]
    snapshot_ref: ArtifactRef
    head_sha: GitSha40
    cycle_number: PositiveInt
    trigger_marker: NonEmptyId

    @model_validator(mode="after")
    def validate_threads(self) -> FrozenThreadSet:
        if not self.thread_ids:
            raise ValueError("frozen thread set must be non-empty")
        if len(self.thread_ids) != len(set(self.thread_ids)):
            raise ValueError("frozen thread IDs must be unique")
        return self


class ThreadDecisionRecord(DomainModel):
    thread_id: ThreadId
    decision: AdjudicationDecisionKind
    safe_summary: NonEmptyStr
    reply_ref: ArtifactRef | None = None


class AdjudicationEvidence(DomainModel):
    frozen: FrozenThreadSet
    decisions: tuple[ThreadDecisionRecord, ...]
    result_ref: ArtifactRef
    fix_prompt_ref: ArtifactRef | None = None

    @model_validator(mode="after")
    def validate_coverage(self) -> AdjudicationEvidence:
        decision_ids = tuple(item.thread_id for item in self.decisions)
        if len(decision_ids) != len(set(decision_ids)):
            raise ValueError("adjudication decisions must have unique thread IDs")
        if set(decision_ids) != set(self.frozen.thread_ids):
            raise ValueError("adjudication must cover exactly the frozen thread set")
        all_actionable = all(
            item.decision is AdjudicationDecisionKind.ACTIONABLE for item in self.decisions
        )
        if all_actionable:
            if self.fix_prompt_ref is None:
                raise ValueError("all-actionable adjudication requires fix_prompt_ref")
            if any(item.reply_ref is not None for item in self.decisions):
                raise ValueError("all-actionable adjudication forbids reply_ref values")
        else:
            if self.fix_prompt_ref is not None:
                raise ValueError("non-actionable adjudication forbids fix_prompt_ref")
            for item in self.decisions:
                if item.decision is AdjudicationDecisionKind.ACTIONABLE:
                    if item.reply_ref is not None:
                        raise ValueError("actionable decisions must not carry reply_ref")
                elif item.reply_ref is None:
                    raise ValueError(
                        "not_applicable/uncertain decisions require a reply artifact ref"
                    )
        return self


class ReplyIntent(DomainModel):
    thread_id: ThreadId
    reply_ref: ArtifactRef


class UserContinuationEvidence(DomainModel):
    repository: RepositoryIdentity
    pr_number: PositiveInt
    cycle_number: PositiveInt
    head_sha: GitSha40
    evidence_ref: ArtifactRef


def require_unique_nonempty(values: tuple[T, ...], *, label: str) -> tuple[T, ...]:
    if not values:
        raise ValueError(f"{label} must be non-empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")
    return values


def cycle_label(cycle_number: int) -> str:
    if cycle_number < 1:
        raise ValueError("cycle_number must be >= 1")
    return f"{cycle_number:02d}"


def build_effect_identity(
    *,
    run_id: str,
    cycle_number: int,
    operation: str,
    target: str | None = None,
) -> str:
    base = f"pr-review:{run_id}:cycle:{cycle_label(cycle_number)}:{operation}"
    if target is None:
        return base
    if not target:
        raise ValueError("target must be non-empty when provided")
    return f"{base}:{target}"


def model_dump_jsonable(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
