"""Phase 23.2 prompt, schema, and inherited evidence contracts."""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.common import canonical_json_sha256
from ai_dev_loop.scheduler.domain.cursor_initial_recovery import (
    RECOVERY_NOTE,
    CursorInitialRecoveryPublicationIntentV1,
    CursorInitialRecoveryRecordV1,
    effective_prompt_bytes,
    recovery_key_for,
)
from ai_dev_loop.scheduler.infrastructure.paths import apply_database_permissions
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore
from ai_dev_loop.state import sha256_bytes

BASE = b"exact failed prompt\n"
NOTE = RECOVERY_NOTE.encode("utf-8")


def test_schema_migration_adds_initial_recovery_relation(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    store.bootstrap()
    assert SCHEMA_VERSION == 13
    with store.begin_read() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert version == 13
    assert "scheduler_cursor_initial_recoveries" in tables


def test_effective_prompt_appends_the_plan_note_once() -> None:
    effective = effective_prompt_bytes(BASE)
    assert effective == BASE + b"\n\n" + NOTE + b"\n"
    assert effective.count(NOTE) == 1
    second = effective_prompt_bytes(BASE)
    assert second.count(NOTE) == 1


def test_recovery_key_ignores_timestamps() -> None:
    first = recovery_key_for(source_run_id="run-a", failed_attempt_id="att-1")
    second = recovery_key_for(source_run_id="run-a", failed_attempt_id="att-1")
    other = recovery_key_for(source_run_id="run-a", failed_attempt_id="att-2")
    assert first == second
    assert first != other


def _record(**overrides: object) -> CursorInitialRecoveryRecordV1:
    payload: dict[str, object] = {
        "schema_version": 1,
        "turn_kind": "initial",
        "reviewer_created": False,
        "reviews_completed": 0,
        "source_run_id": "source",
        "successor_run_id": "successor",
        "failed_attempt_id": "attempt",
        "dispatch_id": "dispatch",
        "chat_id": "11111111-1111-4111-8111-111111111111",
        "iteration": 1,
        "admitted_artifact_path": "admission/checkpoint.json",
        "admitted_artifact_sha256": "a" * 64,
        "plan_sha256": "b" * 64,
        "submitted_prompt_sha256": "c" * 64,
        "config_sha256": "d" * 64,
        "base_prompt_path": "prompts/submitted-prompt.txt",
        "base_prompt_sha256": sha256_bytes(BASE),
        "effective_prompt_path": "prompts/cursor-initial-recovery/effective.txt",
        "effective_prompt_sha256": sha256_bytes(effective_prompt_bytes(BASE)),
        "parent_source_run_id": None,
        "parent_recovery_key": None,
        "parent_record_sha256": None,
    }
    payload.update(overrides)
    return CursorInitialRecoveryRecordV1.model_validate(payload)


def _complete_record_payload() -> dict[str, object]:
    record = _record()
    return record.model_dump(mode="json")


def _complete_intent_payload(*, status: str = "pending") -> dict[str, object]:
    return {
        "schema_version": 1,
        "recovery_key": "e" * 64,
        "source_run_id": "source",
        "successor_run_id": "successor",
        "failed_attempt_id": "attempt",
        "dispatch_id": "dispatch",
        "status": status,
        "parent_recovery_key": None,
        "record_sha256": "a" * 64 if status == "ready" else None,
        "base_prompt_path": "prompts/submitted-prompt.txt",
        "base_prompt_sha256": "b" * 64,
        "chat_owner_run_id": "source",
        "chat_artifact_path": "cursor/chat.json",
        "chat_artifact_sha256": "c" * 64,
        "chat_id": "11111111-1111-4111-8111-111111111111",
        "iteration": 1,
        "admitted_artifact_path": "admission/checkpoint.json",
        "admitted_artifact_sha256": "d" * 64,
        "plan_sha256": "1" * 64,
        "submitted_prompt_sha256": "2" * 64,
        "config_sha256": "3" * 64,
    }


def test_record_rejects_absent_required_fields_and_coercion() -> None:
    schema = json.loads(
        schema_path("cursor-initial-recovery-record-v1.json").read_text(encoding="utf-8")
    )
    validator = jsonschema.Draft202012Validator(schema)
    complete = _complete_record_payload()
    CursorInitialRecoveryRecordV1.model_validate(complete)
    validator.validate(complete)
    for field in schema["required"]:
        missing = dict(complete)
        del missing[field]
        with pytest.raises(ValidationError):
            CursorInitialRecoveryRecordV1.model_validate(missing)
        errors = list(validator.iter_errors(missing))
        assert errors
    with pytest.raises(ValidationError):
        _record(schema_version="1")
    with pytest.raises(ValidationError):
        _record(iteration=1.0)
    with pytest.raises(ValidationError):
        _record(iteration=True)
    with pytest.raises(ValidationError):
        _record(parent_source_run_id="parent", parent_recovery_key=None, parent_record_sha256=None)
    parents = dict(complete)
    parents.update(
        {
            "parent_source_run_id": "parent-run",
            "parent_recovery_key": "9" * 64,
            "parent_record_sha256": "8" * 64,
        }
    )
    CursorInitialRecoveryRecordV1.model_validate(parents)
    validator.validate(parents)
    inconsistent = dict(parents)
    inconsistent["parent_record_sha256"] = None
    with pytest.raises(ValidationError):
        CursorInitialRecoveryRecordV1.model_validate(inconsistent)
    assert list(validator.iter_errors(inconsistent))
    empty_parent = dict(parents)
    empty_parent["parent_source_run_id"] = ""
    with pytest.raises(ValidationError):
        CursorInitialRecoveryRecordV1.model_validate(empty_parent)
    assert list(validator.iter_errors(empty_parent))
    absent_parent = dict(complete)
    absent_parent.update(
        {
            "parent_source_run_id": None,
            "parent_recovery_key": None,
            "parent_record_sha256": None,
        }
    )
    CursorInitialRecoveryRecordV1.model_validate(absent_parent)
    validator.validate(absent_parent)
    assert list(validator.iter_errors({**complete, "schema_version": "1"}))
    assert list(validator.iter_errors({**complete, "iteration": 1.5}))
    assert list(validator.iter_errors({**complete, "chat_id": "not-a-uuid"}))


def test_intent_required_fields_nulls_and_coercion() -> None:
    schema = json.loads(
        schema_path("cursor-initial-recovery-publication-intent-v1.json").read_text(
            encoding="utf-8"
        )
    )
    validator = jsonschema.Draft202012Validator(schema)
    pending = _complete_intent_payload()
    CursorInitialRecoveryPublicationIntentV1.model_validate(pending)
    validator.validate(pending)
    ready = _complete_intent_payload(status="ready")
    CursorInitialRecoveryPublicationIntentV1.model_validate(ready)
    validator.validate(ready)
    for field in schema["required"]:
        missing = dict(pending)
        del missing[field]
        with pytest.raises(ValidationError):
            CursorInitialRecoveryPublicationIntentV1.model_validate(missing)
        assert list(validator.iter_errors(missing))
    with pytest.raises(ValidationError):
        CursorInitialRecoveryPublicationIntentV1.model_validate({**ready, "record_sha256": None})
    assert list(validator.iter_errors({**ready, "record_sha256": None}))
    assert list(validator.iter_errors({**pending, "schema_version": True}))
    assert list(validator.iter_errors({**pending, "iteration": "1"}))


def test_record_hash_uses_canonical_bytes(tmp_path: Path) -> None:
    record = _record()
    path = tmp_path / "record-v1.json"
    path.write_bytes(record.canonical_bytes())
    schema = json.loads(
        schema_path("cursor-initial-recovery-record-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(json.loads(path.read_text(encoding="utf-8")))
    loaded = CursorInitialRecoveryRecordV1.model_validate_json(path.read_text(encoding="utf-8"))
    assert loaded.canonical_sha256() == record.canonical_sha256()
    assert sha256_bytes(path.read_bytes()) == record.canonical_sha256()
    parsed = json.loads(path.read_text(encoding="utf-8"))
    assert canonical_json_sha256(parsed) == record.canonical_sha256()


def test_intent_ready_requires_record_digest() -> None:
    with pytest.raises(ValidationError):
        CursorInitialRecoveryPublicationIntentV1.model_validate(
            {
                "schema_version": 1,
                "recovery_key": "e" * 64,
                "source_run_id": "source",
                "successor_run_id": "successor",
                "failed_attempt_id": "attempt",
                "dispatch_id": "dispatch",
                "status": "ready",
                "parent_recovery_key": None,
                "record_sha256": None,
                "base_prompt_path": "prompts/submitted-prompt.txt",
                "base_prompt_sha256": "a" * 64,
                "chat_owner_run_id": "source",
                "chat_artifact_path": "cursor/chat.json",
                "chat_artifact_sha256": "b" * 64,
                "chat_id": "11111111-1111-4111-8111-111111111111",
                "iteration": 1,
                "admitted_artifact_path": "admission/checkpoint.json",
                "admitted_artifact_sha256": "c" * 64,
                "plan_sha256": "d" * 64,
                "submitted_prompt_sha256": "f" * 64,
                "config_sha256": "1" * 64,
            }
        )


def test_database_permissions_ignore_sidecar_removed_during_chmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "engine.sqlite3"
    database.write_bytes(b"")
    sidecar = Path(f"{database}-shm")
    sidecar.write_bytes(b"")
    original = os.chmod

    def chmod(path: os.PathLike[str] | str, mode: int) -> None:
        if str(path).endswith("-shm"):
            sidecar.unlink(missing_ok=True)
            raise FileNotFoundError(path)
        original(path, mode)

    monkeypatch.setattr(os, "chmod", chmod)
    apply_database_permissions(database)
    assert database.stat().st_mode & 0o777 == 0o600
