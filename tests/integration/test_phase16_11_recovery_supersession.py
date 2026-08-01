"""Engine/control integration for legacy mixed-adjudication recovery supersession."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from tests.integration.phase16_4_matrix_helpers import claim_next, complete_ok
from tests.integration.test_phase16_8_control_matrix import _control_stack
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import (
    HASH_2,
    SHA_A,
    T3,
    artifact,
    legacy_mixed_defect_adjudication,
    mixed_adjudication,
)

from ai_dev_loop.launcher import read_process_pgid, read_process_starttime
from ai_dev_loop.pr_review_v2.application.contracts import EventDisposition, LeaseStatus
from ai_dev_loop.pr_review_v2.application.control import ControlError, ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import SafeNextAction
from ai_dev_loop.pr_review_v2.domain import (
    AdjudicationRecordedOutcome,
    EffectSucceeded,
    FrozenThreadSet,
    LocalFixFinishedOutcome,
    LocalFixOutcomeKind,
    PostThreadReplyEffect,
    PullRequestBinding,
    SafeAction,
    SafeActionKind,
    StartRequested,
    ThreadReplyConfirmedOutcome,
    TriggerEvidence,
    WaitingForUserState,
    awaiting_operator_continuation,
    deferred_reply_intents_from_evidence,
    pending_deferred_reply_dispatch,
)
from ai_dev_loop.pr_review_v2.domain import (
    stable_effect_ids as domain_stable_effect_ids,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    MIXED_ADJUDICATION_RECOVERY_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import (
    PrReviewV2Supervisor,
    SupervisorLauncherMetadata,
    SupervisorLauncherStore,
    _read_process_executable,
)


def _build_legacy_waiting_state(state, *, binding: PullRequestBinding) -> WaitingForUserState:
    frozen = FrozenThreadSet(
        thread_ids=("r1", "a1", "a2"),
        snapshot_ref=artifact("snap-cycle3.json"),
        head_sha=binding.head_sha,
        cycle_number=3,
        trigger_marker="marker-recover",
    )
    legacy = legacy_mixed_defect_adjudication(frozen)
    replies = deferred_reply_intents_from_evidence(legacy)
    head = replies[0]
    reply_id, reply_idem = domain_stable_effect_ids(
        run_id=state.run_id,
        cycle_number=frozen.cycle_number,
        operation="post_thread_reply",
        target=head.thread_id,
    )
    reply = PostThreadReplyEffect(
        effect_id=reply_id,
        idempotency_key=reply_idem,
        run_id=state.run_id,
        cycle_number=frozen.cycle_number,
        attempt=1,
        max_attempts=state.limits.github_max_attempts_per_batch,
        repository=binding.repository,
        bound_head_sha=binding.head_sha,
        binding=binding,
        thread_id=head.thread_id,
        reply_ref=head.reply_ref,
    )
    trigger = TriggerEvidence(
        marker="marker-recover",
        comment_ref=artifact("trigger.json", HASH_2),
        head_sha=binding.head_sha,
    )
    return WaitingForUserState(
        run_id=state.run_id,
        origin=state.origin,
        limits=state.limits,
        binding=binding,
        cycle_number=frozen.cycle_number,
        entered_at=T3,
        adjudication=legacy,
        remaining_replies=replies,
        active_effect=reply,
        safe_action=SafeAction(
            kind=SafeActionKind.CONTINUE_AFTER_USER_REPLY,
            condition="post remaining replies then continue observation",
        ),
        trigger_evidence=trigger,
    )


def _seed_legacy_waiting_with_pending_reply(
    tmp_path: Path,
    clock: FakeClock,
) -> tuple:
    engine, _control, run_id = _control_stack(tmp_path, clock)
    _control.start(run_id)
    with engine.store.begin_read() as conn:
        state, version, _ = engine.store.load_validated_snapshot(conn, run_id)
    binding = PullRequestBinding(
        repository=state.origin.repository,
        pr_number=7,
        head_branch=state.origin.head_branch,
        base_branch=state.origin.base_branch,
        head_sha=SHA_A,
    )
    waiting = _build_legacy_waiting_state(state, binding=binding)
    assert waiting.active_effect is not None
    reply_effect_id = waiting.active_effect.effect_id
    now = clock.now()
    store = engine.store
    with store.begin_immediate() as conn:
        store.insert_event_row(
            conn,
            event_id="evt-reply-only",
            run_id=run_id,
            sequence=store.next_event_sequence(conn, run_id),
            event=StartRequested(occurred_at=now),
            disposition=EventDisposition.ACCEPTED.value,
            expected_run_version=version,
            observed_run_version=version,
            resulting_run_version=version + 1,
            rejection_code=None,
            safe_detail=None,
            now=now,
            resulting_state=waiting,
        )
        store.cas_update_snapshot(
            conn,
            run_id=run_id,
            observed_version=version,
            new_state=waiting,
            now=now,
        )
        store.cancel_live_work(conn, run_id=run_id, now=now)
        store.insert_effect_dispatches(
            conn,
            source_event_id="evt-reply-only",
            run_id=run_id,
            effects=(waiting.active_effect,),
            now=now,
            claimed_run_version=version + 1,
        )
    arts = ProtectedResultStore(tmp_path / "artifacts")
    control = ControlPlaneService(
        engine,
        artifact_store=arts,
        launcher_store=SupervisorLauncherStore(arts.root),
        spawner=lambda _rid: "spawned",
    )
    return engine, control, run_id, reply_effect_id


def _effect_rows(store, run_id: str) -> list[dict[str, str]]:
    with store.begin_read() as conn:
        rows = conn.execute(
            """
            SELECT effect_id, effect_kind, status
            FROM pr_review_effects
            WHERE run_id = ?
            ORDER BY created_at ASC, dispatch_id ASC
            """,
            (run_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _recovery_artifact_count(artifact_root: Path, run_id: str) -> int:
    recovery_dir = artifact_root / run_id / MIXED_ADJUDICATION_RECOVERY_DIR
    if not recovery_dir.is_dir():
        return 0
    return sum(1 for path in recovery_dir.iterdir() if path.is_file())


def _write_live_supervisor(
    launchers: SupervisorLauncherStore,
    *,
    run_id: str,
    clock: FakeClock,
) -> None:
    pid = os.getpid()
    starttime = read_process_starttime(pid)
    executable = _read_process_executable(pid)
    assert starttime is not None
    assert executable is not None
    launchers.write(
        SupervisorLauncherMetadata(
            schema_version=1,
            run_id=run_id,
            token="status-test-supervisor",
            pid=pid,
            pgid=read_process_pgid(pid),
            process_start_time=str(starttime),
            executable=executable,
            created_at=clock.now().isoformat(),
        )
    )


@pytest.mark.parametrize(
    ("dispatch_status", "claim_fields"),
    [
        ("claimed", {"status": "claimed", "claim_id": "claim-1", "claim_owner_id": "owner"}),
        ("retry_wait", {"status": "retry_wait"}),
    ],
)
def test_legacy_mixed_status_does_not_advertise_recovery_for_non_pending_dispatch(
    tmp_path: Path,
    dispatch_status: str,
    claim_fields: dict[str, str],
) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    store = control._engine.store  # noqa: SLF001
    set_clause = ", ".join(f"{key} = ?" for key in claim_fields)
    values = list(claim_fields.values()) + [run_id]
    with store.begin_immediate() as conn:
        conn.execute(
            f"""
            UPDATE pr_review_effects
            SET {set_clause}
            WHERE run_id = ? AND status = 'pending'
            """,
            values,
        )
    status = control.status(run_id)
    assert status.state_kind == "waiting_for_user"
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    assert status.next_action is not SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION
    assert status.resumable is True
    arts_root = tmp_path / "artifacts"
    before = _recovery_artifact_count(arts_root, run_id)
    with pytest.raises(ControlError) as exc_info:
        control.resume(run_id, recover_mixed_adjudication=True)
    assert exc_info.value.kind.value == "not_resumable"
    assert _recovery_artifact_count(arts_root, run_id) == before
    rows = _effect_rows(store, run_id)
    assert not any(row["effect_kind"] == "adjudicate_threads" for row in rows)


def test_legacy_mixed_status_advertises_recovery_for_pending_unclaimed_reply(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    status = control.status(run_id)
    assert status.state_kind == "waiting_for_user"
    assert status.effect_status == "pending"
    assert status.supervisor_live is False
    assert status.lease_active is False
    assert status.resumable is True
    assert status.next_action is SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_legacy_mixed_status_waits_when_supervisor_live(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    launchers = SupervisorLauncherStore(tmp_path / "artifacts")
    _write_live_supervisor(launchers, run_id=run_id, clock=clock)
    status = control.status(run_id)
    assert status.supervisor_live is True
    assert status.resumable is False
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    assert status.next_action is not SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_legacy_mixed_status_waits_when_lease_active(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    lease = control._engine.acquire_lease(run_id, "lingering-owner")  # noqa: SLF001
    assert lease.status is LeaseStatus.ACTIVE
    status = control.status(run_id)
    assert status.lease_active is True
    assert status.resumable is False
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    assert status.next_action is not SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_recovery_supersedes_pending_reply_and_inserts_readjudication(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    result = control.resume(run_id, recover_mixed_adjudication=True)
    assert result.transition_applied is True
    assert result.state_kind == "adjudicating"
    rows = _effect_rows(control._engine.store, run_id)  # noqa: SLF001
    live = [row for row in rows if row["status"] in {"pending", "claimed"}]
    assert len(live) == 1
    assert live[0]["effect_kind"] == "adjudicate_threads"
    superseded = [row for row in rows if row["effect_id"] == reply_effect_id]
    assert len(superseded) == 1
    assert superseded[0]["status"] == "superseded"
    with pytest.raises(ControlError):
        control.resume(run_id, recover_mixed_adjudication=True)


def test_recovery_rejects_claimed_reply_dispatch(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    _engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    store = control._engine.store  # noqa: SLF001
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE pr_review_effects
            SET status = 'claimed', claim_id = 'claim-1', claim_owner_id = 'owner'
            WHERE run_id = ? AND status = 'pending'
            """,
            (run_id,),
        )
    with pytest.raises(ControlError):
        control.resume(run_id, recover_mixed_adjudication=True)
    rows = _effect_rows(store, run_id)
    assert any(row["status"] == "claimed" for row in rows)
    assert not any(row["effect_kind"] == "adjudicate_threads" for row in rows)


