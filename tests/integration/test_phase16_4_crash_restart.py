"""Phase 16.4 crash/restart and recovery integration tests."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
    NextActionCategory,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectRetryableFailure,
    ErrorSummary,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import FaultInjector, SequenceIdFactory
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


def _engine(db_path: Path, clock: FakeClock, faults: FaultInjector | None = None) -> PrReviewEngine:
    return PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="crash"),
        fault_hook=faults or FaultInjector(),
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


@pytest.mark.parametrize(
    "checkpoint",
    ["read", "journal", "snapshot_cas", "timer_outbox_insert", "pre_commit"],
)
def test_fault_before_commit_rolls_back(
    db_path: Path,
    clock: FakeClock,
    prepared: PreparedState,
    checkpoint: str,
) -> None:
    faults = FaultInjector()
    engine = _engine(db_path, clock, faults)
    engine.create_run(prepared.run_id, prepared)

    def boom() -> None:
        raise RuntimeError(f"fault:{checkpoint}")

    faults.set(checkpoint, boom)
    with pytest.raises(RuntimeError, match=checkpoint):
        engine.apply_event(
            EventSubmission(
                submission_id="sub-start",
                run_id=prepared.run_id,
                expected_version=1,
                event=StartRequested(occurred_at=clock.now()),
            )
        )
    faults.clear()
    status = engine.get_status(prepared.run_id)
    assert status.run_version == 1
    assert status.state_kind == "prepared"

    # same submission id recovers after unknown outcome
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED


def test_post_commit_fault_then_duplicate_recovery(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    faults = FaultInjector()
    engine = _engine(db_path, clock, faults)
    engine.create_run(prepared.run_id, prepared)

    def boom() -> None:
        raise RuntimeError("fault:post_commit")

    faults.set("post_commit", boom)
    with pytest.raises(RuntimeError, match="post_commit"):
        engine.apply_event(
            EventSubmission(
                submission_id="sub-start",
                run_id=prepared.run_id,
                expected_version=1,
                event=StartRequested(occurred_at=clock.now()),
            )
        )
    faults.clear()
    # Commit already happened; reopen and dedupe
    engine2 = _engine(db_path, clock)
    status = engine2.get_status(prepared.run_id)
    assert status.run_version == 2
    dup = engine2.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert dup.disposition is EventDisposition.DUPLICATE


def test_states_survive_reopen(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = _engine(db_path, clock)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="c1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    next_at = clock.now() + timedelta(seconds=45)
    engine.complete_claim(
        EffectCompletionRequest(
            submission_id="c-retry",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_503, safe_summary="unavailable"),
                failed_attempt=claim.attempt,
                next_attempt_at=next_at,
            ),
        )
    )
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "waiting_retry"
    assert status.next_action is NextActionCategory.WAIT_FOR_RETRY

    engine2 = _engine(db_path, clock)
    status2 = engine2.get_status(prepared.run_id)
    assert status2.state_kind == "waiting_retry"
    assert status2.run_version == status.run_version
    assert status2.next_eligible_at == next_at


def test_observation_not_before_delays_claim_without_retry_budget(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    """Drive to waiting_for_bot and ensure not_before gates eligibility."""

    # Use pure domain to build waiting_for_bot, then persist via create is not allowed.
    # Instead drive through engine completions.
    engine = _engine(db_path, clock)
    start_run(engine, prepared)

    def complete_once(submission_id: str, event_factory):
        lease = engine.acquire_lease(prepared.run_id, "worker-a")
        claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
        assert claim is not None
        return engine.complete_claim(
            EffectCompletionRequest(
                submission_id=submission_id,
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                owner_id="worker-a",
                lease_generation=lease.generation,
                event=event_factory(claim),
            )
        )

    complete_once("g1", lambda c: publication_success(c.effect, c.completion_token, clock.now()))

    from ai_dev_loop.pr_review_v2.domain import (
        CommitRecordedOutcome,
        EffectSucceeded,
        PrBoundOutcome,
        PullRequestBinding,
        PushConfirmedOutcome,
    )

    complete_once(
        "g2",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=CommitRecordedOutcome(
                commit_sha="b" * 40, new_head_sha="b" * 40, expected_remote_sha_before_push=None
            ),
        ),
    )
    complete_once(
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
    complete_once(
        "g4",
        lambda c: EffectSucceeded(
            occurred_at=clock.now(),
            token=c.completion_token,
            outcome=PrBoundOutcome(binding=binding),
        ),
    )
    # now request_bot_review
    from ai_dev_loop.pr_review_v2.domain import ReviewTriggerConfirmedOutcome, TriggerEvidence

    complete_once(
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
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "waiting_for_bot"
    assert status.effect_attempt == 1
    # observe effect may have future not_before
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    # If available_at is in the future, claim returns none
    live = engine.store
    with live.begin_read() as conn:
        row = conn.execute(
            "SELECT available_at, attempt FROM pr_review_effects WHERE run_id=? AND status='pending'",
            (prepared.run_id,),
        ).fetchone()
    assert row is not None
    attempt_before = int(row["attempt"])
    # force available_at into the future by advancing claim check with early clock
    # If already eligible, advance not needed; set clock earlier than available_at
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import parse_utc_instant

    available = parse_utc_instant(row["available_at"])
    clock.set(available - timedelta(seconds=1))
    result = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation)
    assert result.claim is None
    status = engine.get_status(prepared.run_id)
    assert status.effect_attempt == attempt_before
    assert status.next_action is NextActionCategory.WAIT_FOR_ELIGIBILITY

    clock.set(available)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    result = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation)
    assert result.claim is not None
    assert result.claim.attempt == attempt_before
