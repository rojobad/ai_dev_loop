"""Phase 27.4 authenticated same-run implementer timeout continuation contracts."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import jsonschema
import pytest
from tests.conftest import FIXTURE_REPO
from tests.integration.test_phase27_3_implementer_sequences import (
    leaf_state,
    prepare,
    sequence_state,
)
from tests.unit.scheduler.test_phase27_2_codex_implementer import (
    IMPLEMENTER_ID,
    _submit,
    _tick,
)
from typer.testing import CliRunner

from ai_dev_loop.cli import app
from ai_dev_loop.integration_api.run_projection import build_run_inspect_data
from ai_dev_loop.integration_api.run_service import IntegrationRunReadService
from ai_dev_loop.paths import schema_path
from ai_dev_loop.process import StreamingProcessResult
from ai_dev_loop.scheduler import codex_implementer_attempt_runner
from ai_dev_loop.scheduler.application.abort import SchedulerAbortService
from ai_dev_loop.scheduler.application.attempt_envelope import attempt_result_rel
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexCapacityObservation,
    CodexCapacityStatus,
)
from ai_dev_loop.scheduler.application.contracts import (
    SafeNextActionKind,
    SchedulerEngineError,
    SchedulerEngineErrorKind,
)
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.implementer_timeout_retry import (
    CodexImplementerTimeoutRetryService,
    ImplementerRetryService,
)
from ai_dev_loop.scheduler.application.sequence_start import start_sequence
from ai_dev_loop.scheduler.application.sequence_status import SequenceStatusService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.status import SchedulerStatusService
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_IMPLEMENTER_TIMEOUT_RETRY_DELAY_SECONDS,
    RESUME_CODEX_IMPLEMENTER_EFFECT_KIND,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore


@pytest.fixture
def scheduler_paths(isolated_xdg: Path, fake_clis: dict[str, Path]) -> dict[str, Path]:
    fake_clis["codex_models_file"].write_text(
        json.dumps(
            {
                "models": [
                    {"id": "gpt-5.5", "supported_reasoning_efforts": ["xhigh"]},
                    {
                        "id": "gpt-5.6-sol",
                        "supported_reasoning_efforts": ["high", "xhigh"],
                    },
                    {"id": "gpt-6.1-sol", "supported_reasoning_efforts": ["high"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    root = isolated_xdg / "state" / "ai_dev_loop"
    return {"db_path": root / "engine.sqlite3", "artifact_root": root / "artifacts"}


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current

    def advance(self, *, minutes: int) -> None:
        self.current += timedelta(minutes=minutes)


class _InlineCodexBackend(FakeAgentProcessBackend):
    """Run the production adapter in-process with a deterministic provider transport."""

    def __init__(self, modes: list[str]) -> None:
        super().__init__()
        self.modes = modes
        self.implementer_calls = 0
        self.provider_argv: list[list[str]] = []
        self.prompt_bytes: list[bytes] = []
        self.timeouts: list[float | None] = []

    def _execute_cursor_attempt(self, request):  # type: ignore[no-untyped-def]
        if "ai_dev_loop.scheduler.codex_implementer_attempt_runner" not in request.agent_argv:
            return super()._execute_cursor_attempt(request)
        index = self.implementer_calls
        self.implementer_calls += 1
        mode = self.modes[min(index, len(self.modes) - 1)]

        def fake_process(argv: list[str], **kwargs: object) -> StreamingProcessResult:
            self.provider_argv.append(argv)
            prompt = str(kwargs["stdin_text"])
            self.prompt_bytes.append(prompt.encode("utf-8"))
            timeout = kwargs.get("timeout")
            assert timeout is None or isinstance(timeout, (int, float))
            self.timeouts.append(float(timeout) if timeout is not None else None)
            stdout_path = kwargs["stdout_path"]
            stderr_path = kwargs["stderr_path"]
            assert isinstance(stdout_path, Path)
            assert isinstance(stderr_path, Path)
            events: list[dict[str, object]] = []
            if "resume" not in argv and mode != "timeout_without_identity":
                events.append({"type": "thread.started", "thread_id": IMPLEMENTER_ID})
            if mode == "success":
                workspace = Path(str(kwargs["cwd"]))
                with (workspace / "inline-codex-implementation.txt").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(f"implementation {index + 1}\n")
                events.append(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": "implemented"},
                    }
                )
                returncode, timed_out, stderr = 0, False, ""
            elif mode == "usage_limit":
                events.extend(
                    [
                        {"type": "error", "message": "usage_limit_exceeded"},
                        {
                            "type": "turn.failed",
                            "error": {"message": "usage_limit_exceeded"},
                        },
                    ]
                )
                returncode, timed_out, stderr = 2, False, "usage limit"
            elif mode == "fail":
                workspace = Path(str(kwargs["cwd"]))
                with (workspace / "inline-generic-failure.txt").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(f"generic failure {index + 1}\n")
                returncode, timed_out, stderr = 2, False, "generic failure"
            else:
                workspace = Path(str(kwargs["cwd"]))
                with (workspace / "inline-timeout-partial.txt").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(f"partial timeout {index + 1}\n")
                returncode, timed_out, stderr = -15, True, "timed out"
            stdout_path.write_text(
                "".join(json.dumps(event) + "\n" for event in events),
                encoding="utf-8",
            )
            stderr_path.write_text(stderr, encoding="utf-8")
            return StreamingProcessResult(
                args=argv,
                returncode=returncode,
                stdout="",
                stderr=stderr,
                timed_out=timed_out,
                elapsed_seconds=30.0,
            )

        with patch(
            "ai_dev_loop.scheduler.codex_implementer_attempt_runner.run_process_streaming",
            fake_process,
        ):
            return codex_implementer_attempt_runner.main(request.agent_argv[3:])


class _StaticProbe:
    def __init__(self, status: CodexCapacityStatus) -> None:
        self.status = status

    def probe(self, codex_command: str) -> CodexCapacityObservation:
        assert codex_command
        return CodexCapacityObservation(status=self.status)


def _state(paths: dict[str, Path], run_id: str) -> object:
    store = SqliteSchedulerStore(paths["db_path"])
    with store.begin_read() as conn:
        return store.load_validated_snapshot(conn, run_id)[0]


def _drive_to_timeout(tick, paths: dict[str, Path], run_id: str, generation: int = 1):  # type: ignore[no-untyped-def]
    for _ in range(60):
        tick.run_once()
        state = _state(paths, run_id)
        codex = getattr(state, "codex", None)
        continuation = codex.implementer_timeout_continuation if codex is not None else None
        if continuation is not None and continuation.retry_generation == generation:
            return state
        if state.kind == "blocked":
            raise AssertionError(f"blocked: {state.block_reason_kind}")
    raise AssertionError("timeout continuation was not persisted")


def _drive_to_kind(tick, paths: dict[str, Path], run_id: str, kind: str):  # type: ignore[no-untyped-def]
    for _ in range(80):
        tick.run_once()
        state = _state(paths, run_id)
        if state.kind == kind:
            return state
        if state.kind == "blocked":
            raise AssertionError(f"blocked: {state.block_reason_kind}")
    raise AssertionError(f"run did not reach {kind}")


def _attempt_count(paths: dict[str, Path], run_id: str) -> int:
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_attempts "
            "WHERE run_id=? AND component='codex_implementer'",
            (run_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _timeout_effect_count(paths: dict[str, Path], run_id: str) -> int:
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=? AND effect_kind=?",
            (run_id, RESUME_CODEX_IMPLEMENTER_EFFECT_KIND),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _timeout_event_count(paths: dict[str, Path], run_id: str) -> int:
    with sqlite3.connect(paths["db_path"]) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduler_events "
            "WHERE run_id=? AND event_kind='codex_implementer_timeout_retry'",
            (run_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_real_cli_manual_advance_restart_resumes_exact_session_config_and_prompt(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout"])
    run_id = _submit(git_repo, scheduler_paths, prompt=prompt)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    state = _drive_to_timeout(
        _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock),
        scheduler_paths,
        run_id,
    )
    continuation = state.codex.implementer_timeout_continuation
    assert continuation.session_id == IMPLEMENTER_ID
    assert continuation.automatic_retries == 1
    assert continuation.wait_until == "2026-10-14T12:30:00.000000Z"
    partial_before = (git_repo / "inline-timeout-partial.txt").read_bytes()
    version_before_late_result = state.version
    late = CodexImplementerTimeoutRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).record_timeout(
        run_id,
        continuation.source_attempt_id,
        binding_artifact_path=state.codex.implementer_binding_artifact_path,
        binding_artifact_sha256=state.codex.implementer_binding_artifact_sha256,
    )
    assert late.action == "implementer_result_stale"
    assert _state(scheduler_paths, run_id).version == version_before_late_result
    intent = json.loads(
        (
            ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
            / continuation.intent_path
        ).read_text(encoding="utf-8")
    )
    assert (intent["model"], intent["reasoning_effort"]) == ("gpt-5.5", "xhigh")
    assert intent["session_id"] == IMPLEMENTER_ID
    jsonschema.validate(
        intent,
        json.loads(schema_path("scheduler-codex-implementer-timeout-intent-v1.json").read_text()),
    )
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    with store.begin_read() as conn:
        wire = build_run_inspect_data(
            store,
            conn,
            state,
            submitted_at=state.submitted_at,
            updated_at=state.updated_at,
        ).model_dump(mode="json", by_alias=True)
        event_row = conn.execute(
            "SELECT event_payload FROM scheduler_events WHERE run_id=? "
            "AND event_kind='codex_implementer_timeout_retry' ORDER BY sequence DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    assert wire["implementerRetryWaitUntil"] == continuation.wait_until
    assert wire["implementerTimeoutAutomaticRetries"] == 1
    assert wire["implementerTimeoutRetryLimit"] == 3
    assert event_row is not None
    jsonschema.validate(
        json.loads(event_row[0]),
        json.loads(
            schema_path("scheduler-codex-implementer-timeout-retry-event-v1.json").read_text()
        ),
    )

    import ai_dev_loop.scheduler.application.implementer_timeout_retry as retry_module

    production_service = ImplementerRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    )
    monkeypatch.setattr(
        retry_module,
        "scheduler_implementer_retry",
        lambda requested_run_id, force=False: production_service.retry(
            requested_run_id, force=force
        ),
    )

    result = CliRunner().invoke(app, ["scheduler", "implementer-retry", run_id, "--output", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload == {
        "run_id": run_id,
        "provider": "codex",
        "changed": True,
        "available_at": "2026-10-14T12:00:00.000000Z",
        "automatic_retries": 0,
    }

    restarted_backend = _InlineCodexBackend(["success"])
    completed = _drive_to_kind(
        _tick(
            git_repo,
            scheduler_paths,
            backend=restarted_backend,
            now_factory=clock,
            owner="tick-restarted",
        ),
        scheduler_paths,
        run_id,
        "completed",
    )
    assert completed.codex.implementer_timeout_continuation is None
    assert completed.codex.reviews_completed == 1
    assert _attempt_count(scheduler_paths, run_id) == 2
    assert "resume" in restarted_backend.provider_argv[0]
    assert IMPLEMENTER_ID in restarted_backend.provider_argv[0]
    assert restarted_backend.prompt_bytes == [prompt.encode("utf-8")]
    assert backend.prompt_bytes == restarted_backend.prompt_bytes
    assert "--last" not in restarted_backend.provider_argv[0]
    assert (git_repo / "inline-timeout-partial.txt").read_bytes() == partial_before


def test_three_automatic_retries_are_spaced_and_manual_continues_after_exhaustion(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "timeout", "timeout", "timeout"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    state = _drive_to_timeout(tick, scheduler_paths, run_id)
    waits = [state.codex.implementer_timeout_continuation.wait_until]
    for generation in (2, 3, 4):
        clock.advance(minutes=30)
        state = _drive_to_timeout(tick, scheduler_paths, run_id, generation)
        waits.append(state.codex.implementer_timeout_continuation.wait_until)
    continuation = state.codex.implementer_timeout_continuation
    assert waits == [
        "2026-10-14T12:30:00.000000Z",
        "2026-10-14T13:00:00.000000Z",
        "2026-10-14T13:30:00.000000Z",
        None,
    ]
    assert continuation.automatic_retries == 3
    assert continuation.scheduled_generation is None
    assert _attempt_count(scheduler_paths, run_id) == 4

    service = ImplementerRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    )
    manual = service.retry(run_id)
    replay = service.retry(run_id)
    assert manual.changed is True and manual.automatic_retries == 3
    assert replay.changed is False
    with sqlite3.connect(scheduler_paths["db_path"]) as conn:
        live = conn.execute(
            "SELECT COUNT(*) FROM scheduler_effects WHERE run_id=? "
            "AND effect_kind=? AND status IN ('pending','claimed')",
            (run_id, RESUME_CODEX_IMPLEMENTER_EFFECT_KIND),
        ).fetchone()
    assert live is not None and live[0] == 1


def test_correction_timeout_starts_a_fresh_budget_and_binds_raw_correction(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    correction = "Fix exact correction.\r\nUnicode λ雪.\n"
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "findings,no_findings")
    monkeypatch.setenv("FAKE_CODEX_FIX_PROMPT", correction)
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success", "timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    initial = _drive_to_timeout(
        tick,
        scheduler_paths,
        run_id,
    )
    assert initial.codex.implementer_timeout_continuation.turn == "initial"
    assert initial.codex.implementer_timeout_continuation.automatic_retries == 1
    ImplementerRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).retry(run_id)
    for _ in range(80):
        tick.run_once()
        state = _state(scheduler_paths, run_id)
        continuation = state.codex.implementer_timeout_continuation
        if continuation is not None and continuation.turn == "correction":
            break
        assert state.kind != "blocked"
    else:
        raise AssertionError("correction timeout was not persisted")
    continuation = state.codex.implementer_timeout_continuation
    assert continuation.turn == "correction"
    assert continuation.iteration == 2
    assert continuation.automatic_retries == 1
    assert continuation.prompt_artifact_path.endswith("01.execution-envelope.txt")
    assert backend.prompt_bytes[2].endswith(correction.encode("utf-8"))
    assert state.codex.reviews_completed == 1
    frozen_implementer = state.context.implementer

    retry = ImplementerRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).retry(run_id)
    assert retry.changed is True
    for _ in range(80):
        tick.run_once()
        state = _state(scheduler_paths, run_id)
        if state.kind == "awaiting_codex_review" and backend.implementer_calls == 4:
            break
        assert state.kind != "blocked"
    else:
        raise AssertionError("retried correction did not reach review boundary")

    assert state.codex.reviews_completed == 1
    assert state.codex.implementer_timeout_continuation is None
    assert state.context.implementer == frozen_implementer
    assert backend.prompt_bytes[2] == backend.prompt_bytes[3]
    assert backend.prompt_bytes[3].endswith(correction.encode("utf-8"))
    assert frozen_implementer is not None
    assert backend.timeouts[2:] == [
        float(frozen_implementer.timeout_minutes * 60),
        float(frozen_implementer.timeout_minutes * 60),
    ]
    for argv in backend.provider_argv[2:]:
        assert "resume" in argv
        assert IMPLEMENTER_ID in argv
        assert argv[argv.index("--model") + 1] == "gpt-5.5"
        assert 'model_reasoning_effort="xhigh"' in argv
        assert "--last" not in argv

    completed = _drive_to_kind(tick, scheduler_paths, run_id, "completed")
    assert completed.codex.reviews_completed == 2
    assert completed.context.implementer == frozen_implementer


def test_timeout_capacity_roundtrip_preserves_nonzero_budget_and_next_spacing(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    prompt = (FIXTURE_REPO / "docs/plans/prompt_sample-plan.txt").read_text(encoding="utf-8")
    expected_prompt_bytes = prompt.encode("utf-8")
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "usage_limit", "timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths, prompt=prompt)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        now_factory=clock,
        capacity_probe=_StaticProbe(CodexCapacityStatus.AVAILABLE),
    )
    timed = _drive_to_timeout(tick, scheduler_paths, run_id)
    first = timed.codex.implementer_timeout_continuation
    frozen_implementer = timed.context.implementer
    first_partial = (git_repo / "inline-timeout-partial.txt").read_bytes()
    assert first.automatic_retries == 1
    assert first.wait_until == "2026-10-14T12:30:00.000000Z"
    assert first.turn == "initial"
    assert first.iteration == 1
    assert first.session_id == IMPLEMENTER_ID
    assert timed.codex.reviews_completed == 0
    assert first_partial == b"partial timeout 1\n"
    assert backend.prompt_bytes == [expected_prompt_bytes]

    clock.advance(minutes=30)
    waiting = _drive_to_kind(tick, scheduler_paths, run_id, "waiting_codex_capacity")
    waiting_timeout = waiting.codex.implementer_timeout_continuation
    capacity = waiting.codex.capacity_continuation
    assert waiting_timeout.automatic_retries == 1
    assert waiting_timeout.turn == first.turn
    assert waiting_timeout.iteration == first.iteration
    assert waiting_timeout.session_id == first.session_id
    assert waiting_timeout.prompt_artifact_path == first.prompt_artifact_path
    assert waiting_timeout.prompt_sha256 == first.prompt_sha256
    assert capacity.role == "implementer"
    assert capacity.turn == first.turn
    assert capacity.session_id == first.session_id
    assert capacity.prompt_artifact_path == first.prompt_artifact_path
    assert capacity.prompt_sha256 == first.prompt_sha256
    assert waiting.context.implementer == frozen_implementer
    assert waiting.codex.reviews_completed == 0
    assert (git_repo / "inline-timeout-partial.txt").read_bytes() == first_partial
    assert backend.prompt_bytes == [expected_prompt_bytes] * 2

    after_capacity_timeout = _drive_to_timeout(tick, scheduler_paths, run_id, generation=2)
    second = after_capacity_timeout.codex.implementer_timeout_continuation
    assert second.automatic_retries == 2
    assert second.wait_until == "2026-10-14T13:00:00.000000Z"
    assert second.turn == first.turn
    assert second.iteration == first.iteration
    assert second.session_id == first.session_id
    assert second.prompt_artifact_path == first.prompt_artifact_path
    assert second.prompt_sha256 == first.prompt_sha256
    assert after_capacity_timeout.context.implementer == frozen_implementer
    assert after_capacity_timeout.codex.reviews_completed == 0
    second_partial = (git_repo / "inline-timeout-partial.txt").read_bytes()
    assert second_partial == first_partial + b"partial timeout 3\n"
    assert backend.prompt_bytes == [expected_prompt_bytes] * 3

    clock.advance(minutes=30)
    ready_for_review = _drive_to_kind(
        tick,
        scheduler_paths,
        run_id,
        "awaiting_codex_review",
    )
    assert backend.implementer_calls == 4
    assert ready_for_review.codex.reviews_completed == 0
    assert ready_for_review.codex.implementer_timeout_continuation is None
    assert ready_for_review.context.implementer == frozen_implementer
    assert (git_repo / "inline-timeout-partial.txt").read_bytes() == second_partial
    assert backend.prompt_bytes == [expected_prompt_bytes] * 4
    assert frozen_implementer is not None
    assert backend.timeouts == [float(frozen_implementer.timeout_minutes * 60)] * 4
    for argv in backend.provider_argv[1:]:
        assert "resume" in argv
        assert IMPLEMENTER_ID in argv
        assert argv[argv.index("--model") + 1] == frozen_implementer.model
        assert f'model_reasoning_effort="{frozen_implementer.reasoning_effort}"' in argv
        assert "--last" not in argv

    completed = _drive_to_kind(tick, scheduler_paths, run_id, "completed")
    assert completed.codex.implementer_timeout_continuation is None
    assert completed.codex.reviews_completed == 1
    assert completed.context.implementer == frozen_implementer


def test_timeout_after_capacity_continuation_keeps_same_budget_and_spacing(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["usage_limit", "timeout", "timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        now_factory=clock,
        capacity_probe=_StaticProbe(CodexCapacityStatus.AVAILABLE),
    )
    waiting = _drive_to_kind(tick, scheduler_paths, run_id, "waiting_codex_capacity")
    assert waiting.codex.capacity_continuation.role == "implementer"
    assert waiting.codex.implementer_timeout_continuation is None

    first_timeout = _drive_to_timeout(tick, scheduler_paths, run_id)
    first = first_timeout.codex.implementer_timeout_continuation
    assert first.automatic_retries == 1
    assert first.wait_until == "2026-10-14T12:30:00.000000Z"
    assert first_timeout.codex.capacity_continuation is None

    clock.advance(minutes=30)
    second_timeout = _drive_to_timeout(tick, scheduler_paths, run_id, generation=2)
    second = second_timeout.codex.implementer_timeout_continuation
    assert second.automatic_retries == 2
    assert second.wait_until == "2026-10-14T13:00:00.000000Z"
    assert second.turn == first.turn == "initial"
    assert second.iteration == first.iteration == 1

    manual = ImplementerRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    ).retry(run_id)
    assert manual.changed is True
    assert manual.automatic_retries == 1
    completed = _drive_to_kind(tick, scheduler_paths, run_id, "completed")
    assert completed.codex.reviews_completed == 1
    assert backend.prompt_bytes[0] == backend.prompt_bytes[1]
    assert backend.prompt_bytes[1] == backend.prompt_bytes[2] == backend.prompt_bytes[3]
    frozen = completed.context.implementer
    assert frozen is not None
    assert all(timeout == float(frozen.timeout_minutes * 60) for timeout in backend.timeouts)


def test_abort_cancels_wait_and_future_tick_cannot_revive_turn(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    _drive_to_timeout(tick, scheduler_paths, run_id)
    result = SchedulerAbortService(tick.store, backend, now_factory=clock).abort_run(run_id)
    assert result.state_kind == "aborted"
    clock.advance(minutes=60)
    for _ in range(5):
        tick.run_once()
    aborted = _state(scheduler_paths, run_id)
    assert aborted.kind == "aborted"
    assert _attempt_count(scheduler_paths, run_id) == 1
    status = SchedulerStatusService(tick.store).get_status(run_id).summary
    assert status.safe_next_action.kind is SafeNextActionKind.NONE
    assert status.implementer_retry_wait_until is None
    assert status.implementer_timeout_automatic_retries is None
    inspect = IntegrationRunReadService(
        tick.store,
        artifact_root=scheduler_paths["artifact_root"],
    ).inspect_run(run_id)
    assert inspect.state == "aborted"
    assert inspect.safe_next_action.kind == SafeNextActionKind.NONE.value
    assert inspect.implementer_retry_wait_until is None
    assert inspect.implementer_timeout_automatic_retries is None


def test_unresolved_abort_projects_only_pending_termination_actions(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    _drive_to_timeout(tick, scheduler_paths, run_id)
    backend.default_scenario = FakeAttemptScenario(
        active_ticks=10,
        stays_active_after_terminate=True,
    )
    clock.advance(minutes=30)
    tick.run_once()
    assert _attempt_count(scheduler_paths, run_id) == 2

    result = SchedulerAbortService(tick.store, backend, now_factory=clock).abort_run(run_id)
    assert result.state_kind == "aborted"
    assert result.termination_pending is True
    assert result.safe_next_action.kind is SafeNextActionKind.SCHEDULER_ABORT

    status = SchedulerStatusService(tick.store).get_status(run_id).summary
    assert status.safe_next_action.kind is SafeNextActionKind.SCHEDULER_ABORT
    assert status.implementer_retry_wait_until is None
    assert status.implementer_timeout_automatic_retries is None
    inspect = IntegrationRunReadService(
        tick.store,
        artifact_root=scheduler_paths["artifact_root"],
    ).inspect_run(run_id)
    assert inspect.state == "aborted"
    assert inspect.safe_next_action.kind == SafeNextActionKind.SCHEDULER_ABORT.value
    assert inspect.implementer_retry_wait_until is None
    assert inspect.implementer_timeout_automatic_retries is None
    with tick.store.begin_read() as conn:
        assert tick.store.has_unresolved_abort_hold(conn, run_id)
        assert tick.store.get_reservation_for_run(conn, run_id) is not None


def test_corrupt_intent_and_uncertain_bootstrap_identity_never_launch_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    state = _drive_to_timeout(
        _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock),
        scheduler_paths,
        run_id,
    )
    root = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id)
    (root / state.codex.implementer_timeout_continuation.intent_path).write_text(
        "{}\n", encoding="utf-8"
    )
    service = CodexImplementerTimeoutRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    )
    with pytest.raises(SchedulerEngineError, match="intent digest"):
        service.retry(run_id)
    clock.advance(minutes=30)
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    for _ in range(5):
        tick.run_once()
    blocked = _state(scheduler_paths, run_id)
    assert blocked.kind == "blocked"
    assert blocked.block_reason_kind == "codex_implementer_launch_guard_failed"
    assert _attempt_count(scheduler_paths, run_id) == 1

    other_paths = {
        "db_path": scheduler_paths["db_path"].with_name("other.sqlite3"),
        "artifact_root": scheduler_paths["artifact_root"].with_name("other-artifacts"),
    }
    uncertain_backend = _InlineCodexBackend(["timeout_without_identity"])
    uncertain_id = _submit(
        git_repo,
        other_paths,
        resubmission_id="019def00-0000-0000-0000-0000000000cc",
    )
    start_run(uncertain_id, db_path=other_paths["db_path"])
    uncertain_tick = _tick(git_repo, other_paths, backend=uncertain_backend, now_factory=clock)
    uncertain = _drive_to_kind(uncertain_tick, other_paths, uncertain_id, "blocked")
    assert uncertain.block_reason_kind == "codex_implementer_identity_uncertain"
    with uncertain_tick.store.begin_read() as conn:
        assert uncertain_tick.store.get_reservation_for_run(conn, uncertain_id) is not None


@pytest.mark.parametrize("source_mutation", ["corrupt", "missing"])
def test_automatic_dispatch_reauthenticates_timeout_source_evidence(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    source_mutation: str,
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    timed = _drive_to_timeout(tick, scheduler_paths, run_id)
    source_attempt_id = timed.codex.implementer_timeout_continuation.source_attempt_id
    with tick.store.begin_read() as conn:
        source = tick.store.get_attempt_by_id(conn, source_attempt_id)
    assert source is not None
    result_path = ProtectedArtifactStore(scheduler_paths["artifact_root"]).run_root(run_id) / str(
        source["result_artifact_path"]
    )
    if source_mutation == "corrupt":
        result_path.write_bytes(b"{}\n")
    else:
        result_path.unlink()

    clock.advance(minutes=30)
    for _ in range(5):
        tick.run_once()
        blocked = _state(scheduler_paths, run_id)
        if blocked.kind == "blocked":
            break
    else:
        raise AssertionError("invalid timeout source evidence did not block dispatch")
    assert blocked.block_reason_kind == "codex_implementer_launch_guard_failed"
    assert _attempt_count(scheduler_paths, run_id) == 1
    assert len(backend.launch_calls) == 1
    assert backend.implementer_calls == 1
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, run_id) is not None


def test_genuine_timeout_evidence_rejection_blocks_and_consumes_source(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    workflow = tick._codex_workflow
    assert workflow is not None
    original_persist = workflow._persist_implementer_binding

    def persist_then_corrupt(*args: object, **kwargs: object) -> tuple[str, str]:
        result = original_persist(*args, **kwargs)  # type: ignore[arg-type]
        attempt_id = str(kwargs["attempt_id"])
        (tick.artifacts.run_root(run_id) / attempt_result_rel(attempt_id)).write_bytes(b"{}\n")
        return result

    monkeypatch.setattr(workflow, "_persist_implementer_binding", persist_then_corrupt)
    blocked = _drive_to_kind(tick, scheduler_paths, run_id, "blocked")
    assert blocked.block_reason_kind == "codex_implementer_timeout_evidence_invalid"
    assert _attempt_count(scheduler_paths, run_id) == 1
    with tick.store.begin_read() as conn:
        attempts = conn.execute(
            "SELECT attempt_id, ingested FROM scheduler_attempts "
            "WHERE run_id=? AND component='codex_implementer'",
            (run_id,),
        ).fetchall()
        assert len(attempts) == 1 and int(attempts[0]["ingested"]) == 1
        assert tick.store.get_reservation_for_run(conn, run_id) is not None


@pytest.mark.parametrize("failure_point", ["publication", "ledger"])
def test_timeout_checkpoint_transient_failure_retries_and_converges_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    failed = False

    if failure_point == "publication":
        original_publish = tick.artifacts.publish_or_verify_bytes

        def fail_publication_once(
            requested_run_id: str,
            relative_path: str,
            content: bytes,
            *,
            max_bytes: int,
        ):  # type: ignore[no-untyped-def]
            nonlocal failed
            if "/timeouts/" in relative_path and not failed:
                original_publish(
                    requested_run_id,
                    relative_path,
                    content,
                    max_bytes=max_bytes,
                )
                failed = True
                raise OSError("injected uncertain post-publication failure")
            return original_publish(
                requested_run_id,
                relative_path,
                content,
                max_bytes=max_bytes,
            )

        monkeypatch.setattr(tick.artifacts, "publish_or_verify_bytes", fail_publication_once)
        expected_error: type[BaseException] = OSError
    else:
        original_cas = tick.store.compare_and_swap_state

        def fail_timeout_cas_once(
            conn: sqlite3.Connection,
            *,
            run_id: str,
            expected_version: int,
            new_state: object,
            now: datetime,
        ) -> bool:
            nonlocal failed
            codex = getattr(new_state, "codex", None)
            continuation = (
                getattr(codex, "implementer_timeout_continuation", None)
                if codex is not None
                else None
            )
            if continuation is not None and not failed:
                intent = tick.artifacts.run_root(run_id) / continuation.intent_path
                assert intent.is_file()
                failed = True
                raise SchedulerEngineError(
                    SchedulerEngineErrorKind.BUSY,
                    "injected timeout checkpoint busy",
                )
            return original_cas(
                conn,
                run_id=run_id,
                expected_version=expected_version,
                new_state=new_state,  # type: ignore[arg-type]
                now=now,
            )

        monkeypatch.setattr(tick.store, "compare_and_swap_state", fail_timeout_cas_once)
        expected_error = SchedulerEngineError

    with pytest.raises(expected_error):
        for _ in range(20):
            tick.run_once()
    assert failed
    interrupted = _state(scheduler_paths, run_id)
    assert interrupted.kind == "preflight_complete"
    assert interrupted.codex is None or interrupted.codex.implementer_timeout_continuation is None
    assert _attempt_count(scheduler_paths, run_id) == 1
    assert _timeout_effect_count(scheduler_paths, run_id) == 0
    assert _timeout_event_count(scheduler_paths, run_id) == 0
    with tick.store.begin_read() as conn:
        attempt = tick.store.get_latest_completed_codex_implementer_attempt(conn, run_id)
        assert attempt is not None and int(attempt["ingested"]) == 0
        source_attempt_id = str(attempt["attempt_id"])
        assert tick.store.get_reservation_for_run(conn, run_id) is not None
    intent_paths_before = list(
        tick.artifacts.run_root(run_id).glob("implementer/timeouts/*.intent.json")
    )
    assert len(intent_paths_before) == 1
    published_intent = intent_paths_before[0].read_bytes()

    if failure_point == "publication":
        monkeypatch.setattr(tick.artifacts, "publish_or_verify_bytes", original_publish)
    else:
        monkeypatch.setattr(tick.store, "compare_and_swap_state", original_cas)
    restarted = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner=f"tick-restart-{failure_point}",
        now_factory=clock,
    )
    converged = _drive_to_timeout(restarted, scheduler_paths, run_id)
    assert converged.codex.implementer_timeout_continuation.automatic_retries == 1
    assert _attempt_count(scheduler_paths, run_id) == 1
    assert _timeout_effect_count(scheduler_paths, run_id) == 1
    assert _timeout_event_count(scheduler_paths, run_id) == 1
    intent_paths_after = list(
        restarted.artifacts.run_root(run_id).glob("implementer/timeouts/*.intent.json")
    )
    assert len(intent_paths_after) == 1
    assert intent_paths_after[0].read_bytes() == published_intent
    with restarted.store.begin_read() as conn:
        attempt = restarted.store.get_attempt_by_id(conn, source_attempt_id)
        assert attempt is not None and int(attempt["ingested"]) == 1
        assert restarted.store.get_reservation_for_run(conn, run_id) is not None


def test_published_timeout_intent_does_not_revive_aborted_state(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    original_cas = tick.store.compare_and_swap_state
    failed = False

    def fail_timeout_cas_once(
        conn: sqlite3.Connection,
        *,
        run_id: str,
        expected_version: int,
        new_state: object,
        now: datetime,
    ) -> bool:
        nonlocal failed
        codex = getattr(new_state, "codex", None)
        continuation = (
            getattr(codex, "implementer_timeout_continuation", None) if codex is not None else None
        )
        if continuation is not None and not failed:
            assert (tick.artifacts.run_root(run_id) / continuation.intent_path).is_file()
            failed = True
            raise SchedulerEngineError(
                SchedulerEngineErrorKind.BUSY,
                "injected timeout checkpoint busy",
            )
        return original_cas(
            conn,
            run_id=run_id,
            expected_version=expected_version,
            new_state=new_state,  # type: ignore[arg-type]
            now=now,
        )

    monkeypatch.setattr(tick.store, "compare_and_swap_state", fail_timeout_cas_once)
    with pytest.raises(SchedulerEngineError):
        for _ in range(20):
            tick.run_once()
    assert failed
    monkeypatch.setattr(tick.store, "compare_and_swap_state", original_cas)

    aborted = SchedulerAbortService(tick.store, backend, now_factory=clock).abort_run(run_id)
    assert aborted.state_kind == "aborted"
    assert aborted.termination_pending is False
    for _ in range(3):
        tick.run_once()
    assert _state(scheduler_paths, run_id).kind == "aborted"
    assert _timeout_effect_count(scheduler_paths, run_id) == 0
    assert _timeout_event_count(scheduler_paths, run_id) == 0
    assert _attempt_count(scheduler_paths, run_id) == 1
    with tick.store.begin_read() as conn:
        assert tick.store.get_reservation_for_run(conn, run_id) is None


def test_force_rejects_before_mutation_and_manual_auto_race_dispatches_once(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    run_id = _submit(git_repo, scheduler_paths)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    state = _drive_to_timeout(tick, scheduler_paths, run_id)
    before_version = state.version
    service = ImplementerRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    )
    with pytest.raises(SchedulerEngineError, match="blocked run"):
        service.retry(run_id, force=True)
    assert _state(scheduler_paths, run_id).version == before_version
    rejected = CliRunner().invoke(
        app, ["scheduler", "implementer-retry", run_id, "--force", "--output", "json"]
    )
    assert rejected.exit_code == 4
    assert "requires a blocked run" in rejected.output
    assert _state(scheduler_paths, run_id).version == before_version

    clock.advance(minutes=CODEX_IMPLEMENTER_TIMEOUT_RETRY_DELAY_SECONDS // 60)
    claim_entered = threading.Event()
    release_claim = threading.Event()

    def pause_before_claim() -> None:
        claim_entered.set()
        if not release_claim.wait(timeout=10):
            raise AssertionError("timeout retry claim barrier timed out")

    race_tick = _tick(
        git_repo,
        scheduler_paths,
        backend=backend,
        owner="tick-timeout-race",
        now_factory=clock,
        before_effect_claim=pause_before_claim,
    )
    errors: list[BaseException] = []

    def automatic_dispatch() -> None:
        try:
            race_tick.run_once()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=automatic_dispatch)
    thread.start()
    assert claim_entered.wait(timeout=10)
    manual = ImplementerRetryService(
        SqliteSchedulerStore(scheduler_paths["db_path"]),
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=lambda: datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
    ).retry(run_id)
    assert manual.changed is True
    assert manual.available_at == "2026-10-14T12:00:00.000000Z"
    release_claim.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert _timeout_effect_count(scheduler_paths, run_id) == 1
    assert _attempt_count(scheduler_paths, run_id) == 2
    assert len(backend.launch_calls) == 2

    completed = _drive_to_kind(race_tick, scheduler_paths, run_id, "completed")
    assert completed.kind == "completed"
    assert _attempt_count(scheduler_paths, run_id) == 2
    assert backend.implementer_calls == 2


def test_sequence_timeout_remains_the_same_current_leaf_and_projects_retry(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
) -> None:
    payload = {
        "schema_version": 3,
        "name": "timeout-current-leaf",
        "short_name": "Timeout current leaf",
        "phases": [
            {
                "name": "phase-1",
                "short_name": "Timeout phase one",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "implementer": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "reasoning_effort": "xhigh",
                    "timeout_minutes": 15,
                },
                "codex": {
                    "review_model": "gpt-5.6-sol",
                    "review_reasoning_effort": "high",
                },
                "commit_message": "accepted timeout phase",
            },
            {
                "name": "phase-2",
                "short_name": "Timeout phase two",
                "plan_path": "docs/plans/sample-plan.md",
                "prompt_source_path": "docs/plans/prompt_sample-plan.txt",
                "implementer": {
                    "provider": "codex",
                    "model": "gpt-5.5",
                    "reasoning_effort": "xhigh",
                    "timeout_minutes": 15,
                },
                "codex": {
                    "review_model": "gpt-5.6-sol",
                    "review_reasoning_effort": "high",
                },
            },
        ],
    }
    prepared = prepare(
        git_repo,
        scheduler_paths,
        ("codex",),
        payload=payload,
    )
    started = start_sequence(prepared.sequence_id, db_path=scheduler_paths["db_path"])
    clock = _Clock()
    backend = _InlineCodexBackend(["timeout", "success"])
    tick = _tick(git_repo, scheduler_paths, backend=backend, now_factory=clock)
    leaf_id = started.run_id
    _drive_to_timeout(tick, scheduler_paths, leaf_id)

    sequence = sequence_state(scheduler_paths, prepared.sequence_id)
    assert sequence.current_run_id == leaf_id
    assert sequence.current_ordinal == 1
    assert leaf_state(scheduler_paths, leaf_id).context.sequence.ordinal == 1
    status = SequenceStatusService(
        tick.store,
        artifacts=ProtectedArtifactStore(scheduler_paths["artifact_root"]),
    ).get_status(prepared.sequence_id)
    assert status.current_run_id == leaf_id
    assert status.implementer_retry_wait_until == "2026-10-14T12:30:00.000000Z"
    assert status.implementer_timeout_automatic_retries == 1
    assert status.safe_next_action.command == (f"ai_dev_loop scheduler implementer-retry {leaf_id}")

    service = ImplementerRetryService(
        tick.store,
        ProtectedArtifactStore(scheduler_paths["artifact_root"]),
        now_factory=clock,
    )
    assert service.retry(leaf_id).changed is True
    assert sequence_state(scheduler_paths, prepared.sequence_id).current_run_id == leaf_id
    assert leaf_state(scheduler_paths, leaf_id).context.sequence.ordinal == 1
