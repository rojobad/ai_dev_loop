"""Unit tests for Phase 18 timeline projection and nullable controller provenance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests.unit.scheduler.helpers import (
    sample_agent_led_submitted_context,
    sample_submitted_state,
)

from ai_dev_loop.scheduler.application.timeline import SchedulerTimelineService
from ai_dev_loop.scheduler.domain.state import (
    ControllerBinding,
    submission_identity_payload,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def test_submission_identity_payload_ignores_controller_provenance() -> None:
    with_a = sample_agent_led_submitted_context()
    without_a = sample_agent_led_submitted_context()
    without_a = without_a.model_copy(
        update={"controller": ControllerBinding(controller_session_id=None)}
    )
    assert submission_identity_payload(with_a) == submission_identity_payload(without_a)


def test_nullable_controller_binding_validates() -> None:
    context = sample_agent_led_submitted_context().model_copy(
        update={"controller": ControllerBinding(controller_session_id=None)}
    )
    assert context.controller.controller_session_id is None


def test_timeline_duration_and_phase_attempts(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="timeline-run")
    from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent

    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
        launch = now
        complete = now + timedelta(seconds=42)
        launch_text = launch.isoformat().replace("+00:00", "Z")
        complete_text = complete.isoformat().replace("+00:00", "Z")
        for index, status in enumerate(("completed", "failed"), start=1):
            dispatch_id = f"dispatch-{index:02d}"
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, 'evt-submit', ?, ?, ?, ?, 'cursor.turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    index,
                    state.run_id,
                    dispatch_id,
                    f"{state.run_id}:{dispatch_id}",
                    "d" * 64,
                    launch_text,
                    launch_text,
                    launch_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"attempt-{index:02d}",
                    state.run_id,
                    dispatch_id,
                    "cursor",
                    1,
                    status,
                    launch_text,
                    complete_text if status == "completed" else launch_text,
                    launch_text,
                    complete_text if status == "completed" else None,
                ),
            )

    service = SchedulerTimelineService(store)
    timeline = service.get_timeline(state.run_id, limit=10, order="oldest")
    assert len(timeline.entries) == 2
    assert timeline.entries[0].phase_attempt == 1
    assert timeline.entries[1].phase_attempt == 2
    assert timeline.entries[0].observed_duration_seconds == 42.0
    assert timeline.entries[1].observed_duration_seconds is None
    assert timeline.entries[1].status == "failed"

    newest = service.get_timeline(state.run_id, limit=1, order="newest")
    assert newest.truncated is True
    assert newest.entries[0].phase_attempt == 2


def test_timeline_ordering_uses_consistent_chronological_keys(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="timeline-order-run")
    from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent

    base = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    attempts = (
        {
            "attempt_id": "attempt-zzz",
            "created_at": base + timedelta(seconds=30),
            "launch_requested_at": base + timedelta(seconds=20),
            "completed_at": base + timedelta(seconds=25),
            "status": "completed",
        },
        {
            "attempt_id": "attempt-aaa",
            "created_at": base + timedelta(seconds=10),
            "launch_requested_at": None,
            "completed_at": None,
            "status": "failed",
        },
        {
            "attempt_id": "attempt-mmm",
            "created_at": base + timedelta(seconds=5),
            "launch_requested_at": base + timedelta(seconds=15),
            "completed_at": base + timedelta(seconds=18),
            "status": "completed",
        },
        {
            "attempt_id": "attempt-bbb",
            "created_at": base + timedelta(seconds=40),
            "launch_requested_at": base + timedelta(seconds=12),
            "completed_at": None,
            "status": "active",
        },
    )

    def _iso(moment: datetime | None) -> str | None:
        if moment is None:
            return None
        return moment.isoformat().replace("+00:00", "Z")

    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=base,
        )
        for index, attempt in enumerate(attempts, start=1):
            dispatch_id = f"dispatch-{index:02d}"
            created_text = _iso(attempt["created_at"])
            conn.execute(
                """
                INSERT INTO scheduler_effects(
                    dispatch_id, source_event_id, effect_ordinal, run_id, effect_id,
                    idempotency_key, effect_kind, effect_payload, effect_payload_sha256,
                    status, available_at, claimed_run_version, created_at, updated_at
                ) VALUES (?, 'evt-submit', ?, ?, ?, ?, 'cursor.turn', '{}', ?, 'succeeded', ?, 1, ?, ?)
                """,
                (
                    dispatch_id,
                    index,
                    state.run_id,
                    dispatch_id,
                    f"{state.run_id}:{dispatch_id}",
                    "d" * 64,
                    created_text,
                    created_text,
                    created_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, 'cursor', 1, ?, ?, ?, ?, ?)
                """,
                (
                    attempt["attempt_id"],
                    state.run_id,
                    dispatch_id,
                    attempt["status"],
                    created_text,
                    created_text,
                    _iso(attempt["launch_requested_at"]),
                    _iso(attempt["completed_at"]),
                ),
            )

    service = SchedulerTimelineService(store)
    oldest = service.get_timeline(state.run_id, limit=10, order="oldest")
    assert [entry.phase_attempt for entry in oldest.entries] == [1, 2, 3, 4]
    assert oldest.entries[0].launch_requested_at is None
    assert oldest.entries[0].completed_at is None
    assert oldest.entries[0].observed_duration_seconds is None
    assert oldest.entries[1].observed_duration_seconds is None
    assert oldest.entries[2].observed_duration_seconds == 3.0
    assert oldest.entries[3].observed_duration_seconds == 5.0

    oldest_page = service.get_timeline(state.run_id, limit=2, order="oldest")
    assert oldest_page.truncated is True
    assert oldest_page.entries[0].phase_attempt == 1
    assert oldest_page.entries[1].phase_attempt == 2

    newest_page = service.get_timeline(state.run_id, limit=2, order="newest")
    assert newest_page.truncated is True
    assert newest_page.entries[0].phase_attempt == 4
    assert newest_page.entries[1].phase_attempt == 3
    assert newest_page.entries[0].observed_duration_seconds == 5.0
