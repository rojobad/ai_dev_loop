"""Review budget extension recovery tests."""

from __future__ import annotations

import hashlib
import itertools
import json
import subprocess
import threading
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_agent_led_submitted_context
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_phase20_2_sequence_start import (
    _prepare_sequence,
    _start_service,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.errors import EXIT_VALIDATION_ERROR
from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.controller_read import _candidate_from_state
from ai_dev_loop.scheduler.application.history import SchedulerHistoryService
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
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
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
from ai_dev_loop.scheduler.domain.sequence import ACTIVE_SEQUENCE_STATE_KIND, ActiveSequenceState
from ai_dev_loop.scheduler.domain.state import (
    AbortedState,
    AwaitingCodexReviewState,
    BlockedState,
    MaxIterationsReachedState,
    SubmittedState,
    WaitingForCursorFixState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()
_CLI_RUNNER = CliRunner()


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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _full_durable_snapshot(store: SqliteSchedulerStore, run_id: str) -> dict[str, object]:
    with store.begin_read() as conn:
        state, version, _ = store.load_validated_snapshot(conn, run_id)
        row = store.get_run_row(conn, run_id)
        events = conn.execute(
            """
            SELECT sequence, event_kind, event_payload_sha256
            FROM scheduler_events
            WHERE run_id = ?
            ORDER BY sequence ASC
            """,
            (run_id,),
        ).fetchall()
        effects = conn.execute(
            """
            SELECT dispatch_id, effect_kind, effect_payload, source_event_id
            FROM scheduler_effects
            WHERE run_id = ?
            ORDER BY dispatch_id ASC
            """,
            (run_id,),
        ).fetchall()
        worktree_key = state.context.repository.worktree_key
        reservation_row = conn.execute(
            """
            SELECT worktree_key, run_id, status, repository_root
            FROM scheduler_repository_reservations
            WHERE worktree_key = ?
            """,
            (worktree_key,),
        ).fetchone()
        run_reservation = store.get_reservation_for_run(conn, run_id)
        return {
            "state_kind": state.kind,
            "version": version,
            "state_payload_sha256": str(row["state_payload_sha256"]),
            "events": [(int(item[0]), str(item[1]), str(item[2])) for item in events],
            "effects": [
                (str(item[0]), str(item[1]), str(item[2]), str(item[3])) for item in effects
            ],
            "worktree_reservation": (
                None
                if reservation_row is None
                else (
                    str(reservation_row[0]),
                    str(reservation_row[1]),
                    str(reservation_row[2]),
                    str(reservation_row[3]),
                )
            ),
            "run_reservation_status": (
                str(run_reservation["status"]) if run_reservation is not None else None
            ),
        }


def _durable_snapshot(store: SqliteSchedulerStore, run_id: str) -> dict[str, object]:
    return _full_durable_snapshot(store, run_id)


def _cas_run_state(
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    new_state: object,
    now: datetime,
) -> None:
    with store.begin_read() as conn:
        _, version, _ = store.load_validated_snapshot(conn, run_id)
    with store.begin_immediate() as conn:
        store.compare_and_swap_state(
            conn,
            run_id=run_id,
            expected_version=version,
            new_state=new_state,
            now=now,
        )


def _cli_extend(
    run_id: str,
    *,
    target_total: int,
    db_path: Path,
    artifact_root: Path,
    output: str = "text",
) -> object:
    with (
        patch(
            "ai_dev_loop.scheduler.application.review_budget_extend.default_engine_db_path",
            return_value=db_path,
        ),
        patch(
            "ai_dev_loop.scheduler.application.review_budget_extend.default_artifact_root",
            return_value=artifact_root,
        ),
    ):
        return _CLI_RUNNER.invoke(
            app,
            [
                "scheduler",
                "extend",
                run_id,
                "--max-review-iterations",
                str(target_total),
                "--output",
                output,
            ],
        )


def _extension_correction_effect_count(store: SqliteSchedulerStore, run_id: str) -> int:
    with store.begin_read() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM scheduler_effects AS effects
            JOIN scheduler_events AS events
              ON events.event_id = effects.source_event_id
            WHERE effects.run_id = ?
              AND events.event_kind = ?
              AND effects.effect_kind = 'cursor.run_turn'
            """,
            (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
        ).fetchone()
    return int(row[0])


def _run_records_snapshot(store: SqliteSchedulerStore, run_id: str) -> dict[str, object]:
    snapshot = _full_durable_snapshot(store, run_id)
    snapshot.pop("worktree_reservation")
    return snapshot


def _assert_replay_read_only(
    service: ReviewBudgetExtendService,
    store: SqliteSchedulerStore,
    run_id: str,
    *,
    target_total: int,
    expected_kind: str,
) -> None:
    before = _full_durable_snapshot(store, run_id)
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
        from ai_dev_loop.scheduler.application.safe_actions import (
            safe_next_action_for_scheduler_state,
        )

        expected_action = safe_next_action_for_scheduler_state(store, conn, state)
    with patch(
        "ai_dev_loop.scheduler.application.review_budget_extend.verify_review_retry_repository_checkpoint",
    ) as verify_mock:
        replay = service.extend(run_id, target_total=target_total)
    verify_mock.assert_not_called()
    assert replay.changed is False
    assert replay.idempotent_replay is True
    assert replay.state_kind == expected_kind
    assert replay.safe_next_action == expected_action
    assert _full_durable_snapshot(store, run_id) == before


def _setup_maxed_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    max_reviews: int = 2,
    review_sequence: str = "findings,findings",
) -> tuple[str, TickService, ReviewBudgetExtendService, datetime]:
    from ai_dev_loop.scheduler.application.fake_attempt_backend import (
        FakeAgentProcessBackend,
        FakeAttemptScenario,
    )

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_reviews=max_reviews)
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
    return run_id, tick, service, fixed_now


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


class TestReviewBudgetReplayFromProgressedStates:
    @pytest.mark.parametrize(
        "target_kind",
        [
            "waiting_for_cursor_fix",
            "cursor_ready",
            "awaiting_codex_review",
            "completed",
        ],
    )
    def test_exact_replay_is_read_only_with_current_safe_action(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
        target_kind: str,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        run_id, tick, service, _ = _setup_maxed_run(
            git_repo,
            scheduler_paths,
            monkeypatch,
            review_sequence="findings,findings,no_findings",
        )
        service.extend(run_id, target_total=3)
        if target_kind != "waiting_for_cursor_fix":
            _run_until(tick, run_id, target_kind=target_kind, max_ticks=80)
        _assert_replay_read_only(
            service,
            tick.store,
            run_id,
            target_total=3,
            expected_kind=target_kind,
        )

    def test_replay_from_completed_returns_terminal_action_not_tick(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        run_id, tick, service, _ = _setup_maxed_run(
            git_repo,
            scheduler_paths,
            monkeypatch,
            review_sequence="findings,findings,no_findings",
        )
        service.extend(run_id, target_total=3)
        _run_until(tick, run_id, target_kind="completed", max_ticks=80)
        replay = service.extend(run_id, target_total=3)
        assert replay.state_kind == "completed"
        assert replay.safe_next_action.kind is SafeNextActionKind.NONE
        assert replay.safe_next_action.command is None

    def test_replay_from_blocked_returns_inspect_action(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, fixed_now = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        service.extend(run_id, target_total=3)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingForCursorFixState)
        blocked = BlockedState(
            run_id=state.run_id,
            version=state.version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            updated_at="2026-09-13T12:30:00.000000Z",
            context=state.context,
            blocked_at="2026-09-13T12:30:00.000000Z",
            block_reason_kind="dirty_worktree",
            block_reason_summary="worktree drift blocked continuation",
        )
        _cas_run_state(tick.store, run_id, new_state=blocked, now=fixed_now)
        _assert_replay_read_only(
            service,
            tick.store,
            run_id,
            target_total=3,
            expected_kind="blocked",
        )
        replay = service.extend(run_id, target_total=3)
        assert replay.safe_next_action.kind is SafeNextActionKind.INSPECT_BLOCKED

    def test_replay_from_aborted_returns_none_action(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, fixed_now = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        service.extend(run_id, target_total=3)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingForCursorFixState)
        aborted = AbortedState(
            run_id=state.run_id,
            version=state.version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            updated_at="2026-09-13T12:31:00.000000Z",
            context=state.context,
            aborted_at="2026-09-13T12:31:00.000000Z",
            abort_reason="user_requested_abort",
            prior_state_kind=state.kind,
            checkpoint=state.checkpoint,
            cursor=state.cursor,
            codex=state.codex,
        )
        with tick.store.begin_immediate() as conn:
            tick.store.release_reservation(
                conn,
                worktree_key=state.context.repository.worktree_key,
                now=fixed_now,
            )
        _cas_run_state(tick.store, run_id, new_state=aborted, now=fixed_now)
        _assert_replay_read_only(
            service,
            tick.store,
            run_id,
            target_total=3,
            expected_kind="aborted",
        )
        replay = service.extend(run_id, target_total=3)
        assert replay.safe_next_action.kind is SafeNextActionKind.NONE

    def test_replay_from_aborted_pending_cleanup_returns_tick_cleanup_action(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, fixed_now = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        service.extend(run_id, target_total=3)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingForCursorFixState)
            assert tick.store.get_reservation_for_run(conn, run_id) is not None
        aborted = AbortedState(
            run_id=state.run_id,
            version=state.version + 1,
            idempotency_key=state.idempotency_key,
            submitted_at=state.submitted_at,
            updated_at="2026-09-13T12:32:00.000000Z",
            context=state.context,
            aborted_at="2026-09-13T12:32:00.000000Z",
            abort_reason="user_requested_abort",
            prior_state_kind=state.kind,
            checkpoint=state.checkpoint,
            cursor=state.cursor,
            codex=state.codex,
        )
        _cas_run_state(tick.store, run_id, new_state=aborted, now=fixed_now)
        replay = service.extend(run_id, target_total=3)
        assert replay.state_kind == "aborted"
        assert replay.safe_next_action.kind is SafeNextActionKind.SCHEDULER_TICK
        assert "finalize cleanup" in (replay.safe_next_action.command or "")

    def test_replay_bypasses_git_validation_when_repository_drifts(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        run_id, tick, service, _ = _setup_maxed_run(
            git_repo,
            scheduler_paths,
            monkeypatch,
            review_sequence="findings,findings,no_findings",
        )
        service.extend(run_id, target_total=3)
        _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=80)
        (git_repo / "untracked-drift.txt").write_text("drift\n", encoding="utf-8")
        _assert_replay_read_only(
            service,
            tick.store,
            run_id,
            target_total=3,
            expected_kind="awaiting_codex_review",
        )


class TestReviewBudgetExtendCli:
    def test_cli_successful_new_grant_and_idempotent_replay_contracts(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
        run_id, tick, _, _ = _setup_maxed_run(
            git_repo,
            scheduler_paths,
            monkeypatch,
            review_sequence="findings,findings,no_findings",
        )
        db_path = scheduler_paths["db_path"]
        artifact_root = scheduler_paths["artifact_root"]
        before = _full_durable_snapshot(tick.store, run_id)
        grant = _cli_extend(
            run_id,
            target_total=3,
            db_path=db_path,
            artifact_root=artifact_root,
            output="json",
        )
        assert grant.exit_code == 0
        grant_payload = json.loads(grant.stdout)
        assert grant_payload["changed"] is True
        assert grant_payload["idempotent_replay"] is False
        assert grant_payload["new_effective_total"] == 3
        assert grant_payload["state_kind"] == "waiting_for_cursor_fix"
        assert "prompts/fixes" not in grant.stdout
        assert BOOTSTRAP_ID not in grant.stdout
        after_grant = _full_durable_snapshot(tick.store, run_id)
        assert after_grant["version"] != before["version"]
        assert len(after_grant["events"]) == len(before["events"]) + 1  # type: ignore[arg-type]
        assert _extension_correction_effect_count(tick.store, run_id) == 1
        replay_before = _full_durable_snapshot(tick.store, run_id)
        text_replay = _cli_extend(
            run_id,
            target_total=3,
            db_path=db_path,
            artifact_root=artifact_root,
            output="text",
        )
        assert text_replay.exit_code == 0
        assert "Idempotent replay: True" in text_replay.stdout
        assert "prompts/fixes" not in text_replay.stdout
        json_replay = _cli_extend(
            run_id,
            target_total=3,
            db_path=db_path,
            artifact_root=artifact_root,
            output="json",
        )
        assert json_replay.exit_code == 0
        replay_payload = json.loads(json_replay.stdout)
        assert replay_payload["changed"] is False
        assert replay_payload["idempotent_replay"] is True
        assert replay_payload["state_kind"] == "waiting_for_cursor_fix"
        assert replay_payload["safe_next_action"]["kind"] == SafeNextActionKind.SCHEDULER_TICK
        assert _full_durable_snapshot(tick.store, run_id) == replay_before

    def test_cli_rejects_lower_and_unrecorded_equal_targets_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, _, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        db_path = scheduler_paths["db_path"]
        before = _full_durable_snapshot(tick.store, run_id)
        artifact_root = scheduler_paths["artifact_root"]
        lower = _cli_extend(
            run_id,
            target_total=1,
            db_path=db_path,
            artifact_root=artifact_root,
            output="text",
        )
        assert lower.exit_code == EXIT_VALIDATION_ERROR
        assert "lower than the current effective total" in lower.stderr
        assert _full_durable_snapshot(tick.store, run_id) == before
        equal = _cli_extend(
            run_id,
            target_total=2,
            db_path=db_path,
            artifact_root=artifact_root,
            output="json",
        )
        assert equal.exit_code == EXIT_VALIDATION_ERROR
        assert "equals the current effective total" in equal.stderr
        assert _full_durable_snapshot(tick.store, run_id) == before


class TestReviewBudgetExtendGitAndReservationConflicts:
    def test_untracked_and_unstaged_drift_reject_new_grant_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        (git_repo / "untracked.txt").write_text("new\n", encoding="utf-8")
        with pytest.raises(SchedulerEngineError, match="untracked non-ignored files"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == before
        _git(git_repo, "clean", "-fd")
        tracked = git_repo / "docs/plans/sample-plan.md"
        tracked.write_text(tracked.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with pytest.raises(SchedulerEngineError, match="tracked unstaged changes"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == before

    def test_head_drift_rejects_new_grant_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        (git_repo / "head-drift.txt").write_text("advance\n", encoding="utf-8")
        _git(git_repo, "add", "head-drift.txt")
        _git(git_repo, "commit", "-m", "advance head")
        with pytest.raises(SchedulerEngineError, match="repository HEAD changed"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == before

    def test_branch_drift_rejects_new_grant_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        _git(git_repo, "checkout", "-b", "review-budget-drift")
        with pytest.raises(SchedulerEngineError, match="repository branch changed"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == before

    def test_staged_patch_drift_rejects_new_grant_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        extra = git_repo / "extra-staged.txt"
        extra.write_text("extra staged drift\n", encoding="utf-8")
        _git(git_repo, "add", "extra-staged.txt")
        with pytest.raises(SchedulerEngineError, match="staged index no longer matches"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == before

    def test_active_attempt_and_conflicting_reservation_fail_closed(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, fixed_now = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _durable_snapshot(tick.store, run_id)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, MaxIterationsReachedState)
        with tick.store.begin_read() as conn:
            attempt_row = conn.execute(
                """
                SELECT attempt_id FROM scheduler_attempts
                WHERE run_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        assert attempt_row is not None
        with tick.store.begin_immediate() as conn:
            conn.execute(
                """
                UPDATE scheduler_attempts
                SET status = 'launching', updated_at = updated_at
                WHERE attempt_id = ?
                """,
                (str(attempt_row[0]),),
            )
        with pytest.raises(SchedulerEngineError, match="attempt is active"):
            service.extend(run_id, target_total=3)
        assert _durable_snapshot(tick.store, run_id) == before
        with tick.store.begin_immediate() as conn:
            conn.execute(
                """
                UPDATE scheduler_attempts
                SET status = 'completed', updated_at = updated_at
                WHERE run_id = ?
                """,
                (run_id,),
            )
        holder_run_id = _submit(git_repo, scheduler_paths)
        start_run(holder_run_id, db_path=scheduler_paths["db_path"])
        run_before_conflict = _run_records_snapshot(tick.store, run_id)
        with pytest.raises(SchedulerEngineError, match="held by another run"):
            service.extend(run_id, target_total=3)
        assert _run_records_snapshot(tick.store, run_id) == run_before_conflict


class TestReviewBudgetExtendConcurrency:
    def test_injected_failures_roll_back_without_partial_writes(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        snapshot_before = _full_durable_snapshot(tick.store, run_id)
        original_reacquire = tick.store.reacquire_released_reservation_for_run

        def fail_reacquire(*args: object, **kwargs: object) -> bool:
            return False

        tick.store.reacquire_released_reservation_for_run = fail_reacquire  # type: ignore[method-assign]
        with pytest.raises(SchedulerEngineError, match="could not be reacquired"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == snapshot_before
        tick.store.reacquire_released_reservation_for_run = original_reacquire  # type: ignore[method-assign]

        original_append = tick.store.append_event

        def fail_append(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected append failure")

        tick.store.append_event = fail_append  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected append failure"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == snapshot_before
        tick.store.append_event = original_append  # type: ignore[method-assign]

        original_cas = tick.store.compare_and_swap_state

        def fail_cas(*args: object, **kwargs: object) -> bool:
            return False

        tick.store.compare_and_swap_state = fail_cas  # type: ignore[method-assign]
        with pytest.raises(SchedulerEngineError, match="concurrent state update"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == snapshot_before
        tick.store.compare_and_swap_state = original_cas  # type: ignore[method-assign]

        def fail_insert(*args: object, **kwargs: object) -> None:
            raise RuntimeError("injected effect failure")

        tick.store.insert_effect = fail_insert  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected effect failure"):
            service.extend(run_id, target_total=3)
        assert _full_durable_snapshot(tick.store, run_id) == snapshot_before

    def test_concurrent_identical_extend_requests_do_not_double_grant(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        before_commit = threading.Event()
        release_first = threading.Event()
        results: list[object] = []
        errors: list[Exception] = []

        def first_worker() -> None:
            try:
                results.append(service.extend(run_id, target_total=3))
            except Exception as exc:
                errors.append(exc)

        def second_worker() -> None:
            before_commit.wait(timeout=5)
            try:
                results.append(service.extend(run_id, target_total=3))
            except Exception as exc:
                errors.append(exc)
            finally:
                release_first.set()

        original_reacquire = tick.store.reacquire_released_reservation_for_run

        def slow_reacquire(*args: object, **kwargs: object) -> bool:
            before_commit.set()
            release_first.wait(timeout=5)
            return original_reacquire(*args, **kwargs)  # type: ignore[arg-type]

        tick.store.reacquire_released_reservation_for_run = slow_reacquire  # type: ignore[method-assign]
        first = threading.Thread(target=first_worker)
        second = threading.Thread(target=second_worker)
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not errors
        assert len(results) == 2
        changed_count = sum(1 for item in results if getattr(item, "changed", False))
        replay_count = sum(1 for item in results if getattr(item, "idempotent_replay", False))
        assert changed_count == 1
        assert replay_count == 1
        after = _full_durable_snapshot(tick.store, run_id)
        assert len(after["events"]) == len(before["events"]) + 1  # type: ignore[arg-type]
        assert len(after["effects"]) == len(before["effects"]) + 1  # type: ignore[arg-type]
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            extension_events = conn.execute(
                """
                SELECT COUNT(*) FROM scheduler_events
                WHERE run_id = ? AND event_kind = ?
                """,
                (run_id, REVIEW_BUDGET_EXTENDED_EVENT_KIND),
            ).fetchone()[0]
            effective = effective_review_ceiling_for_run(tick.store, conn, state)
        assert int(extension_events) == 1
        assert effective == 3
        assert _extension_correction_effect_count(tick.store, run_id) == 1

    def test_concurrent_different_higher_targets_grant_once_with_monotonic_ceiling(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        before = _full_durable_snapshot(tick.store, run_id)
        first_at_append = threading.Event()
        second_observed_exhausted = threading.Event()
        release = threading.Event()
        results: list[object] = []
        errors: list[Exception] = []

        def worker(target: int) -> None:
            threading.current_thread().name = f"extend-worker-{target}"
            try:
                results.append(service.extend(run_id, target_total=target))
            except Exception as exc:
                errors.append(exc)

        original_append = tick.store.append_event
        original_begin_immediate = tick.store.begin_immediate

        def gated_append(*args: object, **kwargs: object) -> object:
            first_at_append.set()
            assert release.wait(timeout=5), "timed out waiting to commit first grant"
            return original_append(*args, **kwargs)  # type: ignore[arg-type]

        def sync_begin_immediate() -> object:
            if threading.current_thread().name == "extend-worker-4":
                second_observed_exhausted.set()
                assert release.wait(timeout=5), (
                    "timed out waiting for synchronized concurrent recheck"
                )
            return original_begin_immediate()

        tick.store.append_event = gated_append  # type: ignore[method-assign]
        tick.store.begin_immediate = sync_begin_immediate  # type: ignore[method-assign]
        first = threading.Thread(target=lambda: worker(3), name="extend-thread-3")
        second = threading.Thread(target=lambda: worker(4), name="extend-thread-4")
        first.start()
        assert first_at_append.wait(timeout=5), "first grant did not reach append gate"
        second.start()
        assert second_observed_exhausted.wait(timeout=5), (
            "second grant did not observe exhausted state before release"
        )
        release.set()
        first.join(timeout=10)
        second.join(timeout=10)
        assert not first.is_alive()
        assert not second.is_alive()
        assert len(results) + len(errors) == 2
        assert len(errors) == 1
        assert isinstance(errors[0], SchedulerEngineError)
        assert errors[0].kind is SchedulerEngineErrorKind.CONFLICT
        assert "state changed concurrently" in str(errors[0])
        success = results[0]
        assert getattr(success, "changed", False) is True
        assert getattr(success, "new_effective_total", None) == 3
        after = _full_durable_snapshot(tick.store, run_id)
        assert len(after["events"]) == len(before["events"]) + 1  # type: ignore[arg-type]
        assert len(after["effects"]) == len(before["effects"]) + 1  # type: ignore[arg-type]
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            effective = effective_review_ceiling_for_run(tick.store, conn, state)
        assert effective == 3
        assert _extension_correction_effect_count(tick.store, run_id) == 1


class TestReviewBudgetExtendProjections:
    def test_status_controller_and_history_disclose_safe_totals_only(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        run_id, tick, service, _ = _setup_maxed_run(git_repo, scheduler_paths, monkeypatch)
        service.extend(run_id, target_total=3)
        store = tick.store
        status = SchedulerStatusService(store).get_status(run_id)
        assert status.summary.max_review_iterations == 3
        assert status.summary.submitted_max_review_iterations == 2
        assert status.summary.review_iterations_completed >= 2
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            capacity = store.get_capacity_row(conn)
            holder = (
                str(capacity["holder_run_id"]) if capacity["holder_run_id"] is not None else None
            )
            candidate = _candidate_from_state(
                store,
                conn,
                state,
                holder_run_id=holder,
            )
        assert candidate.max_review_iterations == 3
        history = SchedulerHistoryService(store).get_history(run_id, limit=20)
        rendered = json.dumps(
            {
                "status": status.model_dump(mode="json"),
                "candidate": candidate.model_dump(mode="json"),
                "history": history.model_dump(mode="json"),
            }
        )
        assert "prompts/fixes" not in rendered
        assert BOOTSTRAP_ID not in rendered
        assert "git/diffs" not in rendered


class TestReviewBudgetSequenceNonAdvancement:
    def test_sequence_does_not_advance_on_budget_grant(
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
        sequence_id = _prepare_sequence(git_repo, scheduler_paths)
        start = _start_service(scheduler_paths).start(sequence_id)
        run_id = start.run_id
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
        store = tick.store
        with store.begin_read() as conn:
            sequence_before = store.load_validated_sequence_state(conn, sequence_id)
            assert isinstance(sequence_before, ActiveSequenceState)
        service = ReviewBudgetExtendService(
            store,
            tick.artifacts,
            now_factory=lambda: fixed_now,
        )
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            effective = effective_review_ceiling_for_run(store, conn, state)
        service.extend(run_id, target_total=effective + 1)
        sequence_status = SequenceStatusService(
            SqliteSchedulerStore.open_readonly(scheduler_paths["db_path"])
        ).get_status(sequence_id)
        with store.begin_read() as conn:
            sequence_after = store.load_validated_sequence_state(conn, sequence_id)
        assert isinstance(sequence_after, ActiveSequenceState)
        assert sequence_after.current_ordinal == sequence_before.current_ordinal
        assert sequence_after.current_run_id == sequence_before.current_run_id
        assert sequence_after.version == sequence_before.version
        assert sequence_after.updated_at == sequence_before.updated_at
        assert sequence_status.state_kind == ACTIVE_SEQUENCE_STATE_KIND
        assert sequence_status.current_ordinal == sequence_before.current_ordinal
        assert "Phase 20.3" not in (sequence_status.safe_next_action.command or "")
