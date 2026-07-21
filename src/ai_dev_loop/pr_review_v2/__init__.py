"""Public PR review v2 package.

Phase 16.3 exposes the pure domain under ``ai_dev_loop.pr_review_v2.domain``.
Phase 16.4 adds a small durable application API under ``application``.
Phase 16.5 adds a read-only GitHub observation boundary under infrastructure/workers.
"""

from __future__ import annotations

from ai_dev_loop.pr_review_v2 import domain as domain
from ai_dev_loop.pr_review_v2.application.contracts import (
    ApplicationReceipt,
    EventDisposition,
    EventSubmission,
    PrReviewStatus,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.github_read import GitHubReadPolicy
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor

__all__ = [
    "ApplicationReceipt",
    "EffectWorker",
    "EventDisposition",
    "EventSubmission",
    "GitHubReadExecutor",
    "GitHubReadPolicy",
    "PrReviewEngine",
    "PrReviewStatus",
    "domain",
]
