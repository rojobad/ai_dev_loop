"""Regression tests for Phase 17.5 Codex correction findings."""

from __future__ import annotations

import itertools
import json
import subprocess
import textwrap
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.iterations import (
    build_correction_execution_envelope,
    correction_execution_envelope_path,
    fix_prompt_path,
)
from ai_dev_loop.process import StreamingProcessResult, run_process_streaming
from ai_dev_loop.scheduler.application.codex_argv import (
    build_scheduler_codex_bootstrap_args,
    build_scheduler_codex_resume_args,
    scheduler_codex_review_sandbox,
)
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    load_validated_review_result,
    verify_codex_invocation_evidence,
    verify_pre_execution_codex_guards,
)
from ai_dev_loop.scheduler.application.codex_workflow_service import CodexWorkflowService
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    invocation_evidence_sha256,
    verify_correction_envelope_binding,
    verify_pre_execution_cursor_guards,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    MAX_CODEX_CAPTURE_STDERR_BYTES,
    MAX_CODEX_CAPTURE_STDOUT_BYTES,
    MAX_CODEX_EVENTS_ARTIFACT_BYTES,
    SCHEDULER_CODEX_BINDING_ARTIFACT,
    SCHEDULER_CODEX_REVIEW_SANDBOX,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_KIND,
    invocation_evidence_rel,
)
from ai_dev_loop.scheduler.domain.events import CodexReviewerBoundEvent
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore
from ai_dev_loop.state import sha256_bytes

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
        tick_owner_factory=lambda: f"tick-17-5-corr-{next(_ATTEMPT_COUNTER)}",
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
    store = tick.store
    for _ in range(max_ticks):
        tick.run_once()
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
            if state.kind == "blocked":
                summary = getattr(state, "block_reason_summary", "")
                kind = getattr(state, "block_reason_kind", "")
                raise AssertionError(f"run blocked: {kind}: {summary}")
    raise AssertionError(f"did not reach {target_kind} within {max_ticks} ticks")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_scheduler_codex_argv_enforces_read_only_for_writable_config(tmp_path: Path) -> None:
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    result = tmp_path / "result.json"
    for builder, effect in (
        (build_scheduler_codex_bootstrap_args, "bootstrap"),
        (build_scheduler_codex_resume_args, "resume"),
    ):
        kwargs = {
            "command": "codex",
            "repo_root": str(tmp_path),
            "sandbox": "workspace-write",
            "review_model": "gpt-5.6-sol",
            "review_reasoning_effort": "high",
            "schema_file": schema,
            "result_file": result,
        }
        if effect == "resume":
            kwargs["session_id"] = BOOTSTRAP_ID
        args = builder(**kwargs)
        assert args[args.index("--sandbox") + 1] == SCHEDULER_CODEX_REVIEW_SANDBOX
    assert scheduler_codex_review_sandbox("workspace-write") == SCHEDULER_CODEX_REVIEW_SANDBOX


def test_codex_pre_execution_guard_authenticates_frozen_plan_and_prompt(tmp_path: Path) -> None:
    plan_rel = "plan/plan.md"
    prompt_rel = "prompts/cursor-initial.txt"
    plan_path = tmp_path / plan_rel
    prompt_path = tmp_path / prompt_rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    prompt_path.write_text("initial prompt\n", encoding="utf-8")
    evidence = {
        "run_id": "run-test",
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "codex_sandbox": SCHEDULER_CODEX_REVIEW_SANDBOX,
        "plan_artifact_path": plan_rel,
        "plan_sha256": sha256_bytes(plan_path.read_bytes()),
        "prompt_artifact_path": prompt_rel,
        "prompt_sha256": sha256_bytes(prompt_path.read_bytes()),
    }
    verify_pre_execution_codex_guards(tmp_path, evidence, run_id="run-test")
    prompt_path.write_text("tampered prompt\n", encoding="utf-8")
    with pytest.raises((CodexEvidenceError, CursorEvidenceError), match="hash does not match"):
        verify_pre_execution_codex_guards(tmp_path, evidence, run_id="run-test")


