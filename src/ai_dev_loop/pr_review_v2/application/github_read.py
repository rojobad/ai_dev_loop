"""Typed GitHub read policy, observation contracts, and pure backoff helpers."""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import (
    Field,
    PositiveInt,
    StringConstraints,
    field_validator,
    model_validator,
)

from ai_dev_loop.pr_review_v2.application.contracts import AppModel
from ai_dev_loop.pr_review_v2.domain.common import (
    ArtifactRef,
    ErrorSummary,
    GitSha40,
    NonEmptyId,
    NonEmptyStr,
    PauseReasonKind,
    PullRequestBinding,
    SafeAction,
    SafeActionKind,
    TransientErrorKind,
    UtcInstant,
)

DEFAULT_REVIEWER_LOGIN = "chatgpt-codex-connector"
DEFAULT_POLL_INTERVAL_SECONDS = 60
LOCAL_RETRY_BASE_DELAYS_SECONDS: tuple[int, ...] = (10, 30, 90, 180, 300)
MAX_SERVER_DIRECTED_WAIT_SECONDS = 3600
MAX_TOTAL_SANITIZED_CHARS_LIMIT = 2_000_000
DEFAULT_REVIEWED_COMMIT_PREFIX_LENGTH = 12
JITTER_MIN = -0.20
JITTER_MAX = 0.20

NonEmptyLogin = Annotated[
    str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)
]


class ObservationEvidenceKind(StrEnum):
    BOT_STILL_WAITING = "bot_still_waiting"
    VERIFIED_NO_FINDINGS = "verified_no_findings"
    ELIGIBLE_THREADS = "eligible_threads"


class RateLimitClass(StrEnum):
    NONE = "none"
    HTTP_429 = "http_429"
    PRIMARY = "primary"
    SECONDARY = "secondary"


@runtime_checkable
class JitterSource(Protocol):
    """Injectable source of inclusive jitter factors in [-0.20, +0.20]."""

    def sample(self) -> float: ...


class FixedJitter:
    """Deterministic jitter for tests and injected production defaults."""

    def __init__(self, factor: float = 0.0) -> None:
        if factor < JITTER_MIN or factor > JITTER_MAX:
            raise ValueError("jitter factor must be within [-0.20, +0.20]")
        self._factor = factor

    def sample(self) -> float:
        return self._factor


class GitHubReadPolicy(AppModel):
    """Constructor-injected read policy; never reads ProjectConfig."""

    gh_command: NonEmptyStr = "gh"
    repository_cwd: NonEmptyStr
    per_call_timeout_seconds: float = Field(default=60.0, gt=0.0, le=600.0)
    overall_timeout_seconds: float = Field(default=180.0, gt=0.0, le=7200.0)
    max_pages: PositiveInt = Field(default=20, le=100)
    max_items: PositiveInt = Field(default=500, le=5000)
    reviewer_logins: tuple[NonEmptyLogin, ...] = (DEFAULT_REVIEWER_LOGIN,)
    poll_interval_seconds: PositiveInt = Field(default=DEFAULT_POLL_INTERVAL_SECONDS, le=3600)
    accepted_no_findings_prefixes: tuple[str, ...] = ()
    accept_bot_thumbs_up: bool = False
    reviewed_commit_prefix_length: int = Field(
        default=DEFAULT_REVIEWED_COMMIT_PREFIX_LENGTH, ge=7, le=40
    )
    max_server_directed_wait_seconds: PositiveInt = Field(
        default=MAX_SERVER_DIRECTED_WAIT_SECONDS, le=MAX_SERVER_DIRECTED_WAIT_SECONDS
    )
    max_sanitized_body_chars: PositiveInt = Field(default=16_384, le=200_000)
    max_total_sanitized_chars: PositiveInt = Field(
        default=200_000, le=MAX_TOTAL_SANITIZED_CHARS_LIMIT
    )

    @field_validator("accepted_no_findings_prefixes")
    @classmethod
    def validate_prefixes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for prefix in value:
            if not prefix or not prefix.strip():
                raise ValueError("accepted_no_findings_prefixes entries must be non-empty")
            if "\x00" in prefix:
                raise ValueError("accepted_no_findings_prefixes must not contain NUL")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> GitHubReadPolicy:
        if not self.reviewer_logins:
            raise ValueError("reviewer_logins must be non-empty")
        if self.overall_timeout_seconds < self.per_call_timeout_seconds:
            raise ValueError("overall_timeout_seconds must be >= per_call_timeout_seconds")
        if self.max_total_sanitized_chars < self.max_sanitized_body_chars:
            raise ValueError("max_total_sanitized_chars must be >= max_sanitized_body_chars")
        return self

    @property
    def no_findings_enabled(self) -> bool:
        return bool(self.accepted_no_findings_prefixes) or self.accept_bot_thumbs_up

    @property
    def comment_no_findings_enabled(self) -> bool:
        return bool(self.accepted_no_findings_prefixes)


