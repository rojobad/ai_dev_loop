"""Phase 25.8 classification, witness, and schema contracts."""

from __future__ import annotations

import json

import jsonschema
import pytest
from pydantic import ValidationError
from tests.unit.scheduler.test_phase25_2_sdk_worker import (
    AGENT_ID,
    NATIVE_RUN_ID,
    FakeSdkAdapter,
    _evidence,
    _repo,
    _run_sdk_turn,
)

from ai_dev_loop.integrations.cursor_sdk.port import CursorSdkEnvelope
from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.cursor_sdk_no_progress import (
    NO_PROGRESS_MESSAGE,
    NO_PROGRESS_MESSAGE_SHA256,
    CursorSdkNoProgressAuthorizationV1,
    CursorSdkNoProgressWitnessV1,
    exact_status_no_progress,
)

EXACT = NO_PROGRESS_MESSAGE


def _status_payload(
    *,
    message: str = EXACT,
    status: str = "ERROR",
    kind: str = "status",
    agent_id: str = AGENT_ID,
    run_id: str = NATIVE_RUN_ID,
) -> dict[str, object]:
    return {
        "kind": "sdk_message",
        "sdk_message": {
            "type": kind,
            "status": status,
            "message": message,
            "agent_id": agent_id,
            "run_id": run_id,
        },
    }


def test_exact_public_status_matches_and_other_text_does_not() -> None:
    assert exact_status_no_progress(
        _status_payload(message=f"  {EXACT}\n"),
        agent_id=AGENT_ID,
        native_run_id=NATIVE_RUN_ID,
    )
    rejects = [
        _status_payload(message=f"prefix {EXACT}"),
        _status_payload(kind="assistant", message=EXACT),
        _status_payload(kind="tool_call", status="error", message=EXACT),
        _status_payload(status="FINISHED"),
        _status_payload(agent_id="other-agent"),
        _status_payload(run_id="other-run"),
        {"sdk_message": {"type": "status", "status": "ERROR", "message": EXACT}},
        {"nested": {"message": EXACT}},
    ]
    for payload in rejects:
        assert not exact_status_no_progress(
            payload,
            agent_id=AGENT_ID,
            native_run_id=NATIVE_RUN_ID,
        )


def test_authorization_rejects_boolean_consumed_count() -> None:
    payload = {
        "schema_version": 1,
        "logical_turn_key": "a" * 64,
        "consumed_count": True,
        "status": "reserved",
        "category": "cursor_sdk_resume_no_progress",
        "source_run_id": "run",
        "source_attempt_id": "attempt",
        "witness_path": "cursor/sdk-no-progress/attempt/witness-v1.json",
        "witness_sha256": "b" * 64,
        "authorized_at": "2027-01-01T00:00:00.000000Z",
        "due_at": "2027-01-01T00:01:00.000000Z",
        "turn_kind": "initial",
        "conversation_key": "c" * 64,
        "base_prompt_sha256": "d" * 64,
        "reviews_completed": 0,
    }
    with pytest.raises(ValidationError):
        CursorSdkNoProgressAuthorizationV1.model_validate(payload)


def test_published_no_progress_models_match_json_schemas() -> None:
    models = (
        (CursorSdkNoProgressWitnessV1, "scheduler-cursor-sdk-no-progress-witness-v1.json"),
        (
            CursorSdkNoProgressAuthorizationV1,
            "scheduler-cursor-sdk-no-progress-authorization-v1.json",
        ),
    )
    for model, name in models:
        published = json.loads(schema_path(name).read_text(encoding="utf-8"))
        published.pop("$schema", None)
        assert published == model.model_json_schema()
        jsonschema.Draft202012Validator.check_schema(published)
    assert len(NO_PROGRESS_MESSAGE_SHA256) == 64


def test_worker_writes_witness_only_for_exact_terminal_status(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("a" * 64)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    envelope = CursorSdkEnvelope(offset="9", payload=_status_payload())
    adapter = FakeSdkAdapter(repo, terminal_status="error", terminal_text="", envelopes=(envelope,))
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter",
        lambda evidence: adapter,
    )
    attempt_id = "att-" + "9" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    witness_path = run_root / outcome["no_progress_witness_path"]
    witness = CursorSdkNoProgressWitnessV1.model_validate_json(witness_path.read_text())
    assert witness.message_sha256 == NO_PROGRESS_MESSAGE_SHA256
    assert witness.final_native_status == "error"
    assert EXACT not in witness_path.read_text()
    assert outcome["returncode"] == 1

    assistant = CursorSdkEnvelope(
        offset="10",
        payload={
            "kind": "sdk_message",
            "sdk_message": {"type": "assistant", "message": EXACT},
        },
    )
    assistant_adapter = FakeSdkAdapter(
        repo,
        terminal_status="error",
        terminal_text="",
        envelopes=(assistant,),
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter",
        lambda evidence: assistant_adapter,
    )
    other_attempt = "att-" + "a" * 32
    other = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=other_attempt)
    other.update(evidence)
    other["attempt_id"] = other_attempt
    other["dispatch_id"] = f"dsp-{other_attempt}"
    other_outcome = _run_sdk_turn(other, run_root, attempt_id=other_attempt)
    assert "no_progress_witness_path" not in other_outcome


def test_contradictory_terminal_status_does_not_write_a_witness(monkeypatch, tmp_path) -> None:  # type: ignore[no-untyped-def]
    repo = _repo(tmp_path)
    run_root = tmp_path / "artifacts" / "runs" / ("b" * 64)
    prompt = run_root / "prompts/cursor-initial.txt"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("implement exactly", encoding="utf-8")
    adapter = FakeSdkAdapter(
        repo,
        terminal_status="error",
        terminal_text="",
        envelopes=(
            CursorSdkEnvelope(offset="9", payload=_status_payload()),
            CursorSdkEnvelope(
                offset="10",
                payload=_status_payload(message="finished", status="FINISHED"),
            ),
        ),
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter",
        lambda evidence: adapter,
    )
    attempt_id = "att-" + "9" * 32
    evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=attempt_id)
    evidence.update(
        {
            "iteration": 1,
            "agent_id": AGENT_ID,
            "prompt_path": "prompts/cursor-initial.txt",
            "prompt_sha256": __import__("hashlib").sha256(b"implement exactly").hexdigest(),
        }
    )
    outcome = _run_sdk_turn(evidence, run_root, attempt_id=attempt_id)
    assert "no_progress_witness_path" not in outcome

    running = FakeSdkAdapter(
        repo,
        terminal_status="error",
        terminal_text="",
        envelopes=(
            CursorSdkEnvelope(
                offset="8", payload=_status_payload(message="running", status="RUNNING")
            ),
            CursorSdkEnvelope(offset="9", payload=_status_payload()),
        ),
    )
    monkeypatch.setattr(
        "ai_dev_loop.scheduler.cursor_attempt_runner._sdk_adapter",
        lambda evidence: running,
    )
    kept = "att-" + "8" * 32
    kept_evidence = _evidence(repo, effect_kind="cursor.run_turn", attempt_id=kept)
    kept_evidence.update(evidence)
    kept_evidence["attempt_id"] = kept
    kept_evidence["dispatch_id"] = f"dsp-{kept}"
    kept_outcome = _run_sdk_turn(kept_evidence, run_root, attempt_id=kept)
    assert "no_progress_witness_path" in kept_outcome