def test_codex_pre_execution_guard_rejects_non_readonly_sandbox(tmp_path: Path) -> None:
    evidence = {
        "run_id": "run-test",
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "codex_sandbox": "workspace-write",
    }
    with pytest.raises(CodexEvidenceError, match="read-only sandbox"):
        verify_pre_execution_codex_guards(tmp_path, evidence, run_id="run-test")


def test_codex_launch_intent_tampering_rejected(tmp_path: Path) -> None:
    attempt_id = "att-" + "b" * 32
    run_id = "run-test"
    dispatch_id = "fx-test"
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "codex_sandbox": SCHEDULER_CODEX_REVIEW_SANDBOX,
        "review_model": "gpt-5.6-sol",
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    verify_codex_invocation_evidence(
        tmp_path,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id=dispatch_id,
        unit_identity=f"unit-{attempt_id}",
        launch_nonce="nonce",
        launch_intent_sha256=payload_sha256(launch_intent),
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    )
    evidence["review_model"] = "other-model"
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CodexEvidenceError, match="launch intent does not match"):
        verify_codex_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        )


def test_load_validated_review_result_schema_error_is_safe(tmp_path: Path) -> None:
    result_rel = "codex/reviews/01.json"
    result_path = tmp_path / result_rel
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        json.dumps(
            {
                "has_actionable_findings": True,
                "findings_count": 1,
                "highest_severity": "high",
                "tests_status": "passed",
                "review_markdown": "report",
                "cursor_fix_prompt": "",
            }
        ),
        encoding="utf-8",
    )
    digest = sha256_bytes(result_path.read_bytes())
    outcome = {"review_result_path": result_rel, "review_result_sha256": digest}
    with pytest.raises(CodexEvidenceError, match="schema validation") as exc_info:
        load_validated_review_result(tmp_path, outcome)
    assert "cursor_fix_prompt" not in str(exc_info.value)


def test_verify_correction_envelope_binding_rejects_replacement(tmp_path: Path) -> None:
    fix_text = "exact fix prompt body"
    fix_rel = fix_prompt_path(1)
    fix_path = tmp_path / fix_rel
    fix_path.parent.mkdir(parents=True, exist_ok=True)
    fix_path.write_text(fix_text, encoding="utf-8")
    fix_sha = sha256_bytes(fix_text.encode("utf-8"))
    envelope_rel = correction_execution_envelope_path(1)
    envelope_text = build_correction_execution_envelope(fix_text)
    envelope_path = tmp_path / envelope_rel
    envelope_path.write_text(envelope_text, encoding="utf-8")
    envelope_sha = sha256_bytes(envelope_text.encode("utf-8"))
    verify_correction_envelope_binding(
        tmp_path,
        envelope_path=envelope_rel,
        envelope_sha256=envelope_sha,
        fix_prompt_path=fix_rel,
        fix_prompt_sha256=fix_sha,
    )
    envelope_path.write_text(build_correction_execution_envelope("tampered"), encoding="utf-8")
    with pytest.raises(CursorEvidenceError, match="hash does not match"):
        verify_correction_envelope_binding(
            tmp_path,
            envelope_path=envelope_rel,
            envelope_sha256=envelope_sha,
            fix_prompt_path=fix_rel,
            fix_prompt_sha256=fix_sha,
        )


