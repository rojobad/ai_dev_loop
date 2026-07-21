"""ObserveBotReviewEffect-only executor for PR review v2."""

from __future__ import annotations

from datetime import datetime, timedelta

from ai_dev_loop.pr_review_v2.application.contracts import Clock, EffectExecutor
from ai_dev_loop.pr_review_v2.application.github_read import (
    FixedJitter,
    GatewayTransient,
    GitHubReadPolicy,
    JitterSource,
    ObservationEvidenceKind,
    compute_retry_backoff,
    map_transient_to_error_summary,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectCompletionToken,
    FrozenThreadSet,
    PauseReasonKind,
    SafeAction,
    SafeActionKind,
    VerifiedNoFindingsEvidence,
)
from ai_dev_loop.pr_review_v2.domain.effects import ObserveBotReviewEffect, PrReviewEffect
from ai_dev_loop.pr_review_v2.domain.events import (
    BotStillWaitingOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    PrReviewEvent,
    VerifiedNoFindingsOutcome,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import (
    GitHubReadGateway,
    next_poll_not_before,
)


class UnsupportedEffectError(Exception):
    """Raised before any transport call when the effect kind is unsupported."""

    def __init__(self, kind: str) -> None:
        super().__init__(f"unsupported effect kind for GitHub read executor: {kind}")
        self.kind = kind


class GitHubReadExecutor:
    """Narrow EffectExecutor that accepts only ObserveBotReviewEffect."""

    def __init__(
        self,
        *,
        gateway: GitHubReadGateway,
        policy: GitHubReadPolicy,
        clock: Clock,
        jitter: JitterSource | None = None,
    ) -> None:
        self._gateway = gateway
        self._policy = policy
        self._clock = clock
        self._jitter = jitter or FixedJitter(0.0)

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
    ) -> PrReviewEvent:
        del now  # Completion/observation time comes from the injected clock after the read.
        if not isinstance(effect, ObserveBotReviewEffect):
            raise UnsupportedEffectError(effect.kind)
        if (
            effect.effect_id != token.effect_id
            or effect.bound_head_sha != token.bound_head_sha
            or effect.cycle_number != token.cycle_number
        ):
            raise UnsupportedEffectError("token_mismatch")
        if effect.trigger_marker is None:
            occurred_at = self._clock.now()
            return EffectBlocked(
                occurred_at=occurred_at,
                token=token,
                reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
                safe_action=SafeAction(
                    kind=SafeActionKind.INSPECT_ARTIFACTS,
                    condition="restore one complete consistent observation before resume",
                ),
                safe_summary="observe effect is missing trigger_marker",
            )

        try:
            snapshot, artifact_ref = self._gateway.observe_with_artifact(
                effect,
                observation_time=self._clock.now(),
            )
        except GhTransportError as exc:
            occurred_at = self._clock.now()
            if exc.block is not None:
                return EffectBlocked(
                    occurred_at=occurred_at,
                    token=token,
                    reason=exc.block.pause_reason,
                    safe_action=exc.block.safe_action,
                    safe_summary=exc.block.safe_summary,
                )
            assert exc.transient is not None
            return self._retryable_from_transient(
                effect=effect,
                token=token,
                occurred_at=occurred_at,
                transient=exc.transient,
            )

        occurred_at = self._clock.now()
        if snapshot.evidence_kind is ObservationEvidenceKind.BOT_STILL_WAITING:
            return EffectSucceeded(
                occurred_at=occurred_at,
                token=token,
                outcome=BotStillWaitingOutcome(
                    next_not_before=next_poll_not_before(
                        occurred_at,
                        poll_interval_seconds=int(self._policy.poll_interval_seconds),
                    ),
                    poll_sequence=effect.poll_sequence + 1,
                ),
            )
        if snapshot.evidence_kind is ObservationEvidenceKind.VERIFIED_NO_FINDINGS:
            return EffectSucceeded(
                occurred_at=occurred_at,
                token=token,
                outcome=VerifiedNoFindingsOutcome(
                    evidence=VerifiedNoFindingsEvidence(
                        head_sha=effect.bound_head_sha,
                        observation_ref=artifact_ref,
                        verified_at=occurred_at,
                    )
                ),
            )
        if snapshot.evidence_kind is ObservationEvidenceKind.ELIGIBLE_THREADS:
            thread_ids = tuple(thread.thread_id for thread in snapshot.eligible_threads)
            return EffectSucceeded(
                occurred_at=occurred_at,
                token=token,
                outcome=EligibleThreadsObservedOutcome(
                    frozen=FrozenThreadSet(
                        thread_ids=thread_ids,
                        snapshot_ref=artifact_ref,
                        head_sha=effect.bound_head_sha,
                        cycle_number=effect.cycle_number,
                        trigger_marker=effect.trigger_marker,
                    )
                ),
            )
        raise UnsupportedEffectError("unknown_evidence_kind")

    def _retryable_from_transient(
        self,
        *,
        effect: ObserveBotReviewEffect,
        token: EffectCompletionToken,
        occurred_at: datetime,
        transient: GatewayTransient,
    ) -> EffectRetryableFailure:
        error = map_transient_to_error_summary(transient)
        if effect.attempt >= effect.max_attempts:
            next_at = occurred_at + timedelta(seconds=1)
        else:
            decision = compute_retry_backoff(
                failed_attempt=effect.attempt,
                observation_time=occurred_at,
                headers=transient.headers,
                jitter=self._jitter,
                max_server_directed_wait_seconds=int(self._policy.max_server_directed_wait_seconds),
            )
            next_at = decision.next_attempt_at
        return EffectRetryableFailure(
            occurred_at=occurred_at,
            token=token,
            error=error,
            failed_attempt=effect.attempt,
            next_attempt_at=next_at,
        )


_: type[EffectExecutor] = GitHubReadExecutor
