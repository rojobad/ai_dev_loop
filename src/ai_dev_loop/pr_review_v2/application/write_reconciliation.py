"""Pure marker/proof/error mapping helpers for write reconciliation."""

from __future__ import annotations

from datetime import datetime, timedelta

from ai_dev_loop.pr_review_v2.application.github_read import (
    AllowlistedHeaders,
    FixedJitter,
    GatewayTransient,
    GatewayTransientKind,
    JitterSource,
    compute_retry_backoff,
    map_transient_to_error_summary,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ReconciliationProof,
    WriteProofKind,
    build_reconciliation_identity,
    resolution_from_proof,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    ErrorSummary,
    NonEmptyId,
    PauseReasonKind,
    ReconciliationStrategyKind,
    SafeAction,
    SafeActionKind,
    TransientErrorKind,
)
from ai_dev_loop.pr_review_v2.domain.effects import MutatingEffect, strategy_for_mutating_effect
from ai_dev_loop.pr_review_v2.domain.events import (
    ConfirmedWriteOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    ReconciliationResolvedOutcome,
    WriteOutcomeUncertain,
)

_STRATEGY_BY_KIND: dict[str, ReconciliationStrategyKind] = {
    "commit_patch": ReconciliationStrategyKind.FIND_COMMIT_AT_HEAD,
    "push_commit": ReconciliationStrategyKind.FIND_REMOTE_REF,
    "create_or_update_pr": ReconciliationStrategyKind.FIND_PR_BY_HEAD_BASE,
    "request_bot_review": ReconciliationStrategyKind.FIND_REVIEW_MARKER,
    "post_thread_reply": ReconciliationStrategyKind.FIND_THREAD_REPLY,
    "update_pr_text": ReconciliationStrategyKind.FIND_PR_TEXT,
    "resolve_thread": ReconciliationStrategyKind.FIND_THREAD_RESOLVED,
}

_TRANSIENT_KIND_TO_GATEWAY: dict[TransientErrorKind, GatewayTransientKind] = {
    TransientErrorKind.TIMEOUT: GatewayTransientKind.TIMEOUT,
    TransientErrorKind.DNS_FAILURE: GatewayTransientKind.DNS_FAILURE,
    TransientErrorKind.CONNECTION_REFUSED: GatewayTransientKind.CONNECTION_REFUSED,
    TransientErrorKind.CONNECTION_RESET: GatewayTransientKind.CONNECTION_RESET,
    TransientErrorKind.NETWORK_UNAVAILABLE: GatewayTransientKind.NETWORK_UNAVAILABLE,
    TransientErrorKind.HTTP_429: GatewayTransientKind.HTTP_429,
    TransientErrorKind.PRIMARY_RATE_LIMIT: GatewayTransientKind.PRIMARY_RATE_LIMIT,
    TransientErrorKind.SECONDARY_RATE_LIMIT: GatewayTransientKind.SECONDARY_RATE_LIMIT,
    TransientErrorKind.HTTP_500: GatewayTransientKind.HTTP_500,
    TransientErrorKind.HTTP_502: GatewayTransientKind.HTTP_502,
    TransientErrorKind.HTTP_503: GatewayTransientKind.HTTP_503,
    TransientErrorKind.HTTP_504: GatewayTransientKind.HTTP_504,
    TransientErrorKind.OTHER_HTTP_5XX: GatewayTransientKind.OTHER_HTTP_5XX,
    TransientErrorKind.TEMPORARY_CLI_FAILURE: GatewayTransientKind.TEMPORARY_CLI_FAILURE,
}


def strategy_for_original_write(original: MutatingEffect) -> ReconciliationStrategyKind:
    return strategy_for_mutating_effect(original)


def expected_strategy_for_kind(kind: str) -> ReconciliationStrategyKind:
    try:
        return _STRATEGY_BY_KIND[kind]
    except KeyError as exc:
        raise ValueError(f"unknown mutating kind: {kind}") from exc


def validate_reconcile_pair(
    *,
    original_write: MutatingEffect,
    strategy: ReconciliationStrategyKind,
) -> None:
    expected = strategy_for_original_write(original_write)
    if strategy != expected:
        raise ValueError("reconciliation strategy must match original write kind")


def uncertain_error_summary(
    kind: TransientErrorKind = TransientErrorKind.TEMPORARY_CLI_FAILURE,
    *,
    safe_summary: str = "write outcome is uncertain; reconcile before retry",
) -> ErrorSummary:
    return ErrorSummary(kind=kind, safe_summary=safe_summary)


def make_write_uncertain(
    *,
    token: EffectCompletionToken,
    original_write: MutatingEffect,
    occurred_at: datetime,
    error: ErrorSummary | None = None,
) -> WriteOutcomeUncertain:
    strategy = strategy_for_original_write(original_write)
    identity = build_reconciliation_identity(
        run_id=original_write.run_id,
        effect_id=original_write.effect_id,
        attempt=original_write.attempt,
        strategy=strategy,
    )
    return WriteOutcomeUncertain(
        occurred_at=occurred_at,
        token=token,
        error=error or uncertain_error_summary(),
        reconciliation_identity=identity,
        original_write=original_write,
    )


