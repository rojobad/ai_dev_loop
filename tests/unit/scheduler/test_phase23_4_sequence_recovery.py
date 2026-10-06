"""Phase 23.4 lineage v2, historical v1 fixtures, and schema migration."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from ai_dev_loop.paths import schema_path
from ai_dev_loop.scheduler.domain.cursor_sequence_replacement import (
    CursorSequenceReplacementIntent,
)
from ai_dev_loop.scheduler.domain.sequence_execution_replacement import (
    SequenceExecutionReplacementIntent,
)
from ai_dev_loop.scheduler.domain.sequence_run_lineage import (
    SequencePhaseExecution,
    SequenceRunAttempt,
    SequenceRunLineage,
    SequenceRunLineageValidationError,
    validate_lineage_json_schema,
)
from ai_dev_loop.scheduler.infrastructure.sqlite_store import SCHEMA_VERSION, SqliteSchedulerStore

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "phase23_4_historical"
DIGEST = "a" * 64


def _load(name: str) -> tuple[bytes, dict[str, object]]:
    raw = (FIXTURE / name).read_bytes()
    if not raw.endswith(b"\n"):
        raise AssertionError(f"{name} is missing a trailing newline")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise AssertionError(f"{name} is not an object")
    return raw[:-1], payload


def test_schema_migration_reopens_v12_without_rewriting_lineage(tmp_path: Path) -> None:
    db = tmp_path / "engine.sqlite3"
    store = SqliteSchedulerStore(db)
    store.bootstrap()
    assert SCHEMA_VERSION == 13
    with store.begin_immediate() as conn:
        conn.execute("DROP TABLE scheduler_sequence_cursor_replacements")
        conn.execute("DROP INDEX IF EXISTS idx_scheduler_sequence_cursor_replacements_sequence")
        conn.execute("DELETE FROM scheduler_schema_migrations WHERE version = 13")
        conn.execute("PRAGMA user_version = 12")
    store.bootstrap()
    with store.begin_read() as conn:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        attempts = conn.execute("SELECT COUNT(*) FROM scheduler_sequence_run_attempts").fetchone()
    assert version == 13
    assert "scheduler_sequence_cursor_replacements" in tables
    assert int(attempts[0]) == 0


def test_historical_v1_lineage_and_review_intent_load_without_regeneration() -> None:
    provenance = (FIXTURE / "provenance.txt").read_text(encoding="utf-8")
    assert "source_commit: 0203f777043e02b390500ffe46f9324f96a37428" in provenance
    assert "must not regenerate" in provenance
    lineage_bytes, lineage_payload = _load("sequence-run-lineage-v1.json")
    intent_bytes, intent_payload = _load("sequence-execution-replacement-intent-v1.json")
    assert "lineage_sha256: " in provenance
    listed = provenance.split("lineage_sha256: ", 1)[1].splitlines()[0]
    import hashlib

    assert hashlib.sha256(lineage_bytes).hexdigest() == listed
    validate_lineage_json_schema(lineage_payload)
    loaded = SequenceRunLineage.model_validate(lineage_payload)
    assert loaded.schema_version == 1
    assert loaded.phase_executions[0].attempts[1].attempt_kind == "same_reviewer_retry"
    assert loaded.model_dump(mode="json") == lineage_payload
    intent = SequenceExecutionReplacementIntent.model_validate(intent_payload)
    assert intent.schema_version == 1
    assert intent.staged_patch_sha256 == DIGEST
    assert intent.reviewer_session_id
    assert json.dumps(intent.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode() == (
        intent_bytes
    )


def test_cursor_retry_does_not_validate_as_v1_lineage() -> None:
    _, payload = _load("sequence-run-lineage-v1.json")
    phase = payload["phase_executions"]
    assert isinstance(phase, list)
    attempts = phase[0]["attempts"]  # type: ignore[index]
    assert isinstance(attempts, list)
    attempts[1]["attempt_kind"] = "cursor_retry"
    with pytest.raises(SequenceRunLineageValidationError):
        validate_lineage_json_schema(payload)
    with pytest.raises(ValidationError):
        SequenceRunLineage.model_validate(payload)


def test_v1_reader_keeps_integral_float_and_v2_rejects_lax_integers() -> None:
    historical = SequenceRunAttempt.model_validate(
        {
            "schema_version": 1.0,
            "generation": 2.0,
            "run_id": "run-review",
            "source_run_id": "run-planned",
            "attempt_kind": "same_reviewer_retry",
            "materialized_at": "2026-09-12T12:00:00.000000Z",
            "terminal_outcome": None,
            "resolved_at": None,
        }
    )
    assert historical.schema_version == 1
    assert historical.generation == 2
    with pytest.raises(ValidationError):
        SequenceRunAttempt.model_validate(
            {
                "schema_version": 2,
                "generation": True,
                "run_id": "run-cursor",
                "source_run_id": "run-planned",
                "attempt_kind": "cursor_retry",
                "materialized_at": "2026-09-12T12:00:00.000000Z",
                "terminal_outcome": None,
                "resolved_at": None,
            }
        )
    with pytest.raises(ValidationError):
        SequenceRunAttempt.model_validate(
            {
                "schema_version": 2,
                "generation": "2",
                "run_id": "run-cursor",
                "source_run_id": "run-planned",
                "attempt_kind": "cursor_retry",
                "materialized_at": "2026-09-12T12:00:00.000000Z",
                "terminal_outcome": None,
                "resolved_at": None,
            }
        )


def test_v2_cursor_lineage_round_trip_rejects_broken_chain() -> None:
    lineage = SequenceRunLineage(
        schema_version=2,
        sequence_id="seq-cursor",
        phase_executions=(
            SequencePhaseExecution(
                schema_version=2,
                ordinal=1,
                planned_run_id="run-planned",
                current_run_id="run-cursor",
                accepted_run_id=None,
                attempts=(
                    SequenceRunAttempt(
                        schema_version=2,
                        generation=1,
                        run_id="run-planned",
                        source_run_id=None,
                        attempt_kind="planned_run",
                        materialized_at="2026-09-12T12:00:00.000000Z",
                        terminal_outcome="blocked",
                        resolved_at="2026-09-12T12:00:01.000000Z",
                    ),
                    SequenceRunAttempt(
                        schema_version=2,
                        generation=2,
                        run_id="run-cursor",
                        source_run_id="run-planned",
                        attempt_kind="cursor_retry",
                        materialized_at="2026-09-12T12:00:00.000000Z",
                        terminal_outcome=None,
                        resolved_at=None,
                    ),
                ),
            ),
        ),
    )
    payload = lineage.model_dump(mode="json")
    validate_lineage_json_schema(payload)
    assert SequenceRunLineage.model_validate(payload) == lineage
    broken = json.loads(json.dumps(payload))
    broken["phase_executions"][0]["attempts"][1]["source_run_id"] = "run-other"
    with pytest.raises(ValidationError):
        SequenceRunLineage.model_validate(broken)
    with pytest.raises(SequenceRunLineageValidationError):
        validate_lineage_json_schema(broken)


def test_cursor_sequence_intent_digest_rules_reject_non_integers() -> None:
    pending = CursorSequenceReplacementIntent(
        sequence_id="seq",
        sequence_version=2,
        ordinal=1,
        source_run_id="source",
        source_generation=1,
        successor_run_id="successor",
        successor_generation=2,
        recovery_key=DIGEST,
        record_digest=None,
        entry_hash=DIGEST,
        worktree_key="worktree",
        repository_root="/tmp/repo",
        reservation_owner_run_id="successor",
        adoption_state="pending",
    )
    assert pending.publication_postcondition == "sequence_cursor_successor_dispatch_eligible"
    schema = json.loads(
        schema_path("cursor-sequence-replacement-intent-v1.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator(schema).validate(json.loads(pending.canonical_bytes()))
    adopted = pending.model_copy(update={"adoption_state": "adopted", "record_digest": DIGEST})
    jsonschema.Draft202012Validator(schema).validate(json.loads(adopted.canonical_bytes()))
    with pytest.raises(ValidationError):
        CursorSequenceReplacementIntent.model_validate(
            {**json.loads(pending.canonical_bytes()), "adoption_state": "adopted"}
        )
    with pytest.raises(ValidationError):
        CursorSequenceReplacementIntent.model_validate(
            {
                **json.loads(pending.canonical_bytes()),
                "ordinal": True,
            }
        )
    with pytest.raises(ValidationError):
        CursorSequenceReplacementIntent.model_validate(
            {
                **json.loads(pending.canonical_bytes()),
                "source_generation": "1",
            }
        )
