"""Phase 16.8 production-boundary fault matrix and gateway/runtime tests."""

from __future__ import annotations

import hashlib
import stat
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
from tests.integration.phase16_8_checkpoint_helpers import (
    GH_FAILURE_KINDS,
    MUTATING_GH_EFFECT_KINDS,
    RECONCILE_MATRIX,
    assemble_write_gateway,
    assert_effect_identity_preserved,
    assert_open_claim,
    reopen_engine,
)
from tests.integration.phase16_8_helpers import (
    EFFECT_RESILIENCE_MATRIX,
    MARKER,
    SHA_B,
    assemble_read_stack,
    assemble_recording_read_stack,
    assert_classifications_align,
    assert_matrix_covers_all_effects,
    init_git_remote,
    policy_for_kind,
    read_artifact_root,
    seed_observation_for_effect,
)
from tests.integration.stateful_fake_gh import StatefulFakeGhController
from tests.unit.pr_review_v2 import write_helpers as H
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.github_read_helpers import observe_effect, policy, token_for

from ai_dev_loop.pr_review_v2.application.contracts import (
    DispatchStatus,
    EffectClaim,
    EventDisposition,
    EventSubmission,
    LeaseAcquireResult,
)
from ai_dev_loop.pr_review_v2.application.engine import PrReviewEngine
from ai_dev_loop.pr_review_v2.application.github_read import (
    GitHubReadPolicy,
    ObservationEvidenceKind,
    local_base_delay_seconds,
)
from ai_dev_loop.pr_review_v2.application.write_contracts import (
    AmbiguousWriteError,
    WriteProofKind,
    html_comment_marker,
)
from ai_dev_loop.pr_review_v2.domain import (
    ArtifactRef,
    PauseReasonKind,
    PreparedState,
    RepositoryIdentity,
    SourceRunOrigin,
    StartRequested,
    TransientErrorKind,
    WorkflowLimits,
    build_opaque_trigger_marker,
)
from ai_dev_loop.pr_review_v2.domain.effects import LOCAL_KINDS, MUTATING_KINDS
from ai_dev_loop.pr_review_v2.infrastructure.paths import run_artifact_root
from ai_dev_loop.pr_review_v2.infrastructure.review_artifacts import ReviewArtifactStore

READ_CHECKPOINTS = (
    "before_transport",
    "after_artifact_before_complete_claim",
)
LEASE_TTL_SECONDS = 30


def test_effect_matrix_tracks_production_domain_registry() -> None:
    assert_matrix_covers_all_effects()
    assert_classifications_align()
    assert len(EFFECT_RESILIENCE_MATRIX) == len(
        {item.effect_kind for item in EFFECT_RESILIENCE_MATRIX}
    )


@pytest.mark.parametrize("policy_row", EFFECT_RESILIENCE_MATRIX, ids=lambda p: p.effect_kind)
def test_matrix_declares_recovery_contract(policy_row) -> None:
    assert policy_row.immutable_input_fields
    assert policy_row.protected_output_fields
    assert policy_row.idempotency_target
    assert policy_row.retry_rule
    assert policy_row.reconcile_rule
    assert policy_row.lease_expiry
    assert policy_row.abort_behavior
    assert len(policy_row.crash_checkpoints) >= 1
    assert policy_for_kind(policy_row.effect_kind) is policy_row


@dataclass(frozen=True, slots=True)
class ReadClaimHarness:
    """One persistent run parked on its first claimed ``observe_bot_review`` effect."""

    engine: PrReviewEngine
    run_id: str
    clock: FakeClock
    db_path: Path
    artifact_root: Path
    controller: StatefulFakeGhController
    lease: LeaseAcquireResult
    claim: EffectClaim


@dataclass(frozen=True, slots=True)
class ReadFailureCase:
    """One common operational GitHub read failure and its expected retry contract."""

    fixture: str
    expected_error: TransientErrorKind
    expected_delay_seconds: float
    extra: dict[str, object] = field(default_factory=dict)


