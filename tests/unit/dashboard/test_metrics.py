from __future__ import annotations

from datetime import UTC, datetime

from ai_dev_loop.dashboard.application.ports import AttemptObservation, RunObservation
from ai_dev_loop.dashboard.domain.filters import DashboardFilters
from ai_dev_loop.dashboard.domain.metrics import (
    first_execution_launches,
    first_review_round_launches,
    process_second_buckets,
    state_group,
)


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _attempt(
    *,
    run_id: str = "run-a",
    component: str = "cursor",
    effect_kind: str | None = "cursor.run_turn",
    iteration: int = 1,
    status: str = "completed",
    launch: str | None = "2026-03-08T05:00:00Z",
    completed: str | None = "2026-03-08T06:00:00Z",
    ingested: bool = True,
) -> AttemptObservation:
    return AttemptObservation(
        run_id=run_id,
        project_name="project-a",
        component=component,
        effect_kind=effect_kind,
        iteration=iteration,
        status=status,
        launch_requested_at=_instant(launch) if launch else None,
        completed_at=_instant(completed) if completed else None,
        ingested=ingested,
    )


def test_state_mapping_keeps_reviewed_variants_and_unknown_as_other() -> None:
    assert state_group("completed") == "completed"
    assert state_group("completed_with_residual_risk") == "residual-risk"
    assert state_group("waiting_codex_capacity") == "waiting"
    assert state_group("checkpoint_pending") == "running"
    assert state_group("future_scheduler_state") == "other"


def test_first_execution_excludes_chat_and_separates_implementer_from_reviewer() -> None:
    runs = (
        RunObservation(
            "run-a", "project-a", "codex", "completed", _instant("2026-01-01T00:00:00Z"), None
        ),
        RunObservation(
            "run-b", "project-a", "cursor", "queued", _instant("2026-01-01T00:00:00Z"), None
        ),
    )
    attempts = (
        _attempt(effect_kind="cursor.create_chat", launch="2026-01-01T01:00:00Z"),
        _attempt(
            component="codex_implementer",
            effect_kind="codex.implementer_bootstrap",
            launch="2026-01-01T02:00:00Z",
        ),
        _attempt(
            component="codex",
            effect_kind="codex.bootstrap_review",
            launch="2026-01-01T03:00:00Z",
        ),
        _attempt(
            run_id="run-b",
            component="codex_implementer",
            effect_kind="codex.bootstrap_review",
            launch="2026-01-01T04:00:00Z",
        ),
    )
    launches, coverage = first_execution_launches(runs, attempts)
    assert launches == {"run-a": _instant("2026-01-01T02:00:00Z")}
    assert coverage.unstarted_runs == 1
    assert coverage.unknown_execution_kinds == 1


def test_review_rounds_deduplicate_retries_but_not_runs_and_never_count_implementer() -> None:
    attempts = (
        _attempt(
            component="codex",
            effect_kind="codex.bootstrap_review",
            iteration=1,
            launch="2026-01-01T01:00:00Z",
        ),
        _attempt(
            component="codex_implementer",
            effect_kind="codex.implementer_resume",
            iteration=1,
            launch="2026-01-01T01:30:00Z",
        ),
        _attempt(
            component="codex",
            effect_kind="codex.resume_review",
            iteration=1,
            launch="2026-01-01T02:00:00Z",
        ),
        _attempt(
            component="codex",
            effect_kind="codex.resume_review",
            iteration=2,
            launch="2026-01-01T03:00:00Z",
        ),
        _attempt(
            run_id="run-successor",
            component="codex",
            effect_kind="codex.resume_review",
            iteration=1,
            launch="2026-01-01T04:00:00Z",
        ),
    )
    rounds, missing = first_review_round_launches(attempts)
    assert missing == 0
    assert rounds == {
        ("run-a", 1): _instant("2026-01-01T01:00:00Z"),
        ("run-a", 2): _instant("2026-01-01T03:00:00Z"),
        ("run-successor", 1): _instant("2026-01-01T04:00:00Z"),
    }


def test_process_seconds_sum_parallel_resolved_attempts_and_do_not_fabricate_missing() -> None:
    filters = DashboardFilters.parse(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-01-02T00:00:00Z",
        time_zone="UTC",
    )
    attempts = (
        _attempt(launch="2026-01-01T01:00:00Z", completed="2026-01-01T02:00:00Z"),
        _attempt(
            run_id="run-b",
            launch="2026-01-01T01:00:00Z",
            completed="2026-01-01T02:00:00Z",
        ),
        _attempt(
            run_id="run-cancelled-pending",
            status="cancelled",
            ingested=False,
            launch="2026-01-01T02:00:00Z",
            completed="2026-01-01T03:00:00Z",
        ),
        _attempt(
            run_id="run-active",
            status="active",
            launch="2026-01-01T02:00:00Z",
            completed=None,
        ),
        _attempt(
            run_id="run-cancelled-resolved",
            status="cancelled",
            ingested=True,
            launch="2026-01-01T03:00:00Z",
            completed="2026-01-01T04:00:00Z",
        ),
        _attempt(
            run_id="run-failed-missing-end",
            status="failed",
            launch="2026-01-01T04:00:00Z",
            completed=None,
        ),
    )
    buckets, missing, partial = process_second_buckets(attempts, filters)
    assert buckets == {"2026-01-01": 10800.0}
    assert missing == 3
    assert partial is True


def test_process_seconds_split_at_named_zone_midnight_across_dst_day() -> None:
    filters = DashboardFilters.parse(
        from_value="2026-03-08T05:00:00Z",
        to_value="2026-03-09T04:00:00Z",
        time_zone="America/New_York",
    )
    attempt = _attempt(
        launch="2026-03-08T05:00:00Z",
        completed="2026-03-09T04:00:00Z",
    )
    buckets, missing, partial = process_second_buckets((attempt,), filters)
    assert buckets == {"2026-03-08": 23 * 60 * 60}
    assert missing == 0 and partial is False


def test_process_seconds_reject_completion_before_launch() -> None:
    filters = DashboardFilters.parse(
        from_value="2026-01-01T00:00:00Z",
        to_value="2026-01-02T00:00:00Z",
        time_zone="UTC",
    )
    attempt = _attempt(
        launch="2026-01-01T02:00:00Z",
        completed="2026-01-01T01:00:00Z",
    )
    try:
        process_second_buckets((attempt,), filters)
    except ValueError as exc:
        assert "precedes" in str(exc)
    else:
        raise AssertionError("invalid interval was accepted")
