"""Phase 27.2 role-bound Codex implementer capacity continuation contracts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    IMPLEMENTER_ID,
    REVIEWER_ID,
    _effect_kinds,
    _run_until,
    _submit,
    _tick,
)

from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.attempt_backend import LaunchRequest, ObserveResult
from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityStatus,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError, TickReceipt
from ai_dev_loop.scheduler.application.cursor_timeout_retry import CursorTimeoutRetryService
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.safe_actions import safe_next_action_for_scheduler_state
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND,
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
)
from ai_dev_loop.scheduler.domain.cursor_contract import NORMALIZE_STAGING_EFFECT_KIND
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {"id": "gpt-5.6-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _state(paths: dict[str, Path], run_id: str) -> object:
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        state, _, _ = store.load_validated_snapshot(conn, run_id)
    return state


def _attempt_count(paths: dict[str, Path], run_id: str, component: str) -> int:
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts WHERE run_id = ? AND component = ?",
            (run_id, component),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _active_reservation(paths: dict[str, Path], run_id: str) -> sqlite3.Row | None:
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        return store.get_reservation_for_run(conn, run_id)


class _StaticCapacityProbe:
    def __init__(self, status: CodexCapacityStatus) -> None:
        self.status = status
        self.calls: list[str] = []

    def probe(self, codex_command: str) -> CodexCapacityObservation:
        self.calls.append(codex_command)
        return CodexCapacityObservation(status=self.status)


class _BlockingCapacityProbe(_StaticCapacityProbe):
    def __init__(self, status: CodexCapacityStatus) -> None:
        super().__init__(status)
        self.entered = threading.Event()
        self.release = threading.Event()

    def probe(self, codex_command: str) -> CodexCapacityObservation:
        self.calls.append(codex_command)
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("capacity probe barrier timed out")
        return CodexCapacityObservation(status=self.status)


def _run_to_blocked(tick: object, run_id: str, *, limit: int = 50) -> object:
    for _ in range(limit):
        tick.run_once()
        state = _state({"db_path": tick.store.db_path}, run_id)
        if state.kind == "blocked":
            return state
    raise AssertionError("run did not block")


def test_initial_quota_preserves_partial_work_and_restart_resumes_exact_identity(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "partial-implementation.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    first_tick = _tick(git_repo, scheduler_paths)

    waiting = _run_until(first_tick, run_id, "waiting_codex_capacity")

    assert waiting.codex.capacity_continuation.role == "implementer"
    assert waiting.codex.capacity_continuation.turn == "initial"
    assert waiting.codex.implementer_session_id == IMPLEMENTER_ID
    continuation = waiting.codex.capacity_continuation
    run_root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
    assert sha256_file(run_root / continuation.wait_intent_path) == continuation.wait_intent_sha256
    assert (
        sha256_file(run_root / continuation.partial_output_path)
        == continuation.partial_output_sha256
    )
    assert (git_repo / "partial-implementation.txt").read_text(encoding="utf-8")
    kinds = _effect_kinds(scheduler_paths, run_id)
    assert NORMALIZE_STAGING_EFFECT_KIND not in kinds
    assert BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND not in kinds
    assert waiting.codex.reviews_completed == 0
    with first_tick.store.begin_read() as conn:
        safe_action = safe_next_action_for_scheduler_state(first_tick.store, conn, waiting)
    assert safe_action.kind == "scheduler_tick"
    assert "review retry" not in safe_action.command
    assert IMPLEMENTER_ID not in safe_action.command

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    restarted_tick = _tick(git_repo, scheduler_paths)
    completed = _run_until(restarted_tick, run_id, "completed")

    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    log_text = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert IMPLEMENTER_ID in log_text
    assert "--last" not in log_text


def test_exhausted_and_unavailable_probe_do_not_launch_or_stage(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    _run_until(tick, run_id, "waiting_codex_capacity")
    attempts = _attempt_count(scheduler_paths, run_id, "codex_implementer")

    tick.run_once()
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    tick.run_once()

    assert _state(scheduler_paths, run_id).kind == "waiting_codex_capacity"
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == attempts
    kinds = _effect_kinds(scheduler_paths, run_id)
    assert NORMALIZE_STAGING_EFFECT_KIND not in kinds
    assert BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND not in kinds


def test_repeated_initial_quota_waits_again_without_review_charge(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "repeated-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths, max_reviews=5)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    first = _run_until(tick, run_id, "waiting_codex_capacity")
    first_version = first.version

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    for _ in range(30):
        tick.run_once()
        current = _state(scheduler_paths, run_id)
        if current.kind == "waiting_codex_capacity" and current.version > first_version:
            break
    else:
        raise AssertionError("run did not re-enter implementer capacity wait")

    assert current.codex.reviews_completed == 0
    assert current.codex.capacity_continuation.role == "implementer"
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)


def test_correction_quota_waits_then_resumes_same_implementer_without_extra_review(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "success,usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "correction-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths, max_reviews=4)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)

    waiting = _run_until(tick, run_id, "waiting_codex_capacity")

    assert waiting.codex.capacity_continuation.turn == "correction"
    assert waiting.codex.reviews_completed == 1
    assert waiting.codex.reviewer_session_id == REVIEWER_ID
    assert _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND) == 1

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    completed = _run_until(tick, run_id, "completed")
    assert completed.codex.reviews_completed == 2
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID


@pytest.mark.parametrize(
    ("turn", "sequence"),
    [
        ("initial", "usage_limit,success"),
        ("correction", "success,usage_limit,success"),
    ],
)
def test_production_prompt_byte_witness_for_initial_correction_and_capacity_resume(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    turn: str,
    sequence: str,
) -> None:
    initial = "Approved task.\r\nMixed LF.\nUnicode: ñ雪.\r\n\r\n"
    correction = "Fix line one.\r\nFix line two.\nUnicode: λ終.\r\n\r\n"
    source_rel = "docs/plans/prompt_sample-plan.txt"
    (git_repo / source_rel).write_bytes(initial.encode("utf-8"))
    subprocess.run(["git", "add", "--", source_rel], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "test fixture: exact mixed newline prompt"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )
    witness_dir = tmp_path / "implementer-byte-witness"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_STDIN_WITNESS_DIR", str(witness_dir))
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", sequence)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "byte-witness-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", correction)
    monkeypatch.setenv(
        "FAKE_CODEX_REVIEW_SEQUENCE",
        "findings,no_findings" if turn == "correction" else "no_findings",
    )
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths, prompt=initial)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    waiting = _run_until(tick, run_id, "waiting_codex_capacity")
    run_root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
    continuation = waiting.codex.capacity_continuation
    pending_bytes = (run_root / continuation.prompt_artifact_path).read_bytes()
    assert continuation.turn == turn
    assert waiting.codex.reviews_completed == (0 if turn == "initial" else 1)

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    restarted = _tick(git_repo, scheduler_paths)
    ready = _run_until(restarted, run_id, "cursor_ready")
    assert ready.codex.inferred_operational_failure_kind is None
    assert ready.codex.capacity_continuation is None
    assert ready.codex.reviews_completed == (0 if turn == "initial" else 1)
    completed = _run_until(restarted, run_id, "completed")
    witnesses = sorted(witness_dir.glob("*.bin"))
    assert len(witnesses) == (2 if turn == "initial" else 3)
    initial_bytes = (run_root / "prompts/cursor-initial.txt").read_bytes()
    assert initial.encode("utf-8") in initial_bytes
    assert witnesses[0].read_bytes() == initial_bytes
    assert witnesses[-2].read_bytes() == pending_bytes
    assert witnesses[-1].read_bytes() == pending_bytes
    assert sha256_file(witnesses[-1]) == continuation.prompt_sha256
    if turn == "correction":
        assert (run_root / "prompts/fixes/01.txt").read_bytes() == correction.encode("utf-8")
        assert pending_bytes.endswith(correction.encode("utf-8"))
    resumed_argv = json.loads(witnesses[-1].with_suffix(".argv.json").read_text(encoding="utf-8"))
    assert "resume" in resumed_argv
    assert resumed_argv[resumed_argv.index("--json") + 1] == IMPLEMENTER_ID
    assert resumed_argv[resumed_argv.index("--model") + 1] == "gpt-5.5"
    assert 'model_reasoning_effort="xhigh"' in resumed_argv
    assert completed.codex.reviewer_session_id == REVIEWER_ID


def test_provider_message_quota_preserves_evidence_classification(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "message_only_usage_limit")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    waiting = _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    assert waiting.codex.capacity_evidence_source == "provider_message_limit"
    assert waiting.codex.capacity_continuation.session_id == IMPLEMENTER_ID


@pytest.mark.parametrize(
    ("turn", "implementer_sequence", "review_sequence", "reviews_before_wait"),
    [
        ("initial", "fail,success", "no_findings", 0),
        ("correction", "success,fail,success", "findings,no_findings", 1),
    ],
)
def test_post_failure_exhausted_probe_waits_and_resumes_exact_pending_turn(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    turn: str,
    implementer_sequence: str,
    review_sequence: str,
    reviews_before_wait: int,
) -> None:
    probe_log = scheduler_paths["db_path"].parent / f"post-failure-{turn}.probe"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", implementer_sequence)
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", review_sequence)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", f"post-failure-{turn}.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_REVIEWER_SESSION_ID", REVIEWER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_PROBE_TOUCH_FILE", str(probe_log))
    run_id = _submit(git_repo, scheduler_paths, max_reviews=4)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)

    waiting = _run_until(tick, run_id, "waiting_codex_capacity")

    assert waiting.codex.capacity_continuation.role == "implementer"
    assert waiting.codex.capacity_continuation.turn == turn
    assert waiting.codex.capacity_continuation.session_id == IMPLEMENTER_ID
    assert waiting.codex.capacity_evidence_source == "post_failure_capacity_probe"
    assert waiting.codex.inferred_operational_failure_kind == "codex_implementer_failure"
    assert waiting.codex.reviews_completed == reviews_before_wait
    with tick.store.begin_read() as conn:
        failed_attempt = tick.store.get_attempt_by_id(
            conn, waiting.codex.capacity_continuation.terminated_attempt_id
        )
    assert failed_attempt is not None
    run_root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
    raw_outcome = json.loads(
        (run_root / failed_attempt["stdout_artifact_path"]).read_text(encoding="utf-8")
    )
    assert raw_outcome["usage_limit"] is False
    assert raw_outcome["limit_evidence_kind"] == "unknown"
    assert raw_outcome["failure_code"] == "unknown"
    assert raw_outcome["wait_intent_sha256"] == sha256_file(
        run_root / raw_outcome["wait_intent_path"]
    )
    assert probe_log.read_text(encoding="utf-8").splitlines() == ["probe"]
    kinds = _effect_kinds(scheduler_paths, run_id)
    assert kinds.count(NORMALIZE_STAGING_EFFECT_KIND) == reviews_before_wait
    assert kinds.count(BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND) == reviews_before_wait
    attempts_before_resume = _attempt_count(scheduler_paths, run_id, "codex_implementer")

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    restarted_tick = _tick(git_repo, scheduler_paths)
    ready = _run_until(restarted_tick, run_id, "cursor_ready")
    assert ready.codex.inferred_operational_failure_kind is None
    assert ready.codex.reviews_completed == reviews_before_wait
    completed = _run_until(restarted_tick, run_id, "completed")

    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert completed.codex.reviewer_session_id == REVIEWER_ID
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == (
        attempts_before_resume + 1
    )
    expected_resumes = 1 if turn == "initial" else 2
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == (
        expected_resumes
    )
    log_text = fake_clis["codex_log"].read_text(encoding="utf-8")
    assert IMPLEMENTER_ID in log_text
    assert "--last" not in log_text


@pytest.mark.parametrize("probe_status", ["available", "unavailable"])
@pytest.mark.parametrize("turn", ["initial", "correction"])
def test_post_failure_probe_without_exhaustion_blocks_without_retry_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    probe_status: str,
    turn: str,
) -> None:
    probe_log = scheduler_paths["db_path"].parent / f"non-quota-{probe_status}.probe"
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", f"non-quota-{turn}.txt")
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_SEQUENCE", "fail" if turn == "initial" else "success,fail"
    )
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", probe_status)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_PROBE_TOUCH_FILE", str(probe_log))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])

    blocked = _run_to_blocked(_tick(git_repo, scheduler_paths), run_id)

    assert blocked.block_reason_kind == "codex_implementer_failure"
    assert probe_log.read_text(encoding="utf-8").splitlines() == ["probe"]
    kinds = _effect_kinds(scheduler_paths, run_id)
    assert kinds.count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == (0 if turn == "initial" else 1)
    assert kinds.count(NORMALIZE_STAGING_EFFECT_KIND) == (0 if turn == "initial" else 1)
    assert kinds.count(BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND) == (0 if turn == "initial" else 1)
    assert _active_reservation(scheduler_paths, run_id) is None
    with pytest.raises(SchedulerEngineError, match="terminal"):
        SchedulerAbortService(
            SqliteSchedulerStore(scheduler_paths["db_path"]), FakeAgentProcessBackend()
        ).abort_run(run_id)


def test_proven_no_session_quota_gets_one_bootstrap_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    waiting = _run_until(tick, run_id, "waiting_codex_capacity")
    assert waiting.codex.capacity_continuation.no_session_bootstrap is True
    assert waiting.codex.implementer_session_id is None

    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "bootstrap-retry.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    completed = _run_until(tick, run_id, "completed")
    assert completed.codex.implementer_no_session_bootstrap_retries == 1
    assert (
        _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND) == 2
    )


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("conflicting_identity", "codex_implementer_identity_uncertain"),
        ("fail", "codex_implementer_failure"),
    ],
)
def test_uncertain_identity_and_generic_failure_block_without_staging(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    reason: str,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", mode)
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    blocked = _run_to_blocked(_tick(git_repo, scheduler_paths), run_id)
    assert blocked.block_reason_kind == reason
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)


@pytest.mark.parametrize("mode", ["conflicting_identity", "usage_limit_without_identity"])
def test_uncertain_bootstrap_with_possible_work_retains_reservation_across_restart(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_MODE",
        "usage_limit" if mode == "usage_limit_without_identity" else mode,
    )
    monkeypatch.setenv(
        "FAKE_CODEX_IMPLEMENTER_SESSION_ID",
        "" if mode == "usage_limit_without_identity" else IMPLEMENTER_ID,
    )
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", f"uncertain-{mode}.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])

    blocked = _run_to_blocked(_tick(git_repo, scheduler_paths), run_id)

    assert blocked.block_reason_kind == "codex_implementer_identity_uncertain"
    assert (git_repo / f"uncertain-{mode}.txt").is_file()
    reservation = _active_reservation(scheduler_paths, run_id)
    assert reservation is not None and str(reservation["run_id"]) == run_id
    restarted_store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with restarted_store.begin_read() as conn:
        restarted = restarted_store.get_reservation_for_run(conn, run_id)
    assert restarted is not None and str(restarted["run_id"]) == run_id
    with pytest.raises(SchedulerEngineError, match="active scheduler reservation"):
        _submit(
            git_repo,
            scheduler_paths,
            resubmission_id="019def00-0000-0000-0000-0000000000cc",
        )
    with restarted_store.begin_read() as conn:
        action = safe_next_action_for_scheduler_state(restarted_store, conn, blocked)
    assert action.kind == "scheduler_abort"
    attempts_before = _attempt_count(scheduler_paths, run_id, "codex_implementer")
    result = SchedulerAbortService(restarted_store, FakeAgentProcessBackend()).abort_run(run_id)
    assert result.abort_persisted
    assert not result.termination_pending
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _active_reservation(scheduler_paths, run_id) is None
    _tick(git_repo, scheduler_paths).run_once()
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == attempts_before
    assert (git_repo / f"uncertain-{mode}.txt").is_file()


def test_abort_during_implementer_wait_cancels_retry_and_preserves_partial_work(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "abort-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    _run_until(tick, run_id, "waiting_codex_capacity")
    before = _attempt_count(scheduler_paths, run_id, "codex_implementer")

    SchedulerAbortService(
        tick.store,
        FakeAgentProcessBackend(),
        now_factory=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=UTC),
    ).abort_run(run_id)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    tick.run_once()

    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == before
    assert (git_repo / "abort-partial.txt").is_file()


def test_second_proven_no_session_quota_blocks_instead_of_looping(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", "")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    _run_until(tick, run_id, "waiting_codex_capacity")

    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    blocked = _run_to_blocked(tick, run_id)

    assert blocked.block_reason_kind == "codex_implementer_bootstrap_retry_exhausted"
    assert (
        _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND) == 2
    )
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("timeout", "codex_implementer_timeout"),
        ("incomplete", "codex_implementer_output_incomplete"),
        ("signal", "codex_implementer_failure"),
    ],
)
def test_unsafe_failure_never_gains_post_failure_probe_authority(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    reason: str,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "timeout-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    probe = _StaticCapacityProbe(CodexCapacityStatus.EXHAUSTED)
    tick = _tick(git_repo, scheduler_paths, capacity_probe=probe)
    workflow = tick._codex_workflow
    assert workflow is not None
    with pytest.MonkeyPatch.context() as local_patch:
        local_patch.setattr(
            workflow,
            "_maybe_ingest_completed_implementer_attempt",
            lambda *_args: None,
        )
        for _ in range(12):
            tick.run_once()
            with tick.store.begin_read() as conn:
                attempt = tick.store.get_latest_completed_codex_implementer_attempt(conn, run_id)
            if attempt is not None:
                break
        else:
            raise AssertionError("implementer attempt did not complete")
    synthetic = workflow._authenticated_implementer_outcome(run_id, attempt)
    synthetic.update(
        {
            "timed_out": case == "timeout",
            "output_incomplete": case == "incomplete",
            "usage_limit": False,
            "returncode": -15 if case == "signal" else 124,
        }
    )
    monkeypatch.setattr(workflow, "_authenticated_implementer_outcome", lambda *_: synthetic)

    receipt = workflow._ingest_codex_implementer(run_id, attempt)

    assert receipt.action == "blocked"
    assert receipt.detail == reason
    assert probe.calls == []
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert RESUME_CODEX_IMPLEMENTER_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert (git_repo / "timeout-partial.txt").is_file()
    if case == "incomplete":
        assert _active_reservation(scheduler_paths, run_id) is not None


def test_uncertain_attempt_ownership_holds_then_converges_without_duplicate_work(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "uncertain-converges.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=10))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend)
    tick.run_once()
    tick.run_once()
    assert len(backend.launch_calls) == 1
    backend.default_scenario.owned = False
    for _ in range(3):
        tick.run_once()

    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        row = conn.execute(
            "SELECT status FROM scheduler_attempts WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        capacity = conn.execute(
            "SELECT holder_run_id FROM scheduler_capacity "
            "WHERE capacity_name = 'global_active_agent'"
        ).fetchone()
    assert row is not None and str(row[0]) == "uncertain"
    assert capacity is not None and str(capacity[0]) == run_id
    assert _state(scheduler_paths, run_id).kind == "preflight_complete"
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)

    backend.default_scenario.owned = True
    backend.default_scenario.active_ticks = 0
    waiting = _run_until(tick, run_id, "waiting_codex_capacity")
    assert waiting.codex.capacity_continuation.session_id == IMPLEMENTER_ID
    assert waiting.codex.reviews_completed == 0
    assert _active_reservation(scheduler_paths, run_id) is not None
    assert len(backend.launch_calls) == 1
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    completed = _run_until(tick, run_id, "completed")

    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert len(backend.launch_calls) == 3  # two implementer turns and independent reviewer
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2
    assert (
        _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    )


def test_competing_ticks_around_available_probe_launch_one_exact_continuation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "competing-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    waiting = _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    prompt_sha = waiting.codex.capacity_continuation.prompt_sha256

    stale_probe = _BlockingCapacityProbe(CodexCapacityStatus.AVAILABLE)
    winner_probe = _StaticCapacityProbe(CodexCapacityStatus.AVAILABLE)
    winner_backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=10))
    stale_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=winner_backend,
        owner="tick-capacity-stale",
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
        capacity_probe=stale_probe,
        lease_ttl_seconds=1,
    )
    winner_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=winner_backend,
        owner="tick-capacity-winner",
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, 2, tzinfo=UTC),
        capacity_probe=winner_probe,
        lease_ttl_seconds=1,
    )
    stale_result: list[TickReceipt] = []
    stale_error: list[BaseException] = []

    def run_stale_tick() -> None:
        try:
            stale_result.append(stale_tick.run_once())
        except BaseException as exc:  # pragma: no cover - surfaced below
            stale_error.append(exc)

    thread = threading.Thread(target=run_stale_tick)
    thread.start()
    assert stale_probe.entered.wait(timeout=10)
    winner_receipt = winner_tick.run_once()
    stale_probe.release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert not stale_error
    assert stale_result
    assert winner_receipt.tick_owner_id == "tick-capacity-winner"
    assert stale_result[0].tick_owner_id == "tick-capacity-stale"
    assert stale_result[0].lease_generation < winner_receipt.lease_generation
    assert all(r.action != "codex_capacity_available" for r in stale_result[0].run_receipts)
    assert len(stale_probe.calls) == 1
    assert len(winner_probe.calls) == 1
    winner_tick.run_once()  # authorization and dispatch are separate tick passes
    assert len(winner_backend.launch_calls) == 1
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    state = _state(scheduler_paths, run_id)
    assert state.kind == "preflight_complete"
    assert state.codex.reviews_completed == 0
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert (git_repo / "competing-partial.txt").is_file()
    request = winner_backend.launch_calls[0]
    invocation = json.loads(
        (request.stdout_path.parent / "invocation-evidence.json").read_text(encoding="utf-8")
    )
    assert invocation["role"] == "implementer"
    assert invocation["session_id"] == IMPLEMENTER_ID
    assert invocation["prompt_sha256"] == prompt_sha
    assert invocation["model"] == "gpt-5.5"
    assert invocation["reasoning_effort"] == "xhigh"
    with winner_tick.store.begin_read() as conn:
        events = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? "
            "AND event_kind = 'codex_capacity_available'",
            (run_id,),
        ).fetchone()
    assert events is not None and int(events[0]) == 1


def test_competing_tick_owners_at_continuation_claim_dispatch_only_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "dispatch-race-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    waiting = _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    available_probe = _StaticCapacityProbe(CodexCapacityStatus.AVAILABLE)
    _tick(git_repo, scheduler_paths, capacity_probe=available_probe).run_once()
    claim_entered = threading.Event()
    release_claim = threading.Event()

    def pause_before_claim() -> None:
        claim_entered.set()
        if not release_claim.wait(timeout=10):
            raise AssertionError("continuation claim barrier timed out")

    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=10))
    stale_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="dispatch-stale",
        before_effect_claim=pause_before_claim,
        lease_ttl_seconds=1,
    )
    winner_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="dispatch-winner",
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, 2, tzinfo=UTC),
        lease_ttl_seconds=1,
    )
    errors: list[BaseException] = []
    receipts: list[TickReceipt] = []

    def stale_dispatch() -> None:
        try:
            receipts.append(stale_tick.run_once())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=stale_dispatch)
    thread.start()
    assert claim_entered.wait(timeout=10)
    winner_receipt = winner_tick.run_once()
    release_claim.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert receipts
    assert receipts[0].lease_generation < winner_receipt.lease_generation
    assert any(r.action == "attempt_stale" for r in receipts[0].run_receipts)
    assert len(backend.launch_calls) == 1
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    request = backend.launch_calls[0]
    invocation = json.loads(
        (request.stdout_path.parent / "invocation-evidence.json").read_text(encoding="utf-8")
    )
    assert invocation["session_id"] == IMPLEMENTER_ID
    assert invocation["prompt_sha256"] == waiting.codex.capacity_continuation.prompt_sha256
    assert invocation["model"] == "gpt-5.5"
    assert invocation["reasoning_effort"] == "xhigh"
    assert _state(scheduler_paths, run_id).codex.reviews_completed == 0
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert (git_repo / "dispatch-race-partial.txt").is_file()


@pytest.mark.parametrize(
    "late_status", [CodexCapacityStatus.EXHAUSTED, CodexCapacityStatus.AVAILABLE]
)
def test_late_post_failure_probe_cannot_reingest_a_consumed_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    late_status: CodexCapacityStatus,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "fail,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "late-failure-partial.txt")
    backend = FakeAgentProcessBackend()
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    initial_tick = _tick(git_repo, scheduler_paths, backend=backend)
    assert initial_tick._codex_workflow is not None
    with pytest.MonkeyPatch.context() as local_patch:
        local_patch.setattr(
            initial_tick._codex_workflow,
            "_maybe_ingest_completed_implementer_attempt",
            lambda *_: None,
        )
        for _ in range(10):
            initial_tick.run_once()
            with initial_tick.store.begin_read() as conn:
                attempt = initial_tick.store.get_latest_completed_codex_implementer_attempt(
                    conn, run_id
                )
            if attempt is not None:
                break
        else:
            raise AssertionError("failed implementer attempt never completed")
    delayed_probe = _BlockingCapacityProbe(late_status)
    current_probe = _StaticCapacityProbe(CodexCapacityStatus.EXHAUSTED)
    stale_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="post-failure-stale",
        capacity_probe=delayed_probe,
        lease_ttl_seconds=1,
    )
    winner_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="post-failure-winner",
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, 2, tzinfo=UTC),
        capacity_probe=current_probe,
        lease_ttl_seconds=1,
    )
    errors: list[BaseException] = []
    receipts: list[TickReceipt] = []

    def delayed_ingestion() -> None:
        try:
            receipts.append(stale_tick.run_once())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=delayed_ingestion)
    thread.start()
    assert delayed_probe.entered.wait(timeout=10)
    winner_tick.run_once()
    assert _state(scheduler_paths, run_id).kind == "waiting_codex_capacity"
    current_probe.status = CodexCapacityStatus.AVAILABLE
    winner_tick.run_once()
    backend.default_scenario.active_ticks = 10
    winner_tick.run_once()
    state_before_late_result = _state(scheduler_paths, run_id)
    delayed_probe.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    state_after = _state(scheduler_paths, run_id)
    assert state_after.kind == "preflight_complete"
    assert state_after.version == state_before_late_result.version
    assert any(r.action == "implementer_result_stale" for r in receipts[0].run_receipts)
    assert len(backend.launch_calls) == 2
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2
    assert state_after.codex.reviews_completed == 0
    assert _active_reservation(scheduler_paths, run_id) is not None
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert (git_repo / "late-failure-partial.txt").is_file()


def test_uncertain_completion_evidence_converges_to_capacity_wait_after_resolution(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "resolved-evidence-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")

    class DamagedEnvelopeBackend(FakeAgentProcessBackend):
        saved_envelope: tuple[Path, bytes] | None = None

        def observe(self, *, unit_identity: str, attempt_id: str) -> ObserveResult:
            result = super().observe(unit_identity=unit_identity, attempt_id=attempt_id)
            if result.result_envelope_path is not None and self.saved_envelope is None:
                path = result.result_envelope_path
                self.saved_envelope = (path, path.read_bytes())
                path.write_bytes(b"incomplete completion evidence")
            return result

    backend = DamagedEnvelopeBackend()
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend)
    for _ in range(10):
        tick.run_once()
        if backend.saved_envelope is not None:
            break
    assert backend.saved_envelope is not None
    assert _state(scheduler_paths, run_id).kind == "preflight_complete"
    assert _active_reservation(scheduler_paths, run_id) is not None
    assert len(backend.launch_calls) == 1
    with tick.store.begin_read() as conn:
        attempt = tick.store.get_nonterminal_attempt_for_run(conn, run_id)
    assert attempt is not None and attempt["status"] == "uncertain"
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    path, valid_envelope = backend.saved_envelope
    path.write_bytes(valid_envelope)
    waiting = _run_until(
        _tick(git_repo, scheduler_paths, backend=backend), run_id, "waiting_codex_capacity"
    )
    assert waiting.codex.implementer_session_id == IMPLEMENTER_ID
    assert waiting.codex.capacity_continuation.terminated_attempt_id == attempt["attempt_id"]
    assert waiting.codex.reviews_completed == 0
    assert len(backend.launch_calls) == 1
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    completed = _run_until(tick, run_id, "completed")
    assert completed.codex.implementer_session_id == IMPLEMENTER_ID
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2


def test_abort_during_available_probe_rejects_late_authorization(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "abort-during-probe.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    attempts_before = _attempt_count(scheduler_paths, run_id, "codex_implementer")
    probe = _BlockingCapacityProbe(CodexCapacityStatus.AVAILABLE)
    backend = FakeAgentProcessBackend()
    probing_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="tick-abort-during-probe",
        capacity_probe=probe,
    )
    errors: list[BaseException] = []

    def run_probe_tick() -> None:
        try:
            probing_tick.run_once()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=run_probe_tick)
    thread.start()
    assert probe.entered.wait(timeout=10)
    SchedulerAbortService(
        probing_tick.store,
        backend,
        now_factory=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=UTC),
    ).abort_run(run_id)
    probe.release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert not errors
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == attempts_before
    assert _state(scheduler_paths, run_id).codex.reviews_completed == 0
    assert RESUME_CODEX_IMPLEMENTER_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert not backend.launch_calls
    assert (git_repo / "abort-during-probe.txt").is_file()


def test_abort_after_probe_before_dispatch_cancels_continuation_effect(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "abort-before-dispatch.txt")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    attempts_before = _attempt_count(scheduler_paths, run_id, "codex_implementer")
    backend = FakeAgentProcessBackend()
    hook_calls = 0
    abort_store = SqliteSchedulerStore(scheduler_paths["db_path"])

    def abort_before_claim() -> None:
        nonlocal hook_calls
        hook_calls += 1
        SchedulerAbortService(
            abort_store,
            backend,
            now_factory=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=UTC),
        ).abort_run(run_id)

    tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="tick-abort-before-dispatch",
        capacity_probe=_StaticCapacityProbe(CodexCapacityStatus.AVAILABLE),
        before_effect_claim=abort_before_claim,
    )
    tick.run_once()
    tick.run_once()  # the first pass publishes the continuation effect

    assert hook_calls == 1
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _state(scheduler_paths, run_id).codex.reviews_completed == 0
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == attempts_before
    assert not backend.launch_calls
    assert _effect_kinds(scheduler_paths, run_id).count(RESUME_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    assert (git_repo / "abort-before-dispatch.txt").is_file()


def test_abort_after_continuation_dispatch_holds_until_termination_and_rejects_late_result(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SEQUENCE", "usage_limit,success")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_EDIT_PATH", "abort-dispatched-partial.txt")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    waiting = _run_until(_tick(git_repo, scheduler_paths), run_id, "waiting_codex_capacity")
    prompt_sha = waiting.codex.capacity_continuation.prompt_sha256
    launched = threading.Event()
    release = threading.Event()

    class PausedLaunchBackend(FakeAgentProcessBackend):
        def launch(self, request: LaunchRequest) -> None:
            super().launch(request)
            launched.set()
            if not release.wait(timeout=10):
                raise AssertionError("continuation launch barrier timed out")

    scenario = FakeAttemptScenario(active_ticks=10, stays_active_after_terminate=True)
    backend = PausedLaunchBackend(default_scenario=scenario)
    tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="tick-abort-after-dispatch",
        capacity_probe=_StaticCapacityProbe(CodexCapacityStatus.AVAILABLE),
    )
    tick.run_once()
    errors: list[BaseException] = []

    def dispatch() -> None:
        try:
            tick.run_once()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=dispatch)
    thread.start()
    assert launched.wait(timeout=10)
    result = SchedulerAbortService(
        tick.store,
        backend,
        now_factory=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=UTC),
    ).abort_run(run_id)
    assert result.termination_pending
    assert _active_reservation(scheduler_paths, run_id) is not None
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    request = backend.launch_calls[0]
    assert backend.terminate_calls == [(request.unit_identity, request.attempt_id)]
    invocation = json.loads(
        (request.stdout_path.parent / "invocation-evidence.json").read_text(encoding="utf-8")
    )
    assert invocation["session_id"] == IMPLEMENTER_ID
    assert invocation["prompt_sha256"] == prompt_sha
    scenario.active_ticks = 0
    scenario.stays_active_after_terminate = False
    tick.run_once()
    tick.run_once()
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _active_reservation(scheduler_paths, run_id) is None
    assert len(backend.launch_calls) == 1
    assert _state(scheduler_paths, run_id).codex.reviews_completed == 0
    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 2
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    assert (git_repo / "abort-dispatched-partial.txt").is_file()
    with tick.store.begin_read() as conn:
        stale = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events WHERE run_id = ? "
            "AND event_kind = 'attempt_result_stale'",
            (run_id,),
        ).fetchone()
    assert stale is not None and int(stale[0]) == 1


def test_replayed_ticks_create_one_implementer_dispatch_and_attempt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    backend = FakeAgentProcessBackend(default_scenario=FakeAttemptScenario(active_ticks=10))
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    first = _tick(git_repo, scheduler_paths, backend=backend)
    second = _tick(git_repo, scheduler_paths, backend=backend)
    first.run_once()
    first.run_once()
    for _ in range(4):
        second.run_once()

    assert _attempt_count(scheduler_paths, run_id, "codex_implementer") == 1
    assert (
        _effect_kinds(scheduler_paths, run_id).count(BOOTSTRAP_CODEX_IMPLEMENTER_EFFECT_KIND) == 1
    )
    assert len(backend.launch_calls) == 1


def test_abort_active_implementer_stops_owned_attempt_without_relaunch(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    scenario = FakeAttemptScenario(active_ticks=10, stays_active_after_terminate=True)
    backend = FakeAgentProcessBackend(default_scenario=scenario)
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend)
    tick.run_once()
    tick.run_once()
    assert len(backend.launch_calls) == 1

    result = SchedulerAbortService(
        tick.store,
        backend,
        now_factory=lambda: datetime(2026, 10, 14, 12, 1, tzinfo=UTC),
    ).abort_run(run_id)
    scenario.active_ticks = 0
    scenario.stays_active_after_terminate = False
    tick.run_once()

    assert result.abort_persisted is True
    assert backend.terminate_calls
    assert len(backend.launch_calls) == 1
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert NORMALIZE_STAGING_EFFECT_KIND not in _effect_kinds(scheduler_paths, run_id)
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        stale_results = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events "
            "WHERE run_id = ? AND event_kind = 'attempt_result_stale'",
            (run_id,),
        ).fetchone()
    assert stale_results is not None and int(stale_results[0]) == 1


def test_reviewer_and_cursor_retry_reject_implementer_wait_without_mutation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_IMPLEMENTER_SESSION_ID", IMPLEMENTER_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths)
    waiting = _run_until(tick, run_id, "waiting_codex_capacity")
    before_version = waiting.version
    before_effects = list(_effect_kinds(scheduler_paths, run_id))
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])

    with pytest.raises(SchedulerEngineError):
        ReviewRetryService(tick.store, artifacts).retry(run_id)
    with pytest.raises(SchedulerEngineError):
        CursorTimeoutRetryService(tick.store, artifacts).retry(run_id)

    after = _state(scheduler_paths, run_id)
    assert after.version == before_version
    assert _effect_kinds(scheduler_paths, run_id) == before_effects
