"""Phase 20.5 reliable Codex limit detection and capacity-probe recovery tests."""

from __future__ import annotations

import itertools
import json
import os
import stat
import sys
import textwrap
import time
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.conftest import FIXTURE_REPO
from tests.unit.scheduler.helpers import CONTROLLER_SESSION
from tests.unit.scheduler.test_phase17_5_codex_corrections import BOOTSTRAP_ID
from tests.unit.scheduler.test_tick import FakeGitAdmissionPort, OkPreflightPort

from ai_dev_loop.response_schema import (
    events_text_indicates_provider_message_limit,
    provider_message_indicates_usage_limit,
)
from ai_dev_loop.runners.codex_failure import (
    CodexLimitEvidenceKind,
    classify_codex_review_events_text,
)
from ai_dev_loop.scheduler.application.codex_capacity_probe import (
    CodexAppServerCapacityProbe,
    CodexCapacityReason,
    CodexCapacityStatus,
    _InteractiveProbeResult,
    _ProbeExchangeTracker,
    _reap_probe_process,
    _run_interactive_capacity_probe,
    capacity_from_rate_limits_payload,
    parse_capacity_probe_stdout,
)
from ai_dev_loop.scheduler.application.codex_subprocess_env import sanitize_codex_subprocess_env
from ai_dev_loop.scheduler.application.fake_attempt_backend import (
    FakeAgentProcessBackend,
    FakeAttemptScenario,
)
from ai_dev_loop.scheduler.application.review_retry import ReviewRetryService
from ai_dev_loop.scheduler.application.start import start_run
from ai_dev_loop.scheduler.application.submission import SubmitOptions, submit_run
from ai_dev_loop.scheduler.application.tick import TickService
from ai_dev_loop.scheduler.domain.state import (
    WaitingCodexCapacityState,
    WaitingCodexReviewRetryState,
)
from ai_dev_loop.scheduler.infrastructure.protected_artifacts import ProtectedArtifactStore
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SqliteSchedulerStore

_ATTEMPT_COUNTER = itertools.count()


def _explicit_limit_record(**fields: object) -> dict:
    return {"rateLimitReachedType": None, **fields}


class TestProviderMessageMarkers:
    @pytest.mark.parametrize(
        "marker",
        [
            "usage limit",
            "usage_limit_exceeded",
            "rate limit",
            "rate_limit_exceeded",
            "quota exceeded",
            "insufficient_quota",
            "too many requests",
        ],
    )
    def test_each_marker_in_error_wrapper(self, marker: str) -> None:
        text = json.dumps({"type": "error", "message": f"Provider says {marker} now"})
        assert events_text_indicates_provider_message_limit(text)
        classification = classify_codex_review_events_text(text)
        assert classification.evidence == CodexLimitEvidenceKind.PROVIDER_MESSAGE_LIMIT

    @pytest.mark.parametrize(
        "marker",
        [
            "usage limit",
            "rate_limit_exceeded",
        ],
    )
    def test_each_marker_in_turn_failed_wrapper(self, marker: str) -> None:
        text = json.dumps({"type": "turn.failed", "error": {"message": marker.upper()}})
        assert events_text_indicates_provider_message_limit(text)

    def test_generic_limit_word_is_not_evidence(self) -> None:
        text = json.dumps({"type": "error", "message": "hit the limit"})
        assert not events_text_indicates_provider_message_limit(text)

    def test_try_again_alone_is_not_evidence(self) -> None:
        text = json.dumps({"type": "error", "message": "try again later"})
        assert not events_text_indicates_provider_message_limit(text)

    def test_agent_message_content_is_ignored(self) -> None:
        text = json.dumps({"type": "message", "content": "usage_limit_exceeded"})
        assert not events_text_indicates_provider_message_limit(text)

    def test_whitespace_and_case_normalization(self) -> None:
        assert provider_message_indicates_usage_limit("  USAGE   LIMIT  ")


