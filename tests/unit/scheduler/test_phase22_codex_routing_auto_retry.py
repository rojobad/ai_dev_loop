"""Phase 22 Codex workspace routing automatic retry tests."""

from __future__ import annotations

import itertools
import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.response_schema import (
    WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE,
    events_text_indicates_workspace_routing_timeout,
)
from ai_dev_loop.scheduler.application.codex_routing_auto_retry import (
    classify_codex_workspace_routing_timeout_from_outcome,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.codex_routing_policy import (
    FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
    ROUTING_AUTO_RETRY_DELAY_SECONDS,
    ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
)
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
        tick_owner_factory=lambda: f"tick-22-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
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


class TestWorkspaceRoutingClassifier:
    def test_exact_turn_failed_message_is_positive(self) -> None:
        text = json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
            }
        )
        assert events_text_indicates_workspace_routing_timeout(text)

    def test_substring_in_tool_content_is_negative(self) -> None:
        text = json.dumps(
            {
                "type": "message",
                "content": f"quoted {WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE} text",
            }
        )
        assert not events_text_indicates_workspace_routing_timeout(text)

    def test_whitespace_trimmed_message_matches(self) -> None:
        text = json.dumps(
            {
                "type": "turn.failed",
                "error": {"message": f"  {WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE}  "},
            }
        )
        assert events_text_indicates_workspace_routing_timeout(text)

    def test_usage_limit_precedence_over_similar_prose(self, tmp_path: Path) -> None:
        events = tmp_path / "events.jsonl"
        events.write_text(
            "\n".join(
                [
                    json.dumps({"type": "error", "message": "usage_limit_exceeded"}),
                    json.dumps(
                        {
                            "type": "turn.failed",
                            "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        outcome = {
            "failure_code": "codex_usage_limit",
            "events_path": events.name,
            "review_result_sha256": "",
        }
        assert not classify_codex_workspace_routing_timeout_from_outcome(tmp_path, outcome)


class TestAutomaticRoutingRetryTick:
    def test_tick_authorizes_only_after_due_time(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
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
            assert state.codex.review_retry_failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
            assert state.codex.routing_auto_retry_eligible is True
            due = state.codex.routing_auto_retry_due_at
            assert due is not None
            assert state.codex.routing_failure_post_probe_status == "unavailable"

        early = _tick_service(
            git_repo,
            scheduler_paths,
            now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS - 1),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        early.run_once()
        with early.store.begin_read() as conn:
            state, _, _ = early.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_codex_review_retry"

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
            assert state.kind == "awaiting_codex_review"
            assert state.codex.routing_auto_retry_authorizations_used == 1
            assert state.codex.review_retry_scheduled_generation == state.codex.review_retry_generation

        replay_tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        replay_tick.run_once()
        with replay_tick.store.begin_read() as conn:
            state, _, _ = replay_tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "awaiting_codex_review"
            assert state.codex.routing_auto_retry_authorizations_used == 1

    def test_exhausted_policy_fields_when_allowance_spent(self) -> None:
        from ai_dev_loop.scheduler.application.codex_routing_auto_retry import (
            routing_auto_retry_fields_for_new_failure,
        )

        fields = routing_auto_retry_fields_for_new_failure(
            authorizations_used=ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS,
            failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            due_at_text="2026-10-02T12:05:00.000000Z",
        )
        assert fields["routing_auto_retry_eligible"] is False
        assert fields["routing_auto_retry_exhausted"] is True
        assert fields["routing_auto_retry_due_at"] is None


def _routing_failure_cycle(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    *,
    now: datetime,
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TickService, str]:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=now,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
    return tick, run_id


def _authorize_due_routing_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    run_id: str,
    *,
    failure_time: datetime,
    offset_seconds: int,
) -> TickService:
    due_tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=failure_time + timedelta(seconds=offset_seconds),
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    due_tick.run_once()
    return due_tick


class TestRoutingRetryAuthorizationGuards:
    def test_expired_tick_lease_skips_automatic_authorization(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
        tick, run_id = _routing_failure_cycle(
            git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
        )
        assert tick._codex_workflow is not None
        receipt = tick._codex_workflow._maybe_authorize_automatic_routing_retry(
            "stale-owner",
            0,
            run_id,
        )
        assert receipt is None
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_codex_review_retry"

    def test_manual_authorization_before_due_is_allowed(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService

        failure_time = datetime(2026, 10, 2, 12, 0, 0, tzinfo=UTC)
        tick, run_id = _routing_failure_cycle(
            git_repo, scheduler_paths, now=failure_time, fake_clis=fake_clis, monkeypatch=monkeypatch
        )
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingCodexReviewRetryState)
            generation = state.codex.review_retry_generation
        service = ReviewRetryService(
            tick.store,
            ProtectedArtifactStore(scheduler_paths["artifact_root"]),
            now_factory=lambda: failure_time + timedelta(seconds=30),
        )
        from ai_dev_loop.scheduler.application.review_retry_authorization import (
            ReviewRetryAuthorizationRequest,
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
            assert state.codex.routing_auto_retry_authorizations_used == 0


class TestRoutingEvidenceAuthentication:
    def test_truncated_outcome_rejects_routing_classification(self, tmp_path: Path) -> None:
        from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file

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
        outcome = {
            "events_path": events.name,
            "operational_failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            "codex_events_sha256": sha256_file(events),
            "stdout_truncated": True,
        }
        assert not classify_codex_workspace_routing_timeout_from_outcome(tmp_path, outcome)

    def test_tampered_events_digest_blocks_routing_retry(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from tests.unit.scheduler.test_phase17_5_codex_corrections import (
            _awaiting_codex_review_workflow,
        )

        from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND

        workflow, run_id, attempt, dispatch_id = _awaiting_codex_review_workflow(
            git_repo, scheduler_paths, monkeypatch, fake_clis=fake_clis
        )
        events_rel = "codex/events/tamper-test.jsonl"
        run_root = workflow.artifacts.run_root(run_id)
        events_path = run_root / events_rel
        events_path.parent.mkdir(parents=True, exist_ok=True)
        events_path.write_text(
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
                }
            ),
            encoding="utf-8",
        )
        synthetic_outcome = {
            "run_id": run_id,
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "attempt_id": attempt["attempt_id"],
            "dispatch_id": dispatch_id,
            "review_iteration": 1,
            "timed_out": False,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "review_output_truncated": False,
            "bootstrap_uncertainty_reason": "",
            "bootstrap_session_id": BOOTSTRAP_ID,
            "review_result_sha256": "",
            "operational_failure_kind": FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
            "codex_events_sha256": "f" * 64,
            "events_path": events_rel,
        }
        with patch.object(workflow, "_authenticated_outcome", return_value=synthetic_outcome):
            receipt = workflow._ingest_codex_review(run_id, attempt)
        assert receipt.action == "blocked"
        assert receipt.detail == "codex_routing_evidence_invalid"
        with workflow.store.begin_read() as conn:
            state, _, _ = workflow.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "blocked"
            assert state.block_reason_kind == "codex_routing_evidence_invalid"


class TestRoutingCapacityPreservation:
    def test_exhausted_capacity_wait_preserves_routing_metadata(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
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
        _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_codex_capacity"
            assert state.codex.review_retry_failure_kind == FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT
            assert state.codex.routing_auto_retry_eligible is True
            assert state.codex.routing_failure_post_probe_status == "exhausted"
            assert state.codex.routing_auto_retry_due_at is not None


class TestRoutingAutoRetryBound:
    def test_twelve_automatic_authorizations_without_thirteenth(
        self,
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
                git_repo,
                scheduler_paths,
                run_id,
                failure_time=failure_time,
                offset_seconds=offset,
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
            assert isinstance(state, WaitingCodexReviewRetryState)
            assert state.codex.routing_auto_retry_authorizations_used == (
                ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS
            )
        final_offset = ROUTING_AUTO_RETRY_DELAY_SECONDS * (ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS + 1)
        _authorize_due_routing_retry(
            git_repo,
            scheduler_paths,
            run_id,
            failure_time=failure_time,
            offset_seconds=final_offset,
        )
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert isinstance(state, WaitingCodexReviewRetryState)
            assert state.codex.routing_auto_retry_authorizations_used == (
                ROUTING_AUTO_RETRY_MAX_AUTHORIZATIONS
            )


class TestRoutingTypedMetadata:
    def test_event_rejects_bool_authorization_counter(self) -> None:
        from ai_dev_loop.scheduler.domain.events import CodexReviewRetryableFailureEvent

        with pytest.raises(ValueError, match="integer"):
            CodexReviewRetryableFailureEvent(
                run_id="run-1",
                review_iteration=1,
                failure_kind=FAILURE_KIND_CODEX_WORKSPACE_ROUTING_TIMEOUT,
                attempt_id="att-1",
                retry_generation=1,
                routing_auto_retry_authorizations_used=True,  # type: ignore[arg-type]
            )

    def test_extend_resets_routing_allowance_for_next_iteration(self) -> None:
        from tests.unit.scheduler.helpers import CONTROLLER_SESSION, sample_submitted_state

        from ai_dev_loop.scheduler.domain.events import (
            MaxIterationsReachedEvent,
            ReviewBudgetExtendedEvent,
        )
        from ai_dev_loop.scheduler.domain.reducer import (
            apply_max_iterations_reached,
            apply_review_budget_extended,
        )
        from ai_dev_loop.scheduler.domain.state import (
            AdmittedRunCheckpoint,
            AwaitingCodexReviewState,
            CodexWorkflowCheckpoint,
            CursorWorkflowCheckpoint,
        )

        submitted = sample_submitted_state(run_id="run-routing-extend")
        now_text = "2026-10-02T12:00:00.000000Z"
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
                routing_auto_retry_authorizations_used=12,
                routing_auto_retry_exhausted=True,
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
            now_text="2026-10-02T12:00:00.000000Z",
        )
        extended = apply_review_budget_extended(
            maxed,
            ReviewBudgetExtendedEvent(
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
            ),
            now_text="2026-10-02T12:05:00.000000Z",
        )
        assert extended.codex.routing_auto_retry_authorizations_used == 0
        assert extended.codex.routing_auto_retry_exhausted is False


class TestRoutingTerminalEvidence:
    def test_malformed_trailing_line_rejects_routing(self) -> None:
        from ai_dev_loop.response_schema import events_text_verifies_workspace_routing_timeout

        text = (
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
                }
            )
            + "\nnot-json\n"
        )
        assert not events_text_verifies_workspace_routing_timeout(text)

    def test_turn_completed_with_routing_rejects(self) -> None:
        from ai_dev_loop.response_schema import events_text_verifies_workspace_routing_timeout

        text = (
            json.dumps({"type": "turn.completed"})
            + "\n"
            + json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
                }
            )
        )
        assert not events_text_verifies_workspace_routing_timeout(text)

    def test_auth_error_after_routing_rejects(self) -> None:
        from ai_dev_loop.response_schema import events_text_verifies_workspace_routing_timeout

        text = (
            json.dumps(
                {
                    "type": "turn.failed",
                    "error": {"message": WORKSPACE_ROUTING_DISCOVERY_TIMEOUT_MESSAGE},
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "error",
                    "message": json.dumps(
                        {"error": {"code": "authentication_failed", "message": "denied"}},
                        separators=(",", ":"),
                    ),
                }
            )
        )
        assert not events_text_verifies_workspace_routing_timeout(text)


class TestRoutingCapacityRecoveryRegression:
    def test_routing_origin_capacity_available_returns_retry_wait(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
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
        _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_codex_capacity"
            due = state.codex.routing_auto_retry_due_at
            assert due is not None
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "waiting_codex_review_retry"
            assert state.codex.routing_auto_retry_eligible is True
            assert state.codex.routing_auto_retry_due_at == due

    def test_quota_capacity_after_routing_authorization_resumes_awaiting_review(
        self,
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
            scheduled = state.codex.review_retry_scheduled_generation
            generation = state.codex.review_retry_generation
            assert scheduled == generation
            used = state.codex.routing_auto_retry_authorizations_used
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        resume_tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=failure_time + timedelta(seconds=ROUTING_AUTO_RETRY_DELAY_SECONDS + 10),
            backend=FakeAgentProcessBackend(
                default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
            ),
        )
        _run_until(resume_tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
        resume_tick.run_once()
        with resume_tick.store.begin_read() as conn:
            state, _, _ = resume_tick.store.load_validated_snapshot(conn, run_id)
            assert state.kind == "awaiting_codex_review"
            assert state.codex.review_retry_scheduled_generation == scheduled
            assert state.codex.routing_auto_retry_authorizations_used == used


class TestRoutingPublicProjections:
    def test_capacity_wait_exposes_routing_diagnostics(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from ai_dev_loop.scheduler.application.contracts import (
            scheduler_status_projection_from_state,
        )

        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "workspace_routing_timeout")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
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
        _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        projection = scheduler_status_projection_from_state(state)
        assert projection["codex_routing_auto_retry_due_at"] is not None
        assert projection["codex_routing_auto_retry_limit"] is not None
        assert projection["codex_routing_failure_post_probe_status"] == "exhausted"
