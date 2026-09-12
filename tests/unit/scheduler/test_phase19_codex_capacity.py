"""Phase 19 Codex usage-capacity continuation tests."""

from __future__ import annotations

import itertools
import json
import os
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import (
    CONTROLLER_SESSION,
    DIGEST,
    sample_agent_led_submitted_context,
)
from tests.unit.scheduler.test_phase17_5_codex_corrections import (
    BOOTSTRAP_ID,
    _awaiting_codex_review_workflow,
)
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.response_schema import (
    events_text_indicates_usage_limit_exceeded,
)
from ai_dev_loop.runners.codex_failure import (
    FAILURE_CODE_CODEX_USAGE_LIMIT,
    classify_codex_review_events_text,
    is_codex_usage_limit_recovery_eligible,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexAppServerCapacityProbe,
    CodexCapacityStatus,
    build_capacity_probe_stdin,
    capacity_from_rate_limits_payload,
    parse_capacity_probe_stdout,
)
from ai_dev_loop.scheduler.application.codex_subprocess_env import sanitize_codex_subprocess_env
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.reducer import apply_codex_usage_capacity_detected
from ai_dev_loop.scheduler.domain.state import (
    SCHEDULER_STATE_ADAPTER,
    WaitingCodexCapacityState,
    parse_scheduler_state,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _api_envelope(*, code: str, message: str) -> dict:
    return {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": code,
            "message": message,
        },
        "status": 429,
    }


def _codex_error_wrapper(envelope: dict) -> dict:
    return {"type": "error", "message": json.dumps(envelope, separators=(",", ":"))}


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
        tick_owner_factory=lambda: f"tick-19-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=_next_attempt_id,
        attempt_backend=backend,
        preflight_port=OkPreflightPort(),
    )


def _run_until(
    tick: TickService,
    run_id: str,
    *,
    target_kind: str,
    max_ticks: int = 40,
) -> None:
    for _ in range(max_ticks):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            if state.kind == target_kind:
                return
    raise AssertionError(f"run {run_id} did not reach {target_kind}")


class TestUsageLimitClassifier:
    def test_canonical_usage_limit_exceeded(self) -> None:
        text = json.dumps(
            _codex_error_wrapper(
                _api_envelope(code="usage_limit_exceeded", message="quota exceeded")
            )
        )
        assert classify_codex_review_events_text(text).is_usage_limit

    def test_rate_limit_exceeded_is_not_classified(self) -> None:
        text = json.dumps(
            _codex_error_wrapper(_api_envelope(code="rate_limit_exceeded", message="slow down"))
        )
        assert not classify_codex_review_events_text(text).is_usage_limit

    def test_prose_usage_limit_is_not_classified(self) -> None:
        text = json.dumps({"type": "error", "message": "usage_limit_exceeded in prose"})
        assert not classify_codex_review_events_text(text).is_usage_limit

    def test_split_facts_fail_closed(self) -> None:
        text = "\n".join(
            [
                json.dumps(_codex_error_wrapper(_api_envelope(code="other", message="partial"))),
                json.dumps({"type": "error", "message": "usage_limit_exceeded"}),
            ]
        )
        assert not events_text_indicates_usage_limit_exceeded(text)


class TestUsageLimitRecoveryEligibility:
    def test_timeout_blocks_recovery_even_with_marker(self) -> None:
        outcome = {
            "failure_code": FAILURE_CODE_CODEX_USAGE_LIMIT,
            "timed_out": True,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "review_output_truncated": False,
            "bootstrap_uncertainty_reason": "",
            "review_block_reason": "",
            "failure_kind": "",
        }
        assert not is_codex_usage_limit_recovery_eligible(outcome)

    def test_truncation_blocks_recovery_even_with_marker(self) -> None:
        outcome = {
            "failure_code": FAILURE_CODE_CODEX_USAGE_LIMIT,
            "timed_out": False,
            "stdout_truncated": True,
            "stderr_truncated": False,
            "review_output_truncated": True,
            "bootstrap_uncertainty_reason": "",
            "review_block_reason": "codex_review_output_truncated",
            "failure_kind": "",
        }
        assert not is_codex_usage_limit_recovery_eligible(outcome)