def test_verify_pre_execution_cursor_guards_rejects_staged_patch_drift(
    tmp_path: Path, git_repo: Path
) -> None:
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    patch_rel = "git/diffs/01.patch"
    patch_path = tmp_path / patch_rel
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    staged_file = git_repo / "staged.txt"
    staged_file.write_text("original\n", encoding="utf-8")
    _git(git_repo, "add", "staged.txt")
    patch_text = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=git_repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    patch_path.write_text(patch_text, encoding="utf-8")
    patch_sha = sha256_bytes(patch_path.read_bytes())
    fix_rel = fix_prompt_path(1)
    fix_path = tmp_path / fix_rel
    fix_path.parent.mkdir(parents=True, exist_ok=True)
    fix_text = "fix body"
    fix_path.write_text(fix_text, encoding="utf-8")
    envelope_rel = correction_execution_envelope_path(1)
    envelope_text = build_correction_execution_envelope(fix_text)
    (tmp_path / envelope_rel).write_text(envelope_text, encoding="utf-8")
    evidence = {
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "run_id": "run-test",
        "repository_root": str(git_repo.resolve()),
        "repository_git_common_dir": str((git_repo / ".git").resolve()),
        "repository_git_dir": str((git_repo / ".git").resolve()),
        "repository_branch": branch,
        "repository_initial_head": _git_head(git_repo),
        "prompt_path": envelope_rel,
        "prompt_sha256": sha256_bytes(envelope_text.encode("utf-8")),
        "fix_prompt_path": fix_rel,
        "fix_prompt_sha256": sha256_bytes(fix_text.encode("utf-8")),
        "staged_patch_path": patch_rel,
        "staged_patch_sha256": patch_sha,
    }
    verify_pre_execution_cursor_guards(tmp_path, evidence, run_id="run-test")
    staged_file.write_text("drift\n", encoding="utf-8")
    _git(git_repo, "add", "staged.txt")
    with pytest.raises((CursorEvidenceError, Exception)):
        verify_pre_execution_cursor_guards(tmp_path, evidence, run_id="run-test")


def _git_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_bounded_process_capture_terminates_on_stdout_overflow(tmp_path: Path) -> None:
    script = tmp_path / "huge_stdout.py"
    script.write_text(
        textwrap.dedent(
            """
            import sys
            sys.stdout.write("x" * 500000)
            """
        ),
        encoding="utf-8",
    )
    result = run_process_streaming(
        ["python3", str(script)],
        max_stdout_bytes=256 * 1024,
        max_stderr_bytes=MAX_CODEX_CAPTURE_STDERR_BYTES,
    )
    assert result.timed_out is False
    assert result.stdout_truncated is True
    assert result.returncode == 2
    assert result.stdout_captured_bytes <= 256 * 1024


def test_envelope_replacement_after_waiting_for_cursor_fix_blocks(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 15, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix")
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        envelope_rel = state.codex.latest_correction_envelope_path
        assert envelope_rel
    (run_root / envelope_rel).write_text("tampered envelope\n", encoding="utf-8")
    for _ in range(8):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                assert (
                    "envelope" in state.block_reason_kind
                    or "launch_guard" in state.block_reason_kind
                )
                return
    pytest.fail("expected blocked after envelope replacement")


def test_staged_index_drift_prevents_correction_launch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 15, 30, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix")
    launches_before = backend.launch_calls
    (git_repo / "post-review-drift.txt").write_text("drift\n", encoding="utf-8")
    _git(git_repo, "add", "post-review-drift.txt")
    for _ in range(8):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                break
    assert len(backend.launch_calls) == len(launches_before)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind in {"waiting_for_cursor_fix", "blocked"}


def test_usage_limit_during_correction_preserves_chat_and_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success,usage_limit,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(Path(fake_clis["agent_log"]).parent / "corr-usage-limit-counter.txt"),
    )
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 16, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix")
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        chat_id = state.cursor.chat_id
        reviewer_id = state.codex.reviewer_session_id
        fix_path = state.codex.latest_fix_prompt_path
        iteration = state.cursor.iteration
    for _ in range(12):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    assert state.kind == "waiting_usage_limit"
    assert state.codex.reviewer_session_id == reviewer_id
    assert state.codex.latest_fix_prompt_path == fix_path
    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    tick_continue = _tick_service(
        git_repo,
        scheduler_paths,
        now=wait_until + timedelta(seconds=1),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick_continue, run_id, target_kind="completed", max_ticks=40)
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert codex_log.count("'resume'") >= 1
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1
    agent_log = Path(fake_clis["agent_log"]).read_text(encoding="utf-8")
    assert agent_log.count("CREATE_CHAT:") == 1
    with tick_continue.store.begin_read() as conn:
        state, _, _ = tick_continue.store.load_validated_snapshot(conn, run_id)
        assert state.cursor.chat_id == chat_id
        assert state.codex.reviewer_session_id == reviewer_id
        assert state.cursor.iteration >= iteration
        assert state.codex.reviews_completed >= 2


