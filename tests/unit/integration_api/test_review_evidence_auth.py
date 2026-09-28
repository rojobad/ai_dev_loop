"""Production-path tests for authenticated review evidence (F-01, F-03, F-10)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ai_dev_loop.integration_api.review_evidence_auth import (
    ReviewEvidenceIntegrityError,
    load_authenticated_review_evidence,
)
from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
)
from ai_dev_loop.scheduler.application.codex_evidence import invocation_evidence_sha256
from ai_dev_loop.scheduler.domain.codex_contract import (
    BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    SCHEDULER_CODEX_REVIEW_SANDBOX,
    codex_review_prompt_evidence_rel,
    codex_review_prompt_rel,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import invocation_evidence_rel
from ai_dev_loop.scheduler.domain.review_prompt_evidence import SchedulerReviewPromptEvidenceV1
from ai_dev_loop.scheduler.infrastructure.paths import run_artifact_root
from ai_dev_loop.state import sha256_bytes


def _attempt_row(**fields: object) -> sqlite3.Row:
    conn = sqlite3.connect(":memory:")
    columns = [
        "attempt_id",
        "run_id",
        "dispatch_id",
        "iteration",
        "status",
        "unit_identity",
        "launch_intent_sha256",
        "launch_nonce",
        "stdout_artifact_path",
        "stderr_artifact_path",
        "result_artifact_path",
        "completion_envelope_sha256",
    ]
    conn.execute(f"CREATE TABLE t ({', '.join(columns)} TEXT)")
    attempt_id = str(fields.get("attempt_id", "att-" + "a" * 32))
    defaults: dict[str, object] = {
        "attempt_id": attempt_id,
        "run_id": "run-auth",
        "dispatch_id": "dispatch-auth",
        "iteration": 1,
        "status": "completed",
        "unit_identity": f"unit-{attempt_id}",
        "launch_intent_sha256": "",
        "launch_nonce": "nonce-auth",
        "stdout_artifact_path": attempt_stdout_rel(attempt_id),
        "stderr_artifact_path": attempt_stderr_rel(attempt_id),
        "result_artifact_path": attempt_result_rel(attempt_id),
        "completion_envelope_sha256": None,
    }
    defaults.update(fields)
    placeholders = ", ".join("?" * len(columns))
    conn.execute(
        f"INSERT INTO t ({', '.join(columns)}) VALUES ({placeholders})",
        tuple(str(defaults[col]) if defaults[col] is not None else None for col in columns),
    )
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM t").fetchone()


def _write_invocation_binding(
    run_root: Path,
    *,
    attempt_id: str,
    run_id: str,
    dispatch_id: str,
    unit_identity: str,
    launch_nonce: str,
) -> str:
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "codex_sandbox": SCHEDULER_CODEX_REVIEW_SANDBOX,
        "review_model": "gpt-5.6-sol",
        "review_reasoning_effort": "high",
        "review_iteration": 1,
    }
    rel = invocation_evidence_rel(attempt_id)
    path = run_root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": unit_identity,
            "launch_nonce": launch_nonce,
            "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
            "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload_sha256(launch_intent)


def test_f03_evidence_without_prompt_bytes_is_data_integrity(tmp_path: Path) -> None:
    run_id = "run-missing-prompt"
    attempt_id = "att-" + "b" * 32
    unit_identity = f"unit-{attempt_id}"
    run_root = run_artifact_root(tmp_path / "artifacts", run_id)
    run_root.mkdir(parents=True)
    launch_intent_sha = _write_invocation_binding(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id="dispatch-auth",
        unit_identity=unit_identity,
        launch_nonce="nonce-auth",
    )
    evidence_rel = codex_review_prompt_evidence_rel(1, attempt_id)
    prompt_rel = codex_review_prompt_rel(1, attempt_id)
    payload = SchedulerReviewPromptEvidenceV1.model_validate(
        {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "review_iteration": 1,
            "prompt_path": prompt_rel,
            "prompt_sha256": "c" * 64,
            "prompt_size_bytes": 10,
        }
    )
    (run_root / evidence_rel).parent.mkdir(parents=True, exist_ok=True)
    (run_root / evidence_rel).write_text(
        json.dumps(payload.model_dump(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    row = _attempt_row(
        attempt_id=attempt_id,
        run_id=run_id,
        unit_identity=unit_identity,
        launch_intent_sha256=launch_intent_sha,
        status="completed",
        completion_envelope_sha256="d" * 64,
    )
    with pytest.raises(ReviewEvidenceIntegrityError):
        load_authenticated_review_evidence(
            run_root,
            row,
            effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        )


def test_f12_cancelled_without_completion_allows_prompt_pending_response(
    tmp_path: Path,
) -> None:
    run_id = "run-cancelled"
    attempt_id = "att-" + "e" * 32
    unit_identity = f"unit-{attempt_id}"
    run_root = run_artifact_root(tmp_path / "artifacts", run_id)
    run_root.mkdir(parents=True)
    launch_intent_sha = _write_invocation_binding(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id="dispatch-auth",
        unit_identity=unit_identity,
        launch_nonce="nonce-auth",
    )
    prompt_rel = codex_review_prompt_rel(1, attempt_id)
    evidence_rel = codex_review_prompt_evidence_rel(1, attempt_id)
    prompt_bytes = b"cancelled-review-prompt\n"
    prompt_path = run_root / prompt_rel
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_bytes(prompt_bytes)
    digest = sha256_bytes(prompt_bytes)
    payload = SchedulerReviewPromptEvidenceV1.model_validate(
        {
            "schema_version": 1,
            "run_id": run_id,
            "attempt_id": attempt_id,
            "review_iteration": 1,
            "prompt_path": prompt_rel,
            "prompt_sha256": digest,
            "prompt_size_bytes": len(prompt_bytes),
        }
    )
    (run_root / evidence_rel).write_text(
        json.dumps(payload.model_dump(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    row = _attempt_row(
        attempt_id=attempt_id,
        run_id=run_id,
        unit_identity=unit_identity,
        launch_intent_sha256=launch_intent_sha,
        status="cancelled",
        completion_envelope_sha256=None,
    )
    auth = load_authenticated_review_evidence(
        run_root,
        row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    )
    assert auth.prompt_state == "available"
    assert auth.result_state == "not_produced"
    assert auth.validated is None


def test_f01_uncertain_without_completion_binding_stays_pending(tmp_path: Path) -> None:
    run_id = "run-uncertain"
    attempt_id = "att-" + "c" * 32
    unit_identity = f"unit-{attempt_id}"
    run_root = run_artifact_root(tmp_path / "artifacts", run_id)
    run_root.mkdir(parents=True)
    launch_intent_sha = _write_invocation_binding(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id="dispatch-auth",
        unit_identity=unit_identity,
        launch_nonce="nonce-auth",
    )
    row = _attempt_row(
        attempt_id=attempt_id,
        run_id=run_id,
        unit_identity=unit_identity,
        launch_intent_sha256=launch_intent_sha,
        status="uncertain",
        completion_envelope_sha256=None,
    )
    auth = load_authenticated_review_evidence(
        run_root,
        row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    )
    assert auth.result_state == "pending"
    assert auth.validated is None


def test_f10_empty_result_sha_with_path_is_not_produced(tmp_path: Path) -> None:
    run_id = "run-no-response"
    attempt_id = "att-" + "d" * 32
    unit_identity = f"unit-{attempt_id}"
    run_root = run_artifact_root(tmp_path / "artifacts", run_id)
    run_root.mkdir(parents=True)
    launch_intent_sha = _write_invocation_binding(
        run_root,
        attempt_id=attempt_id,
        run_id=run_id,
        dispatch_id="dispatch-auth",
        unit_identity=unit_identity,
        launch_nonce="nonce-auth",
    )
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = run_root / stdout_rel
    stderr_path = run_root / stderr_rel
    result_path = run_root / result_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_bytes(b"")
    review_result_rel = f"codex/reviews/01.{attempt_id}.json"
    outcome = {
        "attempt_id": attempt_id,
        "dispatch_id": "dispatch-auth",
        "effect_kind": BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
        "run_id": run_id,
        "review_result_path": review_result_rel,
        "review_result_sha256": "",
    }
    stdout_path.write_bytes(json.dumps(outcome).encode("utf-8"))
    stdout_sha = sha256_bytes(stdout_path.read_bytes())
    stderr_sha = sha256_bytes(stderr_path.read_bytes())
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=2,
        termination_class=TerminationClass.NONZERO_EXIT,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=stdout_sha,
        stderr_artifact_path=stderr_rel,
        stderr_sha256=stderr_sha,
    )
    result_path.write_bytes(envelope)
    completion_sha = sha256_bytes(envelope)
    row = _attempt_row(
        attempt_id=attempt_id,
        run_id=run_id,
        unit_identity=unit_identity,
        launch_intent_sha256=launch_intent_sha,
        status="failed",
        completion_envelope_sha256=completion_sha,
    )
    auth = load_authenticated_review_evidence(
        run_root,
        row,
        effect_kind=BOOTSTRAP_CODEX_REVIEW_EFFECT_KIND,
    )
    assert auth.result_state == "not_produced"
    assert auth.validated is None