class TestSubprocessEnvironmentPolicy:
    def test_omits_drvfs_codex_home_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEX_HOME", "/mnt/c/Users/me/.codex")
        monkeypatch.setenv("CODEX_SQLITE_HOME", "/home/me/.codex/sqlite")
        monkeypatch.setenv("KEEP_ME", "yes")
        base = {"CODEX_HOME": "/mnt/c/Users/me/.codex", "KEEP_ME": "yes"}
        sanitized = sanitize_codex_subprocess_env(base)
        assert "CODEX_HOME" not in sanitized
        assert sanitized["KEEP_ME"] == "yes"
        assert os.environ["CODEX_HOME"] == "/mnt/c/Users/me/.codex"

    def test_preserves_native_wsl_override(self) -> None:
        base = {"CODEX_HOME": "/home/me/.codex", "CODEX_SQLITE_HOME": "/home/me/.codex/sqlite"}
        sanitized = sanitize_codex_subprocess_env(base)
        assert sanitized["CODEX_HOME"] == "/home/me/.codex"
        assert sanitized["CODEX_SQLITE_HOME"] == "/home/me/.codex/sqlite"


class TestCapacityProbeParsing:
    def test_primary_and_secondary_must_both_have_capacity(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "primary": {"usedPercent": 0},
                    "secondary": {"usedPercent": 100},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_legacy_rate_limits_single_record(self) -> None:
        payload = {
            "rateLimits": {
                "primary": {"usedPercent": 10},
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_primary_only_window(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"primary": {"usedPercent": 5}},
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_secondary_only_window(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"secondary": {"usedPercent": 100}},
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_null_window_is_absent(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "primary": None,
                    "secondary": {"usedPercent": 0},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_invalid_percent_is_unavailable(self) -> None:
        payload = {"rateLimitsByLimitId": {"default": {"primary": {"usedPercent": "full"}}}}
        assert capacity_from_rate_limits_payload(payload) is None

    def test_empty_records_are_unavailable(self) -> None:
        assert capacity_from_rate_limits_payload({"rateLimitsByLimitId": {}}) is None

    def test_exhausted_then_invalid_record_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "exhausted": {
                    "primary": {"usedPercent": 100},
                    "secondary": {"usedPercent": 100},
                },
                "invalid": {"primary": {"usedPercent": "full"}},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_invalid_then_exhausted_record_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "invalid": {"primary": {"usedPercent": "full"}},
                "exhausted": {
                    "primary": {"usedPercent": 100},
                    "secondary": {"usedPercent": 100},
                },
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_exhausted_then_empty_record_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "exhausted": {
                    "primary": {"usedPercent": 100},
                    "secondary": {"usedPercent": 100},
                },
                "empty": {},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_available_then_exhausted_record_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "available": {"primary": {"usedPercent": 0}},
                "exhausted": {
                    "primary": {"usedPercent": 100},
                    "secondary": {"usedPercent": 100},
                },
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_oversized_used_percent_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"primary": {"usedPercent": 10**400}},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None


class TestCapacityProbeExchange:
    def test_build_stdin_uses_initialized_notification(self) -> None:
        stdin_text = build_capacity_probe_stdin(init_id=1, limits_id=2)
        lines = [json.loads(line) for line in stdin_text.splitlines() if line.strip()]
        assert lines[1]["method"] == "initialized"
        assert lines[1].get("method") != "notifications/initialized"

    def test_parse_complete_exchange(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {
                                    "primary": {"usedPercent": 0},
                                    "secondary": {"usedPercent": 0},
                                }
                            }
                        },
                    }
                ),
            ]
        )
        parsed = parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2)
        assert parsed is not None
        assert capacity_from_rate_limits_payload(parsed) == CodexCapacityStatus.AVAILABLE

    def test_malformed_json_fails_closed(self) -> None:
        stdout = "not-json\n"
        assert parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2) is None

    def test_duplicate_init_response_fails_closed(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}),
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}),
            ]
        )
        assert parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2) is None

    def test_error_response_fails_closed(self) -> None:
        stdout = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"code": -1}})
        assert parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2) is None

    def test_unrelated_notification_is_ignored(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {"primary": {"usedPercent": 0}},
                            }
                        },
                    }
                ),
            ]
        )
        parsed = parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2)
        assert parsed is not None

    def test_parse_headerless_wire_format(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"id": 1, "result": {"capabilities": {}}}),
                json.dumps({"method": "turn/started", "params": {"turn": {"id": "turn_1"}}}),
                json.dumps(
                    {
                        "id": 2,
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {
                                    "primary": {"usedPercent": 0},
                                    "secondary": {"usedPercent": 0},
                                }
                            }
                        },
                    }
                ),
            ]
        )
        parsed = parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2)
        assert parsed is not None
        assert capacity_from_rate_limits_payload(parsed) == CodexCapacityStatus.AVAILABLE

    def test_reject_wrong_jsonrpc_version(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"jsonrpc": "1.0", "id": 1, "result": {"capabilities": {}}}),
                json.dumps(
                    {
                        "id": 2,
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {"primary": {"usedPercent": 0}},
                            }
                        },
                    }
                ),
            ]
        )
        assert parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2) is None

    def test_excessive_json_nesting_fails_closed(self) -> None:
        from ai_dev_loop.scheduler.application.codex_capacity_probe import _loads_probe_line
        from ai_dev_loop.scheduler.domain.codex_contract import (
            CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH,
        )

        depth = CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH + 5
        nested: dict[str, object] = {"usedPercent": 0}
        for _ in range(depth):
            nested = {"window": nested}
        line = json.dumps({"id": 1, "result": nested})
        assert _loads_probe_line(line) is None


