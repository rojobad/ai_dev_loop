"""Phase 16.5 durable engine workflows for observe outcomes and retries."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from tests.integration.phase16_4_matrix_helpers import (
    claim_next,
    complete_ok,
    drive_to_waiting_for_bot,
    make_engine,
    make_observe_eligible,
)
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import FakeTransport, policy, result_from_fixture
from tests.unit.pr_review_v2.helpers import SHA_B

from ai_dev_loop.pr_review_v2.application.contracts import EventSubmission
from ai_dev_loop.pr_review_v2.application.github_read import AllowlistedHeaders, GhTransportResult
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    EffectRetryableFailure,
    ErrorSummary,
    PauseReasonKind,
    PreparedState,
    RepositoryIdentity,
    ResumeRequested,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.infrastructure.github_read_gateway import GitHubReadGateway
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore
from ai_dev_loop.pr_review_v2.infrastructure.runtime import completion_submission_id
from ai_dev_loop.pr_review_v2.workers.github_read_executor import GitHubReadExecutor


def _prepared(clock: FakeClock) -> PreparedState:
    return PreparedState(
        run_id="run-16-5",
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


def _waiting_transport(trigger_marker: str) -> FakeTransport:
    transport = FakeTransport()
    transport.identity_pages.append(result_from_fixture("pr_identity_ok.txt"))
    transport.comment_pages.append(
        GhTransportResult(
            http_status=200,
            headers=AllowlistedHeaders(),
            body_json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "comments": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [
                                    {
                                        "id": "IC_1",
                                        "databaseId": 101,
                                        "body": f"please review <!-- {trigger_marker} -->",
                                        "createdAt": "2026-07-21T12:00:00Z",
                                        "author": {"login": "orchestrator"},
                                    }
                                ],
                            }
                        }
                    }
                }
            },
            returncode=0,
        )
    )
    transport.thread_pages.append(
        GhTransportResult(
            http_status=200,
            headers=AllowlistedHeaders(),
            body_json={
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [],
                            }
                        }
                    }
                }
            },
            returncode=0,
        )
    )
    return transport


def test_waiting_outcome_through_complete_claim(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock)
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

    # Peek the pending observe effect marker without consuming the claim permanently:
    lease, claim = claim_next(engine, prepared.run_id)
    marker = claim.effect.trigger_marker
    assert marker is not None
    # Complete with a synthetic waiting outcome via the real executor instead:
    # re-queue by completing? We already claimed. Use this claim with the worker path
    # by building executor and completing manually through worker-equivalent flow.
    transport = _waiting_transport(marker)
    pol = policy(repository_cwd=str(tmp_path))
    executor = GitHubReadExecutor(
        gateway=GitHubReadGateway(
            policy=pol,
            transport=transport,
            artifacts=ReviewArtifactStore(tmp_path / "artifacts"),
        ),
        policy=pol,
        clock=clock,
    )
    event = executor.execute(claim.effect, claim.completion_token, now=clock.now())
    complete_ok(
        engine,
        lease,
        claim,
        completion_submission_id(
            run_id=prepared.run_id,
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            lease_generation=lease.generation,
            result_key="observe-wait",
        ),
        event,
    )
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "waiting_for_bot"
    assert status.effect_attempt == 1
    assert status.next_eligible_at is not None
    assert event.kind == "effect_succeeded"
    assert event.outcome.kind == "bot_still_waiting"


def test_six_transient_failures_then_retry_exhausted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    engine = make_engine(tmp_path / "engine.sqlite3", clock)
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

    original_effect_id = None
    for attempt in range(1, 7):
        lease, claim = claim_next(engine, prepared.run_id)
        if original_effect_id is None:
            original_effect_id = claim.effect.effect_id
        assert claim.effect.effect_id == original_effect_id
        assert claim.effect.attempt == attempt
        occurred = clock.now()
        next_at = occurred + timedelta(seconds=10)
        event = EffectRetryableFailure(
            occurred_at=occurred,
            token=claim.completion_token,
            error=ErrorSummary(kind=TransientErrorKind.HTTP_500, safe_summary="server"),
            failed_attempt=attempt,
            next_attempt_at=next_at,
        )
        complete_ok(
            engine,
            lease,
            claim,
            f"retry-sub-{attempt}",
            event,
        )
        if attempt < 6:
            status = engine.get_status(prepared.run_id)
            assert status.state_kind == "waiting_retry"
            clock.set(next_at)
            receipts = engine.fire_due_timers()
            assert receipts
        else:
            status = engine.get_status(prepared.run_id)
            assert status.state_kind == "paused"
            assert status.last_error_kind == PauseReasonKind.RETRY_EXHAUSTED.value

    status = engine.get_status(prepared.run_id)
    engine.apply_event(
        EventSubmission(
            submission_id="resume",
            run_id=prepared.run_id,
            expected_version=status.run_version,
            event=ResumeRequested(occurred_at=clock.now()),
        )
    )
    lease, claim = claim_next(engine, prepared.run_id)
    assert claim.effect.attempt == 1
    assert claim.effect.effect_id == original_effect_id
    assert claim.effect.bound_head_sha == SHA_B