# Attempt 1 has no server-directed header, so backoff is the local base delay.
LOCAL_FIRST_BACKOFF_SECONDS = float(local_base_delay_seconds(1))
RETRY_AFTER_SECONDS = 45
RATE_LIMIT_RESET_SECONDS = 120

COMMON_READ_FAILURES: tuple[ReadFailureCase, ...] = (
    ReadFailureCase(
        fixture="timeout",
        expected_error=TransientErrorKind.TIMEOUT,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
        extra={"seconds": 5.0},
    ),
    ReadFailureCase(
        fixture="dns",
        expected_error=TransientErrorKind.DNS_FAILURE,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
    ),
    ReadFailureCase(
        fixture="http_429",
        expected_error=TransientErrorKind.HTTP_429,
        expected_delay_seconds=float(RETRY_AFTER_SECONDS),
        extra={"retry_after": RETRY_AFTER_SECONDS},
    ),
    ReadFailureCase(
        fixture="primary_rate_limit",
        expected_error=TransientErrorKind.PRIMARY_RATE_LIMIT,
        expected_delay_seconds=float(RATE_LIMIT_RESET_SECONDS),
    ),
    ReadFailureCase(
        fixture="http_500",
        expected_error=TransientErrorKind.HTTP_500,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
    ),
    ReadFailureCase(
        fixture="http_502",
        expected_error=TransientErrorKind.HTTP_502,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
    ),
    ReadFailureCase(
        fixture="http_503",
        expected_error=TransientErrorKind.HTTP_503,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
    ),
    ReadFailureCase(
        fixture="http_504",
        expected_error=TransientErrorKind.HTTP_504,
        expected_delay_seconds=LOCAL_FIRST_BACKOFF_SECONDS,
    ),
)


def _read_policy(**overrides: object) -> GitHubReadPolicy:
    base: dict[str, object] = {
        "gh_command": "gh",
        "per_call_timeout_seconds": 1.0,
        "overall_timeout_seconds": 5.0,
    }
    base.update(overrides)
    return policy(**base)