def _drive_recovered_remediation_to_deferred_reply(
    engine,
    run_id: str,
    clock: FakeClock,
) -> WaitingForUserState:
    """Complete re-adjudication, local fix, publication, and resolution via engine claims."""

    lease, claim = claim_next(engine, run_id)
    with engine.store.begin_read() as conn:
        state, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    assert state.kind == "adjudicating"
    frozen = state.frozen
    complete_ok(
        engine,
        lease,
        claim,
        "readjudicate-mixed",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=AdjudicationRecordedOutcome(evidence=mixed_adjudication(frozen)),
        ),
    )
    lease, claim = claim_next(engine, run_id)
    new_head = "f" * 40
    complete_ok(
        engine,
        lease,
        claim,
        "local-fix-accepted",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=claim.completion_token,
            outcome=LocalFixFinishedOutcome(
                outcome=LocalFixOutcomeKind.ACCEPTED,
                accepted_patch_ref=artifact("artifacts/fix.patch"),
                new_head_sha=new_head,
                result_ref=artifact("artifacts/local-result.json"),
            ),
        ),
    )
    with engine.store.begin_read() as conn:
        state, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    while state.kind == "publishing_fix":
        lease, claim = claim_next(engine, run_id)
        if claim.effect.kind == "generate_publication_text":
            from tests.unit.pr_review_v2.helpers import publication_text_outcome

            outcome = publication_text_outcome()
        elif claim.effect.kind == "commit_patch":
            from ai_dev_loop.pr_review_v2.domain import CommitRecordedOutcome

            outcome = CommitRecordedOutcome(
                commit_sha=new_head,
                new_head_sha=new_head,
                expected_remote_sha_before_push=state.old_head_sha,
            )
        elif claim.effect.kind == "push_commit":
            from ai_dev_loop.pr_review_v2.domain import PushConfirmedOutcome

            outcome = PushConfirmedOutcome(
                commit_sha=new_head, remote_ref=state.binding.head_branch
            )
        elif claim.effect.kind == "update_pr_text":
            from ai_dev_loop.pr_review_v2.domain import PrTextUpdatedOutcome

            outcome = PrTextUpdatedOutcome(
                binding=state.binding.model_copy(update={"head_sha": new_head}),
                publication_text_ref=state.publication_text_ref
                or artifact("artifacts/publication.md"),
            )
        elif claim.effect.kind == "resolve_thread":
            from ai_dev_loop.pr_review_v2.domain import ThreadResolutionConfirmedOutcome

            outcome = ThreadResolutionConfirmedOutcome(thread_id=claim.effect.thread_id)
        else:
            raise AssertionError(f"unexpected publishing_fix effect {claim.effect.kind}")
        complete_ok(
            engine,
            lease,
            claim,
            f"fix-pub-{claim.dispatch_id}-{claim.effect.kind}",
            EffectSucceeded(
                occurred_at=clock.now(),
                token=claim.completion_token,
                outcome=outcome,
            ),
        )
        with engine.store.begin_read() as conn:
            state, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    assert isinstance(state, WaitingForUserState)
    assert pending_deferred_reply_dispatch(state) is not None
    assert state.deferred_context is not None
    assert state.binding.head_sha == new_head
    assert state.active_effect is not None
    assert state.active_effect.bound_head_sha == new_head
    return state