class AllowlistedHeaders(AppModel):
    """Normalized allowlisted response headers for classification and backoff."""

    retry_after_seconds: int | None = None
    rate_limit_remaining: int | None = None
    rate_limit_reset_epoch: int | None = None
    rate_limit_resource: str | None = None


class TransportRequestKind(StrEnum):
    GRAPHQL = "graphql"
    REST_GET = "rest_get"


class GhTransportRequest(AppModel):
    """Private-to-gateway request shape; not a public arbitrary-query API."""

    kind: TransportRequestKind
    operation: NonEmptyStr
    argv_suffix: tuple[NonEmptyStr, ...]
    stdin_text: str | None = None


class GhTransportResult(AppModel):
    http_status: PositiveInt
    headers: AllowlistedHeaders
    body_json: object | None = None
    returncode: int
    timed_out: bool = False
    argv: tuple[str, ...] = ()


class ObservedTriggerComment(AppModel):
    comment_id: NonEmptyStr
    author_login: NonEmptyLogin
    created_at: UtcInstant
    body_sha256: Annotated[
        str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ]


class ObservedIssueComment(AppModel):
    comment_id: NonEmptyStr
    author_login: NonEmptyLogin
    created_at: UtcInstant
    body_sha256: Annotated[
        str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ]
    matched_no_findings_rule_id: str | None = None
    reviewed_commit_prefix: str | None = None
    sanitized_body: str | None = None


class ObservedReviewThread(AppModel):
    thread_id: NonEmptyStr
    is_resolved: bool
    author_login: NonEmptyLogin
    created_at: UtcInstant
    commit_sha: GitSha40
    root_comment_id: NonEmptyStr
    root_body_sha256: Annotated[
        str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    ]
    path: str | None = None
    line: int | None = None
    review_id: str | None = None
    sanitized_root_body: NonEmptyStr


class ObservedReaction(AppModel):
    reaction_id: NonEmptyStr
    user_login: NonEmptyLogin
    content: NonEmptyStr
    created_at: UtcInstant | None = None


class ObservedNoFindingsReaction(AppModel):
    """Verified thumbs-up reaction on the exact trigger comment."""

    rule_id: Literal["accept_bot_thumbs_up"] = "accept_bot_thumbs_up"
    trigger_comment_id: NonEmptyStr
    reaction_id: NonEmptyStr
    user_login: NonEmptyLogin
    content: Literal["+1"] = "+1"
    created_at: UtcInstant


class ObservationSnapshot(AppModel):
    """Validated complete observation before artifact persistence."""

    schema_version: Literal[1] = 1
    binding: PullRequestBinding
    cycle_number: PositiveInt
    poll_sequence: PositiveInt
    trigger_marker: NonEmptyId
    observed_at: UtcInstant
    evidence_kind: ObservationEvidenceKind
    trigger: ObservedTriggerComment
    eligible_threads: tuple[ObservedReviewThread, ...] = ()
    no_findings_comment: ObservedIssueComment | None = None
    no_findings_reaction: ObservedNoFindingsReaction | None = None
    reactions: tuple[ObservedReaction, ...] = ()