def _claimed_observe_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    run_id: str,
    prefix: str,
) -> ReadClaimHarness:
    """Drive a real engine to the first observe claim and align the fake ``gh`` fixture."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    clock = FakeClock()
    db_path = tmp_path / "engine.sqlite3"
    engine = make_engine(db_path, clock, prefix=prefix)
    prepared = PreparedState(
        run_id=run_id,
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
    assert claim.effect.attempt == 1
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    seed_observation_for_effect(controller, claim.effect)
    return ReadClaimHarness(
        engine=engine,
        run_id=prepared.run_id,
        clock=clock,
        db_path=db_path,
        artifact_root=read_artifact_root(tmp_path),
        controller=controller,
        lease=lease,
        claim=claim,
    )


def _dispatch_rows(engine: PrReviewEngine, run_id: str) -> list[dict[str, object]]:
    with engine.store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT dispatch_id, effect_kind, status, attempt, last_error_kind
            FROM pr_review_effects WHERE run_id = ? ORDER BY dispatch_id
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _pending_timers(engine: PrReviewEngine, run_id: str) -> list[dict[str, object]]:
    with engine.store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT timer_id, status, due_at, target_effect_id
            FROM pr_review_timers WHERE run_id = ? AND status = 'pending'
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _assert_gh_calls_were_read_only(controller: StatefulFakeGhController) -> None:
    state = controller.reload()
    assert state.calls, "expected the production read gateway to call the fake gh executable"
    assert state.mutation_counts == {}
    assert state.applied_markers == []
    assert state.resolved_threads == []
    assert state.thread_replies == {}
    for call in state.calls:
        assert "mutation" not in call.lower()
        for verb in ("POST", "PATCH", "PUT", "DELETE"):
            assert f"--method {verb}" not in call


def _assert_no_downstream_local_or_write_work(
    engine: PrReviewEngine,
    run_id: str,
    *,
    live_dispatch_id: str | None,
) -> None:
    """No LOCAL/model/mutating work may be dispatched by an observation alone."""

    rows = _dispatch_rows(engine, run_id)
    live = [
        row["dispatch_id"]
        for row in rows
        if row["status"] in {DispatchStatus.PENDING.value, DispatchStatus.CLAIMED.value}
    ]
    assert live == ([live_dispatch_id] if live_dispatch_id is not None else [])
    kinds = {str(row["effect_kind"]) for row in rows}
    # generate_publication_text ran before the trigger; adjudication/local fix must not exist.
    assert kinds & LOCAL_KINDS == {"generate_publication_text"}
    for row in rows:
        if row["effect_kind"] in MUTATING_KINDS:
            assert row["status"] == DispatchStatus.SUCCEEDED.value


def test_read_matrix_crash_checkpoints_are_the_executed_ones() -> None:
    assert policy_for_kind("observe_bot_review").crash_checkpoints == READ_CHECKPOINTS


def test_read_observe_failure_before_transport_has_no_side_effects(tmp_path: Path) -> None:
    """Checkpoint ``before_transport``: the first read call fails, nothing is applied."""

    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    controller.queue_failure("http_500")
    _, executor = assemble_read_stack(tmp_path, controller, policy=policy(gh_command="gh"))
    effect = observe_effect(attempt=1)
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind == "effect_retryable_failure"
    assert event.error.kind is TransientErrorKind.HTTP_500
    obs_dir = run_artifact_root(read_artifact_root(tmp_path), effect.run_id) / "observations"
    assert not obs_dir.exists()
    _assert_gh_calls_were_read_only(controller)


def test_read_observe_crash_after_artifact_before_complete_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checkpoint ``after_artifact_before_complete_claim`` on the exact engine claim."""

    harness = _claimed_observe_run(
        tmp_path,
        monkeypatch,
        run_id="run-read-cp",
        prefix="p168-read-cp",
    )
    effect = harness.claim.effect
    token = harness.claim.completion_token
    before_fp = fingerprint(harness.engine, harness.run_id)
    observed_at = harness.clock.now()

    recorder, executor = assemble_recording_read_stack(
        tmp_path,
        harness.controller,
        policy=_read_policy(),
        clock=harness.clock,
    )
    event = executor.execute(effect, token, now=observed_at)

    assert event.kind == "effect_succeeded", getattr(event, "safe_summary", event)
    assert event.token == token
    assert event.outcome.kind == "bot_still_waiting"
    assert event.outcome.poll_sequence == effect.poll_sequence + 1

    # The gateway persists exactly one observation, bound to the claimed run/cycle/poll.
    assert len(recorder.observations) == 1
    snapshot, ref = recorder.observations[0]
    assert snapshot.binding == effect.binding
    run_root = run_artifact_root(harness.artifact_root, effect.run_id)
    expected_relative = (
        f"observations/cycle-{effect.cycle_number:02d}/"
        f"poll-{effect.poll_sequence:04d}/{ref.sha256}.json"
    )
    assert ref.relative_path == expected_relative
    obs_path = run_root / ref.relative_path
    assert obs_path.is_file()
    assert hashlib.sha256(obs_path.read_bytes()).hexdigest() == ref.sha256
    assert stat.S_IMODE(obs_path.stat().st_mode) & 0o077 == 0
    manifest = ReviewArtifactStore(harness.artifact_root).read_and_verify(
        run_id=effect.run_id,
        ref=ref,
        expected_binding=effect.binding,
    )
    assert manifest.cycle_number == effect.cycle_number
    assert manifest.poll_sequence == effect.poll_sequence
    assert manifest.trigger_marker == effect.trigger_marker
    assert manifest.head_sha == effect.bound_head_sha
    assert manifest.observed_at == observed_at
    assert manifest.evidence_kind is ObservationEvidenceKind.BOT_STILL_WAITING

    # Crash here: complete_claim never runs. Reopen the same DB and artifact root.
    engine2 = reopen_engine(harness.db_path, harness.clock, prefix="p168-read-reopen")
    assert_open_claim(
        engine2,
        harness.run_id,
        dispatch_id=harness.claim.dispatch_id,
        effect_id=effect.effect_id,
        effect_kind="observe_bot_review",
    )
    after_fp = fingerprint(engine2, harness.run_id)
    assert_effect_identity_preserved(before_fp, after_fp, dispatch_id=harness.claim.dispatch_id)
    assert (
        ReviewArtifactStore(harness.artifact_root).read_and_verify(
            run_id=effect.run_id,
            ref=ref,
            expected_binding=effect.binding,
        )
        == manifest
    )

    # The expired READ claim requeues the same dispatch and the same effect identity.
    harness.clock.advance(timedelta(seconds=LEASE_TTL_SECONDS + 1))
    lease2 = engine2.acquire_lease(harness.run_id, "owner-b")
    assert engine2.recover_expired_claims(harness.run_id, "owner-b", lease2.generation) == 1
    claim2 = engine2.claim_next_effect(harness.run_id, "owner-b", lease2.generation).claim
    assert claim2 is not None
    assert claim2.dispatch_id == harness.claim.dispatch_id
    assert claim2.effect == effect
    assert claim2.completion_token.effect_id == token.effect_id

    _assert_gh_calls_were_read_only(harness.controller)
    _assert_no_downstream_local_or_write_work(
        engine2,
        harness.run_id,
        live_dispatch_id=harness.claim.dispatch_id,
    )
    assert sorted(path.name for path in run_root.iterdir()) == ["observations"]