def test_legacy_mixed_full_recovery_through_deferred_reply_and_continuation(
    tmp_path: Path,
) -> None:
    """Phase 16.12 validation trace: recovery -> remediation -> deferred reply -> continue."""

    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    store = engine.store
    with store.begin_read() as conn:
        event_count_before = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
        effect_count_before = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_effects WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]

    recover = control.resume(run_id, recover_mixed_adjudication=True)
    assert recover.transition_applied is True
    assert recover.state_kind == "adjudicating"

    rows_after_recovery = _effect_rows(store, run_id)
    live_after_recovery = [
        row for row in rows_after_recovery if row["status"] in {"pending", "claimed"}
    ]
    assert len(live_after_recovery) == 1
    assert live_after_recovery[0]["effect_kind"] == "adjudicate_threads"
    assert not any(
        row["effect_kind"] == "post_thread_reply" and row["status"] == "pending"
        for row in rows_after_recovery
    )

    waiting = _drive_recovered_remediation_to_deferred_reply(engine, run_id, clock)
    assert waiting.cycle_number == 3
    assert waiting.remaining_replies[0].thread_id == "r1"

    clock.advance(timedelta(seconds=31))
    status = control.status(run_id)
    assert status.state_kind == "waiting_for_user"
    assert status.next_action is SafeNextAction.RESUME
    assert status.next_action is not SafeNextAction.RESUME_CONFIRM_USER_CONTINUATION
    assert status.resumable is True

    spawn_calls: list[str] = []
    control_with_spawn = ControlPlaneService(
        engine,
        artifact_store=ProtectedResultStore(tmp_path / "artifacts"),
        launcher_store=SupervisorLauncherStore(tmp_path / "artifacts"),
        spawner=lambda rid: spawn_calls.append(rid) or "spawned",
    )
    resumed = control_with_spawn.resume(run_id)
    assert resumed.transition_applied is False
    assert resumed.supervisor_action == "spawned"
    assert spawn_calls == [run_id]

    pending_before = [
        row
        for row in _effect_rows(store, run_id)
        if row["effect_kind"] == "post_thread_reply" and row["status"] == "pending"
    ]
    control_with_spawn.resume(run_id)
    pending_after = [
        row
        for row in _effect_rows(store, run_id)
        if row["effect_kind"] == "post_thread_reply" and row["status"] == "pending"
    ]
    assert pending_before == pending_after
    assert len(pending_after) == 1

    lease, reply_claim = claim_next(engine, run_id)
    assert reply_claim.effect.kind == "post_thread_reply"
    reply_effect_id = reply_claim.effect.effect_id
    complete_ok(
        engine,
        lease,
        reply_claim,
        "dispatch-deferred-reply",
        EffectSucceeded(
            occurred_at=clock.now(),
            token=reply_claim.completion_token,
            outcome=ThreadReplyConfirmedOutcome(
                thread_id=reply_claim.effect.thread_id,
                reply_ref=reply_claim.effect.reply_ref,
            ),
        ),
    )
    with engine.store.begin_read() as conn:
        state, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    assert isinstance(state, WaitingForUserState)
    assert awaiting_operator_continuation(state)
    assert pending_deferred_reply_dispatch(state) is None

    status_after_reply = control_with_spawn.status(run_id)
    assert status_after_reply.next_action is SafeNextAction.RESUME_CONFIRM_USER_CONTINUATION

    continued = control_with_spawn.resume(run_id, confirm_user_continuation=True)
    assert continued.transition_applied is True
    assert engine.get_status(run_id).state_kind == "waiting_for_bot"

    with store.begin_read() as conn:
        event_count_after = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
        effect_count_after = conn.execute(
            "SELECT COUNT(*) AS n FROM pr_review_effects WHERE run_id = ?",
            (run_id,),
        ).fetchone()["n"]
    assert event_count_after > event_count_before
    assert effect_count_after > effect_count_before
    rows_final = _effect_rows(store, run_id)
    readjudicate_rows = [row for row in rows_final if row["effect_kind"] == "adjudicate_threads"]
    assert len(readjudicate_rows) == 1
    reply_rows = [row for row in rows_final if row["effect_id"] == reply_effect_id]
    assert len(reply_rows) == 2
    assert sum(1 for row in reply_rows if row["status"] == "superseded") == 1
    assert sum(1 for row in reply_rows if row["status"] == "succeeded") == 1