class GatewayBlockKind(StrEnum):
    AUTHENTICATION = "authentication"
    PERMISSIONS = "permissions"
    NOT_FOUND = "not_found"
    CLOSED_PR = "closed_pr"
    UNSUPPORTED_FORK = "unsupported_fork"
    REPOSITORY_DRIFT = "repository_drift"
    HEAD_DRIFT = "head_drift"
    BRANCH_DRIFT = "branch_drift"
    HTTP_VALIDATION_REJECTION = "http_validation_rejection"
    MALFORMED_EVIDENCE = "malformed_evidence"
    CONTRADICTORY_EVIDENCE = "contradictory_evidence"
    MISSING_EXECUTABLE = "missing_executable"
    ARTIFACT_FAILURE = "artifact_failure"


class GatewayTransientKind(StrEnum):
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


class GatewayBlock(AppModel):
    kind: GatewayBlockKind
    safe_summary: NonEmptyStr
    pause_reason: PauseReasonKind
    safe_action: SafeAction
    http_status: int | None = None


class GatewayTransient(AppModel):
    kind: GatewayTransientKind
    safe_summary: NonEmptyStr
    transient_kind: TransientErrorKind
    headers: AllowlistedHeaders = Field(default_factory=AllowlistedHeaders)
    http_status: int | None = None
    rate_limit_class: RateLimitClass = RateLimitClass.NONE


class GatewayObservationSuccess(AppModel):
    snapshot: ObservationSnapshot
    artifact_ref: ArtifactRef | None = None


class BackoffDecision(AppModel):
    next_attempt_at: UtcInstant
    base_delay_seconds: float
    applied_jitter_factor: float | None
    server_directed: bool
    clamped_to_one_hour: bool = False


def local_base_delay_seconds(failed_attempt: int) -> int:
    """Return the local base delay after a failed attempt in 1..5."""

    if failed_attempt < 1 or failed_attempt > len(LOCAL_RETRY_BASE_DELAYS_SECONDS):
        raise ValueError("failed_attempt must be in 1..5 for local backoff")
    return LOCAL_RETRY_BASE_DELAYS_SECONDS[failed_attempt - 1]


def compute_retry_backoff(
    *,
    failed_attempt: int,
    observation_time: datetime,
    headers: AllowlistedHeaders,
    jitter: JitterSource,
    max_server_directed_wait_seconds: int = MAX_SERVER_DIRECTED_WAIT_SECONDS,
) -> BackoffDecision:
    """Compute absolute UTC eligibility for attempts 1..5.

    Precedence: valid Retry-After delta, else exhausted X-RateLimit-Reset, else
    local jittered delay. Server-directed waits are never jittered and are clamped
    to at most one hour after observation_time.
    """

    if observation_time.tzinfo is None:
        raise ValueError("observation_time must be timezone-aware")
    if failed_attempt < 1 or failed_attempt > 5:
        raise ValueError("failed_attempt must be in 1..5")
    # Absolute one-hour ceiling regardless of injected policy/callers.
    max_wait = min(int(max_server_directed_wait_seconds), MAX_SERVER_DIRECTED_WAIT_SECONDS)
    if max_wait < 1:
        raise ValueError("max_server_directed_wait_seconds must be >= 1")

    directed = _server_directed_delay_seconds(
        observation_time=observation_time,
        headers=headers,
        max_wait=max_wait,
    )
    if directed is not None:
        delay, clamped = directed
        return BackoffDecision(
            next_attempt_at=observation_time + timedelta(seconds=delay),
            base_delay_seconds=delay,
            applied_jitter_factor=None,
            server_directed=True,
            clamped_to_one_hour=clamped,
        )

    base = float(local_base_delay_seconds(failed_attempt))
    factor = jitter.sample()
    if factor < JITTER_MIN or factor > JITTER_MAX:
        raise ValueError("jitter sample must be within [-0.20, +0.20]")
    delay = base * (1.0 + factor)
    return BackoffDecision(
        next_attempt_at=observation_time + timedelta(seconds=delay),
        base_delay_seconds=base,
        applied_jitter_factor=factor,
        server_directed=False,
        clamped_to_one_hour=False,
    )