class TestCapacityProbeRunner:
    def test_fake_app_server_probe_available(self, fake_clis: dict[str, Path]) -> None:
        probe = CodexAppServerCapacityProbe()
        observation = probe.probe("codex")
        assert observation.status == CodexCapacityStatus.AVAILABLE

    def test_fake_app_server_probe_exhausted(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        probe = CodexAppServerCapacityProbe()
        observation = probe.probe("codex")
        assert observation.status == CodexCapacityStatus.EXHAUSTED

    def test_fake_app_server_probe_legacy_shape(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "legacy")
        probe = CodexAppServerCapacityProbe()
        assert probe.probe("codex").status == CodexCapacityStatus.AVAILABLE

    def test_missing_command_is_unavailable(self) -> None:
        probe = CodexAppServerCapacityProbe()
        observation = probe.probe("/tmp/does-not-exist-codex-probe")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE

    def test_fake_app_server_headerless_protocol(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "primary_only")
        probe = CodexAppServerCapacityProbe()
        assert probe.probe("codex").status == CodexCapacityStatus.AVAILABLE

    def test_oversized_numeric_limits_are_unavailable(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FAKE_CODEX_CAPACITY_LIMITS_JSON",
            json.dumps(
                {
                    "rateLimitsByLimitId": {
                        "default": {"primary": {"usedPercent": 10**400}},
                    }
                }
            ),
        )
        probe = CodexAppServerCapacityProbe()
        assert probe.probe("codex").status == CodexCapacityStatus.UNAVAILABLE

    def test_protocol_error_response_is_unavailable(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "protocol_error")
        probe = CodexAppServerCapacityProbe()
        assert probe.probe("codex").status == CodexCapacityStatus.UNAVAILABLE

    def test_wrong_handshake_is_unavailable(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FAKE_CODEX_CAPACITY_LIMITS_JSON",
            json.dumps({"rateLimitsByLimitId": {"default": {"primary": {"usedPercent": 0}}}}),
        )

        def bad_stdin(*, init_id: int, limits_id: int) -> str:
            initialize = build_capacity_probe_stdin(init_id=init_id, limits_id=limits_id)
            return initialize.replace('"initialized"', '"notifications/initialized"', 1)

        monkeypatch.setattr(
            "ai_dev_loop.scheduler.application.codex_capacity_probe.build_capacity_probe_stdin",
            bad_stdin,
        )
        probe = CodexAppServerCapacityProbe()
        assert probe.probe("codex").status == CodexCapacityStatus.UNAVAILABLE


class TestWaitingStateSchema:
    def test_waiting_snapshot_round_trip(self) -> None:
        from ai_dev_loop.scheduler.domain.state import (
            AdmittedRunCheckpoint,
            AwaitingCodexReviewState,
            CodexWorkflowCheckpoint,
            CursorWorkflowCheckpoint,
        )

        context = sample_agent_led_submitted_context()
        awaiting = AwaitingCodexReviewState(
            run_id="run-1",
            version=2,
            submitted_at="2026-01-01T00:00:00.000000Z",
            updated_at="2026-01-01T00:00:00.000000Z",
            idempotency_key=DIGEST,
            context=context,
            checkpoint=AdmittedRunCheckpoint(
                authorized_at="2026-01-01T00:00:00.000000Z",
                admitted_at="2026-01-01T00:00:00.000000Z",
                admission_status_artifact_path="admission/status.json",
                admission_status_sha256=DIGEST,
            ),
            cursor=CursorWorkflowCheckpoint(
                iteration=1,
                chat_id="22222222-2222-2222-2222-222222222222",
                staged_patch_path="git/diffs/01.patch",
                staged_patch_sha256=DIGEST,
            ),
            codex=CodexWorkflowCheckpoint(
                review_iteration=1,
                reviewer_session_id=BOOTSTRAP_ID,
            ),
        )
        from ai_dev_loop.scheduler.domain.events import CodexUsageCapacityDetectedEvent

        waiting = apply_codex_usage_capacity_detected(
            awaiting,
            CodexUsageCapacityDetectedEvent(run_id="run-1", review_iteration=1),
            now_text="2026-01-01T00:00:01.000000Z",
        )
        state = parse_scheduler_state(waiting.model_dump(mode="json"))
        assert isinstance(state, WaitingCodexCapacityState)
        round_trip = SCHEDULER_STATE_ADAPTER.validate_python(state.model_dump(mode="json"))
        assert round_trip.kind == "waiting_codex_capacity"


def test_quota_failure_waits_then_resumes_same_reviewer(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    now = datetime(2026, 1, 1, tzinfo=UTC)
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
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=60)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_capacity"
        reviewer_id = state.codex.reviewer_session_id
        assert reviewer_id == BOOTSTRAP_ID
        reviews_completed = state.codex.reviews_completed
    tick.run_once()
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_capacity"
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "no_findings")
    tick.run_once()
    _run_until(tick, run_id, target_kind="completed", max_ticks=60)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.codex.reviewer_session_id == reviewer_id
        assert state.codex.reviews_completed == reviews_completed + 1
    codex_log = Path(fake_clis["codex_log"]).read_text(encoding="utf-8")
    assert codex_log.count("'resume'") >= 1