class TestCapacityExhaustionSemantics:
    def test_rate_limit_reached_type_exhausts_without_windows(self) -> None:
        payload = {"rateLimitsByLimitId": {"default": {"rateLimitReachedType": "weekly"}}}
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_above_one_hundred_percent_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": _explicit_limit_record(primary={"usedPercent": 150}),
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_exhausted_record_wins_over_invalid_sibling(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "invalid": {"primary": {"usedPercent": "full"}},
                "exhausted": _explicit_limit_record(primary={"usedPercent": 100}),
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_zero_credits_without_windows_is_unavailable(self) -> None:
        payload = {"rateLimitsByLimitId": {"default": {"credits": 0}}}
        assert capacity_from_rate_limits_payload(payload) is None

    def test_malformed_primary_with_exhausted_secondary_window_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": _explicit_limit_record(
                    primary={"usedPercent": "full"},
                    secondary={"usedPercent": 100},
                ),
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_malformed_record_with_exhausted_sibling_record_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "invalid": {"primary": {"usedPercent": "full"}},
                "exhausted": _explicit_limit_record(primary={"usedPercent": 100}),
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_present_null_current_schema_does_not_fall_back_to_legacy(self) -> None:
        payload = {
            "rateLimitsByLimitId": None,
            "rateLimits": {"primary": {"usedPercent": 0}},
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_absent_current_schema_uses_legacy(self) -> None:
        payload = {"rateLimits": {"primary": {"usedPercent": 0}}}
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_null_reached_marker_without_windows_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"rateLimitReachedType": None},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_null_reached_marker_with_available_windows_is_available(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "rateLimitReachedType": None,
                    "primary": {"usedPercent": 0},
                    "secondary": {"usedPercent": 10},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.AVAILABLE

    def test_empty_string_reached_marker_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {"rateLimitReachedType": ""},
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None

    def test_malformed_marker_with_exhausted_window_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "default": {
                    "rateLimitReachedType": None,
                    "primary": {"usedPercent": 100},
                }
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_non_object_sibling_with_exhausted_record_is_exhausted(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "bad": "not-a-record",
                "exhausted": _explicit_limit_record(primary={"usedPercent": 100}),
            }
        }
        assert capacity_from_rate_limits_payload(payload) == CodexCapacityStatus.EXHAUSTED

    def test_available_record_with_malformed_sibling_is_unavailable(self) -> None:
        payload = {
            "rateLimitsByLimitId": {
                "good": {"primary": {"usedPercent": 50}},
                "bad": "not-a-record",
            }
        }
        assert capacity_from_rate_limits_payload(payload) is None


