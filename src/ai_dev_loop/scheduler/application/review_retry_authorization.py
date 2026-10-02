"""Transactional guards for same-run Codex review retry authorization."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ai_dev_loop.scheduler.application.contracts import (
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    ReviewCheckpointVerificationError,
    verify_retry_state_repository,
)
from ai_dev_loop.scheduler.application.tick_fencing import tick_lease_is_active
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
)
from ai_dev_loop.scheduler.domain.common import encode_utc_instant
from ai_dev_loop.scheduler.domain.sequence import ActiveSequenceState
from ai_dev_loop.scheduler.domain.state import WaitingCodexReviewRetryState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

ReviewRetryAuthorizationSource = Literal["manual", "automatic"]


@dataclass(frozen=True)
class ReviewRetryAuthorizationRequest:
    run_id: str
    source: ReviewRetryAuthorizationSource
    expected_failure_generation: int
    tick_owner_id: str | None = None
    tick_lease_generation: int | None = None


def _sequence_blocks_retry(
    store: SqliteSchedulerStore,
    conn: sqlite3.Connection,
    state: WaitingCodexReviewRetryState,
) -> str | None:
    binding = state.context.sequence
    if binding is None:
        return None
    try:
        sequence_state = store.load_sequence_state_only(conn, binding.sequence_id)
    except (SchedulerEngineError, ValueError, TypeError, KeyError):
        return "sequence state unavailable for retry authorization"
    if isinstance(sequence_state, ActiveSequenceState):
        if sequence_state.current_run_id != state.run_id:
            return "run is not the active sequence execution leaf"
        return None
    kind = getattr(sequence_state, "kind", type(sequence_state).__name__)
    return f"sequence is not active for retry authorization ({kind})"


def validate_review_retry_authorization(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    conn: sqlite3.Connection,
    state: WaitingCodexReviewRetryState,
    *,
    request: ReviewRetryAuthorizationRequest,
    now: datetime,
    skip_repository_verify: bool = False,
) -> None:
    if request.expected_failure_generation != state.codex.review_retry_generation:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "review retry failure generation changed",
        )
    if not state.codex.reviewer_session_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "review retry requires bound reviewer identity",
        )
    if not state.codex.last_failed_attempt_id:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.VALIDATION,
            "review retry requires authenticated failed attempt evidence",
        )
    if not skip_repository_verify:
        try:
            verify_retry_state_repository(state, artifacts=artifacts)
        except ReviewCheckpointVerificationError as exc:
            raise SchedulerEngineError(SchedulerEngineErrorKind.VALIDATION, str(exc)) from exc
    if store.get_nonterminal_attempt_for_run(conn, state.run_id) is not None:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "cannot authorize review retry while a Codex attempt is active",
        )
    if store.has_abort_requested_for_run(conn, state.run_id):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "cannot authorize review retry after abort was requested",
        )
    if store.has_unresolved_abort_hold(conn, state.run_id):
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "cannot authorize review retry while abort reconciliation is pending",
        )
    sequence_reason = _sequence_blocks_retry(store, conn, state)
    if sequence_reason is not None:
        raise SchedulerEngineError(SchedulerEngineErrorKind.CONFLICT, sequence_reason)

    if request.source == "automatic":
        if request.tick_owner_id is None or request.tick_lease_generation is None:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "automatic review retry requires tick lease context",
            )
        if not tick_lease_is_active(
            store,
            conn,
            owner_id=request.tick_owner_id,
            generation=request.tick_lease_generation,
            now=now,
        ):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "tick lease is not active for automatic review retry",
            )
        if state.codex.review_retry_failure_kind != FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "automatic review retry is only supported for workspace routing timeouts",
            )
        if not state.codex.routing_auto_retry_eligible:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "automatic review retry is not eligible for this failure",
            )
        due_at = state.codex.routing_auto_retry_due_at
        if not due_at or due_at > encode_utc_instant(now):
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.CONFLICT,
                "automatic review retry is not yet due",
            )
        if state.codex.routing_auto_retry_authorizations_used >= ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS:
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.VALIDATION,
                "automatic routing retry allowance is exhausted",
            )
    if state.codex.review_retry_scheduled_generation == state.codex.review_retry_generation:
        raise SchedulerEngineError(
            SchedulerEngineErrorKind.CONFLICT,
            "review retry is already authorized for this failure generation",
        )
