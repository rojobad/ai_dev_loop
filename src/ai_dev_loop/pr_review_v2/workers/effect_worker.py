"""Bounded one-step effect worker for PR review v2."""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import Protocol, runtime_checkable

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EffectExecutor,
    EventDisposition,
    LeaseStatus,
    WorkerStepResult,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.infrastructure.runtime import completion_submission_id

DEFAULT_HEARTBEAT_INTERVAL = timedelta(seconds=10)


@runtime_checkable
class IntervalWait(Protocol):
    """Stoppable interval wait for lease-renewal coordination.

    ``wait(timeout)`` returns True for an early wake (no heartbeat) and False when
    the interval elapses. ``wake_for_stop()`` must unblock any in-flight ``wait`` so
    ``LeaseRenewalCoordinator.stop()`` can join without a separate controller cleanup.
    """

    def wait(self, timeout: float) -> bool: ...

    def wake_for_stop(self) -> None: ...


class EventIntervalWait:
    """Default interval wait backed by a threading.Event."""

    def __init__(self, event: threading.Event) -> None:
        self._event = event

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout=timeout)

    def wake_for_stop(self) -> None:
        self._event.set()


class LeaseRenewalCoordinator:
    """Bounded in-flight lease renewal using independent engine heartbeats."""

    def __init__(
        self,
        engine: PrReviewEngine,
        *,
        run_id: str,
        owner_id: str,
        generation: int,
        interval: timedelta,
        wait: threading.Event | None = None,
        interval_wait: IntervalWait | None = None,
    ) -> None:
        if interval.total_seconds() <= 0:
            raise ValueError("heartbeat interval must be positive")
        lease_ttl = engine.lease_ttl
        if interval >= lease_ttl:
            raise ValueError("heartbeat interval must be strictly less than lease_ttl")
        self._engine = engine
        self._run_id = run_id
        self._owner_id = owner_id
        self._generation = generation
        self._interval = interval
        self._stop = threading.Event()
        self._wait = wait or threading.Event()
        self._interval_wait: IntervalWait = interval_wait or EventIntervalWait(self._wait)
        self._lease_lost = False
        self._thread: threading.Thread | None = None
        self._heartbeat_count = 0

    @property
    def lease_lost(self) -> bool:
        return self._lease_lost

    @property
    def heartbeat_count(self) -> int:
        return self._heartbeat_count

    @property
    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("renewal already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"pr-review-v2-lease-renewal-{self._run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wait.set()
        self._interval_wait.wake_for_stop()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=max(1.0, self._interval.total_seconds() + 1.0))
        # Never discard a still-live thread reference; join may have timed out.
        if not thread.is_alive():
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait first so short executions never start a heartbeat race.
            triggered = self._interval_wait.wait(self._interval.total_seconds())
            if self._stop.is_set():
                return
            if triggered:
                self._wait.clear()
                continue
            try:
                result = self._engine.heartbeat_lease(
                    self._run_id,
                    self._owner_id,
                    self._generation,
                )
            except Exception:  # noqa: BLE001 - durable fence; still complete through claim
                self._mark_lease_lost_durably()
                return
            self._heartbeat_count += 1
            if not result.accepted or result.status is not LeaseStatus.ACTIVE:
                self._lease_lost = True
                return

    def _mark_lease_lost_durably(self) -> None:
        """Mark lease lost in memory and relinquish the durable lease when possible.

        Heartbeat exceptions must not leave an active lease that would allow
        ``complete_claim()`` to accept a late result. Relinquishment preserves the
        original owner/generation identity for the eventual fenced completion offer.
        """

        self._lease_lost = True
        try:
            self._engine.release_lease(self._run_id, self._owner_id, self._generation)
        except Exception:  # noqa: BLE001 - still offer through complete_claim fencing
            return


class EffectWorker:
    """Acquire/recover/claim, execute outside the transaction, then complete."""

    def __init__(
        self,
        engine: PrReviewEngine,
        executor: EffectExecutor,
        *,
        owner_id: str,
        heartbeat_interval: timedelta | None = None,
        heartbeat_wait: threading.Event | None = None,
        heartbeat_interval_wait: IntervalWait | None = None,
    ) -> None:
        self._engine = engine
        self._executor = executor
        self._owner_id = owner_id
        self._heartbeat_interval = (
            DEFAULT_HEARTBEAT_INTERVAL if heartbeat_interval is None else heartbeat_interval
        )
        self._heartbeat_wait = heartbeat_wait
        self._heartbeat_interval_wait = heartbeat_interval_wait

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
        renewal = LeaseRenewalCoordinator(
            self._engine,
            run_id=run_id,
            owner_id=self._owner_id,
            generation=lease.generation,
            interval=self._heartbeat_interval,
            wait=self._heartbeat_wait,
            interval_wait=self._heartbeat_interval_wait,
        )
        renewal.start()
        try:
            # Claim is already committed before executor runs.
            # Use engine clock at execute entry; executor may also inject its own
            # post-read observation clock for occurred_at / next_attempt_at.
            event = self._executor.execute(
                claim.effect,
                claim.completion_token,
                now=self._engine.clock.now(),
            )
        except Exception:
            renewal.stop()
            raise
        else:
            renewal.stop()

        submission_id = completion_submission_id(
            run_id=claim.run_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            lease_generation=lease.generation,
            result_key=result_key,
        )
        # Always submit through complete_claim for final fencing, even after lease loss.
        # lease_authority_lost makes fencing independent of best-effort release_lease().
        receipt = self._engine.complete_claim(
            EffectCompletionRequest(
                submission_id=submission_id,
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id=self._owner_id,
                lease_generation=lease.generation,
                event=event,
                lease_authority_lost=renewal.lease_lost,
            )
        )
        if not renewal.lease_lost:
            self._engine.heartbeat_lease(run_id, self._owner_id, lease.generation)
        return WorkerStepResult(
            run_id=run_id,
            claimed=True,
            completed=receipt.disposition
            in {EventDisposition.ACCEPTED, EventDisposition.DUPLICATE},
            disposition=receipt.disposition,
            dispatch_id=claim.dispatch_id,
            effect_kind=claim.effect.kind,
            safe_detail=receipt.safe_detail
            if not renewal.lease_lost
            else (receipt.safe_detail or "lease_lost_during_execution"),
        )
