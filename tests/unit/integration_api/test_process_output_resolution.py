"""Unit tests for process output stream resolution (Phase 21.5 corrections)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_dev_loop.integration_api.process_output_auth import (
    AuthenticatedProcessAttempt,
    ProcessOutputIntegrityError,
    execution_writer_stopped,
)
from ai_dev_loop.integration_api.process_output_resolution import (
    load_stream_truncation,
    resolve_process_stream,
)
from ai_dev_loop.runners.cursor import CREATE_CHAT_STDOUT_REL
from ai_dev_loop.scheduler.application.attempt_envelope import sha256_file
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    codex_attempt_events_rel,
    codex_review_metadata_rel,
)
from ai_dev_loop.scheduler.domain.cursor_contract import CREATE_CHAT_EFFECT_KIND


def _row(**fields: object) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (attempt_id TEXT, run_id TEXT, component TEXT, status TEXT)")
    base = {
        "attempt_id": "att-00000000000000000000000000000001",
        "run_id": "run-a",
        "component": "cursor",
        "status": "completed",
    }
    base.update(fields)
    conn.execute(
        "INSERT INTO t VALUES (:attempt_id, :run_id, :component, :status)",
        base,
    )
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM t").fetchone()


def test_f02_create_chat_requires_authenticated_byte_binding(tmp_path: Path) -> None:
    stdout_path = tmp_path / CREATE_CHAT_STDOUT_REL
    stdout_path.parent.mkdir(parents=True)
    stdout_path.write_text("chat-id\n", encoding="utf-8")
    sha = sha256_file(stdout_path)
    auth = AuthenticatedProcessAttempt(
        invocation={"effect_kind": CREATE_CHAT_EFFECT_KIND},
        outcome={
            "effect_kind": CREATE_CHAT_EFFECT_KIND,
            "create_chat_stdout_artifact_path": CREATE_CHAT_STDOUT_REL,
            "create_chat_stdout_sha256": sha,
        },
        writer_stopped=True,
    )
    resolved = resolve_process_stream(
        tmp_path,
        attempt_row=_row(),
        effect_kind=CREATE_CHAT_EFFECT_KIND,
        stream="stdout",
        auth=auth,
    )
    assert resolved.unavailable_reason is None
    assert resolved.relative_path == CREATE_CHAT_STDOUT_REL

    auth_metadata_only = AuthenticatedProcessAttempt(
        invocation={},
        outcome={
            "effect_kind": CREATE_CHAT_EFFECT_KIND,
            "create_chat_metadata_path": "cursor/create-chat/metadata.json",
        },
        writer_stopped=True,
    )
    unresolved = resolve_process_stream(
        tmp_path,
        attempt_row=_row(),
        effect_kind=CREATE_CHAT_EFFECT_KIND,
        stream="stdout",
        auth=auth_metadata_only,
    )
    assert unresolved.unavailable_reason == "not_captured"


def test_f06_create_chat_bound_missing_file_is_integrity(tmp_path: Path) -> None:
    stdout_path = tmp_path / CREATE_CHAT_STDOUT_REL
    stdout_path.parent.mkdir(parents=True)
    stdout_path.write_text("chat-id\n", encoding="utf-8")
    sha = sha256_file(stdout_path)
    stdout_path.unlink()
    auth = AuthenticatedProcessAttempt(
        invocation={},
        outcome={
            "effect_kind": CREATE_CHAT_EFFECT_KIND,
            "create_chat_stdout_artifact_path": CREATE_CHAT_STDOUT_REL,
            "create_chat_stdout_sha256": sha,
        },
        writer_stopped=True,
    )
    with pytest.raises(ProcessOutputIntegrityError, match="missing"):
        resolve_process_stream(
            tmp_path,
            attempt_row=_row(),
            effect_kind=CREATE_CHAT_EFFECT_KIND,
            stream="stdout",
            auth=auth,
        )


def test_f02_create_chat_shared_file_overwrite_is_integrity(tmp_path: Path) -> None:
    stdout_path = tmp_path / CREATE_CHAT_STDOUT_REL
    stdout_path.parent.mkdir(parents=True)
    stdout_path.write_text("first-chat\n", encoding="utf-8")
    first_sha = sha256_file(stdout_path)
    auth = AuthenticatedProcessAttempt(
        invocation={},
        outcome={
            "effect_kind": CREATE_CHAT_EFFECT_KIND,
            "create_chat_stdout_artifact_path": CREATE_CHAT_STDOUT_REL,
            "create_chat_stdout_sha256": first_sha,
        },
        writer_stopped=True,
    )
    stdout_path.write_text("second-chat\n", encoding="utf-8")
    with pytest.raises(ProcessOutputIntegrityError, match="hash mismatch"):
        resolve_process_stream(
            tmp_path,
            attempt_row=_row(),
            effect_kind=CREATE_CHAT_EFFECT_KIND,
            stream="stdout",
            auth=auth,
        )


def test_f03_cancelled_finalized_writer_stopped() -> None:
    assert execution_writer_stopped("cancelled", ingested=True) is True
    assert execution_writer_stopped("cancelled", ingested=False) is False


def test_f03_cancelled_pending_stream_is_not_yet_produced(tmp_path: Path) -> None:
    from ai_dev_loop.integration_api.process_output_resolution import (
        ResolvedProcessStream,
        stream_availability_reason,
    )
    from ai_dev_loop.scheduler.domain.cursor_contract import cursor_attempt_events_rel

    run_root = tmp_path / "run"
    run_root.mkdir()
    attempt_id = "att-cancel-pending"
    iteration = 1
    rel = cursor_attempt_events_rel(iteration, attempt_id)
    auth = AuthenticatedProcessAttempt(
        invocation={"iteration": iteration},
        outcome=None,
        writer_stopped=False,
    )
    resolved = ResolvedProcessStream(rel, "application/x-ndjson", "jsonl", "cursor")
    reason = stream_availability_reason(
        run_root,
        resolved=resolved,
        auth=auth,
        attempt_status="cancelled",
    )
    assert reason == "not_yet_produced"


def test_f03_cancelled_finalized_missing_stream_is_integrity(tmp_path: Path) -> None:
    from ai_dev_loop.integration_api.process_output_resolution import (
        ResolvedProcessStream,
        stream_availability_reason,
    )
    from ai_dev_loop.scheduler.domain.cursor_contract import cursor_attempt_events_rel

    run_root = tmp_path / "run"
    run_root.mkdir()
    attempt_id = "att-cancel-done"
    iteration = 1
    rel = cursor_attempt_events_rel(iteration, attempt_id)
    auth = AuthenticatedProcessAttempt(
        invocation={"iteration": iteration},
        outcome=None,
        writer_stopped=True,
    )
    resolved = ResolvedProcessStream(rel, "application/x-ndjson", "jsonl", "cursor")
    with pytest.raises(ProcessOutputIntegrityError, match="finalized child stream is missing"):
        stream_availability_reason(
            run_root,
            resolved=resolved,
            auth=auth,
            attempt_status="cancelled",
        )


def test_f05_codex_truncation_conflict_rejected(tmp_path: Path) -> None:
    attempt_id = "att-0000000000000000000000000000002a"
    iteration = 1
    meta_rel = codex_review_metadata_rel(iteration, attempt_id)
    meta_path = tmp_path / meta_rel
    meta_path.parent.mkdir(parents=True)
    meta_path.write_text(
        json.dumps({"stdout_truncated": False, "stdout_captured_bytes": 10}),
        encoding="utf-8",
    )
    events_rel = codex_attempt_events_rel(iteration, attempt_id)
    (tmp_path / events_rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / events_rel).write_bytes(b"x" * 10)
    auth = AuthenticatedProcessAttempt(
        invocation={"review_iteration": iteration},
        outcome={"stdout_truncated": True, "stdout_captured_bytes": 10},
        writer_stopped=True,
    )
    row = _row(attempt_id=attempt_id, component="codex", status="completed")
    resolved = resolve_process_stream(
        tmp_path,
        attempt_row=row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        stream="stdout",
        auth=auth,
    )
    with pytest.raises(ProcessOutputIntegrityError, match="disagrees"):
        load_stream_truncation(
            tmp_path,
            attempt_row=row,
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            stream="stdout",
            resolved=resolved,
            auth=auth,
        )
