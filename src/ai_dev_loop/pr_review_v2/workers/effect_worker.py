"""Bounded one-step effect worker for PR review v2."""

from __future__ import annotations

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EffectExecutor,
    EventDisposition,
    WorkerStepResult,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.infrastructure.runtime import completion_submission_id


class EffectWorker:
    """Acquire/recover/claim, execute outside the transaction, then complete."""

    def __init__(
        self,
        engine: PrReviewEngine,
        executor: EffectExecutor,
        *,
        owner_id: str,
    ) -> None:
        self._engine = engine
        self._executor = executor
        self._owner_id = owner_id

    def run_once(
        self,
        run_id: str,
        *,
        result_key: str = "primary",
    ) -> WorkerStepResult:
        lease = self._engine.acquire_lease(run_id, self._owner_id)
        self._engine.recover_expired_claims(run_id, self._owner_id, lease.generation)
        claim_result = self._engine.claim_next_effect(run_id, self._owner_id, lease.generation)
        if claim_result.claim is None:
            self._engine.release_lease(run_id, self._owner_id, lease.generation)
            return WorkerStepResult(
                run_id=run_id,
                claimed=False,
                completed=False,
                safe_detail=claim_result.reason,
            )

        claim = claim_result.claim
        # Claim is already committed before executor runs.
        event = self._executor.execute(
            claim.effect,
            claim.completion_token,
            now=claim.claimed_at,
        )
        submission_id = completion_submission_id(
            run_id=claim.run_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            lease_generation=lease.generation,
            result_key=result_key,
        )
        receipt = self._engine.complete_claim(
            EffectCompletionRequest(
                submission_id=submission_id,
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id=self._owner_id,
                lease_generation=lease.generation,
                event=event,
            )
        )
        self._engine.heartbeat_lease(run_id, self._owner_id, lease.generation)
        return WorkerStepResult(
            run_id=run_id,
            claimed=True,
            completed=receipt.disposition
            in {EventDisposition.ACCEPTED, EventDisposition.DUPLICATE},
            disposition=receipt.disposition,
            dispatch_id=claim.dispatch_id,
            effect_kind=claim.effect.kind,
            safe_detail=receipt.safe_detail,
        )