def test_repeated_quota_cycle_waits_again_without_extra_review_count(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_SEQUENCE", "usage_limit,usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    run_id = _submit(git_repo, scheduler_paths, max_reviews=5)
    start_run(run_id, db_path=scheduler_paths["db_path"])
    tick = _tick_service(
        git_repo,
        scheduler_paths,
        now=now,
        backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
    )
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=80)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        reviewer_id = state.codex.reviewer_session_id
        reviews_completed = state.codex.reviews_completed
        first_version = state.version
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
    for _ in range(80):
        tick.run_once()
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        if state.kind == "waiting_codex_capacity" and state.version > first_version:
            break
    else:
        raise AssertionError("run did not re-enter waiting_codex_capacity after second quota")
    assert state.codex.reviewer_session_id == reviewer_id
    assert state.codex.reviews_completed == reviews_completed


def test_unbound_bootstrap_quota_blocks(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.delenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", raising=False)
    monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", "")
    now = datetime(2026, 1, 1, tzinfo=UTC)
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
    _run_until(tick, run_id, target_kind="blocked", max_ticks=60)
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.block_reason_kind == "codex_bootstrap_uncertain"


def test_probe_malformed_response_blocks_from_waiting(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    now = datetime(2026, 1, 1, tzinfo=UTC)
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
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=60)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY_SHAPE", "protocol_error")
    tick.run_once()
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "codex_capacity_probe_unavailable"


def test_probe_unavailable_blocks_from_waiting(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    now = datetime(2026, 1, 1, tzinfo=UTC)
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
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=60)
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    tick.run_once()
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "blocked"
        assert state.block_reason_kind == "codex_capacity_probe_unavailable"


def test_stale_tick_lease_skips_capacity_probe_block(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
    now = datetime(2026, 1, 1, tzinfo=UTC)
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
    _run_until(tick, run_id, target_kind="waiting_codex_capacity", max_ticks=60)
    assert tick._codex_workflow is not None
    monkeypatch.setenv("FAKE_CODEX_CAPACITY", "unavailable")
    with patch(
        "ai_dev_loop.scheduler.application.codex_workflow_service.tick_lease_is_active",
        return_value=False,
    ):
        receipt = tick._codex_workflow._maybe_probe_codex_capacity("stale-owner", 0, run_id)
    assert receipt is None
    with tick.store.begin_read() as conn:
        state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert state.kind == "waiting_codex_capacity"


def test_ingest_usage_limit_blocked_by_timeout(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, run_id, attempt, dispatch_id = _awaiting_codex_review_workflow(
        git_repo, scheduler_paths, monkeypatch, fake_clis=fake_clis
    )
    synthetic_outcome = {
        "effect_kind": "codex.bootstrap_review",
        "attempt_id": attempt["attempt_id"],
        "dispatch_id": dispatch_id,
        "review_iteration": 1,
        "timed_out": True,
        "stdout_truncated": False,
        "stderr_truncated": False,
        "review_output_truncated": False,
        "bootstrap_uncertainty_reason": "",
        "bootstrap_session_id": BOOTSTRAP_ID,
        "failure_code": FAILURE_CODE_CODEX_USAGE_LIMIT,
        "review_result_sha256": "",
    }
    with patch.object(workflow, "_authenticated_outcome", return_value=synthetic_outcome):
        receipt = workflow._ingest_codex_review(run_id, attempt)
    assert receipt.action == "blocked"
    assert receipt.detail == "codex_review_timeout"


def test_ingest_usage_limit_blocked_by_truncation(
    git_repo: Path,
    scheduler_paths: dict[str, Path],
    fake_clis: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow, run_id, attempt, dispatch_id = _awaiting_codex_review_workflow(
        git_repo, scheduler_paths, monkeypatch, fake_clis=fake_clis
    )
    synthetic_outcome = {
        "effect_kind": "codex.bootstrap_review",
        "attempt_id": attempt["attempt_id"],
        "dispatch_id": dispatch_id,
        "review_iteration": 1,
        "timed_out": False,
        "stdout_truncated": True,
        "stderr_truncated": False,
        "review_output_truncated": True,
        "review_block_reason": "codex_review_output_truncated",
        "bootstrap_uncertainty_reason": "",
        "bootstrap_session_id": BOOTSTRAP_ID,
        "failure_code": FAILURE_CODE_CODEX_USAGE_LIMIT,
        "review_result_sha256": "",
    }
    with patch.object(workflow, "_authenticated_outcome", return_value=synthetic_outcome):
        receipt = workflow._ingest_codex_review(run_id, attempt)
    assert receipt.action == "blocked"
    assert receipt.detail == "codex_review_output_truncated"
