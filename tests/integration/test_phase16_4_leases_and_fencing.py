"""Phase 16.4 lease fencing, races, and expired-claim recovery tests."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    EffectCompletionRequest,
    EventDisposition,
    NextActionCategory,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectSucceeded,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "engine.sqlite3"


def _engine(db_path: Path, clock: FakeClock, *, prefix: str = "lease") -> PrReviewEngine:
    return PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix=prefix),
        lease_ttl=timedelta(seconds=30),
    )


@pytest.fixture
def prepared() -> PreparedState:
    return PreparedState(
        run_id="run-1",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256=HASH_1),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256=HASH_2,
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=FakeClock().now(),
    )


def test_two_workers_claim_race(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine_a = _engine(db_path, clock, prefix="a")
    start_run(engine_a, prepared)
    barrier = threading.Barrier(2)
    results: list[str | None] = [None, None]
    lease = engine_a.acquire_lease(prepared.run_id, "owner-a")

    def claim_racer(idx: int) -> None:
        eng = _engine(db_path, clock, prefix=f"c{idx}")
        barrier.wait(timeout=5)
        claim = eng.claim_next_effect(prepared.run_id, "owner-a", lease.generation)
        results[idx] = claim.claim.claim_id if claim.claim else None

    t1 = threading.Thread(target=claim_racer, args=(0,))
    t2 = threading.Thread(target=claim_racer, args=(1,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    winners = [r for r in results if r is not None]
    assert len(winners) == 1


def test_reacquired_lease_fences_old_completion(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = _engine(db_path, clock)
    start_run(engine, prepared)
    lease1 = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease1.generation).claim
    assert claim is not None

    # expire and reacquire
    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    assert lease2.generation == lease1.generation + 1

    hb = engine.heartbeat_lease(prepared.run_id, "owner-a", lease1.generation)
    assert hb.accepted is False
    rel = engine.release_lease(prepared.run_id, "owner-a", lease1.generation)
    assert rel.accepted is False

    late = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="late",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease1.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    assert late.disposition is EventDisposition.STALE


def test_expired_local_claim_pauses(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = _engine(db_path, clock)
    start_run(engine, prepared)
    lease1 = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease1.generation).claim
    assert claim is not None
    assert claim.classification == "local"

    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    recovered = engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    assert recovered == 1
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "paused"
    assert status.next_action is NextActionCategory.INSPECT
    assert status.effect_status in {DispatchStatus.BLOCKED, None}


def test_expired_mutating_claim_reconciles(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = _engine(db_path, clock)
    start_run(engine, prepared)
    # complete generate -> commit (mutating)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="g1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    assert claim.classification == "mutating"

    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    recovered = engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    assert recovered == 1
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "reconciling_write"
    assert status.ambiguous_write_pending is True
    assert status.next_action is NextActionCategory.RECONCILE
    assert status.active_effect_kind == "reconcile_write"


def test_expired_readonly_requeues_same_dispatch(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    # Drive to observe_bot_review then expire claim
    from ai_dev_loop.pr_review_v2.domain import (
        CommitRecordedOutcome,
        EffectSucceeded,
        PrBoundOutcome,
        PullRequestBinding,
        PushConfirmedOutcome,
        ReviewTriggerConfirmedOutcome,
        TriggerEvidence,
    )

    engine = _engine(db_path, clock)
    start_run(engine, prepared)

    def step(sub_id: str, factory):
        lease = engine.acquire_lease(prepared.run_id, "owner-a")
        claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
        assert claim is not None
        engine.complete_claim(
            EffectCompletionRequest(
                submission_id=sub_id,
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="owner-a",
                lease_generation=lease.generation,
                event=factory(claim),
            )
        )
        return claim

    step("g1", lambda c: publication_success(c.effect, c.completion_token, clock.now()))
    step(
        "g2",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=CommitRecordedOutcome(
                commit_sha="b" * 40, new_head_sha="b" * 40, expected_remote_sha_before_push=None
            ),
        ),
    )
    step(
        "g3",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=PushConfirmedOutcome(commit_sha="b" * 40, remote_ref="feature"),
        ),
    )
    binding = PullRequestBinding(
        repository=prepared.origin.repository,
        pr_number=7,
        head_branch="feature",
        base_branch="main",
        head_sha="b" * 40,
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
                    comment_ref=ArtifactRef(relative_path="artifacts/trigger.json", sha256=HASH_2),
                    head_sha=binding.head_sha,
                )
            ),
        ),
    )
    # claim observe
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    # ensure eligible
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT available_at FROM pr_review_effects WHERE status='pending' AND run_id=?",
            (prepared.run_id,),
        ).fetchone()
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import parse_utc_instant

    clock.set(parse_utc_instant(row["available_at"]))
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    assert claim.classification == "read_only"
    dispatch_id = claim.dispatch_id
    attempt = claim.attempt

    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    recovered = engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    assert recovered == 1
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "waiting_for_bot"
    assert status.effect_status is DispatchStatus.PENDING
    assert status.effect_attempt == attempt
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT dispatch_id, attempt, status FROM pr_review_effects WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert int(row["attempt"]) == attempt


def test_stale_token_fences(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = _engine(db_path, clock)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "owner-a")
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    bad_token = claim.completion_token.model_copy(update={"bound_head_sha": "c" * 40})
    event = EffectSucceeded(
        occurred_at=clock.now(),
        token=bad_token,
        outcome=publication_success(claim.effect, claim.completion_token, clock.now()).outcome,
    )
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="bad-sha",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.STALE
    assert engine.get_status(prepared.run_id).run_version == 2


def test_abort_completion_race_both_orders(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    # completion then abort
    engine = _engine(db_path, clock, prefix="ord1")
    start_run(engine, prepared)
    lease, claim = engine.acquire_lease(prepared.run_id, "owner-a"), None
    claim = engine.claim_next_effect(prepared.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    done = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="done-1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    assert done.disposition is EventDisposition.ACCEPTED
    abort = engine.abort_run(submission_id="abort-after", run_id=prepared.run_id)
    assert abort.disposition is EventDisposition.ACCEPTED
    assert engine.get_status(prepared.run_id).state_kind == "aborted"

    # abort then completion
    prepared2 = prepared.model_copy(update={"run_id": "run-2"})
    engine2 = _engine(db_path, clock, prefix="ord2")
    # need fresh DB for second run in same file — use sibling path
    engine2 = _engine(db_path.with_name("engine2.sqlite3"), clock, prefix="ord2")
    start_run(engine2, prepared2)
    lease = engine2.acquire_lease(prepared2.run_id, "owner-a")
    claim = engine2.claim_next_effect(prepared2.run_id, "owner-a", lease.generation).claim
    assert claim is not None
    abort = engine2.abort_run(submission_id="abort-first", run_id=prepared2.run_id)
    assert abort.disposition is EventDisposition.ACCEPTED
    late = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id="late-2",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    assert late.disposition is EventDisposition.STALE
