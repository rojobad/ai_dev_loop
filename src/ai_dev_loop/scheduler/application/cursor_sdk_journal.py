"""Durable write-ahead identity journal for Cursor SDK dispatch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from ai_dev_loop.paths import SENSITIVE_FILE_MODE
from ai_dev_loop.state import sha256_file

JOURNAL_SCHEMA_VERSION = 1
CREATE_SEQUENCE = ("create_intent", "agent_bound", "terminal")
SEND_SEQUENCE = ("run_inventory_baseline", "send_intent", "native_run_bound", "terminal")
LEGACY_SEND_SEQUENCE = ("send_intent", "native_run_bound", "terminal")
SEND_UNCERTAIN_SEQUENCES = (
    ("run_inventory_baseline", "send_intent", "terminal"),
    ("run_inventory_baseline", "send_intent", "native_run_bound", "terminal"),
)


def append_sdk_journal(path: Path, *, kind: str, payload: dict[str, object]) -> None:
    """Append and fsync one record before the caller crosses an external boundary."""

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
        "schema_version": JOURNAL_SCHEMA_VERSION,
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


def authenticate_sdk_journal(
    path: Path,
    *,
    expected_sha256: str,
    operation: str,
    expected_binding: dict[str, str],
) -> tuple[dict[str, object], ...]:
    if path.is_symlink() or not path.is_file() or sha256_file(path) != expected_sha256:
        raise ValueError("Cursor SDK journal integrity check failed")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise ValueError("Cursor SDK journal record is invalid")
        records.append(value)
    actual = tuple(str(item.get("kind", "")) for item in records)
    allowed = (CREATE_SEQUENCE,) if operation == "create" else (SEND_SEQUENCE, LEGACY_SEND_SEQUENCE)
    if actual not in allowed:
        raise ValueError("Cursor SDK journal ordering is invalid")
    if sum(item.get("kind") == "terminal" for item in records) != 1:
        raise ValueError("Cursor SDK journal must contain exactly one terminal record")
    for record in records:
        if any(str(record.get(key, "")) != value for key, value in expected_binding.items()):
            raise ValueError("Cursor SDK journal attempt binding is invalid")
    return tuple(records)


def authenticate_sdk_recovery_journal(
    path: Path,
    *,
    expected_sha256: str,
    expected_binding: dict[str, str],
) -> tuple[dict[str, object], ...]:
    """Authenticate a completed uncertain send journal for native reconciliation."""

    if path.is_symlink() or not path.is_file() or sha256_file(path) != expected_sha256:
        raise ValueError("Cursor SDK journal integrity check failed")
    records: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict) or value.get("schema_version") != JOURNAL_SCHEMA_VERSION:
            raise ValueError("Cursor SDK journal record is invalid")
        records.append(value)
    actual = tuple(str(item.get("kind", "")) for item in records)
    if actual not in SEND_UNCERTAIN_SEQUENCES:
        raise ValueError("Cursor SDK recovery journal ordering is invalid")
    if records[-1].get("outcome") not in {"uncertain", "success", "failed"}:
        raise ValueError("Cursor SDK recovery journal must have a terminal outcome")
    for record in records:
        if any(str(record.get(key, "")) != value for key, value in expected_binding.items()):
            raise ValueError("Cursor SDK journal attempt binding is invalid")
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
    if kinds in SEND_UNCERTAIN_SEQUENCES or kinds == SEND_SEQUENCE:
        return True
    if kinds not in {
        ("run_inventory_baseline", "send_intent"),
        ("run_inventory_baseline", "send_intent", "native_run_bound"),
    }:
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
        if key in last
    }
    append_sdk_journal(
        path,
        kind="terminal",
        payload={
            **payload,
            "outcome": "uncertain",
            "failure_kind": "cursor_sdk_worker_lost",
        },
    )
    return True
