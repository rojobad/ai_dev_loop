"""Durable application engine for PR review v2."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ai_dev_loop.pr_review_v2.application.contracts import (
    ApplicationReceipt,
    Clock,
    DispatchStatus,
    EffectClaim,
    EffectClaimResult,
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
    FaultHook,
    IdFactory,
    LeaseAcquireResult,
    LeaseHeartbeatResult,
    LeaseReleaseResult,
    LeaseStatus,
    PrReviewEngineError,
    PrReviewEngineErrorKind,
    PrReviewStatus,
    TimerFireReceipt,
    TimerStatus,
)
from ai_dev_loop.pr_review_v2.application.control_contracts import PreparedOwnershipKeys
from ai_dev_loop.pr_review_v2.application.status import build_status
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    ClaimAuthorityResult,
    ClaimAuthoritySnapshot,
    WriteAuthorityStatus,
)
from ai_dev_loop.pr_review_v2.domain.common import (
    EffectClassification,
    EffectCompletionToken,
    ErrorSummary,
    PauseReasonKind,
    SafeAction,
    SafeActionKind,
    TransientErrorKind,
)
from ai_dev_loop.pr_review_v2.domain.effects import (
    MutatingEffect,
    classify_effect,
    is_mutating_effect,
)
from ai_dev_loop.pr_review_v2.domain.events import (
    AbortRequested,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    FatalFailureDetected,
    PrReviewEvent,
    RecoverMixedAdjudicationRequested,
    RetryDue,
    WriteOutcomeUncertain,
)
from ai_dev_loop.pr_review_v2.domain.reducer import (
    TransitionApplied,
    TransitionRejected,
    reduce_pr_review,
)
from ai_dev_loop.pr_review_v2.domain.state import PreparedState, PrReviewState, active_effect
from ai_dev_loop.pr_review_v2.infrastructure.paths import default_engine_db_path
from ai_dev_loop.pr_review_v2.infrastructure.runtime import (
    SequenceIdFactory,
    SystemClock,
    encode_utc_instant,
    parse_utc_instant,
    payload_sha256,
    recovery_submission_id,
    safe_diagnostic,
    timer_event_id,
    validate_owner_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

FENCED_APPLY_EVENT_KINDS = frozenset(
    {
        "effect_succeeded",
        "effect_retryable_failure",
        "effect_blocked",
        "write_outcome_uncertain",
        "retry_due",
        "abort_requested",
        "fatal_failure_detected",
    }
)

EFFECT_RESULT_KINDS = frozenset(
    {
        "effect_succeeded",
        "effect_retryable_failure",
        "effect_blocked",
        "write_outcome_uncertain",
    }
)

TERMINAL_KINDS = frozenset({"completed", "failed", "aborted"})


class _NoFault:
    def maybe_raise(self, checkpoint: str) -> None:
        return None


class PrReviewEngine:
    """Typed durable engine API for Phase 16.4."""

    def __init__(
        self,
        store: SqlitePrReviewStore,
        *,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
        fault_hook: FaultHook | None = None,
        lease_ttl: timedelta | None = None,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._ids = ids or SequenceIdFactory(prefix="engine")
        self._fault = fault_hook or _NoFault()
        ttl = timedelta(seconds=30) if lease_ttl is None else lease_ttl
        if ttl.total_seconds() <= 0:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "lease_ttl must be positive",
            )
        self._lease_ttl = ttl

    @classmethod
    def open(
        cls,
        db_path: Path | None = None,
        *,
        clock: Clock | None = None,
        ids: IdFactory | None = None,
        fault_hook: FaultHook | None = None,
        lease_ttl: timedelta | None = None,
        busy_timeout_ms: int = 5000,
    ) -> PrReviewEngine:
        path = db_path if db_path is not None else default_engine_db_path()
        store = SqlitePrReviewStore(path, busy_timeout_ms=busy_timeout_ms)
        return cls(
            store,
            clock=clock,
            ids=ids,
            fault_hook=fault_hook,
            lease_ttl=lease_ttl,
        )

    @property
    def store(self) -> SqlitePrReviewStore:
        return self._store

    @property
    def lease_ttl(self) -> timedelta:
        """Read-only lease duration used for renew-interval validation."""

        return self._lease_ttl

    @property
    def clock(self) -> Clock:
        """Injected completion/observation clock (never claim wall time)."""

        return self._clock

    def create_run(self, run_id: str, state: PreparedState) -> PrReviewStatus:
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            self._store.insert_prepared_run(conn, run_id=run_id, state=state, now=now)
        return self.get_status(run_id)

    def create_or_reuse_prepared_run(
        self,
        *,
        run_id: str,
        state: PreparedState,
        ownership: PreparedOwnershipKeys,
    ) -> tuple[PrReviewStatus, bool]:
        """Atomically create a prepared run or reuse an exact prepared identity.

        Conflicting active ownership (same source run, same PR, or same head
        publication branch) is rejected. Exact prepared identity reuse returns
        the existing run without mutation.
        """

        if state.run_id != run_id:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "PreparedState.run_id must equal requested run_id",
            )
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            identity = _prepared_identity_fingerprint(state)
            for row in self._store.list_nonterminal_run_rows(conn):
                existing_state = self._store.load_state(row["state_payload"])
                existing_id = str(row["run_id"])
                if existing_id == run_id:
                    if existing_state.kind != "prepared":
                        raise PrReviewEngineError(
                            PrReviewEngineErrorKind.CONFLICT,
                            "run_id already exists in a non-prepared active state",
                        )
                    if _prepared_identity_fingerprint(existing_state) != identity:
                        raise PrReviewEngineError(
                            PrReviewEngineErrorKind.CONFLICT,
                            "run_id already exists with a different prepared identity",
                        )
                    return self.get_status(run_id), True
                if (
                    existing_state.kind == "prepared"
                    and _prepared_identity_fingerprint(existing_state) == identity
                ):
                    return self.get_status(existing_id), True
                if _ownership_conflicts(existing_state, ownership):
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.CONFLICT,
                        "active ownership conflict for prepared run",
                    )
            self._store.insert_prepared_run(conn, run_id=run_id, state=state, now=now)
        return self.get_status(run_id), False

    def apply_event(self, submission: EventSubmission) -> ApplicationReceipt:
        if submission.event.kind in FENCED_APPLY_EVENT_KINDS:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.FENCED,
                f"event kind {submission.event.kind} cannot enter via apply_event",
            )
        return self._apply_submission(submission, allow_fenced=False)

    def apply_fatal_failure(
        self,
        *,
        submission_id: str,
        run_id: str,
        expected_version: int,
        event: FatalFailureDetected,
    ) -> ApplicationReceipt:
        if event.kind != "fatal_failure_detected":
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "apply_fatal_failure requires FatalFailureDetected",
            )
        return self._apply_submission(
            EventSubmission(
                submission_id=submission_id,
                run_id=run_id,
                expected_version=expected_version,
                event=event,
            ),
            allow_fenced=True,
        )

    def abort_run(
        self,
        *,
        submission_id: str,
        run_id: str,
        reason: str = "user_requested_abort",
    ) -> ApplicationReceipt:
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            existing = self._store.find_event_by_id(conn, submission_id)
            if existing is not None:
                # Retry after unknown commit must not regenerate occurred_at identity,
                # but a changed abort reason is a caller payload collision.
                if existing["run_id"] != run_id:
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.COLLISION,
                        "submission_id reused with different run or payload",
                    )
                if existing["event_kind"] != "abort_requested":
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.COLLISION,
                        "submission_id reused with different run or payload",
                    )
                prior_event = self._store.load_event(existing["event_payload"])
                if not isinstance(prior_event, AbortRequested):
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.CORRUPTION,
                        "abort journal row is not AbortRequested",
                    )
                if prior_event.reason != reason:
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.COLLISION,
                        "submission_id reused with different run or payload",
                    )
                receipt = self._duplicate_or_collision_receipt(
                    conn,
                    EventSubmission(
                        submission_id=submission_id,
                        run_id=run_id,
                        expected_version=int(existing["observed_run_version"]),
                        event=prior_event,
                    ),
                    existing,
                )
            else:
                _state, version, _ = self._store.load_validated_snapshot(conn, run_id)
                submission = EventSubmission(
                    submission_id=submission_id,
                    run_id=run_id,
                    expected_version=version,
                    event=AbortRequested(occurred_at=now, reason=reason),
                )
                receipt = self._apply_submission_on_conn(
                    conn,
                    submission,
                    now=now,
                    allow_fenced=True,
                )
        self._fault.maybe_raise("post_commit")
        return receipt

    def _apply_submission(
        self,
        submission: EventSubmission,
        *,
        allow_fenced: bool,
    ) -> ApplicationReceipt:
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            receipt = self._apply_submission_on_conn(
                conn,
                submission,
                now=now,
                allow_fenced=allow_fenced,
            )
        self._fault.maybe_raise("post_commit")
        return receipt

    def _apply_submission_on_conn(
        self,
        conn: sqlite3.Connection,
        submission: EventSubmission,
        *,
        now: datetime,
        allow_fenced: bool,
        mark_completed_dispatch: tuple[str, str, DispatchStatus] | None = None,
    ) -> ApplicationReceipt:
        if not allow_fenced and submission.event.kind in FENCED_APPLY_EVENT_KINDS:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.FENCED,
                f"event kind {submission.event.kind} is fenced",
            )

        self._fault.maybe_raise("read")
        existing = self._store.find_event_by_id(conn, submission.submission_id)
        if existing is not None:
            return self._duplicate_or_collision_receipt(conn, submission, existing)

        state, version, _ = self._store.load_validated_snapshot(conn, submission.run_id)
        sequence = self._store.next_event_sequence(conn, submission.run_id)
        event_id = submission.submission_id

        if submission.expected_version != version:
            self._fault.maybe_raise("journal")
            self._store.insert_event_row(
                conn,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                event=submission.event,
                disposition=EventDisposition.STALE.value,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code="stale_expected_version",
                safe_detail="expected run version does not match current snapshot",
                now=now,
            )
            self._fault.maybe_raise("pre_commit")
            receipt = ApplicationReceipt(
                disposition=EventDisposition.STALE,
                submission_id=submission.submission_id,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code="stale_expected_version",
                safe_detail="expected run version does not match current snapshot",
                state=None,
                effects=(),
            )
            return receipt

        result = reduce_pr_review(state, submission.event)
        if isinstance(result, TransitionRejected):
            self._fault.maybe_raise("journal")
            detail = safe_diagnostic(result.detail or str(result.code))
            self._store.insert_event_row(
                conn,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                event=submission.event,
                disposition=EventDisposition.REJECTED.value,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code=str(result.code),
                safe_detail=detail,
                now=now,
            )
            self._fault.maybe_raise("pre_commit")
            receipt = ApplicationReceipt(
                disposition=EventDisposition.REJECTED,
                submission_id=submission.submission_id,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code=str(result.code),
                safe_detail=detail,
                state=None,
                effects=(),
            )
            return receipt

        assert isinstance(result, TransitionApplied)
        if isinstance(
            submission.event, RecoverMixedAdjudicationRequested
        ) and not self._store.supersede_pending_dispatch(
            conn,
            run_id=submission.run_id,
            effect_id=submission.event.superseded_reply_effect_id,
            now=now,
        ):
            detail = safe_diagnostic("recovery requires exact pending queue-head reply dispatch")
            self._fault.maybe_raise("journal")
            self._store.insert_event_row(
                conn,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                event=submission.event,
                disposition=EventDisposition.REJECTED.value,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code="dispatch_supersession_failed",
                safe_detail=detail,
                now=now,
            )
            self._fault.maybe_raise("pre_commit")
            return ApplicationReceipt(
                disposition=EventDisposition.REJECTED,
                submission_id=submission.submission_id,
                event_id=event_id,
                run_id=submission.run_id,
                sequence=sequence,
                expected_run_version=submission.expected_version,
                observed_run_version=version,
                resulting_run_version=None,
                rejection_code="dispatch_supersession_failed",
                safe_detail=detail,
                state=None,
                effects=(),
            )
        self._fault.maybe_raise("journal")
        self._store.insert_event_row(
            conn,
            event_id=event_id,
            run_id=submission.run_id,
            sequence=sequence,
            event=submission.event,
            disposition=EventDisposition.ACCEPTED.value,
            expected_run_version=submission.expected_version,
            observed_run_version=version,
            resulting_run_version=version + 1,
            rejection_code=None,
            safe_detail=None,
            now=now,
            resulting_state=result.state,
        )
        self._fault.maybe_raise("snapshot_cas")
        resulting = self._store.cas_update_snapshot(
            conn,
            run_id=submission.run_id,
            observed_version=version,
            new_state=result.state,
            now=now,
        )
        self._fault.maybe_raise("timer_outbox_insert")
        # Finalize the completed claim before inserting successor pending work so the
        # partial unique live-dispatch index never sees claimed+pending together.
        if mark_completed_dispatch is not None:
            dispatch_id, claim_id, status = mark_completed_dispatch
            self._finalize_dispatch_status(
                conn,
                dispatch_id=dispatch_id,
                claim_id=claim_id,
                status=status,
                event=submission.event,
                now=now,
            )
        self._store.reconcile_retry_timer(
            conn,
            source_event_id=event_id,
            run_id=submission.run_id,
            new_state=result.state,
            resulting_version=resulting,
            now=now,
        )
        effects = tuple(result.effects)
        self._store.insert_effect_dispatches(
            conn,
            source_event_id=event_id,
            run_id=submission.run_id,
            effects=effects,
            now=now,
            claimed_run_version=resulting,
        )
        if result.state.kind in TERMINAL_KINDS:
            self._store.cancel_live_work(conn, run_id=submission.run_id, now=now)
            lease_status = (
                LeaseStatus.ABORTED if result.state.kind == "aborted" else LeaseStatus.INACTIVE
            )
            self._store.invalidate_lease(
                conn,
                run_id=submission.run_id,
                now=now,
                status=lease_status,
            )
        self._fault.maybe_raise("pre_commit")
        receipt = ApplicationReceipt(
            disposition=EventDisposition.ACCEPTED,
            submission_id=submission.submission_id,
            event_id=event_id,
            run_id=submission.run_id,
            sequence=sequence,
            expected_run_version=submission.expected_version,
            observed_run_version=version,
            resulting_run_version=resulting,
            state=result.state,
            effects=effects,
        )
        return receipt

    def _duplicate_or_collision_receipt(
        self,
        conn: sqlite3.Connection,
        submission: EventSubmission,
        existing: sqlite3.Row,
        *,
        ignore_payload_digest: bool = False,
    ) -> ApplicationReceipt:
        _, _payload, digest = self._store.dump_event(submission.event)
        if existing["run_id"] != submission.run_id:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.COLLISION,
                "submission_id reused with different run or payload",
            )
        if not ignore_payload_digest and existing["event_payload_sha256"] != digest:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.COLLISION,
                "submission_id reused with different run or payload",
            )
        disposition = EventDisposition(existing["disposition"])
        state = None
        effects: tuple[Any, ...] = ()
        if (
            disposition is EventDisposition.ACCEPTED
            and existing["resulting_run_version"] is not None
        ):
            # Reconstruct the historical resulting state, never the current snapshot.
            resulting_payload = existing["resulting_state_payload"]
            resulting_digest = existing["resulting_state_payload_sha256"]
            if resulting_payload is None or resulting_digest is None:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CORRUPTION,
                    "accepted event missing historical resulting state",
                )
            if payload_sha256(resulting_payload) != resulting_digest:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CORRUPTION,
                    "accepted event resulting state hash mismatch",
                )
            state = self._store.load_state(resulting_payload)
            rows = conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE source_event_id = ?
                ORDER BY effect_ordinal ASC
                """,
                (existing["event_id"],),
            ).fetchall()
            effects_list: list[Any] = []
            for row in rows:
                if row["run_id"] != submission.run_id:
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.CORRUPTION,
                        "historical effect run_id disagrees with event",
                    )
                if row["source_event_id"] != existing["event_id"]:
                    raise PrReviewEngineError(
                        PrReviewEngineErrorKind.CORRUPTION,
                        "historical effect source_event_id mismatch",
                    )
                effects_list.append(self._store.load_validated_effect(row))
            effects = tuple(effects_list)
        return ApplicationReceipt(
            disposition=EventDisposition.DUPLICATE,
            submission_id=submission.submission_id,
            event_id=existing["event_id"],
            run_id=submission.run_id,
            sequence=int(existing["sequence"]),
            expected_run_version=existing["expected_run_version"],
            observed_run_version=int(existing["observed_run_version"]),
            resulting_run_version=existing["resulting_run_version"],
            rejection_code=existing["rejection_code"],
            safe_detail=existing["safe_detail"],
            state=state if disposition is EventDisposition.ACCEPTED else None,
            effects=effects if disposition is EventDisposition.ACCEPTED else (),
            duplicate_of_submission=True,
        )

    def _finalize_dispatch_status(
        self,
        conn: sqlite3.Connection,
        *,
        dispatch_id: str,
        claim_id: str,
        status: DispatchStatus,
        event: PrReviewEvent,
        now: datetime,
    ) -> None:
        error_kind = None
        error_summary = None
        if isinstance(event, (EffectRetryableFailure, WriteOutcomeUncertain)):
            error_kind = str(event.error.kind)
            error_summary = event.error.safe_summary
        elif isinstance(event, EffectBlocked):
            error_kind = str(event.reason)
            error_summary = event.safe_summary
        conn.execute(
            """
            UPDATE pr_review_effects
            SET status = ?, completed_at = ?, updated_at = ?,
                last_error_kind = COALESCE(?, last_error_kind),
                last_error_summary = COALESCE(?, last_error_summary)
            WHERE dispatch_id = ? AND claim_id = ? AND status = ?
            """,
            (
                status.value,
                encode_utc_instant(now),
                encode_utc_instant(now),
                error_kind,
                error_summary,
                dispatch_id,
                claim_id,
                DispatchStatus.CLAIMED.value,
            ),
        )

    def acquire_lease(self, run_id: str, owner_id: str) -> LeaseAcquireResult:
        owner = validate_owner_id(owner_id)
        now = self._clock.now()
        expires = now + self._lease_ttl
        with self._store.begin_immediate() as conn:
            self._store.load_validated_snapshot(conn, run_id)
            row = self._store.get_lease_row(conn, run_id)
            if (
                row["status"] == LeaseStatus.ACTIVE.value
                and row["expires_at"] is not None
                and parse_utc_instant(row["expires_at"]) > now
                and row["owner_id"] != owner
            ):
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CONFLICT,
                    "active lease held by another owner",
                )
            generation = int(row["generation"]) + 1
            conn.execute(
                """
                UPDATE pr_review_worker_leases
                SET owner_id = ?, generation = ?, status = ?,
                    acquired_at = ?, heartbeat_at = ?, expires_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    owner,
                    generation,
                    LeaseStatus.ACTIVE.value,
                    encode_utc_instant(now),
                    encode_utc_instant(now),
                    encode_utc_instant(expires),
                    encode_utc_instant(now),
                    run_id,
                ),
            )
        return LeaseAcquireResult(
            run_id=run_id,
            owner_id=owner,
            generation=generation,
            status=LeaseStatus.ACTIVE,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires,
        )

    def heartbeat_lease(self, run_id: str, owner_id: str, generation: int) -> LeaseHeartbeatResult:
        owner = validate_owner_id(owner_id)
        now = self._clock.now()
        expires = now + self._lease_ttl
        with self._store.begin_immediate() as conn:
            row = self._store.get_lease_row(conn, run_id)
            accepted = self._lease_matches(row, owner=owner, generation=generation, now=now)
            if accepted:
                conn.execute(
                    """
                    UPDATE pr_review_worker_leases
                    SET heartbeat_at = ?, expires_at = ?, updated_at = ?
                    WHERE run_id = ? AND owner_id = ? AND generation = ? AND status = ?
                    """,
                    (
                        encode_utc_instant(now),
                        encode_utc_instant(expires),
                        encode_utc_instant(now),
                        run_id,
                        owner,
                        generation,
                        LeaseStatus.ACTIVE.value,
                    ),
                )
            else:
                expires = (
                    parse_utc_instant(row["expires_at"]) if row["expires_at"] is not None else now
                )
                now_hb = (
                    parse_utc_instant(row["heartbeat_at"])
                    if row["heartbeat_at"] is not None
                    else now
                )
                return LeaseHeartbeatResult(
                    run_id=run_id,
                    owner_id=owner,
                    generation=int(row["generation"]),
                    status=LeaseStatus(row["status"]),
                    heartbeat_at=now_hb,
                    expires_at=expires,
                    accepted=False,
                )
        return LeaseHeartbeatResult(
            run_id=run_id,
            owner_id=owner,
            generation=generation,
            status=LeaseStatus.ACTIVE,
            heartbeat_at=now,
            expires_at=expires,
            accepted=True,
        )

    def release_lease(self, run_id: str, owner_id: str, generation: int) -> LeaseReleaseResult:
        owner = validate_owner_id(owner_id)
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            row = self._store.get_lease_row(conn, run_id)
            accepted = self._lease_matches(row, owner=owner, generation=generation, now=now)
            if accepted:
                conn.execute(
                    """
                    UPDATE pr_review_worker_leases
                    SET status = ?, owner_id = NULL, updated_at = ?,
                        heartbeat_at = NULL, expires_at = NULL
                    WHERE run_id = ? AND generation = ?
                    """,
                    (
                        LeaseStatus.INACTIVE.value,
                        encode_utc_instant(now),
                        run_id,
                        generation,
                    ),
                )
            return LeaseReleaseResult(
                run_id=run_id,
                owner_id=owner,
                generation=int(row["generation"]),
                status=LeaseStatus.INACTIVE if accepted else LeaseStatus(row["status"]),
                accepted=accepted,
            )

    @staticmethod
    def _lease_matches(
        row: sqlite3.Row,
        *,
        owner: str,
        generation: int,
        now: datetime,
    ) -> bool:
        if row["status"] != LeaseStatus.ACTIVE.value:
            return False
        if row["owner_id"] != owner or int(row["generation"]) != generation:
            return False
        if row["expires_at"] is None:
            return False
        return parse_utc_instant(row["expires_at"]) > now

    def check_claim_authority(self, snapshot: ClaimAuthoritySnapshot) -> ClaimAuthorityResult:
        """Read-only pre-mutation authority check.

        Validates the current lease, claimed dispatch, run version, and effect
        identity without exposing SQL rows or holding a transaction across
        repository locks or external processes.
        """

        now = self._clock.now()
        try:
            owner = validate_owner_id(snapshot.owner_id)
        except PrReviewEngineError:
            return ClaimAuthorityResult(
                status=WriteAuthorityStatus.REJECTED,
                safe_summary="owner_id is invalid",
            )
        with self._store.begin_read() as conn:
            try:
                state, version, _ = self._store.load_validated_snapshot(conn, snapshot.run_id)
            except PrReviewEngineError:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="run snapshot is unavailable",
                )
            if state.kind in TERMINAL_KINDS:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="run is terminal",
                )
            lease = self._store.get_lease_row(conn, snapshot.run_id)
            if not self._lease_matches(
                lease,
                owner=owner,
                generation=snapshot.lease_generation,
                now=now,
            ):
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="lease owner, generation, or expiry fence",
                )
            row = conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE run_id = ? AND dispatch_id = ?
                """,
                (snapshot.run_id, snapshot.dispatch_id),
            ).fetchone()
            if row is None:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="dispatch not found",
                )
            if row["status"] != DispatchStatus.CLAIMED.value:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="dispatch is not claimed",
                )
            if row["claim_id"] != snapshot.claim_id:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claim_id mismatch",
                )
            if row["claim_owner_id"] != owner:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claim owner mismatch",
                )
            if int(row["claim_lease_generation"] or 0) != snapshot.lease_generation:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claim lease generation mismatch",
                )
            if int(row["claimed_run_version"] or 0) != snapshot.claimed_run_version:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claimed run version mismatch",
                )
            if int(row["claimed_run_version"] or 0) != version:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claimed run version is stale",
                )
            try:
                effect = self._store.load_validated_effect(row)
            except PrReviewEngineError:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="claimed effect payload is invalid",
                )
            if effect.effect_id != snapshot.effect_id:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="effect_id mismatch",
                )
            if int(effect.attempt) != snapshot.attempt:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="attempt mismatch",
                )
            if int(effect.cycle_number) != snapshot.cycle_number:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="cycle_number mismatch",
                )
            if effect.bound_head_sha != snapshot.bound_head_sha:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="bound_head_sha mismatch",
                )
            active = active_effect(state)
            if active is None or active.effect_id != effect.effect_id:
                return ClaimAuthorityResult(
                    status=WriteAuthorityStatus.REJECTED,
                    safe_summary="active effect mismatch",
                )
        return ClaimAuthorityResult(status=WriteAuthorityStatus.AUTHORIZED)

    def recover_expired_claims(self, run_id: str, owner_id: str, generation: int) -> int:
        owner = validate_owner_id(owner_id)
        now = self._clock.now()
        recovered = 0
        with self._store.begin_immediate() as conn:
            state, version, _ = self._store.load_validated_snapshot(conn, run_id)
            lease = self._store.get_lease_row(conn, run_id)
            if not self._lease_matches(lease, owner=owner, generation=generation, now=now):
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.FENCED,
                    "recovery requires current active lease",
                )
            rows = conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE run_id = ? AND status = ?
                """,
                (run_id, DispatchStatus.CLAIMED.value),
            ).fetchall()
            for row in rows:
                if row["claim_lease_generation"] is None:
                    continue
                claim_gen = int(row["claim_lease_generation"])
                if claim_gen >= generation:
                    continue
                # Claim generation older than current lease => expired fencing.
                recovered += self._recover_one_claim(
                    conn,
                    run_id=run_id,
                    state=state,
                    version=version,
                    row=row,
                    now=now,
                )
                state, version, _ = self._store.load_validated_snapshot(conn, run_id)
        return recovered

    def _recover_one_claim(
        self,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        state: PrReviewState,
        version: int,
        row: sqlite3.Row,
        now: datetime,
    ) -> int:
        effect = self._store.load_validated_effect(row)
        active = active_effect(state)
        if active is None or active != effect:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.CORRUPTION,
                "expired claim does not match active state effect",
            )
        classification = EffectClassification(row["classification"])
        expired_gen = int(row["claim_lease_generation"])
        attempt = int(row["attempt"])
        dispatch_id = row["dispatch_id"]
        claim_id = row["claim_id"]

        if classification in {EffectClassification.READ_ONLY, EffectClassification.RECONCILING}:
            conn.execute(
                """
                UPDATE pr_review_effects
                SET status = ?, claim_id = NULL, claim_owner_id = NULL,
                    claim_lease_generation = NULL, claimed_at = NULL,
                    claimed_run_version = NULL, available_at = MIN(available_at, ?),
                    updated_at = ?
                WHERE dispatch_id = ?
                """,
                (
                    DispatchStatus.PENDING.value,
                    encode_utc_instant(now),
                    encode_utc_instant(now),
                    dispatch_id,
                ),
            )
            return 1

        if classification is EffectClassification.LOCAL:
            token = EffectCompletionToken(
                effect_id=effect.effect_id,
                expected_run_version=version,
                lease_generation=int(self._store.get_lease_row(conn, run_id)["generation"]),
                cycle_number=effect.cycle_number,
                bound_head_sha=effect.bound_head_sha,
            )
            event = EffectBlocked(
                occurred_at=now,
                token=token,
                reason=PauseReasonKind.REQUIRED_OPERATOR_ACTION,
                safe_action=SafeAction(
                    kind=SafeActionKind.INSPECT_ARTIFACTS,
                    condition="inspect local effect artifacts after expired claim",
                ),
                safe_summary="local effect claim expired; operator inspection required",
            )
            submission_id = recovery_submission_id(
                run_id=run_id,
                dispatch_id=dispatch_id,
                attempt=attempt,
                expired_generation=expired_gen,
                kind="local-blocked",
            )
            receipt = self._apply_submission_on_conn(
                conn,
                EventSubmission(
                    submission_id=submission_id,
                    run_id=run_id,
                    expected_version=version,
                    event=event,
                ),
                now=now,
                allow_fenced=True,
                mark_completed_dispatch=(
                    dispatch_id,
                    claim_id,
                    DispatchStatus.BLOCKED,
                ),
            )
            if receipt.disposition is not EventDisposition.ACCEPTED:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.INTERNAL,
                    "local expired-claim recovery failed",
                )
            return 1

        if classification is EffectClassification.MUTATING:
            if not is_mutating_effect(effect):
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CORRUPTION,
                    "mutating classification without MutatingEffect payload",
                )
            token = EffectCompletionToken(
                effect_id=effect.effect_id,
                expected_run_version=version,
                lease_generation=int(self._store.get_lease_row(conn, run_id)["generation"]),
                cycle_number=effect.cycle_number,
                bound_head_sha=effect.bound_head_sha,
            )
            recon_id = recovery_submission_id(
                run_id=run_id,
                dispatch_id=dispatch_id,
                attempt=attempt,
                expired_generation=expired_gen,
                kind="mutating-uncertain",
            )
            uncertain_event = WriteOutcomeUncertain(
                occurred_at=now,
                token=token,
                error=ErrorSummary(
                    kind=TransientErrorKind.TIMEOUT,
                    safe_summary="mutating effect claim expired before completion",
                ),
                reconciliation_identity=recon_id,
                original_write=cast(MutatingEffect, effect),
            )
            receipt = self._apply_submission_on_conn(
                conn,
                EventSubmission(
                    submission_id=recon_id,
                    run_id=run_id,
                    expected_version=version,
                    event=uncertain_event,
                ),
                now=now,
                allow_fenced=True,
                mark_completed_dispatch=(
                    dispatch_id,
                    claim_id,
                    DispatchStatus.UNCERTAIN,
                ),
            )
            if receipt.disposition is not EventDisposition.ACCEPTED:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.INTERNAL,
                    "mutating expired-claim recovery failed",
                )
            return 1

        raise PrReviewEngineError(
            PrReviewEngineErrorKind.CORRUPTION,
            f"unknown effect classification {classification}",
        )

    def claim_next_effect(self, run_id: str, owner_id: str, generation: int) -> EffectClaimResult:
        owner = validate_owner_id(owner_id)
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            state, version, _ = self._store.load_validated_snapshot(conn, run_id)
            lease = self._store.get_lease_row(conn, run_id)
            if not self._lease_matches(lease, owner=owner, generation=generation, now=now):
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.FENCED,
                    "claim requires current active lease",
                )
            if state.kind in TERMINAL_KINDS:
                return EffectClaimResult(claim=None, reason="run is terminal")
            if state.kind == "paused":
                return EffectClaimResult(claim=None, reason="run is paused")
            if state.kind == "waiting_retry":
                return EffectClaimResult(claim=None, reason="run is waiting for retry")
            if state.kind == "waiting_for_user":
                # May still have an active reply effect.
                pass

            # Recover claims from older generations before selecting work.
            claimed_rows = conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE run_id = ? AND status = ?
                """,
                (run_id, DispatchStatus.CLAIMED.value),
            ).fetchall()
            for row in claimed_rows:
                claim_gen = row["claim_lease_generation"]
                if claim_gen is not None and int(claim_gen) < generation:
                    self._recover_one_claim(
                        conn,
                        run_id=run_id,
                        state=state,
                        version=version,
                        row=row,
                        now=now,
                    )
                    state, version, _ = self._store.load_validated_snapshot(conn, run_id)

            active = active_effect(state)
            if active is None:
                return EffectClaimResult(claim=None, reason="no active effect")

            row = conn.execute(
                """
                SELECT * FROM pr_review_effects
                WHERE run_id = ? AND status = ? AND available_at <= ?
                ORDER BY available_at ASC, dispatch_id ASC
                LIMIT 1
                """,
                (
                    run_id,
                    DispatchStatus.PENDING.value,
                    encode_utc_instant(now),
                ),
            ).fetchone()
            if row is None:
                return EffectClaimResult(claim=None, reason="no eligible pending effect")

            effect = self._store.load_validated_effect(row)
            if effect != active:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.CORRUPTION,
                    "pending dispatch does not match active state effect",
                )

            claim_id = self._ids.new_id("claim")
            cur = conn.execute(
                """
                UPDATE pr_review_effects
                SET status = ?, claim_id = ?, claim_owner_id = ?,
                    claim_lease_generation = ?, claimed_at = ?,
                    claimed_run_version = ?, updated_at = ?
                WHERE dispatch_id = ? AND status = ?
                """,
                (
                    DispatchStatus.CLAIMED.value,
                    claim_id,
                    owner,
                    generation,
                    encode_utc_instant(now),
                    version,
                    encode_utc_instant(now),
                    row["dispatch_id"],
                    DispatchStatus.PENDING.value,
                ),
            )
            if cur.rowcount != 1:
                return EffectClaimResult(claim=None, reason="claim race lost")

            token = EffectCompletionToken(
                effect_id=effect.effect_id,
                expected_run_version=version,
                lease_generation=generation,
                cycle_number=effect.cycle_number,
                bound_head_sha=effect.bound_head_sha,
            )
            claim = EffectClaim(
                dispatch_id=row["dispatch_id"],
                claim_id=claim_id,
                run_id=run_id,
                effect=effect,
                attempt=effect.attempt,
                max_attempts=effect.max_attempts,
                classification=classify_effect(effect).value,
                claimed_run_version=version,
                owner_id=owner,
                lease_generation=generation,
                claimed_at=now,
                lease_expires_at=parse_utc_instant(lease["expires_at"]),
                completion_token=token,
            )
            return EffectClaimResult(claim=claim)

    def complete_claim(self, request: EffectCompletionRequest) -> ApplicationReceipt:
        if request.event.kind not in EFFECT_RESULT_KINDS:
            raise PrReviewEngineError(
                PrReviewEngineErrorKind.VALIDATION,
                "complete_claim requires an effect-result event",
            )
        now = self._clock.now()
        with self._store.begin_immediate() as conn:
            # Deduplicate before fencing so post-commit retries return the prior receipt
            # even when the dispatch is no longer claimed.
            existing = self._store.find_event_by_id(conn, request.submission_id)
            if existing is not None:
                dispatch = self._store.get_dispatch(conn, request.dispatch_id)
                run_id = dispatch["run_id"]
                receipt = self._duplicate_or_collision_receipt(
                    conn,
                    EventSubmission(
                        submission_id=request.submission_id,
                        run_id=run_id,
                        expected_version=int(
                            existing["expected_run_version"] or existing["observed_run_version"]
                        ),
                        event=request.event,
                    ),
                    existing,
                )
            else:
                dispatch = self._store.get_dispatch(conn, request.dispatch_id)
                run_id = dispatch["run_id"]
                state, version, _ = self._store.load_validated_snapshot(conn, run_id)
                lease = self._store.get_lease_row(conn, run_id)
                owner = validate_owner_id(request.owner_id)

                stale_reason = self._completion_fence_reason(
                    state=state,
                    version=version,
                    lease=lease,
                    dispatch=dispatch,
                    request=request,
                    owner=owner,
                    now=now,
                )
                if stale_reason is not None:
                    sequence = self._store.next_event_sequence(conn, run_id)
                    token = getattr(request.event, "token", None)
                    journaled_expected = (
                        int(token.expected_run_version) if token is not None else version
                    )
                    self._store.insert_event_row(
                        conn,
                        event_id=request.submission_id,
                        run_id=run_id,
                        sequence=sequence,
                        event=request.event,
                        disposition=EventDisposition.STALE.value,
                        expected_run_version=journaled_expected,
                        observed_run_version=version,
                        resulting_run_version=None,
                        rejection_code="stale_completion",
                        safe_detail=stale_reason,
                        now=now,
                    )
                    receipt = ApplicationReceipt(
                        disposition=EventDisposition.STALE,
                        submission_id=request.submission_id,
                        event_id=request.submission_id,
                        run_id=run_id,
                        sequence=sequence,
                        expected_run_version=journaled_expected,
                        observed_run_version=version,
                        rejection_code="stale_completion",
                        safe_detail=stale_reason,
                    )
                else:
                    status = self._dispatch_status_for_event(request.event)
                    receipt = self._apply_submission_on_conn(
                        conn,
                        EventSubmission(
                            submission_id=request.submission_id,
                            run_id=run_id,
                            expected_version=version,
                            event=request.event,
                        ),
                        now=now,
                        allow_fenced=True,
                        mark_completed_dispatch=(
                            request.dispatch_id,
                            request.claim_id,
                            status,
                        ),
                    )
        self._fault.maybe_raise("post_commit")
        return receipt

    def _completion_fence_reason(
        self,
        *,
        state: PrReviewState,
        version: int,
        lease: sqlite3.Row,
        dispatch: sqlite3.Row,
        request: EffectCompletionRequest,
        owner: str,
        now: datetime,
    ) -> str | None:
        # Explicit lost-authority signal from the worker: fence before any mutation
        # regardless of whether best-effort durable lease relinquishment succeeded.
        if request.lease_authority_lost:
            return "worker reported lease authority lost"
        if dispatch["status"] != DispatchStatus.CLAIMED.value:
            return "dispatch is not claimed"
        if dispatch["claim_id"] != request.claim_id:
            return "claim_id mismatch"
        if dispatch["claim_owner_id"] != owner:
            return "claim owner mismatch"
        if int(dispatch["claim_lease_generation"]) != request.lease_generation:
            return "claim lease generation mismatch"
        if not self._lease_matches(
            lease, owner=owner, generation=request.lease_generation, now=now
        ):
            return "lease generation or expiry fence"
        if int(dispatch["claimed_run_version"]) != version:
            return "claimed run version mismatch"
        token = getattr(request.event, "token", None)
        if token is None:
            return "missing completion token"
        if int(token.expected_run_version) != version:
            return "token expected_run_version mismatch"
        if int(token.lease_generation) != request.lease_generation:
            return "token lease_generation mismatch"
        effect = self._store.load_validated_effect(dispatch)
        if token.effect_id != effect.effect_id:
            return "token effect_id mismatch"
        if token.cycle_number != effect.cycle_number:
            return "token cycle mismatch"
        if token.bound_head_sha != effect.bound_head_sha:
            return "token head sha mismatch"
        active = active_effect(state)
        if active is None or active != effect:
            return "active effect mismatch"
        if (
            isinstance(request.event, EffectRetryableFailure)
            and request.event.failed_attempt != effect.attempt
        ):
            return "failed_attempt mismatch"
        return None

    @staticmethod
    def _dispatch_status_for_event(event: PrReviewEvent) -> DispatchStatus:
        if isinstance(event, EffectSucceeded):
            return DispatchStatus.SUCCEEDED
        if isinstance(event, EffectRetryableFailure):
            return DispatchStatus.RETRY_WAIT
        if isinstance(event, EffectBlocked):
            return DispatchStatus.BLOCKED
        if isinstance(event, WriteOutcomeUncertain):
            return DispatchStatus.UNCERTAIN
        raise PrReviewEngineError(
            PrReviewEngineErrorKind.VALIDATION,
            "unsupported completion event",
        )

    def fire_due_timers(self, *, limit: int = 100) -> list[TimerFireReceipt]:
        now = self._clock.now()
        with self._store.begin_read() as conn:
            timer_ids = self._store.list_due_timer_ids(conn, now)[:limit]
        receipts: list[TimerFireReceipt] = []
        for timer_id in timer_ids:
            receipts.append(self._fire_one_timer(timer_id, now=now))
        return receipts

    def fire_due_timers_for_run(self, run_id: str, *, limit: int = 100) -> list[TimerFireReceipt]:
        now = self._clock.now()
        with self._store.begin_read() as conn:
            timer_ids = self._store.list_due_timer_ids_for_run(conn, run_id, now)[:limit]
        receipts: list[TimerFireReceipt] = []
        for timer_id in timer_ids:
            receipts.append(self._fire_one_timer(timer_id, now=now))
        return receipts

    def _fire_one_timer(self, timer_id: str, *, now: datetime) -> TimerFireReceipt:
        event_id = timer_event_id(timer_id)
        with self._store.begin_immediate() as conn:
            row = self._store.get_timer_row(conn, timer_id)
            if row is None:
                raise PrReviewEngineError(
                    PrReviewEngineErrorKind.NOT_FOUND,
                    "timer not found",
                )
            run_id = row["run_id"]
            if row["status"] != TimerStatus.PENDING.value:
                return TimerFireReceipt(
                    timer_id=timer_id,
                    run_id=run_id,
                    disposition=EventDisposition.DUPLICATE,
                    event_id=row["fired_event_id"],
                    superseded=row["status"] == TimerStatus.SUPERSEDED.value,
                )
            state, version, _ = self._store.load_validated_snapshot(conn, run_id)
            if (
                state.kind != "waiting_retry"
                or state.retrying_effect_id != row["target_effect_id"]
                or version != int(row["expected_run_version"])
            ):
                conn.execute(
                    """
                    UPDATE pr_review_timers
                    SET status = ?, updated_at = ?
                    WHERE timer_id = ? AND status = ?
                    """,
                    (
                        TimerStatus.SUPERSEDED.value,
                        encode_utc_instant(now),
                        timer_id,
                        TimerStatus.PENDING.value,
                    ),
                )
                return TimerFireReceipt(
                    timer_id=timer_id,
                    run_id=run_id,
                    disposition=EventDisposition.STALE,
                    superseded=True,
                )

            existing = self._store.find_event_by_id(conn, event_id)
            if existing is not None:
                return TimerFireReceipt(
                    timer_id=timer_id,
                    run_id=run_id,
                    disposition=EventDisposition.DUPLICATE,
                    event_id=event_id,
                    resulting_run_version=existing["resulting_run_version"],
                )

            event = RetryDue(
                occurred_at=now,
                pending_effect_id=row["target_effect_id"],
                current_time=now,
            )
            receipt = self._apply_submission_on_conn(
                conn,
                EventSubmission(
                    submission_id=event_id,
                    run_id=run_id,
                    expected_version=version,
                    event=event,
                ),
                now=now,
                allow_fenced=True,
            )
            if receipt.disposition is EventDisposition.ACCEPTED:
                conn.execute(
                    """
                    UPDATE pr_review_timers
                    SET status = ?, fired_event_id = ?, updated_at = ?
                    WHERE timer_id = ?
                    """,
                    (
                        TimerStatus.FIRED.value,
                        event_id,
                        encode_utc_instant(now),
                        timer_id,
                    ),
                )
            return TimerFireReceipt(
                timer_id=timer_id,
                run_id=run_id,
                disposition=receipt.disposition,
                event_id=event_id,
                resulting_run_version=receipt.resulting_run_version,
            )

    def get_status(self, run_id: str) -> PrReviewStatus:
        now = self._clock.now()
        with self._store.begin_read() as conn:
            state, version, updated_at = self._store.load_validated_snapshot(conn, run_id)
            lease = self._store.get_lease_row(conn, run_id)
            live = self._store.get_live_dispatch(conn, run_id)
            retry_wait = None
            if live is None:
                retry_wait = conn.execute(
                    """
                    SELECT * FROM pr_review_effects
                    WHERE run_id = ? AND status = ?
                    ORDER BY updated_at DESC, dispatch_id DESC
                    LIMIT 1
                    """,
                    (run_id, DispatchStatus.RETRY_WAIT.value),
                ).fetchone()
            effect_row = live or retry_wait
            effect_status = DispatchStatus(effect_row["status"]) if effect_row is not None else None
            next_eligible = None
            effect_attempt = None
            effect_max = None
            last_error_kind = None
            last_error_summary = None
            if effect_row is not None:
                next_eligible = parse_utc_instant(effect_row["available_at"])
                effect_attempt = int(effect_row["attempt"])
                effect_max = int(effect_row["max_attempts"])
                last_error_kind = effect_row["last_error_kind"]
                last_error_summary = effect_row["last_error_summary"]
            if state.kind == "waiting_retry":
                next_eligible = state.next_attempt_at
            lease_active = (
                lease["status"] == LeaseStatus.ACTIVE.value
                and lease["expires_at"] is not None
                and parse_utc_instant(lease["expires_at"]) > now
            )
            return build_status(
                state=state,
                run_version=version,
                updated_at=updated_at,
                effect_status=effect_status,
                effect_attempt=effect_attempt,
                effect_max_attempts=effect_max,
                next_eligible_at=next_eligible,
                last_error_kind=last_error_kind,
                last_error_summary=last_error_summary,
                lease_active=lease_active,
                lease_generation=int(lease["generation"]),
                lease_heartbeat_at=(
                    parse_utc_instant(lease["heartbeat_at"])
                    if lease["heartbeat_at"] is not None
                    else None
                ),
                lease_expires_at=(
                    parse_utc_instant(lease["expires_at"])
                    if lease["expires_at"] is not None
                    else None
                ),
                now=now,
            )


