"""Phase 16.4 durable engine integration tests over real SQLite."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import (
    FakeClock,
    publication_success,
    start_run,
)

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
    NextActionCategory,
    PrReviewEngineError,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    PreparedState,
    PublicationTextPreparedOutcome,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import FaultInjector, SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.effect_worker import EffectWorker

SHA_A = "a" * 40
SHA_B = "b" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(tmp_path: Path, clock: FakeClock) -> PrReviewEngine:
    store = SqlitePrReviewStore(tmp_path / "engine.sqlite3")
    return PrReviewEngine(
        store,
        clock=clock,
        ids=SequenceIdFactory(prefix="int"),
        fault_hook=FaultInjector(),
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


def _claim(engine: PrReviewEngine, run_id: str, owner: str = "worker-a"):
    lease = engine.acquire_lease(run_id, owner)
    result = engine.claim_next_effect(run_id, owner, lease.generation)
    assert result.claim is not None
    return lease, result.claim


def test_accepted_event_atomically_journals_and_emits(
    engine: PrReviewEngine, prepared: PreparedState
) -> None:
    engine.create_run(prepared.run_id, prepared)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    assert receipt.resulting_run_version == 2
    assert receipt.state is not None and receipt.state.kind == "publishing_initial"
    assert len(receipt.effects) == 1
    status = engine.get_status(prepared.run_id)
    assert status.run_version == 2
    assert status.effect_status is DispatchStatus.PENDING


def test_reducer_rejection_audited_without_mutation(
    engine: PrReviewEngine, prepared: PreparedState
) -> None:
    engine.create_run(prepared.run_id, prepared)
    from ai_dev_loop.pr_review_v2.domain import ResumeRequested

    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-resume",
            run_id=prepared.run_id,
            expected_version=1,
            event=ResumeRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition is EventDisposition.REJECTED
    status = engine.get_status(prepared.run_id)
    assert status.run_version == 1
    assert status.state_kind == "prepared"
    assert status.effect_status is None


def test_stale_expected_version_audited(engine: PrReviewEngine, prepared: PreparedState) -> None:
    start_run(engine, prepared)
    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-stale",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition is EventDisposition.STALE
    assert engine.get_status(prepared.run_id).run_version == 2


def test_submission_dedup_and_collision(
    engine: PrReviewEngine, prepared: PreparedState, tmp_path: Path, clock: FakeClock
) -> None:
    start_run(engine, prepared)
    first = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert first.disposition is EventDisposition.DUPLICATE
    assert first.duplicate_of_submission is True

    # reopen store
    engine2 = PrReviewEngine(
        SqlitePrReviewStore(tmp_path / "engine.sqlite3"),
        clock=clock,
        ids=SequenceIdFactory(prefix="reopen"),
        lease_ttl=timedelta(seconds=30),
    )
    again = engine2.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    assert again.disposition is EventDisposition.DUPLICATE

    with pytest.raises(PrReviewEngineError, match="different run or payload"):
        engine.apply_event(
            EventSubmission(
                submission_id="sub-start",
                run_id=prepared.run_id,
                expected_version=1,
                event=StartRequested(occurred_at=clock.now() + timedelta(seconds=1)),
            )
        )


def test_fault_injection_atomicity(engine: PrReviewEngine, prepared: PreparedState) -> None:
    faults = engine._fault  # noqa: SLF001
    engine.create_run(prepared.run_id, prepared)

    def boom() -> None:
        raise RuntimeError("injected fault")

    faults.set("pre_commit", boom)
    with pytest.raises(RuntimeError, match="injected"):
        engine.apply_event(
            EventSubmission(
                submission_id="sub-start",
                run_id=prepared.run_id,
                expected_version=1,
                event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
            )
        )
    faults.clear()
    status = engine.get_status(prepared.run_id)
    assert status.run_version == 1
    assert status.state_kind == "prepared"

    receipt = engine.apply_event(
        EventSubmission(
            submission_id="sub-start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=engine._clock.now()),  # noqa: SLF001
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED


def test_complete_claim_and_worker_path(
    engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock
) -> None:
    start_run(engine, prepared)
    lease, claim = _claim(engine, prepared.run_id)
    event = publication_success(claim.effect, claim.completion_token, clock.now())
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="sub-complete-1",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=event,
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    assert receipt.state is not None
    assert receipt.state.kind == "publishing_initial"
    assert receipt.effects[0].kind == "commit_patch"

    # rejected completion leaves claim intact when reducer rejects
    lease2 = engine.acquire_lease(prepared.run_id, "worker-a")
    claim2 = engine.claim_next_effect(prepared.run_id, "worker-a", lease2.generation).claim
    assert claim2 is not None
    bad = EffectSucceeded(
        occurred_at=clock.now(),
        token=claim2.completion_token,
        outcome=PublicationTextPreparedOutcome(
            publication_text_ref=ArtifactRef(
                relative_path="artifacts/publication.md", sha256=HASH_1
            ),
            commit_message_ref=ArtifactRef(
                relative_path="artifacts/commit-message.txt", sha256=HASH_2
            ),
        ),
    )
    rejected = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="sub-bad-outcome",
            dispatch_id=claim2.dispatch_id,
            claim_id=claim2.claim_id,
            owner_id="worker-a",
            lease_generation=lease2.generation,
            event=bad,
        )
    )
    assert rejected.disposition is EventDisposition.REJECTED
    status = engine.get_status(prepared.run_id)
    assert status.effect_status is DispatchStatus.CLAIMED


def test_retry_timer_and_observation_delay(
    engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock
) -> None:
    start_run(engine, prepared)
    lease, claim = _claim(engine, prepared.run_id)
    # succeed generate -> commit
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
    lease, claim = _claim(engine, prepared.run_id)
    # force retryable failure on commit
    next_at = clock.now() + timedelta(seconds=30)
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="c-retry",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=EffectRetryableFailure(
                occurred_at=clock.now(),
                token=claim.completion_token,
                error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="timeout"),
                failed_attempt=claim.attempt,
                next_attempt_at=next_at,
            ),
        )
    )
    assert receipt.disposition is EventDisposition.ACCEPTED
    assert receipt.state is not None and receipt.state.kind == "waiting_retry"
    status = engine.get_status(prepared.run_id)
    assert status.next_action is NextActionCategory.WAIT_FOR_RETRY
    assert status.effect_attempt == 1

    # early fire supersedes nothing useful / no fire yet
    fired = engine.fire_due_timers()
    assert fired == []

    clock.set(next_at)
    fired = engine.fire_due_timers()
    assert len(fired) == 1
    assert fired[0].disposition is EventDisposition.ACCEPTED
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "publishing_initial"
    assert status.effect_attempt == 2
    assert status.effect_status is DispatchStatus.PENDING

    # reopen preserves attempt
    engine2 = PrReviewEngine(
        SqlitePrReviewStore(engine.store.db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix="re"),
        lease_ttl=timedelta(seconds=30),
    )
    status2 = engine2.get_status(prepared.run_id)
    assert status2.effect_attempt == 2


def test_abort_cancels_and_fences(
    engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock
) -> None:
    start_run(engine, prepared)
    lease, claim = _claim(engine, prepared.run_id)
    abort = engine.abort_run(submission_id="abort-1", run_id=prepared.run_id)
    assert abort.disposition is EventDisposition.ACCEPTED
    assert abort.state is not None and abort.state.kind == "aborted"
    status = engine.get_status(prepared.run_id)
    assert status.next_action is NextActionCategory.TERMINAL_ABORTED
    assert status.lease_active is False

    late = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="late-complete",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="worker-a",
            lease_generation=lease.generation,
            event=publication_success(claim.effect, claim.completion_token, clock.now()),
        )
    )
    assert late.disposition is EventDisposition.STALE
    assert engine.get_status(prepared.run_id).state_kind == "aborted"


def test_worker_one_step(engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock) -> None:
    start_run(engine, prepared)

    class PubExec:
        def execute(self, effect, token, *, now):
            return publication_success(effect, token, now)

    worker = EffectWorker(engine, PubExec(), owner_id="worker-a")
    step = worker.run_once(prepared.run_id)
    assert step.claimed is True
    assert step.completed is True
    assert step.effect_kind == "generate_publication_text"
    assert engine.get_status(prepared.run_id).active_effect_kind == "commit_patch"


def test_no_sensitive_payloads_in_db(
    engine: PrReviewEngine, prepared: PreparedState, clock: FakeClock
) -> None:
    start_run(engine, prepared)
    lease, claim = _claim(engine, prepared.run_id)
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
    raw = engine.store.db_path.read_bytes()
    for banned in (b"sk-secret", b"ghp_", b"BEGIN PRIVATE", b"password=", b"API_KEY="):
        assert banned not in raw
    # Artifact refs are paths/hashes only
    assert b"artifacts/publication.md" in raw or b"generate_publication_text" in raw
