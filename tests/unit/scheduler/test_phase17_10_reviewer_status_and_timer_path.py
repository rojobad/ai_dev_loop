"""Regression tests for Phase 17.10 reviewer status projection and timer PATH."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import (
    CONTROLLER_SESSION,
    REVIEWER_SESSION,
    sample_fresh_submitted_context,
    sample_legacy_submitted_context,
    sample_submitted_state,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.commands.controller import controller_status, render_controller_status
from ai_dev_loop.commands.scheduler import render_list_output, render_status_output
from ai_dev_loop.scheduler.application.contracts import (
    redacted_session_prefix,
    reviewer_session_id_prefix_for_projection,
    summary_from_context,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import (
    SchedulerStatusService,
    scheduler_list,
    scheduler_status,
)
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.application.timer_ops import install_scheduler_timer
from ai_dev_loop.scheduler.domain.events import RunSubmittedEvent
from ai_dev_loop.scheduler.domain.state import (
    AdmittedRunCheckpoint,
    AwaitingCodexReviewState,
    CodexWorkflowCheckpoint,
    CursorWorkflowCheckpoint,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.scheduler.infrastructure.systemd_assets import (
    EXPECTED_SERVICE_EXEC_START,
    EXPECTED_SERVICE_PATH,
    load_service_template,
    validate_packaged_assets,
)

BOOTSTRAP_ID = "019def00-0000-0000-0000-0000000000bb"
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


def test_reviewer_prefix_projection_rules() -> None:
    fresh_context = sample_fresh_submitted_context()
    legacy_context = sample_legacy_submitted_context()

    assert reviewer_session_id_prefix_for_projection(fresh_context) is None
    assert reviewer_session_id_prefix_for_projection(
        fresh_context,
        bound_reviewer_session_id=BOOTSTRAP_ID,
    ) == redacted_session_prefix(BOOTSTRAP_ID)
    assert reviewer_session_id_prefix_for_projection(legacy_context) == redacted_session_prefix(
        REVIEWER_SESSION
    )
    assert reviewer_session_id_prefix_for_projection(
        legacy_context,
        bound_reviewer_session_id=BOOTSTRAP_ID,
    ) == redacted_session_prefix(REVIEWER_SESSION)


def test_summary_projection_uses_bound_state_without_changing_safe_action() -> None:
    context = sample_fresh_submitted_context()
    unbound = summary_from_context(
        run_id="fixture-run",
        state_kind="awaiting_codex_review",
        submitted_at="2026-09-09T12:00:00.000000Z",
        updated_at="2026-09-09T12:00:00.000000Z",
        context=context,
    )
    bound = summary_from_context(
        run_id="fixture-run",
        state_kind="awaiting_codex_review",
        submitted_at="2026-09-09T12:00:00.000000Z",
        updated_at="2026-09-09T12:00:00.000000Z",
        context=context,
        bound_reviewer_session_id=BOOTSTRAP_ID,
    )
    assert unbound.reviewer_session_id_prefix is None
    assert bound.reviewer_session_id_prefix == redacted_session_prefix(BOOTSTRAP_ID)
    assert unbound.safe_next_action == bound.safe_next_action


def test_status_list_and_controller_show_bound_fresh_reviewer_prefix(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")

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
    )
    with patch("sys.stdin", StringIO(prompt)):
        run_id = submit_run(options).run_id

    queued_status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    assert queued_status.summary.reviewer_session_id_prefix is None

    start_run(run_id, db_path=scheduler_paths["db_path"])
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    tick = TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: datetime(2026, 9, 9, 16, 0, tzinfo=UTC),
        tick_owner_factory=lambda: f"tick-17-10-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
        preflight_port=OkPreflightPort(),
    )
    for _ in range(60):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == "completed":
                break
            if state.kind == "blocked":
                pytest.fail(f"run blocked: {state.block_reason_kind} {state.block_reason_summary}")
    assert state.kind == "completed"

    status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    listings = scheduler_list(db_path=scheduler_paths["db_path"])
    status_text = render_status_output(status, output="text")
    list_text = render_list_output(listings, output="json")
    controller = controller_status(
        controller_session_id=CONTROLLER_SESSION,
        repo_path=git_repo,
        run_id=run_id,
        include_terminal=True,
    )
    controller_text = render_controller_status(controller, output="json")

    expected_prefix = redacted_session_prefix(BOOTSTRAP_ID)
    assert status.summary.reviewer_session_id_prefix == expected_prefix
    assert listings[0].reviewer_session_id_prefix == expected_prefix
    assert controller.reviewer_session_id_prefix == expected_prefix

    blob = "\n".join([status_text, list_text, controller_text])
    assert BOOTSTRAP_ID not in blob
    assert expected_prefix in blob


def test_status_service_reads_bound_prefix_from_validated_state(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    submitted = sample_submitted_state(repo_root=str(tmp_path / "repo"))
    now = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
    checkpoint = AdmittedRunCheckpoint(
        authorized_at="2026-09-09T11:00:00.000000Z",
        authorized_controller_session_id=CONTROLLER_SESSION,
        admitted_at="2026-09-09T11:05:00.000000Z",
        admission_status_artifact_path="admission/status.txt",
        admission_status_sha256="a" * 64,
    )
    state = AwaitingCodexReviewState(
        run_id=submitted.run_id,
        version=1,
        submitted_at=submitted.submitted_at,
        updated_at="2026-09-09T12:00:00.000000Z",
        idempotency_key=submitted.idempotency_key,
        context=submitted.context,
        checkpoint=checkpoint,
        cursor=CursorWorkflowCheckpoint(
            iteration=1,
            staged_patch_path="git/diffs/01.patch",
            staged_patch_sha256="c" * 64,
        ),
        codex=CodexWorkflowCheckpoint(reviewer_session_id=BOOTSTRAP_ID),
    )
    with store.begin_immediate() as conn:
        store.insert_submitted_run(
            conn,
            run_id=submitted.run_id,
            state=submitted,
            event_id="evt-submit",
            event=RunSubmittedEvent(
                run_id=submitted.run_id,
                idempotency_key=submitted.idempotency_key,
                worktree_key=submitted.context.repository.worktree_key,
                reused_existing=False,
            ),
            now=now,
        )
        store.compare_and_swap_state(
            conn,
            run_id=submitted.run_id,
            expected_version=1,
            new_state=state,
            now=now,
        )

    status = SchedulerStatusService(store).get_status(submitted.run_id)
    assert status.summary.reviewer_session_id_prefix == redacted_session_prefix(BOOTSTRAP_ID)
    assert BOOTSTRAP_ID not in render_status_output(status, output="json")


def test_packaged_service_uses_env_and_constrained_path() -> None:
    service = load_service_template()
    assert f"Environment=PATH={EXPECTED_SERVICE_PATH}" in service
    assert f"ExecStart={EXPECTED_SERVICE_EXEC_START}" in service
    assert validate_packaged_assets() == []


def test_packaged_service_validation_rejects_missing_path_contract() -> None:
    from ai_dev_loop.scheduler.infrastructure import systemd_assets

    original_loader = systemd_assets.load_service_template

    def broken_service() -> str:
        return "[Service]\nType=oneshot\nExecStart=ai_dev_loop scheduler tick\n"

    systemd_assets.load_service_template = broken_service
    try:
        errors = validate_packaged_assets()
    finally:
        systemd_assets.load_service_template = original_loader

    assert any("constrained PATH" in error for error in errors)
    assert any("/usr/bin/env" in error for error in errors)


def test_timer_install_writes_path_contract(tmp_path: Path) -> None:
    def runner(args: list[str], **kwargs: object) -> object:
        from ai_dev_loop.process import ProcessResult

        return ProcessResult(args=list(args), returncode=0, stdout="", stderr="")

    result = install_scheduler_timer(
        enable=False,
        unit_dir=tmp_path / "systemd/user",
        runner=runner,
    )
    service_text = Path(result.service_unit_path).read_text(encoding="utf-8")
    assert f"Environment=PATH={EXPECTED_SERVICE_PATH}" in service_text
    assert f"ExecStart={EXPECTED_SERVICE_EXEC_START}" in service_text