def test_reviewer_binding_artifact_written_before_db_bind_retries_safely(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 16, 30, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    injected_crash = False
    binding_artifact_seen = False
    original_append = tick.store.append_event

    def flaky_append(conn, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal injected_crash, binding_artifact_seen
        event = kwargs.get("event")
        if isinstance(event, CodexReviewerBoundEvent):
            binding_path = (
                run_artifact_root(scheduler_paths["artifact_root"], run_id)
                / SCHEDULER_CODEX_BINDING_ARTIFACT
            )
            if binding_path.is_file():
                binding_artifact_seen = True
            if binding_artifact_seen and not injected_crash:
                injected_crash = True
                raise OSError("simulated crash before database bind")
        return original_append(conn, **kwargs)

    with patch.object(tick.store, "append_event", side_effect=flaky_append):
        for _ in range(40):
            try:
                tick.run_once()
            except OSError as exc:
                if "simulated crash before database bind" not in str(exc):
                    raise
            with tick.store.begin_read() as conn:
                state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
                if state.kind == "completed":
                    break
                if state.kind == "awaiting_codex_review" and state.codex.reviewer_session_id:
                    break

    assert binding_artifact_seen, "expected reviewer binding artifact before simulated crash"
    assert injected_crash, "expected one injected crash before durable reviewer binding"
    binding_path = (
        run_artifact_root(scheduler_paths["artifact_root"], run_id)
        / SCHEDULER_CODEX_BINDING_ARTIFACT
    )
    assert binding_path.is_file()
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1


def test_second_findings_cycle_after_correction_usage_limit_completes(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,findings,no_findings")
    monkeypatch.setenv("FAKE_AGENT_RUN_SEQUENCE", "success,usage_limit,success,success")
    monkeypatch.setenv(
        "FAKE_AGENT_RUN_COUNTER",
        str(Path(fake_clis["agent_log"]).parent / "two-cycle-usage-limit-counter.txt"),
    )
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths, max_reviews=3)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 18, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_for_cursor_fix")
    for _ in range(12):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "waiting_usage_limit":
                break
    assert state.kind == "waiting_usage_limit"
    wait_until = datetime.fromisoformat(state.cursor.wait_until.replace("Z", "+00:00"))
    reviewer_id = state.codex.reviewer_session_id
    tick_continue = _tick_service(
        git_repo,
        scheduler_paths,
        now=wait_until + timedelta(seconds=1),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick_continue, run_id, target_kind="waiting_for_cursor_fix", max_ticks=60)
    with tick_continue.store.begin_read() as conn:
        state, _, _ = tick_continue.store.load_validated_snapshot(conn, run_id)
        assert state.cursor.usage_limit_fingerprint_path is None
        assert state.cursor.continuation_envelope_path is None
        assert state.codex.reviewer_session_id == reviewer_id
    tick_finish = _tick_service(
        git_repo,
        scheduler_paths,
        now=wait_until + timedelta(seconds=2),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick_finish, run_id, target_kind="completed", max_ticks=80)
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1
    assert codex_log.count("'resume'") >= 2


def test_invalid_review_json_blocks_without_sensitive_payload(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,invalid_json")
    monkeypatch.delenv("FAKE_CODEX_REVIEW_MODE", raising=False)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 17, 0, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=60)
    for _ in range(20):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                assert (
                    "invalid" in state.block_reason_kind
                    or "outcome" in state.block_reason_kind
                    or "review" in state.block_reason_kind
                )
                assert "ValidationError" not in state.block_reason_summary
                assert "cursor_fix_prompt" not in state.block_reason_summary
                return
    pytest.fail("expected blocked run after invalid review JSON")


def test_codex_capture_stdout_limit_matches_events_artifact_bound() -> None:
    assert MAX_CODEX_CAPTURE_STDOUT_BYTES == MAX_CODEX_EVENTS_ARTIFACT_BYTES


def test_launch_request_uses_frozen_ninety_minute_cursor_timeout(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 18, 45, tzinfo=UTC),
        backend=backend,
    )
    for _ in range(30):
        tick.run_once()
        cursor_launches = [
            call
            for call in backend.launch_calls
            if any("cursor_attempt_runner" in part for part in call.agent_argv)
        ]
        if cursor_launches:
            assert cursor_launches[0].execution_timeout_seconds == 90 * 60
            return
    pytest.fail("expected a cursor attempt launch")


def test_launch_request_uses_frozen_ninety_minute_codex_timeout(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 19, 0, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=30)
    tick.run_once()
    codex_launches = [
        call
        for call in backend.launch_calls
        if any("codex_attempt_runner" in part for part in call.agent_argv)
    ]
    assert codex_launches, "expected a codex attempt launch"
    assert codex_launches[0].execution_timeout_seconds == 90 * 60


def test_large_jsonl_truncation_still_completes_valid_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "large_jsonl")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 19, 30, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="completed", max_ticks=40)
    run_root = run_artifact_root(scheduler_paths["artifact_root"], run_id)
    events_files = list((run_root / "codex" / "events").glob("01.*.jsonl"))
    assert events_files, "expected codex events artifact"
    assert events_files[0].stat().st_size <= MAX_CODEX_EVENTS_ARTIFACT_BYTES
    metadata_path = run_root / "codex" / "reviews" / "01.metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata.get("stdout_truncated") is True
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    bootstrap_count = sum(
        1 for line in codex_log.splitlines() if line.startswith("ARGS:") and "'resume'" not in line
    )
    assert bootstrap_count == 1
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == BOOTSTRAP_ID


