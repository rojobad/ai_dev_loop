"""Unit tests for scheduler start authorization."""

from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state

from ai_dev_loop.scheduler.application.start import StartService
from ai_dev_loop.scheduler.application.submission import SubmissionService, SubmitOptions
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.repository_target import RepositoryTarget
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


def _insert_queued_run(store: SqliteSchedulerStore, *, run_id: str = "fixture-run") -> None:
    state = sample_submitted_state(run_id=run_id)
    event = RunSubmittedEvent(
        run_id=state.run_id,
        idempotency_key=state.idempotency_key,
        worktree_key=state.context.repository.worktree_key,
        reused_existing=False,
    )
    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=UTC)
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=state.run_id,
            state=state,
            event_id="evt-submit",
            event=event,
            now=now,
        )


def test_start_authorizes_queued_run(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    _insert_queued_run(store)
    service = StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC))
    result = service.start("fixture-run")
    assert result.changed is True
    assert result.state_kind == "authorized"
    assert result.safe_next_action.command == "ai_dev_loop scheduler tick"
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, "fixture-run")
        assert state.kind == "authorized"


def test_start_is_idempotent(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    _insert_queued_run(store)
    service = StartService(store, now_factory=lambda: datetime(2026, 9, 4, 12, 1, tzinfo=UTC))
    first = service.start("fixture-run")
    second = service.start("fixture-run")
    assert first.changed is True
    assert second.changed is False
    assert second.idempotent_replay is True


def test_submit_created_reservation_required_at_start(tmp_path: Path) -> None:
    store = SqliteSchedulerStore(tmp_path / "engine.sqlite3")
    state = sample_submitted_state(run_id="fixture-run")
    with store.begin_immediate() as conn:
        kind, payload, digest = store.dump_state(state)
        conn.execute(
            """
            INSERT INTO scheduler_runs(
                run_id, state_kind, state_payload, state_payload_sha256,
                version, idempotency_key, worktree_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                state.run_id,
                kind,
                payload,
                digest,
                state.version,
                state.idempotency_key,
                state.context.repository.worktree_key,
                "2026-09-04T12:00:00.000000Z",
                "2026-09-04T12:00:00.000000Z",
            ),
        )
    service = StartService(store)
    with pytest.raises(Exception, match="reservation"):
        service.start("fixture-run")


def test_submit_time_reservation_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "engine.sqlite3"
    artifacts = tmp_path / "artifacts"
    repo = FIXTURE_REPO
    store = SqliteSchedulerStore(db)
    submission = SubmissionService(
        store,
        ProtectedArtifactStore(artifacts),
        repository_discoverer=lambda _path: RepositoryTarget(root=repo.resolve()),
    )
    prompt = (repo / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    options = SubmitOptions(
        repo_path=repo,
        plan_path=Path("docs/plans/sample-plan.md"),
        prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
        controller_session_id=CONTROLLER_SESSION,
        codex_review_model="gpt-5.6-sol",
        codex_review_reasoning_effort="high",
        db_path=db,
        artifact_root=artifacts,
    )
    with patch("sys.stdin", StringIO(prompt)):
        first = submission.submit(options)
    with patch("sys.stdin", StringIO(prompt)), pytest.raises(Exception, match="reservation"):
        submission.submit(
            SubmitOptions(
                repo_path=repo,
                plan_path=Path("docs/plans/sample-plan.md"),
                prompt_source_path=Path("docs/plans/prompt_sample-plan.txt"),
                controller_session_id=CONTROLLER_SESSION,
                codex_review_model="gpt-5.6-sol",
                codex_review_reasoning_effort="high",
                db_path=db,
                artifact_root=artifacts,
                project_name="other-name",
            )
        )
    assert first.reused_existing is False
