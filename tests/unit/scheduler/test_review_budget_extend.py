"""Review budget extension recovery tests."""

from __future__ import annotations

import hashlib
import itertools
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_agent_led_submitted_context
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.review_budget import (
    effective_review_ceiling_for_run,
    effective_review_ceiling_for_state,
    fold_review_budget_extensions,
    load_validated_review_budget_extensions,
)
from ai_dev_loop.scheduler.application.review_budget_artifacts import (
    load_exhausted_review_artifacts,
)
from ai_dev_loop.scheduler.application.review_budget_extend import ReviewBudgetExtendService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.events import (
    REVIEW_BUDGET_EXTENDED_EVENT_KIND,
    ReviewBudgetExtendedEvent,
    TimerFiredEvent,
    parse_scheduler_event,
)
from ai_dev_loop.scheduler.domain.reducer import (
    apply_max_iterations_reached,
    apply_review_budget_extended,
)
from ai_dev_loop.scheduler.domain.state import (
    AwaitingCodexReviewState,
    SubmittedState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _next_attempt_id() -> str:
    return f"att-{next(_ATTEMPT_COUNTER):032x}"


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit(git_repo: Path, scheduler_paths: dict[str, Path], *, max_reviews: int = 3) -> str:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=git_repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
        max_review_iterations=max_reviews,
    )
    with patch("sys.stdin", StringIO(prompt)):
        return submit_run(options).run_id