def test_large_jsonl_without_valid_result_blocks_with_truncated_reason(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "large_jsonl_no_result")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    backend = FakeAgentProcessBackend(
        default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
    )
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 20, 0, tzinfo=UTC),
        backend=backend,
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=30)
    for _ in range(20):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                assert state.block_reason_kind == "codex_review_output_truncated"
                assert "truncated" in state.block_reason_summary.lower()
                assert BOOTSTRAP_ID not in state.block_reason_summary
                capacity = tick.store.get_capacity_row(conn)
                assert capacity["holder_run_id"] is None
                return
    pytest.fail("expected blocked run after truncated codex review without valid result")


def _awaiting_codex_review_workflow(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_clis: dict[str, Path],
) -> tuple[CodexWorkflowService, str, dict[str, object], str]:
    _ = fake_clis
    monkeypatch.setenv("FAKE_AGENT_MODIFY_MODE", "tracked")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, CONTROLLER_SESSION, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=datetime(2026, 9, 9, 20, 30, tzinfo=UTC),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="awaiting_codex_review", max_ticks=30)
    assert tick._codex_workflow is not None
    now = datetime(2026, 9, 9, 20, 31, tzinfo=UTC)
    with tick.store.begin_read() as conn:
        effects = tick.store.list_eligible_effects(conn, run_id=run_id, now=now)
        codex_effects = [
            row for row in effects if str(row["effect_kind"]) == BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
        ]
        assert codex_effects, "expected bootstrap codex review effect"
        dispatch_id = str(codex_effects[0]["dispatch_id"])
    attempt_id = _next_attempt_id()
    attempt: dict[str, object] = {
        "attempt_id": attempt_id,
        "dispatch_id": dispatch_id,
        "stderr_artifact_path": f"attempts/{attempt_id}/stderr.txt",
        "stdout_artifact_path": f"attempts/{attempt_id}/stdout.txt",
        "result_artifact_path": f"attempts/{attempt_id}/result.json",
        "exit_code": 124,
        "completion_envelope_sha256": None,
    }
    return tick._codex_workflow, run_id, attempt, dispatch_id