def _server_directed_delay_seconds(
    *,
    observation_time: datetime,
    headers: AllowlistedHeaders,
    max_wait: int,
) -> tuple[float, bool] | None:
    if headers.retry_after_seconds is not None:
        if headers.retry_after_seconds < 0:
            return None
        delay = float(headers.retry_after_seconds)
        if delay == 0:
            return None
        if delay > max_wait:
            return float(max_wait), True
        return delay, False

    if (
        headers.rate_limit_remaining is not None
        and headers.rate_limit_remaining <= 0
        and headers.rate_limit_reset_epoch is not None
    ):
        reset_at = datetime.fromtimestamp(
            headers.rate_limit_reset_epoch, tz=observation_time.tzinfo
        )
        delta = (reset_at - observation_time).total_seconds()
        if delta <= 0:
            return None
        if delta > max_wait:
            return float(max_wait), True
        return delta, False
    return None


def map_block_to_domain(block: GatewayBlock) -> tuple[PauseReasonKind, SafeAction, str]:
    return block.pause_reason, block.safe_action, block.safe_summary


def map_transient_to_error_summary(transient: GatewayTransient) -> ErrorSummary:
    return ErrorSummary(kind=transient.transient_kind, safe_summary=transient.safe_summary)


def block_for_kind(kind: GatewayBlockKind, *, detail: str | None = None) -> GatewayBlock:
    """Centralized safe-action mapping for permanent observation failures."""

    mapping: dict[GatewayBlockKind, tuple[PauseReasonKind, SafeActionKind, str, str]] = {
        GatewayBlockKind.AUTHENTICATION: (
            PauseReasonKind.AUTHENTICATION,
            SafeActionKind.FIX_AUTH_THEN_RESUME,
            "repair gh authentication for the configured executable",
            "GitHub authentication failed",
        ),
        GatewayBlockKind.PERMISSIONS: (
            PauseReasonKind.PERMISSIONS,
            SafeActionKind.FIX_PERMISSIONS_THEN_RESUME,
            "grant required repository read permissions then resume",
            "GitHub permission denied",
        ),
        GatewayBlockKind.NOT_FOUND: (
            PauseReasonKind.NOT_FOUND,
            SafeActionKind.RESUME_SAME_EFFECT,
            "verify repository/PR visibility and the recorded binding before resume",
            "repository or pull request was not found",
        ),
        GatewayBlockKind.CLOSED_PR: (
            PauseReasonKind.CLOSED_PR,
            SafeActionKind.NO_FURTHER_WORK,
            "pull request is closed; no further bot observation work",
            "pull request is not open",
        ),
        GatewayBlockKind.UNSUPPORTED_FORK: (
            PauseReasonKind.REPOSITORY_DRIFT,
            SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
            "use a same-repository PR binding or stop",
            "cross-repository fork pull requests are unsupported",
        ),
        GatewayBlockKind.REPOSITORY_DRIFT: (
            PauseReasonKind.REPOSITORY_DRIFT,
            SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
            "open a new cycle for the observed repository or stop",
            "repository identity drifted from the bound effect",
        ),
        GatewayBlockKind.HEAD_DRIFT: (
            PauseReasonKind.HEAD_DRIFT,
            SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
            "open a new cycle for the observed head SHA or stop",
            "pull request head SHA drifted from the bound effect",
        ),
        GatewayBlockKind.BRANCH_DRIFT: (
            PauseReasonKind.BRANCH_DRIFT,
            SafeActionKind.OPEN_NEW_CYCLE_OR_STOP,
            "open a new cycle for the observed branches or stop",
            "pull request head/base branch drifted from the bound effect",
        ),
        GatewayBlockKind.HTTP_VALIDATION_REJECTION: (
            PauseReasonKind.HTTP_VALIDATION_REJECTION,
            SafeActionKind.INSPECT_ARTIFACTS,
            "inspect safe observation diagnostics and remote PR before resume",
            "GitHub rejected the read request as invalid",
        ),
        GatewayBlockKind.MALFORMED_EVIDENCE: (
            PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            SafeActionKind.INSPECT_ARTIFACTS,
            "restore one complete consistent observation before resume",
            "remote observation evidence was incomplete or malformed",
        ),
        GatewayBlockKind.CONTRADICTORY_EVIDENCE: (
            PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            SafeActionKind.INSPECT_ARTIFACTS,
            "restore one complete consistent observation before resume",
            "remote observation evidence was contradictory",
        ),
        GatewayBlockKind.MISSING_EXECUTABLE: (
            PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            SafeActionKind.RESUME_SAME_EFFECT,
            "install or repair the configured gh executable before resume",
            "configured gh executable was not found",
        ),
        GatewayBlockKind.ARTIFACT_FAILURE: (
            PauseReasonKind.REQUIRED_OPERATOR_ACTION,
            SafeActionKind.INSPECT_ARTIFACTS,
            "repair the protected artifact root and verify no corrupt artifact is reused",
            "failed to persist or verify a protected observation artifact",
        ),
    }
    pause, action_kind, condition, summary = mapping[kind]
    if detail:
        summary = detail
    return GatewayBlock(
        kind=kind,
        safe_summary=summary,
        pause_reason=pause,
        safe_action=SafeAction(kind=action_kind, condition=condition),
    )


