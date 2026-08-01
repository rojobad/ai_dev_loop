"""Engine/control integration for legacy mixed-adjudication recovery supersession."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.integration.test_phase16_8_control_matrix import _control_stack
from tests.unit.pr_review_v2.durable_helpers import FakeClock
from tests.unit.pr_review_v2.helpers import (
    HASH_2,
    SHA_A,
    T3,
    artifact,
    legacy_mixed_defect_adjudication,
)

from ai_dev_loop.launcher import read_process_pgid, read_process_starttime
from ai_dev_loop.pr_review_v2.application.contracts import EventDisposition, LeaseStatus
from ai_dev_loop.pr_review_v2.application.control import ControlError, ControlPlaneService
from ai_dev_loop.pr_review_v2.application.control_contracts import SafeNextAction
from ai_dev_loop.pr_review_v2.domain import (
    FrozenThreadSet,
    PostThreadReplyEffect,
    PullRequestBinding,
    SafeAction,
    SafeActionKind,
    StartRequested,
    TriggerEvidence,
    WaitingForUserState,
    deferred_reply_intents_from_evidence,
)
from ai_dev_loop.pr_review_v2.domain import (
    stable_effect_ids as domain_stable_effect_ids,
)
from ai_dev_loop.pr_review_v2.infrastructure.protected_result_store import (
    MIXED_ADJUDICATION_RECOVERY_DIR,
    ProtectedResultStore,
)
from ai_dev_loop.pr_review_v2.workers.supervisor import (
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
) -> tuple[ControlPlaneService, str, str]:
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
    return control, run_id, reply_effect_id


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
    control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
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
    control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
    status = control.status(run_id)
    assert status.state_kind == "waiting_for_user"
    assert status.effect_status == "pending"
    assert status.supervisor_live is False
    assert status.lease_active is False
    assert status.resumable is True
    assert status.next_action is SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_legacy_mixed_status_waits_when_supervisor_live(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
    launchers = SupervisorLauncherStore(tmp_path / "artifacts")
    _write_live_supervisor(launchers, run_id=run_id, clock=clock)
    status = control.status(run_id)
    assert status.supervisor_live is True
    assert status.resumable is False
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    assert status.next_action is not SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_legacy_mixed_status_waits_when_lease_active(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
    lease = control._engine.acquire_lease(run_id, "lingering-owner")  # noqa: SLF001
    assert lease.status is LeaseStatus.ACTIVE
    status = control.status(run_id)
    assert status.lease_active is True
    assert status.resumable is False
    assert status.next_action is SafeNextAction.WAIT_UNTIL
    assert status.next_action is not SafeNextAction.RESUME_RECOVER_MIXED_ADJUDICATION


def test_recovery_supersedes_pending_reply_and_inserts_readjudication(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC))
    control, run_id, reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
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
    control, run_id, _reply_effect_id = _seed_legacy_waiting_with_pending_reply(tmp_path, clock)
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