class TestInteractiveTransport:
    def test_interactive_fake_succeeds_while_stdin_stays_open(
        self, fake_clis: dict[str, Path]
    ) -> None:
        probe = CodexAppServerCapacityProbe()
        observation = probe.probe("codex")
        assert observation.status == CodexCapacityStatus.AVAILABLE

    def test_oneshot_eof_transport_can_be_unavailable(
        self, fake_clis: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_CAPACITY_TRANSPORT", "oneshot_eof")
        probe = CodexAppServerCapacityProbe(timeout_seconds=3)
        observation = probe.probe("codex")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE

    def test_ambiguous_init_response_shape_is_protocol_error(self, tmp_path: Path) -> None:
        script = tmp_path / "probe_ambiguous_init.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import json, sys
                sys.stdout.write(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "initialize",
                            "result": {"capabilities": {}},
                        }
                    )
                    + "\\n"
                )
                sys.stdout.flush()
                import time
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=2,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        assert result.protocol_error
        assert not result.cleanup_failed

    def test_boolean_limits_response_is_protocol_error(self, tmp_path: Path) -> None:
        script = tmp_path / "probe_boolean_limits_id.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import json, sys
                init = {"id": 1, "result": {"capabilities": {}}}
                limits = {
                    "id": True,
                    "result": {
                        "rateLimitsByLimitId": {
                            "default": {"primary": {"usedPercent": 0}},
                        }
                    },
                }
                sys.stdout.write(json.dumps(init) + "\\n")
                sys.stdout.write(json.dumps(limits) + "\\n")
                sys.stdout.flush()
                import time
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=2,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        assert result.protocol_error
        assert not result.cleanup_failed


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


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_child_pid(marker_path: Path) -> int:
    return int(marker_path.read_text(encoding="utf-8").strip())


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
) -> TickService:
    store = SqliteSchedulerStore(scheduler_paths["db_path"])
    artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
    return TickService(
        store,
        artifacts,
        FakeGitAdmissionPort(resolved_root=str(git_repo.resolve())),
        now_factory=lambda: now,
        tick_owner_factory=lambda: f"tick-20-5-{next(_ATTEMPT_COUNTER)}",
        attempt_id_factory=lambda: f"att-{next(_ATTEMPT_COUNTER):032x}",
        attempt_backend=FakeAgentProcessBackend(
            default_scenario=FakeAttemptScenario(active_ticks=0, exit_code=0)
        ),
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


class TestOrderedAppServerExchange:
    def _limits_line(self) -> str:
        return json.dumps(
            {
                "id": 2,
                "result": {
                    "rateLimitsByLimitId": {
                        "default": {"primary": {"usedPercent": 0}},
                    }
                },
            }
        )

    def _init_line(self) -> str:
        return json.dumps({"id": 1, "result": {"capabilities": {}}})

    def test_limits_before_init_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes((self._limits_line() + "\n").encode("utf-8"))
        assert tracker.protocol_error

    def test_duplicate_limits_response_is_protocol_error(self) -> None:
        stdout = "\n".join([self._init_line(), self._limits_line(), self._limits_line()])
        assert (
            parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2, outbound_complete=True)
            is None
        )

    def test_trailing_terminal_response_after_limits_is_protocol_error(self) -> None:
        stdout = "\n".join(
            [
                self._init_line(),
                self._limits_line(),
                json.dumps({"id": 99, "result": {"capabilities": {}}}),
            ]
        )
        assert (
            parse_capacity_probe_stdout(stdout, init_id=1, limits_id=2, outbound_complete=True)
            is None
        )

    def test_fragmented_multi_chunk_exchange_parses(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        init = self._init_line() + "\n"
        limits = self._limits_line() + "\n"
        combined = init + limits
        tracker.outbound_complete = True
        tracker.feed_bytes(combined[:10].encode("utf-8"))
        tracker.feed_bytes(combined[10:].encode("utf-8"))
        tracker.mark_draining_complete()
        assert not tracker.protocol_error
        assert tracker.limits_result is not None

    def test_out_of_order_limits_before_init_fails(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes((self._limits_line() + "\n" + self._init_line() + "\n").encode("utf-8"))
        assert tracker.protocol_error

    def test_late_duplicate_limits_during_drain_fails(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.outbound_complete = True
        tracker.feed_bytes((self._init_line() + "\n" + self._limits_line() + "\n").encode("utf-8"))
        assert tracker.phase.value == "draining"
        tracker.feed_bytes((self._limits_line() + "\n").encode("utf-8"))
        assert tracker.protocol_error

    def test_malformed_line_before_init_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(b"not-json\n")
        assert tracker.protocol_error

    def test_invalid_jsonrpc_before_init_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(
            (json.dumps({"jsonrpc": "1.0", "id": 1, "result": {}}) + "\n").encode("utf-8")
        )
        assert tracker.protocol_error

    def test_init_and_limits_in_one_read_before_outbound_complete_fails(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        combined = self._init_line() + "\n" + self._limits_line() + "\n"
        tracker.feed_bytes(combined.encode("utf-8"))
        assert tracker.protocol_error

    def test_explicit_null_jsonrpc_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(
            (json.dumps({"jsonrpc": None, "id": 1, "result": {}}) + "\n").encode("utf-8")
        )
        assert tracker.protocol_error

    def test_notification_with_explicit_null_id_is_not_notification(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(
            (
                json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "id": None})
                + "\n"
            ).encode("utf-8")
        )
        assert tracker.protocol_error

    def test_notification_without_id_member_is_ignored(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.outbound_complete = True
        tracker.feed_bytes(
            (
                json.dumps({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
                + "\n"
                + self._init_line()
                + "\n"
                + self._limits_line()
                + "\n"
            ).encode("utf-8")
        )
        assert not tracker.protocol_error
        assert tracker.limits_result is not None

    def test_limits_only_accepted_after_outbound_complete(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes((self._init_line() + "\n").encode("utf-8"))
        assert not tracker.protocol_error
        tracker.feed_bytes((self._limits_line() + "\n").encode("utf-8"))
        assert tracker.protocol_error
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.outbound_complete = True
        tracker.feed_bytes((self._init_line() + "\n" + self._limits_line() + "\n").encode("utf-8"))
        assert tracker.limits_result is not None

    def test_boolean_init_id_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(
            (json.dumps({"id": True, "result": {"capabilities": {}}}) + "\n").encode("utf-8")
        )
        assert tracker.protocol_error

    def test_boolean_limits_id_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.outbound_complete = True
        tracker.feed_bytes(
            (
                self._init_line()
                + "\n"
                + json.dumps(
                    {
                        "id": True,
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {"primary": {"usedPercent": 0}},
                            }
                        },
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        assert tracker.protocol_error

    def test_init_response_mixing_method_and_result_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.feed_bytes(
            (
                json.dumps(
                    {
                        "id": 1,
                        "method": "initialize",
                        "result": {"capabilities": {}},
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        assert tracker.protocol_error

    def test_limits_response_mixing_method_and_result_is_protocol_error(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2)
        tracker.outbound_complete = True
        tracker.feed_bytes(
            (
                self._init_line()
                + "\n"
                + json.dumps(
                    {
                        "id": 2,
                        "method": "account/rateLimits/read",
                        "result": {
                            "rateLimitsByLimitId": {
                                "default": {"primary": {"usedPercent": 0}},
                            }
                        },
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
        assert tracker.protocol_error


class TestBoundedCapture:
    def test_newline_free_stdout_flood_hits_output_limit(self, tmp_path: Path) -> None:
        script = tmp_path / "stdout_flood.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import sys, time
                sys.stdout.write("x" * 4096)
                sys.stdout.flush()
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=5,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=128,
            max_stderr_bytes=65536,
        )
        assert result.output_limit
        assert not result.timed_out

    def test_newline_free_stderr_flood_hits_output_limit(self, tmp_path: Path) -> None:
        script = tmp_path / "stderr_flood.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import sys, time
                sys.stderr.write("e" * 4096)
                sys.stderr.flush()
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=5,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=128,
        )
        assert result.output_limit
        assert not result.timed_out

    def test_line_buffer_boundary_enforced_incrementally(self) -> None:
        tracker = _ProbeExchangeTracker(init_id=1, limits_id=2, max_line_bytes=32)
        assert tracker.feed_bytes(b"x" * 33)
        assert tracker.output_limit_exceeded

    def test_output_limit_maps_to_observation_reason(self) -> None:
        limited = _InteractiveProbeResult(
            stdout="",
            stderr="",
            timed_out=False,
            stdout_truncated=True,
            stderr_truncated=False,
            output_limit=True,
            returncode=-15,
            premature_eof=False,
            protocol_error=True,
            cleanup_failed=False,
        )
        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._run_interactive_capacity_probe",
            return_value=limited,
        ):
            observation = CodexAppServerCapacityProbe().probe("codex")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE
        assert observation.reason == CodexCapacityReason.OUTPUT_LIMIT


class TestProbeCleanup:
    def test_setup_exception_still_reaps_child(self, tmp_path: Path) -> None:
        marker = tmp_path / "setup_fail.child.pid"
        script = tmp_path / "probe_setup_fail.py"
        _write_executable(
            script,
            textwrap.dedent(
                f"""
                import time
                from pathlib import Path
                Path({repr(str(marker))}).write_text(str(__import__("os").getpid()), encoding="utf-8")
                time.sleep(30)
                """
            ),
        )

        def fail_nonblocking(_fd: int) -> None:
            raise OSError("simulated nonblocking setup failure")

        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._set_nonblocking",
            fail_nonblocking,
        ):
            result = _run_interactive_capacity_probe(
                [sys.executable, str(script)],
                init_id=1,
                limits_id=2,
                timeout_seconds=2,
                env=sanitize_codex_subprocess_env(),
                max_stdout_bytes=65536,
                max_stderr_bytes=65536,
            )
        assert result.protocol_error
        if marker.is_file():
            assert not _pid_is_alive(_read_child_pid(marker))

    def test_leader_exit_still_terminates_surviving_child(self, tmp_path: Path) -> None:
        marker = tmp_path / "orphan.child.pid"
        script = tmp_path / "probe_orphan_child.py"
        _write_executable(
            script,
            textwrap.dedent(
                f"""
                import os
                from pathlib import Path
                child = os.fork()
                if child == 0:
                    Path({repr(str(marker))}).write_text(str(os.getpid()), encoding="utf-8")
                    import time
                    time.sleep(30)
                    raise SystemExit(0)
                os._exit(0)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=2,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        assert result.premature_eof or result.timed_out
        assert marker.is_file()
        assert not _pid_is_alive(_read_child_pid(marker))

    def test_protocol_error_leader_exits_and_term_ignoring_child_is_killed(
        self, tmp_path: Path
    ) -> None:
        marker = tmp_path / "protocol_fork.child.pid"
        script = tmp_path / "probe_protocol_fork.py"
        _write_executable(
            script,
            textwrap.dedent(
                f"""
                import os
                import signal
                import sys
                import time
                from pathlib import Path

                marker = Path({repr(str(marker))})
                read_fd, write_fd = os.pipe()
                child = os.fork()
                if child == 0:
                    os.close(read_fd)
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    marker.write_text(str(os.getpid()), encoding="utf-8")
                    os.write(write_fd, b"ready")
                    os.close(write_fd)
                    time.sleep(30)
                    raise SystemExit(0)
                os.close(write_fd)
                os.read(read_fd, 5)
                os.close(read_fd)
                sys.stdout.write("not-json\\n")
                sys.stdout.flush()
                signal.signal(signal.SIGTERM, lambda *_args: os._exit(0))
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=2,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        assert result.protocol_error
        assert marker.is_file()
        assert not _pid_is_alive(_read_child_pid(marker))

    def test_term_resistant_child_is_killed_via_process_group(self, tmp_path: Path) -> None:
        marker = tmp_path / "term_ignore.child.pid"
        script = tmp_path / "probe_term_ignore.py"
        _write_executable(
            script,
            textwrap.dedent(
                f"""
                import signal, time
                from pathlib import Path
                Path({repr(str(marker))}).write_text(str(__import__("os").getpid()), encoding="utf-8")
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                time.sleep(30)
                """
            ),
        )
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=2,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        assert result.premature_eof or result.timed_out
        assert marker.is_file()
        assert not _pid_is_alive(_read_child_pid(marker))

    def test_fast_exit_race_classifies_premature_eof(self, tmp_path: Path) -> None:
        script = tmp_path / "probe_fast_exit.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import os
                os._exit(0)
                """
            ),
        )
        started = time.monotonic()
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=5,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        elapsed = time.monotonic() - started
        assert result.premature_eof
        assert not result.timed_out
        assert elapsed < 1.0

    def test_stalled_exchange_times_out_and_cleans_up(self, tmp_path: Path) -> None:
        marker = tmp_path / "stall.child.pid"
        script = tmp_path / "probe_stall.py"
        _write_executable(
            script,
            textwrap.dedent(
                f"""
                import sys, time
                from pathlib import Path
                Path({repr(str(marker))}).write_text(str(__import__("os").getpid()), encoding="utf-8")
                for _ in sys.stdin:
                    pass
                time.sleep(30)
                """
            ),
        )
        started = time.monotonic()
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=1,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        elapsed = time.monotonic() - started
        assert result.timed_out
        assert not result.premature_eof
        assert marker.is_file()
        assert not _pid_is_alive(_read_child_pid(marker))
        assert elapsed >= 0.8
        assert elapsed < 3.0

    def test_persistent_group_liveness_marks_cleanup_failed(self, tmp_path: Path) -> None:
        script = tmp_path / "probe_cleanup_group_alive.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                import time
                time.sleep(30)
                """
            ),
        )

        def always_alive(_pgid: int) -> bool:
            return True

        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._process_group_is_alive",
            always_alive,
        ):
            result = _run_interactive_capacity_probe(
                [sys.executable, str(script)],
                init_id=1,
                limits_id=2,
                timeout_seconds=1,
                env=sanitize_codex_subprocess_env(),
                max_stdout_bytes=65536,
                max_stderr_bytes=65536,
            )
        assert result.cleanup_failed

    def test_signaling_failure_marks_cleanup_failed(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 42424
        mock_proc.stdin = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()

        def noop_signal(_pgid: int, *, sig: int) -> None:
            return None

        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._signal_process_group",
            noop_signal,
        ):
            verified = _reap_probe_process(
                mock_proc,
                deadline=time.monotonic(),
                pgid=42424,
            )
        assert not verified

        real_reap = _reap_probe_process

        def force_cleanup_failed(proc: object, *, deadline: float, pgid: int | None = None) -> bool:
            real_reap(proc, deadline=deadline, pgid=pgid)  # type: ignore[arg-type]
            return False

        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._reap_probe_process",
            side_effect=force_cleanup_failed,
        ):
            result = _run_interactive_capacity_probe(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                init_id=1,
                limits_id=2,
                timeout_seconds=0.2,
                env=sanitize_codex_subprocess_env(),
                max_stdout_bytes=65536,
                max_stderr_bytes=65536,
            )
        assert result.cleanup_failed

    def test_cleanup_failure_returns_unavailable_not_available(self) -> None:
        available_stdout = "\n".join(
            [
                json.dumps({"id": 1, "result": {"capabilities": {}}}),
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
        failed_cleanup = _InteractiveProbeResult(
            stdout=available_stdout,
            stderr="",
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            output_limit=False,
            returncode=0,
            premature_eof=False,
            protocol_error=False,
            cleanup_failed=True,
        )
        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._run_interactive_capacity_probe",
            return_value=failed_cleanup,
        ):
            observation = CodexAppServerCapacityProbe().probe("codex")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE
        assert observation.reason == CodexCapacityReason.CLEANUP_FAILURE

    def test_cleanup_failure_precedes_protocol_error(self) -> None:
        combined = _InteractiveProbeResult(
            stdout="",
            stderr="",
            timed_out=False,
            stdout_truncated=False,
            stderr_truncated=False,
            output_limit=False,
            returncode=0,
            premature_eof=False,
            protocol_error=True,
            cleanup_failed=True,
        )
        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._run_interactive_capacity_probe",
            return_value=combined,
        ):
            observation = CodexAppServerCapacityProbe().probe("codex")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE
        assert observation.reason == CodexCapacityReason.CLEANUP_FAILURE

    def test_cleanup_failure_precedes_timeout(self) -> None:
        combined = _InteractiveProbeResult(
            stdout="",
            stderr="",
            timed_out=True,
            stdout_truncated=False,
            stderr_truncated=False,
            output_limit=False,
            returncode=124,
            premature_eof=False,
            protocol_error=False,
            cleanup_failed=True,
        )
        with patch(
            "ai_dev_loop.scheduler.application.codex_capacity_probe._run_interactive_capacity_probe",
            return_value=combined,
        ):
            observation = CodexAppServerCapacityProbe().probe("codex")
        assert observation.status == CodexCapacityStatus.UNAVAILABLE
        assert observation.reason == CodexCapacityReason.CLEANUP_FAILURE


class TestPrematureEofTiming:
    def test_immediate_exit_is_not_a_full_timeout(self, tmp_path: Path) -> None:
        script = tmp_path / "probe_immediate_eof.py"
        _write_executable(
            script,
            textwrap.dedent(
                """
                raise SystemExit(0)
                """
            ),
        )
        started = time.monotonic()
        result = _run_interactive_capacity_probe(
            [sys.executable, str(script)],
            init_id=1,
            limits_id=2,
            timeout_seconds=4,
            env=sanitize_codex_subprocess_env(),
            max_stdout_bytes=65536,
            max_stderr_bytes=65536,
        )
        elapsed = time.monotonic() - started
        assert result.premature_eof
        assert not result.timed_out
        assert elapsed < 1.0

    def test_parse_requires_outbound_complete_for_limits(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"id": 1, "result": {"capabilities": {}}}),
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
        assert (
            parse_capacity_probe_stdout(
                stdout,
                init_id=1,
                limits_id=2,
                outbound_complete=True,
            )
            is not None
        )


class TestWorkflowDecisionMatrix:
    def test_structured_usage_limit_always_waits_even_with_lineage(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 14, 18, 0, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        assert tick._codex_workflow is not None
        probe_calls: list[str] = []

        class RecordingProbe:
            def probe(self, codex_command: str) -> object:
                probe_calls.append(codex_command)
                return MagicMock(status=CodexCapacityStatus.AVAILABLE)

        tick._codex_workflow._capacity_probe = RecordingProbe()
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        service = ReviewRetryService(tick.store, tick.artifacts)
        service.retry(run_id)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        assert probe_calls == []

    def test_structured_lineage_provider_message_does_not_repeat_probe(
        self,
        git_repo: Path,
        scheduler_paths: dict[str, Path],
        fake_clis: dict[str, Path],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "usage_limit")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        run_id = _submit(git_repo, scheduler_paths)
        start_run(run_id, db_path=scheduler_paths["db_path"])
        tick = _tick_service(
            git_repo,
            scheduler_paths,
            now=datetime(2026, 9, 14, 19, 0, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
            assert state.codex.capacity_evidence_source == "structured_error"
        ReviewRetryService(tick.store, tick.artifacts).retry(run_id)
        assert tick._codex_workflow is not None
        probe_calls: list[str] = []

        class RecordingProbe:
            def probe(self, codex_command: str) -> object:
                probe_calls.append(codex_command)
                return MagicMock(status=CodexCapacityStatus.AVAILABLE)

        tick._codex_workflow._capacity_probe = RecordingProbe()
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        assert probe_calls == []

    def test_repeat_provider_message_with_available_probe_enters_manual_retry(
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
            now=datetime(2026, 9, 14, 18, 5, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        service = ReviewRetryService(tick.store, tick.artifacts)
        service.retry(run_id)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "message_only_usage_limit")
        _run_until(tick, run_id, target_kind="waiting_codex_review_retry")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexReviewRetryState)

    def test_repeat_operational_failure_with_exhausted_probe_reenters_capacity_wait(
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
            now=datetime(2026, 9, 14, 18, 10, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "available")
        ReviewRetryService(tick.store, tick.artifacts).retry(run_id)
        monkeypatch.setenv("FAKE_CODEX_REVIEW_MODE", "fail")
        monkeypatch.setenv("FAKE_CODEX_BOOTSTRAP_SESSION_ID", BOOTSTRAP_ID)
        monkeypatch.setenv("FAKE_CODEX_CAPACITY", "exhausted")
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        with tick.store.begin_read() as conn:
            state, _, _ = tick.store.load_validated_snapshot(conn, run_id)
        assert isinstance(state, WaitingCodexCapacityState)
        assert state.codex.capacity_evidence_source == "post_failure_capacity_probe"
        assert state.codex.inferred_operational_failure_kind is not None


class TestCapacityRetryIdempotency:
    def test_manual_capacity_retry_is_idempotent_per_generation(
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
            now=datetime(2026, 9, 14, 18, 15, tzinfo=UTC),
        )
        _run_until(tick, run_id, target_kind="waiting_codex_capacity")
        store = SqliteSchedulerStore(scheduler_paths["db_path"])
        artifacts = ProtectedArtifactStore(scheduler_paths["artifact_root"])
        service = ReviewRetryService(store, artifacts)
        with store.begin_read() as conn:
            before = len(store.list_events_for_run(conn, run_id, limit=200, newest_first=False))
        first = service.retry(run_id)
        assert first.changed is True
        assert first.state_kind == "awaiting_codex_review"
        second = service.retry(run_id)
        assert second.idempotent_replay is True
        with store.begin_read() as conn:
            events = store.list_events_for_run(conn, run_id, limit=200, newest_first=False)
            kinds = [str(row["event_kind"]) for row in events]
        assert kinds.count("codex_capacity_retry_authorized") == 1
        assert "codex_capacity_available" not in kinds[before:]
        assert len(events) == before + 1
