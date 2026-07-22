"""Phase 16.6 integration: authority fencing, lease replacement, and no second write.

These tests use the real SQLite engine (temp DB) to drive a run to a mutating
``commit_patch`` claim, then prove that:

* ``check_claim_authority`` authorizes the live claim/lease;
* replacing the lease (new generation) makes the stale claim's authority snapshot
  REJECTED, so the ``WriteExecutor`` raises ``AuthorityLostError`` with zero writes
  (``EffectWorker`` then completes with ``lease_authority_lost=True``);
* a stale ``complete_claim`` after replacement is fenced (STALE), so no second
  write can be recorded;
* an expired mutating claim recovers into a reconciliation checkpoint on restart.

No real Cursor/Codex/gh/network activity occurs; a recording gateway stands in for
the real Git/GitHub writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import (
    FakeClock,
    publication_success,
    start_run,
)

from ai_dev_loop.pr_review_v2.application.contracts import (
    EffectCompletionRequest,
    EventDisposition,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AuthorityLostError,
    ClaimAuthoritySnapshot,
    WriteAuthorityStatus,
    WriteGatewaySuccess,
    claim_authority_snapshot_from_claim,
)
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    WorkflowLimits,
)
from ai_dev_loop.pr_review_v2.domain.events import CommitRecordedOutcome
from ai_dev_loop.pr_review_v2.infrastructure.runtime import SequenceIdFactory
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore
from ai_dev_loop.pr_review_v2.workers.write_executor import WriteExecutor

SHA_A = "a" * 40
HASH_1 = "1" * 64
HASH_2 = "2" * 64


def _engine(db_path: Path, clock: FakeClock, *, prefix: str = "fence") -> PrReviewEngine:
    return PrReviewEngine(
        SqlitePrReviewStore(db_path),
        clock=clock,
        ids=SequenceIdFactory(prefix=prefix),
        lease_ttl=timedelta(seconds=30),
    )


def _prepared(run_id: str = "run-1") -> PreparedState:
    return PreparedState(
        run_id=run_id,
        origin=SourceRunOrigin(
            source_run_id="local-run-001",
            repository=RepositoryIdentity(name_with_owner="acme/demo"),
            head_branch="feature",
            base_branch="main",
            expected_head_sha=SHA_A,
            accepted_patch=ArtifactRef(relative_path="artifacts/accepted.patch", sha256=HASH_1),
            execution_context_ref=ArtifactRef(
                relative_path="artifacts/execution-context.json", sha256=HASH_2
            ),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=FakeClock().now(),
    )


class _EngineAuthority:
    """Adapts the engine's read-only authority check to the guard protocol."""

    def __init__(self, engine: PrReviewEngine) -> None:
        self._engine = engine

    def check_authority(self, snapshot: ClaimAuthoritySnapshot):
        return self._engine.check_claim_authority(snapshot)


@dataclass
class _RecordingGateway:
    writes: list[str] = field(default_factory=list)

    def commit(self, effect, *, run_id, now, authorize):
        authorize()  # authority is consulted immediately before any mutation
        self.writes.append("commit")
        return WriteGatewaySuccess(
            outcome=CommitRecordedOutcome(
                commit_sha="b" * 40, new_head_sha="b" * 40, expected_remote_sha_before_push=None
            ),
            already_applied=False,
        )


def _drive_to_commit_claim(engine: PrReviewEngine, run_id: str, clock: FakeClock):
    """Complete the LOCAL publication effect and claim the mutating commit effect."""

    lease = engine.acquire_lease(run_id, "owner-a")
    claim = engine.claim_next_effect(run_id, "owner-a", lease.generation).claim
    assert claim is not None and claim.classification == "local"
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
    lease = engine.acquire_lease(run_id, "owner-a")
    claim = engine.claim_next_effect(run_id, "owner-a", lease.generation).claim
    assert claim is not None and claim.classification == "mutating"
    return lease, claim


def test_authority_authorizes_live_claim(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = _engine(tmp_path / "e.sqlite3", clock)
    prepared = _prepared()
    start_run(engine, prepared)
    _lease, claim = _drive_to_commit_claim(engine, prepared.run_id, clock)
    snapshot = claim_authority_snapshot_from_claim(claim)
    result = engine.check_claim_authority(snapshot)
    assert result.status is WriteAuthorityStatus.AUTHORIZED


def test_lease_replacement_fences_write_with_zero_writes(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = _engine(tmp_path / "e.sqlite3", clock)
    prepared = _prepared()
    start_run(engine, prepared)
    lease1, claim = _drive_to_commit_claim(engine, prepared.run_id, clock)

    # Expire and replace the lease: the stale claim's generation is now fenced.
    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    assert lease2.generation == lease1.generation + 1

    snapshot = claim_authority_snapshot_from_claim(claim)
    assert engine.check_claim_authority(snapshot).status is WriteAuthorityStatus.REJECTED

    gateway = _RecordingGateway()
    executor = WriteExecutor(
        git_gateway=gateway,  # type: ignore[arg-type]
        github_gateway=gateway,  # type: ignore[arg-type]
        github_policy=_github_policy(),
    )
    with pytest.raises(AuthorityLostError):
        executor.execute(
            claim.effect,
            claim.completion_token,
            now=clock.now(),
            authority=_EngineAuthority(engine),
            claim=claim,
        )
    assert gateway.writes == []  # zero writes when authority is lost


def test_stale_completion_after_replacement_is_fenced(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = _engine(tmp_path / "e.sqlite3", clock)
    prepared = _prepared()
    start_run(engine, prepared)
    lease1, claim = _drive_to_commit_claim(engine, prepared.run_id, clock)

    clock.advance(60)
    engine.acquire_lease(prepared.run_id, "owner-b")  # generation bump

    late = engine.complete_claim(
        EffectCompletionRequest(
            submission_id="late-commit",
            dispatch_id=claim.dispatch_id,
            claim_id=claim.claim_id,
            owner_id="owner-a",
            lease_generation=lease1.generation,
            event=_commit_ok(claim, clock),
        )
    )
    assert late.disposition is EventDisposition.STALE


def test_expired_mutating_claim_recovers_to_reconcile(tmp_path: Path) -> None:
    clock = FakeClock()
    engine = _engine(tmp_path / "e.sqlite3", clock)
    prepared = _prepared()
    start_run(engine, prepared)
    _lease, _claim = _drive_to_commit_claim(engine, prepared.run_id, clock)

    clock.advance(60)
    lease2 = engine.acquire_lease(prepared.run_id, "owner-b")
    recovered = engine.recover_expired_claims(prepared.run_id, "owner-b", lease2.generation)
    assert recovered == 1
    status = engine.get_status(prepared.run_id)
    assert status.state_kind == "reconciling_write"
    assert status.ambiguous_write_pending is True


def _commit_ok(claim, clock: FakeClock):
    from ai_dev_loop.pr_review_v2.domain.events import EffectSucceeded

    return EffectSucceeded(
        occurred_at=clock.now(),
        token=claim.completion_token,
        outcome=CommitRecordedOutcome(
            commit_sha="b" * 40, new_head_sha="b" * 40, expected_remote_sha_before_push=None
        ),
    )


def _github_policy():
    from ai_dev_loop.pr_review_v2.application.write_contracts import GitHubWritePolicy

    return GitHubWritePolicy(repository_cwd="/tmp/repo")
