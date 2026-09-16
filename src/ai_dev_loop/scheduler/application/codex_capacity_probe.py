"""Bounded Codex app-server capacity probe for scheduler waiting states."""

from __future__ import annotations

import contextlib
import json
import math
import os
import select
import signal
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.process import (
    _BoundedTextCapture,
    _read_nonblocking,
    _select_timeout,
    _set_nonblocking,
    _terminate_process_group,
    _write_nonblocking,
)
from ai_dev_loop.scheduler.application.codex_subprocess_env import sanitize_codex_subprocess_env
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_CAPACITY_PROBE_JSON_LINE_MAX_BYTES,
    CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH,
    CODEX_CAPACITY_PROBE_STDERR_MAX_BYTES,
    CODEX_CAPACITY_PROBE_STDOUT_MAX_BYTES,
    CODEX_CAPACITY_PROBE_TIMEOUT_SECONDS,
)

USED_PERCENT_MIN = 0


class CodexCapacityStatus(StrEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"


class CodexCapacityReason(StrEnum):
    RESPONSE_RECEIVED = "response_received"
    EXHAUSTED_WINDOW = "exhausted_window"
    REACHED_STATE_MARKER = "reached_state_marker"
    TIMEOUT = "timeout"
    PREMATURE_EOF = "premature_eof"
    PROTOCOL_ERROR = "protocol_error"
    MALFORMED_RESPONSE = "malformed_response"
    PROCESS_FAILURE = "process_failure"
    CLEANUP_FAILURE = "cleanup_failure"
    OUTPUT_LIMIT = "output_limit"


@dataclass(frozen=True)
class CodexCapacityObservation:
    status: CodexCapacityStatus
    reason: CodexCapacityReason | None = None


class CodexCapacityProbePort(Protocol):
    def probe(self, codex_command: str) -> CodexCapacityObservation:
        """Return a typed capacity observation without leaking raw protocol data."""


def _jsonrpc_header_ok(payload: dict[str, Any]) -> bool:
    if "jsonrpc" not in payload:
        return True
    return bool(payload["jsonrpc"] == "2.0")


def _object_nesting_depth(value: object, *, current: int = 1) -> int:
    if current > CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH:
        return current
    if isinstance(value, dict):
        if not value:
            return current
        return max(_object_nesting_depth(nested, current=current + 1) for nested in value.values())
    if isinstance(value, list):
        if not value:
            return current
        return max(_object_nesting_depth(item, current=current + 1) for item in value)
    return current


def _loads_probe_line(stripped: str) -> dict[str, Any] | None:
    if len(stripped.encode("utf-8")) > CODEX_CAPACITY_PROBE_JSON_LINE_MAX_BYTES:
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError, ValueError, OverflowError):
        return None
    if not isinstance(payload, dict):
        return None
    if _object_nesting_depth(payload) > CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH:
        return None
    return payload


