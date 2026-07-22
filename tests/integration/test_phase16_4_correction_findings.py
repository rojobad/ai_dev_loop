"""Correction-turn integration tests for Phase 16.4 review findings."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    PrReviewEngineError,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import (
    FaultInjector,
    SequenceIdFactory,
    completion_submission_id,
)
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


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


def _engine(
    db_path: Path,
    clock: FakeClock,
    *,
    faults: FaultInjector | None = None,
    prefix: str = "corr",
) -> PrReviewEngine:
    return PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix=prefix),
        fault_hook=faults or FaultInjector(),
        lease_ttl=timedelta(seconds=30),
    )


def test_complete_claim_accepted_retry_after_post_commit_and_reopen(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    faults = FaultInjector()
    engine = _engine(db, clock, faults=faults)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    submission_id = completion_submission_id(
        run_id=claim.run_id,
        dispatch_id=claim.dispatch_id,
        claim_id=claim.claim_id,
        lease_generation=lease.generation,
    )
    event = publication_success(claim.effect, claim.completion_token, clock.now())
    request = EffectCompletionRequest(
        submission_id=submission_id,
        dispatch_id=claim.dispatch_id,
        claim_id=claim.claim_id,
        owner_id="worker-a",
        lease_generation=lease.generation,
        event=event,
    )

    def boom() -> None:
        raise RuntimeError("fault:post_commit")

    faults.set("post_commit", boom)
    with pytest.raises(RuntimeError, match="post_commit"):
        engine.complete_claim(request)
    faults.clear()

    # reopen: accepted work is durable; retry returns duplicate prior receipt
    engine2 = _engine(db, clock, prefix="reopen")
    status = engine2.get_status(prepared.run_id)
    assert status.run_version == 3
    assert status.active_effect_kind == "commit_patch"
    dup = engine2.complete_claim(request)
    assert dup.disposition is EventDisposition.DUPLICATE
    assert dup.resulting_run_version == 3
    assert dup.state is not None
    assert dup.state.kind == "publishing_initial"
    assert dup.effects[0].kind == "commit_patch"


def test_complete_claim_stale_retry_after_post_commit(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    faults = FaultInjector()
    engine = _engine(db, clock, faults=faults)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None

    # Expire and reacquire so old claim completion is stale.
    clock.advance(60)
    engine.acquire_lease(prepared.run_id, "worker-b")
    submission_id = "stale-complete-1"
    event = publication_success(claim.effect, claim.completion_token, clock.now())
    request = EffectCompletionRequest(
        submission_id=submission_id,
        dispatch_id=claim.dispatch_id,
        claim_id=claim.claim_id,
        owner_id="worker-a",
        lease_generation=lease.generation,
        event=event,
    )

    def boom() -> None:
        raise RuntimeError("fault:post_commit")

    faults.set("post_commit", boom)
    with pytest.raises(RuntimeError, match="post_commit"):
        engine.complete_claim(request)
    faults.clear()

    engine2 = _engine(db, clock, prefix="stale-re")
    dup = engine2.complete_claim(request)
    assert dup.disposition is EventDisposition.DUPLICATE
    assert dup.rejection_code == "stale_completion"
    assert engine2.get_status(prepared.run_id).run_version == 2


def test_abort_retry_after_clock_advance_and_reopen(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    faults = FaultInjector()
    engine = _engine(db, clock, faults=faults)
    start_run(engine, prepared)

    def boom() -> None:
        raise RuntimeError("fault:post_commit")

    faults.set("post_commit", boom)
    with pytest.raises(RuntimeError, match="post_commit"):
        engine.abort_run(
            submission_id="abort-stable",
            run_id=prepared.run_id,
            reason="user_requested_abort",
        )
    faults.clear()

    clock.advance(120)
    engine2 = _engine(db, clock, prefix="abort-re")
    dup = engine2.abort_run(
        submission_id="abort-stable",
        run_id=prepared.run_id,
        reason="user_requested_abort",
    )
    assert dup.disposition is EventDisposition.DUPLICATE
    assert dup.state is not None
    assert dup.state.kind == "aborted"
    assert engine2.get_status(prepared.run_id).state_kind == "aborted"

    with pytest.raises(PrReviewEngineError, match="different run or payload"):
        engine2.abort_run(
            submission_id="abort-stable",
            run_id=prepared.run_id,
            reason="operator_requested_abort",
        )


def test_duplicate_accepted_receipt_is_historical_not_current(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)
    start_run(engine, prepared)
    from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
    from ai_dev_loop.pr_review_v2.domain import StartRequested

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
    assert engine.get_status(prepared.run_id).run_version == 3

    engine2 = _engine(db, clock, prefix="hist")
    # Rebuild the exact original start event identity (same occurred_at as start_run).
    from tests.unit.pr_review_v2.durable_helpers import T0

    dup = engine2.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=T0),
        )
    )
    assert dup.disposition is EventDisposition.DUPLICATE
    assert dup.resulting_run_version == 2
    assert dup.state is not None
    assert dup.state.kind == "publishing_initial"
    assert dup.state.active_effect.kind == "generate_publication_text"
    assert engine2.get_status(prepared.run_id).active_effect_kind == "commit_patch"


def test_tampered_classification_fails_closed(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)
    start_run(engine, prepared)
    with engine.store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE pr_review_effects
            SET classification = 'mutating'
            WHERE run_id = ? AND status = 'pending'
            """,
            (prepared.run_id,),
        )
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    with pytest.raises(PrReviewEngineError, match="classification"):
        engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation)


