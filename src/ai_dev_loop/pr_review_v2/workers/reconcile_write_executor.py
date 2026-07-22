"""RECONCILING effect executor: seven strategies to typed reconciliation events.

Each reconcile effect embeds the exact original write and its strategy. This executor
validates that pair, invokes the correct read-only gateway proof, and maps the typed
``ReconciliationProof`` to ``EffectSucceeded`` (APPLIED / PROVEN_NOT_APPLIED /
UNRESOLVED). Transient read failures become ``EffectRetryableFailure``; genuinely
ambiguous or malformed evidence resolves as UNRESOLVED; deterministic permission/auth
blocks become ``EffectBlocked``. It never performs a write and never nests a
``WriteOutcomeUncertain``.
"""

from __future__ import annotations

from datetime import datetime

from ai_dev_loop.errors import LockError
from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.github_read import (
    FixedJitter,
    GatewayBlock,
    GatewayBlockKind,
    GatewayTransient,
    JitterSource,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ClaimAuthorityGuard,
    GitHubWritePolicy,
    ReconciliationProof,
    WriteProofKind,
    repository_lock_contention_transient,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import (
    make_reconciliation_success,
    make_retryable_failure,
    unresolved_proof,
    validate_reconcile_pair,
)
from ai_dev_loop.pr_review_v2.domain.common import EffectCompletionToken, TransientErrorKind
from ai_dev_loop.pr_review_v2.domain.effects import (
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    PostThreadReplyEffect,
    PrReviewEffect,
    PushCommitEffect,
    ReconcileWriteEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    UpdatePrTextEffect,
    is_reconciling_effect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway

ReconcileResult = EffectSucceeded | EffectRetryableFailure | EffectBlocked

# Blocks that mean "evidence was incomplete/ambiguous" rather than a hard permission
# failure resolve as UNRESOLVED so the reducer schedules one bounded original retry.
_UNRESOLVED_BLOCK_KINDS = frozenset(
    {GatewayBlockKind.MALFORMED_EVIDENCE, GatewayBlockKind.CONTRADICTORY_EVIDENCE}
)


class UnsupportedReconcileEffectError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(f"unsupported reconcile effect kind: {kind}")
        self.kind = kind


class ReconcileWriteExecutor:
    requires_authority = True

    def __init__(
        self,
        *,
        git_gateway: GitPublicationGateway,
        github_gateway: GitHubWriteGateway,
        github_policy: GitHubWritePolicy,
        jitter: JitterSource | None = None,
    ) -> None:
        self._git = git_gateway
        self._github = github_gateway
        self._policy = github_policy
        self._jitter = jitter or FixedJitter(0.0)

    def execute(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        *,
        now: datetime,
        authority: ClaimAuthorityGuard,
        claim: EffectClaim,
    ) -> ReconcileResult:
        del authority, claim
        if not is_reconciling_effect(effect) or not isinstance(effect, ReconcileWriteEffect):
            raise UnsupportedReconcileEffectError(effect.kind)
        self._validate_token(effect, token)
        original = effect.original_write
        validate_reconcile_pair(original_write=original, strategy=effect.strategy)
        try:
            proof = self._dispatch(original, run_id=effect.run_id, now=now)
        except LockError:
            transient = repository_lock_contention_transient()
            return make_retryable_failure(
                token=token,
                occurred_at=now,
                failed_attempt=effect.attempt,
                transient_kind=TransientErrorKind.TEMPORARY_CLI_FAILURE,
                safe_summary=transient.safe_summary,
                jitter=self._jitter,
                max_server_directed_wait_seconds=int(self._policy.max_server_directed_wait_seconds),
            )
        except (GhTransportError, GitTransportError) as exc:
            return self._from_transport_error(effect, token, now, exc)
        return make_reconciliation_success(
            token=token,
            occurred_at=now,
            original_effect_id=original.effect_id,
            proof=proof,
            original_attempt=original.attempt,
            jitter=self._jitter,
            max_server_directed_wait_seconds=int(self._policy.max_server_directed_wait_seconds),
        )

    def _dispatch(
        self, original: PrReviewEffect, *, run_id: str, now: datetime
    ) -> ReconciliationProof:
        if isinstance(original, CommitPatchEffect):
            return self._git.reconcile_commit(original, run_id=run_id, now=now)
        if isinstance(original, PushCommitEffect):
            return self._git.reconcile_push(original, run_id=run_id, now=now)
        if isinstance(original, CreateOrUpdatePrEffect):
            return self._github.reconcile_create_or_update_pr(original, run_id=run_id, now=now)
        if isinstance(original, RequestBotReviewEffect):
            return self._github.reconcile_request_review(original, run_id=run_id, now=now)
        if isinstance(original, PostThreadReplyEffect):
            return self._github.reconcile_post_thread_reply(original, run_id=run_id, now=now)
        if isinstance(original, UpdatePrTextEffect):
            return self._github.reconcile_update_pr_text(original, run_id=run_id, now=now)
        if isinstance(original, ResolveThreadEffect):
            return self._github.reconcile_resolve_thread(original, run_id=run_id, now=now)
        raise UnsupportedReconcileEffectError(original.kind)

    def _from_transport_error(
        self,
        effect: ReconcileWriteEffect,
        token: EffectCompletionToken,
        now: datetime,
        exc: GhTransportError | GitTransportError,
    ) -> ReconcileResult:
        if exc.block is not None:
            block: GatewayBlock = exc.block
            if block.kind in _UNRESOLVED_BLOCK_KINDS:
                return make_reconciliation_success(
                    token=token,
                    occurred_at=now,
                    original_effect_id=effect.original_write.effect_id,
                    proof=unresolved_proof(
                        strategy=effect.strategy, safe_summary=block.safe_summary
                    ),
                )
            return EffectBlocked(
                occurred_at=now,
                token=token,
                reason=block.pause_reason,
                safe_action=block.safe_action,
                safe_summary=block.safe_summary,
            )
        transient: GatewayTransient = exc.transient  # type: ignore[assignment]
        return make_retryable_failure(
            token=token,
            occurred_at=now,
            failed_attempt=effect.attempt,
            transient_kind=transient.transient_kind,
            safe_summary=transient.safe_summary,
            headers=transient.headers,
            jitter=self._jitter,
            max_server_directed_wait_seconds=int(self._policy.max_server_directed_wait_seconds),
        )

    @staticmethod
    def _validate_token(effect: ReconcileWriteEffect, token: EffectCompletionToken) -> None:
        if (
            effect.effect_id != token.effect_id
            or effect.bound_head_sha != token.bound_head_sha
            or effect.cycle_number != token.cycle_number
        ):
            raise UnsupportedReconcileEffectError("token_mismatch")


# Ensure UNRESOLVED remains a valid success shape (no confirmed outcome).
assert WriteProofKind.UNRESOLVED  # noqa: S101 - import-time sanity for enum presence

__all__ = ["ReconcileWriteExecutor", "UnsupportedReconcileEffectError"]
