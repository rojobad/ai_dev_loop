"""Classification-based effect executor router for PR review v2.

The router is the single authority-aware executor handed to ``EffectWorker``. It
dispatches READ_ONLY effects to the Phase 16.5 ``GitHubReadExecutor`` (no authority),
MUTATING effects to the authority-aware ``WriteExecutor``, RECONCILING effects to
``ReconcileWriteExecutor``, and LOCAL effects to the Phase 16.7 ``LocalEffectExecutor``
when injected. Unsupported kinds are rejected before any process, artifact, or
network activity.
"""

from __future__ import annotations

from datetime import datetime

from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.write_contracts import ClaimAuthorityGuard
from ai_dev_loop.pr_review_v2.domain.common import EffectClassification, EffectCompletionToken
from ai_dev_loop.pr_review_v2.domain.effects import PrReviewEffect, classify_effect
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    PrReviewEvent,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor
from ai_dev_loop.pr_review_v2.workers.local_executor import LocalEffectExecutor
from ai_dev_loop.pr_review_v2.workers.reconcile_write_executor import ReconcileWriteExecutor
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

RouterResult = EffectSucceeded | EffectRetryableFailure | EffectBlocked | WriteOutcomeUncertain


class UnsupportedRoutedEffectError(Exception):
    """Raised for LOCAL/unknown effects with zero external side effects."""

    def __init__(self, kind: str, classification: str) -> None:
        super().__init__(f"router refuses effect kind {kind} ({classification})")
        self.kind = kind
        self.classification = classification


class EffectExecutorRouter:
    requires_authority = True

    def __init__(
        self,
        *,
        read_executor: GitHubReadExecutor,
        write_executor: WriteExecutor,
        reconcile_executor: ReconcileWriteExecutor,
        local_executor: LocalEffectExecutor | None = None,
    ) -> None:
        self._read = read_executor
        self._write = write_executor
        self._reconcile = reconcile_executor
        self._local = local_executor

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority: ClaimAuthorityGuard,
        claim: EffectClaim,
    ) -> RouterResult | PrReviewEvent:
        classification = classify_effect(effect)
        if classification is EffectClassification.READ_ONLY:
            return self._read.execute(effect, token, now=now)
        if classification is EffectClassification.MUTATING:
            return self._write.execute(effect, token, now=now, authority=authority, claim=claim)
        if classification is EffectClassification.RECONCILING:
            return self._reconcile.execute(effect, token, now=now, authority=authority, claim=claim)
        if classification is EffectClassification.LOCAL:
            if self._local is None:
                raise UnsupportedRoutedEffectError(effect.kind, classification.value)
            return self._local.execute(effect, token, now=now, authority=authority, claim=claim)
        raise UnsupportedRoutedEffectError(effect.kind, classification.value)


__all__ = ["EffectExecutorRouter", "UnsupportedRoutedEffectError"]