def test_tampered_max_attempts_fails_closed_and_mutating_not_requeued(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)
    start_run(engine, prepared)
    # progress to commit_patch (mutating)
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
    assert claim.classification == "mutating"
    with engine.store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE pr_review_effects
            SET max_attempts = max_attempts + 1
            WHERE dispatch_id = ?
            """,
            (claim.dispatch_id,),
        )
    # expire and attempt recovery — must fail closed, not requeue write
    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "worker-b")
    with pytest.raises(PrReviewEngineError, match="max_attempts"):
        engine.recover_expired_claims(prepared.run_id, "worker-b", lease2.generation)
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "publishing_initial"
    assert status.active_effect_kind == "commit_patch"


def test_worker_completion_ids_restart_safe_and_multi_run(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)

    class PubExec:
        def execute(self, effect, token, *, now):
            return publication_success(effect, token, now)

    start_run(engine, prepared)
    worker = EffectWorker(engine, PubExec(), owner_id="worker-a")
    step = worker.run_once(prepared.run_id)
    assert step.completed is True
    dispatch_id = step.dispatch_id
    assert dispatch_id is not None

    # Reconstruct worker with fresh SequenceIdFactory-free identity; retry same claim
    # completion is a duplicate via stable submission id.
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT claim_id, claim_lease_generation FROM pr_review_effects WHERE dispatch_id = ?",
            (dispatch_id,),
        ).fetchone()
    claim_id = row["claim_id"]
    lease_generation = int(row["claim_lease_generation"])
    submission_id = completion_submission_id(
        run_id=prepared.run_id,
        dispatch_id=dispatch_id,
        claim_id=claim_id,
        lease_generation=lease_generation,
    )
    engine2 = _engine(db, clock, prefix="worker-re")
    # Cannot complete again with primary id without a live claim; duplicate lookup still works
    # via complete_claim after we synthesize a request with the durable event.
    with engine2.store.begin_read() as conn:
        event_row = conn.execute(
            "SELECT event_payload FROM pr_review_events WHERE event_id = ?",
            (submission_id,),
        ).fetchone()
    from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore as Store

    event = Store.load_event(event_row["event_payload"])
    dup = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id=submission_id,
            dispatch_id=dispatch_id,
            claim_id=claim_id,
            owner_id="worker-a",
            lease_generation=lease_generation,
            event=event,
        )
    )
    assert dup.disposition is EventDisposition.DUPLICATE

    # Second run gets a distinct completion identity space.
    prepared2 = prepared.model_copy(update={"run_id": "run-2"})
    engine.create_run(prepared2.run_id, prepared2)
    from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
    from ai_dev_loop.pr_review_v2.domain import StartRequested

    engine.apply_event(
        EventSubmission(
            submission_id="sub-start-2",
            run_id=prepared2.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    worker2 = EffectWorker(engine, PubExec(), owner_id="worker-b")
    step2 = worker2.run_once(prepared2.run_id)
    assert step2.completed is True
    assert step2.dispatch_id != dispatch_id
    # Corrected result key is distinct from primary for the same claim shape.
    primary = completion_submission_id(
        run_id="run-x",
        dispatch_id="d",
        claim_id="c",
        lease_generation=1,
        result_key="primary",
    )
    correction = completion_submission_id(
        run_id="run-x",
        dispatch_id="d",
        claim_id="c",
        lease_generation=1,
        result_key="correction-1",
    )
    assert primary != correction
    gen2 = completion_submission_id(
        run_id="run-x",
        dispatch_id="d",
        claim_id="c",
        lease_generation=2,
        result_key="primary",
    )
    assert primary != gen2


def _drive_to_waiting_for_bot(engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock):
    from ai_dev_loop.pr_review_v2.domain import (
        CommitRecordedOutcome,
        EffectSucceeded,
        PrBoundOutcome,
        PullRequestBinding,
        PushConfirmedOutcome,
        ReviewTriggerConfirmedOutcome,
        TriggerEvidence,
    )

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
    return binding


def test_completion_id_distinct_across_lease_generations_after_reclaim(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    """Same reset claim-id factory must not collide across lease generations."""
    from ai_dev_loop.pr_review_v2.domain import (
        BotStillWaitingOutcome,
        EffectSucceeded,
    )
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import parse_utc_instant

    class FixedClaimIds:
        def __init__(self) -> None:
            self._n = 0

        def new_id(self, prefix: str) -> str:
            if prefix == "claim":
                return "claim:fixed"
            self._n += 1
            return f"{prefix}:other-{self._n}"

    db = tmp_path / "engine.sqlite3"
    engine = PrReviewEngine(
        SqlitePrReviewStore(db),
        clock=clock,
        ids=FixedClaimIds(),
        fault_hook=FaultInjector(),
        lease_ttl=timedelta(seconds=30),
    )
    start_run(engine, prepared)
    _drive_to_waiting_for_bot(engine, prepared, clock)

    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT available_at FROM pr_review_effects WHERE status='pending' AND run_id=?",
            (prepared.run_id,),
        ).fetchone()
    clock.set(parse_utc_instant(row["available_at"]))

    lease1 = engine.acquire_lease(prepared.run_id, "owner-a")
    claim1 = engine.claim_next_effect(prepared.run_id, "owner-a", lease1.generation).claim
    assert claim1 is not None
    assert claim1.classification == "read_only"
    assert claim1.claim_id == "claim:fixed"
    old_submission = completion_submission_id(
        run_id=claim1.run_id,
        dispatch_id=claim1.dispatch_id,
        claim_id=claim1.claim_id,
        lease_generation=lease1.generation,
    )

    # Restart with the same fixed claim-id factory so claim IDs collide without gen.
    clock.advance(60)
    engine2 = PrReviewEngine(
        SqlitePrReviewStore(db),
        clock=clock,
        ids=FixedClaimIds(),
        fault_hook=FaultInjector(),
        lease_ttl=timedelta(seconds=30),
    )
    lease2 = engine2.acquire_lease(prepared.run_id, "owner-b")
    assert lease2.generation == lease1.generation + 1
    recovered = engine2.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    assert recovered == 1
    claim2 = engine2.claim_next_effect(prepared.run_id, "owner-b", lease2.generation).claim
    assert claim2 is not None
    assert claim2.dispatch_id == claim1.dispatch_id
    assert claim2.claim_id == claim1.claim_id
    new_submission = completion_submission_id(
        run_id=claim2.run_id,
        dispatch_id=claim2.dispatch_id,
        claim_id=claim2.claim_id,
        lease_generation=lease2.generation,
    )
    assert old_submission != new_submission

    late = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id=old_submission,
            dispatch_id=claim1.dispatch_id,
            claim_id=claim1.claim_id,
            owner_id="owner-a",
            lease_generation=lease1.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=claim1.completion_token,
                outcome=BotStillWaitingOutcome(
                    next_not_before=clock.now() + timedelta(seconds=10),
                    poll_sequence=2,
                ),
            ),
        )
    )
    assert late.disposition is EventDisposition.STALE

    ok = engine2.complete_claim(
        EffectCompletionRequest(
            submission_id=new_submission,
            dispatch_id=claim2.dispatch_id,
            claim_id=claim2.claim_id,
            owner_id="owner-b",
            lease_generation=lease2.generation,
            event=EffectSucceeded(
                occurred_at=clock.now(),
                token=claim2.completion_token,
                outcome=BotStillWaitingOutcome(
                    next_not_before=clock.now() + timedelta(seconds=10),
                    poll_sequence=2,
                ),
            ),
        )
    )
    assert ok.disposition is EventDisposition.ACCEPTED
    assert engine2.get_status(prepared.run_id).state_kind == "waiting_for_bot"


def test_stale_token_expected_version_receipt_matches_journal(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)
    start_run(engine, prepared)
    lease = engine.acquire_lease(prepared.run_id, "worker-a")
    claim = engine.claim_next_effect(prepared.run_id, "worker-a", lease.generation).claim
    assert claim is not None
    bad_token = claim.completion_token.model_copy(update={"expected_run_version": 99})
    event = publication_success(claim.effect, bad_token, clock.now())
    submission_id = "stale-token-version"
    first = engine.complete_claim(
        EffectCompletionRequest(
            submission_id=submission_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert first.disposition is EventDisposition.STALE
    assert first.expected_run_version == 99
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT expected_run_version, disposition FROM pr_review_events WHERE event_id=?",
            (submission_id,),
        ).fetchone()
    assert int(row["expected_run_version"]) == 99
    assert row["disposition"] == "stale"

    dup = engine.complete_claim(
        EffectCompletionRequest(
            submission_id=submission_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert dup.disposition is EventDisposition.DUPLICATE
    assert dup.expected_run_version == first.expected_run_version
    assert dup.observed_run_version == first.observed_run_version
    assert dup.rejection_code == first.rejection_code
    assert dup.safe_detail == first.safe_detail


def test_duplicate_receipt_validates_historical_effects(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    from tests.unit.pr_review_v2.durable_helpers import T0

    from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
    from ai_dev_loop.pr_review_v2.domain import StartRequested

    db = tmp_path / "engine.sqlite3"
    engine = _engine(db, clock)
    start_run(engine, prepared)
    with engine.store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE pr_review_effects
            SET effect_payload_sha256 = ?
            WHERE source_event_id = 'sub-start'
            """,
            ("0" * 64,),
        )
    with pytest.raises(PrReviewEngineError, match="payload hash"):
        engine.apply_event(
            EventSubmission(
                submission_id="sub-start",
                run_id=prepared.run_id,
                expected_version=1,
                event=StartRequested(occurred_at=T0),
            )
        )
