"""Phase 21.4 mandatory acceptance matrix (C-01 through C-06)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import jsonschema
import pytest
from tests.conftest import FIXTURE_REPO
from tests.integration.phase21_4_helpers import (
    BOOTSTRAP_ID,
    FIX_PROMPT_ALPHA,
    FIX_PROMPT_BETA,
    MARKDOWN_ALPHA,
    MARKDOWN_BETA_PREFIX,
    PerTickClock,
    build_bootstrap_codex_invocation_evidence,
    codex_bootstrap_invocation_count,
    codex_log_text,
    codex_resume_invocation_count,
    codex_runner_attempt_ids,
    codex_runner_launch_count,
    expected_reviewer_session_ref,
    fetch_review_content_bytes,
    fetch_review_content_chunks,
    install_pre_21_4_historical_review_fixture,
    invoke_cli,
    make_tick_service,
    pending_bootstrap_dispatch_id,
    run_tick_once,
    run_until,
    submit_sample_run,
)

from ai_dev_loop.commands.scheduler import render_scheduler_history_output, render_status_output
from ai_dev_loop.integration_api.review_service import default_review_read_service
from ai_dev_loop.iterations import fix_prompt_path
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.history import scheduler_history
from ai_dev_loop.scheduler.application.status import scheduler_status
from ai_dev_loop.scheduler.domain.codex_contract import (
    RESUME_CODEX_REVIEW_EFFECT_KIND,
    REVIEW_RETRY_OPERATIONAL_ENVELOPE,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
    codex_review_result_rel,
)
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root

SECRET_SENTINEL = "INTEGRATION_PHASE21_4_SECRET_SENTINEL"
LARGE_MARKDOWN_PAD_BYTES = 90_000


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


@pytest.fixture(autouse=True)
def _fresh_codex_review_counter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(tmp_path / "codex-review-counter.txt"))


def test_c01_bootstrap_resume_and_operational_retry_witness(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,invalid_json,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_COUNTER", str(tmp_path / "review-counter.txt"))
    witness_dir = tmp_path / "witness"
    monkeypatch.setenv("FAKE_CODEX_STDIN_SHA256_WITNESS_DIR", str(witness_dir))
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=200)
    witnesses = sorted(witness_dir.glob("witness-*.sha256"))
    assert len(witnesses) >= 3
    resume_flags = [
        (witness_dir / name.replace(".sha256", ".resume")).read_text(encoding="utf-8").strip()
        for name in [w.name for w in witnesses]
    ]
    assert resume_flags[0] == "0"
    assert any(flag == "1" for flag in resume_flags[1:])
    codex_log = codex_log_text(fake_clis)
    assert codex_bootstrap_invocation_count(codex_log) == 1
    assert codex_resume_invocation_count(codex_log) >= 1
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    with tick.store.begin_read() as conn:
        rows, _ = tick.store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=20)
    review_iteration = 2
    iter_resumes = [
        row
        for row in rows
        if int(row["iteration"]) == review_iteration
        and str(row["effect_kind"]) == RESUME_CODEX_REVIEW_EFFECT_KIND
    ]
    assert len(iter_resumes) >= 2
    retry_attempts = [
        row
        for row in iter_resumes
        if (run_root / codex_review_prompt_rel(review_iteration, str(row["attempt_id"])))
        .read_text(encoding="utf-8")
        .startswith(REVIEW_RETRY_OPERATIONAL_ENVELOPE)
    ]
    assert retry_attempts, "expected operational retry envelope on a same-iteration retry"
    for row in iter_resumes:
        attempt_id = str(row["attempt_id"])
        prompt_path = run_root / codex_review_prompt_rel(review_iteration, attempt_id)
        evidence_path = run_root / codex_review_prompt_evidence_rel(review_iteration, attempt_id)
        prompt_bytes = prompt_path.read_bytes()
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["prompt_sha256"] == hashlib.sha256(prompt_bytes).hexdigest()
        assert evidence["prompt_size_bytes"] == len(prompt_bytes)


def test_c02_tick_ingestion_preserves_single_bootstrap_identity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "invalid_json,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=200)
    codex_log = codex_log_text(fake_clis)
    assert codex_bootstrap_invocation_count(codex_log) == 1
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID


def test_c02_after_prompt_fault_integration_then_recovery(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    service = default_review_read_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    resume_before_fault = codex_resume_invocation_count(codex_log_text(fake_clis))
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "after_prompt")
    fault_attempt_id: str | None = None
    for _ in range(40):
        tick.run_once()
        with tick.store.begin_read() as conn:
            rows, _ = tick.store.list_integration_codex_review_rows(
                conn, run_id, offset=0, limit=20
            )
            for row in rows:
                attempt_id = str(row["attempt_id"])
                iteration = int(row["iteration"])
                prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
                evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
                if prompt_path.is_file() and not evidence_path.is_file():
                    detail = service.inspect_review(run_id, attempt_id=attempt_id)
                    if detail.content.prompt.reason == "incomplete_capture":
                        fault_attempt_id = attempt_id
                        break
            if fault_attempt_id:
                break
    assert fault_attempt_id is not None, "expected after_prompt partial publication"
    assert codex_resume_invocation_count(codex_log_text(fake_clis)) == resume_before_fault
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind in {"awaiting_codex_review", "blocked", "waiting_codex_review_retry"}


def test_c02_after_evidence_fault_leaves_pair_without_codex_child(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    resume_before = codex_resume_invocation_count(codex_log_text(fake_clis))
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "after_evidence")
    for _ in range(40):
        tick.run_once()
        with tick.store.begin_read() as conn:
            rows, _ = tick.store.list_integration_codex_review_rows(
                conn, run_id, offset=0, limit=20
            )
            for row in rows:
                attempt_id = str(row["attempt_id"])
                iteration = int(row["iteration"])
                prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
                evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
                if prompt_path.is_file() and evidence_path.is_file():
                    assert codex_resume_invocation_count(codex_log_text(fake_clis)) == resume_before
                    return
    pytest.fail("expected prompt+evidence without Codex resume launch after after_evidence fault")


def test_c02_production_runner_identical_prelaunch_replay(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler import codex_attempt_runner

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    attempt_id = f"att-{2:032x}"
    dispatch_id = pending_bootstrap_dispatch_id(tick, run_id)
    evidence = build_bootstrap_codex_invocation_evidence(
        tick,
        run_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
    )
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    publish_calls = {"count": 0}
    original_publish = codex_attempt_runner.publish_review_prompt_before_launch

    def counting_publish(*args: object, **kwargs: object) -> object:
        publish_calls["count"] += 1
        return original_publish(*args, **kwargs)

    launched = {"called": False}

    def block_codex_child(*args: object, **kwargs: object) -> object:
        launched["called"] = True
        raise AssertionError("codex child must not execute")

    monkeypatch.setattr(
        codex_attempt_runner,
        "publish_review_prompt_before_launch",
        counting_publish,
    )
    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", block_codex_child)
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_launch")
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=scheduler_paths["artifact_root"],
        )
    assert publish_calls["count"] == 1
    assert not launched["called"]
    iteration = int(evidence["review_iteration"])
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
    original_prompt = prompt_path.read_bytes()
    original_evidence = evidence_path.read_bytes()
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=scheduler_paths["artifact_root"],
        )
    assert publish_calls["count"] == 2
    assert not launched["called"]
    assert prompt_path.read_bytes() == original_prompt
    assert evidence_path.read_bytes() == original_evidence


def test_c02_production_runner_conflicting_prelaunch_bytes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler import codex_attempt_runner

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    attempt_id = f"att-{3:032x}"
    dispatch_id = pending_bootstrap_dispatch_id(tick, run_id)
    evidence = build_bootstrap_codex_invocation_evidence(
        tick,
        run_id,
        attempt_id=attempt_id,
        dispatch_id=dispatch_id,
    )
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    launched = {"called": False}

    def block_codex_child(*args: object, **kwargs: object) -> object:
        launched["called"] = True
        raise AssertionError("codex child must not execute")

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", block_codex_child)
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_launch")
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=scheduler_paths["artifact_root"],
        )
    iteration = int(evidence["review_iteration"])
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    before_conflict = prompt_path.read_bytes()
    prompt_path.write_bytes(before_conflict + b"conflict")
    monkeypatch.delenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", raising=False)
    with pytest.raises(RuntimeError):
        codex_attempt_runner._run_codex_review(
            evidence,
            run_root,
            run_id,
            artifact_root=scheduler_paths["artifact_root"],
        )
    assert not launched["called"]
    assert prompt_path.read_bytes() == before_conflict + b"conflict"


def test_c02_scheduler_reconciliation_preserves_bytes_after_before_launch_fault(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    runner_before = codex_runner_launch_count(backend)
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_launch")
    attempt_id: str | None = None
    iteration = 0
    for _ in range(40):
        tick.run_once()
        with tick.store.begin_read() as conn:
            rows, _ = tick.store.list_integration_codex_review_rows(
                conn, run_id, offset=0, limit=10
            )
            for row in rows:
                aid = str(row["attempt_id"])
                it = int(row["iteration"])
                prompt_path = run_root / codex_review_prompt_rel(it, aid)
                evidence_path = run_root / codex_review_prompt_evidence_rel(it, aid)
                if prompt_path.is_file() and evidence_path.is_file():
                    attempt_id = aid
                    iteration = it
                    break
            if attempt_id:
                break
    assert attempt_id is not None
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
    original_prompt = prompt_path.read_bytes()
    original_evidence = evidence_path.read_bytes()
    assert codex_runner_launch_count(backend) == runner_before + 1
    monkeypatch.delenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", raising=False)
    launches_after_publish = codex_runner_launch_count(backend)
    terminal_kind = "running"
    for _ in range(30):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            terminal_kind = state.kind
    assert prompt_path.read_bytes() == original_prompt
    assert evidence_path.read_bytes() == original_evidence
    assert codex_runner_launch_count(backend) == launches_after_publish
    assert terminal_kind in {
        "blocked",
        "waiting_codex_review_retry",
        "awaiting_codex_review",
        "completed",
    }


def test_c02_scheduler_reconciliation_blocks_conflicting_bytes_without_codex_child(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=1, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    monkeypatch.setenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", "before_launch")
    attempt_id: str | None = None
    iteration = 0
    for _ in range(40):
        tick.run_once()
        with tick.store.begin_read() as conn:
            rows, _ = tick.store.list_integration_codex_review_rows(
                conn, run_id, offset=0, limit=10
            )
            for row in rows:
                aid = str(row["attempt_id"])
                it = int(row["iteration"])
                prompt_path = run_root / codex_review_prompt_rel(it, aid)
                evidence_path = run_root / codex_review_prompt_evidence_rel(it, aid)
                if prompt_path.is_file() and evidence_path.is_file():
                    attempt_id = aid
                    iteration = it
                    break
            if attempt_id:
                break
    assert attempt_id is not None
    runner_at_published = codex_runner_launch_count(backend)
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    prompt_path.write_bytes(prompt_path.read_bytes() + b"tamper")
    monkeypatch.delenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", raising=False)
    terminal_kind = "running"
    for _ in range(20):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            terminal_kind = state.kind
            if state.kind in {"blocked", "waiting_codex_review_retry", "failed"}:
                break
    assert codex_runner_launch_count(backend) == runner_at_published
    assert terminal_kind in {"blocked", "waiting_codex_review_retry", "failed"}


def test_c02_uncertain_ownership_prompt_evidence_does_not_relaunch_codex(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler.application.attempt_backend import ObserveResult, UnitLifecycleState
    from ai_dev_loop.scheduler.application.codex_review_prompt_evidence import (
        publish_review_prompt_before_launch,
    )

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=3, exit_code=0)
    )
    tick = make_tick_service(git_repo, scheduler_paths, backend=backend)
    run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=40)
    run_tick_once(tick)
    codex_launches = [
        call
        for call in backend.launch_calls
        if any("codex_attempt_runner" in part for part in call.agent_argv)
    ]
    assert len(codex_launches) == 1
    attempt_id = codex_launches[0].attempt_id
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        iteration = state.cursor.iteration
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    prompt = "durable uncertain-ownership prompt bytes\n"
    publish_review_prompt_before_launch(
        tick.artifacts,
        run_id=run_id,
        attempt_id=attempt_id,
        review_iteration=iteration,
        prompt=prompt,
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
        run_tick_once(tick)
        with tick.store.begin_read() as conn:
            attempt = tick.store.get_attempt_by_id(conn, attempt_id)
            if attempt is not None and str(attempt["status"]) == "uncertain":
                uncertain_seen = True
                break
    assert uncertain_seen
    for _ in range(12):
        run_tick_once(tick)
    assert codex_runner_launch_count(backend) == launches_before
    assert codex_resume_invocation_count(codex_log_text(fake_clis)) == 0


def test_c03_two_attempts_same_iteration_later_iteration_and_wrong_run(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,invalid_json,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    attempt_ids = [f"att-{index:032x}" for index in range(1, 32)]
    attempt_iter = iter(attempt_ids)
    per_tick = PerTickClock(
        datetime(2026, 9, 20, 16, 0, 0, tzinfo=UTC),
        step=timedelta(minutes=1),
    )
    launch_tick: dict[str, int] = {}
    complete_tick: dict[str, int] = {}
    completed_codex: set[str] = set()

    def _attempt_id_factory() -> str:
        return next(attempt_iter)

    run_a = submit_sample_run(git_repo, scheduler_paths)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_a, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = make_tick_service(
        git_repo,
        scheduler_paths,
        backend=backend,
        attempt_id_factory=_attempt_id_factory,
        now_factory=per_tick.now,
    )
    from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService

    retry_service = ReviewRetryService(tick.store, tick.artifacts)
    last_kind = "unknown"
    for _ in range(220):
        tick_index_during = per_tick.tick_index
        run_tick_once(tick, per_tick)
        for call in backend.launch_calls:
            if call.attempt_id not in launch_tick and any(
                "codex_attempt_runner" in part for part in call.agent_argv
            ):
                launch_tick[call.attempt_id] = tick_index_during
        for attempt_id, _result in backend._completed.items():
            if attempt_id in completed_codex:
                continue
            launched_request = backend._launched.get(attempt_id)
            if launched_request is None:
                continue
            if any("codex_attempt_runner" in part for part in launched_request.agent_argv):
                completed_codex.add(attempt_id)
                complete_tick.setdefault(attempt_id, tick_index_during)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_a)
            last_kind = state.kind
            if state.kind == "completed":
                break
            if state.kind == "waiting_codex_review_retry":
                retry_service.retry(run_a)
    else:
        raise AssertionError(f"did not reach completed within 220 ticks; last_state={last_kind}")
    run_b = submit_sample_run(
        git_repo,
        scheduler_paths,
        prompt_text="Distinct second-run prompt for wrong-run denial coverage.\n",
    )
    assert run_a != run_b
    codex_ids = codex_runner_attempt_ids(backend)
    assert len(codex_ids) >= 3
    bootstrap_id, iter2_invalid_id, iter2_valid_id = codex_ids[0], codex_ids[1], codex_ids[2]
    expected_ref = expected_reviewer_session_ref(BOOTSTRAP_ID)

    expected_bootstrap_created = per_tick.instant_at_tick(launch_tick[bootstrap_id])
    expected_bootstrap_completed = per_tick.instant_at_tick(complete_tick[bootstrap_id])
    expected_invalid_created = per_tick.instant_at_tick(launch_tick[iter2_invalid_id])
    expected_invalid_completed = per_tick.instant_at_tick(complete_tick[iter2_invalid_id])
    expected_valid_created = per_tick.instant_at_tick(launch_tick[iter2_valid_id])
    expected_valid_completed = per_tick.instant_at_tick(complete_tick[iter2_valid_id])

    code, payload, _ = invoke_cli(["integration", "run", "reviews", run_a, "--output", "json"])
    assert code == 0
    list_by_attempt = {item["attemptId"]: item for item in payload["data"]["items"]}
    assert list_by_attempt[bootstrap_id]["findingsCount"] > 0
    assert list_by_attempt[iter2_invalid_id]["findingsCount"] is None
    assert list_by_attempt[iter2_valid_id]["findingsCount"] == 0
    for attempt_id in (bootstrap_id, iter2_invalid_id, iter2_valid_id):
        item = list_by_attempt[attempt_id]
        assert item["reviewModel"] == "gpt-5.6-sol"
        assert item["reasoningEffort"] == "high"
        assert item["reviewerSessionRef"] == expected_ref

    _, bootstrap_detail, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            run_a,
            "--attempt",
            bootstrap_id,
            "--output",
            "json",
        ]
    )
    _, invalid_detail, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            run_a,
            "--attempt",
            iter2_invalid_id,
            "--output",
            "json",
        ]
    )
    _, valid_detail, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            run_a,
            "--attempt",
            iter2_valid_id,
            "--output",
            "json",
        ]
    )
    assert bootstrap_detail["data"]["resultState"] == "valid"
    assert bootstrap_detail["data"]["findingsCount"] > 0
    assert bootstrap_detail["data"]["createdAt"] == expected_bootstrap_created
    assert bootstrap_detail["data"]["completedAt"] == expected_bootstrap_completed
    assert invalid_detail["data"]["resultState"] == "invalid"
    assert invalid_detail["data"]["findingsCount"] is None
    assert invalid_detail["data"]["response"] is None
    assert invalid_detail["data"]["createdAt"] == expected_invalid_created
    assert invalid_detail["data"]["completedAt"] == expected_invalid_completed
    assert valid_detail["data"]["resultState"] == "valid"
    assert valid_detail["data"]["findingsCount"] == 0
    assert valid_detail["data"]["createdAt"] == expected_valid_created
    assert valid_detail["data"]["completedAt"] == expected_valid_completed
    assert bootstrap_detail["data"]["createdAt"] != invalid_detail["data"]["createdAt"]
    assert invalid_detail["data"]["createdAt"] != valid_detail["data"]["createdAt"]
    assert bootstrap_detail["data"]["completedAt"] != valid_detail["data"]["completedAt"]
    with tick.store.begin_read() as conn:
        invalid_row = tick.store.get_integration_codex_review_row(conn, run_a, iter2_invalid_id)
        valid_row = tick.store.get_integration_codex_review_row(conn, run_a, iter2_valid_id)
        assert invalid_row is not None and valid_row is not None
        assert int(valid_row["iteration"]) == 2
        assert int(invalid_row["iteration"]) == 2
        assert invalid_row["attempt_id"] != valid_row["attempt_id"]

    code, denied, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            run_b,
            "--attempt",
            bootstrap_id,
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert denied["error"]["code"] == "NOT_FOUND"


def test_c04_full_content_kinds_multichunk_and_fix_prompt_isolation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_LARGE_MARKDOWN", "1")
    monkeypatch.setenv("FAKE_CODEX_LARGE_MARKDOWN_BYTES", str(LARGE_MARKDOWN_PAD_BYTES))
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    base_prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    padded_prompt = base_prompt + "\n" + ("Z" * 20_000)
    run_id = submit_sample_run(git_repo, scheduler_paths, prompt_text=padded_prompt)
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=220)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    expected_beta_markdown = (MARKDOWN_BETA_PREFIX + "x" * LARGE_MARKDOWN_PAD_BYTES).encode("utf-8")
    findings_attempts: list[tuple[str, int]] = []
    with tick.store.begin_read() as conn:
        rows, _ = tick.store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=50)
    for row in rows:
        attempt_id = str(row["attempt_id"])
        iteration = int(row["iteration"])
        code, detail, _ = invoke_cli(
            [
                "integration",
                "run",
                "review",
                run_id,
                "--attempt",
                attempt_id,
                "--output",
                "json",
            ]
        )
        assert code == 0
        if detail["data"]["resultState"] == "valid" and detail["data"]["findingsCount"]:
            findings_attempts.append((attempt_id, iteration))
    assert len(findings_attempts) >= 2
    alpha = None
    beta = None
    for attempt_id, iteration in findings_attempts:
        fix_bytes = fetch_review_content_bytes(
            run_id, attempt_id, "cursor-fix-prompt", chunk_size=2048
        )
        if fix_bytes.decode("utf-8") == FIX_PROMPT_ALPHA:
            alpha = (attempt_id, iteration)
        if fix_bytes.decode("utf-8") == FIX_PROMPT_BETA:
            beta = (attempt_id, iteration)
    assert alpha is not None
    assert beta is not None
    assert alpha[0] != beta[0]
    overwrite_path = run_root / fix_prompt_path(2)
    assert overwrite_path.is_file(), "expected iteration convenience fix-prompt artifact"
    original_convenience = overwrite_path.read_bytes()
    overwrite_path.write_text("OVERWRITTEN convenience artifact\n", encoding="utf-8")
    assert overwrite_path.read_text(encoding="utf-8") != original_convenience.decode("utf-8")
    alpha_bytes = fetch_review_content_bytes(run_id, alpha[0], "cursor-fix-prompt", chunk_size=2048)
    beta_bytes = fetch_review_content_bytes(run_id, beta[0], "cursor-fix-prompt", chunk_size=2048)
    assert alpha_bytes.decode("utf-8") == FIX_PROMPT_ALPHA
    assert beta_bytes.decode("utf-8") == FIX_PROMPT_BETA
    alpha_markdown = fetch_review_content_bytes(
        run_id, alpha[0], "review-markdown", chunk_size=4096
    )
    assert alpha_markdown.decode("utf-8").startswith(MARKDOWN_ALPHA)
    large_attempt = beta[0]
    prompt_chunks, prompt_chunk_count = fetch_review_content_chunks(
        run_id, large_attempt, "prompt", chunk_size=4096
    )
    assert prompt_chunk_count >= 2
    run_root_prompt = (run_root / codex_review_prompt_rel(beta[1], large_attempt)).read_bytes()
    assert b"".join(prompt_chunks) == run_root_prompt
    assert len(run_root_prompt) > 4096
    response_bytes = fetch_review_content_bytes(run_id, large_attempt, "response", chunk_size=4096)
    expected_response = (run_root / codex_review_result_rel(beta[1], large_attempt)).read_bytes()
    assert response_bytes == expected_response
    markdown_bytes = fetch_review_content_bytes(
        run_id, large_attempt, "review-markdown", chunk_size=4096
    )
    assert markdown_bytes == expected_beta_markdown


def test_c05_historical_absence_partial_and_invalid_json_bytes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = install_pre_21_4_historical_review_fixture(scheduler_paths)
    historical_run = str(manifest["run_id"])
    historical_id = str(manifest["attempt_id"])
    historical_iteration = int(manifest["review_iteration"])
    assert manifest["provenance"]["baseline_commit"] == "e4c6b08824b76d3ef7fdf38a6d30f6533b5aed02"
    historical_root = run_artifact_root(scheduler_paths["artifact_root"], historical_run)
    historical_prompt = historical_root / codex_review_prompt_rel(
        historical_iteration, historical_id
    )
    historical_evidence = historical_root / codex_review_prompt_evidence_rel(
        historical_iteration, historical_id
    )
    assert not historical_prompt.is_file()
    assert not historical_evidence.is_file()
    service = default_review_read_service(
        db_path=scheduler_paths["db_path"],
        artifact_root=scheduler_paths["artifact_root"],
    )
    detail = service.inspect_review(historical_run, attempt_id=historical_id)
    assert detail.content.prompt.available is False
    assert detail.content.prompt.reason == "not_recorded"
    assert detail.result_state == "valid"
    code, cli_detail, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            historical_run,
            "--attempt",
            historical_id,
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert cli_detail["data"]["content"]["prompt"]["reason"] == "not_recorded"

    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.delenv("AI_DEV_LOOP_CODEX_REVIEW_PROMPT_FAULT", raising=False)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,invalid_json,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    from ai_dev_loop.scheduler.application.start import start_run

    run_id = submit_sample_run(
        git_repo,
        scheduler_paths,
        prompt_text="Second run for partial and invalid-json coverage.\n",
    )
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=240)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    with tick.store.begin_read() as conn:
        rows, _ = tick.store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=20)

    def _review_cli(attempt_id: str) -> dict[str, object]:
        code, payload, _ = invoke_cli(
            [
                "integration",
                "run",
                "review",
                run_id,
                "--attempt",
                attempt_id,
                "--output",
                "json",
            ]
        )
        assert code == 0
        return payload["data"]

    invalid_id = None
    no_fix_id = None
    for row in rows:
        attempt_id = str(row["attempt_id"])
        data = _review_cli(attempt_id)
        if data["resultState"] == "invalid":
            invalid_id = attempt_id
            assert data["response"] is None
            assert data["findingsCount"] is None
        elif (
            data["resultState"] == "valid"
            and data["findingsCount"] == 0
            and data["content"]["cursorFixPrompt"]["reason"] == "not_applicable"
        ):
            no_fix_id = attempt_id
    assert invalid_id is not None
    assert no_fix_id is not None
    raw = fetch_review_content_bytes(run_id, invalid_id, "response", chunk_size=1024)
    assert raw == b"not-json"

    invalid_row = next(row for row in rows if str(row["attempt_id"]) == invalid_id)
    invalid_iteration = int(invalid_row["iteration"])
    invalid_evidence_path = run_root / codex_review_prompt_evidence_rel(
        invalid_iteration, invalid_id
    )
    assert invalid_evidence_path.is_file()
    invalid_evidence_path.write_text("{not-valid-json", encoding="utf-8")
    corrupt_code, corrupt_payload, _ = invoke_cli(
        [
            "integration",
            "run",
            "review",
            run_id,
            "--attempt",
            invalid_id,
            "--output",
            "json",
        ]
    )
    assert corrupt_code == 5
    assert corrupt_payload["error"]["code"] == "DATA_INTEGRITY"

    partial_source = next(row for row in rows if str(row["attempt_id"]) == no_fix_id)
    partial_id = str(partial_source["attempt_id"])
    partial_iteration = int(partial_source["iteration"])
    (run_root / codex_review_prompt_evidence_rel(partial_iteration, partial_id)).unlink(
        missing_ok=True
    )
    partial_detail = service.inspect_review(run_id, attempt_id=partial_id)
    assert partial_detail.content.prompt.reason == "incomplete_capture"
    code, chunk, _ = invoke_cli(
        [
            "integration",
            "run",
            "review-content",
            run_id,
            "--attempt",
            no_fix_id,
            "--kind",
            "cursor-fix-prompt",
            "--output",
            "json",
        ]
    )
    assert code == 0
    assert chunk["data"]["available"] is False
    assert chunk["data"]["reason"] == "not_applicable"


def test_c06_sentinel_excluded_from_integration_outputs(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = SECRET_SENTINEL
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = submit_sample_run(
        git_repo,
        scheduler_paths,
        prompt_text=f"Initial prompt with {sentinel} embedded.\n",
    )
    from ai_dev_loop.scheduler.application.start import start_run

    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = make_tick_service(git_repo, scheduler_paths)
    run_until(tick, run_id, target_kind="completed", max_ticks=80)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    with tick.store.begin_read() as conn:
        rows, _ = tick.store.list_integration_codex_review_rows(conn, run_id, offset=0, limit=5)
    attempt_id = str(rows[0]["attempt_id"])
    iteration = int(rows[0]["iteration"])
    prompt_path = run_root / codex_review_prompt_rel(iteration, attempt_id)
    evidence_path = run_root / codex_review_prompt_evidence_rel(iteration, attempt_id)
    assert sentinel in prompt_path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(prompt_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(evidence_path.stat().st_mode) == 0o600
    schema = json.loads(schema_path("scheduler-review-prompt-evidence-v1.json").read_text())
    evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    jsonschema.validate(evidence_payload, schema)
    SchedulerReviewPromptEvidenceV1.model_validate(evidence_payload)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**evidence_payload, "prompt_sha256": "UPPER"}, schema)
    for cmd in (
        ["integration", "run", "reviews", run_id, "--output", "json"],
        [
            "integration",
            "run",
            "review",
            run_id,
            "--attempt",
            attempt_id,
            "--output",
            "json",
        ],
        ["integration", "run", "history", run_id, "--output", "json"],
    ):
        code, payload, stderr = invoke_cli(cmd)
        assert code == 0
        blob = json.dumps(payload) + stderr
        assert sentinel not in blob
    status = scheduler_status(run_id, db_path=scheduler_paths["db_path"])
    status_text = render_status_output(status, output="json")
    history = scheduler_history(
        run_id, db_path=scheduler_paths["db_path"], order="newest", limit=20
    )
    history_text = render_scheduler_history_output(history, output="json")
    assert sentinel not in status_text
    assert sentinel not in history_text
    code, missing_payload, stderr = invoke_cli(
        [
            "integration",
            "run",
            "review",
            "missing-run-id",
            "--attempt",
            attempt_id,
            "--output",
            "json",
        ]
    )
    assert code == 3
    assert sentinel not in json.dumps(missing_payload) + stderr