def test_supervisor_continues_for_pending_deferred_reply(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    control.resume(run_id, recover_mixed_adjudication=True)
    _drive_recovered_remediation_to_deferred_reply(engine, run_id, clock)
    clock.advance(timedelta(seconds=31))

    class _ReplyWorker:
        def __init__(self, eng) -> None:  # noqa: ANN001
            self._engine = eng
            self.completed = False

        def run_once(self, run_id: str):
            lease = self._engine.acquire_lease(run_id, "sup-worker")
            result = self._engine.claim_next_effect(run_id, "sup-worker", lease.generation)
            if result.claim is None:
                from ai_dev_loop.pr_review_v2.application.contracts import WorkerStepResult

                return WorkerStepResult(run_id=run_id, claimed=False, completed=False)
            claim = result.claim
            assert claim.effect.kind == "post_thread_reply"
            complete_ok(
                self._engine,
                lease,
                claim,
                "sup-reply",
                EffectSucceeded(
                    occurred_at=clock.now(),
                    token=claim.completion_token,
                    outcome=ThreadReplyConfirmedOutcome(
                        thread_id=claim.effect.thread_id,
                        reply_ref=claim.effect.reply_ref,
                    ),
                ),
                owner="sup-worker",
            )
            self.completed = True
            from ai_dev_loop.pr_review_v2.application.contracts import WorkerStepResult

            return WorkerStepResult(
                run_id=run_id,
                claimed=True,
                completed=True,
                effect_kind="post_thread_reply",
            )

    worker = _ReplyWorker(engine)
    supervisor = PrReviewV2Supervisor(
        engine,
        worker,  # type: ignore[arg-type]
        run_id=run_id,
        max_steps=5,
        clock=clock,
    )
    exit_kind = supervisor.run_until_idle()
    assert worker.completed is True
    assert exit_kind == "waiting_for_user"
    with engine.store.begin_read() as conn:
        state, _, _ = engine.store.load_validated_snapshot(conn, run_id)
    assert isinstance(state, WaitingForUserState)
    assert awaiting_operator_continuation(state)


def test_deferred_reply_waits_for_future_eligibility_before_dispatch(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    engine, control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(
        tmp_path, clock
    )
    control.resume(run_id, recover_mixed_adjudication=True)
    _drive_recovered_remediation_to_deferred_reply(engine, run_id, clock)
    clock.advance(timedelta(seconds=31))

    store = engine.store
    future_at = clock.now() + timedelta(seconds=120)
    from ai_dev_loop.pr_review_v2.infrastructure.runtime import encode_utc_instant

    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE pr_review_effects
            SET available_at = ?
            WHERE run_id = ? AND status = 'pending' AND effect_kind = 'post_thread_reply'
            """,
            (encode_utc_instant(future_at), run_id),
        )

    status_before = control.status(run_id)
    assert status_before.state_kind == "waiting_for_user"
    assert status_before.next_action is SafeNextAction.WAIT_UNTIL
    assert status_before.resumable is False
    assert status_before.next_eligible_at == future_at

    spawn_calls: list[str] = []
    control_with_spawn = ControlPlaneService(
        engine,
        artifact_store=ProtectedResultStore(tmp_path / "artifacts"),
        launcher_store=SupervisorLauncherStore(tmp_path / "artifacts"),
        spawner=lambda rid: spawn_calls.append(rid) or "spawned",
    )
    with pytest.raises(ControlError) as exc_info:
        control_with_spawn.resume(run_id)
    assert exc_info.value.kind.value == "not_resumable"
    assert spawn_calls == []

    lease = engine.acquire_lease(run_id, "eligibility-check")
    claim_result = engine.claim_next_effect(run_id, "eligibility-check", lease.generation)
    assert claim_result.claim is None

    clock.set(future_at)
    status_after = control_with_spawn.status(run_id)
    assert status_after.next_action is SafeNextAction.RESUME
    assert status_after.resumable is True

    resumed = control_with_spawn.resume(run_id)
    assert resumed.supervisor_action == "spawned"
    assert spawn_calls == [run_id]
