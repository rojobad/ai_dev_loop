"""Phase 20.1.1 retryable Codex reviewer failure tests."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.response_schema import events_text_indicates_usage_limit_exceeded
from ai_dev_loop.runners.codex_failure import (
    classify_codex_review_events_text,
    is_operational_review_block_kind,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.state import WaitingCodexReviewRetryState
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
    backend: FakeAgentProcessBackend,
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-20-1-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 60,
) -> None:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
            if state.kind == "blocked":
                kind = getattr(state, "block_reason_kind", "")
                summary = getattr(state, "block_reason_summary", "")
                raise AssertionError(f"run blocked: {kind}: {summary}")
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


class TestMessageOnlyUsageLimitClassifier:
    def test_message_only_events_are_provider_message_limit(self) -> None:
        text = "\n".join(
            [
                json.dumps({"type": "error", "message": "usage_limit_exceeded"}),
                json.dumps({"type": "turn.failed", "error": {"message": "usage_limit_exceeded"}}),
            ]
        )
        classification = classify_codex_review_events_text(text)
        assert classification.is_usage_limit
        assert classification.is_provider_message_limit
        assert not events_text_indicates_usage_limit_exceeded(text)


class TestSchemaMigration:
    def test_v6_migration_adds_review_retry_tables(self, tmp_path: Path) -> None:
        db = tmp_path / "engine.sqlite3"
        paused = False

        def pause_v7(statement: str) -> None:
            nonlocal paused
            if not paused and "CREATE TABLE scheduler_checkpoint_holds" in statement:
                paused = True
                raise RuntimeError("pause-v7")

        with pytest.raises(RuntimeError, match="pause-v7"):
            SqliteSchedulerStore(db, migration_fault_hook=pause_v7)
        import sqlite3

        conn = sqlite3.connect(db)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 6
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            conn.close()
        assert "scheduler_review_retry_generations" in tables
        assert "scheduler_review_recovery_successors" in tables


class TestOperationalFailureRouting:
    def test_message_only_limit_with_exhausted_probe_waits_for_capacity(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 12, 18, 15, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.codex.reviewer_session_id == BOOTSTRAP_ID
            assert state.codex.capacity_evidence_source == "provider_message_limit"
            assert state.codex.inferred_operational_failure_kind is None

    def test_operational_failure_with_available_probe_enters_manual_retry(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 12, 18, 20, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_review_retry", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingCodexReviewRetryState)
            assert is_operational_review_block_kind(state.codex.review_retry_failure_kind or "")
            reservation = tick.store.get_reservation_for_run(conn, run_id)
            assert reservation is not None


class TestManualReviewRetry:
    def test_review_retry_is_idempotent_and_process_free(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 12, 18, 25, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_review_retry", max_ticks=80)
        store = SqliteSchedulerStore(scheduler_paths["db_path"])
        artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
        service = ReviewRetryService(store, artifacts)
        first = service.retry(run_id)
        assert first.changed is True
        assert first.state_kind == "awaiting_codex_review"
        second = service.retry(run_id)
        assert second.idempotent_replay is True
        assert second.state_kind == "awaiting_codex_review"
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
        tick.run_once()
        _run_until(tick, run_id, target_kind="completed", max_ticks=80)
        codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
        assert codex_log.count("'resume'") >= 1


class TestFalseQuotaInference:
    def test_inferred_quota_retry_then_manual_retry_on_repeat_failure(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 12, 18, 30, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
        for _ in range(80):
            tick.run_once()
            with tick.store.begin_read() as conn:
                state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_codex_review_retry":
                break
        else:
            raise AssertionError(f"expected waiting_codex_review_retry, got {state.kind}")
