"""MUTATING effect executor: seven writes to typed events via authority + gateways.

The executor validates the completion token against the claimed effect, then calls
the worker-supplied authority guard immediately before each gateway mutation. Guard
rejection raises ``AuthorityLostError`` (zero writes) which the worker fences through
``complete_claim``. Deterministic preflight blocks become ``EffectBlocked``, transient
failures become ``EffectRetryableFailure``, and ambiguous post-start outcomes become
``WriteOutcomeUncertain`` for the exact original write.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ai_dev_loop.errors import LockError
from ai_dev_loop.pr_review_v2.application.contracts import EffectClaim
from ai_dev_loop.pr_review_v2.application.github_read import (
    FixedJitter,
    GatewayBlock,
    GatewayTransient,
    JitterSource,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    AuthorityLostError,
    ClaimAuthorityGuard,
    GitHubWritePolicy,
    WriteAuthorityStatus,
    WriteGatewaySuccess,
    claim_authority_snapshot_from_claim,
    repository_lock_contention_transient,
)
from ai_dev_loop.pr_review_v2.application.write_reconciliation import (
    make_retryable_failure,
    make_write_uncertain,
)
from ai_dev_loop.pr_review_v2.domain.common import EffectCompletionToken, TransientErrorKind
from ai_dev_loop.pr_review_v2.domain.effects import (
    CommitPatchEffect,
    CreateOrUpdatePrEffect,
    MutatingEffect,
    PostThreadReplyEffect,
    PrReviewEffect,
    PushCommitEffect,
    RequestBotReviewEffect,
    ResolveThreadEffect,
    UpdatePrTextEffect,
    is_mutating_effect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.infrastructure.gh_transport import GhTransportError
from ai_dev_loop.pr_review_v2.infrastructure.git_publication_gateway import GitPublicationGateway
from ai_dev_loop.pr_review_v2.infrastructure.git_write_transport import GitTransportError
from ai_dev_loop.pr_review_v2.infrastructure.github_write_gateway import GitHubWriteGateway

WriteResult = EffectSucceeded | EffectRetryableFailure | EffectBlocked | WriteOutcomeUncertain


class UnsupportedMutatingEffectError(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(f"unsupported mutating effect kind: {kind}")
        self.kind = kind


class WriteExecutor:
    """Authority-aware executor for the seven MUTATING effects."""

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
    ) -> WriteResult:
        if not is_mutating_effect(effect):
            raise UnsupportedMutatingEffectError(effect.kind)
        self._validate_token(effect, token)
        snapshot = claim_authority_snapshot_from_claim(claim)

        def authorize() -> None:
            result = authority.check_authority(snapshot)
            if result.status is not WriteAuthorityStatus.AUTHORIZED:
                raise AuthorityLostError(result.safe_summary or "authority rejected")

        run_id = claim.run_id
        try:
            success = self._dispatch(effect, run_id=run_id, now=now, authorize=authorize)
        except AuthorityLostError:
            # Propagate so EffectWorker can set lease_authority_lost=True on the
            # final EffectCompletionRequest. Do not swallow into EffectBlocked here.
            raise
        except AmbiguousWriteError:
            return make_write_uncertain(
                token=token,
                original_write=_as_mutating(effect),
                occurred_at=now,
            )
        except LockError:
            # Pre-write lock contention: no authority check and no mutation occurred.
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
        return EffectSucceeded(occurred_at=now, token=token, outcome=success.outcome)

    def _dispatch(
        self,
        effect: PrReviewEffect,
        *,
        run_id: str,
        now: datetime,
        authorize: Callable[[], None],
    ) -> WriteGatewaySuccess:
        if isinstance(effect, CommitPatchEffect):
            return self._git.commit(effect, run_id=run_id, now=now, authorize=authorize)
        if isinstance(effect, PushCommitEffect):
            return self._git.push(effect, run_id=run_id, now=now, authorize=authorize)
        if isinstance(effect, CreateOrUpdatePrEffect):
            return self._github.create_or_update_pr(
                effect, run_id=run_id, now=now, authorize=authorize
            )
        if isinstance(effect, RequestBotReviewEffect):
            return self._github.request_review(effect, run_id=run_id, now=now, authorize=authorize)
        if isinstance(effect, PostThreadReplyEffect):
            return self._github.post_thread_reply(
                effect, run_id=run_id, now=now, authorize=authorize
            )
        if isinstance(effect, UpdatePrTextEffect):
            return self._github.update_pr_text(effect, run_id=run_id, now=now, authorize=authorize)
        if isinstance(effect, ResolveThreadEffect):
            return self._github.resolve_thread(effect, run_id=run_id, now=now, authorize=authorize)
        raise UnsupportedMutatingEffectError(effect.kind)

    def _from_transport_error(
        self,
        effect: PrReviewEffect,
        token: EffectCompletionToken,
        now: datetime,
        exc: GhTransportError | GitTransportError,
    ) -> EffectBlocked | EffectRetryableFailure:
        if exc.block is not None:
            block: GatewayBlock = exc.block
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
    def _validate_token(effect: PrReviewEffect, token: EffectCompletionToken) -> None:
        if (
            effect.effect_id != token.effect_id
            or effect.bound_head_sha != token.bound_head_sha
            or effect.cycle_number != token.cycle_number
        ):
            raise UnsupportedMutatingEffectError("token_mismatch")


def _as_mutating(effect: PrReviewEffect) -> MutatingEffect:
    return effect  # type: ignore[return-value]


__all__ = ["UnsupportedMutatingEffectError", "WriteExecutor"]
