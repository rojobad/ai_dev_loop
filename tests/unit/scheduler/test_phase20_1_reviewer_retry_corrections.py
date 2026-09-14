"""Phase 20.1.1 correction tests for integrity, recovery auth, and retry replay."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import CodexCapacityStatus
from ai_dev_loop.scheduler.application.codex_evidence import (
    CodexEvidenceError,
    validate_codex_review_outcome_integrity,
)
from ai_dev_loop.scheduler.application.contracts import SchedulerEngineError
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_recovery import (
    analyze_blocked_review_recovery,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_contract import (
    cursor_attempt_events_rel,
    cursor_attempt_final_rel,
)
from ai_dev_loop.scheduler.domain.state import BlockedState
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


@pytest.fixture(autouse=True)
def _fast_fake_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_CODEX_SLEEP_SECONDS", "0")


@pytest.fixture
def scheduler_paths(isolated_xdg: Path) -> dict[str, Path]:
    state_root = isolated_xdg / "state" / "ai_dev_loop"
    return {
        "db_path": state_root / "engine.sqlite3",
        "artifact_root": state_root / "artifacts",
    }


def _submit(git_repo: Path, scheduler_paths: dict[str, Path]) -> str:
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
        max_review_iterations=3,
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
        tick_owner_factory=lambda: f"tick-20-1-corr-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_COUNTER):032x}",
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 80,
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


def _run_until_blocked(
    tick: TickService,
    run_id: str,
    *,
    max_ticks: int = 80,
) -> BlockedState:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == "blocked":
                assert isinstance(state, BlockedState)
                return state
    raise AssertionError(f"run {run_id} did not reach blocked")


def _legacy_block_instead_of_retry(tick: TickService, monkeypatch: pytest.MonkeyPatch) -> None:
    assert tick._codex_workflow is not None
    workflow = tick._codex_workflow

    def _blocked(
        run_id: str,
        *,
        attempt_id: str,
        review_iteration: int,
        failure_kind: str,
    ):
        return workflow._block_review(
            run_id,
            attempt_id=attempt_id,
            reason_kind=failure_kind,
            summary="legacy blocked review failure",
        )

    monkeypatch.setattr(workflow, "_enter_review_retry_wait", _blocked)


def _blocked_recovery_fixture(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    codex_mode: str = "message_only_usage_limit",
    capacity: str = "available",
) -> tuple[TickService, str, ProtectedArtifactStore, SqliteSchedulerStore]:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", codex_mode)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", capacity)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
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
    _legacy_block_instead_of_retry(tick, monkeypatch)
    _run_until_blocked(tick, run_id)
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return tick, run_id, artifacts, store


def _legacy_cursor_outcome_fixture(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    run_id: str,
    *,
    iteration: int = 1,
) -> None:
    with store.begin_read() as conn:
        attempt = store.get_cursor_turn_attempt_for_iteration(
            conn,
            run_id=run_id,
            iteration=iteration,
        )
    assert attempt is not None
    attempt_id = str(attempt["attempt_id"])
    unit_identity = str(attempt["unit_identity"])
    run_root = artifacts.run_root(run_id)
    stdout_rel = str(attempt["stdout_artifact_path"])
    stderr_rel = str(attempt["stderr_artifact_path"])
    result_rel = str(attempt["result_artifact_path"])
    stdout_path = run_root / stdout_rel
    outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
    outcome.pop("final_response_path", None)
    outcome.pop("final_response_sha256", None)
    stdout_path.write_text(json.dumps(outcome, sort_keys=True) + "\n", encoding="utf-8")

    metadata_rel = str(outcome.get("metadata_path", "")).strip()
    if metadata_rel:
        metadata_path = run_root / metadata_rel
        if metadata_path.is_file():
            metadata_payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata_payload.pop("final_response_path", None)
            metadata_payload.pop("final_response_sha256", None)
            metadata_path.write_text(
                json.dumps(metadata_payload, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    exit_code = int(attempt["exit_code"]) if attempt["exit_code"] is not None else 0
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=exit_code,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    result_path.write_bytes(envelope)
    new_envelope_sha = envelope_sha256(envelope)
    with store.begin_immediate() as conn:
        conn.execute(
            """
            UPDATE scheduler_attempts
            SET completion_envelope_sha256 = ?
            WHERE attempt_id = ?
            """,
            (new_envelope_sha, attempt_id),
        )


def _replace_unbound_cursor_review_inputs(
    store: SqliteSchedulerStore,
    artifacts: ProtectedArtifactStore,
    run_id: str,
    *,
    final_text: str,
    iteration: int = 1,
) -> None:
    _legacy_cursor_outcome_fixture(store, artifacts, run_id, iteration=iteration)
    with store.begin_read() as conn:
        attempt = store.get_cursor_turn_attempt_for_iteration(
            conn,
            run_id=run_id,
            iteration=iteration,
        )
    assert attempt is not None
    attempt_id = str(attempt["attempt_id"])
    run_root = artifacts.run_root(run_id)
    events_path = run_root / cursor_attempt_events_rel(iteration, attempt_id)
    final_path = run_root / cursor_attempt_final_rel(iteration, attempt_id)
    events_path.write_text(
        json.dumps({"type": "result", "result": final_text}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    final_path.write_text(final_text, encoding="utf-8")


class TestIntegrityAuthentication:
    def test_session_mismatch_is_integrity_not_operational(self, tmp_path: Path) -> None:
        outcome = {
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "review_iteration": 1,
            "bootstrap_session_id": "019def00-0000-0000-0000-000000000099",
            "timed_out": True,
        }
        with pytest.raises(CodexEvidenceError, match="identity conflicts"):
            validate_codex_review_outcome_integrity(
                outcome,
                run_root=tmp_path,
                expected_review_iteration=1,
                expected_effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
                bound_session_id=BOOTSTRAP_ID,
            )

    def test_bound_review_result_missing_is_integrity_not_operational(self, tmp_path: Path) -> None:
        result_rel = "codex/reviews/01.att-fail.json"
        outcome = {
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "review_iteration": 1,
            "bootstrap_session_id": BOOTSTRAP_ID,
            "review_result_path": result_rel,
            "review_result_sha256": "0" * 64,
            "timed_out": False,
            "returncode": 1,
        }
        with pytest.raises(CodexEvidenceError, match="artifact missing"):
            validate_codex_review_outcome_integrity(
                outcome,
                run_root=tmp_path,
                expected_review_iteration=1,
                expected_effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
                bound_session_id=BOOTSTRAP_ID,
            )

    def test_integrity_failure_hard_blocks_without_capacity_probe(
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
            now=datetime(2026, 9, 12, 19, 5, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="awaiting_codex_review")
        assert tick._codex_workflow is not None
        probe = MagicMock()
        tick._codex_workflow._capacity_probe = probe
        captured_attempt = None
        original_ingest = tick._codex_workflow._ingest_codex_review

        def _defer_ingest(run_id: str, attempt: object):
            nonlocal captured_attempt
            captured_attempt = attempt
            from ai_dev_loop.scheduler.application.contracts import TickRunReceipt

            return TickRunReceipt(run_id=run_id, action="codex_ingest_deferred")

        monkeypatch.setattr(tick._codex_workflow, "_ingest_codex_review", _defer_ingest)
        for _ in range(20):
            tick.run_once()
            if captured_attempt is not None:
                break
        assert captured_attempt is not None
        attempt = captured_attempt

        original = tick._codex_workflow._authenticated_outcome

        def _tampered_outcome(run_id: str, attempt_row: object) -> dict[str, object]:
            outcome = original(run_id, attempt_row)
            outcome = dict(outcome)
            outcome["review_iteration"] = 99
            outcome["timed_out"] = True
            return outcome

        monkeypatch.setattr(tick._codex_workflow, "_authenticated_outcome", _tampered_outcome)
        receipt = original_ingest(run_id, attempt)
        assert receipt.action == "blocked"
        probe.probe.assert_not_called()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "blocked"
            assert state.block_reason_kind == "outcome_evidence_invalid"

    def test_operational_failure_still_probes_capacity(
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
            now=datetime(2026, 9, 12, 19, 10, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="awaiting_codex_review")
        assert tick._codex_workflow is not None
        probe = MagicMock()
        probe.probe.return_value = MagicMock(status=CodexCapacityStatus.AVAILABLE)
        tick._codex_workflow._capacity_probe = probe
        for _ in range(20):
            tick.run_once()
            with tick.store.begin_read() as conn:
                state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
                if state.kind == "waiting_codex_review_retry":
                    break
        probe.probe.assert_called()
        assert state.kind == "waiting_codex_review_retry"


class TestRecoveryArtifactAuthentication:
    def test_tampered_binding_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        binding_path = artifacts.run_root(run_id) / "codex/fresh-reviewer-binding.json"
        binding_path.write_text('{"tampered": true}\n', encoding="utf-8")
        service = ReviewRetryService(store, artifacts)
        with pytest.raises(SchedulerEngineError, match="hash mismatch"):
            service.retry(run_id)
        with store.begin_read() as conn:
            rows = conn.execute(
                "SELECT successor_run_id FROM scheduler_review_recovery_successors WHERE source_run_id = ?",
                (run_id,),
            ).fetchall()
            assert rows == []

    def test_tampered_effective_config_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, BlockedState)
            config_rel = state.context.effective_config.effective_config_artifact_path
        config_path = artifacts.run_root(run_id) / config_rel
        config_path.write_text("tampered: true\n", encoding="utf-8")
        with pytest.raises(SchedulerEngineError, match="hash mismatch"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_missing_fresh_reviewer_input_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        input_path = artifacts.run_root(run_id) / "codex/fresh-reviewer-input.json"
        input_path.unlink()
        with pytest.raises(SchedulerEngineError, match="missing"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_tampered_fresh_reviewer_input_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        input_path = artifacts.run_root(run_id) / "codex/fresh-reviewer-input.json"
        input_path.write_text('{"tampered": true}\n', encoding="utf-8")
        with pytest.raises(SchedulerEngineError, match="hash mismatch"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_tampered_cursor_final_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        final_path = next(artifacts.run_root(run_id).glob("cursor/iterations/01/*/final.txt"))
        final_path.write_text("tampered final response\n", encoding="utf-8")
        with pytest.raises(SchedulerEngineError, match="hash mismatch"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_oversized_cursor_final_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application import review_recovery as recovery_mod

        monkeypatch.setattr(recovery_mod, "MAX_CURSOR_FINAL_BYTES", 32)
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        final_path = next(artifacts.run_root(run_id).glob("cursor/iterations/01/*/final.txt"))
        final_path.write_bytes(b"x" * 33)
        with pytest.raises(SchedulerEngineError, match="exceeds size bound"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_unsafe_symlink_path_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        run_root = artifacts.run_root(run_id)
        outside = run_root.parent / "outside-final.txt"
        outside.write_text("outside\n", encoding="utf-8")
        final_path = next(run_root.glob("cursor/iterations/01/*/final.txt"))
        final_path.unlink()
        final_path.symlink_to(outside)
        with pytest.raises(SchedulerEngineError, match="unsafe"):
            analyze_blocked_review_recovery(store, artifacts, run_id)


class TestHistoricalRecoveryEligibility:
    def test_digest_mismatch_codex_review_outcome_invalid_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo,
            scheduler_paths,
            fake_clis,
            monkeypatch,
            codex_mode="invalid_json",
        )
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, BlockedState)
            assert state.block_reason_kind == "codex_review_outcome_invalid"
            attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
        assert attempt is not None
        stdout_path = artifacts.run_root(run_id) / str(attempt["stdout_artifact_path"])
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
        result_rel = str(outcome.get("review_result_path", "")).strip()
        assert result_rel
        result_path = artifacts.run_root(run_id) / result_rel
        assert result_path.is_file()
        result_path.write_text('{"tampered": true}\n', encoding="utf-8")
        source_event_count = 0
        with store.begin_read() as conn:
            source_event_count = len(
                store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
            )
        with pytest.raises(SchedulerEngineError, match="integrity evidence"):
            analyze_blocked_review_recovery(store, artifacts, run_id)
        service = ReviewRetryService(store, artifacts)
        with pytest.raises(SchedulerEngineError, match="integrity evidence"):
            service.retry(run_id)
        with store.begin_read() as conn:
            assert (
                store.get_review_recovery_successor(
                    conn,
                    source_run_id=run_id,
                    recovery_key=f"codex_review_outcome_invalid:{attempt['attempt_id']}",
                )
                is None
            )
            events_after = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        assert len(events_after) == source_event_count

    def test_bound_review_result_removal_rejects_recovery_without_mutation(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo,
            scheduler_paths,
            fake_clis,
            monkeypatch,
            codex_mode="invalid_json",
        )
        with store.begin_read() as conn:
            state, _, _ = store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, BlockedState)
            attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
            source_event_count = len(
                store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
            )
        assert attempt is not None
        stdout_path = artifacts.run_root(run_id) / str(attempt["stdout_artifact_path"])
        outcome = json.loads(stdout_path.read_text(encoding="utf-8"))
        result_rel = str(outcome.get("review_result_path", "")).strip()
        assert result_rel
        result_path = artifacts.run_root(run_id) / result_rel
        assert result_path.is_file()
        result_path.unlink()
        with pytest.raises(SchedulerEngineError, match="integrity evidence"):
            analyze_blocked_review_recovery(store, artifacts, run_id)
        service = ReviewRetryService(store, artifacts)
        with pytest.raises(SchedulerEngineError, match="integrity evidence"):
            service.retry(run_id)
        with store.begin_read() as conn:
            assert (
                store.get_review_recovery_successor(
                    conn,
                    source_run_id=run_id,
                    recovery_key=f"codex_review_outcome_invalid:{attempt['attempt_id']}",
                )
                is None
            )
            events_after = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        assert len(events_after) == source_event_count

    def test_pre_phase_cursor_outcome_without_binding_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        _legacy_cursor_outcome_fixture(store, artifacts, run_id)
        with pytest.raises(SchedulerEngineError, match="authenticated final response binding"):
            analyze_blocked_review_recovery(store, artifacts, run_id)

    def test_consistent_unbound_cursor_review_input_replacement_rejects_recovery(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, run_id, artifacts, store = _blocked_recovery_fixture(
            git_repo, scheduler_paths, fake_clis, monkeypatch
        )
        with store.begin_read() as conn:
            attempt = store.get_latest_recorded_codex_attempt(conn, run_id)
            source_event_count = len(
                store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
            )
        assert attempt is not None
        _replace_unbound_cursor_review_inputs(
            store,
            artifacts,
            run_id,
            final_text="replacement final response that would change review inputs",
        )
        with pytest.raises(SchedulerEngineError, match="authenticated final response binding"):
            analyze_blocked_review_recovery(store, artifacts, run_id)
        service = ReviewRetryService(store, artifacts)
        with pytest.raises(SchedulerEngineError, match="authenticated final response binding"):
            service.retry(run_id)
        with store.begin_read() as conn:
            assert (
                store.get_review_recovery_successor(
                    conn,
                    source_run_id=run_id,
                    recovery_key=f"codex_review_outcome_invalid:{attempt['attempt_id']}",
                )
                is None
            )
            events_after = store.list_events_for_run(conn, run_id, limit=500, newest_first=False)
        assert len(events_after) == source_event_count

    def test_bootstrap_resume_blocked_run_recovers_without_cursor(
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
            now=datetime(2026, 9, 12, 18, 20, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        for _ in range(80):
            tick.run_once()
            with tick.store.begin_read() as conn:
                state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
                if state.kind == "waiting_codex_capacity":
                    break
        assert state.kind == "waiting_codex_capacity"
        _legacy_block_instead_of_retry(tick, monkeypatch)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        blocked = _run_until_blocked(tick, run_id)
        assert blocked.block_reason_kind == "codex_review_outcome_invalid"
        with tick.store.begin_read() as conn:
            bootstrap = tick.store.get_codex_bootstrap_attempt(conn, run_id)
            latest = tick.store.get_latest_recorded_codex_attempt(conn, run_id)
        assert bootstrap is not None and latest is not None
        assert str(bootstrap["attempt_id"]) != str(latest["attempt_id"])
        store = SqliteSchedulerStore(scheduler_paths["db_path"])
        artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
        service = ReviewRetryService(store, artifacts)
        recovery = service.retry(run_id)
        assert recovery.recovery_successor is True
        successor_id = recovery.run_id
        replay = service.retry(run_id)
        assert replay.idempotent_replay is True
        assert replay.run_id == successor_id
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
        for _ in range(80):
            tick.run_once()
            with tick.store.begin_read() as conn:
                successor, _, _ = tick.store.load_validated_snapshot(conn, successor_id)
                if successor.kind == "completed":
                    break
        assert successor.kind == "completed"
        assert successor.codex.reviewer_session_id == BOOTSTRAP_ID
        codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
        assert "'resume'" in codex_log


class TestRetryReplaySemantics:
    def test_retry_replay_while_awaiting_codex_review_is_idempotent(
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
            now=datetime(2026, 9, 12, 19, 20, tzinfo=UTC),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
        store = SqliteSchedulerStore(scheduler_paths["db_path"])
        artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
        service = ReviewRetryService(store, artifacts)
        authorized = service.retry(run_id)
        assert authorized.changed is True
        replay = service.retry(run_id)
        assert replay.idempotent_replay is True
        assert replay.state_kind == "awaiting_codex_review"