def _prepared_identity_fingerprint(state: PreparedState) -> str:
    """Canonical fingerprint of prepared origin + limits (secret-free)."""

    payload = {
        "origin": state.origin.model_dump(mode="json"),
        "limits": state.limits.model_dump(mode="json"),
    }
    return _canonical_sha(payload)


def _canonical_sha(payload: dict[str, Any]) -> str:
    import json

    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return payload_sha256(text + "\n")


def _ownership_conflicts(state: PrReviewState, ownership: PreparedOwnershipKeys) -> bool:
    origin = state.origin
    if (
        ownership.source_run_id is not None
        and origin.kind == "source_run"
        and origin.source_run_id == ownership.source_run_id
    ):
        return True
    repo_name: str | None = None
    pr_number: int | None = None
    head_branch: str | None = None
    if origin.kind == "source_run":
        repo_name = origin.repository.name_with_owner
        head_branch = origin.head_branch
    elif origin.kind == "existing_pr":
        repo_name = origin.binding.repository.name_with_owner
        pr_number = origin.binding.pr_number
        head_branch = origin.binding.head_branch
    binding = getattr(state, "binding", None)
    if binding is not None:
        repo_name = binding.repository.name_with_owner
        pr_number = binding.pr_number
        head_branch = binding.head_branch
    if (
        ownership.pr_number is not None
        and pr_number is not None
        and repo_name == ownership.repository
        and pr_number == ownership.pr_number
    ):
        return True
    return (
        ownership.head_branch is not None
        and head_branch is not None
        and repo_name == ownership.repository
        and head_branch == ownership.head_branch
    )
