"""Phase 16.4 acceptance matrix: reopen, fences, races, resume, reconciling, workflows."""

from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    complete_ok,
    drive_to_waiting_for_bot,
    fingerprint,
    make_engine,
    make_observe_eligible,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success, start_run
from tests.unit.pr_review_v2.helpers import HASH_2, SHA_A, artifact, reply_adjudication

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
    NextActionCategory,
    PrReviewEngineError,
)
from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationRecordedOutcome,
    BotStillWaitingOutcome,
    EffectRetryableFailure,
    EffectSucceeded,
    EligibleThreadsObservedOutcome,
    ErrorSummary,
    FailureReasonKind,
    FatalFailureDetected,
    FrozenThreadSet,
    PreparedState,
    RepositoryIdentity,
    ResumeRequested,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    VerifiedNoFindingsEvidence,
    VerifiedNoFindingsOutcome,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import FaultInjector
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

SHA_C = "c" * 40


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "engine.sqlite3"


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
            accepted_patch=artifact("artifacts/accepted.patch"),
            execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=FakeClock().now(),
    )


def test_reopen_pending_claimed_retry_uncertain_paused_completed_failed_aborted(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    def reopen(path: Path, prefix: str):
        return make_engine(path, clock, prefix=prefix)

    path = tmp_path / "pending.sqlite3"
    eng = make_engine(path, clock, prefix="p")
    start_run(eng, prepared)
    status = reopen(path, "p2").get_status(prepared.run_id)
    assert status.effect_status is DispatchStatus.PENDING
    assert status.next_action is NextActionCategory.EXECUTE_EFFECT
    assert status.state_kind == "publishing_initial"

    path = tmp_path / "claimed.sqlite3"
    eng = make_engine(path, clock, prefix="c")
    start_run(eng, prepared)
    claim_next(eng, prepared.run_id)
    status = reopen(path, "c2").get_status(prepared.run_id)
    assert status.effect_status is DispatchStatus.CLAIMED
    assert status.next_action is NextActionCategory.EXECUTE_EFFECT

    path = tmp_path / "retry.sqlite3"
    eng = make_engine(path, clock, prefix="r")
    start_run(eng, prepared)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "retry-1",
        EffectRetryableFailure(
            occurred_at=clock.now(),
            token=claim.completion_token,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
            failed_attempt=claim.attempt,
            next_attempt_at=clock.now() + timedelta(seconds=30),
        ),
    )
    status = reopen(path, "r2").get_status(prepared.run_id)
    assert status.state_kind == "waiting_retry"
    assert status.next_action is NextActionCategory.WAIT_FOR_RETRY

    path = tmp_path / "uncertain.sqlite3"
    eng = make_engine(path, clock, prefix="u")
    start_run(eng, prepared)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "pub-u",
        publication_success(claim.effect, claim.completion_token, clock.now()),
    )
    claim_next(eng, prepared.run_id)
    clock.advance(60)
    lease2 = eng.acquire_lease(prepared.run_id, "owner-b")
    assert eng.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation) == 1
    status = reopen(path, "u2").get_status(prepared.run_id)
    assert status.state_kind == "reconciling_write"
    assert status.next_action is NextActionCategory.RECONCILE
    assert status.ambiguous_write_pending is True

    path = tmp_path / "paused.sqlite3"
    eng = make_engine(path, clock, prefix="z")
    start_run(eng, prepared)
    claim_next(eng, prepared.run_id)
    clock.advance(60)
    lease2 = eng.acquire_lease(prepared.run_id, "owner-b")
    assert eng.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation) == 1
    status = reopen(path, "z2").get_status(prepared.run_id)
    assert status.state_kind == "paused"
    assert status.next_action is NextActionCategory.INSPECT

    path = tmp_path / "completed.sqlite3"
    eng = make_engine(path, clock, prefix="done")
    start_run(eng, prepared)
    binding = drive_to_waiting_for_bot(eng, prepared, clock)
    make_observe_eligible(eng, prepared.run_id, clock)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "done-obs",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=VerifiedNoFindingsOutcome(
                evidence=VerifiedNoFindingsEvidence(
                    head_sha=binding.head_sha,
                    observation_ref=artifact("obs.json"),
                    verified_at=clock.now(),
                )
            ),
        ),
    )
    status = reopen(path, "done2").get_status(prepared.run_id)
    assert status.state_kind == "completed"
    assert status.next_action is NextActionCategory.TERMINAL_COMPLETED

    path = tmp_path / "failed.sqlite3"
    eng = make_engine(path, clock, prefix="fail")
    eng.create_run(prepared.run_id, prepared)
    eng.apply_fatal_failure(
        submission_id="fatal-1",
        run_id=prepared.run_id,
        expected_version=1,
        event=FatalFailureDetected(
            occurred_at=clock.now(),
            reason=FailureReasonKind.INTERNAL_CORRUPTION,
            safe_summary="corrupt",
        ),
    )
    status = reopen(path, "fail2").get_status(prepared.run_id)
    assert status.state_kind == "failed"
    assert status.next_action is NextActionCategory.TERMINAL_FAILED

    path = tmp_path / "aborted.sqlite3"
    eng = make_engine(path, clock, prefix="abort")
    start_run(eng, prepared)
    eng.abort_run(submission_id="abort-1", run_id=prepared.run_id)
    status = reopen(path, "abort2").get_status(prepared.run_id)
    assert status.state_kind == "aborted"
    assert status.next_action is NextActionCategory.TERMINAL_ABORTED


