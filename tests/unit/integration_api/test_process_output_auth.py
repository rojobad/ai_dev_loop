"""Unit tests for process output attempt authentication (Phase 21.5 corrections)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_dev_loop.integration_api.process_output_auth import (
    ProcessOutputIntegrityError,
    attempt_reconciliation_ingested,
    authenticate_process_attempt,
    execution_writer_stopped,
)
from ai_dev_loop.scheduler.application.codex_evidence import invocation_evidence_sha256
from ai_dev_loop.scheduler.domain.codex_contract import BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel


def _attempt_row(**overrides: object) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE t (
            attempt_id TEXT, run_id TEXT, dispatch_id TEXT, component TEXT,
            status TEXT, unit_identity TEXT, launch_intent_sha256 TEXT,
            launch_nonce TEXT, completion_envelope_sha256 TEXT, exit_code INTEGER,
            stdout_artifact_path TEXT, stderr_artifact_path TEXT,
            result_artifact_path TEXT, iteration INTEGER, ingested INTEGER
        )
        """
    )
    base = {
        "attempt_id": "att-00000000000000000000000000000001",
        "run_id": "run-test",
        "dispatch_id": "dispatch-1",
        "component": "codex",
        "status": "launching",
        "unit_identity": "unit-1",
        "launch_intent_sha256": "",
        "launch_nonce": "nonce-1",
        "completion_envelope_sha256": None,
        "exit_code": None,
        "stdout_artifact_path": "attempts/att-00000000000000000000000000000001/stdout.txt",
        "stderr_artifact_path": "attempts/att-00000000000000000000000000000001/stderr.txt",
        "result_artifact_path": "attempts/att-00000000000000000000000000000001/result.json",
        "iteration": 1,
        "ingested": 0,
    }
    base.update(overrides)
    conn.execute(
        """
        INSERT INTO t VALUES (
            :attempt_id, :run_id, :dispatch_id, :component, :status,
            :unit_identity, :launch_intent_sha256, :launch_nonce,
            :completion_envelope_sha256, :exit_code, :stdout_artifact_path,
            :stderr_artifact_path, :result_artifact_path, :iteration, :ingested
        )
        """,
        base,
    )
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM t").fetchone()


def test_f01_missing_launch_binding_rejected(tmp_path: Path) -> None:
    row = _attempt_row(launch_intent_sha256="")
    with pytest.raises(ProcessOutputIntegrityError, match="launch binding"):
        authenticate_process_attempt(
            tmp_path,
            row,
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        )


def test_f01_invocation_run_id_mismatch_rejected(tmp_path: Path) -> None:
    attempt_id = "att-00000000000000000000000000000001"
    evidence = {
        "schema_version": 1,
        "attempt_id": attempt_id,
        "run_id": "wrong-run",
        "dispatch_id": "dispatch-1",
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "review_iteration": 1,
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence), encoding="utf-8")
    digest = invocation_evidence_sha256(evidence)
    from ai_dev_loop.scheduler.domain.common import payload_sha256

    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": "dispatch-1",
            "run_id": "run-test",
            "unit_identity": "unit-1",
            "launch_nonce": "nonce-1",
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "invocation_evidence_sha256": digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    row = _attempt_row(launch_intent_sha256=payload_sha256(launch_intent))
    with pytest.raises(ProcessOutputIntegrityError):
        authenticate_process_attempt(
            tmp_path,
            row,
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        )


def test_f03_cancelled_ingested_marks_writer_stopped_without_outcome() -> None:
    row = _attempt_row(status="cancelled", completion_envelope_sha256=None, ingested=1)
    assert attempt_reconciliation_ingested(row) is True
    assert execution_writer_stopped("cancelled", ingested=True) is True
    assert execution_writer_stopped("cancelled", ingested=False) is False
