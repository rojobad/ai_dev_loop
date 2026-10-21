"""Versioned safe wait evidence for one Cursor SDK send.

Schema 1 records a stage name and a UTC timestamp. It is diagnostic evidence,
not scheduler authority, and it never describes model progress. A native
``running`` label, a CPU sample, or missing text is ignored.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE

PROGRESS_SCHEMA_VERSION = 1
SEND_AUTHORIZED = "send_authorized"
NATIVE_HANDLE_BOUND = "native_handle_bound"
TERMINAL_OBSERVED = "terminal_observed"
INTERRUPTED = "interrupted"
WAITING_FOR_INITIAL_RESPONSE = "waiting_for_initial_sdk_response"
UNKNOWN = "unknown"

_STAGE_ORDER = (SEND_AUTHORIZED, NATIVE_HANDLE_BOUND, TERMINAL_OBSERVED)
_APPENDABLE = frozenset(_STAGE_ORDER)
_SEALABLE_PREFIXES = {(): 0, (SEND_AUTHORIZED,): 1, (SEND_AUTHORIZED, NATIVE_HANDLE_BOUND): 2}
_ATTEMPT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_IGNORED_EVIDENCE = frozenset(
    {
        "native_status",
        "status",
        "cpu",
        "cpu_percent",
        "text",
        "prompt",
        "agent_id",
        "native_run_id",
        "stdout",
        "stderr",
    }
)


@dataclass(frozen=True)
class SdkWaitInspection:
    """Safe projection of one wait-progress file."""

    stage: str
    elapsed_seconds: str
    waiting_for_initial_response: str


UNKNOWN_WAIT = SdkWaitInspection(UNKNOWN, UNKNOWN, UNKNOWN)


def cursor_sdk_wait_progress_is_diagnostic(inspection: SdkWaitInspection) -> bool:
    """True when the projection does not claim a stall or model progress."""

    rendered = format_sdk_wait_log(inspection)
    return "stall" not in rendered and "model_progress" not in rendered and "cpu" not in rendered


def format_sdk_wait_log(inspection: SdkWaitInspection) -> str:
    """One safe log line. It names the evidence stage and elapsed wait only."""

    return (
        f"sdk_wait stage={inspection.stage} "
        f"waiting_for_initial_sdk_response={inspection.waiting_for_initial_response} "
        f"elapsed_seconds={inspection.elapsed_seconds}"
    )


def append_sdk_wait_progress(
    path: Path,
    *,
    kind: str,
    attempt_id: str,
    recorded_at: datetime | None = None,
) -> None:
    """Append one stage after the previous prefix and fsync it."""

    if kind not in _APPENDABLE:
        raise ValueError("Cursor SDK wait progress kind is invalid")
    if not _ATTEMPT_ID.fullmatch(attempt_id):
        raise ValueError("Cursor SDK wait progress attempt id is invalid")
    existing = _load_records(path)
    if existing is None:
        raise ValueError("Cursor SDK wait progress cannot be extended")
    kinds = tuple(str(item.get("kind", "")) for item in existing)
    expected = _STAGE_ORDER[: len(kinds)]
    if kind in kinds and kinds == expected:
        return
    if kinds != expected or len(kinds) >= len(_STAGE_ORDER) or _STAGE_ORDER[len(kinds)] != kind:
        raise ValueError("Cursor SDK wait progress ordering is invalid")
    if existing and str(existing[0].get("attempt_id", "")) != attempt_id:
        raise ValueError("Cursor SDK wait progress attempt binding is invalid")
    moment = recorded_at or datetime.now(UTC)
    if moment.tzinfo is None:
        raise ValueError("Cursor SDK wait progress timestamp must be timezone-aware")
    _append_record(
        path,
        {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "kind": kind,
            "attempt_id": attempt_id,
            "recorded_at": moment.astimezone(UTC).isoformat(),
        },
    )


def seal_interrupted_sdk_wait_progress(path: Path) -> bool:
    """Seal every interrupted prefix without inventing a later stage.

    A missing file is already complete: historical attempts have no wait
    evidence. A symlink or corrupt file fails closed.
    """

    if not path.exists():
        return True
    if path.is_symlink() or not path.is_file():
        return False
    records = _load_records(path)
    if records is None:
        return False
    kinds = tuple(str(item.get("kind", "")) for item in records)
    if not kinds or kinds[-1] in {TERMINAL_OBSERVED, INTERRUPTED}:
        return kinds in _complete_sequences(include_empty=True)
    if kinds not in _SEALABLE_PREFIXES:
        return False
    attempt_id = str(records[-1].get("attempt_id", "")) if records else ""
    if records and not _ATTEMPT_ID.fullmatch(attempt_id):
        return False
    _append_record(
        path,
        {
            "schema_version": PROGRESS_SCHEMA_VERSION,
            "kind": INTERRUPTED,
            "attempt_id": attempt_id,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )
    return True


def inspect_sdk_wait(
    path: Path,
    *,
    now: datetime | None = None,
) -> SdkWaitInspection:
    """Project durable stages. Missing or unusable evidence is ``unknown``."""

    records = _load_records(path)
    if not records:
        return UNKNOWN_WAIT
    kinds = tuple(str(item.get("kind", "")) for item in records)
    if kinds not in _complete_sequences(include_empty=False) and kinds not in _SEALABLE_PREFIXES:
        return UNKNOWN_WAIT
    by_kind = {str(item.get("kind", "")): item for item in records}
    if SEND_AUTHORIZED not in by_kind:
        return UNKNOWN_WAIT
    if NATIVE_HANDLE_BOUND in by_kind:
        stage = TERMINAL_OBSERVED if TERMINAL_OBSERVED in by_kind else NATIVE_HANDLE_BOUND
        waiting = "false"
        elapsed = _elapsed_between(
            by_kind[SEND_AUTHORIZED],
            by_kind[NATIVE_HANDLE_BOUND],
        )
    elif INTERRUPTED in by_kind:
        # Stopped before a native handle. Do not keep an open wait, and do not
        # invent a handle or a terminal observation. Elapsed time is the durable
        # gap from authorization to the seal, or unknown when that gap is not
        # established.
        stage = INTERRUPTED
        waiting = "false"
        elapsed = _elapsed_between(by_kind[SEND_AUTHORIZED], by_kind[INTERRUPTED])
    else:
        stage = WAITING_FOR_INITIAL_RESPONSE
        waiting = "true"
        elapsed = _elapsed_until(by_kind[SEND_AUTHORIZED], now or datetime.now(UTC))
    return SdkWaitInspection(stage, elapsed, waiting)


def _complete_sequences(*, include_empty: bool) -> set[tuple[str, ...]]:
    sequences = {
        (SEND_AUTHORIZED, INTERRUPTED),
        (SEND_AUTHORIZED, NATIVE_HANDLE_BOUND, INTERRUPTED),
        (SEND_AUTHORIZED, NATIVE_HANDLE_BOUND, TERMINAL_OBSERVED),
    }
    if include_empty:
        sequences.add(())
        sequences.add((INTERRUPTED,))
    return sequences


def _elapsed_between(start: dict[str, object], end: dict[str, object]) -> str:
    started = _timestamp(start)
    finished = _timestamp(end)
    if started is None or finished is None or finished < started:
        return UNKNOWN
    return f"{(finished - started).total_seconds():.3f}"


def _elapsed_until(start: dict[str, object], now: datetime) -> str:
    started = _timestamp(start)
    if started is None or now.tzinfo is None or now < started:
        return UNKNOWN
    return f"{(now - started).total_seconds():.3f}"


def _timestamp(record: dict[str, object]) -> datetime | None:
    raw = record.get("recorded_at")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _load_records(path: Path) -> list[dict[str, object]] | None:
    if not path.exists():
        return []
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    records: list[dict[str, object]] = []
    for line in text.splitlines():
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(value, dict) or not _record_is_safe(value):
            return None
        records.append(value)
    versions = {item.get("schema_version") for item in records}
    if versions not in (set(), {PROGRESS_SCHEMA_VERSION}):
        return None
    attempt_ids = {str(item.get("attempt_id", "")) for item in records}
    if len(attempt_ids) > 1:
        return None
    return records


def _record_is_safe(record: dict[str, object]) -> bool:
    kind = record.get("kind")
    if kind not in {*_STAGE_ORDER, INTERRUPTED}:
        return False
    if record.get("schema_version") != PROGRESS_SCHEMA_VERSION:
        return False
    attempt_id = record.get("attempt_id")
    if kind != INTERRUPTED and (
        not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id)
    ):
        return False
    if (
        kind == INTERRUPTED
        and attempt_id not in {"", None}
        and (not isinstance(attempt_id, str) or not _ATTEMPT_ID.fullmatch(attempt_id))
    ):
        return False
    if _timestamp(record) is None:
        return False
    # Extra keys are not rendered. Reject only values that cannot be ignored
    # safely because they are nested containers holding arbitrary text.
    for key, value in record.items():
        if key in {"schema_version", "kind", "attempt_id", "recorded_at"}:
            continue
        if key in _IGNORED_EVIDENCE:
            continue
        if isinstance(value, (dict, list)):
            return False
    return True


def _append_record(path: Path, record: dict[str, object]) -> None:
    if path.is_symlink():
        raise ValueError("Cursor SDK wait progress must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, SENSITIVE_FILE_MODE)
    try:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        pending = memoryview(payload)
        while pending:
            written = os.write(descriptor, pending)
            if written < 1:
                raise OSError("Cursor SDK wait progress append made no progress")
            pending = pending[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
