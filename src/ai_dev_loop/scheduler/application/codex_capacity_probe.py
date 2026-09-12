"""Bounded Codex app-server capacity probe for scheduler waiting states."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from ai_dev_loop.errors import AiDevLoopError
from ai_dev_loop.process import run_process_streaming
from ai_dev_loop.scheduler.application.codex_subprocess_env import sanitize_codex_subprocess_env
from ai_dev_loop.scheduler.domain.codex_contract import (
    CODEX_CAPACITY_PROBE_JSON_LINE_MAX_BYTES,
    CODEX_CAPACITY_PROBE_JSON_MAX_NESTING_DEPTH,
    CODEX_CAPACITY_PROBE_STDERR_MAX_BYTES,
    CODEX_CAPACITY_PROBE_STDOUT_MAX_BYTES,
    CODEX_CAPACITY_PROBE_TIMEOUT_SECONDS,
)

USED_PERCENT_MIN = 0
USED_PERCENT_MAX = 100


class CodexCapacityStatus(StrEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CodexCapacityObservation:
    status: CodexCapacityStatus


class CodexCapacityProbePort(Protocol):
    def probe(self, codex_command: str) -> CodexCapacityObservation:
        """Return a typed capacity observation without leaking raw protocol data."""


def _jsonrpc_header_ok(payload: dict[str, Any]) -> bool:
    jsonrpc = payload.get("jsonrpc")
    if jsonrpc is None:
        return True
    return bool(jsonrpc == "2.0")


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


def _finite_percent(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < USED_PERCENT_MIN or value > USED_PERCENT_MAX:
            return None
        return float(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value < USED_PERCENT_MIN or value > USED_PERCENT_MAX:
            return None
        return value
    return None


def _window_has_capacity(window: object) -> bool | None:
    if window is None:
        return None
    if not isinstance(window, dict):
        return None
    used = _finite_percent(window.get("usedPercent"))
    if used is None:
        return None
    return (100.0 - used) > 0.0


def _limit_record_has_capacity(record: object) -> bool | None:
    if not isinstance(record, dict):
        return None
    results: list[bool] = []
    for key in ("primary", "secondary"):
        if key not in record:
            continue
        window = record.get(key)
        if window is None:
            continue
        window_capacity = _window_has_capacity(window)
        if window_capacity is None:
            return None
        results.append(window_capacity)
    if not results:
        return None
    return all(results)


def _iter_limit_records(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    by_limit_id = payload.get("rateLimitsByLimitId")
    if by_limit_id is not None:
        if not isinstance(by_limit_id, dict) or not by_limit_id:
            return None
        records: list[dict[str, Any]] = []
        for record in by_limit_id.values():
            if not isinstance(record, dict):
                return None
            records.append(record)
        return records
    legacy = payload.get("rateLimits")
    if legacy is not None:
        if not isinstance(legacy, dict):
            return None
        return [legacy]
    return None


def capacity_from_rate_limits_payload(payload: dict[str, Any]) -> CodexCapacityStatus | None:
    records = _iter_limit_records(payload)
    if records is None:
        return None
    record_capacities: list[bool] = []
    for record in records:
        record_capacity = _limit_record_has_capacity(record)
        if record_capacity is None:
            return None
        record_capacities.append(record_capacity)
    if not record_capacities:
        return None
    if all(record_capacities):
        return CodexCapacityStatus.AVAILABLE
    return CodexCapacityStatus.EXHAUSTED


def _is_well_formed_notification(payload: dict[str, Any]) -> bool:
    if not _jsonrpc_header_ok(payload):
        return False
    method = payload.get("method")
    if not isinstance(method, str) or not method:
        return False
    if payload.get("id") is not None:
        return False
    return not ("result" in payload or "error" in payload)


def _validate_initialize_result(result: object) -> bool:
    return isinstance(result, dict)


def _parse_result_response(
    payload: dict[str, Any],
    *,
    expected_id: int,
) -> dict[str, Any] | None:
    if not _jsonrpc_header_ok(payload):
        return None
    if payload.get("id") != expected_id:
        return None
    if "error" in payload:
        return None
    if "result" not in payload:
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    return result


def parse_capacity_probe_stdout(
    stdout: str,
    *,
    init_id: int,
    limits_id: int,
) -> dict[str, Any] | None:
    """Parse a complete app-server probe exchange fail-closed."""

    init_result: dict[str, Any] | None = None
    limits_result: dict[str, Any] | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        payload = _loads_probe_line(stripped)
        if payload is None:
            return None
        if _is_well_formed_notification(payload):
            continue
        request_id = payload.get("id")
        if request_id == init_id:
            if init_result is not None:
                return None
            parsed = _parse_result_response(payload, expected_id=init_id)
            if parsed is None or not _validate_initialize_result(parsed):
                return None
            init_result = parsed
            continue
        if request_id == limits_id:
            if limits_result is not None:
                return None
            parsed = _parse_result_response(payload, expected_id=limits_id)
            if parsed is None:
                return None
            limits_result = parsed
            continue
        if request_id is not None:
            return None
        return None
    if init_result is None or limits_result is None:
        return None
    return limits_result


def build_capacity_probe_stdin(*, init_id: int, limits_id: int) -> str:
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
    return "\n".join([initialize, initialized, rate_limits]) + "\n"


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
        stdin_text = build_capacity_probe_stdin(init_id=init_id, limits_id=limits_id)
        args = [codex_command, "app-server", "--stdio"]
        try:
            env = dict(self._env_factory())
            result = run_process_streaming(
                args,
                timeout=self._timeout_seconds,
                stdin_text=stdin_text,
                env=env,
                max_stdout_bytes=self._max_stdout_bytes,
                max_stderr_bytes=self._max_stderr_bytes,
                drain_after_limit=True,
            )
        except (AiDevLoopError, OSError, ValueError, RecursionError, OverflowError):
            return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)

        if result.timed_out or result.returncode != 0:
            return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)
        stdout = result.stdout
        if result.stdout_truncated or result.stderr_truncated:
            return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)
        if len(stdout.encode("utf-8")) > self._max_stdout_bytes:
            return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)

        try:
            limits_result = parse_capacity_probe_stdout(
                stdout,
                init_id=init_id,
                limits_id=limits_id,
            )
            if limits_result is None:
                return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)
            status = capacity_from_rate_limits_payload(limits_result)
            if status is None:
                return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)
            return CodexCapacityObservation(status=status)
        except (RecursionError, ValueError, OverflowError):
            return CodexCapacityObservation(status=CodexCapacityStatus.UNAVAILABLE)


def default_capacity_probe() -> CodexCapacityProbePort:
    return CodexAppServerCapacityProbe()