def _parse_used_percent(value: object) -> float | None:
    """Return a finite used percent, or None when the value is invalid."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < USED_PERCENT_MIN:
            return None
        try:
            return float(value)
        except OverflowError:
            return None
    if isinstance(value, float):
        if not math.isfinite(value) or value < USED_PERCENT_MIN:
            return None
        return value
    return None


def _reached_marker_status(record: dict[str, Any]) -> tuple[bool | None, bool]:
    """Return (exhausted, marker_present).

    exhausted: True when exhausted, False when explicitly absent, None when malformed.
    marker_present: True when rateLimitReachedType key exists (including null).
    """

    if "rateLimitReachedType" not in record:
        return False, False
    reached = record.get("rateLimitReachedType")
    if reached is None:
        return False, True
    if isinstance(reached, str) and reached.strip():
        return True, True
    return None, True


def _window_is_exhausted(window: object) -> bool | None:
    if window is None:
        return None
    if not isinstance(window, dict):
        return None
    used = _parse_used_percent(window.get("usedPercent"))
    if used is None:
        return None
    return used >= 100.0


def _record_capacity_status(
    record: object,
    *,
    require_reached_marker: bool = False,
) -> CodexCapacityStatus | None:
    """Return record status, or None when the record cannot be interpreted."""

    if not isinstance(record, dict):
        return None
    reached, marker_present = _reached_marker_status(record)
    if reached is True:
        return CodexCapacityStatus.EXHAUSTED
    malformed_marker = reached is None
    if not marker_present:
        if require_reached_marker:
            return None
        marker_present = True
        reached = False
    window_results: list[bool] = []
    saw_invalid_window = False
    for key in ("primary", "secondary"):
        if key not in record:
            continue
        window = record.get(key)
        if window is None:
            continue
        exhausted = _window_is_exhausted(window)
        if exhausted is None:
            saw_invalid_window = True
            continue
        window_results.append(exhausted)
    if window_results and any(window_results):
        return CodexCapacityStatus.EXHAUSTED
    if malformed_marker or saw_invalid_window:
        return None
    if window_results and all(not exhausted for exhausted in window_results):
        return CodexCapacityStatus.AVAILABLE
    return None


@dataclass(frozen=True)
class _LimitRecordScan:
    records: list[dict[str, Any]]
    saw_invalid_entry: bool = False
    require_reached_marker: bool = False


def _scan_limit_records(payload: dict[str, Any]) -> _LimitRecordScan | None:
    if "rateLimitsByLimitId" in payload:
        by_limit_id = payload["rateLimitsByLimitId"]
        if by_limit_id is None or not isinstance(by_limit_id, dict) or not by_limit_id:
            return None
        records: list[dict[str, Any]] = []
        saw_invalid_entry = False
        for record in by_limit_id.values():
            if isinstance(record, dict):
                records.append(record)
            else:
                saw_invalid_entry = True
        return _LimitRecordScan(
            records=records,
            saw_invalid_entry=saw_invalid_entry,
            require_reached_marker=True,
        )
    legacy = payload.get("rateLimits")
    if legacy is not None:
        if not isinstance(legacy, dict):
            return None
        return _LimitRecordScan(records=[legacy], saw_invalid_entry=False)
    return None


def capacity_from_rate_limits_payload(payload: dict[str, Any]) -> CodexCapacityStatus | None:
    scan = _scan_limit_records(payload)
    if scan is None:
        return None
    saw_invalid = scan.saw_invalid_entry
    saw_available = False
    for record in scan.records:
        status = _record_capacity_status(
            record,
            require_reached_marker=scan.require_reached_marker,
        )
        if status == CodexCapacityStatus.EXHAUSTED:
            return CodexCapacityStatus.EXHAUSTED
        if status is None:
            saw_invalid = True
            continue
        if status == CodexCapacityStatus.AVAILABLE:
            saw_available = True
    if saw_invalid:
        return None
    if saw_available:
        return CodexCapacityStatus.AVAILABLE
    return None


def _is_well_formed_notification(payload: dict[str, Any]) -> bool:
    if not _jsonrpc_header_ok(payload):
        return False
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return False
    if "id" in payload:
        return False
    return not ("result" in payload or "error" in payload)


def _validate_initialize_result(result: object) -> bool:
    return isinstance(result, dict)


def _jsonrpc_response_id_matches(request_id: object, expected_id: int) -> bool:
    return type(request_id) is int and request_id == expected_id


def _has_jsonrpc_request_members(payload: dict[str, Any]) -> bool:
    return "method" in payload or "params" in payload


def _parse_result_response(
    payload: dict[str, Any],
    *,
    expected_id: int,
) -> dict[str, Any] | None:
    if not _jsonrpc_header_ok(payload):
        return None
    if _has_jsonrpc_request_members(payload):
        return None
    if "id" not in payload:
        return None
    if not _jsonrpc_response_id_matches(payload["id"], expected_id):
        return None
    if "error" in payload:
        return None
    if "result" not in payload:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return result


class _ProbeExchangePhase(StrEnum):
    AWAITING_INIT = "awaiting_init"
    AWAITING_LIMITS = "awaiting_limits"
    DRAINING = "draining"
    COMPLETE = "complete"
    PROTOCOL_ERROR = "protocol_error"


@dataclass
class _ProbeExchangeTracker:
    """Incremental, ordered App Server response parser for the capacity probe."""

    init_id: int
    limits_id: int
    max_line_bytes: int = CODEX_CAPACITY_PROBE_JSON_LINE_MAX_BYTES
    phase: _ProbeExchangePhase = _ProbeExchangePhase.AWAITING_INIT
    line_buffer: bytearray = field(default_factory=bytearray)
    limits_result: dict[str, Any] | None = None
    init_seen: bool = False
    limits_seen: bool = False
    outbound_complete: bool = False
    output_limit_exceeded: bool = False

    @property
    def protocol_error(self) -> bool:
        return self.phase == _ProbeExchangePhase.PROTOCOL_ERROR

    @property
    def complete(self) -> bool:
        return self.phase in {_ProbeExchangePhase.COMPLETE, _ProbeExchangePhase.DRAINING}

    def _mark_protocol_error(self) -> None:
        self.phase = _ProbeExchangePhase.PROTOCOL_ERROR

    def _mark_output_limit(self) -> None:
        self.output_limit_exceeded = True

    def feed_bytes(self, piece: bytes) -> bool:
        """Return True when incremental line-buffer capacity is exceeded."""

        if not piece or self.protocol_error or self.output_limit_exceeded:
            return self.output_limit_exceeded
        if len(self.line_buffer) + len(piece) > self.max_line_bytes:
            self._mark_output_limit()
            return True
        self.line_buffer.extend(piece)
        while True:
            newline = self.line_buffer.find(b"\n")
            if newline < 0:
                if len(self.line_buffer) > self.max_line_bytes:
                    self._mark_output_limit()
                    return True
                break
            line = self.line_buffer[:newline].decode("utf-8", errors="replace").strip()
            del self.line_buffer[: newline + 1]
            if line:
                self._consume_line(line)
                if self.protocol_error or self.output_limit_exceeded:
                    return self.output_limit_exceeded
        return self.output_limit_exceeded

    def _consume_line(self, stripped: str) -> None:
        if self.protocol_error or self.output_limit_exceeded:
            return
        if len(stripped.encode("utf-8")) > self.max_line_bytes:
            self._mark_output_limit()
            return
        payload = _loads_probe_line(stripped)
        if payload is None:
            self._mark_protocol_error()
            return
        if not _jsonrpc_header_ok(payload):
            self._mark_protocol_error()
            return
        if _is_well_formed_notification(payload):
            return
        if "id" not in payload:
            self._mark_protocol_error()
            return
        request_id = payload["id"]
        if self.phase in {_ProbeExchangePhase.DRAINING, _ProbeExchangePhase.COMPLETE}:
            self._mark_protocol_error()
            return
        if _jsonrpc_response_id_matches(request_id, self.init_id):
            if self.init_seen or self.phase != _ProbeExchangePhase.AWAITING_INIT:
                self._mark_protocol_error()
                return
            parsed = _parse_result_response(payload, expected_id=self.init_id)
            if parsed is None or not _validate_initialize_result(parsed):
                self._mark_protocol_error()
                return
            self.init_seen = True
            self.phase = _ProbeExchangePhase.AWAITING_LIMITS
            return
        if _jsonrpc_response_id_matches(request_id, self.limits_id):
            if not self.outbound_complete or not self.init_seen or self.limits_seen:
                self._mark_protocol_error()
                return
            parsed = _parse_result_response(payload, expected_id=self.limits_id)
            if parsed is None:
                self._mark_protocol_error()
                return
            self.limits_seen = True
            self.limits_result = parsed
            self.phase = _ProbeExchangePhase.DRAINING
            return
        self._mark_protocol_error()

    def mark_draining_complete(self) -> None:
        if self.phase == _ProbeExchangePhase.DRAINING:
            self.phase = _ProbeExchangePhase.COMPLETE


def _flush_probe_line_buffer(tracker: _ProbeExchangeTracker) -> None:
    if not tracker.line_buffer:
        return
    if len(tracker.line_buffer) > tracker.max_line_bytes:
        tracker._mark_output_limit()
        tracker.line_buffer.clear()
        return
    remainder = tracker.line_buffer.decode("utf-8", errors="replace").strip()
    tracker.line_buffer.clear()
    if remainder:
        tracker._consume_line(remainder)


def parse_capacity_probe_stdout(
    stdout: str,
    *,
    init_id: int,
    limits_id: int,
    outbound_complete: bool = False,
) -> dict[str, Any] | None:
    """Parse a complete app-server probe exchange fail-closed."""

    tracker = _ProbeExchangeTracker(init_id=init_id, limits_id=limits_id)
    tracker.outbound_complete = outbound_complete
    tracker.feed_bytes(stdout.encode("utf-8"))
    _flush_probe_line_buffer(tracker)
    if tracker.output_limit_exceeded or tracker.protocol_error:
        return None
    if not tracker.complete or tracker.limits_result is None:
        return None
    tracker.mark_draining_complete()
    if tracker.protocol_error:
        return None
    return tracker.limits_result


def build_capacity_probe_lines(*, init_id: int, limits_id: int) -> tuple[bytes, bytes, bytes]:
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "ai_dev_loop", "version": "0.1"},
            },
        },
        separators=(",", ":"),
    )
    initialized = json.dumps(
        {"jsonrpc": "2.0", "method": "initialized"},
        separators=(",", ":"),
    )
    rate_limits = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": limits_id,
            "method": "account/rateLimits/read",
            "params": {},
        },
        separators=(",", ":"),
    )
    return (
        (initialize + "\n").encode("utf-8"),
        (initialized + "\n").encode("utf-8"),
        (rate_limits + "\n").encode("utf-8"),
    )


def build_capacity_probe_stdin(*, init_id: int, limits_id: int) -> str:
    init_line, initialized_line, limits_line = build_capacity_probe_lines(
        init_id=init_id,
        limits_id=limits_id,
    )
    return (init_line + initialized_line + limits_line).decode("utf-8")


@dataclass(frozen=True)
class _InteractiveProbeResult:
    stdout: str
    stderr: str
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    output_limit: bool
    returncode: int
    premature_eof: bool
    protocol_error: bool
    cleanup_failed: bool = False


def _close_pipe(pipe: Any) -> None:
    if pipe is not None:
        with contextlib.suppress(OSError):
            pipe.close()


def _process_group_is_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def _signal_process_group(pgid: int, *, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, OSError):
        os.killpg(pgid, sig)


def _probe_session_pgid(proc: subprocess.Popen[Any]) -> int:
    """Return the PGID for a probe child launched with ``start_new_session=True``."""

    return proc.pid


def _direct_child_is_alive(proc: subprocess.Popen[Any]) -> bool:
    return proc.poll() is None


def _probe_cleanup_verified(
    proc: subprocess.Popen[Any],
    *,
    pgid: int | None,
) -> bool:
    if pgid is not None and _process_group_is_alive(pgid):
        return False
    return not _direct_child_is_alive(proc)


def _reap_probe_process(
    proc: subprocess.Popen[Any],
    *,
    deadline: float,
    pgid: int | None = None,
) -> bool:
    reap_deadline = time.monotonic() + 1.0

    if pgid is not None:
        _signal_process_group(pgid, sig=signal.SIGTERM)
    elif proc.poll() is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            _terminate_process_group(proc)

    initial_wait = min(deadline, reap_deadline) - time.monotonic()
    if initial_wait > 0 and proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=initial_wait)

    while time.monotonic() < reap_deadline:
        group_alive = pgid is not None and _process_group_is_alive(pgid)
        child_alive = _direct_child_is_alive(proc)
        if not group_alive and not child_alive:
            break
        if pgid is not None and group_alive:
            _signal_process_group(pgid, sig=signal.SIGKILL)
        elif child_alive:
            with contextlib.suppress(ProcessLookupError, OSError):
                _terminate_process_group(proc)
        if proc.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=min(0.05, max(0.0, reap_deadline - time.monotonic())))
        time.sleep(0.05)

    _close_pipe(proc.stdin)
    _close_pipe(proc.stdout)
    _close_pipe(proc.stderr)
    return _probe_cleanup_verified(proc, pgid=pgid)


def _exchange_finished(
    proc: subprocess.Popen[Any],
    *,
    stdout_eof: bool,
    stderr_eof: bool,
) -> bool:
    return proc.poll() is not None and stdout_eof and stderr_eof


def _finalize_exchange_on_process_exit(
    tracker: _ProbeExchangeTracker,
    *,
    output_limit: bool,
) -> tuple[bool, bool]:
    """Flush trailing bytes and classify premature EOF vs completed drain."""

    if output_limit or tracker.output_limit_exceeded:
        return False, False
    _flush_probe_line_buffer(tracker)
    if tracker.protocol_error:
        return True, False
    if tracker.limits_result is not None:
        tracker.mark_draining_complete()
        return False, False
    return False, True


def _run_interactive_capacity_probe(
    args: list[str],
    *,
    init_id: int,
    limits_id: int,
    timeout_seconds: float,
    env: Mapping[str, str],
    max_stdout_bytes: int,
    max_stderr_bytes: int,
) -> _InteractiveProbeResult:
    """Exchange initialize/initialized/limits over a persistent stdio transport."""

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_capture = _BoundedTextCapture(stdout_chunks, None, max_stdout_bytes)
    stderr_capture = _BoundedTextCapture(stderr_chunks, None, max_stderr_bytes)
    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    premature_eof = False
    limit_terminate = False
    protocol_error = False
    output_limit = False
    cleanup_failed = False

    proc: subprocess.Popen[Any] | None = None
    proc_pgid: int | None = None
    tracker = _ProbeExchangeTracker(init_id=init_id, limits_id=limits_id)
    init_line, initialized_line, limits_line = build_capacity_probe_lines(
        init_id=init_id,
        limits_id=limits_id,
    )
    outbound = init_line
    stdin_offset = 0
    stdin_closed = False
    stdout_eof = False
    stderr_eof = False
    draining = False
    follow_up_sent = False
    tracker_feed_enabled = True

    try:
        proc = subprocess.Popen(
            args,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            start_new_session=True,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc_pgid = _probe_session_pgid(proc)

        stdin_fd = proc.stdin.fileno()
        stdout_fd = proc.stdout.fileno()
        stderr_fd = proc.stderr.fileno()
        _set_nonblocking(stdin_fd)
        _set_nonblocking(stdout_fd)
        _set_nonblocking(stderr_fd)

        while True:
            if limit_terminate or protocol_error or output_limit:
                break

            if _exchange_finished(proc, stdout_eof=stdout_eof, stderr_eof=stderr_eof):
                protocol_hit, eof_hit = _finalize_exchange_on_process_exit(
                    tracker,
                    output_limit=output_limit,
                )
                if protocol_hit:
                    protocol_error = True
                elif eof_hit:
                    premature_eof = True
                break

            if time.monotonic() >= deadline:
                timed_out = True
                break

            if draining and proc.poll() is not None and stdout_eof and stderr_eof:
                _flush_probe_line_buffer(tracker)
                if tracker.protocol_error:
                    protocol_error = True
                else:
                    tracker.mark_draining_complete()
                break

            if tracker.init_seen and not follow_up_sent:
                outbound = initialized_line + limits_line
                stdin_offset = 0
                follow_up_sent = True

            if follow_up_sent and stdin_offset >= len(outbound):
                tracker.outbound_complete = True

            if tracker.phase == _ProbeExchangePhase.DRAINING and not draining:
                _close_pipe(proc.stdin)
                stdin_closed = True
                draining = True

            if not stdin_closed and not draining and stdin_offset < len(outbound):
                stdin_offset, pipe_closed = _write_nonblocking(stdin_fd, outbound, stdin_offset)
                if pipe_closed:
                    stdin_closed = True
                if follow_up_sent and stdin_offset >= len(outbound):
                    tracker.outbound_complete = True

            read_fds: list[int] = []
            if not stdout_eof:
                read_fds.append(stdout_fd)
            if not stderr_eof:
                read_fds.append(stderr_fd)

            if read_fds:
                ready, _, _ = select.select(read_fds, [], read_fds, _select_timeout(deadline))
                if not ready:
                    continue
                for fd in ready:
                    if fd == stdout_fd and not stdout_eof:
                        piece, stdout_eof = _read_nonblocking(stdout_fd)
                        stdout_capture.append(piece, drain_after_limit=True)
                        if stdout_capture.truncated:
                            limit_terminate = True
                            output_limit = True
                            tracker_feed_enabled = False
                            break
                        if tracker_feed_enabled and piece:
                            if tracker.feed_bytes(piece):
                                limit_terminate = True
                                output_limit = True
                                tracker_feed_enabled = False
                                break
                            if tracker.protocol_error:
                                protocol_error = True
                                break
                    elif fd == stderr_fd and not stderr_eof:
                        piece, stderr_eof = _read_nonblocking(stderr_fd)
                        stderr_capture.append(piece, drain_after_limit=True)
                        if stderr_capture.truncated:
                            limit_terminate = True
                            output_limit = True
                            break

            if tracker.protocol_error:
                protocol_error = True
                break
            if limit_terminate:
                break

        if stdout_capture.finalize(drain_after_limit=True):
            limit_terminate = True
            output_limit = True
        if stderr_capture.finalize(drain_after_limit=True):
            limit_terminate = True
            output_limit = True
        if stdout_capture.truncated or stderr_capture.truncated:
            output_limit = True
        if tracker.output_limit_exceeded:
            output_limit = True
        if tracker.protocol_error and not output_limit:
            protocol_error = True
    except (AiDevLoopError, OSError, ValueError, RecursionError, OverflowError):
        protocol_error = True
    finally:
        if proc is not None:
            cleanup_failed = not _reap_probe_process(proc, deadline=deadline, pgid=proc_pgid)

    returncode = 1
    if proc is not None and proc.returncode is not None:
        returncode = proc.returncode
    elif timed_out:
        returncode = 124

    return _InteractiveProbeResult(
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        timed_out=timed_out,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
        output_limit=output_limit,
        returncode=returncode,
        premature_eof=premature_eof,
        protocol_error=protocol_error,
        cleanup_failed=cleanup_failed,
    )


class CodexAppServerCapacityProbe:
    """Concrete bounded probe over ``codex app-server --stdio`` JSON-RPC."""

    def __init__(
        self,
        *,
        timeout_seconds: float = CODEX_CAPACITY_PROBE_TIMEOUT_SECONDS,
        max_stdout_bytes: int = CODEX_CAPACITY_PROBE_STDOUT_MAX_BYTES,
        max_stderr_bytes: int = CODEX_CAPACITY_PROBE_STDERR_MAX_BYTES,
        env_factory: Callable[[], Mapping[str, str]] | None = None,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._max_stdout_bytes = max_stdout_bytes
        self._max_stderr_bytes = max_stderr_bytes
        self._env_factory = env_factory or sanitize_codex_subprocess_env

    def probe(self, codex_command: str) -> CodexCapacityObservation:
        init_id = 1
        limits_id = 2
        args = [codex_command, "app-server", "--stdio"]
        try:
            env = dict(self._env_factory())
            result = _run_interactive_capacity_probe(
                args,
                init_id=init_id,
                limits_id=limits_id,
                timeout_seconds=self._timeout_seconds,
                env=env,
                max_stdout_bytes=self._max_stdout_bytes,
                max_stderr_bytes=self._max_stderr_bytes,
            )
        except (AiDevLoopError, OSError, ValueError, RecursionError, OverflowError):
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.PROCESS_FAILURE,
            )

        if result.cleanup_failed:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.CLEANUP_FAILURE,
            )
        if result.output_limit:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.OUTPUT_LIMIT,
            )
        if result.protocol_error:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.PROTOCOL_ERROR,
            )
        if result.timed_out:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.TIMEOUT,
            )
        if result.premature_eof:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.PREMATURE_EOF,
            )
        if result.returncode != 0:
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.PROCESS_FAILURE,
            )

        try:
            limits_result = parse_capacity_probe_stdout(
                result.stdout,
                init_id=init_id,
                limits_id=limits_id,
                outbound_complete=True,
            )
            if limits_result is None:
                return CodexCapacityObservation(
                    status=CodexCapacityStatus.UNAVAILABLE,
                    reason=CodexCapacityReason.MALFORMED_RESPONSE,
                )
            status = capacity_from_rate_limits_payload(limits_result)
            if status is None:
                return CodexCapacityObservation(
                    status=CodexCapacityStatus.UNAVAILABLE,
                    reason=CodexCapacityReason.MALFORMED_RESPONSE,
                )
            reason = CodexCapacityReason.RESPONSE_RECEIVED
            if status == CodexCapacityStatus.EXHAUSTED:
                reason = CodexCapacityReason.EXHAUSTED_WINDOW
            return CodexCapacityObservation(status=status, reason=reason)
        except (RecursionError, ValueError, OverflowError):
            return CodexCapacityObservation(
                status=CodexCapacityStatus.UNAVAILABLE,
                reason=CodexCapacityReason.PROTOCOL_ERROR,
            )


def default_capacity_probe() -> CodexCapacityProbePort:
    return CodexAppServerCapacityProbe()