def make_retryable_failure(
    *,
    token: EffectCompletionToken,
    occurred_at: datetime,
    failed_attempt: int,
    transient_kind: TransientErrorKind,
    safe_summary: str,
    headers: AllowlistedHeaders | None = None,
    jitter: JitterSource | None = None,
    max_server_directed_wait_seconds: int = 3600,
) -> EffectRetryableFailure:
    headers = headers or AllowlistedHeaders()
    gateway_kind = _TRANSIENT_KIND_TO_GATEWAY.get(
        transient_kind, GatewayTransientKind.TEMPORARY_CLI_FAILURE
    )
    transient = GatewayTransient(
        kind=gateway_kind,
        safe_summary=safe_summary,
        transient_kind=transient_kind,
        headers=headers,
    )
    # Attempt 6 still emits a schema-valid retryable result; reducer owns exhaustion.
    backoff_attempt = min(max(failed_attempt, 1), 5)
    decision = compute_retry_backoff(
        failed_attempt=backoff_attempt,
        observation_time=occurred_at,
        headers=headers,
        jitter=jitter or FixedJitter(0.0),
        max_server_directed_wait_seconds=max_server_directed_wait_seconds,
    )
    next_at = decision.next_attempt_at
    if failed_attempt >= 6:
        next_at = occurred_at + timedelta(seconds=300)
    return EffectRetryableFailure(
        occurred_at=occurred_at,
        token=token,
        error=map_transient_to_error_summary(transient),
        failed_attempt=failed_attempt,
        next_attempt_at=next_at,
    )


def make_blocked(
    *,
    token: EffectCompletionToken,
    occurred_at: datetime,
    reason: PauseReasonKind,
    safe_summary: str,
    safe_action: SafeAction | None = None,
) -> EffectBlocked:
    action = safe_action or SafeAction(
        kind=SafeActionKind.INSPECT_ARTIFACTS,
        condition="inspect write preflight failure",
    )
    return EffectBlocked(
        occurred_at=occurred_at,
        token=token,
        reason=reason,
        safe_action=action,
        safe_summary=safe_summary,
    )


def compute_proven_not_applied_next_attempt(
    *,
    occurred_at: datetime,
    failed_attempt: int,
    headers: AllowlistedHeaders | None = None,
    jitter: JitterSource | None = None,
    max_server_directed_wait_seconds: int = 3600,
) -> datetime:
    """Absolute retry eligibility for PROVEN_NOT_APPLIED, strictly after ``occurred_at``.

    Uses the approved local/server-directed backoff policy keyed by the original write
    attempt. Attempt 6 still yields a schema-valid future time; the reducer owns
    exhaustion and will not schedule a seventh write.
    """

    headers = headers or AllowlistedHeaders()
    backoff_attempt = min(max(failed_attempt, 1), 5)
    decision = compute_retry_backoff(
        failed_attempt=backoff_attempt,
        observation_time=occurred_at,
        headers=headers,
        jitter=jitter or FixedJitter(0.0),
        max_server_directed_wait_seconds=max_server_directed_wait_seconds,
    )
    next_at = decision.next_attempt_at
    if failed_attempt >= 6:
        next_at = occurred_at + timedelta(seconds=300)
    if next_at <= occurred_at:
        raise ValueError("proven_not_applied next_attempt_at must be strictly after occurred_at")
    return next_at


def make_reconciliation_success(
    *,
    token: EffectCompletionToken,
    occurred_at: datetime,
    original_effect_id: NonEmptyId,
    proof: ReconciliationProof,
    original_attempt: int | None = None,
    headers: AllowlistedHeaders | None = None,
    jitter: JitterSource | None = None,
    max_server_directed_wait_seconds: int = 3600,
) -> EffectSucceeded:
    next_attempt_at = proof.next_attempt_at
    if proof.proof is WriteProofKind.PROVEN_NOT_APPLIED:
        if original_attempt is None:
            raise ValueError("proven_not_applied requires original_attempt for backoff")
        next_attempt_at = compute_proven_not_applied_next_attempt(
            occurred_at=occurred_at,
            failed_attempt=original_attempt,
            headers=headers,
            jitter=jitter,
            max_server_directed_wait_seconds=max_server_directed_wait_seconds,
        )
    return EffectSucceeded(
        occurred_at=occurred_at,
        token=token,
        outcome=ReconciliationResolvedOutcome(
            resolution=resolution_from_proof(proof.proof),
            original_effect_id=original_effect_id,
            confirmed_outcome=proof.confirmed_outcome,
            next_attempt_at=next_attempt_at,
        ),
    )


def applied_proof(
    *,
    strategy: ReconciliationStrategyKind,
    confirmed_outcome: ConfirmedWriteOutcome,
    safe_summary: str = "write proven applied",
) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.APPLIED,
        strategy=strategy,
        confirmed_outcome=confirmed_outcome,
        next_attempt_at=None,
        safe_summary=safe_summary,
    )


def proven_not_applied_proof(
    *,
    strategy: ReconciliationStrategyKind,
    occurred_at: datetime,
    failed_attempt: int,
    safe_summary: str = "write proven not applied",
    headers: AllowlistedHeaders | None = None,
    jitter: JitterSource | None = None,
    max_server_directed_wait_seconds: int = 3600,
) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.PROVEN_NOT_APPLIED,
        strategy=strategy,
        confirmed_outcome=None,
        next_attempt_at=compute_proven_not_applied_next_attempt(
            occurred_at=occurred_at,
            failed_attempt=failed_attempt,
            headers=headers,
            jitter=jitter,
            max_server_directed_wait_seconds=max_server_directed_wait_seconds,
        ),
        safe_summary=safe_summary,
    )


def unresolved_proof(
    *,
    strategy: ReconciliationStrategyKind,
    safe_summary: str = "write reconciliation unresolved",
) -> ReconciliationProof:
    return ReconciliationProof(
        proof=WriteProofKind.UNRESOLVED,
        strategy=strategy,
        confirmed_outcome=None,
        next_attempt_at=None,
        safe_summary=safe_summary,
    )
