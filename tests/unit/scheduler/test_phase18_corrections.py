"""Regression tests for Phase 18 Codex correction findings."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from tests.unit.scheduler.helpers import (
    CONTROLLER_SESSION,
    sample_agent_led_submitted_context,
    sample_submitted_state,
)

from ai_dev_loop.commands.controller import controller_status, render_controller_status
from ai_dev_loop.scheduler.application.contracts import (
    DEFAULT_TIMELINE_LIMIT,
    HARD_TIMELINE_MAX,
    review_budget_from_state,
)
from ai_dev_loop.scheduler.application.controller_read import _candidate_from_state
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.application.submission import (
    _legacy_submission_idempotency_key,
)
from ai_dev_loop.scheduler.application.timeline import SchedulerTimelineService
from ai_dev_loop.scheduler.domain.events import (
    CodexReviewBlockedEvent,
    RunSubmittedEvent,
    WaitingForCursorFixEnteredEvent,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_codex_review_blocked,
)
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    BlockedState,
    CodexWorkflowCheckpoint,
    CompletedState,
    CompletedWithResidualRiskState,
    ControllerBinding,
    CursorWorkflowCheckpoint,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.scheduler.infrastructure.systemd_assets import validate_packaged_assets


def _insert_legacy_a_bearing_run(
    store: SqliteSchedulerStore,
    *,
    run_id: str,
    context,
    terminal: bool = False,
) -> str:
    legacy_key = _legacy_submission_idempotency_key(context)
    submitted = sample_submitted_state(run_id=run_id, repo_root=context.repository.root).model_copy(
        update={"idempotency_key": legacy_key, "context": context}
    )
    event = RunSubmittedEvent(
        run_id=run_id,
        idempotency_key=legacy_key,
        worktree_key=context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=run_id,
            state=submitted,
            event_id=f"evt-{run_id}",
            event=event,
            now=now,
        )
        if terminal:
            checkpoint = AdmittedRunCheckpoint(
                authorized_at=now_text,
                authorized_controller_session_id=CONTROLLER_SESSION,
                admitted_at=now_text,
                admission_status_artifact_path="git/admission-status.txt",
                admission_status_sha256="a" * 64,
            )
            completed = CompletedState(
                run_id=run_id,
                version=2,
                submitted_at=now_text,
                updated_at=now_text,
                idempotency_key=legacy_key,
                context=context,
                checkpoint=checkpoint,
                cursor=CursorWorkflowCheckpoint(iteration=1),
                codex=CodexWorkflowCheckpoint(
                    review_iteration=1,
                    reviews_completed=1,
                    reviewer_session_id="019abc00-0000-0000-0000-000000000000",
                ),
            )
            store.compare_and_swap_state(
                conn,
                run_id=run_id,
                expected_version=1,
                new_state=completed,
                now=now + timedelta(seconds=1),
            )
    return legacy_key


def test_afree_lookup_reuses_active_legacy_a_bearing_run(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    context = sample_agent_led_submitted_context()
    _insert_legacy_a_bearing_run(store, run_id="legacy-active", context=context)
    retry_context = context.model_copy(
        update={"controller": ControllerBinding(controller_session_id=None)}
    )
    with store.begin_read() as conn:
        existing = store.find_existing_submission(
            conn,
            context=retry_context,
            resubmission_id=None,
        )
    assert existing is not None
    assert str(existing["run_id"]) == "legacy-active"


def test_afree_lookup_reuses_terminal_legacy_a_bearing_run(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    context = sample_agent_led_submitted_context()
    _insert_legacy_a_bearing_run(
        store,
        run_id="legacy-terminal",
        context=context,
        terminal=True,
    )
    retry_context = context.model_copy(
        update={"controller": ControllerBinding(controller_session_id=None)}
    )
    with store.begin_read() as conn:
        existing = store.find_existing_submission(
            conn,
            context=retry_context,
            resubmission_id=None,
        )
    assert existing is not None
    assert str(existing["run_id"]) == "legacy-terminal"


def test_legacy_a_bearing_retry_with_same_controller_reuses_run(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    context = sample_agent_led_submitted_context()
    _insert_legacy_a_bearing_run(store, run_id="legacy-with-a", context=context)
    with store.begin_read() as conn:
        existing = store.find_existing_submission(
            conn,
            context=context,
            resubmission_id=None,
        )
    assert existing is not None
    assert str(existing["run_id"]) == "legacy-with-a"


def test_blocked_run_reports_completed_reviews_from_ledger(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    repo_root = str(tmp_path / "repo")
    submitted = sample_submitted_state(run_id="blocked-review-run", repo_root=repo_root)
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    checkpoint = AdmittedRunCheckpoint(
        authorized_at=now_text,
        authorized_controller_session_id=CONTROLLER_SESSION,
        admitted_at=now_text,
        admission_status_artifact_path="git/admission-status.txt",
        admission_status_sha256="a" * 64,
    )
    waiting = WaitingForCursorFixState(
        run_id=submitted.run_id,
        version=3,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=submitted.idempotency_key,
        context=submitted.context,
        checkpoint=checkpoint,
        cursor=CursorWorkflowCheckpoint(
            iteration=1,
            staged_patch_path="git/diffs/01.patch",
            staged_patch_sha256="a" * 64,
        ),
        codex=CodexWorkflowCheckpoint(
            review_iteration=1,
            reviews_completed=1,
            reviewer_session_id="019abc00-0000-0000-0000-000000000000",
            latest_fix_prompt_path="prompts/fixes/01.txt",
            latest_fix_prompt_sha256="b" * 64,
        ),
    )
    fix_event = WaitingForCursorFixEnteredEvent(
        run_id=submitted.run_id,
        review_iteration=1,
        fix_prompt_path="prompts/fixes/01.txt",
        fix_prompt_sha256="b" * 64,
        correction_envelope_path="prompts/fixes/01.execution-envelope.txt",
        correction_envelope_sha256="c" * 64,
    )
    blocked_event = CodexReviewBlockedEvent(
        run_id=submitted.run_id,
        block_reason_kind="codex_review_outcome_invalid",
        block_reason_summary="invalid review output",
    )
    blocked = apply_codex_review_blocked(waiting, blocked_event, now_text=now_text)
    assert isinstance(blocked, BlockedState)
    assert not hasattr(blocked, "codex")

    event = RunSubmittedEvent(
        run_id=submitted.run_id,
        idempotency_key=submitted.idempotency_key,
        worktree_key=submitted.context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=submitted.run_id,
            state=submitted,
            event_id="evt-submit",
            event=event,
            now=now,
        )
        store.compare_and_swap_state(
            conn,
            run_id=submitted.run_id,
            expected_version=1,
            new_state=blocked,
            now=now + timedelta(seconds=1),
        )
        store.append_event(
            conn,
            event_id="evt-fix",
            run_id=submitted.run_id,
            sequence=store.next_event_sequence(conn, submitted.run_id),
            event=fix_event,
            now=now,
        )

    with store.begin_read() as conn:
        ledger_count = store.count_review_completion_events(conn, submitted.run_id)
        status_summary = SchedulerStatusService(store)._summary_for_state(conn, blocked)
        controller_summary = _candidate_from_state(store, conn, blocked, holder_run_id=None)
    assert ledger_count == 1
    assert review_budget_from_state(blocked, ledger_reviews_completed=ledger_count) == (1, 3)
    assert status_summary.review_iterations_completed == 1
    assert controller_summary.review_iterations_completed == 1


def test_timer_validation_rejects_commented_and_duplicate_directives() -> None:
    from ai_dev_loop.scheduler.infrastructure import systemd_assets

    original = systemd_assets.load_timer_template

    def commented_accuracy() -> str:
        return original().replace("AccuracySec=1s", "# AccuracySec=1s")

    def duplicate_boot() -> str:
        text = original()
        return text.replace("[Timer]", "[Timer]\nOnBootSec=30", 1)

    def wrong_accuracy() -> str:
        return original().replace("AccuracySec=1s", "AccuracySec=2s")

    systemd_assets.load_timer_template = commented_accuracy
    assert any("AccuracySec" in error for error in validate_packaged_assets())
    systemd_assets.load_timer_template = duplicate_boot
    assert any("exactly one OnBootSec" in error for error in validate_packaged_assets())
    systemd_assets.load_timer_template = wrong_accuracy
    assert any("AccuracySec" in error and "1s" in error for error in validate_packaged_assets())
    systemd_assets.load_timer_template = original
    assert validate_packaged_assets() == []


def test_timer_validation_rejects_whitespace_duplicate_directives() -> None:
    from ai_dev_loop.scheduler.infrastructure import systemd_assets

    original = systemd_assets.load_timer_template
    cases = {
        "OnBootSec": "OnBootSec = 60",
        "OnUnitActiveSec": "OnUnitActiveSec = 300",
        "AccuracySec": "AccuracySec = 60s",
    }
    for directive, conflicting_line in cases.items():
        text = original().replace("[Timer]", f"[Timer]\n{conflicting_line}", 1)
        systemd_assets.load_timer_template = lambda template=text: template
        errors = validate_packaged_assets()
        assert any(f"exactly one {directive}" in error for error in errors), errors
    systemd_assets.load_timer_template = original
    assert validate_packaged_assets() == []


def test_controller_status_by_run_id_preserves_terminal_none_next_action(
    tmp_path: Path,
    git_repo: Path,
) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    repo_root = git_repo.resolve()
    context = sample_agent_led_submitted_context(repo_root=str(repo_root)).model_copy(
        update={"controller": ControllerBinding(controller_session_id=None)}
    )
    run_id = "residual-risk-terminal"
    submitted = sample_submitted_state(run_id=run_id, repo_root=str(repo_root)).model_copy(
        update={"context": context}
    )
    now = datetime(2026, 9, 12, 12, 0, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    checkpoint = AdmittedRunCheckpoint(
        authorized_at=now_text,
        authorized_controller_session_id=None,
        admitted_at=now_text,
        admission_status_artifact_path="git/admission-status.txt",
        admission_status_sha256="a" * 64,
    )
    terminal = CompletedWithResidualRiskState(
        run_id=run_id,
        version=2,
        submitted_at=now_text,
        updated_at=now_text,
        idempotency_key=submitted.idempotency_key,
        context=context,
        checkpoint=checkpoint,
        cursor=CursorWorkflowCheckpoint(iteration=1),
        codex=CodexWorkflowCheckpoint(
            review_iteration=1,
            reviews_completed=1,
            reviewer_session_id="019abc00-0000-0000-0000-000000000000",
        ),
    )
    event = RunSubmittedEvent(
        run_id=run_id,
        idempotency_key=submitted.idempotency_key,
        worktree_key=context.repository.worktree_key,
        reused_existing=False,
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=run_id,
            state=submitted,
            event_id="evt-submit",
            event=event,
            now=now,
        )
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=1,
            new_state=terminal,
            now=now + timedelta(seconds=1),
        )

    with patch(
        "ai_dev_loop.scheduler.application.controller_read.default_engine_db_path",
        return_value=store.db_path,
    ):
        status = controller_status(
            repo_path=repo_root,
            run_id=run_id,
            include_terminal=True,
        )
    assert status.match_count == 1
    assert status.run_id == run_id
    assert status.status == "completed_with_residual_risk"
    assert status.next_safe_action == "none"

    json_payload = json.loads(render_controller_status(status, output="json"))
    assert json_payload["next_safe_action"] == "none"
    text_output = render_controller_status(status, output="text")
    assert "Next safe action: none" in text_output
    assert "Inspect scheduler artifacts for the blocked run." not in text_output


def test_timeline_enforces_hard_max_without_full_table_scan(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="timeline-cap-run")
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 11, 12, 0, 0, tzinfo=UTC)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )
        for index in range(HARD_TIMELINE_MAX + 5):
            dispatch_id = f"dispatch-{index:04d}"
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
                    now_text,
                    now_text,
                    now_text,
                ),
            )
            conn.execute(
                """
                INSERT INTO scheduler_attempts(
                    attempt_id, run_id, dispatch_id, component, iteration, status,
                    created_at, updated_at, launch_requested_at, completed_at
                ) VALUES (?, ?, ?, 'cursor', 1, 'completed', ?, ?, ?, ?)
                """,
                (
                    f"attempt-{index:04d}",
                    state.run_id,
                    dispatch_id,
                    now_text,
                    now_text,
                    now_text,
                    now_text,
                ),
            )

    service = SchedulerTimelineService(store)
    default_timeline = service.get_timeline(state.run_id)
    assert default_timeline.limit == DEFAULT_TIMELINE_LIMIT
    assert len(default_timeline.entries) == DEFAULT_TIMELINE_LIMIT

    capped = service.get_timeline(state.run_id, limit=HARD_TIMELINE_MAX + 10)
    assert capped.limit == HARD_TIMELINE_MAX
    assert capped.truncated is True
    assert len(capped.entries) == HARD_TIMELINE_MAX