def _tick_service(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
    backend,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-budget-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rebind_codex_attempt_outcome(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    *,
    run_id: str,
    attempt: object,
    outcome: dict[str, object],
) -> None:
    from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
    from ai_dev_loop.scheduler.application.attempt_envelope import (
        build_result_envelope,
        envelope_sha256,
        sha256_file,
    )

    run_root = artifacts.run_root(run_id)
    stdout_rel = str(attempt["stdout_artifact_path"])  # type: ignore[index]
    stderr_rel = str(attempt["stderr_artifact_path"])  # type: ignore[index]
    result_rel = str(attempt["result_artifact_path"])  # type: ignore[index]
    stdout_path = run_root / stdout_rel
    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    stdout_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")
    if not stderr_path.is_file():
        stderr_path.write_text("", encoding="utf-8")
    envelope = build_result_envelope(
        attempt_id=str(attempt["attempt_id"]),  # type: ignore[index]
        unit_identity=str(attempt["unit_identity"]),  # type: ignore[index]
        exit_code=int(attempt["exit_code"]),  # type: ignore[index]
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_attempts
            SET completion_envelope_sha256 = ?, updated_at = updated_at
            WHERE attempt_id = ?
            """,
            (envelope_sha256(envelope), str(attempt["attempt_id"])),  # type: ignore[index]
        )


def _seed_timer_events(
    store: SqliteSchedulerStore,
    run_id: str,
    count: int,
    *,
    now: datetime | None = None,
) -> None:
    fired_at = now or datetime(2026, 9, 13, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM scheduler_events WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        sequence = int(row[0]) + 1
        for index in range(count):
            store.append_event(
                conn,
                event_id=f"evt-dummy-{index:05d}",
                run_id=run_id,
                sequence=sequence + index,
                event=TimerFiredEvent(
                    run_id=run_id,
                    timer_id=f"timer-{index:05d}",
                    target_effect_id="fx-dummy",
                    expected_run_version=1,
                ),
                now=fired_at,
            )


def _run_until(tick: TickService, run_id: str, *, target_kind: str, max_ticks: int = 80) -> None:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
            if state.kind == "blocked":
                raise AssertionError(
                    f"run blocked: {getattr(state, 'block_reason_kind', '')}: "
                    f"{getattr(state, 'block_reason_summary', '')}"
                )
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


class TestReviewBudgetFolding:
    def test_no_events_uses_frozen_base(self) -> None:
        context = sample_agent_led_submitted_context()
        submitted = SubmittedState(
            run_id="run-budget-1",
            version=1,
            submitted_at="2026-09-13T00:00:00.000Z",
            updated_at="2026-09-13T00:00:00.000Z",
            idempotency_key="a" * 64,
            context=context,
        )
        assert effective_review_ceiling_for_state(submitted, ()) == 3

    def test_one_event_raises_ceiling(self) -> None:
        event = ReviewBudgetExtendedEvent(
            run_id="run-budget-1",
            review_iteration=3,
            previous_effective_total=3,
            new_effective_total=5,
            review_result_path="codex/reviews/03.json",
            review_result_sha256="a" * 64,
            fix_prompt_path="prompts/fixes/03.txt",
            fix_prompt_sha256="b" * 64,
            correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
            correction_envelope_sha256="c" * 64,
        )
        assert fold_review_budget_extensions(3, (event,)) == 5

    def test_repeated_target_is_monotonic(self) -> None:
        first = ReviewBudgetExtendedEvent(
            run_id="run-budget-1",
            review_iteration=3,
            previous_effective_total=3,
            new_effective_total=5,
            review_result_path="codex/reviews/03.json",
            review_result_sha256="a" * 64,
            fix_prompt_path="prompts/fixes/03.txt",
            fix_prompt_sha256="b" * 64,
            correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
            correction_envelope_sha256="c" * 64,
        )
        second = ReviewBudgetExtendedEvent(
            run_id="run-budget-1",
            review_iteration=5,
            previous_effective_total=5,
            new_effective_total=7,
            review_result_path="codex/reviews/05.json",
            review_result_sha256="d" * 64,
            fix_prompt_path="prompts/fixes/05.txt",
            fix_prompt_sha256="e" * 64,
            correction_envelope_path="prompts/fixes/05.execution-envelope.txt",
            correction_envelope_sha256="f" * 64,
        )
        assert fold_review_budget_extensions(3, (first, second)) == 7

    def test_malformed_grant_rejects_inconsistent_previous_total(self) -> None:
        event = ReviewBudgetExtendedEvent(
            run_id="run-budget-1",
            review_iteration=3,
            previous_effective_total=2,
            new_effective_total=5,
            review_result_path="codex/reviews/03.json",
            review_result_sha256="a" * 64,
            fix_prompt_path="prompts/fixes/03.txt",
            fix_prompt_sha256="b" * 64,
            correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
            correction_envelope_sha256="c" * 64,
        )
        with pytest.raises(SchedulerEngineError, match="inconsistent previous total") as exc:
            fold_review_budget_extensions(3, (event,))
        assert exc.value.kind is SchedulerEngineErrorKind.CORRUPTION

    def test_out_of_order_grant_rejects_non_monotonic_ceiling(self) -> None:
        event = ReviewBudgetExtendedEvent(
            run_id="run-budget-1",
            review_iteration=3,
            previous_effective_total=3,
            new_effective_total=3,
            review_result_path="codex/reviews/03.json",
            review_result_sha256="a" * 64,
            fix_prompt_path="prompts/fixes/03.txt",
            fix_prompt_sha256="b" * 64,
            correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
            correction_envelope_sha256="c" * 64,
        )
        with pytest.raises(SchedulerEngineError, match="does not raise the effective ceiling"):
            fold_review_budget_extensions(3, (event,))


class TestReviewBudgetLedgerValidation:
    def test_invalid_digest_rejects_extension_load(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        service.extend(run_id, target_total=3)
        with tick.store.begin_immediate() as conn:
            conn.execute(
                """
                UPDATE scheduler_events
                SET event_payload_sha256 = ?
                WHERE run_id = ? AND event_kind = ?
                """,
                ("0" * 64, run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            )
            with pytest.raises(SchedulerEngineError, match="payload digest mismatch") as exc:
                load_validated_review_budget_extensions(conn, run_id)
            assert exc.value.kind is SchedulerEngineErrorKind.CORRUPTION

    def test_wrong_payload_run_id_rejects_extension_load(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        service.extend(run_id, target_total=3)
        with tick.store.begin_immediate() as conn:
            row = conn.execute(
                """
                SELECT event_payload FROM scheduler_events
                WHERE run_id = ? AND event_kind = ?
                """,
                (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["run_id"] = "run-other"
            _, payload_text, digest = SqliteSchedulerStore.dump_event(
                parse_scheduler_event(payload)
            )
            conn.execute(
                """
                UPDATE scheduler_events
                SET event_payload = ?, event_payload_sha256 = ?
                WHERE run_id = ? AND event_kind = ?
                """,
                (payload_text, digest, run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            )
            with pytest.raises(SchedulerEngineError, match="payload run_id disagrees") as exc:
                load_validated_review_budget_extensions(conn, run_id)
            assert exc.value.kind is SchedulerEngineErrorKind.CORRUPTION

    def test_invalid_exhaustion_iteration_rejects_extension_load(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        service.extend(run_id, target_total=3)
        with tick.store.begin_immediate() as conn:
            row = conn.execute(
                """
                SELECT event_payload FROM scheduler_events
                WHERE run_id = ? AND event_kind = ?
                """,
                (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["review_iteration"] = 1
            _, payload_text, digest = SqliteSchedulerStore.dump_event(
                parse_scheduler_event(payload)
            )
            conn.execute(
                """
                UPDATE scheduler_events
                SET event_payload = ?, event_payload_sha256 = ?
                WHERE run_id = ? AND event_kind = ?
                """,
                (payload_text, digest, run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            )
            with pytest.raises(
                SchedulerEngineError,
                match="invalid exhausted review iteration",
            ) as exc:
                load_validated_review_budget_extensions(conn, run_id)
            assert exc.value.kind is SchedulerEngineErrorKind.CORRUPTION


class TestReviewBudgetReducer:
    def test_extension_preserves_review_count_and_sets_correction_iteration(self) -> None:
        from tests.unit.scheduler.helpers import sample_submitted_state

        from ai_dev_loop.scheduler.domain.events import MaxIterationsReachedEvent
        from ai_dev_loop.scheduler.domain.state import (
            AdmittedRunCheckpoint,
            CodexWorkflowCheckpoint,
            CursorWorkflowCheckpoint,
        )

        submitted = sample_submitted_state(run_id="run-reducer-1")
        now_text = "2026-09-13T00:00:00.000000Z"
        checkpoint = AdmittedRunCheckpoint(
            authorized_at=now_text,
            authorized_controller_session_id=CONTROLLER_SESSION,
            admitted_at=now_text,
            admission_status_artifact_path="git/admission-status.txt",
            admission_status_sha256="d" * 64,
        )
        awaiting = AwaitingCodexReviewState(
            run_id=submitted.run_id,
            version=4,
            submitted_at=now_text,
            updated_at=now_text,
            idempotency_key=submitted.idempotency_key,
            context=submitted.context,
            checkpoint=checkpoint,
            cursor=CursorWorkflowCheckpoint(
                iteration=3,
                chat_id="019abc00-0000-0000-0000-000000000001",
                staged_patch_path="git/diffs/03.patch",
                staged_patch_sha256="d" * 64,
            ),
            codex=CodexWorkflowCheckpoint(
                review_iteration=2,
                reviews_completed=2,
                reviewer_session_id=BOOTSTRAP_ID,
            ),
        )
        maxed = apply_max_iterations_reached(
            awaiting,
            MaxIterationsReachedEvent(
                run_id=awaiting.run_id,
                review_iteration=3,
                review_result_path="codex/reviews/03.json",
                review_result_sha256="c" * 64,
                fix_prompt_path="prompts/fixes/03.txt",
                fix_prompt_sha256="a" * 64,
                correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
                correction_envelope_sha256="b" * 64,
            ),
            now_text="t2",
        )
        extend_event = ReviewBudgetExtendedEvent(
            run_id=maxed.run_id,
            review_iteration=3,
            previous_effective_total=3,
            new_effective_total=5,
            review_result_path="codex/reviews/03.json",
            review_result_sha256="c" * 64,
            fix_prompt_path="prompts/fixes/03.txt",
            fix_prompt_sha256="a" * 64,
            correction_envelope_path="prompts/fixes/03.execution-envelope.txt",
            correction_envelope_sha256="b" * 64,
        )
        resumed = apply_review_budget_extended(maxed, extend_event, now_text="t3")
        assert isinstance(resumed, WaitingForCursorFixState)
        assert resumed.codex.reviews_completed == 3
        assert resumed.codex.review_iteration == 3
        assert resumed.cursor.iteration == 4
        assert resumed.context.workflow.max_review_iterations == 3
        assert resumed.codex.latest_fix_prompt_path == "prompts/fixes/03.txt"
        assert resumed.context is maxed.context


class TestReviewBudgetExtendIntegration:
    def test_extend_after_exhaustion_schedules_correction_without_launching_agents(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        backend = FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        )
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=backend,
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        with tick.store.begin_read() as conn:
            before, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert before.context.workflow.max_review_iterations == 2
            assert before.codex.review_iteration == 2
            reservation = tick.store.get_reservation_for_run(conn, run_id)
            assert reservation is None
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        result = service.extend(run_id, target_total=3)
        assert result.changed is True
        assert result.idempotent_replay is False
        assert result.new_effective_total == 3
        replay = service.extend(run_id, target_total=3)
        assert replay.changed is False
        assert replay.idempotent_replay is True
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_for_cursor_fix"
            assert state.context.workflow.max_review_iterations == 2
            assert state.cursor.iteration == 3
            reservation = tick.store.get_reservation_for_run(conn, run_id)
            assert reservation is not None
            effects = conn.execute(
                "SELECT effect_kind, effect_payload FROM scheduler_effects WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        cursor_effects = [
            json.loads(row[1])
            for row in effects
            if row[0] == "cursor.run_turn" and json.loads(row[1]).get("iteration") == 3
        ]
        assert cursor_effects
        agent_log_before = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
        prompt_runs_before = sum(
            1
            for line in agent_log_before.splitlines()
            if line.startswith("ARGS:") and "'-p'" in line
        )
        _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=80)
        agent_log_after = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
        prompt_runs_after = sum(
            1
            for line in agent_log_after.splitlines()
            if line.startswith("ARGS:") and "'-p'" in line
        )
        assert prompt_runs_after == prompt_runs_before + 1
        assert agent_log_after.count("CREATE_CHAT:") == 1
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "awaiting_codex_review"
            assert state.codex.reviewer_session_id == BOOTSTRAP_ID
            assert state.context.workflow.max_review_iterations == 2
        _run_until(tick, run_id, target_kind="completed", max_ticks=40)
        codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
        assert codex_log.count("'resume'") >= 2


class TestReviewBudgetArtifactBinding:
    def test_stale_fix_prompt_rejects_extend_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )
        from ai_dev_loop.scheduler.domain.state import MaxIterationsReachedState

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, MaxIterationsReachedState)
            attempt = tick.store.get_latest_recorded_codex_attempt(conn, run_id)
            source_event_count = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert attempt is not None
        stdout_path = tick.artifacts.run_root(run_id) / str(attempt["stdout_artifact_path"])
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
        fix_rel = str(outcome["fix_prompt_path"])
        fix_path = tick.artifacts.run_root(run_id) / fix_rel
        tampered = "stale fix prompt body that does not match review JSON\n"
        fix_path.write_text(tampered, encoding="utf-8")
        outcome["fix_prompt_sha256"] = _sha256_text(tampered)
        _rebind_codex_attempt_outcome(
            tick.store,
            tick.artifacts,
            run_id=run_id,
            attempt=attempt,
            outcome=outcome,
        )
        with pytest.raises(
            SchedulerEngineError,
            match="does not match schema-validated cursor_fix_prompt",
        ):
            load_exhausted_review_artifacts(tick.store, tick.artifacts, state)
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        with pytest.raises(
            SchedulerEngineError,
            match="does not match schema-validated cursor_fix_prompt",
        ):
            service.extend(run_id, target_total=3)
        with tick.store.begin_read() as conn:
            after_state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert after_state.kind == "max_iterations_reached"
            event_count = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert event_count == source_event_count

    def test_empty_fix_prompt_rejects_extend_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )
        from ai_dev_loop.scheduler.domain.state import MaxIterationsReachedState

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, MaxIterationsReachedState)
            attempt = tick.store.get_latest_recorded_codex_attempt(conn, run_id)
            source_event_count = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert attempt is not None
        stdout_path = tick.artifacts.run_root(run_id) / str(attempt["stdout_artifact_path"])
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
        fix_rel = str(outcome["fix_prompt_path"])
        fix_path = tick.artifacts.run_root(run_id) / fix_rel
        fix_path.write_text("", encoding="utf-8")
        outcome["fix_prompt_sha256"] = _sha256_text("")
        _rebind_codex_attempt_outcome(
            tick.store,
            tick.artifacts,
            run_id=run_id,
            attempt=attempt,
            outcome=outcome,
        )
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        with pytest.raises(SchedulerEngineError, match="artifact is empty"):
            service.extend(run_id, target_total=3)
        with tick.store.begin_read() as conn:
            after_state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert after_state.kind == "max_iterations_reached"
            event_count = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert event_count == source_event_count


class TestReviewBudgetLedgerCompleteness:
    def test_extension_beyond_five_hundred_ordinary_events(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        _seed_timer_events(tick.store, run_id, 500, now=fixed_now)
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        result = service.extend(run_id, target_total=3)
        assert result.changed is True
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert effective_review_ceiling_for_run(tick.store, conn, state) == 3
        _run_until(tick, run_id, target_kind="completed", max_ticks=80)


class TestReviewBudgetReExhaustionReplay:
    def test_replay_after_re_exhaustion_recommends_extend_not_tick(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.fake_attempt_backend import (
            FakeAgentProcessBackend,
            FakeAttemptScenario,
        )

        monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,findings")
        monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
        run_id = _submit(git_repo, scheduler_paths, max_reviews=2)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        fixed_now = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=fixed_now,
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="max_iterations_reached")
        service = ReviewBudgetExtendService(
            tick.store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        service.extend(run_id, target_total=3)
        _run_until(tick, run_id, target_kind="max_iterations_reached", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, version_before, _ = tick.store.load_validated_snapshot(conn, run_id)
            event_count_before = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            effect_count_before = conn.execute(
                "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        replay = service.extend(run_id, target_total=3)
        assert replay.changed is False
        assert replay.idempotent_replay is True
        assert replay.state_kind == "max_iterations_reached"
        assert replay.safe_next_action.kind is SafeNextActionKind.NONE
        assert replay.safe_next_action.command is not None
        assert "scheduler extend" in replay.safe_next_action.command
        assert "scheduler tick" not in replay.safe_next_action.command
        with tick.store.begin_read() as conn:
            after_state, version_after, _ = tick.store.load_validated_snapshot(conn, run_id)
            event_count_after = conn.execute(
                "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            effect_count_after = conn.execute(
                "SELECT COUNT(*) FROM scheduler_effects WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        assert after_state.kind == "max_iterations_reached"
        assert version_after == version_before
        assert event_count_after == event_count_before
        assert effect_count_after == effect_count_before
