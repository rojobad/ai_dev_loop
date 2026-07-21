"""Phase 16.5 lease renewal, restart recovery, and completion fencing."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    drive_to_waiting_for_bot,
    fingerprint,
    make_engine,
    make_observe_eligible,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
    EventSubmission,
)
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    BotStillWaitingOutcome,
    EffectSucceeded,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.runtime import completion_submission_id


def _prepared(clock: FakeClock) -> PreparedState:
    return PreparedState(
        run_id="run-16-5-lease",
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha="a" * 40,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256="1" * 64),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json",
                sha256="2" * 64,
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=clock.now(),
    )


def test_expired_readonly_claim_requeues_same_dispatch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock)
    prepared = _prepared(clock)
    engine.create_run(prepared.run_id, prepared)
    engine.apply_event(
        EventSubmission(
            submission_id="start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, prepared.run_id, clock)
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.effect.kind == "observe_bot_review"
    dispatch_id = claim.dispatch_id
    effect_id = claim.effect.effect_id
    # Expire lease and reopen.
    clock.advance(timedelta(seconds=31))
    engine2 = make_engine(tmp_path / "db.sqlite3", clock, prefix="reopen")
    lease2 = engine2.acquire_lease(prepared.run_id, "owner-b")
    engine2.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    result = engine2.claim_next_effect(prepared.run_id, "owner-b", lease2.generation)
    assert result.claim is not None
    assert result.claim.dispatch_id == dispatch_id
    assert result.claim.effect.effect_id == effect_id
    assert result.claim.effect.kind == "observe_bot_review"


def test_stale_generation_completion_is_fenced(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "db.sqlite3", clock)
    prepared = _prepared(clock)
    engine.create_run(prepared.run_id, prepared)
    engine.apply_event(
        EventSubmission(
            submission_id="start",
            run_id=prepared.run_id,
            expected_version=1,
            event=StartRequested(occurred_at=clock.now()),
        )
    )
    drive_to_waiting_for_bot(engine, prepared, clock)
    make_observe_eligible(engine, prepared.run_id, clock)
    lease, claim = claim_next(engine, prepared.run_id)
    before = fingerprint(engine, prepared.run_id)
    late = EffectSucceeded(
        occurred_at=clock.now(),
        token=claim.completion_token,
        outcome=BotStillWaitingOutcome(
            next_not_before=clock.now() + timedelta(seconds=60),
            poll_sequence=claim.effect.poll_sequence + 1,
        ),
    )
    receipt = engine.complete_claim(
        EffectCompletionRequest(
            submission_id=completion_submission_id(
                run_id=prepared.run_id,
                dispatch_id=claim.dispatch_id,
                claim_id=claim.claim_id,
                lease_generation=lease.generation + 99,
                result_key="late",
            ),
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease.generation + 99,
            event=late,
        )
    )
    assert receipt.disposition in {EventDisposition.STALE, EventDisposition.REJECTED}
    assert fingerprint(engine, prepared.run_id) == before
