"""Durable write-ahead identity journal for Cursor SDK dispatch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.state import sha256_file

JOURNAL_SCHEMA_VERSION = 1
JOURNAL_SCHEMA_VERSION_V2 = 2
CREATE_SEQUENCE = ("create_intent", "agent_bound", "terminal")
CREATE_SEQUENCE_V2 = ("create_intent", "agent_bound", "creation_inventory", "terminal")
SEND_SEQUENCE = ("run_inventory_baseline", "send_intent", "native_run_bound", "terminal")
LEGACY_SEND_SEQUENCE = ("send_intent", "native_run_bound", "terminal")
SEND_UNCERTAIN_SEQUENCES = (
    ("run_inventory_baseline", "send_intent", "terminal"),
    ("run_inventory_baseline", "send_intent", "native_run_bound", "terminal"),
)
SEND_SUCCESS_SEQUENCES_V2 = (
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
)
SEND_UNCERTAIN_SEQUENCES_V2 = (
    ("run_inventory_baseline", "terminal"),
    ("run_inventory_baseline", "readiness_hold", "terminal"),
    ("run_inventory_baseline", "readiness_observation", "readiness_hold", "terminal"),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "native_run_bound",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_hold",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_hold",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
        "terminal",
    ),
    ("run_inventory_baseline", "readiness_observation", "terminal"),
    ("run_inventory_baseline", "readiness_clear", "terminal"),
    ("run_inventory_baseline", "readiness_clear", "readiness_hold", "terminal"),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "readiness_hold",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_hold",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_hold",
        "terminal",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "terminal",
    ),
)
_SEALABLE_SEND_PREFIXES_V2 = {
    ("run_inventory_baseline",),
    ("run_inventory_baseline", "readiness_observation"),
    ("run_inventory_baseline", "readiness_clear"),
    ("run_inventory_baseline", "readiness_observation", "creation_blocker_cancel_intent"),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "native_run_bound",
    ),
    ("run_inventory_baseline", "readiness_clear", "send_intent"),
    ("run_inventory_baseline", "readiness_clear", "send_intent", "native_run_bound"),
    ("run_inventory_baseline", "readiness_clear", "send_intent", "send_rejected"),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_observation",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_observation",
    ),
    (
        "run_inventory_baseline",
        "readiness_clear",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
    ),
    (
        "run_inventory_baseline",
        "readiness_observation",
        "creation_blocker_cancel_intent",
        "creation_blocker_cancel_confirmed",
        "send_intent",
        "send_rejected",
        "readiness_observation",
        "send_intent",
    ),
}
_TERMINAL_RECORDED_STATUSES = frozenset(
    {"finished", "cancelled", "canceled", "expired", "error", "failed"}
)


def append_sdk_journal(
    path: Path,
    *,
    kind: str,
    payload: dict[str, object],
    schema_version: int = JOURNAL_SCHEMA_VERSION,
) -> None:
    """Append and fsync one record before the caller crosses an external boundary."""

    if schema_version not in {JOURNAL_SCHEMA_VERSION, JOURNAL_SCHEMA_VERSION_V2}:
        raise ValueError("Cursor SDK journal schema version is invalid")
    if schema_version == JOURNAL_SCHEMA_VERSION_V2 and any(
        value is None for value in payload.values()
    ):
        raise ValueError("Cursor SDK journal v2 payload cannot contain null")

    missing_parents: list[Path] = []
    current = path.parent
    while not current.exists():
        missing_parents.append(current)
        current = current.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Cursor SDK journal must not be a symlink")
    created = not path.exists()
    record = {
        "schema_version": schema_version,
        "kind": kind,
        "recorded_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, SENSITIVE_FILE_MODE)
    try:
        pending = memoryview(
            (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        while pending:
            written = os.write(descriptor, pending)
            if written < 1:
                raise OSError("Cursor SDK journal append made no progress")
            pending = pending[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if created:
        durable_directories = [path.parent, *missing_parents[1:]]
        if current not in durable_directories:
            durable_directories.append(current)
        for directory in durable_directories:
            parent_descriptor = os.open(
                directory,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)


def _strict_int(record: dict[str, object], key: str, *, minimum: int, maximum: int) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"Cursor SDK journal {key} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"Cursor SDK journal {key} is outside its range")
    return value


def _run_facts(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"Cursor SDK journal {label} is invalid")
    identities: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError(f"Cursor SDK journal {label} entry is invalid")
        run_id = item.get("run_id")
        status = item.get("status")
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(status, str)
            or not status
            or any(child is None for child in item.values())
        ):
            raise ValueError(f"Cursor SDK journal {label} entry is invalid")
        identities.append(run_id)
    if len(set(identities)) != len(identities):
        raise ValueError(f"Cursor SDK journal {label} identities are not unique")
    return tuple(identities)


def _run_id_list(value: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f"Cursor SDK journal {label} is invalid")
    if len(set(value)) != len(value):
        raise ValueError(f"Cursor SDK journal {label} identities are not unique")
    return tuple(value)


def _validate_v2_record(record: dict[str, object]) -> None:
    kind = str(record.get("kind", ""))
    if kind == "creation_inventory":
        _run_facts(record.get("runs"), label="creation inventory")
        _strict_int(record, "message_count", minimum=0, maximum=1_000_000)
        _strict_int(record, "authorized_send_count", minimum=0, maximum=1_000_000)
    elif kind == "run_inventory_baseline":
        run_ids = _run_id_list(record.get("run_ids"), label="baseline")
        runs = _run_facts(record.get("runs"), label="baseline runs")
        creation_ids = _run_id_list(record.get("creation_run_ids"), label="creation runs")
        if tuple(sorted(runs)) != tuple(sorted(run_ids)) or not set(creation_ids).issubset(run_ids):
            raise ValueError("Cursor SDK journal baseline identities disagree")
        _strict_int(record, "message_count", minimum=0, maximum=1_000_000)
        _strict_int(record, "authorized_send_count", minimum=0, maximum=1_000_000)
    elif kind in {"readiness_observation", "readiness_clear", "readiness_hold"}:
        _strict_int(record, "observation_ordinal", minimum=1, maximum=3)
        _run_facts(record.get("runs"), label="readiness runs")
        _strict_int(record, "message_count", minimum=0, maximum=1_000_000)
        reason = record.get("reason")
        if not isinstance(reason, str) or not reason or len(reason) > 160:
            raise ValueError("Cursor SDK journal readiness reason is invalid")
    elif kind == "creation_blocker_cancel_intent":
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("Cursor SDK journal cancellation intent is invalid")
    elif kind == "creation_blocker_cancel_confirmed":
        run_id = record.get("run_id")
        status = record.get("status")
        if not isinstance(run_id, str) or not run_id or not isinstance(status, str):
            raise ValueError("Cursor SDK journal cancellation confirmation is invalid")
        if status.lower() not in _TERMINAL_RECORDED_STATUSES:
            raise ValueError("Cursor SDK journal cancellation is not terminal")
    elif kind == "send_intent":
        _strict_int(record, "dispatch_ordinal", minimum=1, maximum=2)
    elif kind == "send_rejected":
        _strict_int(record, "dispatch_ordinal", minimum=1, maximum=2)
        if record.get("evidence_class") not in {"typed", "structured", "message"}:
            raise ValueError("Cursor SDK journal rejection evidence is invalid")
    elif kind == "terminal":
        if record.get("outcome") not in {"success", "failed", "uncertain"}:
            raise ValueError("Cursor SDK journal terminal outcome is invalid")
    elif kind not in {"create_intent", "agent_bound", "native_run_bound"}:
        raise ValueError("Cursor SDK journal record kind is invalid")


def _validate_v2_relations(records: list[dict[str, object]]) -> None:
    ordinals = [
        _strict_int(record, "dispatch_ordinal", minimum=1, maximum=2)
        for record in records
        if record.get("kind") == "send_intent"
    ]
    if ordinals and (
        len(ordinals) != len(set(ordinals)) or ordinals != list(range(1, len(ordinals) + 1))
    ):
        raise ValueError("Cursor SDK journal dispatch ordinals are invalid")
    observations = [
        _strict_int(record, "observation_ordinal", minimum=1, maximum=3)
        for record in records
        if str(record.get("kind", "")).startswith("readiness_")
    ]
    if observations != sorted(observations) or len(observations) != len(set(observations)):
        raise ValueError("Cursor SDK journal readiness observations do not progress")
    intents = [
        str(record.get("run_id", ""))
        for record in records
        if record.get("kind") == "creation_blocker_cancel_intent"
    ]
    confirmed = [
        str(record.get("run_id", ""))
        for record in records
        if record.get("kind") == "creation_blocker_cancel_confirmed"
    ]
    if len(intents) > 1 or len(confirmed) > 1:
        raise ValueError("Cursor SDK journal cancellation is not unique")
    if confirmed and confirmed != intents:
        raise ValueError("Cursor SDK journal cancellation confirmation disagrees")
    baseline = next(
        (record for record in records if record.get("kind") == "run_inventory_baseline"),
        None,
    )
    if baseline is not None and intents:
        creation_ids = _run_id_list(baseline.get("creation_run_ids"), label="creation runs")
        if intents[0] not in creation_ids:
            raise ValueError("Cursor SDK journal cancellation is not a creation occupant")


def _load_journal_records(
    path: Path,
    *,
    expected_sha256: str,
) -> tuple[list[dict[str, object]], int]:
    if path.is_symlink() or not path.is_file() or sha256_file(path) != expected_sha256:
        raise ValueError("Cursor SDK journal integrity check failed")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Cursor SDK journal record is invalid")
        records.append(value)
    versions = {item.get("schema_version") for item in records}
    if versions == {JOURNAL_SCHEMA_VERSION}:
        return records, JOURNAL_SCHEMA_VERSION
    if versions == {JOURNAL_SCHEMA_VERSION_V2}:
        for record in records:
            _validate_v2_record(record)
        _validate_v2_relations(records)
        return records, JOURNAL_SCHEMA_VERSION_V2
    raise ValueError("Cursor SDK journal schema version is invalid")


def _binding_matches(records: list[dict[str, object]], expected_binding: dict[str, str]) -> None:
    for record in records:
        if any(str(record.get(key, "")) != value for key, value in expected_binding.items()):
            raise ValueError("Cursor SDK journal attempt binding is invalid")


def authenticate_sdk_journal(
    path: Path,
    *,
    expected_sha256: str,
    operation: str,
    expected_binding: dict[str, str],
) -> tuple[dict[str, object], ...]:
    records, version = _load_journal_records(path, expected_sha256=expected_sha256)
    actual = tuple(str(item.get("kind", "")) for item in records)
    allowed: tuple[tuple[str, ...], ...]
    if version == JOURNAL_SCHEMA_VERSION:
        allowed = (
            (CREATE_SEQUENCE,) if operation == "create" else (SEND_SEQUENCE, LEGACY_SEND_SEQUENCE)
        )
    elif operation == "create":
        allowed = (CREATE_SEQUENCE_V2,)
    else:
        allowed = SEND_SUCCESS_SEQUENCES_V2
    if actual not in allowed:
        raise ValueError("Cursor SDK journal ordering is invalid")
    if sum(item.get("kind") == "terminal" for item in records) != 1:
        raise ValueError("Cursor SDK journal must contain exactly one terminal record")
    _binding_matches(records, expected_binding)
    return tuple(records)


def authenticate_sdk_recovery_journal(
    path: Path,
    *,
    expected_sha256: str,
    expected_binding: dict[str, str],
) -> tuple[dict[str, object], ...]:
    """Authenticate a completed uncertain send journal for native reconciliation."""

    records, version = _load_journal_records(path, expected_sha256=expected_sha256)
    actual = tuple(str(item.get("kind", "")) for item in records)
    allowed = (
        SEND_UNCERTAIN_SEQUENCES
        if version == JOURNAL_SCHEMA_VERSION
        else (*SEND_UNCERTAIN_SEQUENCES_V2, *SEND_SUCCESS_SEQUENCES_V2)
    )
    if actual not in allowed:
        raise ValueError("Cursor SDK recovery journal ordering is invalid")
    if records[-1].get("outcome") not in {"uncertain", "success", "failed"}:
        raise ValueError("Cursor SDK recovery journal must have a terminal outcome")
    _binding_matches(records, expected_binding)
    baseline = records[0].get("run_ids")
    if not isinstance(baseline, list) or any(
        not isinstance(item, str) or not item for item in baseline
    ):
        raise ValueError("Cursor SDK journal baseline is invalid")
    if len(set(baseline)) != len(baseline):
        raise ValueError("Cursor SDK journal baseline identities are not unique")
    return tuple(records)


def seal_interrupted_sdk_send_journal(path: Path) -> bool:
    """Seal a post-send journal only after the owning worker is proved absent."""

    if path.is_symlink() or not path.is_file():
        return False
    values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if not values or any(not isinstance(item, dict) for item in values):
        return False
    records = [item for item in values if isinstance(item, dict)]
    kinds = tuple(str(item.get("kind", "")) for item in records)
    versions = {item.get("schema_version") for item in records}
    if versions == {JOURNAL_SCHEMA_VERSION_V2}:
        schema_version = JOURNAL_SCHEMA_VERSION_V2
        complete = SEND_UNCERTAIN_SEQUENCES_V2 + SEND_SUCCESS_SEQUENCES_V2
        if kinds in complete:
            return True
        if kinds not in _SEALABLE_SEND_PREFIXES_V2:
            return False
    elif versions == {JOURNAL_SCHEMA_VERSION}:
        schema_version = JOURNAL_SCHEMA_VERSION
        if kinds in SEND_UNCERTAIN_SEQUENCES or kinds == SEND_SEQUENCE:
            return True
        if kinds not in {
            ("run_inventory_baseline", "send_intent"),
            ("run_inventory_baseline", "send_intent", "native_run_bound"),
        }:
            return False
    else:
        return False
    last = records[-1]
    payload = {
        key: last[key]
        for key in (
            "attempt_id",
            "dispatch_id",
            "conversation_owner_run_id",
            "conversation_key",
            "agent_id",
            "prompt_sha256",
            "native_run_id",
        )
        if key in last and last[key] is not None
    }
    append_sdk_journal(
        path,
        kind="terminal",
        payload={
            **payload,
            "outcome": "uncertain",
            "failure_kind": "cursor_sdk_worker_lost",
        },
        schema_version=schema_version,
    )
    return True