def _assert_stale_unchanged(engine, prepared, *, build_request, detail_substr: str) -> None:
    start_run(engine, prepared)
    lease, claim = claim_next(engine, prepared.run_id)
    before = fingerprint(engine, prepared.run_id)
    request = build_request(lease, claim)
    receipt = engine.complete_claim(request)
    assert receipt.disposition is EventDisposition.STALE
    assert receipt.rejection_code == "stale_completion"
    assert detail_substr in (receipt.safe_detail or "")
    assert fingerprint(engine, prepared.run_id) == before
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT disposition, rejection_code, safe_detail FROM pr_review_events WHERE event_id=?",
            (request.submission_id,),
        ).fetchone()
    assert row["disposition"] == "stale"
    assert row["rejection_code"] == "stale_completion"
    assert detail_substr in (row["safe_detail"] or "")


def test_fence_stale_run_version(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fv")

    def build(lease, claim):
        bad = claim.completion_token.model_copy(update={"expected_run_version": 99})
        return EffectCompletionRequest(
            submission_id="fence-version",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, bad, clock.now()),
        )

    _assert_stale_unchanged(
        engine, prepared, build_request=build, detail_substr="expected_run_version"
    )


def test_fence_stale_dispatch_id(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fd")
    other = prepared.model_copy(update={"run_id": "run-b"})
    start_run(engine, prepared)
    engine.create_run(other.run_id, other)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start-b",
            run_id=other.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    lease_a, claim_a = claim_next(engine, prepared.run_id)
    claim_next(engine, other.run_id)
    before_a = fingerprint(engine, prepared.run_id)
    before_b = fingerprint(engine, other.run_id)
    with engine.store.begin_read() as conn:
        other_dispatch = conn.execute(
            "SELECT dispatch_id FROM pr_review_effects WHERE run_id=? AND status='claimed'",
            (other.run_id,),
        ).fetchone()["dispatch_id"]
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="fence-dispatch",
            dispatch_id=other_dispatch,
            claim_id=claim_a.claim_id,
            owner_id="owner-a",
            lease_generation=lease_a.generation,
            event=publication_success(claim_a.effect, claim_a.completion_token, clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.STALE
    assert "claim_id" in (receipt.safe_detail or "")
    assert fingerprint(engine, prepared.run_id) == before_a
    after_b = fingerprint(engine, other.run_id)
    assert after_b[0] == before_b[0]
    assert after_b[1] == before_b[1]
    assert after_b[2] == before_b[2]


def test_fence_stale_claim_id(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fc")

    def build(lease, claim):
        return EffectCompletionRequest(
            submission_id="fence-claim",
            dispatch_id=claim.dispatch_id,
            claim_id="claim:not-this-one",
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )

    _assert_stale_unchanged(engine, prepared, build_request=build, detail_substr="claim_id")


def test_fence_stale_lease_generation(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="fl")
    start_run(engine, prepared)
    lease1, claim = claim_next(engine, prepared.run_id)
    before = fingerprint(engine, prepared.run_id)
    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    assert lease2.generation == lease1.generation + 1
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="fence-lease-gen",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease1.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.STALE
    assert "lease" in (receipt.safe_detail or "")
    after = fingerprint(engine, prepared.run_id)
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2]


def test_fence_stale_effect_id(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fe")

    def build(lease, claim):
        bad = claim.completion_token.model_copy(update={"effect_id": "effect:wrong"})
        return EffectCompletionRequest(
            submission_id="fence-effect",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, bad, clock.now()),
        )

    _assert_stale_unchanged(engine, prepared, build_request=build, detail_substr="effect_id")


def test_fence_stale_attempt(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fa")

    def build(lease, claim):
        return EffectCompletionRequest(
            submission_id="fence-attempt",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
                failed_attempt=claim.attempt + 1,
                next_attempt_at=clock.now() + timedelta(seconds=30),
            ),
        )

    _assert_stale_unchanged(engine, prepared, build_request=build, detail_substr="failed_attempt")


def test_fence_stale_cycle(db_path: Path, clock: FakeClock, prepared: PreparedState) -> None:
    engine = make_engine(db_path, clock, prefix="fy")

    def build(lease, claim):
        bad = claim.completion_token.model_copy(
            update={"cycle_number": claim.effect.cycle_number + 1}
        )
        return EffectCompletionRequest(
            submission_id="fence-cycle",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, bad, clock.now()),
        )

    _assert_stale_unchanged(engine, prepared, build_request=build, detail_substr="cycle")


def test_fence_stale_bound_head_sha(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="fh")

    def build(lease, claim):
        bad = claim.completion_token.model_copy(update={"bound_head_sha": SHA_C})
        return EffectCompletionRequest(
            submission_id="fence-sha",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, bad, clock.now()),
        )

    _assert_stale_unchanged(engine, prepared, build_request=build, detail_substr="head sha")