def transient_for_http_status(
    status: int,
    *,
    headers: AllowlistedHeaders | None = None,
    rate_limit_class: RateLimitClass = RateLimitClass.NONE,
) -> GatewayTransient:
    hdrs = headers or AllowlistedHeaders()
    if rate_limit_class is RateLimitClass.PRIMARY:
        return GatewayTransient(
            kind=GatewayTransientKind.PRIMARY_RATE_LIMIT,
            safe_summary="GitHub primary rate limit exhausted",
            transient_kind=TransientErrorKind.PRIMARY_RATE_LIMIT,
            headers=hdrs,
            http_status=status,
            rate_limit_class=RateLimitClass.PRIMARY,
        )
    if rate_limit_class is RateLimitClass.SECONDARY:
        return GatewayTransient(
            kind=GatewayTransientKind.SECONDARY_RATE_LIMIT,
            safe_summary="GitHub secondary rate limit exhausted",
            transient_kind=TransientErrorKind.SECONDARY_RATE_LIMIT,
            headers=hdrs,
            http_status=status,
            rate_limit_class=RateLimitClass.SECONDARY,
        )
    if status == 429 or rate_limit_class is RateLimitClass.HTTP_429:
        return GatewayTransient(
            kind=GatewayTransientKind.HTTP_429,
            safe_summary="GitHub returned HTTP 429",
            transient_kind=TransientErrorKind.HTTP_429,
            headers=hdrs,
            http_status=status,
            rate_limit_class=RateLimitClass.HTTP_429,
        )
    mapping = {
        500: (
            GatewayTransientKind.HTTP_500,
            TransientErrorKind.HTTP_500,
            "GitHub returned HTTP 500",
        ),
        502: (
            GatewayTransientKind.HTTP_502,
            TransientErrorKind.HTTP_502,
            "GitHub returned HTTP 502",
        ),
        503: (
            GatewayTransientKind.HTTP_503,
            TransientErrorKind.HTTP_503,
            "GitHub returned HTTP 503",
        ),
        504: (
            GatewayTransientKind.HTTP_504,
            TransientErrorKind.HTTP_504,
            "GitHub returned HTTP 504",
        ),
    }
    if status in mapping:
        kind, transient, summary = mapping[status]
        return GatewayTransient(
            kind=kind,
            safe_summary=summary,
            transient_kind=transient,
            headers=hdrs,
            http_status=status,
        )
    if 500 <= status <= 599:
        return GatewayTransient(
            kind=GatewayTransientKind.OTHER_HTTP_5XX,
            safe_summary=f"GitHub returned HTTP {status}",
            transient_kind=TransientErrorKind.OTHER_HTTP_5XX,
            headers=hdrs,
            http_status=status,
        )
    raise ValueError(f"status {status} is not a transient HTTP failure")
