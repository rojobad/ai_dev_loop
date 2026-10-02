"""Phase 22 mandatory acceptance paths beyond routing policy unit slices."""

from __future__ import annotations

import json
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase22_codex_routing_auto_retry import (
    _authorize_due_routing_retry,
    _routing_failure_cycle,
    _run_until,
    _submit,
    _tick_service,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.codex_routing_auto_retry import (
    annotate_codex_outcome_routing_evidence,
    classify_codex_workspace_routing_timeout_from_outcome,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError, TickRunReceipt
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.history import _safe_detail_for_event
from ai_dev_loop.scheduler.application.review_checkpoint_verify import (
    verify_retry_state_repository,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.review_retry_authorization import (
    ReviewRetryAuthorizationRequest,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_DELAY_SECONDS,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
)
from ai_dev_loop.scheduler.domain.state import WaitingCodexReviewRetryState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _visit_run_only(
    tick: TickService,
    run_id: str,
    *,
    now: datetime | None = None,
) -> list[TickRunReceipt]:
    owner = f"visit-{run_id[-12:]}"
    clock = now if now is not None else tick._now_factory()
    with tick.store.begin_immediate() as conn:
        lease = tick.store.acquire_global_tick_lease(
            conn,
            owner_id=owner,
            now=clock,
            ttl_seconds=300,
        )
        assert lease is not None
        generation, _ = lease
    receipts = list(tick._visit_run(owner, generation, run_id))
    with tick.store.begin_immediate() as conn:
        tick.store.release_global_tick_lease(
            conn,
            owner_id=owner,
            generation=generation,
            now=clock,
        )
    return receipts


def _codex_attempt_ids(store: SqliteSchedulerStore, conn: object, run_id: str) -> set[str]:
    rows = conn.execute(  # type: ignore[union-attr]
        """
        SELECT attempt_id FROM scheduler_attempts
        WHERE run_id = ? AND component = 'codex'
        """,
        (run_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _latest_retry_authorization_source(store: SqliteSchedulerStore, conn: object, run_id: str) -> str:
    row = conn.execute(  # type: ignore[union-attr]
        """
        SELECT event_payload FROM scheduler_events
        WHERE run_id = ? AND event_kind = 'codex_review_retry_requested'
        ORDER BY sequence DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    assert row is not None
    payload = json.loads(str(row[0]))
    return str(payload["authorization_source"])


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def test_abort_before_due_blocks_automatic_routing_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    SchedulerAbortService(store, artifacts).abort_run(run_id)
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = due_tick.run_once()
    assert not any(
        item.action == "codex_routing_auto_retry_authorized" for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        row = store.get_review_retry_generation_row(
            conn,
            run_id=run_id,
            failure_generation=1,
        )
        assert row is None


def test_abort_before_launch_blocks_codex_dispatch_after_routing_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    store = tick.store
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    SchedulerAbortService(store, artifacts).abort_run(run_id)
    launch_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 5),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    with store.begin_read() as conn:
        codex_before = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id = ? AND component = 'codex' AND status != 'completed'
            """,
            (run_id,),
        ).fetchone()[0]
    receipt = launch_tick.run_once()
    assert not any(
        item.action in {"attempt_launched", "attempt_adopted"} for item in receipt.run_receipts
    )
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "aborted"
        codex_after = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id = ? AND component = 'codex' AND status != 'completed'
            """,
            (run_id,),
        ).fetchone()[0]
        assert codex_after == codex_before


def test_historical_manual_retry_fixture_has_inactive_routing_defaults() -> None:
    import json

    from ai_dev_loop.scheduler.domain.events import parse_scheduler_event

    fixture = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "phase22_historical"
        / "manual_retry_event_v1.json"
    )
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    event = parse_scheduler_event(payload)
    assert event.routing_auto_retry_eligible is False
    assert event.routing_auto_retry_authorizations_used == 0
    assert event.routing_auto_retry_due_at is None


def test_non_routing_manual_retry_wait_never_auto_authorizes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexReviewRetryState)
        assert state.codex.routing_auto_retry_eligible is False
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    receipt = due_tick.run_once()
    assert not any(
        item.action == "codex_routing_auto_retry_authorized" for item in receipt.run_receipts
    )
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_review_retry"
        assert state.codex.routing_auto_retry_authorizations_used == 0
        assert (
            due_tick.store.get_review_retry_generation_row(
                conn, run_id=run_id, failure_generation=state.codex.review_retry_generation
            )
            is None
        )


def test_runner_annotation_and_ingestion_classify_positive_fixture(tmp_path: Path) -> None:
    import json

    from ai_dev_loop.response_schema import WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE

    events = tmp_path / "events.jsonl"
    events.write_text(
        json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
            }
        ),
        encoding="utf-8",
    )
    outcome: dict[str, object] = {"events_path": events.name}
    annotate_codex_outcome_routing_evidence(outcome, events)
    assert classify_codex_workspace_routing_timeout_from_outcome(tmp_path, outcome)


def test_routing_authorization_survives_tick_restart_before_dispatch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        generation = state.codex.review_retry_generation
        row = tick.store.get_review_retry_generation_row(
            conn, run_id=run_id, failure_generation=generation
        )
        assert row is not None
    restart_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 5),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    with restart_tick.store.begin_read() as conn:
        attempts_before = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id = ? AND component = 'codex'
            """,
            (run_id,),
        ).fetchone()[0]
    restart_tick.run_once()
    with restart_tick.store.begin_read() as conn:
        state, _, _ = restart_tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind in {"awaiting_codex_review", "running"}
        assert state.codex.routing_auto_retry_authorizations_used == 1
        row = restart_tick.store.get_review_retry_generation_row(
            conn, run_id=run_id, failure_generation=generation
        )
        assert row is not None
        attempts_after = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_attempts
            WHERE run_id = ? AND component = 'codex'
            """,
            (run_id,),
        ).fetchone()[0]
        assert attempts_after == attempts_before + 1
        assert restart_tick.store.get_reservation_for_run(conn, run_id) is not None


def test_production_tick_lease_expiry_during_verify_blocks_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    due_time = failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS)
    clock = {"now": due_time}

    def advance_verify(state: object, *, artifacts: object) -> None:
        clock["now"] = due_time + timedelta(seconds=35)
        verify_retry_state_repository(state, artifacts=artifacts)  # type: ignore[arg-type]

    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexReviewRetryState)
        generation = state.codex.review_retry_generation
        used_before = state.codex.routing_auto_retry_authorizations_used

    due_tick = TickService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: clock["now"],
        lease_ttl_seconds=30,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    with patch(
        "ai_dev_loop.scheduler.application.review_retry.verify_retry_state_repository",
        advance_verify,
    ):
        receipt = due_tick.run_once()
    assert not any(
        item.action == "codex_routing_auto_retry_authorized" for item in receipt.run_receipts
    )
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_review_retry"
        assert state.codex.routing_auto_retry_authorizations_used == used_before
        assert (
            due_tick.store.get_review_retry_generation_row(
                conn, run_id=run_id, failure_generation=generation
            )
            is None
        )


def test_history_includes_routing_limit_for_retry_and_capacity_events() -> None:
    import json

    retry_detail = _safe_detail_for_event(
        "codex_review_retryable_failure",
        json.dumps(
            {
                "failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
                "retry_generation": 1,
                "routing_auto_retry_eligible": True,
                "routing_auto_retry_authorizations_used": 2,
            }
        ),
    )
    assert f"routing_auto_retry_limit={ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS}" in retry_detail
    capacity_detail = _safe_detail_for_event(
        "codex_usage_capacity_detected",
        json.dumps(
            {
                "review_iteration": 1,
                "evidence_source": "post_failure_capacity_probe",
                "operational_failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
                "routing_auto_retry_authorizations_used": 0,
            }
        ),
    )
    assert f"routing_auto_retry_limit={ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS}" in capacity_detail


def test_manual_retry_after_exhausted_allowance_does_not_replenish_auto_count(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    for index in range(ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS):
        offset = ROUTING_AUTO_RETRY_DELAY_SECONDS * (index + 1)
        _authorize_due_routing_retry(
            git_repo, scheduler_paths, run_id, failure_time=failure_time, offset_seconds=offset
        )
        rerun = _tick_service(
            git_repo,
            scheduler_paths,
            now=failure_time + timedelta(seconds=offset),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(rerun, run_id, target_kind="waiting_codex_review_retry", max_ticks=40)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.routing_auto_retry_authorizations_used == ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS
        assert state.codex.routing_auto_retry_eligible is False
        generation = state.codex.review_retry_generation
    service = ReviewRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS * 20),
    )
    result = service.authorize_waiting_review_retry(
        run_id,
        request=ReviewRetryAuthorizationRequest(
            run_id=run_id,
            source="manual",
            expected_failure_generation=generation,
        ),
    )
    assert result.changed is True
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        assert state.codex.routing_auto_retry_authorizations_used == ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS


def test_routing_bootstrap_resume_completes_authenticated_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        reviewer = state.codex.reviewer_session_id
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    complete_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 10),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(complete_tick, run_id, target_kind="completed", max_ticks=120)
    with complete_tick.store.begin_read() as conn:
        state, _, _ = complete_tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == reviewer
        assert state.codex.reviews_completed == 1


def test_manual_versus_tick_routing_authorization_race_authorizes_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    due_time = failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS)
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=due_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    service = ReviewRetryService(
        due_tick.store,
        artifacts,
        now_factory=lambda: due_time,
    )
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    changed_flags: list[bool] = []

    def manual_worker() -> None:
        try:
            barrier.wait(timeout=30)
            result = service.retry(run_id)
            changed_flags.append(result.changed)
        except SchedulerEngineError:
            return
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def tick_worker() -> None:
        try:
            barrier.wait(timeout=30)
            due_tick.run_once()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=manual_worker), threading.Thread(target=tick_worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert not errors, errors
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        source = _latest_retry_authorization_source(due_tick.store, conn, run_id)
        if source == "automatic":
            assert state.codex.routing_auto_retry_authorizations_used == 1
        else:
            assert source == "manual"
            assert state.codex.routing_auto_retry_authorizations_used == 0
        rows = conn.execute(
            """
            SELECT 1 FROM scheduler_review_retry_generations
            WHERE run_id = ? AND failure_generation = ?
            """,
            (run_id, state.codex.review_retry_generation),
        ).fetchall()
        assert len(rows) == 1
        auth_events = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id = ? AND event_kind = 'codex_review_retry_requested'
            """,
            (run_id,),
        ).fetchone()[0]
        assert auth_events == 1
        codex_attempts = _codex_attempt_ids(due_tick.store, conn, run_id)
    for _ in range(40):
        _visit_run_only(due_tick, run_id, now=due_time + timedelta(seconds=10))
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, run_id)
        assert len(_codex_attempt_ids(due_tick.store, conn, run_id)) == len(codex_attempts) + 1
    assert sum(1 for flag in changed_flags if flag) <= 1


def test_uncertain_codex_launch_after_routing_authorization_does_not_duplicate_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    from tests.integration.phase21_4_helpers import codex_runner_launch_count, run_tick_once

    from ai_dev_loop.scheduler.application.attempt_backend import ObserveResult, UnitLifecycleState
    from ai_dev_loop.scheduler.application.codex_review_prompt_evidence import (
        publish_review_prompt_before_launch,
    )
    from ai_dev_loop.scheduler.domain.codex_contract import (
        codex_review_prompt_evidence_rel,
        codex_review_prompt_rel,
    )
    from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    due_time = failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 5)
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=3, exit_code=0)
    )
    launch_tick = _tick_service(git_repo, scheduler_paths, now=due_time, backend=backend)
    with launch_tick.store.begin_read() as conn:
        state, _, _ = launch_tick.store.load_validated_snapshot(conn, run_id)
        generation = state.codex.review_retry_generation
        used = state.codex.routing_auto_retry_authorizations_used
        reviewer = state.codex.reviewer_session_id
        model = state.context.codex.review_model
        effort = state.context.codex.review_reasoning_effort
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_tick_once(launch_tick)
    codex_launches = [
        call
        for call in backend.launch_calls
        if any("codex_attempt_runner" in part for part in call.agent_argv)
    ]
    assert len(codex_launches) == 1
    attempt_id = codex_launches[0].attempt_id
    with launch_tick.store.begin_read() as conn:
        codex_before = _codex_attempt_ids(launch_tick.store, conn, run_id)
        state, _, _ = launch_tick.store.load_validated_snapshot(conn, run_id)
        iteration = state.cursor.iteration
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    publish_review_prompt_before_launch(
        launch_tick.artifacts,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=iteration,
        prompt="routing-retry uncertain-ownership prompt\n",
    )
    original_observe = backend.observe
    target_attempt = attempt_id

    def unowned_with_prompt(*, unit_identity: str, attempt_id: str) -> ObserveResult:
        if attempt_id == target_attempt:
            prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
            evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
            if prompt_path.is_file() and evidence_path.is_file():
                return ObserveResult(
                    lifecycle_state=UnitLifecycleState.ACTIVE,
                    owned=False,
                    absence_proven=False,
                )
        return original_observe(unit_identity=unit_identity, attempt_id=attempt_id)

    backend.observe = unowned_with_prompt  # type: ignore[method-assign]
    launches_before = codex_runner_launch_count(backend)
    uncertain_seen = False
    for _ in range(20):
        run_tick_once(launch_tick)
        with launch_tick.store.begin_read() as conn:
            attempt = launch_tick.store.get_attempt_by_id(conn, attempt_id)
            if attempt is not None and str(attempt["status"]) == "uncertain":
                uncertain_seen = True
                break
    assert uncertain_seen
    for _ in range(12):
        run_tick_once(launch_tick)
    assert codex_runner_launch_count(backend) == launches_before
    with launch_tick.store.begin_read() as conn:
        state, _, _ = launch_tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.routing_auto_retry_authorizations_used == used
        assert state.codex.reviewer_session_id == reviewer
        assert state.context.codex.review_model == model
        assert state.context.codex.review_reasoning_effort == effort
        row = launch_tick.store.get_review_retry_generation_row(
            conn, run_id=run_id, failure_generation=generation
        )
        assert row is not None
        auth_events = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_events
            WHERE run_id = ? AND event_kind = 'codex_review_retry_requested'
            """,
            (run_id,),
        ).fetchone()[0]
        assert auth_events == 1
        assert _codex_attempt_ids(launch_tick.store, conn, run_id) == codex_before
        assert launch_tick.store.get_reservation_for_run(conn, run_id) is not None


def test_global_capacity_contention_blocks_routing_dispatch_then_resumes_exact_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    due_time = failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS)
    store = tick.store
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        reviewer = state.codex.reviewer_session_id
        used = state.codex.routing_auto_retry_authorizations_used
        generation = state.codex.review_retry_generation
        codex_before = _codex_attempt_ids(store, conn, run_id)

    holder_repo = tmp_path / "holder-repo"
    shutil.copytree(git_repo, holder_repo)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    holder_id = _submit(holder_repo, scheduler_paths)
    start_run(holder_id, db_path=scheduler_paths["db_path"])
    holder_backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=200, exit_code=0)
    )
    holder_tick = _tick_service(
        holder_repo,
        scheduler_paths,
        now=due_time,
        backend=holder_backend,
    )
    for _ in range(120):
        _visit_run_only(holder_tick, holder_id, now=due_time)
        with holder_tick.store.begin_read() as conn:
            if holder_tick.store.get_capacity_row(conn)["holder_run_id"] == holder_id:
                break
    else:
        raise AssertionError("peer run never held global agent capacity")

    launch_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=due_time + timedelta(seconds=5),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    blocked = _visit_run_only(launch_tick, run_id, now=due_time + timedelta(seconds=5))
    assert any(receipt.action == "capacity_busy" for receipt in blocked)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        assert state.kind == "awaiting_codex_review"
        assert state.codex.routing_auto_retry_authorizations_used == used
        assert (
            store.get_review_retry_generation_row(
                conn, run_id=run_id, failure_generation=generation
            )
            is not None
        )
        assert _codex_attempt_ids(store, conn, run_id) == codex_before
        assert store.get_reservation_for_run(conn, run_id) is not None

    cursor_launches = [
        call
        for call in holder_backend.launch_calls
        if any("cursor_attempt_runner" in part for part in call.agent_argv)
    ]
    assert cursor_launches
    holder_backend.set_scenario(
        cursor_launches[-1].attempt_id,
        FakeAttemptScenario(active_ticks=0, exit_code=0),
    )
    for _ in range(80):
        _visit_run_only(holder_tick, holder_id, now=due_time + timedelta(seconds=20))
        with holder_tick.store.begin_read() as conn:
            if holder_tick.store.get_capacity_row(conn)["holder_run_id"] is None:
                break

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    for _ in range(80):
        _visit_run_only(launch_tick, run_id, now=due_time + timedelta(seconds=40))
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if _codex_attempt_ids(store, conn, run_id) != codex_before:
                assert state.codex.reviewer_session_id == reviewer
                break
    else:
        raise AssertionError("routing codex dispatch did not resume after capacity release")


def test_routing_probe_timeout_records_reason_without_vetoing_eligible_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.codex_capacity_probe import (
        CodexAppServerCapacityProbe,
        CodexCapacityObservation,
        CodexCapacityReason,
        CodexCapacityStatus,
    )

    def _timeout_probe(self, codex_command: str) -> CodexCapacityObservation:
        del self, codex_command
        return CodexCapacityObservation(
            status=CodexCapacityStatus.UNAVAILABLE,
            reason=CodexCapacityReason.TIMEOUT,
        )

    monkeypatch.setattr(CodexAppServerCapacityProbe, "probe", _timeout_probe)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexReviewRetryState)
        assert state.codex.routing_auto_retry_eligible is True
        assert state.codex.routing_failure_post_probe_status == "unavailable"
        assert state.codex.routing_failure_post_probe_reason == "timeout"


def test_completed_review_resets_routing_allowance_for_next_iteration(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "findings")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    first_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(first_tick, run_id, target_kind="waiting_for_cursor_fix", max_ticks=120)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    second_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=60),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(second_tick, run_id, target_kind="awaiting_codex_review", max_ticks=120)
    with second_tick.store.begin_read() as conn:
        state, _, _ = second_tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviews_completed == 1
        assert state.codex.routing_auto_retry_authorizations_used == 0
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    routing_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=120),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(routing_tick, run_id, target_kind="waiting_codex_review_retry", max_ticks=160)
    with routing_tick.store.begin_read() as conn:
        state, _, _ = routing_tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviews_completed == 1
        assert state.codex.routing_auto_retry_authorizations_used == 0
        assert state.codex.routing_auto_retry_eligible is True


def test_invalid_review_outcome_preserves_consumed_routing_allowance(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    tick, run_id = _routing_failure_cycle(
        git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
    )
    _authorize_due_routing_retry(
        git_repo,
        scheduler_paths,
        run_id,
        failure_time=failure_time,
        offset_seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS,
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "invalid_json")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    invalid_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 5),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(invalid_tick, run_id, target_kind="waiting_codex_review_retry", max_ticks=120)
    with invalid_tick.store.begin_read() as conn:
        state, _, _ = invalid_tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.routing_auto_retry_authorizations_used == 1
        assert state.codex.routing_auto_retry_eligible is False


def test_historical_waiting_snapshot_readonly_and_tick_never_auto_authorizes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.integration_api.run_projection import build_run_inspect_data

    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "phase22_historical"
        / "manual_wait_state_pre_phase22_v1.json"
    )
    store = tick.store
    with store.begin_immediate() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        payload = state.model_dump(mode="json")
        codex = payload.get("codex", {})
        if isinstance(codex, dict):
            for key in list(codex):
                if key.startswith("routing_auto_retry") or key.startswith("routing_failure_post"):
                    codex.pop(key, None)
        if not fixture_path.is_file():
            fixture_path.parent.mkdir(parents=True, exist_ok=True)
            fixture_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        historical = WaitingCodexReviewRetryState.model_validate(payload).model_copy(
            update={"version": version + 1}
        )
        assert store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=historical,
            now=failure_time,
        )
        payload_sha = conn.execute(
            "SELECT state_payload_sha256 FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]

    readonly = SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
    SchedulerStatusService(readonly).get_status(run_id)
    with readonly.begin_read() as conn:
        after_readonly = conn.execute(
            "SELECT state_payload_sha256 FROM scheduler_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        state, _, _ = readonly.load_validated_snapshot(conn, run_id)
        inspect = build_run_inspect_data(
            readonly,
            conn,
            state,
            submitted_at="2026-10-02T12:00:00.000000Z",
            updated_at="2026-10-02T12:00:00.000000Z",
        )
        assert inspect.codex_routing_auto_retry_eligible in {None, False}
        assert inspect.codex_routing_auto_retry_authorizations_used in {None, 0}
    assert str(after_readonly) == str(payload_sha)
    fixture_payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    reloaded = WaitingCodexReviewRetryState.model_validate(fixture_payload)
    assert reloaded.codex.routing_auto_retry_eligible is False
    assert reloaded.codex.routing_auto_retry_authorizations_used == 0
    assert "routing_auto_retry_eligible" not in fixture_payload.get("codex", {})

    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    due_tick.run_once()
    with due_tick.store.begin_read() as conn:
        state, _, _ = due_tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_review_retry"
        assert state.codex.routing_auto_retry_eligible is False
        assert state.codex.routing_auto_retry_authorizations_used == 0
        assert (
            due_tick.store.get_review_retry_generation_row(
                conn, run_id=run_id, failure_generation=state.codex.review_retry_generation
            )
            is None
        )