@pytest.mark.parametrize("case", COMMON_READ_FAILURES, ids=lambda c: c.fixture)
def test_common_read_failure_persists_retry_without_early_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: ReadFailureCase,
) -> None:
    harness = _claimed_observe_run(
        tmp_path,
        monkeypatch,
        run_id="run-read-retry",
        prefix="p168-read-retry",
    )
    effect = harness.claim.effect
    observed_at = harness.clock.now()
    extra = dict(case.extra)
    if case.fixture == "primary_rate_limit":
        extra["reset_epoch"] = int(observed_at.timestamp()) + RATE_LIMIT_RESET_SECONDS
    harness.controller.queue_failure(case.fixture, **extra)
    _, executor = assemble_read_stack(
        tmp_path,
        harness.controller,
        policy=_read_policy(per_call_timeout_seconds=0.3, overall_timeout_seconds=1.0),
        clock=harness.clock,
    )

    event = executor.execute(effect, harness.claim.completion_token, now=observed_at)

    assert event.kind == "effect_retryable_failure"
    assert event.error.kind is case.expected_error
    assert event.failed_attempt == effect.attempt
    expected_next = observed_at + timedelta(seconds=case.expected_delay_seconds)
    assert event.next_attempt_at == expected_next
    obs_dir = run_artifact_root(harness.artifact_root, effect.run_id) / "observations"
    assert not obs_dir.exists()

    complete_ok(harness.engine, harness.lease, harness.claim, "read-transient", event)

    engine2 = reopen_engine(harness.db_path, harness.clock, prefix="p168-read-retry-reopen")
    rows = {row["dispatch_id"]: row for row in _dispatch_rows(engine2, harness.run_id)}
    failed_row = rows[harness.claim.dispatch_id]
    assert failed_row["status"] == DispatchStatus.RETRY_WAIT.value
    assert failed_row["attempt"] == 1
    assert failed_row["last_error_kind"] == str(case.expected_error)
    status = engine2.get_status(harness.run_id)
    assert status.state_kind == "waiting_retry"
    assert status.next_eligible_at == expected_next
    timers = _pending_timers(engine2, harness.run_id)
    assert len(timers) == 1
    assert timers[0]["target_effect_id"] == effect.effect_id

    # Before the durable next_attempt_at nothing may fire or become claimable.
    harness.clock.set(expected_next - timedelta(seconds=1))
    assert engine2.fire_due_timers_for_run(harness.run_id) == []
    early_lease = engine2.acquire_lease(harness.run_id, "owner-a")
    early = engine2.claim_next_effect(harness.run_id, "owner-a", early_lease.generation)
    assert early.claim is None
    assert _pending_timers(engine2, harness.run_id)

    harness.clock.set(expected_next)
    fired = engine2.fire_due_timers_for_run(harness.run_id)
    assert len(fired) == 1
    assert fired[0].disposition is EventDisposition.ACCEPTED
    retry_lease = engine2.acquire_lease(harness.run_id, "owner-a")
    retry = engine2.claim_next_effect(harness.run_id, "owner-a", retry_lease.generation).claim
    assert retry is not None
    assert retry.effect.effect_id == effect.effect_id
    assert retry.effect.idempotency_key == effect.idempotency_key
    assert retry.attempt == 2

    _assert_gh_calls_were_read_only(harness.controller)
    _assert_no_downstream_local_or_write_work(
        engine2,
        harness.run_id,
        live_dispatch_id=retry.dispatch_id,
    )


