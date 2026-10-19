"""Read-only contracts for retained Cursor CLI artifacts and historical schemas.

These tests authenticate recorded bytes without dispatching the retired runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_dev_loop.scheduler.application.attempt_backend import TerminationClass
from ai_dev_loop.scheduler.application.attempt_envelope import (
    attempt_result_rel,
    attempt_stderr_rel,
    attempt_stdout_rel,
    build_result_envelope,
    envelope_sha256,
    sha256_file,
)
from ai_dev_loop.scheduler.application.cursor_evidence import (
    CursorEvidenceError,
    authenticate_pinned_invocation_evidence,
    invocation_evidence_sha256,
    load_authenticated_cursor_outcome,
    verify_cursor_invocation_evidence,
)
from ai_dev_loop.scheduler.domain.common import payload_sha256
from ai_dev_loop.scheduler.domain.cursor_contract import (
    RUN_CURSOR_TURN_EFFECT_KIND,
    cursor_attempt_events_rel,
    invocation_evidence_rel,
)


def test_tampered_invocation_evidence_rejected_before_launch(tmp_path: Path) -> None:
    attempt_id = "att-" + "a" * 32
    run_id = "run-test"
    dispatch_id = "fx-test"
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "prompt_path": "prompts/cursor-initial.txt",
        "prompt_sha256": "b" * 64,
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "invocation_evidence_sha256": invocation_evidence_sha256(evidence),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    from ai_dev_loop.scheduler.domain.common import payload_sha256

    with pytest.raises(CursorEvidenceError, match="prompt artifact missing"):
        verify_cursor_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )
    evidence["run_id"] = "tampered"
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CursorEvidenceError, match="launch intent does not match"):
        verify_cursor_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_replaced_stdout_fails_completion_envelope_authentication(tmp_path: Path) -> None:
    attempt_id = "att-test"
    unit_identity = f"unit-{attempt_id}"
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = tmp_path / stdout_rel
    stderr_path = tmp_path / stderr_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("", encoding="utf-8")
    original_stdout = (
        '{"effect_kind":"cursor.run_turn","attempt_id":"att-test","dispatch_id":"fx-test",'
        '"returncode":0,"parse_ok":true,"has_completion_signal":true}\n'
    )
    stdout_path.write_text(original_stdout, encoding="utf-8")
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    (tmp_path / result_rel).write_bytes(envelope)
    stdout_path.write_text('{"effect_kind":"cursor.run_turn","chat_id":"evil"}\n', encoding="utf-8")
    with pytest.raises((CursorEvidenceError, ValueError)):
        load_authenticated_cursor_outcome(
            tmp_path,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=0,
            expected_envelope_sha256=envelope_sha256(envelope),
            expected_dispatch_id="fx-test",
            expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_attempt_scoped_cursor_evidence_paths_are_distinct() -> None:
    first = cursor_attempt_events_rel(1, "att-first")
    second = cursor_attempt_events_rel(1, "att-second")
    assert first != second
    assert "att-first" in first
    assert "att-second" in second


def test_completion_envelope_hash_mismatch_blocks_authenticated_outcome(
    tmp_path: Path,
) -> None:
    attempt_id = "att-test"
    unit_identity = f"unit-{attempt_id}"
    stdout_rel = attempt_stdout_rel(attempt_id)
    stderr_rel = attempt_stderr_rel(attempt_id)
    result_rel = attempt_result_rel(attempt_id)
    stdout_path = tmp_path / stdout_rel
    stderr_path = tmp_path / stderr_rel
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.write_text("", encoding="utf-8")
    stdout_path.write_text(
        json.dumps(
            {
                "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
                "attempt_id": attempt_id,
                "dispatch_id": "fx-test",
                "returncode": 0,
                "parse_ok": True,
                "has_completion_signal": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    envelope = build_result_envelope(
        attempt_id=attempt_id,
        unit_identity=unit_identity,
        exit_code=0,
        termination_class=TerminationClass.SUCCESS,
        stdout_artifact_path=stdout_rel,
        stdout_sha256=sha256_file(stdout_path),
        stderr_artifact_path=stderr_rel,
        stderr_sha256=sha256_file(stderr_path),
    )
    (tmp_path / result_rel).write_bytes(envelope)
    with pytest.raises(CursorEvidenceError, match="completion envelope hash"):
        load_authenticated_cursor_outcome(
            tmp_path,
            attempt_id=attempt_id,
            unit_identity=unit_identity,
            result_rel=result_rel,
            stdout_rel=stdout_rel,
            stderr_rel=stderr_rel,
            observed_exit_code=0,
            expected_envelope_sha256="0" * 64,
            expected_dispatch_id="fx-test",
            expected_effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_pinned_invocation_evidence_rejects_post_dispatch_tamper(tmp_path: Path) -> None:
    attempt_id = "att-" + "c" * 32
    run_id = "run-test"
    dispatch_id = "fx-dispatch"
    evidence = {
        "attempt_id": attempt_id,
        "run_id": run_id,
        "dispatch_id": dispatch_id,
        "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
        "prompt_path": "prompts/cursor-initial.txt",
        "prompt_sha256": "b" * 64,
        "repository_root": "/tmp/repo",
        "repository_git_common_dir": "/tmp/repo/.git",
        "repository_git_dir": "/tmp/repo/.git",
        "repository_branch": "main",
        "repository_initial_head": "abc123",
    }
    rel = invocation_evidence_rel(attempt_id)
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    pinned = invocation_evidence_sha256(evidence)
    launch_intent = json.dumps(
        {
            "attempt_id": attempt_id,
            "dispatch_id": dispatch_id,
            "run_id": run_id,
            "unit_identity": f"unit-{attempt_id}",
            "launch_nonce": "nonce",
            "effect_kind": RUN_CURSOR_TURN_EFFECT_KIND,
            "invocation_evidence_sha256": pinned,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence["run_id"] = "tampered-after-dispatch"
    path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
    with pytest.raises(CursorEvidenceError, match="pinned invocation evidence digest mismatch"):
        authenticate_pinned_invocation_evidence(
            tmp_path,
            attempt_id=attempt_id,
            pinned_invocation_evidence_sha256=pinned,
            run_id=run_id,
            dispatch_id=dispatch_id,
            unit_identity=f"unit-{attempt_id}",
            launch_nonce="nonce",
            launch_intent_sha256=payload_sha256(launch_intent),
            effect_kind=RUN_CURSOR_TURN_EFFECT_KIND,
        )


def test_timeout_persisted_models_match_schemas_and_read_old_checkpoint() -> None:
    import jsonschema

    from ai_dev_loop.paths import schema_path
    from ai_dev_loop.scheduler.domain.events import CursorTimeoutRetryEvent, parse_scheduler_event
    from ai_dev_loop.scheduler.domain.state import CursorWorkflowCheckpoint

    legacy = {"iteration": 1}
    cursor = CursorWorkflowCheckpoint.model_validate(legacy)
    assert cursor.timeout_attempt_id is None and cursor.timeout_automatic_retries == 0
    schema = json.loads(schema_path("scheduler-cursor-workflow-checkpoint-v1.json").read_text())
    jsonschema.validate(legacy, schema)
    jsonschema.validate(cursor.model_dump(mode="json"), schema)
    for action, due, count in [
        ("automatic", "2026-09-19T12:30:00Z", 1),
        ("manual", "2026-09-19T12:00:00Z", 0),
        ("exhausted", None, 3),
    ]:
        event = CursorTimeoutRetryEvent(
            run_id="run-test",
            attempt_id="att-test",
            iteration=1,
            automatic_retries=count,
            action=action,
            available_at=due,
        )
        payload = event.model_dump(mode="json")
        schema = json.loads(schema_path("scheduler-cursor-timeout-retry-event-v1.json").read_text())
        jsonschema.validate(payload, schema)
        assert parse_scheduler_event(payload) == event
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**payload, "automatic_retries": 4}, schema)
