"""Phase 23.3 v2 schema and historical v1 compatibility."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import CursorInitialRecoveryRecordV1
from ai_dev_loop.scheduler.domain.cursor_recovery_v2 import (
    CursorRecoveryPublicationIntentV2,
    CursorRecoveryRecordV2,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "phase23_3_historical"
    / "cursor-initial-recovery-record-v1.json"
)
SHA = "a" * 64
SESSION = "11111111-1111-4111-8111-111111111111"


def _artifact(owner: str = "source") -> dict[str, str]:
    return {"owner_run_id": owner, "relative_path": "prompts/fix.txt", "sha256": SHA}


def _bound_reviewer() -> dict[str, str]:
    return {
        "form": "bound",
        "session_id": SESSION,
        "bootstrap_run_id": "source",
        "bootstrap_attempt_id": "bootstrap",
        "bootstrap_events_path": "codex/events.jsonl",
        "bootstrap_events_sha256": SHA,
        "binding_run_id": "source",
        "binding_artifact_path": "codex/binding.json",
        "binding_artifact_sha256": SHA,
    }


def _correction_record() -> dict[str, object]:
    return {
        "schema_version": 2,
        "turn_kind": "correction",
        "reviewer": _bound_reviewer(),
        "reviews_completed": 8,
        "submitted_max_review_iterations": 7,
        "effective_review_ceiling": 9,
        "source_run_id": "source",
        "successor_run_id": "successor",
        "failed_attempt_id": "attempt",
        "dispatch_id": "dispatch",
        "chat_id": SESSION,
        "chat_owner_run_id": "source",
        "iteration": 9,
        "admitted": _artifact(),
        "plan_sha256": SHA,
        "submitted_prompt_sha256": SHA,
        "config_sha256": SHA,
        "base_prompt": _artifact(),
        "effective_prompt_path": "prompts/cursor-initial-recovery/effective.txt",
        "effective_prompt_sha256": SHA,
        "raw_fix": _artifact(),
        "staged_patch": _artifact(),
        "review_result": _artifact(),
        "parent_source_run_id": None,
        "parent_recovery_key": None,
        "parent_record_sha256": None,
        "budget_carry_path": "cursor/correction-recovery/budget-carry-v1.json",
        "budget_carry_sha256": SHA,
        "ancestors": [],
    }


def _intent(*, status: str = "pending") -> dict[str, object]:
    ready = status == "ready"
    return {
        "schema_version": 2,
        "turn_kind": "correction",
        "recovery_key": SHA,
        "source_run_id": "source",
        "successor_run_id": "successor",
        "failed_attempt_id": "attempt",
        "dispatch_id": "dispatch",
        "status": status,
        "parent_recovery_key": None,
        "record_sha256": SHA if ready else None,
        "budget_carry_sha256": SHA if ready else None,
        "base_prompt_path": "prompts/envelope.txt",
        "base_prompt_sha256": SHA,
        "base_prompt_owner_run_id": "source",
        "chat_id": SESSION,
        "chat_owner_run_id": "source",
        "chat_artifact_path": "cursor/chat.json",
        "chat_artifact_sha256": SHA,
        "iteration": 9,
        "admitted_artifact_path": "admission/status.txt",
        "admitted_artifact_sha256": SHA,
        "admitted_owner_run_id": "source",
        "plan_sha256": SHA,
        "submitted_prompt_sha256": SHA,
        "config_sha256": SHA,
        "reviews_completed": 8,
        "submitted_max_review_iterations": 7,
        "effective_review_ceiling": 9,
        "reviewer_session_id": SESSION,
        "bootstrap_run_id": "source",
        "bootstrap_attempt_id": "bootstrap",
        "bootstrap_events_path": "codex/events.jsonl",
        "bootstrap_events_sha256": SHA,
        "binding_run_id": "source",
        "binding_artifact_path": "codex/binding.json",
        "binding_artifact_sha256": SHA,
        "fix_owner_run_id": "source",
        "fix_prompt_path": "prompts/fix.txt",
        "fix_prompt_sha256": SHA,
        "staged_owner_run_id": "source",
        "staged_patch_path": "git/staged.patch",
        "staged_patch_sha256": SHA,
        "review_owner_run_id": "source",
        "review_result_path": "codex/result.json",
        "review_result_sha256": SHA,
    }


def test_historical_v1_initial_record_still_loads() -> None:
    raw = FIXTURE.read_bytes()
    provenance = (FIXTURE.parent / "provenance.txt").read_text(encoding="utf-8")
    recorded = ""
    for line in provenance.splitlines():
        if line.startswith("record_sha256:"):
            recorded = line.split(":", 1)[1].strip()
    assert recorded
    assert hashlib.sha256(raw).hexdigest() == recorded
    payload = json.loads(raw)
    record = CursorInitialRecoveryRecordV1.model_validate(payload)
    assert record.canonical_bytes() == raw
    schema = json.loads(
        schema_path("cursor-initial-recovery-record-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(payload)
    assert record.turn_kind == "initial"
    assert record.reviews_completed == 0
    assert record.reviewer_created is False
    assert "session_id" not in payload
    assert "budget_carry_sha256" not in payload


def test_v2_correction_record_rejects_incomplete_null_and_coerced_fields() -> None:
    schema = json.loads(schema_path("cursor-recovery-record-v2.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    complete = _correction_record()
    CursorRecoveryRecordV2.model_validate(complete)
    validator.validate(complete)
    for field in ("reviewer", "raw_fix", "budget_carry_sha256", "ancestors"):
        missing = dict(complete)
        del missing[field]
        with pytest.raises(ValidationError):
            CursorRecoveryRecordV2.model_validate(missing)
        assert list(validator.iter_errors(missing))
    null_session = _correction_record()
    reviewer = dict(_bound_reviewer())
    reviewer["session_id"] = None
    null_session["reviewer"] = reviewer
    with pytest.raises(ValidationError):
        CursorRecoveryRecordV2.model_validate(null_session)
    assert list(validator.iter_errors(null_session))
    coerced = _correction_record()
    coerced["reviews_completed"] = True
    with pytest.raises(ValidationError):
        CursorRecoveryRecordV2.model_validate(coerced)
    assert list(validator.iter_errors(coerced))
    not_created = _correction_record()
    not_created["reviewer"] = {"form": "not_created"}
    with pytest.raises(ValidationError):
        CursorRecoveryRecordV2.model_validate(not_created)
    assert list(validator.iter_errors(not_created))


def test_ancestry_keys_are_relation_specific() -> None:
    schema = json.loads(schema_path("cursor-recovery-record-v2.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    review_key = "codex_attempt_failed:att-00000000000000000000000000000005"
    record = _correction_record()
    record["ancestors"] = [
        {
            "relation": "review_recovery",
            "source_run_id": "source",
            "successor_run_id": "middle",
            "recovery_key": review_key,
        },
        {
            "relation": "cursor_recovery",
            "source_run_id": "middle",
            "successor_run_id": "source",
            "recovery_key": SHA,
        },
    ]
    loaded = CursorRecoveryRecordV2.model_validate(record)
    validator.validate(record)
    assert loaded.ancestors[0].recovery_key == review_key
    sha_as_review = _correction_record()
    sha_as_review["ancestors"] = [
        {
            "relation": "review_recovery",
            "source_run_id": "source",
            "successor_run_id": "middle",
            "recovery_key": SHA,
        }
    ]
    with pytest.raises(ValidationError):
        CursorRecoveryRecordV2.model_validate(sha_as_review)
    assert list(validator.iter_errors(sha_as_review))
    review_as_cursor = _correction_record()
    review_as_cursor["ancestors"] = [
        {
            "relation": "cursor_recovery",
            "source_run_id": "source",
            "successor_run_id": "middle",
            "recovery_key": review_key,
        }
    ]
    with pytest.raises(ValidationError):
        CursorRecoveryRecordV2.model_validate(review_as_cursor)
    assert list(validator.iter_errors(review_as_cursor))


def test_v2_intent_ready_requires_digests_and_rejects_coercion() -> None:
    schema = json.loads(
        schema_path("cursor-recovery-publication-intent-v2.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    pending = _intent()
    CursorRecoveryPublicationIntentV2.model_validate(pending)
    validator.validate(pending)
    ready = _intent(status="ready")
    CursorRecoveryPublicationIntentV2.model_validate(ready)
    validator.validate(ready)
    incomplete_ready = _intent(status="ready")
    incomplete_ready["record_sha256"] = None
    with pytest.raises(ValidationError):
        CursorRecoveryPublicationIntentV2.model_validate(incomplete_ready)
    assert list(validator.iter_errors(incomplete_ready))
    coerced = _intent()
    coerced["reviews_completed"] = "8"
    with pytest.raises(ValidationError):
        CursorRecoveryPublicationIntentV2.model_validate(coerced)
    assert list(validator.iter_errors(coerced))