def test_abort_completion_race_completion_first_deterministic(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    """Completion holds the write txn; abort contends and then applies to the new state."""
    db = tmp_path / "race-complete-first.sqlite3"
    run = prepared.model_copy(update={"run_id": "run-race-cf"})
    setup = make_engine(db, clock, prefix="setup-cf")
    start_run(setup, run)
    lease, claim = claim_next(setup, run.run_id)

    holds_txn = threading.Event()
    release_txn = threading.Event()
    competitor_ready = threading.Event()
    results: dict[str, EventDisposition] = {}
    errors: list[BaseException] = []

    first_faults = FaultInjector()

    def hold_write_txn() -> None:
        holds_txn.set()
        if not release_txn.wait(timeout=5):
            raise TimeoutError("completion txn was not released")
        first_faults.clear("pre_commit")

    first_faults.set("pre_commit", hold_write_txn)

    def do_complete() -> None:
        try:
            eng = make_engine(
                db, clock, prefix="comp-cf", fault_hook=first_faults, busy_timeout_ms=15000
            )
            results["complete"] = eng.complete_claim(
                EffectCompletionRequest(
                    submission_id="race-complete",
                    dispatch_id=claim.dispatch_id,
                    claim_id=claim.claim_id,
                    owner_id="owner-a",
                    lease_generation=lease.generation,
                    event=publication_success(claim.effect, claim.completion_token, clock.now()),
                )
            ).disposition
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_abort() -> None:
        try:
            eng = make_engine(db, clock, prefix="abort-cf", busy_timeout_ms=15000)
            competitor_ready.set()
            results["abort"] = eng.abort_run(
                submission_id="race-abort", run_id=run.run_id
            ).disposition
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_complete = threading.Thread(target=do_complete, name="complete-first")
    t_abort = threading.Thread(target=do_abort, name="abort-second")
    t_complete.start()
    assert holds_txn.wait(timeout=5), "completion never acquired write transaction"
    t_abort.start()
    assert competitor_ready.wait(timeout=5), "abort competitor never became ready"
    release_txn.set()
    t_complete.join(timeout=10)
    t_abort.join(timeout=10)
    assert not t_complete.is_alive() and not t_abort.is_alive()
    assert not errors, errors
    assert results["complete"] is EventDisposition.ACCEPTED
    assert results["abort"] is EventDisposition.ACCEPTED
    _assert_race_terminal_cleanup(db, clock, run.run_id, prefix="ck-cf")


def test_abort_completion_race_abort_first_deterministic(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    """Abort holds the write txn; late completion is fenced stale with no partial cleanup."""
    db = tmp_path / "race-abort-first.sqlite3"
    run = prepared.model_copy(update={"run_id": "run-race-af"})
    setup = make_engine(db, clock, prefix="setup-af")
    start_run(setup, run)
    lease, claim = claim_next(setup, run.run_id)

    holds_txn = threading.Event()
    release_txn = threading.Event()
    competitor_ready = threading.Event()
    results: dict[str, EventDisposition] = {}
    errors: list[BaseException] = []

    first_faults = FaultInjector()

    def hold_write_txn() -> None:
        holds_txn.set()
        if not release_txn.wait(timeout=5):
            raise TimeoutError("abort txn was not released")
        first_faults.clear("pre_commit")

    first_faults.set("pre_commit", hold_write_txn)

    def do_abort() -> None:
        try:
            eng = make_engine(
                db, clock, prefix="abort-af", fault_hook=first_faults, busy_timeout_ms=15000
            )
            results["abort"] = eng.abort_run(
                submission_id="race-abort", run_id=run.run_id
            ).disposition
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def do_complete() -> None:
        try:
            eng = make_engine(db, clock, prefix="comp-af", busy_timeout_ms=15000)
            competitor_ready.set()
            results["complete"] = eng.complete_claim(
                EffectCompletionRequest(
                    submission_id="race-complete",
                    dispatch_id=claim.dispatch_id,
                    claim_id=claim.claim_id,
                    owner_id="owner-a",
                    lease_generation=lease.generation,
                    event=publication_success(claim.effect, claim.completion_token, clock.now()),
                )
            ).disposition
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t_abort = threading.Thread(target=do_abort, name="abort-first")
    t_complete = threading.Thread(target=do_complete, name="complete-second")
    t_abort.start()
    assert holds_txn.wait(timeout=5), "abort never acquired write transaction"
    t_complete.start()
    assert competitor_ready.wait(timeout=5), "completion competitor never became ready"
    release_txn.set()
    t_abort.join(timeout=10)
    t_complete.join(timeout=10)
    assert not t_abort.is_alive() and not t_complete.is_alive()
    assert not errors, errors
    assert results["abort"] is EventDisposition.ACCEPTED
    assert results["complete"] is EventDisposition.STALE
    _assert_race_terminal_cleanup(db, clock, run.run_id, prefix="ck-af")


def _assert_race_terminal_cleanup(db: Path, clock: FakeClock, run_id: str, *, prefix: str) -> None:
    check = make_engine(db, clock, prefix=prefix)
    status = check.get_status(run_id)
    assert status.state_kind == "aborted"
    assert status.next_action is NextActionCategory.TERMINAL_ABORTED
    with check.store.begin_read() as conn:
        claimed = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_effects WHERE run_id=? AND status='claimed'",
            (run_id,),
        ).fetchone()["n"]
        pending_timers = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_timers WHERE run_id=? AND status='pending'",
            (run_id,),
        ).fetchone()["n"]
        lease_row = conn.execute(
            "SELECT status FROM pr_review_worker_leases WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert int(claimed) == 0
    assert int(pending_timers) == 0
    assert lease_row["status"] in {"inactive", "aborted"}


def test_resume_batch_attempt_1_new_dispatch_preserves_identity(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="resume")
    start_run(engine, prepared)
    lease, claim = claim_next(engine, prepared.run_id)
    original_effect_id = claim.effect.effect_id
    original_idem = claim.effect.idempotency_key
    historical_attempt1_dispatch = claim.dispatch_id

    for attempt in range(1, 7):
        if attempt > 1:
            status = engine.get_status(prepared.run_id)
            assert status.next_eligible_at is not None
            clock.set(status.next_eligible_at)
            fired = engine.fire_due_timers()
            assert len(fired) == 1
            assert fired[0].disposition is EventDisposition.ACCEPTED
            lease, claim = claim_next(engine, prepared.run_id)
            assert claim.effect.attempt == attempt
            assert claim.effect.effect_id == original_effect_id
        next_at = clock.now() + timedelta(seconds=30)
        complete_ok(
            engine,
            lease,
            claim,
            f"fail-{attempt}",
            EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.HTTP_429, safe_summary="rate"),
                failed_attempt=attempt,
                next_attempt_at=next_at,
            ),
        )
        status = engine.get_status(prepared.run_id)
        if attempt < 6:
            assert status.state_kind == "waiting_retry"
        else:
            assert status.state_kind == "paused"
            assert status.next_action is NextActionCategory.RESUME

    version = engine.get_status(prepared.run_id).run_version
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-resume",
            run_id=prepared.run_id,
            expected_version=version,
            event=ResumeRequested(occurred_at=clock.now()),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "publishing_initial"
    assert status.effect_attempt == 1
    assert status.effect_status is DispatchStatus.PENDING

    with engine.store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT dispatch_id, effect_id, idempotency_key, attempt, status
            FROM pr_review_effects
            WHERE run_id=? AND effect_id=?
            ORDER BY created_at, dispatch_id
            """,
            (prepared.run_id, original_effect_id),
        ).fetchall()
    pending = [r for r in rows if r["status"] == "pending"]
    assert len(pending) == 1
    resumed = pending[0]
    assert resumed["dispatch_id"] != historical_attempt1_dispatch
    assert resumed["effect_id"] == original_effect_id
    assert resumed["idempotency_key"] == original_idem
    assert int(resumed["attempt"]) == 1
    historical = next(r for r in rows if r["dispatch_id"] == historical_attempt1_dispatch)
    assert int(historical["attempt"]) == 1
    assert historical["status"] != "pending"


def test_expired_reconciling_claim_requeues_same_dispatch_without_write(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="recon")
    start_run(engine, prepared)
    lease, claim = claim_next(engine, prepared.run_id)
    complete_ok(
        engine,
        lease,
        claim,
        "pub",
        publication_success(claim.effect, claim.completion_token, clock.now()),
    )
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.classification == "mutating"
    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    assert engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation) == 1
    assert engine.get_status(prepared.run_id).state_kind == "reconciling_write"

    lease3, claim3 = claim_next(engine, prepared.run_id, owner="owner-b")
    assert claim3.classification == "reconciling"
    dispatch_id = claim3.dispatch_id
    attempt = claim3.attempt
    effect_id = claim3.effect.effect_id
    before_version = engine.get_status(prepared.run_id).run_version

    clock.advance(60)
    lease4 = engine.acquire_lease(prepared.run_id, "owner-c")
    assert engine.recover_expired_claims(prepared.run_id, "owner-c", lease4.generation) == 1

    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "reconciling_write"
    assert status.run_version == before_version
    assert status.effect_status is DispatchStatus.PENDING
    assert status.effect_attempt == attempt
    assert status.active_effect_kind == "reconcile_write"
    with engine.store.begin_read() as conn:
        row = conn.execute(
            "SELECT attempt, status, effect_id FROM pr_review_effects WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        mutating_pending = conn.execute(
            """
            SELECT COUNT(*) AS n FROM pr_review_effects
            WHERE run_id=? AND classification='mutating' AND status='pending'
            """,
            (prepared.run_id,),
        ).fetchone()["n"]
    assert row["status"] == "pending"
    assert int(row["attempt"]) == attempt
    assert row["effect_id"] == effect_id
    assert int(mutating_pending) == 0
    _ = lease3


def test_status_reconstructible_and_read_only_after_restart(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="st20")
    start_run(engine, prepared)
    status = engine.get_status(prepared.run_id)
    before = fingerprint(engine, prepared.run_id)
    for i in range(3):
        eng = make_engine(db_path, clock, prefix=f"st20-r{i}")
        again = eng.get_status(prepared.run_id)
        assert again.state_kind == status.state_kind
        assert again.next_action == status.next_action
        assert again.run_version == status.run_version
        assert fingerprint(eng, prepared.run_id) == before


def test_simulated_workflows_reach_terminal_and_waiting_outcomes(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    outcomes: dict[str, NextActionCategory] = {}

    eng = make_engine(tmp_path / "wf-completed.sqlite3", clock, prefix="wfc")
    start_run(eng, prepared)
    binding = drive_to_waiting_for_bot(eng, prepared, clock)
    make_observe_eligible(eng, prepared.run_id, clock)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "obs-done",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=VerifiedNoFindingsOutcome(
                evidence=VerifiedNoFindingsEvidence(
                    head_sha=binding.head_sha,
                    observation_ref=artifact("obs.json"),
                    verified_at=clock.now(),
                )
            ),
        ),
    )
    outcomes["completed"] = eng.get_status(prepared.run_id).next_action

    eng = make_engine(tmp_path / "wf-bot.sqlite3", clock, prefix="wfb")
    start_run(eng, prepared)
    drive_to_waiting_for_bot(eng, prepared, clock)
    make_observe_eligible(eng, prepared.run_id, clock)
    lease, claim = claim_next(eng, prepared.run_id)
    poll = claim.effect.poll_sequence
    complete_ok(
        eng,
        lease,
        claim,
        "obs-wait",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=BotStillWaitingOutcome(
                next_not_before=clock.now() + timedelta(seconds=45),
                poll_sequence=poll + 1,
            ),
        ),
    )
    status = eng.get_status(prepared.run_id)
    assert status.state_kind == "waiting_for_bot"
    outcomes["waiting_for_bot"] = status.next_action

    eng = make_engine(tmp_path / "wf-retry.sqlite3", clock, prefix="wfr")
    start_run(eng, prepared)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "retry",
        EffectRetryableFailure(
            occurred_at=clock.now(),
            token=claim.completion_token,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
            failed_attempt=1,
            next_attempt_at=clock.now() + timedelta(seconds=20),
        ),
    )
    outcomes["waiting_retry"] = eng.get_status(prepared.run_id).next_action

    eng = make_engine(tmp_path / "wf-user.sqlite3", clock, prefix="wfu")
    start_run(eng, prepared)
    binding = drive_to_waiting_for_bot(eng, prepared, clock)
    make_observe_eligible(eng, prepared.run_id, clock)
    lease, claim = claim_next(eng, prepared.run_id)
    with eng.store.begin_read() as conn:
        snap, _, _ = eng.store.load_validated_snapshot(conn, prepared.run_id)
    assert snap.trigger_evidence is not None
    frozen = FrozenThreadSet(
        thread_ids=("t1", "t2"),
        snapshot_ref=artifact("artifacts/threads.json"),
        head_sha=binding.head_sha,
        cycle_number=1,
        trigger_marker=snap.trigger_evidence.marker,
    )
    complete_ok(
        eng,
        lease,
        claim,
        "obs-threads",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=EligibleThreadsObservedOutcome(frozen=frozen),
        ),
    )
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "adj-reply",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=AdjudicationRecordedOutcome(evidence=reply_adjudication(frozen)),
        ),
    )
    status = eng.get_status(prepared.run_id)
    assert status.state_kind == "waiting_for_user"
    outcomes["waiting_for_user"] = status.next_action

    eng = make_engine(tmp_path / "wf-paused.sqlite3", clock, prefix="wfp")
    start_run(eng, prepared)
    claim_next(eng, prepared.run_id)
    clock.advance(60)
    lease2 = eng.acquire_lease(prepared.run_id, "owner-b")
    eng.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    outcomes["paused"] = eng.get_status(prepared.run_id).next_action

    eng = make_engine(tmp_path / "wf-recon.sqlite3", clock, prefix="wfq")
    start_run(eng, prepared)
    lease, claim = claim_next(eng, prepared.run_id)
    complete_ok(
        eng,
        lease,
        claim,
        "p",
        publication_success(claim.effect, claim.completion_token, clock.now()),
    )
    claim_next(eng, prepared.run_id)
    clock.advance(60)
    lease2 = eng.acquire_lease(prepared.run_id, "owner-b")
    eng.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    outcomes["reconciling_write"] = eng.get_status(prepared.run_id).next_action

    eng = make_engine(tmp_path / "wf-failed.sqlite3", clock, prefix="wff")
    eng.create_run(prepared.run_id, prepared)
    eng.apply_fatal_failure(
        submission_id="f",
        run_id=prepared.run_id,
        expected_version=1,
        event=FatalFailureDetected(
            occurred_at=clock.now(),
            reason=FailureReasonKind.INVARIANT_VIOLATION,
            safe_summary="fatal",
        ),
    )
    outcomes["failed"] = eng.get_status(prepared.run_id).next_action

    eng = make_engine(tmp_path / "wf-aborted.sqlite3", clock, prefix="wfa")
    start_run(eng, prepared)
    eng.abort_run(submission_id="ab", run_id=prepared.run_id)
    outcomes["aborted"] = eng.get_status(prepared.run_id).next_action

    assert outcomes["completed"] is NextActionCategory.TERMINAL_COMPLETED
    assert outcomes["failed"] is NextActionCategory.TERMINAL_FAILED
    assert outcomes["aborted"] is NextActionCategory.TERMINAL_ABORTED
    assert outcomes["waiting_retry"] is NextActionCategory.WAIT_FOR_RETRY
    assert outcomes["waiting_for_user"] is NextActionCategory.WAIT_FOR_USER
    assert outcomes["paused"] is NextActionCategory.INSPECT
    assert outcomes["reconciling_write"] is NextActionCategory.RECONCILE
    assert outcomes["waiting_for_bot"] in {
        NextActionCategory.EXECUTE_EFFECT,
        NextActionCategory.WAIT_FOR_ELIGIBILITY,
    }


def test_create_run_rejects_non_prepared_state(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    engine = make_engine(db_path, clock, prefix="prep-only")
    start_run(engine, prepared)
    with engine.store.begin_read() as conn:
        state, _, _ = engine.store.load_validated_snapshot(conn, prepared.run_id)
    assert state.kind == "publishing_initial"
    with pytest.raises(PrReviewEngineError, match="PreparedState"):
        engine.create_run("run-other", state)  # type: ignore[arg-type]


def test_worker_simulated_one_step_no_external_side_effects(
    db_path: Path, clock: FakeClock, prepared: PreparedState
) -> None:
    class _Exec:
        def __init__(self) -> None:
            self.calls: list = []

        def execute(self, effect, token, *, now):
            self.calls.append(effect)
            return publication_success(effect, token, now)

    eng = make_engine(db_path, clock, prefix="worker")
    start_run(eng, prepared)
    fake = _Exec()
    worker = EffectWorker(eng, fake, owner_id="worker-a")
    result = worker.run_once(prepared.run_id)
    assert result.claimed is True
    assert result.completed is True
    assert len(fake.calls) == 1
    assert eng.get_status(prepared.run_id).active_effect_kind == "commit_patch"
