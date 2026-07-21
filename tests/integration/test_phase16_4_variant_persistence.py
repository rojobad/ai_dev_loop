"""Store-level persistence round-trips for every Phase 16.3 state/event/effect variant."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.unit.pr_review_v2.durable_helpers import FakeClock, publication_success
from tests.unit.pr_review_v2.helpers import (
    HASH_2,
    SHA_A,
    SHA_B,
    actionable_adjudication,
    artifact,
    confirm_trigger,
    freeze_threads,
    publication_text_outcome,
    reply_adjudication,
    start,
    succeed,
    token_for,
)
from tests.unit.pr_review_v2.test_reducer_matrix import _build_states

from ai_dev_loop.pr_review_v2.application.contracts import EventDisposition
from ai_dev_loop.pr_review_v2.domain import (
    AbortRequested,
    AdjudicationRecordedOutcome,
    CommitRecordedOutcome,
    EffectBlocked,
    EffectRetryableFailure,
    EffectSucceeded,
    ErrorSummary,
    ExistingPrOrigin,
    FailureReasonKind,
    FatalFailureDetected,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PauseReasonKind,
    PrBoundOutcome,
    PreparedState,
    PrTextUpdatedOutcome,
    PullRequestBinding,
    PushConfirmedOutcome,
    RepositoryIdentity,
    ResumeRequested,
    RetryDue,
    SafeAction,
    SafeActionKind,
    SourceRunOrigin,
    StartRequested,
    ThreadResolutionConfirmedOutcome,
    TransientErrorKind,
    UserContinuationEvidence,
    UserContinuationRequested,
    WorkflowLimits,
    WriteOutcomeUncertain,
    active_effect,
)
from ai_dev_loop.pr_review_v2.domain.reducer import EVENT_KINDS, STATE_KINDS
from ai_dev_loop.pr_review_v2.infrastructure.sqlite_store import SqlitePrReviewStore

EXPECTED_EFFECT_KINDS = frozenset(
    {
        "generate_publication_text",
        "commit_patch",
        "push_commit",
        "create_or_update_pr",
        "request_bot_review",
        "observe_bot_review",
        "adjudicate_threads",
        "post_thread_reply",
        "run_local_fix",
        "update_pr_text",
        "resolve_thread",
        "reconcile_write",
    }
)


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
            accepted_patch=artifact("artifacts/accepted.patch"),
            execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
        ),
        limits=WorkflowLimits(max_external_cycles=2, max_local_iterations=3),
        entered_at=FakeClock().now(),
    )


@pytest.fixture
def prepared_existing(prepared: PreparedState) -> PreparedState:
    binding = PullRequestBinding(
        repository=prepared.origin.repository,
        pr_number=42,
        head_branch="feature",
        base_branch="main",
        head_sha=SHA_A,
    )
    return PreparedState(
        run_id="run-existing",
        origin=ExistingPrOrigin(
            binding=binding,
            execution_context_ref=artifact("artifacts/execution-context.json", HASH_2),
        ),
        limits=prepared.limits,
        entered_at=FakeClock().now(),
    )


def _collect_effect_samples(prepared: PreparedState) -> dict[str, object]:
    by_kind: dict[str, object] = {}
    state, effects = start(prepared)
    by_kind.setdefault(effects[0].kind, effects[0])
    state, effects = succeed(state, effects[0], publication_text_outcome())
    by_kind.setdefault(effects[0].kind, effects[0])
    state, effects = succeed(
        state, effects[0], CommitRecordedOutcome(commit_sha=SHA_B, new_head_sha=SHA_B)
    )
    by_kind.setdefault(effects[0].kind, effects[0])
    state, effects = succeed(
        state, effects[0], PushConfirmedOutcome(commit_sha=SHA_B, remote_ref="feature")
    )
    by_kind.setdefault(effects[0].kind, effects[0])
    binding = PullRequestBinding(
        repository=prepared.origin.repository,
        pr_number=7,
        head_branch=prepared.origin.head_branch,
        base_branch=prepared.origin.base_branch,
        head_sha=SHA_B,
    )
    state, effects = succeed(state, effects[0], PrBoundOutcome(binding=binding))
    by_kind.setdefault(effects[0].kind, effects[0])

    waiting_obs, obs_effects = confirm_trigger(state, effects[0], binding)
    by_kind.setdefault(obs_effects[0].kind, obs_effects[0])
    adjudicating, adj_effects = freeze_threads(
        waiting_obs, obs_effects[0], binding, ("thread-a", "thread-b")
    )
    by_kind.setdefault(adj_effects[0].kind, adj_effects[0])
    running, run_effects = succeed(
        adjudicating,
        adj_effects[0],
        AdjudicationRecordedOutcome(evidence=actionable_adjudication(adjudicating.frozen)),
    )
    by_kind.setdefault(run_effects[0].kind, run_effects[0])
    publishing_fix, fix_effects = succeed(
        running,
        run_effects[0],
        LocalFixFinishedOutcome(
            outcome=LocalFixOutcomeKind.ACCEPTED,
            accepted_patch_ref=artifact("artifacts/fix.patch"),
            new_head_sha="e" * 40,
            result_ref=artifact("artifacts/local-result.json"),
        ),
    )
    by_kind.setdefault(fix_effects[0].kind, fix_effects[0])
    cur, cur_effects = succeed(publishing_fix, fix_effects[0], publication_text_outcome())
    by_kind.setdefault(cur_effects[0].kind, cur_effects[0])
    new_sha = "e" * 40
    cur, cur_effects = succeed(
        cur, cur_effects[0], CommitRecordedOutcome(commit_sha=new_sha, new_head_sha=new_sha)
    )
    by_kind.setdefault(cur_effects[0].kind, cur_effects[0])
    cur, cur_effects = succeed(
        cur,
        cur_effects[0],
        PushConfirmedOutcome(commit_sha=new_sha, remote_ref=cur.binding.head_branch),
    )
    by_kind.setdefault(cur_effects[0].kind, cur_effects[0])
    cur, cur_effects = succeed(
        cur,
        cur_effects[0],
        PrTextUpdatedOutcome(
            binding=cur.binding,
            publication_text_ref=cur.publication_text_ref or artifact("artifacts/publication.md"),
        ),
    )
    by_kind.setdefault(cur_effects[0].kind, cur_effects[0])
    while cur.kind == "publishing_fix" and cur_effects:
        effect = cur_effects[0]
        by_kind.setdefault(effect.kind, effect)
        if effect.kind != "resolve_thread":
            break
        cur, cur_effects = succeed(
            cur,
            effect,
            ThreadResolutionConfirmedOutcome(thread_id=effect.thread_id),
        )
        for nxt in cur_effects:
            by_kind.setdefault(nxt.kind, nxt)

    waiting_u, obs_u = confirm_trigger(state, effects[0], binding)
    adjudicating_u, adj_u = freeze_threads(waiting_u, obs_u[0], binding, ("t1", "t2"))
    waiting_user, reply_effects = succeed(
        adjudicating_u,
        adj_u[0],
        AdjudicationRecordedOutcome(evidence=reply_adjudication(adjudicating_u.frozen)),
    )
    for effect in reply_effects:
        by_kind.setdefault(effect.kind, effect)
    active = active_effect(waiting_user)
    if active is not None:
        by_kind.setdefault(active.kind, active)
    return by_kind


def test_every_state_variant_persists_via_snapshot_cas(
    tmp_path: Path,
    clock: FakeClock,
    prepared: PreparedState,
    prepared_existing: PreparedState,
) -> None:
    states = _build_states(prepared, prepared_existing)
    assert set(states) == set(STATE_KINDS)

    for kind, state in states.items():
        # One DB per kind so native run_ids (including nested effects) stay consistent.
        store = SqlitePrReviewStore(tmp_path / f"state-{kind}.sqlite3")
        seed = PreparedState(
            run_id=state.run_id,
            origin=state.origin,
            limits=state.limits,
            entered_at=clock.now(),
        )
        with store.begin_immediate() as conn:
            store.insert_prepared_run(conn, run_id=state.run_id, state=seed, now=clock.now())
            if kind != "prepared":
                store.cas_update_snapshot(
                    conn,
                    run_id=state.run_id,
                    observed_version=1,
                    new_state=state,
                    now=clock.now(),
                )

        store2 = SqlitePrReviewStore(tmp_path / f"state-{kind}.sqlite3")
        with store2.begin_read() as conn:
            loaded, version, _ = store2.load_validated_snapshot(conn, state.run_id)
        assert loaded.kind == kind
        assert version >= 1
        if kind == "prepared":
            assert loaded == seed
        else:
            assert loaded == state


def test_every_event_variant_persists_and_reloads(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState, prepared_existing: PreparedState
) -> None:
    db_path = tmp_path / "events.sqlite3"
    store = SqlitePrReviewStore(db_path)
    states = _build_states(prepared, prepared_existing)
    pub_effect = active_effect(states["publishing_initial"])
    assert pub_effect is not None
    tok = token_for(pub_effect)
    mutating = states["reconciling_write"].active_effect.original_write
    mut_tok = token_for(mutating)

    event_samples = {
        "start_requested": StartRequested(occurred_at=clock.now()),
        "resume_requested": ResumeRequested(occurred_at=clock.now()),
        "abort_requested": AbortRequested(occurred_at=clock.now()),
        "fatal_failure_detected": FatalFailureDetected(
            occurred_at=clock.now(),
            reason=FailureReasonKind.INTERNAL_CORRUPTION,
            safe_summary="corrupt",
        ),
        "user_continuation_requested": UserContinuationRequested(
            occurred_at=clock.now(),
            evidence=UserContinuationEvidence(
                repository=prepared.origin.repository,
                pr_number=7,
                cycle_number=1,
                head_sha=SHA_B,
                evidence_ref=artifact("u.json"),
            ),
        ),
        "effect_succeeded": EffectSucceeded(
            occurred_at=clock.now(),
            token=tok,
            outcome=publication_success(pub_effect, tok, clock.now()).outcome,
        ),
        "effect_retryable_failure": EffectRetryableFailure(
            occurred_at=clock.now(),
            token=tok,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="t"),
            failed_attempt=1,
            next_attempt_at=clock.now() + timedelta(seconds=10),
        ),
        "effect_blocked": EffectBlocked(
            occurred_at=clock.now(),
            token=tok,
            reason=PauseReasonKind.AUTHENTICATION,
            safe_action=SafeAction(kind=SafeActionKind.FIX_AUTH_THEN_RESUME, condition="fix auth"),
            safe_summary="auth",
        ),
        "write_outcome_uncertain": WriteOutcomeUncertain(
            occurred_at=clock.now(),
            token=mut_tok,
            error=ErrorSummary(kind=TransientErrorKind.TIMEOUT, safe_summary="ambiguous"),
            reconciliation_identity="rec-1",
            original_write=mutating,
        ),
        "retry_due": RetryDue(
            occurred_at=clock.now(),
            pending_effect_id=states["waiting_retry"].retrying_effect_id,
            current_time=clock.now(),
        ),
    }
    assert set(event_samples) == set(EVENT_KINDS)

    host = prepared.model_copy(update={"run_id": "event-host"})
    with store.begin_immediate() as conn:
        store.insert_prepared_run(conn, run_id="event-host", state=host, now=clock.now())
        for idx, (kind, event) in enumerate(event_samples.items(), start=1):
            store.insert_event_row(
                conn,
                event_id=f"evt-{kind}",
                run_id="event-host",
                sequence=idx,
                event=event,
                disposition=EventDisposition.ACCEPTED.value,
                expected_run_version=1,
                observed_run_version=1,
                resulting_run_version=2,
                rejection_code=None,
                safe_detail=None,
                now=clock.now(),
                resulting_state=host,
            )

    store2 = SqlitePrReviewStore(db_path)
    with store2.begin_read() as conn:
        for kind, original in event_samples.items():
            row = conn.execute(
                "SELECT event_kind, event_payload FROM pr_review_events WHERE event_id=?",
                (f"evt-{kind}",),
            ).fetchone()
            assert row["event_kind"] == kind
            restored = store2.load_event(row["event_payload"])
            assert restored == original


def test_every_effect_variant_persists_and_reloads(
    tmp_path: Path, clock: FakeClock, prepared: PreparedState, prepared_existing: PreparedState
) -> None:
    states = _build_states(prepared, prepared_existing)
    by_kind = _collect_effect_samples(prepared)
    for state in states.values():
        effect = active_effect(state)
        if effect is not None:
            by_kind.setdefault(effect.kind, effect)
        for attr in ("suspended", "resumable"):
            nested_state = getattr(state, attr, None)
            if nested_state is not None:
                nested = active_effect(nested_state)
                if nested is not None:
                    by_kind.setdefault(nested.kind, nested)
        if state.kind == "reconciling_write":
            by_kind.setdefault("reconcile_write", state.active_effect)
            by_kind.setdefault(
                state.active_effect.original_write.kind, state.active_effect.original_write
            )

    missing = EXPECTED_EFFECT_KINDS - set(by_kind)
    assert not missing, f"missing effect kinds: {missing}"

    for kind in EXPECTED_EFFECT_KINDS:
        effect = by_kind[kind]
        run_id = f"effect-{kind}"
        host = prepared.model_copy(update={"run_id": run_id})
        db = tmp_path / f"effect-{kind}.sqlite3"
        store = SqlitePrReviewStore(db)
        with store.begin_immediate() as conn:
            store.insert_prepared_run(conn, run_id=run_id, state=host, now=clock.now())
            store.insert_event_row(
                conn,
                event_id=f"src-{kind}",
                run_id=run_id,
                sequence=1,
                event=StartRequested(occurred_at=clock.now()),
                disposition=EventDisposition.ACCEPTED.value,
                expected_run_version=1,
                observed_run_version=1,
                resulting_run_version=2,
                rejection_code=None,
                safe_detail=None,
                now=clock.now(),
                resulting_state=host,
            )
            if effect.kind == "reconcile_write":
                patched = effect.model_copy(
                    update={
                        "run_id": run_id,
                        "original_write": effect.original_write.model_copy(
                            update={"run_id": run_id}
                        ),
                    }
                )
            else:
                patched = effect.model_copy(update={"run_id": run_id})
            store.insert_effect_dispatches(
                conn,
                source_event_id=f"src-{kind}",
                run_id=run_id,
                effects=(patched,),
                now=clock.now(),
                claimed_run_version=1,
            )

        store2 = SqlitePrReviewStore(db)
        with store2.begin_read() as conn:
            row = conn.execute(
                "SELECT * FROM pr_review_effects WHERE source_event_id=?",
                (f"src-{kind}",),
            ).fetchone()
            assert row is not None, kind
            assert row["effect_kind"] == kind
            restored = store2.load_validated_effect(row)
            assert restored.kind == kind
            assert restored.run_id == run_id