def test_run_codex_review_uses_frozen_timeout_from_invocation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ai_dev_loop.scheduler import codex_attempt_runner

    plan_rel = "plan/plan.md"
    prompt_rel = "prompts/cursor-initial.txt"
    run_root = tmp_path / "run-artifacts"
    run_root.mkdir()
    plan_path = run_root / plan_rel
    prompt_path = run_root / prompt_rel
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# plan\n", encoding="utf-8")
    prompt_path.write_text("initial prompt\n", encoding="utf-8")
    captured: dict[str, float | None] = {"timeout": None}

    def fake_streaming(*args: object, **kwargs: object) -> StreamingProcessResult:
        captured["timeout"] = kwargs.get("timeout")  # type: ignore[assignment]
        argv = list(args[0]) if args else []
        return StreamingProcessResult(
            args=argv,
            returncode=0,
            stdout='{"type":"thread.started","thread_id":"019def00-0000-0000-0000-0000000000bb"}\n',
            stderr="",
            timed_out=False,
            elapsed_seconds=0.1,
        )

    monkeypatch.setattr(codex_attempt_runner, "run_process_streaming", fake_streaming)
    evidence = {
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "attempt_id": "att-" + "a" * 32,
        "run_id": "run-timeout-test",
        "dispatch_id": "fx-timeout-test",
        "repository_root": str(tmp_path),
        "repository_git_common_dir": str(tmp_path / ".git"),
        "repository_git_dir": str(tmp_path / ".git"),
        "repository_branch": "main",
        "repository_initial_head": "abc123",
        "codex_timeout_minutes": 90,
        "review_iteration": 1,
        "codex_command": "codex",
        "codex_sandbox": SCHEDULER_CODEX_REVIEW_SANDBOX,
        "review_model": "gpt-5.6-sol",
        "review_reasoning_effort": "high",
        "review_skill": "review-staged-cursor-execution",
        "plan_repository_path": "docs/plans/sample-plan.md",
        "plan_artifact_path": plan_rel,
        "plan_sha256": sha256_bytes(plan_path.read_bytes()),
        "prompt_source_repository_path": "docs/plans/prompt.txt",
        "prompt_artifact_path": prompt_rel,
        "prompt_sha256": sha256_bytes(prompt_path.read_bytes()),
        "max_review_iterations": 3,
    }
    codex_attempt_runner._run_codex_review(evidence, run_root, "run-timeout-test")
    assert captured["timeout"] == 90 * 60


def test_ingest_bootstrap_uncertainty_precedes_timeout_classification(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, run_id, attempt, dispatch_id = _awaiting_codex_review_workflow(
        git_repo, scheduler_paths, monkeypatch, fake_clis=fake_clis
    )
    synthetic_outcome = {
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "attempt_id": attempt["attempt_id"],
        "dispatch_id": dispatch_id,
        "review_iteration": 1,
        "timed_out": True,
        "stdout_truncated": True,
        "stderr_truncated": False,
        "bootstrap_uncertainty_reason": "missing_identity",
        "bootstrap_session_id": "",
        "review_result_sha256": "",
    }
    with patch.object(workflow, "_authenticated_outcome", return_value=synthetic_outcome):
        receipt = workflow._ingest_codex_review(run_id, attempt)
    assert receipt.action == "blocked"
    assert receipt.detail == "codex_bootstrap_uncertain"
    with workflow.store.begin_read() as conn:
        state, _, _ = workflow.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "codex_bootstrap_uncertain"
    binding_path = (
        run_artifact_root(scheduler_paths["artifact_root"], run_id)
        / SCHEDULER_CODEX_BINDING_ARTIFACT
    )
    assert not binding_path.is_file()


def test_ingest_timeout_precedes_truncation_when_identity_is_established(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, run_id, attempt, dispatch_id = _awaiting_codex_review_workflow(
        git_repo, scheduler_paths, monkeypatch, fake_clis=fake_clis
    )
    synthetic_outcome = {
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "attempt_id": attempt["attempt_id"],
        "dispatch_id": dispatch_id,
        "review_iteration": 1,
        "timed_out": True,
        "stdout_truncated": True,
        "stderr_truncated": False,
        "bootstrap_uncertainty_reason": "",
        "bootstrap_session_id": BOOTSTRAP_ID,
        "review_result_sha256": "",
    }
    with patch.object(workflow, "_authenticated_outcome", return_value=synthetic_outcome):
        receipt = workflow._ingest_codex_review(run_id, attempt)
    assert receipt.action == "blocked"
    assert receipt.detail == "codex_review_timeout"
    with workflow.store.begin_read() as conn:
        state, _, _ = workflow.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "codex_review_timeout"
        assert state.block_reason_kind != "codex_review_output_truncated"