def test_malformed_read_response_blocks_without_retry_timer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _claimed_observe_run(
        tmp_path,
        monkeypatch,
        run_id="run-read-malformed",
        prefix="p168-read-malformed",
    )
    harness.controller.queue_failure("malformed")
    _, executor = assemble_read_stack(
        tmp_path,
        harness.controller,
        policy=_read_policy(),
        clock=harness.clock,
    )

    event = executor.execute(
        harness.claim.effect,
        harness.claim.completion_token,
        now=harness.clock.now(),
    )

    assert event.kind == "effect_blocked"
    assert event.reason is PauseReasonKind.REQUIRED_OPERATOR_ACTION
    complete_ok(harness.engine, harness.lease, harness.claim, "read-malformed", event)

    engine2 = reopen_engine(harness.db_path, harness.clock, prefix="p168-read-malformed-reopen")
    rows = {row["dispatch_id"]: row for row in _dispatch_rows(engine2, harness.run_id)}
    assert rows[harness.claim.dispatch_id]["status"] == DispatchStatus.BLOCKED.value
    assert engine2.get_status(harness.run_id).state_kind == "paused"
    assert _pending_timers(engine2, harness.run_id) == []
    _assert_gh_calls_were_read_only(harness.controller)
    _assert_no_downstream_local_or_write_work(engine2, harness.run_id, live_dispatch_id=None)


