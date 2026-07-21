"""Shared helpers for Phase 16.4 acceptance-matrix integration tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success
from tests.unit.pr_review_v2.helpers import HASH_2, SHA_B, artifact

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    FaultHook,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    CommitRecordedOutcome,
    EffectSucceeded,
    PrBoundOutcome,
    PullRequestBinding,
    PushConfirmedOutcome,
    ReviewTriggerConfirmedOutcome,
    TriggerEvidence,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory, parse_utc_instant
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore


def make_engine(
    db_path: Path,
    clock: FakeClock,
    *,
    prefix: str = "matrix",
    fault_hook: FaultHook | None = None,
    busy_timeout_ms: int = 5000,
) -> PrReviewEngine:
    return PrReviewEngine(
        SqlitePrReviewStore(db_path, busy_timeout_ms=busy_timeout_ms),
        clock=clock,
        ids=SequenceIdFactory(prefix=prefix),
        fault_hook=fault_hook,
        lease_ttl=timedelta(seconds=30),
    )


def fingerprint(engine: PrReviewEngine, run_id: str) -> tuple:
    with engine.store.begin_read() as conn:
        run = conn.execute(
            "SELECT version, state_kind, state_payload_sha256 FROM pr_review_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        timers = conn.execute(
            """
            SELECT timer_id, status, due_at, target_effect_id
            FROM pr_review_timers WHERE run_id=? ORDER BY timer_id
            """,
            (run_id,),
        ).fetchall()
        effects = conn.execute(
            """
            SELECT dispatch_id, status, attempt, effect_payload_sha256, available_at
            FROM pr_review_effects WHERE run_id=? ORDER BY dispatch_id
            """,
            (run_id,),
        ).fetchall()
    return (
        (int(run["version"]), run["state_kind"], run["state_payload_sha256"]),
        tuple(tuple(r) for r in timers),
        tuple(tuple(r) for r in effects),
    )


def claim_next(engine: PrReviewEngine, run_id: str, owner: str = "owner-a"):
    lease = engine.acquire_lease(run_id, owner)
    result = engine.claim_next_effect(run_id, owner, lease.generation)
    assert result.claim is not None
    return lease, result.claim


def complete_ok(
    engine: PrReviewEngine,
    lease,
    claim,
    submission_id: str,
    event,
    *,
    owner: str = "owner-a",
) -> None:
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id=submission_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id=owner,
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED


def drive_to_waiting_for_bot(engine: PrReviewEngine, prepared, clock: FakeClock):
    def step(sub_id: str, factory):
        lease, claim = claim_next(engine, prepared.run_id)
        complete_ok(engine, lease, claim, sub_id, factory(claim))
        return claim

    step("g1", lambda c: publication_success(c.effect, c.completion_token, clock.now()))
    step(
        "g2",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_B),
        ),
    )
    step(
        "g3",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature"),
        ),
    )
    binding = PullRequestBinding(
        repository=prepared.origin.repository,
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_B,
    )
    step(
        "g4",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=PrBoundOutcome(binding=binding),
        ),
    )
    step(
        "g5",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=ReviewTriggerConfirmedOutcome(
                evidence=TriggerEvidence(
                    marker=c.effect.marker,
                    comment_ref=artifact("artifacts/trigger.json", HASH_2),
                    head_sha=binding.head_sha,
                )
            ),
        ),
    )
    return binding


def make_observe_eligible(engine: PrReviewEngine, run_id: str, clock: FakeClock) -> None:
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT available_at FROM pr_review_effects WHERE status='pending' AND run_id=?",
            (run_id,),
        ).fetchone()
    assert row is not None
    clock.set(parse_utc_instant(row["available_at"]))