def test_normal_bot_polling_is_not_a_transport_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent bot schedules the next poll; it must not consume the retry budget."""

    harness = _claimed_observe_run(
        tmp_path,
        monkeypatch,
        run_id="run-read-polling",
        prefix="p168-read-polling",
    )
    effect = harness.claim.effect
    read_policy = _read_policy()
    _, executor = assemble_read_stack(
        tmp_path,
        harness.controller,
        policy=read_policy,
        clock=harness.clock,
    )
    observed_at = harness.clock.now()

    event = executor.execute(effect, harness.claim.completion_token, now=observed_at)

    assert event.kind == "effect_succeeded"
    assert event.outcome.kind == "bot_still_waiting"
    assert event.outcome.next_not_before == observed_at + timedelta(
        seconds=read_policy.poll_interval_seconds
    )
    complete_ok(harness.engine, harness.lease, harness.claim, "read-waiting", event)

    engine2 = reopen_engine(harness.db_path, harness.clock, prefix="p168-read-polling-reopen")
    rows = {row["dispatch_id"]: row for row in _dispatch_rows(engine2, harness.run_id)}
    assert rows[harness.claim.dispatch_id]["status"] == DispatchStatus.SUCCEEDED.value
    assert _pending_timers(engine2, harness.run_id) == []
    next_rows = [
        row
        for row in _dispatch_rows(engine2, harness.run_id)
        if row["status"] == DispatchStatus.PENDING.value
    ]
    assert len(next_rows) == 1
    assert next_rows[0]["effect_kind"] == "observe_bot_review"
    assert next_rows[0]["attempt"] == 1
    # The next poll is not eligible before the configured poll interval elapses.
    early_lease = engine2.acquire_lease(harness.run_id, "owner-a")
    assert (
        engine2.claim_next_effect(harness.run_id, "owner-a", early_lease.generation).claim is None
    )
    harness.clock.set(observed_at + timedelta(seconds=read_policy.poll_interval_seconds))
    lease = engine2.acquire_lease(harness.run_id, "owner-a")
    nxt = engine2.claim_next_effect(harness.run_id, "owner-a", lease.generation).claim
    assert nxt is not None
    assert nxt.effect.poll_sequence == effect.poll_sequence + 1
    _assert_gh_calls_were_read_only(harness.controller)
    _assert_no_downstream_local_or_write_work(
        engine2,
        harness.run_id,
        live_dispatch_id=nxt.dispatch_id,
    )


@pytest.mark.parametrize("failure_kind", GH_FAILURE_KINDS)
def test_stateful_gh_failure_through_production_read_gateway(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    if failure_kind == "timeout":
        controller.queue_failure("timeout", seconds=0.05)
    elif failure_kind == "apply_then_hang":
        controller.queue_failure("http_500")
    else:
        controller.queue_failure(failure_kind)
    read_policy = policy(gh_command="gh", per_call_timeout_seconds=0.2, overall_timeout_seconds=1.0)
    _, executor = assemble_read_stack(tmp_path, controller, policy=read_policy)
    effect = observe_effect(attempt=1)
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.kind in {"effect_retryable_failure", "effect_blocked"}
    if failure_kind == "malformed":
        assert event.kind == "effect_blocked"
    reloaded = controller.reload()
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 0


_RECONCILE_CASES = [
    (effect_kind, outcome)
    for effect_kind, outcomes in RECONCILE_MATRIX.items()
    for outcome in outcomes
]


@pytest.mark.parametrize(
    ("effect_kind", "outcome"),
    _RECONCILE_CASES,
    ids=[f"{kind}-{outcome}" for kind, outcome in _RECONCILE_CASES],
)
def test_mutating_reconcile_outcome_through_stateful_gh(
    tmp_path: Path,
    effect_kind: str,
    outcome: str,
) -> None:
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    bundle = assemble_write_gateway(tmp_path, controller)
    RECONCILE_MATRIX[effect_kind][outcome](bundle)
    reloaded = bundle.controller.reload()
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 0


@pytest.mark.parametrize("effect_kind", sorted(MUTATING_KINDS))
def test_git_mutating_effects_have_matrix_reconcile_contract(effect_kind: str) -> None:
    row = policy_for_kind(effect_kind)
    assert "APPLIED" in row.reconcile_rule
    assert row.authority == "MUTATING"
    if effect_kind in MUTATING_GH_EFFECT_KINDS:
        assert effect_kind in RECONCILE_MATRIX


def test_apply_then_hang_through_write_transport_reconciles_applied_once(
    tmp_path: Path,
) -> None:
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    controller.queue_failure("apply_then_hang", seconds=60)
    bundle = assemble_write_gateway(tmp_path, controller, per_call_timeout_seconds=2.0)
    marker = build_opaque_trigger_marker(run_id=H.RUN_ID, cycle_number=9)
    effect = H.trigger_effect(marker)
    now = datetime.now(tz=UTC)
    with pytest.raises(AmbiguousWriteError):
        bundle.gateway.request_review(
            effect,
            run_id=H.RUN_ID,
            now=now,
            authorize=lambda: None,
        )
    reloaded = controller.reload()
    assert len(reloaded.consumed_failures) == 1
    assert reloaded.consumed_failures[0]["kind"] == "apply_then_hang"
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 1
    needle = html_comment_marker(marker)
    assert any(needle in str(comment.get("body") or "") for comment in reloaded.issue_comments)
    proof = bundle.gateway.reconcile_request_review(effect, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.APPLIED
    success = bundle.gateway.request_review(
        effect,
        run_id=H.RUN_ID,
        now=now,
        authorize=lambda: None,
    )
    assert success.already_applied is True
    final = controller.reload()
    assert final.mutation_counts.get("create_issue_comment", 0) == 1


def test_stateful_gh_write_reconcile_applied_exactly_once(tmp_path: Path) -> None:
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    bundle = assemble_write_gateway(tmp_path, controller)
    marker = build_opaque_trigger_marker(run_id=H.RUN_ID, cycle_number=1)
    effect = H.trigger_effect(marker)
    needle = html_comment_marker(marker)
    bundle.controller.state.issue_comments.append(
        {
            "id": "IC_999",
            "databaseId": 999,
            "body": f"@codex review\n{needle}",
            "createdAt": "2026-07-21T12:01:00Z",
            "author": {"login": "orchestrator"},
        }
    )
    bundle.controller.state.save(bundle.controller.state_path)
    proof = bundle.gateway.reconcile_request_review(
        effect, run_id=H.RUN_ID, now=datetime.now(tz=UTC)
    )
    assert proof.proof is WriteProofKind.APPLIED
    reloaded = bundle.controller.reload()
    assert reloaded.mutation_counts.get("create_issue_comment", 0) == 0
    assert len(reloaded.applied_markers) == 0


def test_stateful_fake_gh_thumbs_up_through_production_gateway(tmp_path: Path) -> None:
    controller = StatefulFakeGhController(tmp_path / "gh-state.json")
    controller.seed_waiting_observation(marker=MARKER, head_sha=SHA_B)
    controller.queue_thumbs_up()
    read_policy = policy(gh_command="gh", accept_bot_thumbs_up=True)
    _, executor = assemble_read_stack(tmp_path, controller, policy=read_policy)
    effect = observe_effect()
    event = executor.execute(effect, token_for(effect), now=datetime.now(tz=UTC))
    assert event.outcome.kind == "verified_no_findings"


def test_bare_git_remote_initializes_for_publication_boundary(tmp_path: Path) -> None:
    work, bare, parent = init_git_remote(tmp_path)
    assert work.is_dir()
    assert bare.is_dir()
    assert len(parent) == 40


def test_git_push_reconcile_production_boundary(tmp_path: Path) -> None:
    from tests.integration.test_phase16_6_git_writes import _gateway, _init_repo

    work, _bare, parent = _init_repo(tmp_path)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary"],
        cwd=str(work),
        check=True,
        capture_output=True,
    ).stdout
    artifact_root = tmp_path / "artifacts"
    patch_ref = H.write_artifact(artifact_root, H.RUN_ID, "artifacts/patch.bin", patch)
    message_ref = H.write_commit_message(artifact_root, H.RUN_ID, "publish feature", "body")
    git_repo = {
        "work": work,
        "parent": parent,
        "patch_ref": patch_ref,
        "message_ref": message_ref,
        "artifact_root": artifact_root,
    }
    gateway = _gateway(git_repo)
    now = datetime.now(tz=UTC)
    effect = H.commit_effect(
        git_repo["patch_ref"],
        git_repo["message_ref"],
        expected_head_sha=git_repo["parent"],
        bound_head_sha=git_repo["parent"],
    )
    committed = gateway.commit(effect, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    push = H.push_effect(
        commit_sha=committed.outcome.commit_sha,
        bound_head_sha=committed.outcome.new_head_sha,
        expected_remote_sha_before_push=committed.outcome.expected_remote_sha_before_push,
    )
    proof = gateway.reconcile_push(push, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.PROVEN_NOT_APPLIED
    gateway.push(push, run_id=H.RUN_ID, now=now, authorize=lambda: None)
    proof = gateway.reconcile_push(push, run_id=H.RUN_ID, now=now)
    assert proof.proof is WriteProofKind.APPLIED


def test_two_worker_lease_fence_blocks_stale_write(tmp_path: Path) -> None:
    from tests.integration.test_phase16_8_supervisor_restart import (
        test_lease_replacement_fences_stale_mutating_write,
    )

    test_lease_replacement_fences_stale_mutating_write(tmp_path)
